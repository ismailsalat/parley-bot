"""Deployment and security settings, loaded from environment variables.

These settings are the things that differ between machines (tokens, database
URLs, channel IDs). Business rules such as cooldowns and categories live in
``bot.config.runtime`` and can be changed at runtime through the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


class SettingsError(Exception):
    """Raised when an environment variable is present but invalid."""


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _optional_id(name: str) -> int | None:
    raw = _env(name)
    if not raw:
        return None
    if not raw.isdigit():
        raise SettingsError(f"{name} must be a numeric Discord ID.")
    return int(raw)


def _id_list(name: str) -> tuple[int, ...]:
    raw = _env(name)
    if not raw:
        return ()
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise SettingsError(f"{name} must be a comma-separated list of numeric IDs.")
        ids.append(int(part))
    return tuple(ids)


def _bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be true or false.")


def normalize_database_url(raw: str | None) -> tuple[str, dict]:
    """Turn any common database URL into an async SQLAlchemy URL.

    Railway and Heroku style ``postgres://`` / ``postgresql://`` URLs are
    converted to ``postgresql+asyncpg://``. An empty value falls back to a
    local SQLite file. ``sslmode`` query parameters are translated into
    asyncpg's ``ssl`` connect argument because asyncpg rejects ``sslmode``.
    """
    raw = (raw or "").strip()
    if not raw:
        path = (PROJECT_ROOT / "waypoint.db").as_posix()
        return f"sqlite+aiosqlite:///{path}", {}

    if raw.startswith("sqlite"):
        scheme, _, rest = raw.partition("://")
        if "+aiosqlite" not in scheme:
            scheme = "sqlite+aiosqlite"
        return f"{scheme}://{rest}", {}

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg2", "postgresql+psycopg"}:
        raise SettingsError("DATABASE_URL must be a PostgreSQL or SQLite URL.")

    connect_args: dict = {}
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() == "sslmode":
            if value.lower() in _SSL_MODES and value.lower() != "disable":
                connect_args["ssl"] = value.lower()
            continue
        query.append((key, value))

    url = urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url, connect_args


def describe_database(url: str) -> str:
    """A log-safe description of the database (no username or password)."""
    if url.startswith("sqlite"):
        return "SQLite (" + url.split(":///", 1)[-1] + ")"
    parts = urlsplit(url)
    host = parts.hostname or "unknown-host"
    port = f":{parts.port}" if parts.port else ""
    return f"PostgreSQL ({host}{port}{parts.path})"


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_url: str
    database_connect_args: dict = field(default_factory=dict)
    environment: str = "development"
    log_level: str = "INFO"
    log_format: str = "text"
    log_to_file: bool = True

    main_guild_id: int | None = None
    welcome_channel_id: int | None = None
    listings_channel_id: int | None = None
    looking_channel_id: int | None = None
    support_channel_id: int | None = None
    log_channel_id: int | None = None
    staff_role_ids: tuple[int, ...] = ()

    run_migrations_on_startup: bool = True
    sync_commands: bool = True
    on_railway: bool = False
    # Required for the controlled "Paste My Own Ad" channel flow.
    message_content_intent: bool = True
    # "Verify My Servers": listing a server without installing Parley. The client id is
    # the application id; only the secret and redirect URL come from the environment.
    oauth_client_id: int | None = None
    oauth_client_secret: str = ""
    oauth_redirect_uri: str = ""
    oauth_port: int = 8080

    @property
    def oauth_enabled(self) -> bool:
        """Can Parley run "Verify My Servers"? Needs a secret and a callback URL."""
        return bool(self.oauth_client_secret and self.oauth_redirect_uri)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def uses_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def secrets(self) -> list[str]:
        """Values that must never appear in log output."""
        values = [self.discord_token]
        password = urlsplit(self.database_url).password if not self.uses_sqlite else None
        if password:
            values.append(password)
        if self.oauth_client_secret:
            values.append(self.oauth_client_secret)
        return [v for v in values if v and len(v) >= 6]

    def validate(self) -> list[str]:
        """Human-readable warnings. Channels, roles and all business settings are set in Discord (/setup, /settings)."""
        warnings: list[str] = []
        if self.oauth_client_secret and not self.oauth_redirect_uri:
            warnings.append(
                "DISCORD_CLIENT_SECRET is set but DISCORD_OAUTH_REDIRECT_URI is not, so "
                "\"Verify My Servers\" stays off."
            )
        if self.uses_sqlite and self.on_railway:
            warnings.append(
                "Railway is using a temporary SQLite file: data is lost on redeploy. "
                "Set DATABASE_URL to ${{Postgres.DATABASE_URL}}."
            )
        return warnings

    def legacy_hub_values(self) -> list[str]:
        """Old .env hub variables still set (they keep working; /settings -> Channels can save them to the database)."""
        names = {
            "MAIN_GUILD_ID": self.main_guild_id, "WELCOME_CHANNEL_ID": self.welcome_channel_id,
            "LISTINGS_CHANNEL_ID": self.listings_channel_id, "LOOKING_CHANNEL_ID": self.looking_channel_id,
            "SUPPORT_CHANNEL_ID": self.support_channel_id, "LOG_CHANNEL_ID": self.log_channel_id,
            "STAFF_ROLE_IDS": self.staff_role_ids,
        }
        return [name for name, value in names.items() if value]


def load_settings(*, load_env_file: bool = True) -> Settings:
    """Read settings from the environment (and a local .env file if present)."""
    if load_env_file:
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

    database_url, connect_args = normalize_database_url(_env("DATABASE_URL"))

    environment = (_env("ENVIRONMENT") or "development").lower()
    if environment not in {"development", "production"}:
        raise SettingsError("ENVIRONMENT must be 'development' or 'production'.")

    log_level = (_env("LOG_LEVEL") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise SettingsError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL.")

    log_format = (_env("LOG_FORMAT") or "text").lower()
    if log_format not in {"text", "json"}:
        raise SettingsError("LOG_FORMAT must be 'text' or 'json'.")

    on_railway = bool(_env("RAILWAY_ENVIRONMENT") or _env("RAILWAY_PROJECT_ID"))
    return Settings(
        discord_token=_env("DISCORD_TOKEN"),
        database_url=database_url,
        database_connect_args=connect_args,
        environment=environment,
        log_level=log_level,
        log_format=log_format,
        log_to_file=_bool("LOG_TO_FILE", not on_railway),  # Railway keeps its own logs
        main_guild_id=_optional_id("MAIN_GUILD_ID"),
        welcome_channel_id=_optional_id("WELCOME_CHANNEL_ID"),
        listings_channel_id=_optional_id("LISTINGS_CHANNEL_ID"),
        looking_channel_id=_optional_id("LOOKING_CHANNEL_ID"),
        support_channel_id=_optional_id("SUPPORT_CHANNEL_ID"),
        log_channel_id=_optional_id("LOG_CHANNEL_ID"),
        staff_role_ids=_id_list("STAFF_ROLE_IDS"),
        run_migrations_on_startup=_bool("RUN_MIGRATIONS_ON_STARTUP", True),
        sync_commands=_bool("SYNC_COMMANDS", True),
        on_railway=on_railway,
        message_content_intent=True,
        oauth_client_id=_optional_id("DISCORD_CLIENT_ID"),
        oauth_client_secret=_env("DISCORD_CLIENT_SECRET"),
        oauth_redirect_uri=_env("DISCORD_OAUTH_REDIRECT_URI"),
        oauth_port=int(_env("PORT") or 8080),
    )
