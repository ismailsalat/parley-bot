"""Setup wizard logic, TEST/LIVE/OFF mode, staff checks and test-mode side-effect prevention."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest

from bot.config.runtime import HubConfig, apply_overrides, default_config
from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import permissions, setup as setup_service, testmode
from bot.views.base import deliver_dms, guard
from tests.factories import NOW, make_listing
from tests.fakes import ADMIN_ID, MAIN, OWNER_ID, STAFF_ID, USER_ID, FakeBot, FakeInteraction


# ---------------------------------------------------------------- automatic setup


class Snowflake(discord.Object):
    """Hashable like real roles/members."""

    def __init__(self, id: int, **attrs) -> None:
        super().__init__(id=id)
        for key, value in attrs.items():
            setattr(self, key, value)


class SetupGuild:
    def __init__(self, *, existing: tuple[str, ...] = (), fail: tuple[str, ...] = (), manage_channels: bool = True) -> None:
        self.id = MAIN
        self.default_role = Snowflake(MAIN, name="@everyone")
        self.me = Snowflake(99, guild_permissions=discord.Permissions(manage_channels=manage_channels))
        self.text_channels = [SimpleNamespace(id=100 + i, name=name) for i, name in enumerate(existing)]
        self.fail = fail
        self.created: list[tuple[str, dict]] = []

    async def create_text_channel(self, name, *, topic, overwrites, reason):
        if name in self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        channel = SimpleNamespace(id=500 + len(self.created), name=name)
        self.created.append((name, overwrites))
        self.text_channels.append(channel)
        return channel


async def test_automatic_setup_creates_only_the_five_channels():
    guild = SetupGuild()
    result = await setup_service.automatic_setup(guild, staff_roles=[])
    assert [name for name, _ in guild.created] == [
        "start-here", "server-directory", "find-partners", "support", "waypoint-logs"
    ]
    assert result.ok and not result.failed
    assert "general" not in [name for name, _ in guild.created]


async def test_automatic_setup_reuses_existing_channels_and_reports_failures():
    guild = SetupGuild(existing=("start-here",), fail=("support",))
    result = await setup_service.automatic_setup(guild, staff_roles=[])
    assert result.reused == ["start-here"] and result.failed == ["support"]
    assert "support_channel_id" not in result.channels and result.ok  # support is optional


async def test_automatic_setup_reuses_legacy_channel_names_without_duplicates():
    guild = SetupGuild(existing=("welcome", "partner-listings", "looking-for-partners"))
    result = await setup_service.automatic_setup(guild, staff_roles=[])
    assert {"welcome", "partner-listings", "looking-for-partners"}.issubset(set(result.reused))
    created = [name for name, _ in guild.created]
    assert "start-here" not in created
    assert "server-directory" not in created
    assert "find-partners" not in created


def test_channel_permissions():
    guild = SetupGuild()
    staff = Snowflake(7)
    slot = setup_service.SLOT_BY_KEY
    listings = setup_service.overwrites_for(slot["listings_channel_id"], guild, [])
    assert listings[guild.default_role].send_messages is False  # members can't dump ads by hand
    assert listings[guild.me].send_messages is True
    logs = setup_service.overwrites_for(slot["log_channel_id"], guild, [staff])
    assert logs[guild.default_role].view_channel is False and logs[staff].view_channel is True
    looking = setup_service.overwrites_for(slot["looking_channel_id"], guild, [])
    assert guild.default_role not in looking  # people talk freely there
    for overwrites in (listings, logs, looking):
        assert not any(o.administrator for o in overwrites.values() if hasattr(o, "administrator"))


def test_can_auto_setup_needs_manage_channels():
    assert setup_service.can_auto_setup(SetupGuild())
    assert not setup_service.can_auto_setup(SetupGuild(manage_channels=False))


async def test_setup_persists_channels_and_starts_in_test_mode(db):
    from bot.views.admin.setup import save_hub

    bot = FakeBot(db, hub=HubConfig())
    changed: list[bool] = []

    async def settings_changed(**_):
        async with db.session() as session:
            bot.runtime = apply_overrides(default_config(), await repository.runtime_overrides(session))[0]
        changed.append(True)

    async def restore_panels(**_):
        return {}

    bot.settings_changed = settings_changed
    bot.panels = SimpleNamespace(restore_panels=restore_panels)
    guild = SimpleNamespace(id=MAIN)
    await save_hub(bot, guild, {"listings_channel_id": 22, "welcome_channel_id": 21}, actor_id=OWNER_ID)
    assert changed
    hub = bot.runtime.hub
    assert (hub.main_guild_id, hub.listings_channel_id, hub.welcome_channel_id) == (MAIN, 22, 21)
    assert hub.mode == "test" and hub.setup_completed
    # running setup again later does not flip a live network back to test
    bot.runtime = replace(bot.runtime, hub=replace(bot.runtime.hub, mode="live"))
    async with db.session() as session:
        await repository.set_runtime_setting(session, "hub.mode", "live")
    await save_hub(bot, guild, {"listings_channel_id": 23}, actor_id=OWNER_ID)
    assert bot.runtime.hub.mode == "live" and bot.runtime.hub.listings_channel_id == 23


# ---------------------------------------------------------------- staff & owner checks


@pytest.mark.parametrize(
    ("user_id", "expected"), [(OWNER_ID, True), (ADMIN_ID, True), (STAFF_ID, True), (USER_ID, False), (12345, False)]
)
async def test_staff_comes_from_database_roles(db, user_id, expected):
    bot = FakeBot(db)
    assert await permissions.is_staff(bot, user_id) is expected


async def test_owner_keeps_control_even_without_a_main_server(db):
    bot = FakeBot(db, hub=HubConfig())
    assert await permissions.is_staff(bot, OWNER_ID)
    assert not await permissions.is_staff(bot, ADMIN_ID)
    await permissions.require_owner(bot, OWNER_ID)
    with pytest.raises(permissions.PermissionDenied):
        await permissions.require_owner(bot, ADMIN_ID)  # admins of the hub still can't move it


async def test_staff_role_change_applies_after_settings_change(db):
    bot = FakeBot(db)
    assert await permissions.is_staff_cached(bot, STAFF_ID)
    bot.runtime = replace(bot.runtime, hub=replace(bot.runtime.hub, staff_role_ids=()))
    bot.staff_cache.clear()  # what settings_changed()/reload does
    assert not await permissions.is_staff_cached(bot, STAFF_ID)


# ---------------------------------------------------------------- modes


@pytest.mark.parametrize(
    ("mode", "user_id", "allowed", "text"),
    [
        ("live", USER_ID, True, None),
        ("test", USER_ID, False, "being set up"),
        ("off", USER_ID, False, "temporarily unavailable"),
        ("test", STAFF_ID, True, None),
        ("off", OWNER_ID, True, None),
    ],
)
async def test_guard_respects_mode(db, mode, user_id, allowed, text):
    bot = FakeBot(db, mode=mode)
    interaction = FakeInteraction(bot, user_id)
    assert await guard(interaction) is allowed
    if text:
        assert text in interaction.response.sent[0]["content"]


async def test_test_mode_holds_back_dms_to_real_users(db):
    bot = FakeBot(db, mode="test")
    delivered, held = await deliver_dms(bot, [USER_ID, STAFF_ID], actor_id=OWNER_ID, content="Partnership request!")
    assert (delivered, held) == (1, 1)
    assert bot.users[USER_ID].dms == []  # the real user heard nothing
    assert bot.users[STAFF_ID].dms[0]["content"].startswith("-# 🧪 TEST")
    assert "not sent to 1 non-staff" in bot.users[OWNER_ID].dms[0]["content"]  # the tester got a copy


async def test_live_mode_delivers_to_everyone(db):
    bot = FakeBot(db, mode="live")
    delivered, held = await deliver_dms(bot, [USER_ID, STAFF_ID], actor_id=OWNER_ID, content="Hi")
    assert (delivered, held) == (2, 0)
    assert bot.users[OWNER_ID].dms == []


async def test_closed_dms_are_reported_not_faked(db):
    bot = FakeBot(db, mode="live")
    bot.users[USER_ID].closed_dms = True
    delivered, held = await deliver_dms(bot, [USER_ID], actor_id=None, content="Hi")
    assert (delivered, held) == (0, 0)


async def test_network_scheduler_is_paused_outside_live(db):
    from bot.tasks import BackgroundTasks

    for mode in ("test", "off"):
        bot = FakeBot(db, mode=mode)
        bot.is_ready = lambda: True
        tasks = BackgroundTasks(bot)
        await tasks.network_tick()  # would need Discord if it tried to post; returning quietly proves it didn't


async def test_test_listings_are_hidden_in_live_mode(db, config):
    async with db.session() as session:
        await make_listing(session, config, 1, now=NOW)
        await make_listing(session, config, 2, now=NOW)
        (await repository.get_listing(session, 2)).is_test = True
    async with db.session() as session:
        live, _ = await repository.search_listings(session, category=None, accepting_only=True, limit=10)
        test, _ = await repository.search_listings(session, category=None, accepting_only=True, limit=10, include_test=True)
        network_sources = await repository.active_listings(session)
    assert [l.guild_id for l in live] == [1]
    assert sorted(l.guild_id for l in test) == [1, 2]
    assert [l.guild_id for l in network_sources] == [1]


async def test_going_live_discards_or_keeps_test_listings(db, config):
    async with db.session() as session:
        for gid in (1, 2):
            await make_listing(session, config, gid, now=NOW)
        (await repository.get_listing(session, 2)).is_test = True
    async with db.session() as session:
        removed = await testmode.discard_test_listings(session)
    assert [r[0] for r in removed] == [2]
    async with db.session() as session:
        assert await repository.get_listing(session, 2) is None
        assert (await repository.get_listing(session, 1)).status == ListingStatus.ACTIVE
        await make_listing(session, config, 3, now=NOW)
        (await repository.get_listing(session, 3)).is_test = True
    async with db.session() as session:
        assert await testmode.keep_test_listings(session) == [3]
    async with db.session() as session:
        assert (await repository.get_listing(session, 3)).is_test is False


async def test_test_messages_are_tracked_and_deleted(db):
    deleted = []

    async def delete_listing_message(channel_id, message_id):
        deleted.append((channel_id, message_id))

    bot = FakeBot(db)
    bot.panels = SimpleNamespace(delete_listing_message=delete_listing_message)
    async with db.session() as session:
        await testmode.record_message(session, channel_id=10, message_id=11, kind="listing")
        await testmode.record_message(session, channel_id=10, message_id=12, kind="network")
    assert await testmode.delete_test_messages(bot) == 2
    assert deleted == [(10, 11), (10, 12)]
    async with db.session() as session:
        assert await repository.test_messages(session) == []
