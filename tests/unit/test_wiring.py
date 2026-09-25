"""Startup wiring: persistent buttons, action handlers and commands are registered."""

from __future__ import annotations

import re

import discord
import pytest

from bot.config.settings import Settings
from bot.core import ParleyBot, build_intents, persistent_items
from bot.database.session import Database
from bot.views.welcome import registered_actions


@pytest.fixture
async def bot(db):
    instance = ParleyBot(
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




async def test_static_action_router_is_registered_without_message_id(bot):
    routes = bot._connection._view_store._views.get(None, {})
    expected = {
        (discord.ComponentType.button.value, f"wp:act:{action}")
        for action in registered_actions()
    }
    assert expected <= set(routes)
    assert bot.interaction_routing_status()["ok"] is True


async def test_stopping_message_specific_action_view_keeps_global_router(bot):
    from bot.views.welcome import ActionButton

    view = discord.ui.View(timeout=60)
    view.add_item(discord.ui.Button(label="Temporary", custom_id="test:temporary-action"))
    view.add_item(ActionButton("find"))
    bot._connection._view_store.add_view(view, message_id=123123123)
    view.stop()

    routes = bot._connection._view_store._views.get(None, {})
    assert (discord.ComponentType.button.value, "wp:act:find") in routes


async def test_stopping_mixed_view_cannot_unregister_dynamic_buttons(bot):
    """Regression for the real production failure: buttons died minutes after deploy.

    discord.py 2.7 may remove a shared DynamicItem template when a mixed
    transient view stops. Parley's ViewStore guard must restore every persistent
    template synchronously before another user can click an old message.
    """
    from bot.views.management import ManageButton

    view = discord.ui.View(timeout=60)
    view.add_item(discord.ui.Button(label="Temporary", custom_id="test:temporary"))
    view.add_item(ManageButton(123456789012345678))
    bot._connection._view_store.add_view(view, message_id=987654321)
    view.stop()

    registered = set(bot._connection._view_store._dynamic_items.values())
    assert registered == set(persistent_items())
    assert bot.interaction_routing_status()["ok"] is True


async def test_every_panel_action_has_a_handler(bot):
    assert {"post", "connect", "find", "servers", "requests", "looking", "network"} <= registered_actions()


async def test_custom_ids_round_trip_through_templates():
    from bot.views.listings import ReviewButton
    from bot.views.management import ManageButton, ManagementButton
    from bot.views.partnership import RequestPartnershipButton, RequestResponseButton, ViewAdButton
    from bot.views.welcome import ActionButton

    action = ActionButton("post")
    assert action.custom_id == "wp:act:post"
    assert action.item is action

    samples = [
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
    assert sorted(c.name for c in bot.tree.get_commands()) == ["connect", "find", "help", "manage", "network", "relist", "setup"]
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
