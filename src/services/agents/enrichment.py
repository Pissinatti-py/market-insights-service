"""
Deterministic curation enrichment.

Before the LLM scores an item, gather extra evidence and fold it into one context
block appended to the prompt:

- **articles** → the full article body (``article_reader``), so scoring uses the
  whole piece, not the feed excerpt — the "consider mostly articles, full content" path;
- **repos / library releases** → recent web coverage (``web_search``) plus a
  composite trending signal (``trending``), so a common/established library still
  surfaces when a new feature, release, or news pops out.

Every tool here is best-effort — a failure drops its block, never breaks curation.
"""

from __future__ import annotations

from src.core.conf import settings
from src.models.curation import CurationItemType
from src.services.tools import article_reader, trending, web_search


def build_context(item_type: CurationItemType, item, session) -> str:
    """
    Build the extra-context block for one item.

    :param item_type: Which entity ``item`` is.
    :type item_type: CurationItemType
    :param item: The ORM row (Article, Repository, or LibraryRelease).
    :param session: Open sync DB session (used by the trending signal).
    :return: The context text, or ``""`` when nothing useful was gathered.
    :rtype: str
    """
    if item_type == CurationItemType.ARTICLE:
        return _article_context(item)
    return _tech_context(item_type, item, session)


def _article_context(item) -> str:
    """Full article body, when it can be fetched."""
    body = article_reader.read_main_content(item.url)
    return f"FULL ARTICLE CONTENT:\n{body}" if body else ""


def _tech_context(item_type: CurationItemType, item, session) -> str:
    """Related web coverage + trending signal for a repo / library release."""
    name = _item_name(item_type, item)
    parts: list[str] = []

    results = web_search.search(f"{name} release news", settings.WEB_SEARCH_MAX_RESULTS)
    if results:
        bullets = "\n".join(f"- {r['title']}: {r['snippet']}" for r in results)
        parts.append(f"RELATED WEB COVERAGE (recent news):\n{bullets}")

    trend = trending.signal(item_type, item, session)
    if trend:
        parts.append(trend)

    return "\n\n".join(parts)


def _item_name(item_type: CurationItemType, item) -> str:
    """A human name for the item, used as the search/trending key."""
    if item_type == CurationItemType.REPOSITORY:
        return f"{item.owner}/{item.name}"
    return item.library.name  # LibraryRelease
