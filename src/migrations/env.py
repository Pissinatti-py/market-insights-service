import os
import sys
from datetime import datetime, timezone

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# ====================================
# Bring new models here so autogenerate sees them.
# ====================================
import src.models  # noqa
from src.core.conf import settings
from src.db.base import Base

# ====================================

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql"))

target_metadata = Base.metadata


def _process_revision_directives(context, revision, directives):
    """Generate timestamp-based revision IDs (YYYYMMDDHHmmss) for ascending order."""
    if directives:
        rev_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        for directive in directives:
            directive.rev_id = rev_id


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        process_revision_directives=_process_revision_directives,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            process_revision_directives=_process_revision_directives,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
