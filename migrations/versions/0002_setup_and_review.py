"""Discord-based setup: test listings, edit reviews and Test Center messages.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22

Existing rows keep working: new columns have server defaults or are nullable.
Hub/channel configuration uses the existing runtime_settings table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False))
        batch_op.add_column(sa.Column("pending_changes", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("pending_submitted_by", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("review_revision", sa.Integer(), server_default=sa.text("0"), nullable=False))
        batch_op.add_column(sa.Column("review_channel_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("review_message_id", sa.BigInteger(), nullable=True))

    op.create_table(
        "test_messages",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_messages")),
    )


def downgrade() -> None:
    op.drop_table("test_messages")
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_column("review_message_id")
        batch_op.drop_column("review_channel_id")
        batch_op.drop_column("review_revision")
        batch_op.drop_column("pending_submitted_by")
        batch_op.drop_column("pending_changes")
        batch_op.drop_column("is_test")
