"""Alembic environment.

Works two ways:
* from the bot (bot.database.migrations), which passes the URL via config.attributes
* from the command line (`alembic upgrade head` / migrate.bat), which reads
  DATABASE_URL the same way the bot does.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from bot.config.settings import describe_database, load_settings
from bot.database.models import Base, UTCDateTime

config = context.config
target_metadata = Base.metadata

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)


def database_settings() -> tuple[str, dict]:
    url = config.attributes.get("database_url")
    if url:
        return url, config.attributes.get("connect_args") or {}
    settings = load_settings()
    print(f"Using database: {describe_database(settings.database_url)}")
    return settings.database_url, settings.database_connect_args


def run_migrations_offline() -> None:
    url, _ = database_settings()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def render_item(type_: str, obj, autogen_context):
    """Render Parley's UTCDateTime as a plain DateTime so migrations stay import-free."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        render_item=render_item,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite needs batch mode for ALTERs
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url, connect_args = database_settings()
    engine = create_async_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
