"""Usage ledger for the assistant's limits, and a full-text index for keyword search.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chat_usage_user_id"), "chat_usage", ["user_id"])
    op.create_index(op.f("ix_chat_usage_created_at"), "chat_usage", ["created_at"])
    if op.get_bind().dialect.name == "postgresql":
        # Must match TSVECTOR in app/rag/retrieval.py.
        op.execute(
            "CREATE INDEX ix_document_chunks_fts ON document_chunks "
            "USING gin (to_tsvector('english', text))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_document_chunks_fts")
    op.drop_index(op.f("ix_chat_usage_created_at"), table_name="chat_usage")
    op.drop_index(op.f("ix_chat_usage_user_id"), table_name="chat_usage")
    op.drop_table("chat_usage")
