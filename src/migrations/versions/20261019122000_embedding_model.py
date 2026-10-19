"""Track embedding provenance so model changes cannot mix vector spaces.

Revision ID: 20261019122000
Revises: 20260928120000
"""

import sqlalchemy as sa
from alembic import op

revision = "20261019122000"
down_revision = "20260928120000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mi__curation", sa.Column("embedding_model", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("mi__curation", "embedding_model")
