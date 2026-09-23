"""Entry point: ``python -m bot.main`` (locally, in Docker and on Railway).

Startup order:
1. load settings            2. configure logging
3. apply database migrations (or verify the schema is current)
4. connect to the database   5. register persistent views + commands (setup_hook)
6. connect to Discord        7. restore panels, validate channels (on_ready)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import aiohttp
import discord

from bot import __version__
from bot.config.settings import Settings, SettingsError, describe_database, load_settings
from bot.database import migrations
from bot.database.session import Database
from bot.startup_errors import describe_database_error
from bot.utils.logging import configure_logging, details_logger

log = logging.getLogger("bot.main")

EXIT_CONFIG_ERROR = 2
EXIT_STARTUP_ERROR = 1


def install_signal_handlers(bot: discord.Client) -> None:
    """Railway/Docker send SIGTERM on redeploy; close cleanly instead of being killed.

    Windows has no loop signal handlers; CTRL+C there raises KeyboardInterrupt,
    which asyncio.run turns into task cancellation, so cleanup still runs.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _request_shutdown(bot, s))
        except (NotImplementedError, RuntimeError):
            return


def _request_shutdown(bot: discord.Client, sig: signal.Signals) -> None:
    log.info("Received %s", sig.name)
    asyncio.create_task(bot.close(), name="waypoint-shutdown")


def fatal(settings: Settings, problem: str, hint: str, exc: BaseException | None = None) -> None:
    """One clean line on the console; the traceback only goes to the log file (or DEBUG console)."""
    where = " Details: logs/parley.log" if settings.log_to_file and exc is not None else ""
    log.error("[Parley] %s %s%s", problem, hint, where)
    if exc is not None:
        details_logger().error("Startup failure details", exc_info=exc)


async def run(settings: Settings) -> int:
    from bot.core import ParleyBot  # imported here so logging is configured first

    db = Database(settings.database_url, settings.database_connect_args)
    try:
        try:
            await db.ping()
        except Exception as exc:  # noqa: BLE001 - classified and reported without a traceback
            fatal(settings, *describe_database_error(exc), exc)
            return EXIT_STARTUP_ERROR
        log.info("Database connected: %s", describe_database(settings.database_url))

        current = await migrations.current_revision(db.engine)
        head = migrations.head_revision(settings)
        if current != head:
            fatal(
                settings,
                f"The database schema is out of date (at {current}, needs {head}).",
                "Run migrate.bat (Windows) or `alembic upgrade head`, or set RUN_MIGRATIONS_ON_STARTUP=true.",
            )
            return EXIT_STARTUP_ERROR

        bot = ParleyBot(settings, db)
        async with bot:
            install_signal_handlers(bot)
            try:
                await bot.start(settings.discord_token, reconnect=True)
            except discord.LoginFailure as exc:
                fatal(settings, "Discord token invalid.", "Copy a fresh token from the Developer Portal (Bot → Reset Token) into DISCORD_TOKEN.", exc)
                return EXIT_STARTUP_ERROR
            except discord.PrivilegedIntentsRequired as exc:
                fatal(
                    settings,
                    "Discord refused the connection: a required privileged intent is off.",
                    "Developer Portal → your app → Bot → Privileged Gateway Intents → enable Server Members and Message Content, then start again.",
                    exc,
                )
                return EXIT_STARTUP_ERROR
            except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:
                fatal(settings, "Could not reach Discord.", "Check your internet connection and try again.", exc)
                return EXIT_STARTUP_ERROR
        return 0
    finally:
        await db.dispose()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles may not be UTF-8; never crash while printing a server name.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"[Parley] Configuration problem: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    configure_logging(settings)
    log.info("Starting Parley %s (%s)", __version__, settings.environment)

    if not settings.discord_token:
        fatal(settings, "DISCORD_TOKEN is not set.", "Put your bot token in .env (locally) or the Railway variables.")
        return EXIT_CONFIG_ERROR
    for warning in settings.validate():
        log.warning(warning)
    legacy = settings.legacy_hub_values()
    if legacy:
        log.info("Old .env values in use (%s). They still work; /settings → Channels can save them to the database.", ", ".join(legacy))

    if settings.run_migrations_on_startup:
        try:
            migrations.upgrade_to_head(settings)
        except Exception as exc:  # noqa: BLE001 - classified and reported without a traceback
            fatal(settings, *describe_database_error(exc), exc)
            return EXIT_STARTUP_ERROR

    try:
        code = asyncio.run(run(settings))
    except KeyboardInterrupt:
        code = 0
    except OSError as exc:
        fatal(settings, "Could not connect.", "Check your internet connection and DATABASE_URL.", exc)
        code = EXIT_STARTUP_ERROR
    log.info("Parley stopped")
    return code


if __name__ == "__main__":
    sys.exit(main())
