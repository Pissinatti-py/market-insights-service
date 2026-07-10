"""
Composite "trending" signal for curation enrichment.

Gathers three momentum signals into one variable so the curator can weigh how hot
a repo/library is right now:

1. **star velocity** — ``Repository.stars_per_week`` (already collected);
2. **star growth** — the delta between the two most recent
   :class:`RepositorySnapshot` rows (catches acceleration a single velocity misses);
3. **GitHub trending** — a live GitHub search for a library's repo, so an
   established package still surfaces when its repo is spiking.

Each available signal is normalised to ``0..1`` and averaged into a single
composite score, then rendered as a text block for the LLM prompt. Best-effort:
missing snapshots or a GitHub hiccup just drop that component, never raise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.core.exceptions import CollectorError
from src.models.curation import CurationItemType
from src.models.repository import RepositorySnapshot
from src.services.collectors import github

# ponytail: fixed normalisation caps — a repo at/above the cap counts as fully
# trending on that axis. Tune here if the population shifts; not worth a setting yet.
_VELOCITY_CAP = 100.0  # stars/week
_GROWTH_CAP = 200.0  # stars gained between snapshots


def signal(item_type: CurationItemType, item, session) -> str:
    """
    Build the trending-signal text block for one item.

    :param item_type: Which entity ``item`` is.
    :type item_type: CurationItemType
    :param item: The ORM row (Repository or LibraryRelease).
    :param session: Open sync DB session (for snapshot lookups).
    :return: A ``TRENDING SIGNAL`` block, or ``""`` when nothing was gathered.
    :rtype: str
    """
    lines: list[str] = []
    scores: list[float] = []

    if item_type == CurationItemType.REPOSITORY:
        vel = float(item.stars_per_week or 0.0)
        lines.append(f"- star velocity: {vel:.1f} stars/week")
        scores.append(min(vel / _VELOCITY_CAP, 1.0))

        growth = _recent_growth(session, item.id)
        if growth is not None:
            lines.append(f"- recent growth: {growth:+d} stars since previous snapshot")
            scores.append(min(max(growth, 0) / _GROWTH_CAP, 1.0))

    elif item_type == CurationItemType.LIBRARY_RELEASE:
        top = _github_top(item.library.name)
        if top is not None:
            lines.append(
                f"- GitHub repo {top.owner}/{top.name}: {top.stars} stars, {top.stars_per_week:.1f} stars/week"
            )
            scores.append(min(top.stars_per_week / _VELOCITY_CAP, 1.0))

    if not lines:
        return ""

    composite = round(sum(scores) / len(scores), 2) if scores else 0.0
    return f"TRENDING SIGNAL (composite {composite:.2f} / 1.00):\n" + "\n".join(lines)


def _recent_growth(session, repository_id) -> int | None:
    """Stars gained between the two most recent snapshots, or ``None`` if <2 exist."""
    rows = (
        session.execute(
            select(RepositorySnapshot.stars)
            .where(RepositorySnapshot.repository_id == repository_id)
            .order_by(RepositorySnapshot.captured_at.desc())
            .limit(2)
        )
        .scalars()
        .all()
    )
    if len(rows) < 2:
        return None
    return int(rows[0] - rows[1])


def _github_top(name: str):
    """Best-effort: the top GitHub repo matching ``name`` in the last 90 days, or None."""
    if not name:
        return None
    pushed_since = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    try:
        rows = github.fetch_trending([], [name], pushed_since=pushed_since, min_stars=10, max_results=1)
    except CollectorError:
        return None
    return rows[0] if rows else None
