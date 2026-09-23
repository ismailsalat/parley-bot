"""Settings changed from Discord: validation, reset, export/import, buttons, links, env import, hot reload."""

from __future__ import annotations

import json

import pytest

from bot.config.runtime import HubConfig, apply_overrides, default_config
from bot.database import repository
from bot.services import configuration
from bot.services.errors import ValidationError
from tests.conftest import make_settings

ACTOR = 1


async def overrides(db) -> dict:
    async with db.session() as session:
        return await repository.runtime_overrides(session)


async def test_save_validates_before_writing(db):
    async with db.session() as session:
        await configuration.save(session, {"listings.refresh_cooldown_minutes": 15}, actor_id=ACTOR)
    with pytest.raises(ValidationError):
        async with db.session() as session:
            await configuration.save(session, {"listings.refresh_cooldown_minutes": "soon"}, actor_id=ACTOR)
    with pytest.raises(ValidationError, match="Unknown setting"):
        async with db.session() as session:
            await configuration.save(session, {"listings.nope": 1}, actor_id=ACTOR)
    assert await overrides(db) == {"listings.refresh_cooldown_minutes": 15}


async def test_channel_and_staff_config_live_in_the_database(db):
    async with db.session() as session:
        await configuration.save(
            session,
            {"hub.main_guild_id": 11, "hub.listings_channel_id": 22, "hub.staff_role_ids": [5, 6], "hub.mode": "test"},
            actor_id=ACTOR,
        )
    config, notes = apply_overrides(default_config(), await overrides(db))
    assert notes == []
    assert (config.hub.main_guild_id, config.hub.listings_channel_id) == (11, 22)
    assert config.hub.staff_role_ids == (5, 6) and config.hub.mode == "test"


async def test_mode_values(db):
    async with db.session() as session:
        for mode in ("test", "off", "live"):
            await configuration.set_mode(session, mode, actor_id=ACTOR)
        with pytest.raises(ValidationError):
            await configuration.set_mode(session, "party", actor_id=ACTOR)
    assert (await overrides(db))["hub.mode"] == "live"


async def test_reset_section_only_touches_that_section(db):
    async with db.session() as session:
        await configuration.save(
            session,
            {"listings.refresh_cooldown_minutes": 5, "network.repeat_window_hours": 3, "hub.main_guild_id": 9},
            actor_id=ACTOR,
        )
        assert await configuration.reset_section(session, "listings", actor_id=ACTOR) == 1
        with pytest.raises(ValidationError):
            await configuration.reset_section(session, "hub", actor_id=ACTOR)  # never reset the hub in one click
    assert await overrides(db) == {"network.repeat_window_hours": 3, "hub.main_guild_id": 9}


async def test_export_contains_no_secrets_and_imports_back(db):
    secret_settings = make_settings("postgresql://user:hunter2secret@db.example/app")
    async with db.session() as session:
        await configuration.save(session, {"listings.max_contacts": 2, "hub.main_guild_id": 42}, actor_id=ACTOR)
        data = await configuration.export_settings(session, default_config())
    assert "hunter2secret" not in data and "postgresql" not in data and secret_settings.discord_token not in data.split('"')
    payload = json.loads(data)
    assert payload["overrides"]["listings.max_contacts"] == 2

    chosen = configuration.parse_import(data)
    assert chosen == {"listings.max_contacts": 2}  # hub values are skipped by default
    assert configuration.parse_import(data, include_hub=True)["hub.main_guild_id"] == 42


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "valid JSON"),
        ('{"hello": 1}', "Parley settings export"),
        ('{"waypoint_settings_version": 1, "overrides": {"evil.key": 1}}', "Unknown setting"),
        ('{"waypoint_settings_version": 1, "overrides": {"listings.max_contacts": "x"}}', "invalid value"),
        ('{"waypoint_settings_version": 1, "overrides": {"messages.dm_home": "Hi {password}"}}', "Unknown placeholder"),
    ],
)
def test_import_rejects_bad_files(raw, message):
    with pytest.raises(ValidationError, match=message):
        configuration.parse_import(raw)


async def test_button_customization_keeps_routing(db):
    async with db.session() as session:
        await configuration.set_button(session, "post", label="Advertise", emoji="🚀", actor_id=ACTOR)
        await configuration.set_button(session, "find", label="Search", emoji="<:mag:123456789012345678>", actor_id=ACTOR)
    config, _ = apply_overrides(default_config(), await overrides(db))
    assert config.button("post") == ("Advertise", "🚀")
    assert config.button("find") == ("Search", "<:mag:123456789012345678>")

    from bot.views.welcome import ActionButton

    assert ActionButton("post", label="Advertise").item.custom_id == "wp:act:post"  # label never changes routing

    async with db.session() as session:
        await configuration.reset_button(session, "post", actor_id=ACTOR)
    config, _ = apply_overrides(default_config(), await overrides(db))
    assert config.button("post") == ("Post Server Ad", None) and config.button("find")[0] == "Search"


@pytest.mark.parametrize("label, emoji", [("", "📢"), ("x" * 41, ""), ("Ok", "not-an-emoji"), ("Ok", "<:bad:1>")])
async def test_bad_button_values_are_rejected(db, label, emoji):
    with pytest.raises(ValidationError):
        async with db.session() as session:
            await configuration.set_button(session, "post", label=label, emoji=emoji, actor_id=ACTOR)


async def test_internal_buttons_cannot_be_customized(db):
    with pytest.raises(ValidationError):
        async with db.session() as session:
            await configuration.set_button(session, "home", label="x", emoji="", actor_id=ACTOR)


async def test_links(db):
    async with db.session() as session:
        await configuration.set_links(session, support="https://discord.gg/help", rules="", website="https://example.com", actor_id=ACTOR)
        with pytest.raises(ValidationError, match="https://"):
            await configuration.set_links(session, support="discord.gg/help", rules="", website="", actor_id=ACTOR)
    config, _ = apply_overrides(default_config(), await overrides(db))
    assert config.bot.support_url == "https://discord.gg/help" and config.bot.rules_url == ""


def test_legacy_env_values_are_used_until_saved():
    settings = make_settings("sqlite://", main_guild_id=5, listings_channel_id=6, staff_role_ids=(7,))
    hub, used = configuration.merge_env_hub(HubConfig(), settings)
    assert (hub.main_guild_id, hub.listings_channel_id, hub.staff_role_ids) == (5, 6, (7,))
    assert set(used) == {"main_guild_id", "listings_channel_id", "staff_role_ids"}
    # database values win over env values
    hub, used = configuration.merge_env_hub(HubConfig(main_guild_id=99), settings)
    assert hub.main_guild_id == 99 and "main_guild_id" not in used
    changes = configuration.env_hub_changes(*configuration.merge_env_hub(HubConfig(), settings))
    assert changes["hub.main_guild_id"] == 5 and changes["hub.staff_role_ids"] == [7] and changes["hub.setup_completed"]


async def test_settings_hot_reload_without_restart(db):
    """A running bot picks up a change as soon as the Settings UI saves it."""
    from bot.core import ParleyBot

    bot = ParleyBot(make_settings(db.url), db)
    try:
        await bot.reload_runtime_config()
        assert bot.runtime.hub.mode == "live"
        async with db.session() as session:
            await configuration.save(
                session, {"hub.mode": "test", "messages.dm_home": "Hi from {bot_name}", "listings.categories": ["Art"]},
                actor_id=ACTOR,
            )
        await bot.settings_changed()
        assert bot.runtime.hub.mode == "test"
        assert bot.runtime.listings.categories == ("Art",)
        from bot.config import templates

        assert templates.render(bot.runtime, "dm_home") == "Hi from Parley"
    finally:
        await bot.close()


async def test_old_env_deployments_keep_working(db):
    from bot.core import ParleyBot

    bot = ParleyBot(make_settings(db.url, main_guild_id=5, looking_channel_id=8), db)
    try:
        await bot.reload_runtime_config()
        assert bot.hub.main_guild_id == 5 and bot.hub.looking_channel_id == 8
        assert "main_guild_id" in bot.hub_env_fields
    finally:
        await bot.close()
