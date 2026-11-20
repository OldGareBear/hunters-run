"""Asking management is an email now: drop tickets, let one email have several
recipients, a copy and its own Reply-To.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_ticket_messages_ticket_id"), table_name="ticket_messages")
    op.drop_table("ticket_messages")
    op.drop_index(op.f("ix_tickets_status"), table_name="tickets")
    op.drop_table("tickets")
    with op.batch_alter_table("email_messages") as batch:
        batch.alter_column("to_email", type_=sa.Text(), existing_type=sa.String(length=320))
        batch.add_column(sa.Column("cc_email", sa.Text(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("reply_to", sa.String(length=320), nullable=False, server_default="")
        )
    # Ticket notifications still waiting to go out link to pages that no longer exist.
    op.execute("DELETE FROM email_messages WHERE kind = 'ticket' AND status <> 'sent'")


def downgrade() -> None:
    # Older code can't send to several addresses at once, so an unsent message to
    # management becomes undeliverable; mark it failed rather than let it error.
    op.execute(
        "UPDATE email_messages SET status = 'failed', last_error = 'downgraded' "
        "WHERE kind = 'escalation' AND status <> 'sent'"
    )
    op.execute("UPDATE email_messages SET to_email = substr(to_email, 1, 320)")
    with op.batch_alter_table("email_messages") as batch:
        batch.drop_column("reply_to")
        batch.drop_column("cc_email")
        batch.alter_column("to_email", type_=sa.String(length=320), existing_type=sa.Text())
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assigned_to_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tickets_status"), "tickets", ["status"], unique=False)
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ticket_messages_ticket_id"), "ticket_messages", ["ticket_id"], unique=False
    )
