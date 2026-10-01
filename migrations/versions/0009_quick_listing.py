"""Allow one unverified quick listing per user, without giving them server management authority."""
from __future__ import annotations
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column("listings", sa.Column("quick_submitted_by", sa.BigInteger(), nullable=True))
    # NULL is allowed for any number of verified or pre-existing listings.
    op.create_index(op.f("ix_listings_quick_submitted_by"), "listings", ["quick_submitted_by"], unique=True)

def downgrade() -> None:
    op.drop_index(op.f("ix_listings_quick_submitted_by"), table_name="listings")
    op.drop_column("listings", "quick_submitted_by")
