"""add mi__task_runs + hackernews article source

Revision ID: 20260702120000
Revises: 20260630120000
Create Date: 2026-07-02

Downgrade drops the table but leaves the ``hackernews`` enum value in place —
removing a Postgres enum value requires rebuilding the type and every column
using it, and a stray unused value is harmless.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260702120000"
down_revision: Union[str, None] = "20260630120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE article_source_enum ADD VALUE IF NOT EXISTS 'hackernews'")

    op.create_table(
        "mi__task_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_name", sa.String(255), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mi__task_runs_task_name", "mi__task_runs", ["task_name"])
    op.create_index("ix_mi__task_runs_finished_at", "mi__task_runs", ["finished_at"])


def downgrade() -> None:
    op.drop_table("mi__task_runs")
