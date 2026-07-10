"""
Keyless web-search tool for curation enrichment.

Scrapes DuckDuckGo's HTML endpoint over ``httpx`` (no API key, no SDK) and returns
the top result titles + snippets. Curation uses this to surface *recent coverage*
of a library/repo — the signal that an established package is newsworthy again
because a new feature or release just landed.

Best-effort by contract: any network or parse failure returns ``[]`` so a flaky
search never breaks a curation run.
"""

from __future__ import annotations

import html
import re

import httpx

from src.core.conf import settings

_URL = "https://html.duckduckgo.com/html/"
# A browser-ish UA — DDG serves an empty body to a blank/absent User-Agent.
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# ponytail: regex-scrapes DDG's lite result markup; swap for a keyed provider
# (Tavily/Brave) if this layout breaks. Failures already degrade to [] (best-effort).
_TITLE_RE = re.compile(r'class="result__a"[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def search(query: str, max_results: int | None = None) -> list[dict]:
    """
    Return up to ``max_results`` web results for ``query``.

    :param query: The search query.
    :type query: str
    :param max_results: Cap on results (defaults to ``WEB_SEARCH_MAX_RESULTS``).
    :type max_results: int | None
    :return: ``[{"title": str, "snippet": str}, ...]`` — empty on any failure.
    :rtype: list[dict]
    """
    if not settings.ENABLE_WEB_SEARCH or not query.strip():
        return []
    limit = max_results or settings.WEB_SEARCH_MAX_RESULTS
    try:
        with httpx.Client(timeout=settings.COLLECTOR_HTTP_TIMEOUT_SECONDS, headers={"User-Agent": _UA}) as client:
            resp = client.get(_URL, params={"q": query})
            resp.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError):
        return []
    return _parse(resp.text, limit)


def _clean(fragment: str) -> str:
    """Strip inner HTML tags and unescape entities from a captured fragment."""
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _parse(body: str, limit: int) -> list[dict]:
    """Pair result titles with their snippets from a DDG HTML response."""
    titles = _TITLE_RE.findall(body)
    snippets = _SNIPPET_RE.findall(body)
    results: list[dict] = []
    for title, snippet in zip(titles, snippets):
        t = _clean(title)
        if t:
            results.append({"title": t, "snippet": _clean(snippet)})
        if len(results) >= limit:
            break
    return results
