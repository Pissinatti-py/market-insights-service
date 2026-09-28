"""
LLM curation agent.

Talks to a local **Ollama** daemon over plain HTTP (`/api/chat`) — no provider
SDK. Given a collected item plus the user's technical profile, it returns a
JSON object with a short summary and tags. This module is the **only place** the
curation prompt lives; tweak it here, never inline it into the task.

It deliberately does **not** score: the LLM's own importance score did not
predict review verdicts (AUC 0.41 on the first 81 reviews). Ranking is the
local decision model in ``src/services/ranker.py``, learned from those verdicts.

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
    "trending signal) and the user's technical profile, describe it for that user.\n"
    "- summary: terse — what this is, then an honest verdict for this profile. Name the profile "
    "entry it touches when there is one: a stack, an area, a keyword, or a library the user "
    "monitors (monitored_libraries, written ecosystem:name — pypi:langgraph is LangGraph). Say "
    "plainly when it is routine (a patch or minor bump, a mature project with no news, low "
    "technical depth) or old news relative to TODAY. No hype, no second person.\n"
    "- Use the full article body in EXTRA CONTEXT, when present, as the primary basis "
    "for an article.\n"
    "- tags: the technologies and topics it is about.\n"
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
        "- summary: string — one or two short sentences (under ~35 words): what it is + the honest verdict\n"
        "- tags: array of short lowercase strings (technologies/topics)\n"
    )


def curate(
    item_text: str,
    profile: dict,
    context: str = "",
) -> tuple[CurationCreate, dict]:
    """
    Describe one item (summary + tags) for the profile.

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
        # No hidden reasoning: summary + tags don't need it, and qwen3's thinking was
        # ~600 tokens an item — about 10x the latency of the answer itself.
        "think": False,
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
    :raises CollectorTerminal: If validation fails (missing summary).
    """
    try:
        return CurationCreate.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError or coercion error
        raise CollectorTerminal(f"curation: invalid model output: {exc}") from exc
