"""The duty calendar: recurring duties and a record of reminders sent.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "duties",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("owners", sa.JSON(), nullable=False),
        sa.Column("first_due", sa.Date(), nullable=False),
        sa.Column("repeat", sa.String(length=20), nullable=False),
        sa.Column("remind_days", sa.JSON(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "duty_reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("duty_id", sa.Integer(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("lead_days", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["duty_id"], ["duties.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("duty_id", "due_on", "lead_days"),
    )
    op.create_index(op.f("ix_duty_reminders_duty_id"), "duty_reminders", ["duty_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_duty_reminders_duty_id"), table_name="duty_reminders")
    op.drop_table("duty_reminders")
    op.drop_table("duties")
