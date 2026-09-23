"""Health Check: understandable problems + the button that fixes them."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import discord

from bot.config.runtime import HubConfig, default_config
from bot.core import build_intents, persistent_items
from bot.services import health
from tests.conftest import make_settings

MAIN = 950000000000000001
ALL = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True, embed_links=True)


def channel(channel_id: int, name: str, perms: discord.Permissions = ALL):
    return SimpleNamespace(
        id=channel_id, name=name, mention=f"<#{channel_id}>", type=discord.ChannelType.text,
        permissions_for=lambda _member: perms,
    )


class HealthBot:
    def __init__(self, db, hub: HubConfig, channels: dict[int, object], panels: dict[str, str] | None = None) -> None:
        self.db = db
        self.settings = make_settings(db.url)
        self.runtime = replace(default_config(), hub=hub)
        self.latency = 0.05
        self.intents = build_intents()  # message content off: "Post It Myself" unavailable
        self.runtime_notes: list[str] = []
        self.hub_env_fields: tuple[str, ...] = ()
        me = SimpleNamespace(guild_permissions=discord.Permissions(create_instant_invite=True))
        self.guild = SimpleNamespace(id=MAIN, name="HQ", me=me, get_channel=channels.get)
        self._connection = SimpleNamespace(_view_store=SimpleNamespace(_dynamic_items={i: i for i in persistent_items()}))
        self.background = SimpleNamespace(status=lambda: {"running": True})
        states = panels or {}

        async def panel_status(panel_type: str) -> str:
            return states.get(panel_type, "ok")

        self.panels = SimpleNamespace(panel_status=panel_status, listings_channel=lambda: channels.get(2))

    def is_ready(self) -> bool:
        return True

    def get_guild(self, guild_id: int):
        return self.guild if guild_id == MAIN else None


def backend(db) -> str:
    return "SQLite" if db.url.startswith("sqlite") else "PostgreSQL"


def by_name(checks):
    return {c.name: c for c in checks}


def full_hub(**overrides) -> HubConfig:
    values = dict(main_guild_id=MAIN, welcome_channel_id=1, listings_channel_id=2, looking_channel_id=3, log_channel_id=5)
    values.update(overrides)
    return HubConfig(**values)


async def test_healthy_setup(db):
    channels = {1: channel(1, "start-here"), 2: channel(2, "server-directory"), 3: channel(3, "find-partners"), 5: channel(5, "logs")}
    checks = await health.run(HealthBot(db, full_hub(), channels))
    named = by_name(checks)
    for name in ("Discord", backend(db), "Database schema", "Main server", "Listings channel", "Persistent buttons",
                 "Background tasks", "Network scheduler", "Settings"):
        assert named[name].status == health.OK, (name, named[name].detail)
    assert named["Support channel"].status == health.WARN  # optional: warning, not failure
    summary = health.render(checks)
    assert "✅ Discord" in summary and "✅ Listings" in summary  # one line per area, not a wall of checks
    assert len(summary.splitlines()) <= 14
    assert "Database schema" in health.render_details(checks)  # every check is one press away


async def test_not_set_up(db):
    named = by_name(await health.run(HealthBot(db, HubConfig(), {})))
    assert named["Main server"].status == health.FAIL and named["Main server"].fix == "setup"


async def test_deleted_channel_and_missing_permission_offer_a_fix(db):
    no_send = discord.Permissions(view_channel=True, read_message_history=True, embed_links=True)
    channels = {1: channel(1, "welcome", no_send), 3: channel(3, "find-partners")}  # listings (2) deleted
    named = by_name(await health.run(HealthBot(db, full_hub(), channels)))
    assert named["Listings channel"].status == health.FAIL and "deleted" in named["Listings channel"].detail
    assert named["Welcome channel"].status == health.FAIL and "Send Messages" in named["Welcome channel"].detail
    assert named["Listings channel"].fix == "channels"


async def test_missing_panel_offers_repair(db):
    channels = {1: channel(1, "start-here"), 2: channel(2, "server-directory"), 3: channel(3, "find-partners")}
    named = by_name(await health.run(HealthBot(db, full_hub(), channels, panels={"listings": "missing"})))
    assert named["Listings panel"].status == health.WARN and named["Listings panel"].fix == "repair"


async def test_test_mode_and_approval_without_logs(db):
    bot = HealthBot(db, full_hub(mode="test", log_channel_id=0), {2: channel(2, "server-directory")})
    bot.runtime = replace(bot.runtime, listings=replace(bot.runtime.listings, approval_required=True))
    named = by_name(await health.run(bot))
    assert named["Mode"].status == health.WARN and "TEST" in named["Mode"].detail
    assert named["Network scheduler"].status == health.WARN
    assert named["Approvals"].status == health.FAIL


async def test_database_failure_is_a_check_not_a_crash(db):
    bot = HealthBot(db, full_hub(), {})

    async def broken():
        raise OSError("connection refused")

    bot.db = SimpleNamespace(ping=broken, engine=None, session=db.session)
    named = by_name(await health.run(bot))
    assert named[backend(db)].status == health.FAIL
