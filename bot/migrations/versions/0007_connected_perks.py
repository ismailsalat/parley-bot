"""Auto Partner opt-in for the Parley Network.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-23

Existing rows keep working: the column has a server default.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("network_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auto_partner", sa.Boolean(), server_default=sa.text("false"), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("network_settings", schema=None) as batch_op:
        batch_op.drop_column("auto_partner")
