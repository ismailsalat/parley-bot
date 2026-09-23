"""Owner-posted listings ("Post It Myself").

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22

Existing listings keep working: both columns have defaults or are nullable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("self_posted", sa.Boolean(), server_default=sa.text("false"), nullable=False))
        batch_op.add_column(sa.Column("controls_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_column("controls_message_id")
        batch_op.drop_column("self_posted")
