"""Curation task orchestration: dead-letter on terminal output + round-robin batching."""

from datetime import datetime, timedelta, timezone

import pytest

from src.core.exceptions import CollectorRetriable, CollectorTerminal
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


@pytest.fixture(autouse=True)
def requeued(monkeypatch) -> list[str]:
    """Record the follow-up runs a batch queues instead of hitting the broker."""
    sent: list[str] = []
    monkeypatch.setattr(curation_tasks.celery_app, "send_task", lambda name, *a, **kw: sent.append(name))
    return sent


def _drain(requeued: list[str]) -> list[dict]:
    """Run curate_uncurated, then every follow-up it queues, like the worker would."""
    results = []
    while True:
        queued = len(requeued)
        results.append(curation_tasks.curate_uncurated.apply().get())
        if len(requeued) == queued:
            return results


def _fake_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic unit vectors: 'keep' text along x, 'drop' along y, anything else diagonal."""
    return [_KEEP if "keep" in t else _DROP if "drop" in t else _DIAGONAL for t in texts]


_KEEP, _DROP, _DIAGONAL = [1.0, 0.0], [0.0, 1.0], [0.6, 0.8]


@pytest.fixture(autouse=True)
def _offline_embeddings(monkeypatch):
    """The ranker's embedding model is an Ollama call — keep the tasks offline."""
    monkeypatch.setattr(curation_tasks.ranker, "embed", _fake_embed)


def _ok_curate(item_text, profile, context=""):
    return CurationCreate(summary="ok", tags=["x"]), {"summary": "ok"}


def test_terminal_output_dead_letters_and_is_not_reselected(db_session, monkeypatch):
    with SyncSession() as session:
        item = _seed_article(session, "Broken output")
        item_id = item.id

    def _boom(item_text, profile, context=""):
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

    monkeypatch.setattr(curation_tasks, "_acquire_lock", lambda key, ttl_seconds: False)
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

    def _racing_curate(item_text, profile, context=""):
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
        assert float(approved.importance_score) == 0.1  # the score is the ranker's — rerank_all owns it


def _seed_reviewed(session, title: str, status: CurationStatus, embedding: list[float] | None) -> Curation:
    """A past review verdict — the ranker's training material once it has an embedding."""
    article = _seed_article(session, title)
    row = Curation(
        item_type=CurationItemType.ARTICLE,
        item_id=article.id,
        summary=f"summary of {title}",
        tags=["tag-" + title],
        importance_score=0.5,
        status=status,
        reviewed_at=datetime.now(timezone.utc),
        model="old-model",
        embedding=embedding,
    )
    session.add(row)
    session.commit()
    return row


def _seed_verdicts(session, embedded: bool = True) -> None:
    """Two approved 'keep' and two rejected 'drop' items — enough contrast for the ranker."""
    for i in range(2):
        _seed_reviewed(session, f"keep-{i}", CurationStatus.APPROVED, _KEEP if embedded else None)
        _seed_reviewed(session, f"drop-{i}", CurationStatus.REJECTED, _DROP if embedded else None)


def _score_of(title: str) -> float:
    with SyncSession() as session:
        article = session.query(Article).filter(Article.title == title).one()
        row = session.query(Curation).filter(Curation.item_id == article.id).one()
        return float(row.importance_score)


def test_new_items_are_scored_by_their_reviewed_neighbours(db_session, monkeypatch):
    with SyncSession() as session:
        _seed_verdicts(session)
        _seed_article(session, "keep-fresh")
        _seed_article(session, "drop-fresh")

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _ok_curate)
    assert curation_tasks.curate_uncurated.apply().get() == {"curated": 2, "failed": 0}

    assert _score_of("keep-fresh") == 1.0  # all its nearest reviewed neighbours were approved
    assert _score_of("drop-fresh") == 0.0
    with SyncSession() as session:
        fresh = session.query(Article).filter(Article.title == "keep-fresh").one()
        stored = session.query(Curation).filter(Curation.item_id == fresh.id).one()
        assert stored.embedding == _KEEP  # kept for rerank_all and as a future example


def test_pending_rows_are_not_examples(db_session, monkeypatch):
    """Only a human verdict counts — a pending row carries no decision to learn from."""
    with SyncSession() as session:
        for i in range(2):
            _seed_reviewed(session, f"keep-undecided-{i}", CurationStatus.PENDING, _KEEP)
            _seed_reviewed(session, f"drop-{i}", CurationStatus.REJECTED, _DROP)
        _seed_article(session, "keep-fresh")

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _ok_curate)
    curation_tasks.curate_uncurated.apply().get()
    # Only rejections count, so there is no contrast: cold start = similarity to the profile.
    assert _score_of("keep-fresh") == pytest.approx(0.6)


def test_rerank_never_scores_an_item_against_its_own_verdict(db_session):
    """Otherwise every reviewed item ranks itself and the calibration report lies."""
    with SyncSession() as session:
        _seed_reviewed(session, "keep-self", CurationStatus.APPROVED, _KEEP)
        for i in range(2):
            _seed_reviewed(session, f"other-approved-{i}", CurationStatus.APPROVED, _DROP)
            _seed_reviewed(session, f"keep-rejected-{i}", CurationStatus.REJECTED, _KEEP)

    curation_tasks.rerank_all.apply().get()
    # Its own approval would pull it up; its other KEEP-side neighbours were all rejected.
    assert _score_of("keep-self") == 0.0


def test_rerank_backfills_missing_embeddings_and_rescores(db_session):
    with SyncSession() as session:
        _seed_verdicts(session, embedded=False)  # reviewed before the ranker existed
        pending = _seed_article(session, "keep-pending")
        dead = _seed_article(session, "keep-dead")
        session.add(
            Curation(
                item_type=CurationItemType.ARTICLE,
                item_id=pending.id,
                summary="old",
                importance_score=0.3,
                status=CurationStatus.PENDING,
            )
        )
        session.add(Curation(item_type=CurationItemType.ARTICLE, item_id=dead.id, status=CurationStatus.FAILED))
        session.commit()

    assert curation_tasks.rerank_all.apply().get() == {"embedded": 5, "reranked": 5, "changed": 5}
    assert _score_of("keep-pending") == 1.0
    with SyncSession() as session:
        dead_row = session.query(Curation).filter(Curation.status == CurationStatus.FAILED).one()
        assert dead_row.embedding is None and dead_row.importance_score is None  # dead letters stay unscored


def test_rerank_rewrites_only_scores_that_moved(db_session):
    """Unchanged verdicts mean an unchanged feed — no rewrite of the indexed column on every row."""
    with SyncSession() as session:
        _seed_verdicts(session)
        _seed_reviewed(session, "keep-pending", CurationStatus.PENDING, _KEEP)

    first = curation_tasks.rerank_all.apply().get()
    assert first["reranked"] == 5 and first["changed"] > 0
    assert curation_tasks.rerank_all.apply().get() == {"embedded": 0, "reranked": 5, "changed": 0}

    with SyncSession() as session:  # a new verdict moves only what it is close to
        _seed_reviewed(session, "drop-late", CurationStatus.REJECTED, _KEEP)
    assert curation_tasks.rerank_all.apply().get()["changed"] > 0
    assert _score_of("keep-pending") < 1.0


def test_rerank_skips_while_another_run_holds_the_lock(db_session, monkeypatch):
    monkeypatch.setattr(curation_tasks, "_acquire_lock", lambda key, ttl_seconds: False)
    assert curation_tasks.rerank_all.apply().get() == {"skipped": "already running"}


def test_embed_failure_costs_no_llm_call(db_session, monkeypatch):
    """The batch is embedded before any LLM call, so a missing embed model wastes no LLM work."""
    with SyncSession() as session:
        _seed_article(session, "unembeddable")

    def _embed_down(texts):
        raise CollectorRetriable("ollama embed: 404 model not found")

    llm_calls: list[str] = []
    monkeypatch.setattr(curation_tasks.ranker, "embed", _embed_down)
    monkeypatch.setattr(
        curation_tasks.curation_agent, "curate", lambda text, profile, context="": llm_calls.append(text)
    )
    assert not curation_tasks.curate_uncurated.apply().successful()
    assert llm_calls == []
    with SyncSession() as session:
        assert session.query(Curation).count() == 0


def test_empty_backlog_never_calls_ollama(db_session, monkeypatch):
    def _no_ollama(texts):
        raise AssertionError("an empty run must not embed anything")

    monkeypatch.setattr(curation_tasks.ranker, "embed", _no_ollama)
    assert curation_tasks.curate_uncurated.apply().get() == {"curated": 0, "failed": 0}


def test_batch_round_robins_across_types(db_session, monkeypatch, requeued):
    with SyncSession() as session:
        for i in range(2):
            _seed_article(session, f"art-{i}")
            _seed_repo(session, f"owner/repo-{i}")
            _seed_release(session, f"lib-{i}")

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _ok_curate)
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 3)
    result = curation_tasks.curate_uncurated.apply().get()
    assert result == {"curated": 3, "failed": 0}

    with SyncSession() as session:
        types = [c.item_type for c in session.query(Curation).all()]
        # One of each — articles cannot starve the other types.
        assert sorted(t.value for t in types) == ["article", "library_release", "repository"]
    # Three are left, so the batch queues the next run instead of looping in this one.
    assert requeued == ["src.tasks.curation_tasks.curate_uncurated"]


def _seed_aged_articles(session, titles: list[str]) -> None:
    """Seed articles oldest → newest, in list order, a day apart."""
    now = datetime.now(timezone.utc)
    for age, title in enumerate(reversed(titles)):
        _seed_article(session, title).created_at = now - timedelta(days=age)
    session.commit()


def _recording_curate(order: list[str]):
    def _curate(item_text, profile, context=""):
        order.append(item_text.split("'")[1])  # _render_article quotes the title
        return _ok_curate(item_text, profile, context)

    return _curate


def test_backlog_drains_newest_first_across_requeued_runs(db_session, monkeypatch, requeued):
    with SyncSession() as session:
        _seed_aged_articles(session, ["a1", "a2", "a3", "a4", "a5"])

    order: list[str] = []
    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _recording_curate(order))
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 2)

    # Backlog is 2.5× the batch: each run takes one batch and queues the next until empty.
    assert _drain(requeued) == [{"curated": 2, "failed": 0}, {"curated": 2, "failed": 0}, {"curated": 1, "failed": 0}]
    assert order == ["a5", "a4", "a3", "a2", "a1"]


def test_items_collected_mid_drain_jump_the_older_backlog(db_session, monkeypatch, requeued):
    with SyncSession() as session:
        _seed_aged_articles(session, ["old-1", "old-2", "old-3"])

    order: list[str] = []
    record = _recording_curate(order)

    def _curate(item_text, profile, context=""):
        if not order:  # a collector lands a fresh item while batch 1 is being curated
            with SyncSession() as session:
                _seed_article(session, "fresh")
        return record(item_text, profile, context)

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _curate)
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 2)
    _drain(requeued)

    assert order == ["old-3", "old-2", "fresh", "old-1"]


def test_items_landing_during_a_partial_batch_are_requeued(db_session, monkeypatch, requeued):
    """Their own chained run skipped on the lock — the holder's backlog re-check must catch them."""
    with SyncSession() as session:
        _seed_article(session, "only")

    def _curate(item_text, profile, context=""):
        if "'only'" in item_text:
            with SyncSession() as session:
                _seed_article(session, "late")
        return _ok_curate(item_text, profile, context)

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _curate)
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 5)

    # The batch was not full, yet "late" is still picked up by a queued follow-up.
    assert _drain(requeued) == [{"curated": 1, "failed": 0}, {"curated": 1, "failed": 0}]


def test_no_requeue_when_nothing_stores(db_session, monkeypatch, requeued):
    """Rows that never store stay uncurated — requeueing on them would spin forever."""
    with SyncSession() as session:
        _seed_aged_articles(session, ["stuck-1", "stuck-2"])

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _ok_curate)
    monkeypatch.setattr(curation_tasks, "_store", lambda session, row: False)
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 5)
    assert curation_tasks.curate_uncurated.apply().get() == {"curated": 0, "failed": 0}
    assert requeued == []


def test_decisions_made_mid_drain_reach_later_runs(db_session, monkeypatch, requeued):
    """A backlog drains over many runs — verdicts given meanwhile must rank the rest of it."""
    with SyncSession() as session:
        _seed_aged_articles(session, ["keep-a1", "keep-a2", "keep-a3"])

    def _curate(item_text, profile, context=""):
        with SyncSession() as session:
            if not session.query(Curation).filter(Curation.status == CurationStatus.APPROVED).count():
                _seed_verdicts(session)  # the user reviews while batch 1 is being curated
        return _ok_curate(item_text, profile, context)

    monkeypatch.setattr(curation_tasks.curation_agent, "curate", _curate)
    monkeypatch.setattr(curation_tasks.settings, "CURATION_BATCH_SIZE", 2)
    _drain(requeued)

    # Batch 1 had no verdicts yet (profile similarity); batch 2 is ranked by them.
    assert [_score_of(t) for t in ("keep-a3", "keep-a2", "keep-a1")] == [pytest.approx(0.6), pytest.approx(0.6), 1.0]
