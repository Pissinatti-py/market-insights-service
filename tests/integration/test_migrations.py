"""
The Alembic chain must produce exactly the schema the models declare.

The rest of the suite builds its schema with ``Base.metadata.create_all`` (fast,
and what the fixtures need), which means a model change shipped *without* a
migration would pass every other test. This one runs the real migrations on a
scratch database and asserts autogenerate finds nothing left to do.
"""

from pathlib import Path

import psycopg2
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url

from src.core.conf import settings
from src.db.base import Base

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _recreate_scratch_db(url: str) -> None:
    """Drop and recreate the scratch database so migrations always run from zero."""
    parsed = make_url(url)
    conn = psycopg2.connect(
        host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, dbname="postgres"
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        conn.close()


@pytest.fixture
def scratch_db() -> str:
    """A freshly-created database next to the test one, e.g. ``..._test_mig``."""
    url = make_url(settings.DATABASE_URL)
    # render_as_string(hide_password=False): plain str(URL) masks the password as "***".
    scratch = url.set(database=f"{url.database}_mig").render_as_string(hide_password=False)
    try:
        _recreate_scratch_db(scratch)
    except psycopg2.Error as exc:  # no server / no CREATEDB right — same posture as conftest
        pytest.skip(f"cannot create scratch database: {exc}")
    return scratch


def test_migrations_match_the_models(scratch_db, monkeypatch):
    """``alembic upgrade head`` on an empty DB must leave autogenerate with no diff."""
    # env.py builds its engine from settings.DATABASE_URL, so this is what redirects
    # the migration run onto the scratch database.
    monkeypatch.setattr(settings, "DATABASE_URL", scratch_db)
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")

    engine = create_engine(scratch_db.replace("+asyncpg", ""))
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"models and migrations have drifted: {diff}"
