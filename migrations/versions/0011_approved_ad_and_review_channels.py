"""Durable approved-but-unposted Free Listing state.

The dedicated review/rules channel IDs are runtime settings, not columns.
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column("listings", sa.Column("awaiting_ad", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("listings", sa.Column("approval_notice_message_id", sa.BigInteger(), nullable=True))

def downgrade() -> None:
    op.drop_column("listings", "approval_notice_message_id")
    op.drop_column("listings", "awaiting_ad")
