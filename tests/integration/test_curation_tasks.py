"""Curation task orchestration: dead-letter on terminal output + round-robin batching."""

from datetime import datetime, timezone

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
    monkeypatch.setattr(curation_tasks, "build_context", lambda item_type, item, session: "")


def _ok_curate(item_text, profile, context=""):
    return CurationCreate(summary="ok", tags=["x"], importance_score=0.5), {"summary": "ok"}


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


def test_batch_round_robins_across_types(db_session, monkeypatch):
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
