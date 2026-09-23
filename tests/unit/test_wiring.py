"""Startup wiring: persistent buttons, action handlers and commands are registered."""

from __future__ import annotations

import re

import discord
import pytest

from bot.config.settings import Settings
from bot.core import WaypointBot, build_intents, persistent_items
from bot.database.session import Database
from bot.views.welcome import registered_actions


@pytest.fixture
async def bot(db):
    instance = WaypointBot(
        Settings(discord_token="", database_url=db.url, main_guild_id=123456789012345678, sync_commands=False), db
    )
    await instance.setup_hook()
    try:
        yield instance
    finally:
        await instance.close()


async def test_all_persistent_buttons_are_registered(bot):
    registered = set(bot._connection._view_store._dynamic_items.values())
    assert registered == set(persistent_items())


async def test_every_panel_action_has_a_handler(bot):
    assert {"post", "connect", "find", "servers", "requests", "looking", "network"} <= registered_actions()


async def test_custom_ids_round_trip_through_templates():
    from bot.views.listings import ReviewButton
    from bot.views.management import ManageButton, ManagementButton
    from bot.views.partnership import RequestPartnershipButton, RequestResponseButton, ViewAdButton
    from bot.views.welcome import ActionButton

    samples = [
        ActionButton("post"),
        RequestPartnershipButton(123456789012345678),
        ViewAdButton(123456789012345678),
        RequestResponseButton(42, True),
        RequestResponseButton(42, False),
        ReviewButton(123456789012345678, "approve", 3),
        ReviewButton(123456789012345678, "reject", 3),
        ReviewButton(123456789012345678, "ban"),
        ManageButton(123456789012345678),
        ManagementButton("refresh", 123456789012345678),
    ]
    for sample in samples:
        custom_id = sample.item.custom_id
        assert len(custom_id) <= 100
        assert re.fullmatch(type(sample).__discord_ui_compiled_template__, custom_id), custom_id


async def test_commands_stay_tiny(bot):
    assert sorted(c.name for c in bot.tree.get_commands()) == ["connect", "find", "help", "manage", "network", "setup"]
    main = discord.Object(id=123456789012345678)
    assert sorted(c.name for c in bot.tree.get_commands(guild=main)) == ["admin", "settings"]
    admin = bot.tree.get_command("admin", guild=main)
    assert {"remove", "suspend", "ban-server", "unban-server", "listing", "stats", "health", "import-settings"} <= {
        c.name for c in admin.commands
    }


def test_intents_are_minimal():
    intents = build_intents()
    assert intents.members and intents.guilds and intents.dm_messages
    assert intents.message_content and not intents.presences


async def test_runtime_config_reload_reads_database(bot, db):
    from bot.database import repository

    async with db.session() as session:
        await repository.set_runtime_setting(session, "listings.refresh_cooldown_minutes", 15)
    await bot.reload_runtime_config()
    assert bot.runtime.listings.refresh_cooldown_minutes == 15


def test_database_class_accepts_sqlite(tmp_path):
    assert Database(f"sqlite+aiosqlite:///{tmp_path}/a.db").url.startswith("sqlite")
