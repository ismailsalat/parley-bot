"""Shared fixtures.

Every business-logic test runs against a database created by the real Alembic
migrations (not ``create_all``), so the tests also prove the migrations and the
database constraints work.

* default:            a fresh SQLite file per test
* TEST_DATABASE_URL:  the same suite against PostgreSQL (tables truncated per test)
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from bot.config.runtime import RuntimeConfig, default_config
from bot.config.settings import Settings, normalize_database_url
from bot.database.migrations import upgrade_to_head
from bot.database.models import Base
from bot.database.session import Database

POSTGRES_URL = os.getenv("TEST_DATABASE_URL", "").strip()


def make_settings(database_url: str, **overrides) -> Settings:
    url, connect_args = normalize_database_url(database_url)
    return replace(Settings(discord_token="", database_url=url, database_connect_args=connect_args), **overrides)


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path | str:
    """Run the migrations once per test session."""
    if POSTGRES_URL:
        upgrade_to_head(make_settings(POSTGRES_URL))
        return POSTGRES_URL
    path = tmp_path_factory.mktemp("template") / "template.db"
    upgrade_to_head(make_settings(f"sqlite:///{path}"))
    return path


@pytest.fixture
async def db(migrated_template: Path | str, tmp_path: Path) -> AsyncIterator[Database]:
    if POSTGRES_URL:
        settings = make_settings(POSTGRES_URL)
        database = Database(settings.database_url, settings.database_connect_args)
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with database.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    else:
        path = tmp_path / "test.db"
        shutil.copy(migrated_template, path)
        database = Database(normalize_database_url(f"sqlite:///{path}")[0])
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
def config() -> RuntimeConfig:
    return default_config()
