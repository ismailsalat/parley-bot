"""Private paste recovery tracking and timed suspensions."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("suspended_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("listings", sa.Column("suspended_from_status", sa.String(20), nullable=True))
    op.create_table(
        "temporary_ad_sessions",
        sa.Column("channel_id", sa.BigInteger(), primary_key=True),
        sa.Column("hub_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("advertised_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_temporary_ad_sessions_user_id", "temporary_ad_sessions", ["user_id"], unique=True)
    op.create_index("ix_temporary_ad_sessions_expires_at", "temporary_ad_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_temporary_ad_sessions_expires_at", table_name="temporary_ad_sessions")
    op.drop_index("ix_temporary_ad_sessions_user_id", table_name="temporary_ad_sessions")
    op.drop_table("temporary_ad_sessions")
    op.drop_column("listings", "suspended_from_status")
    op.drop_column("listings", "suspended_until")
