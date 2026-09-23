"""Concurrent direct-post sessions.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22

Changes self_post_sessions from one row per channel to one row per
(channel, member), while keeping one active window per listed server.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_self_post_sessions_expires_at", table_name="self_post_sessions")
    op.rename_table("self_post_sessions", "self_post_sessions_old")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE self_post_sessions_old RENAME CONSTRAINT pk_self_post_sessions TO pk_self_post_sessions_old"))

    op.create_table(
        "self_post_sessions",
        sa.Column("channel_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("hub_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("listing_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("had_overwrite", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("previous_allow", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("previous_deny", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", "user_id", name=op.f("pk_self_post_sessions")),
        sa.UniqueConstraint("listing_guild_id", name="uq_self_post_sessions_listing_guild"),
    )
    op.create_index("ix_self_post_sessions_expires_at", "self_post_sessions", ["expires_at"], unique=False)
    op.create_index("ix_self_post_sessions_listing_guild_id", "self_post_sessions", ["listing_guild_id"], unique=False)

    op.execute(sa.text(
        "INSERT INTO self_post_sessions "
        "(channel_id, user_id, hub_guild_id, listing_guild_id, had_overwrite, previous_allow, previous_deny, started_at, expires_at) "
        "SELECT channel_id, user_id, hub_guild_id, listing_guild_id, had_overwrite, previous_allow, previous_deny, started_at, expires_at "
        "FROM self_post_sessions_old"
    ))
    op.drop_table("self_post_sessions_old")


def downgrade() -> None:
    op.drop_index("ix_self_post_sessions_listing_guild_id", table_name="self_post_sessions")
    op.drop_index("ix_self_post_sessions_expires_at", table_name="self_post_sessions")
    op.rename_table("self_post_sessions", "self_post_sessions_new")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE self_post_sessions_new RENAME CONSTRAINT pk_self_post_sessions TO pk_self_post_sessions_new"))

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
    op.create_index("ix_self_post_sessions_expires_at", "self_post_sessions", ["expires_at"], unique=False)
    op.execute(sa.text(
        "INSERT INTO self_post_sessions "
        "(channel_id, hub_guild_id, listing_guild_id, user_id, had_overwrite, previous_allow, previous_deny, started_at, expires_at) "
        "SELECT channel_id, hub_guild_id, listing_guild_id, user_id, had_overwrite, previous_allow, previous_deny, started_at, expires_at "
        "FROM ("
        "  SELECT s.*, ROW_NUMBER() OVER (PARTITION BY channel_id ORDER BY started_at) AS rn "
        "  FROM self_post_sessions_new AS s"
        ") ranked WHERE rn = 1"
    ))
    op.drop_table("self_post_sessions_new")
