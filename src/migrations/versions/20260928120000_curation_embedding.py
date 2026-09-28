"""curation embedding — the vector the importance ranker scores from

Revision ID: 20260928120000
Revises: 20260805164116
Create Date: 2026-09-28

Nullable: existing rows are backfilled by the ``rerank_all`` task, and
dead-lettered (``failed``) rows never get one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928120000"
down_revision: Union[str, None] = "20260805164116"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mi__curation", sa.Column("embedding", postgresql.ARRAY(sa.REAL()), nullable=True))


def downgrade() -> None:
    op.drop_column("mi__curation", "embedding")
