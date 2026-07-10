"""
Full-article reader for curation enrichment.

Fetches an article's page over ``httpx`` and extracts just the **main content**
(no nav/ads/comments) with ``trafilatura``. Curation feeds this to the LLM so an
article is judged on its full body, not the thin feed excerpt.

Best-effort by contract: any fetch or extraction failure returns ``None`` so
curation falls back to the excerpt already on the row.
"""

from __future__ import annotations

import httpx
import trafilatura

from src.core.conf import settings

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def read_main_content(url: str) -> str | None:
    """
    Fetch ``url`` and return its main body text, truncated to ``ARTICLE_MAX_CHARS``.

    :param url: The article URL.
    :type url: str
    :return: Extracted main text, or ``None`` if the page can't be fetched/parsed.
    :rtype: str | None
    """
    if not url:
        return None
    try:
        with httpx.Client(
            timeout=settings.COLLECTOR_HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError):
        return None

    text = trafilatura.extract(resp.text, include_comments=False, include_tables=False)
    if not text:
        return None
    return text[: settings.ARTICLE_MAX_CHARS]
