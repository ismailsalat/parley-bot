"""Environment settings and runtime (database) business settings."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from bot.config import runtime
from bot.config.settings import SettingsError, describe_database, load_settings, normalize_database_url
from bot.utils.helpers import format_duration
from bot.utils.logging import RedactSecretsFilter


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        "DISCORD_TOKEN", "DATABASE_URL", "ENVIRONMENT", "LOG_LEVEL", "LOG_FORMAT", "MAIN_GUILD_ID",
        "LISTINGS_CHANNEL_ID", "STAFF_ROLE_IDS", "LOG_TO_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_railway_postgres_urls_are_normalized():
    url, args = normalize_database_url("postgres://user:pw@host:5432/railway")
    assert url == "postgresql+asyncpg://user:pw@host:5432/railway" and args == {}
    url, _ = normalize_database_url("postgresql://user:pw@host/db")
    assert url.startswith("postgresql+asyncpg://")
    url, args = normalize_database_url("postgresql://u:p@h/db?sslmode=require")
    assert "sslmode" not in url and args == {"ssl": "require"}


def test_empty_database_url_falls_back_to_sqlite():
    url, _ = normalize_database_url("")
    assert url.startswith("sqlite+aiosqlite:///") and url.endswith("waypoint.db")


def test_describe_database_hides_password():
    text = describe_database("postgresql+asyncpg://user:supersecret@db.example:5432/app")
    assert "supersecret" not in text and "user" not in text and "db.example" in text


def test_load_settings_and_validation(clean_env):
    clean_env.setenv("DISCORD_TOKEN", "abc.def.ghi")
    clean_env.setenv("MAIN_GUILD_ID", "123456789012345678")
    clean_env.setenv("STAFF_ROLE_IDS", "1, 2;3")
    settings = load_settings(load_env_file=False)
    assert settings.main_guild_id == 123456789012345678
    assert settings.staff_role_ids == (1, 2, 3)
    # Channels/roles are configured in Discord now: nothing in .env is required besides the token.
    assert settings.validate() == []
    assert settings.legacy_hub_values() == ["MAIN_GUILD_ID", "STAFF_ROLE_IDS"]


def test_invalid_ids_are_reported(clean_env):
    clean_env.setenv("MAIN_GUILD_ID", "not-a-number")
    with pytest.raises(SettingsError, match="MAIN_GUILD_ID"):
        load_settings(load_env_file=False)


def test_sqlite_warning_only_on_railway(clean_env):
    clean_env.setenv("ENVIRONMENT", "production")
    clean_env.delenv("RAILWAY_ENVIRONMENT", raising=False)
    local = load_settings(load_env_file=False)
    assert local.validate() == [] and local.log_to_file  # local Windows install: fine
    clean_env.setenv("RAILWAY_ENVIRONMENT", "production")
    railway = load_settings(load_env_file=False)
    assert any("SQLite" in w for w in railway.validate()) and not railway.log_to_file


def test_secrets_are_redacted_from_logs():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s pw=%s", ("tok123456", "pw654321"), None)
    RedactSecretsFilter(["tok123456", "pw654321"]).filter(record)
    assert "tok123456" not in record.getMessage() and "pw654321" not in record.getMessage()


def test_runtime_overrides_apply_and_bad_values_are_ignored():
    config, notes = runtime.apply_overrides(
        runtime.default_config(),
        {
            "listings.refresh_cooldown_minutes": 30,
            "listings.categories": '["Gaming", "Art", "Any", "Gaming"]',
            "network.min_interval_minutes": 1,
            "partnerships.max_pending_requests": "lots",
            "nope.value": 1,
        },
    )
    assert config.listings.refresh_cooldown_minutes == 30
    assert config.listings.categories == ("Gaming", "Art")  # "Any" is reserved, duplicates removed
    assert config.network.min_interval_minutes == runtime.HARD_MIN_NETWORK_INTERVAL_MINUTES
    assert config.partnerships.max_pending_requests == runtime.PartnershipConfig().max_pending_requests
    assert any("nope.value" in n for n in notes) and any("max_pending_requests" in n for n in notes)


def test_button_labels_are_configurable():
    config, _ = runtime.apply_overrides(
        runtime.default_config(), {"panels.buttons": {"post": {"label": "Advertise", "emoji": "🚀"}}}
    )
    assert config.button("post") == ("Advertise", "🚀")
    assert config.button("find")[0] == "Find Partners"


def test_every_setting_has_a_default_key():
    keys = runtime.known_keys()
    assert "listings.refresh_cooldown_minutes" in keys and "network.rotation_strategy" in keys


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(minutes=36, seconds=1), "37 minutes"),
        (timedelta(minutes=38), "38 minutes"),
        (timedelta(seconds=5), "1 minute"),
        (timedelta(hours=2, minutes=5), "2 hours 5 minutes"),
        (timedelta(days=1), "1 day"),
    ],
)
def test_friendly_durations_round_up(delta, text):
    assert format_duration(delta) == text
