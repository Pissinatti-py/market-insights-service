"""curation reviewed_at: when a human approved or rejected the row

Revision ID: 20260731120000
Revises: 20260710120000
Create Date: 2026-07-31

``updated_at`` is not a usable proxy for the review moment: ``recurate_all``
rewrites the AI fields of already-reviewed rows and bumps ``updated_at`` while
deliberately preserving the human decision, so the original moment is lost.

Rows reviewed before this migration are backfilled from ``updated_at`` — an
approximation, but the best evidence that exists, and it keeps them out of the
NULL bucket (Postgres orders DESC as NULLS FIRST, which would float them to the
top of the newest-approved list).

Downgrade drops the index and the column; the backfilled values are not
recoverable afterwards.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260731120000"
down_revision: Union[str, None] = "20260710120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mi__curation", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_mi__curation_reviewed_at", "mi__curation", ["reviewed_at"])
    op.execute("UPDATE mi__curation SET reviewed_at = updated_at WHERE status IN ('approved', 'rejected')")


def downgrade() -> None:
    op.drop_index("ix_mi__curation_reviewed_at", table_name="mi__curation")
    op.drop_column("mi__curation", "reviewed_at")
