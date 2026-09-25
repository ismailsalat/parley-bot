"""Relist edit cycle tracking.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22

Adds one nullable timestamp so Parley can allow one ad edit per Relist cycle,
and a tiny crash-recovery table for temporary direct-post permission windows.
Existing listings start with no edit used.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_ad_edit_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "self_post_sessions",
        sa.Column("channel_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("hub_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("listing_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("had_overwrite", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("previous_allow", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("previous_deny", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_self_post_sessions")),
    )
    with op.batch_alter_table("self_post_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_self_post_sessions_expires_at"), ["expires_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("self_post_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_self_post_sessions_expires_at"))
    op.drop_table("self_post_sessions")

    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_column("last_ad_edit_at")
