"""Run Alembic migrations from Python (used on startup and by tests)."""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from bot.config.settings import PROJECT_ROOT, Settings

log = logging.getLogger(__name__)


def alembic_config(settings: Settings) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # Passed through attributes (not the ini) so passwords never touch a config file.
    config.attributes["database_url"] = settings.database_url
    config.attributes["connect_args"] = settings.database_connect_args
    config.attributes["configure_logger"] = False
    return config


def head_revision(settings: Settings) -> str | None:
    return ScriptDirectory.from_config(alembic_config(settings)).get_current_head()


def upgrade_to_head(settings: Settings) -> None:
    """Apply all pending migrations. Must run outside a running event loop."""
    log.info("Applying database migrations (target: %s)", head_revision(settings))
    command.upgrade(alembic_config(settings), "head")
    log.info("Database schema is up to date")


async def current_revision(engine) -> str | None:
    """The revision the database is currently at (None for an empty database)."""
    from alembic.runtime.migration import MigrationContext

    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision())
