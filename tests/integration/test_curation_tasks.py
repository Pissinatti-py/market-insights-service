"""Curation task orchestration: dead-letter on terminal output + round-robin batching."""

from datetime import datetime, timedelta, timezone

import pytest

from src.core.exceptions import CollectorTerminal
from src.db.session import SyncSession
from src.models.article import Article, ArticleSource
from src.models.curation import Curation, CurationItemType, CurationStatus
from src.models.library import Library, LibraryRelease, PackageEcosystem
from src.models.repository import Repository
from src.schemas.curation_schema import CurationCreate
from src.tasks import curation_tasks


def _seed_article(session, title: str) -> Article:
    article = Article(
        dedup_key=f"devto:{title}",
        source=ArticleSource.DEVTO,
        title=title,
        url=f"https://blog.example.com/{title}",
    )
    session.add(article)
    session.commit()
    return article


def _seed_repo(session, dedup_key: str) -> Repository:
    repo = Repository(
        dedup_key=dedup_key,
        url=f"https://github.com/{dedup_key}",
        owner=dedup_key.split("/")[0],
        name=dedup_key.split("/")[1],
        stars=500,
        forks=10,
        stars_per_week=12.0,
        relevance_score=9.0,
        languages=["rust"],
        topics=["cli"],
        collected_at=datetime.now(timezone.utc),
    )
    session.add(repo)
    session.commit()
    return repo


def _seed_release(session, name: str) -> LibraryRelease:
    lib = Library(dedup_key=f"pypi:{name}", ecosystem=PackageEcosystem.PYPI, name=name)
    session.add(lib)
    session.commit()
    release = LibraryRelease(
        dedup_key=f"pypi:{name}:1.0.0",
        library_id=lib.id,
        previous_version="0.9.0",
        new_version="1.0.0",
        is_major=True,
        released_at=datetime.now(timezone.utc),
    )
    session.add(release)
    session.commit()
    return release


@pytest.fixture(autouse=True)
def _no_enrichment(monkeypatch):
    """Keep the tasks offline — enrichment would fetch article bodies."""
    monkeypatch.setattr(curation_tasks, "build_context", lambda item_type, item: "")


def _ok_curate(item_text, profile, context="", feedback=None):
    return CurationCreate(summary="ok", tags=["x"], importance_score=0.5), {"summary": "ok"}


def test_terminal_output_dead_letters_and_is_not_reselected(db_session, monkeypatch):
    with SyncSession() as session:
        item = _seed_article(session, "Broken output")
        item_id = item.id

    def _boom(item_text, profile, context="", feedback=None):
        raise CollectorTerminal("bad json")

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _boom)
    result = curation_tasks.curate_uncurated.apply().get()
    assert result == {"curated": 0, "failed": 1}

    with SyncSession() as session:
        row = session.query(Curation).filter(Curation.item_id == item_id).one()
        assert row.status == CurationStatus.FAILED
        assert row.raw_llm_output == {"error": "bad json"}

    # The dead-letter row keeps the item out of the next run entirely.
    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _ok_curate)
    result = curation_tasks.curate_uncurated.apply().get()
    assert result == {"curated": 0, "failed": 0}


def test_lock_held_skips_run_entirely(db_session, monkeypatch):
    with SyncSession() as session:
        _seed_article(session, "locked-out")

    monkeypatch.setattr(curation_tasks, "_acquire_curation_lock", lambda: False)
    result = curation_tasks.curate_uncurated.apply().get()
    assert result == {"skipped": "already running"}

    with SyncSession() as session:
        assert session.query(Curation).count() == 0


def test_concurrent_duplicate_insert_does_not_kill_batch(db_session, monkeypatch):
    """A sibling run inserting the same items mid-flight is skipped per item, not a crash."""
    with SyncSession() as session:
        a1 = _seed_article(session, "raced-1")
        a2 = _seed_article(session, "raced-2")
        _seed_repo(session, "owner/safe")
        article_ids = [a1.id, a2.id]

    def _racing_curate(item_text, profile, context="", feedback=None):
        # First call: a concurrent run finishes both articles before our commits land.
        with SyncSession() as session:
            for item_id in article_ids:
                if not session.query(Curation).filter(Curation.item_id == item_id).count():
                    session.add(
                        Curation(
                            item_type=CurationItemType.ARTICLE,
                            item_id=item_id,
                            status=CurationStatus.PENDING,
                            model="sibling",
                        )
                    )
            session.commit()
        return _ok_curate(item_text, profile, context)

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _racing_curate)
    result = curation_tasks.curate_uncurated.apply().get()
    # Both articles lost the race and were skipped; the repo still curated; no crash.
    assert result == {"curated": 1, "failed": 0}

    with SyncSession() as session:
        assert session.query(Curation).count() == 3


def test_recurate_preserves_review_decisions(db_session, monkeypatch):
    with SyncSession() as session:
        reviewed = _seed_article(session, "reviewed")
        dead = _seed_article(session, "dead-letter")
        session.add(
            Curation(
                item_type=CurationItemType.ARTICLE,
                item_id=reviewed.id,
                summary="old summary",
                tags=["old"],
                importance_score=0.1,
                status=CurationStatus.APPROVED,
                model="old-model",
            )
        )
        session.add(
            Curation(
                item_type=CurationItemType.ARTICLE,
                item_id=dead.id,
                status=CurationStatus.FAILED,
                model="old-model",
            )
        )
        session.commit()
        reviewed_id, dead_id = reviewed.id, dead.id

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _ok_curate)
    result = curation_tasks.recurate_all.apply().get()
    assert result == {"recurated": 2, "skipped": 0, "missing": 0}

    with SyncSession() as session:
        approved = session.query(Curation).filter(Curation.item_id == reviewed_id).one()
        assert approved.status == CurationStatus.APPROVED  # human decision survives
        assert approved.summary == "ok"  # AI fields still refreshed
        retried = session.query(Curation).filter(Curation.item_id == dead_id).one()
        assert retried.status == CurationStatus.PENDING  # failed rows go back into review


def _seed_reviewed(session, title: str, status: CurationStatus, summary: str) -> Curation:
    """A past review decision — the material the feedback loop feeds back to the LLM."""
    article = _seed_article(session, title)
    row = Curation(
        item_type=CurationItemType.ARTICLE,
        item_id=article.id,
        summary=summary,
        tags=["tag-" + title],
        importance_score=0.5,
        status=status,
        reviewed_at=datetime.now(timezone.utc),
        model="old-model",
    )
    session.add(row)
    session.commit()
    return row


def test_review_decisions_reach_the_agent_as_feedback(db_session, monkeypatch):
    with SyncSession() as session:
        for i in range(2):
            _seed_reviewed(session, f"keep-{i}", CurationStatus.APPROVED, f"kept {i}")
            _seed_reviewed(session, f"drop-{i}", CurationStatus.REJECTED, f"dropped {i}")
        _seed_article(session, "fresh")

    seen: list[list[dict]] = []

    def _capture(item_text, profile, context="", feedback=None):
        seen.append(feedback)
        return _ok_curate(item_text, profile, context)

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _capture)
    assert curation_tasks.curate_uncurated.apply().get() == {"curated": 1, "failed": 0}

    (feedback,) = seen
    decisions = sorted(f["decision"] for f in feedback)
    assert decisions == ["approved", "approved", "rejected", "rejected"]
    assert {f["summary"] for f in feedback} == {"kept 0", "kept 1", "dropped 0", "dropped 1"}


def test_pending_and_unreviewed_rows_are_not_feedback(db_session, monkeypatch):
    """Only a human verdict counts — a pending row carries no decision to learn from."""
    with SyncSession() as session:
        pending = _seed_reviewed(session, "undecided", CurationStatus.PENDING, "no verdict")
        pending.reviewed_at = None
        session.commit()
        _seed_article(session, "fresh")

    seen: list[list[dict]] = []

    def _capture(item_text, profile, context="", feedback=None):
        seen.append(feedback)
        return _ok_curate(item_text, profile, context)

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _capture)
    curation_tasks.curate_uncurated.apply().get()
    assert seen == [[]]


def test_recurate_never_feeds_an_item_its_own_decision(db_session, monkeypatch):
    """Otherwise every approved item re-scores against itself and the calibration report lies."""
    with SyncSession() as session:
        target = _seed_reviewed(session, "self", CurationStatus.APPROVED, "the item itself")
        _seed_reviewed(session, "keep-other", CurationStatus.APPROVED, "another keeper")
        for i in range(2):
            _seed_reviewed(session, f"drop-{i}", CurationStatus.REJECTED, f"dropped {i}")
        target_item_id = target.item_id

    by_summary: dict[str, list[dict]] = {}

    def _capture(item_text, profile, context="", feedback=None):
        by_summary[item_text] = feedback
        return _ok_curate(item_text, profile, context)

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _capture)
    curation_tasks.recurate_all.apply().get()

    own_run = next(fb for text, fb in by_summary.items() if "'self'" in text)
    assert target_item_id not in [f["item_id"] for f in own_run]
    assert "the item itself" not in [f["summary"] for f in own_run]
    # The other examples still reach it — only the self-reference is dropped.
    assert "another keeper" in [f["summary"] for f in own_run]


def test_batch_round_robins_across_types(db_session, monkeypatch):
    with SyncSession() as session:
        for i in range(2):
            _seed_article(session, f"art-{i}")
            _seed_repo(session, f"owner/repo-{i}")
            _seed_release(session, f"lib-{i}")

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _ok_curate)
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 3)
    result = curation_tasks.curate_uncurated.apply().get()
    assert result == {"curated": 6, "failed": 0}

    with SyncSession() as session:
        first_page = session.query(Curation).order_by(Curation.created_at).limit(3).all()
        # One of each in the first page — articles cannot starve the other types.
        assert sorted(c.item_type.value for c in first_page) == ["article", "library_release", "repository"]


def _seed_aged_articles(session, titles: list[str]) -> None:
    """Seed articles oldest → newest, in list order, a day apart."""
    now = datetime.now(timezone.utc)
    for age, title in enumerate(reversed(titles)):
        _seed_article(session, title).created_at = now - timedelta(days=age)
    session.commit()


def _recording_curate(order: list[str]):
    def _curate(item_text, profile, context="", feedback=None):
        order.append(item_text.split("'")[1])  # _render_article quotes the title
        return _ok_curate(item_text, profile, context)

    return _curate


def test_single_run_drains_whole_backlog_newest_first(db_session, monkeypatch):
    with SyncSession() as session:
        _seed_aged_articles(session, ["a1", "a2", "a3", "a4", "a5"])

    order: list[str] = []
    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _recording_curate(order))
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 2)
    result = curation_tasks.curate_uncurated.apply().get()

    # Backlog is 2.5× the page size, yet one run curates all of it, newest first.
    assert result == {"curated": 5, "failed": 0}
    assert order == ["a5", "a4", "a3", "a2", "a1"]


def test_items_collected_mid_drain_jump_the_older_backlog(db_session, monkeypatch):
    with SyncSession() as session:
        _seed_aged_articles(session, ["old-1", "old-2", "old-3"])

    order: list[str] = []
    record = _recording_curate(order)

    def _curate(item_text, profile, context="", feedback=None):
        if not order:  # a collector lands a fresh item while page 1 is being curated
            with SyncSession() as session:
                _seed_article(session, "fresh")
        return record(item_text, profile, context)

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _curate)
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 2)
    result = curation_tasks.curate_uncurated.apply().get()

    assert result == {"curated": 4, "failed": 0}
    assert order == ["old-3", "old-2", "fresh", "old-1"]


def test_drain_stops_when_rows_never_store(db_session, monkeypatch):
    """An item whose insert keeps failing is re-selected every page — the run must end, not spin."""
    with SyncSession() as session:
        _seed_aged_articles(session, ["stuck-1", "stuck-2"])

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _ok_curate)
    monkeypatch.setattr(curation_tasks, "_store", lambda session, row: False)
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 5)
    assert curation_tasks.curate_uncurated.apply().get() == {"curated": 0, "failed": 0}
