"""
Deterministic curation enrichment.

Before the LLM scores an **article**, fetch the full body (``article_reader``)
so scoring uses the whole piece, not the feed excerpt. Repos and library
releases carry their evidence (stars, velocity, dates, notes) in the rendered
item text itself — no extra context block.

Best-effort — a fetch failure drops the block, never breaks curation.
"""

from __future__ import annotations

from src.models.curation import CurationItemType
from src.services.tools import article_reader


def build_context(item_type: CurationItemType, item) -> str:
    """
    Build the extra-context block for one item.

    :param item_type: Which entity ``item`` is.
    :type item_type: CurationItemType
    :param item: The ORM row (Article, Repository, or LibraryRelease).
    :return: The context text, or ``""`` when nothing useful was gathered.
    :rtype: str
    """
    if item_type == CurationItemType.ARTICLE:
        body = article_reader.read_main_content(item.url)
        return f"FULL ARTICLE CONTENT:\n{body}" if body else ""
    return ""
