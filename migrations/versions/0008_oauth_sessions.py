"""Verify My Servers: short-lived OAuth sessions.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-23

Nothing existing changes: this only adds the table that lets a user prove which
servers they manage without installing Parley. No access tokens are stored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_sessions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("guilds", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_sessions")),
    )
    op.create_index(op.f("ix_oauth_sessions_state"), "oauth_sessions", ["state"], unique=True)
    op.create_index(op.f("ix_oauth_sessions_discord_user_id"), "oauth_sessions", ["discord_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_sessions_discord_user_id"), table_name="oauth_sessions")
    op.drop_index(op.f("ix_oauth_sessions_state"), table_name="oauth_sessions")
    op.drop_table("oauth_sessions")
