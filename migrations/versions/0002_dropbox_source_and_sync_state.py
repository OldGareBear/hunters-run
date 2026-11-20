"""Documents can come from a Dropbox folder; track sync state.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_state",
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_summary", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("source"),
    )
    with op.batch_alter_table("documents") as batch:
        batch.add_column(
            sa.Column("source", sa.String(length=20), server_default="upload", nullable=False)
        )
        batch.add_column(sa.Column("source_id", sa.String(length=200), nullable=True))
        batch.add_column(
            sa.Column("source_path", sa.String(length=1000), server_default="", nullable=False)
        )
        batch.add_column(
            sa.Column("source_rev", sa.String(length=100), server_default="", nullable=False)
        )
        batch.create_unique_constraint("uq_documents_source_id", ["source_id"])


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_constraint("uq_documents_source_id", type_="unique")
        batch.drop_column("source_rev")
        batch.drop_column("source_path")
        batch.drop_column("source_id")
        batch.drop_column("source")
    op.drop_table("sync_state")
