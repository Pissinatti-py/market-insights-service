"""
LLM curation agent.

Talks to a local **Ollama** daemon over plain HTTP (`/api/chat`) — no provider
SDK. Given a collected item plus the user's technical profile, it returns a
JSON object with a short summary, tags, and an importance score. This module is
the **only place** the curation prompt lives; tweak it here, never inline it
into the task.

The score is a *decision*, not a number the model invents: the model picks one of
four closed bands, and the score is the band anchors weighted by the model's own
token probabilities over those bands (a free float drifted into the top band).
Without usable logprobs it falls back to the picked band's anchor.

The result is validated through :class:`CurationCreate` before it is trusted —
invalid output is a terminal failure (no row written), mirroring how pissync
validates LLM output through the create-schema.
"""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Literal

import httpx
from pydantic import BaseModel

from src.core.conf import settings
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.schemas.curation_schema import CurationCreate

#: Closed score bands → the importance_score each one anchors to (midpoints of the rubric
#: ranges). Their first tokens (noise/routine/relevant/must) must stay distinct: the scorer
#: reads the band's probability off the first token only.
_BANDS: dict[str, float] = {"noise": 0.20, "routine": 0.55, "relevant": 0.80, "must_see": 0.95}

#: Top alternatives requested per token — enough to see all four bands at the band position.
_TOP_LOGPROBS = 10


class _Decision(BaseModel):
    """
    Output contract sent to Ollama as ``format``.

    ``band`` comes **first**, so the pick is judged on the item itself rather than anchored
    by the model's own (often hyped) summary.
    """

    band: Literal["noise", "routine", "relevant", "must_see"]
    summary: str
    tags: list[str] = []


_SYSTEM_PROMPT = (
    "You are a senior software-engineering analyst curating tech-market signals for a "
    "team that also publishes about them. Given one item (a repository, a library "
    "release, or an article) plus optional EXTRA CONTEXT (a full article body or a "
    "trending signal) and the user's technical profile, judge how relevant and "
    "important it is.\n"
    "Weigh these heavily:\n"
    "- News and publishable topics — an article, announcement, or development worth "
    "writing about scores high even if it is not a brand-new library.\n"
    "- A common or already-used library is highly relevant when a NEW feature, release, "
    "or related news pops out around it — established does not mean unimportant.\n"
    "- A library the user explicitly monitors (monitored_libraries in the profile) is a "
    "strong relevance signal for its releases.\n"
    "- Use the full article body in EXTRA CONTEXT, when present, as the primary basis "
    "for an article.\n"
    "- Recency matters: news older than ~2 weeks relative to TODAY is stale — pick noise "
    "unless it is a durable reference/resource.\n"
    "Pick exactly one band — spread picks across all four; do NOT default to the top band:\n"
    "- must_see: exceptional, must-see for this profile (rare — a handful per week at most)\n"
    "- relevant: clearly relevant news or release worth reading soon\n"
    "- routine: solid but routine; fine to batch-read later\n"
    "- noise: marginal or noise for this profile\n"
    "When the user message carries a REVIEW FEEDBACK block, it is ground truth from THIS user — "
    "real items they kept or discarded. It outranks the generic anchor examples below wherever the "
    "two disagree; match the taste it shows, not just the topic overlap.\n"
    "Anchor examples (calibrate against these):\n"
    "- must_see: \"Major framework in the user's stack ships a breaking major release with a "
    'migration guide" — act soon.\n'
    '- relevant: "A library the user monitors ships a minor release with a genuinely useful '
    'new feature" — worth reading this week.\n'
    '- routine: "Competent tutorial covering a topic in the user\'s stack, nothing novel" — '
    "batch-read later.\n"
    '- noise: "Generic listicle or old news resurfaced, unrelated to the profile".\n'
    "Respond with ONLY a JSON object — no prose before or after."
)


#: Minimum examples on *each* side before feedback is used. One-sided feedback has no
#: contrast — a wall of approvals just ratchets every score upward — so below this the
#: block is dropped and the static anchor examples carry the calibration alone.
_MIN_PER_SIDE = 2


def _feedback_block(feedback: list[dict] | None) -> str:
    """
    Render past review decisions as few-shot examples of this user's taste.

    Each example is an already-curated item: the LLM's own summary and tags for it,
    the score it got, and the verdict the human then gave. Returns ``""`` when
    either side is under :data:`_MIN_PER_SIDE`.

    :param feedback: Dicts with ``decision``/``summary``/``tags``/``score``.
    :type feedback: list[dict] | None
    :return: The prompt block, or ``""`` when there is not enough signal.
    :rtype: str
    """
    if not feedback:
        return ""
    approved = [f for f in feedback if f["decision"] == "approved"]
    rejected = [f for f in feedback if f["decision"] == "rejected"]
    if len(approved) < _MIN_PER_SIDE or len(rejected) < _MIN_PER_SIDE:
        return ""

    def _lines(rows: list[dict]) -> str:
        return "\n".join(
            f"- [{', '.join(r['tags']) or 'no tags'}] {r['summary']} (you scored it {r['score']:.2f})" for r in rows
        )

    return (
        "REVIEW FEEDBACK — this user's own past decisions on this same feed:\n"
        f"APPROVED (kept — score items like these HIGH):\n{_lines(approved)}\n"
        f"REJECTED (discarded — score items like these LOW):\n{_lines(rejected)}\n\n"
    )


def _user_prompt(item_text: str, profile: dict, context: str = "", feedback: list[dict] | None = None) -> str:
    """Build the per-item user prompt embedding the profile, feedback, item, context, and contract."""
    extra = f"EXTRA CONTEXT:\n{context}\n\n" if context else ""
    return (
        f"TODAY: {date.today().isoformat()}\n\n"
        f"USER PROFILE (stacks/areas/keywords/monitored_libraries):\n{json.dumps(profile, ensure_ascii=False)}\n\n"
        f"{_feedback_block(feedback)}"
        f"ITEM:\n{item_text}\n\n"
        f"{extra}"
        "Return a JSON object with exactly these keys, in this order:\n"
        "- band: one of noise | routine | relevant | must_see (see the rubric)\n"
        "- summary: string — one or two sentences on what this is and why it matters to the user\n"
        "- tags: array of short lowercase strings (technologies/topics)\n"
    )


def curate(
    item_text: str,
    profile: dict,
    context: str = "",
    feedback: list[dict] | None = None,
) -> tuple[CurationCreate, dict]:
    """
    Curate one item against the profile.

    :param item_text: A compact text rendering of the collected item.
    :type item_text: str
    :param profile: The user's preferences (stacks/areas/keywords).
    :type profile: dict
    :param context: Optional enrichment (full article body / web coverage / trending).
    :type context: str
    :param feedback: Past approve/reject decisions, used as few-shot taste examples.
    :type feedback: list[dict] | None
    :return: ``(validated_curation, raw_model_output)`` — the raw output also carries
        ``band_probs`` and ``score_source`` (``"logprobs"`` or ``"label"``).
    :rtype: tuple[CurationCreate, dict]
    :raises CollectorRetriable: Ollama unreachable / 5xx (the task retries).
    :raises CollectorTerminal: Output that won't validate after the call.
    """
    parsed, logprobs = _call_ollama(_SYSTEM_PROMPT, _user_prompt(item_text, profile, context, feedback))
    return _coerce(parsed, logprobs)


def _call_ollama(system: str, user: str) -> tuple[dict, list[dict]]:
    """
    POST to Ollama's /api/chat in JSON mode; return the parsed message object and its token logprobs.

    :param system: The system prompt.
    :type system: str
    :param user: The user prompt.
    :type user: str
    :return: ``(parsed_json_object, logprobs)`` — ``logprobs`` is ``[]`` when the daemon omits them.
    :rtype: tuple[dict, list[dict]]
    :raises CollectorRetriable: Daemon down, timeout, or 5xx.
    :raises CollectorTerminal: Empty or non-JSON content.
    """
    payload = {
        "model": settings.OLLAMA_MODEL,
        "stream": False,
        # Constrain generation to the output contract, not just "some JSON" —
        # Ollama accepts a JSON schema here, which kills most terminal
        # validation failures at the source.
        "format": _Decision.model_json_schema(),
        # Per-token alternatives, so the band pick can be weighted by the model's own confidence.
        "logprobs": True,
        "top_logprobs": _TOP_LOGPROBS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": 0.1},
    }
    try:
        with httpx.Client(timeout=settings.OLLAMA_REQUEST_TIMEOUT_SECONDS) as client:
            resp = client.post(f"{settings.OLLAMA_BASE_URL}/api/chat", json=payload)
            resp.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        raise CollectorRetriable(f"ollama: {type(exc).__name__}: {exc}") from exc

    body = resp.json()
    content = body.get("message", {}).get("content", "")
    if not content:
        raise CollectorTerminal("ollama: empty message content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CollectorTerminal(f"ollama: response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CollectorTerminal("ollama: response JSON was not an object")
    return parsed, body.get("logprobs") or []


def _band_position(logprobs: list[dict]) -> dict | None:
    """The token entry where the ``band`` value starts, or ``None`` when it can't be located."""
    text = ""
    for i, entry in enumerate(logprobs):
        text += entry.get("token", "")
        # Grammar-constrained JSON: the value's opening quote closes the `"band": "` prefix.
        # Quotes inside the summary are escaped (\"), so they can't fake this match.
        if text.replace(" ", "").endswith('"band":"'):
            return logprobs[i + 1] if i + 1 < len(logprobs) else None
    return None


def _band_score(logprobs: list[dict], band: str) -> tuple[float, dict[str, float], str]:
    """
    Turn the picked band into a score, weighted by the model's probability over all bands.

    Ollama's ``top_logprobs`` are taken before the grammar mask, so they include tokens the
    schema would reject (``"Routine"``, ``"D"``): only band first-tokens count, case-merged,
    renormalized among themselves.

    :return: ``(score, band_probs, source)`` — source is ``"logprobs"``, or ``"label"`` when
        the band position or its alternatives are missing and the picked band's anchor is used.
    """
    first_tokens = {name.split("_")[0]: name for name in _BANDS}
    position = _band_position(logprobs)
    mass: dict[str, float] = {}
    for alt in (position or {}).get("top_logprobs", []):
        name = first_tokens.get(alt.get("token", "").strip().lower())
        if name:
            mass[name] = mass.get(name, 0.0) + math.exp(alt["logprob"])
    total = sum(mass.values())
    if total <= 0:
        return _BANDS[band], {band: 1.0}, "label"
    probs = {name: p / total for name, p in mass.items()}
    return sum(_BANDS[name] * p for name, p in probs.items()), probs, "logprobs"


def _coerce(parsed: dict, logprobs: list[dict]) -> tuple[CurationCreate, dict]:
    """
    Validate the model's decision and turn it into a :class:`CurationCreate`.

    :param parsed: The parsed JSON object from the model (``summary``/``tags``/``band``).
    :type parsed: dict
    :param logprobs: Token logprobs from the same response (may be empty).
    :type logprobs: list[dict]
    :return: ``(validated_curation, raw)`` — raw is the model output plus the band probabilities.
    :rtype: tuple[CurationCreate, dict]
    :raises CollectorTerminal: If validation fails (missing summary, unknown band).
    """
    try:
        decision = _Decision.model_validate(parsed)
        score, probs, source = _band_score(logprobs, decision.band)
        curation = CurationCreate(summary=decision.summary, tags=decision.tags, importance_score=round(score, 3))
    except Exception as exc:  # pydantic ValidationError or coercion error
        raise CollectorTerminal(f"curation: invalid model output: {exc}") from exc
    return curation, {**parsed, "band_probs": probs, "score_source": source}
