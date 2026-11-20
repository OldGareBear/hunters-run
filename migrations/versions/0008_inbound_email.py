"""The inbox people cc: emails read from it for the weekly review.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inbound_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=500), nullable=False),
        sa.Column("from_email", sa.String(length=320), nullable=False),
        sa.Column("from_name", sa.String(length=200), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("audience", sa.String(length=20), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    op.create_index(op.f("ix_inbound_emails_sender_id"), "inbound_emails", ["sender_id"])
    op.create_index(op.f("ix_inbound_emails_received_at"), "inbound_emails", ["received_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_inbound_emails_received_at"), table_name="inbound_emails")
    op.drop_index(op.f("ix_inbound_emails_sender_id"), table_name="inbound_emails")
    op.drop_table("inbound_emails")
