"""The Alembic migrations create exactly the schema the models describe."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from bot.database import migrations
from bot.database.models import Base
from tests.conftest import make_settings


async def test_migrated_database_matches_models(db):
    def diff(connection):
        return compare_metadata(MigrationContext.configure(connection), Base.metadata)

    async with db.engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_database_is_at_head(db):
    head = migrations.head_revision(make_settings("sqlite://"))
    assert head is not None
    assert await migrations.current_revision(db.engine) == head


def test_upgrade_is_idempotent(tmp_path):
    settings = make_settings(f"sqlite:///{tmp_path / 'twice.db'}")
    migrations.upgrade_to_head(settings)
    migrations.upgrade_to_head(settings)  # running again is a no-op, never an error
