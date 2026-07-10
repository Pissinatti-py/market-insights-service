"""
LLM curation agent.

Talks to a local **Ollama** daemon over plain HTTP (`/api/chat`) — no provider
SDK. Given a collected item plus the user's technical profile, it returns a
JSON object with a short summary, tags, and an importance score. This module is
the **only place** the curation prompt lives; tweak it here, never inline it
into the task.

The model's raw JSON is coerced and validated through :class:`CurationCreate`
before it is trusted — invalid output is a terminal failure (no row written),
mirroring how pissync validates LLM output through the create-schema.
"""

from __future__ import annotations

import json
from datetime import date

import httpx

from src.core.conf import settings
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.schemas.curation_schema import CurationCreate

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
    "- Recency matters: news older than ~2 weeks relative to TODAY is stale — cap it at "
    "0.4 unless it is a durable reference/resource.\n"
    "Calibrate importance_score against this rubric — spread scores across the full "
    "range; do NOT default to the top band:\n"
    "- 0.9-1.0: exceptional, must-see for this profile (rare — a handful per week at most)\n"
    "- 0.7-0.9: clearly relevant news or release worth reading soon\n"
    "- 0.4-0.7: solid but routine; fine to batch-read later\n"
    "- below 0.4: marginal or noise for this profile\n"
    "Anchor examples (calibrate against these):\n"
    "- 0.95: \"Major framework in the user's stack ships a breaking major release with a "
    'migration guide" — must-see, act soon.\n'
    '- 0.75: "A library the user monitors ships a minor release with a genuinely useful '
    'new feature" — worth reading this week.\n'
    '- 0.55: "Competent tutorial covering a topic in the user\'s stack, nothing novel" — '
    "batch-read later.\n"
    '- 0.20: "Generic listicle or old news resurfaced, unrelated to the profile" — noise.\n'
    "Respond with ONLY a JSON object — no prose before or after."
)


def _user_prompt(item_text: str, profile: dict, context: str = "") -> str:
    """Build the per-item user prompt embedding the profile, item, context, and contract."""
    extra = f"EXTRA CONTEXT:\n{context}\n\n" if context else ""
    return (
        f"TODAY: {date.today().isoformat()}\n\n"
        f"USER PROFILE (stacks/areas/keywords/monitored_libraries):\n{json.dumps(profile, ensure_ascii=False)}\n\n"
        f"ITEM:\n{item_text}\n\n"
        f"{extra}"
        "Return a JSON object with exactly these keys:\n"
        "- summary: string — one or two sentences on what this is and why it matters to the user\n"
        "- tags: array of short lowercase strings (technologies/topics)\n"
        "- importance_score: number between 0 and 1 (1 = must-see for this profile)\n"
    )


def curate(item_text: str, profile: dict, context: str = "") -> tuple[CurationCreate, dict]:
    """
    Curate one item against the profile.

    :param item_text: A compact text rendering of the collected item.
    :type item_text: str
    :param profile: The user's preferences (stacks/areas/keywords).
    :type profile: dict
    :param context: Optional enrichment (full article body / web coverage / trending).
    :type context: str
    :return: ``(validated_curation, raw_model_output)``.
    :rtype: tuple[CurationCreate, dict]
    :raises CollectorRetriable: Ollama unreachable / 5xx (the task retries).
    :raises CollectorTerminal: Output that won't validate after the call.
    """
    raw = _call_ollama(_SYSTEM_PROMPT, _user_prompt(item_text, profile, context))
    return _coerce(raw), raw


def _call_ollama(system: str, user: str) -> dict:
    """
    POST to Ollama's /api/chat in JSON mode and return the parsed message object.

    :param system: The system prompt.
    :type system: str
    :param user: The user prompt.
    :type user: str
    :return: The parsed JSON object the model returned.
    :rtype: dict
    :raises CollectorRetriable: Daemon down, timeout, or 5xx.
    :raises CollectorTerminal: Empty or non-JSON content.
    """
    payload = {
        "model": settings.OLLAMA_MODEL,
        "stream": False,
        # Constrain generation to the output contract, not just "some JSON" —
        # Ollama accepts a JSON schema here, which kills most terminal
        # validation failures at the source.
        "format": CurationCreate.model_json_schema(),
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

    content = resp.json().get("message", {}).get("content", "")
    if not content:
        raise CollectorTerminal("ollama: empty message content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CollectorTerminal(f"ollama: response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CollectorTerminal("ollama: response JSON was not an object")
    return parsed


def _coerce(raw: dict) -> CurationCreate:
    """
    Validate the raw model output through :class:`CurationCreate`.

    :param raw: The parsed JSON object from the model.
    :type raw: dict
    :return: The validated curation.
    :rtype: CurationCreate
    :raises CollectorTerminal: If validation fails (missing summary, bad score).
    """
    try:
        return CurationCreate.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError or coercion error
        raise CollectorTerminal(f"curation: invalid model output: {exc}") from exc
