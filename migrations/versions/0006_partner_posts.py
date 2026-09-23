"""Independent owner-authored partner posts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("partner_ad_text", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("partner_channel_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("partner_message_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("partner_controls_message_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("partner_posted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_column("partner_posted_at")
        batch_op.drop_column("partner_controls_message_id")
        batch_op.drop_column("partner_message_id")
        batch_op.drop_column("partner_channel_id")
        batch_op.drop_column("partner_ad_text")
