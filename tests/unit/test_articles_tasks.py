"""Unit tests for the article task helpers (age gate + near-dup collapse)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.models.article import ArticleSource
from src.schemas.article_schema import ArticleCreate
from src.services.collectors.articles import title_fingerprint
from src.tasks.articles_tasks import _collapse_near_dups, _drop_stale

_NOW = datetime(2026, 7, 10, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=14)


def _row(
    title: str, likes: int = 0, comments: int = 0, published_at: datetime | None = _NOW, url: str = ""
) -> ArticleCreate:
    return ArticleCreate(
        dedup_key=f"devto:{title}",
        source=ArticleSource.DEVTO,
        title=title,
        title_fingerprint=title_fingerprint(title),
        url=url or f"https://example.com/{title}",
        likes=likes,
        comments=comments,
        published_at=published_at,
    )


class _FakeSession:
    """Session stub whose fingerprint lookup returns a fixed set."""

    def __init__(self, existing: set[str] | None = None):
        self._existing = existing or set()

    def execute(self, query):
        return SimpleNamespace(scalars=lambda: iter(self._existing))


def test_drop_stale_gates_on_published_at():
    fresh = _row("fresh", published_at=_NOW)
    stale = _row("stale", published_at=_NOW - timedelta(days=30))
    undated = _row("undated", published_at=None)
    kept = _drop_stale([fresh, stale, undated], _CUTOFF)
    assert kept == [fresh, undated]  # undated passes — the LLM date-caps it


def test_collapse_keeps_highest_engagement_in_batch():
    low = _row("Same Story", likes=1)
    high = _row("Show HN: Same Story!", likes=100)  # same fingerprint after normalization
    other = _row("Different Story")
    kept = _collapse_near_dups(_FakeSession(), [low, high, other])
    assert high in kept and other in kept
    assert low not in kept


def test_collapse_drops_fingerprints_already_stored():
    row = _row("Already Seen")
    session = _FakeSession(existing={title_fingerprint("Already Seen")})
    assert _collapse_near_dups(session, [row]) == []
