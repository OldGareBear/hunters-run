"""Amenity reservations, and a unit (household) on each account.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("unit", sa.String(length=80), server_default="", nullable=False))
    with op.batch_alter_table("invites") as batch:
        batch.add_column(sa.Column("unit", sa.String(length=80), server_default="", nullable=False))
    op.create_table(
        "reservations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("amenity_group", sa.String(length=60), nullable=False),
        sa.Column("space", sa.String(length=60), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("household", sa.String(length=100), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=False),
        sa.Column("cancelled_by_id", sa.Integer(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=False),
        sa.Column("counts_after_cancel", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cancelled_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reservations_status", "reservations", ["status"])
    op.create_index("ix_reservations_space_starts", "reservations", ["space", "starts_at"])
    op.create_index(
        "ix_reservations_group_household",
        "reservations",
        ["amenity_group", "household", "starts_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_reservations_group_household", table_name="reservations")
    op.drop_index("ix_reservations_space_starts", table_name="reservations")
    op.drop_index("ix_reservations_status", table_name="reservations")
    op.drop_table("reservations")
    with op.batch_alter_table("invites") as batch:
        batch.drop_column("unit")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("unit")
