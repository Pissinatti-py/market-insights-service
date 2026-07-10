"""curation quality: failed status, article title fingerprint, drop constant confidence

Revision ID: 20260710120000
Revises: 20260702120000
Create Date: 2026-07-10

Downgrade re-adds ``confidence`` (nullable, no backfill) and drops the
fingerprint column, but leaves the ``failed`` enum value in place — removing a
Postgres enum value requires rebuilding the type and every column using it, and
a stray unused value is harmless (same precedent as ``hackernews``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260710120000"
down_revision: Union[str, None] = "20260702120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Dead-letter status for items whose LLM output never validates.
    op.execute("ALTER TYPE curation_status_enum ADD VALUE IF NOT EXISTS 'failed'")

    # Near-duplicate collapsing across sources; old rows stay NULL (already curated).
    op.add_column("mi__articles", sa.Column("title_fingerprint", sa.String(64), nullable=True))
    op.create_index("ix_mi__articles_title_fingerprint", "mi__articles", ["title_fingerprint"])

    # confidence was a hardcoded constant (0.85) — zero information.
    op.drop_column("mi__curation", "confidence")


def downgrade() -> None:
    op.add_column("mi__curation", sa.Column("confidence", sa.Numeric(4, 3), nullable=True))
    op.drop_index("ix_mi__articles_title_fingerprint", table_name="mi__articles")
    op.drop_column("mi__articles", "title_fingerprint")
