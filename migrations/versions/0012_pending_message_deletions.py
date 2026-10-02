"""Durable queue for failed Discord message deletions."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_message_deletions",
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", "message_id", name="pk_pending_message_deletions"),
    )
    op.create_index("ix_pending_message_deletions_guild_id", "pending_message_deletions", ["guild_id"])


def downgrade() -> None:
    op.drop_index("ix_pending_message_deletions_guild_id", table_name="pending_message_deletions")
    op.drop_table("pending_message_deletions")
