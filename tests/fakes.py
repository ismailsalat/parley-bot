"""Minimal stand-ins for Discord objects.

They let unit tests check *Waypoint's decisions* (who may do what, what gets
sent where, what is stored). They do not claim to reproduce Discord itself;
live behaviour is covered by tests/integration and ``python -m bot.tools.live_check``.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import discord

from bot.config.runtime import HubConfig, RuntimeConfig, default_config
from bot.core import build_intents
from bot.services.moderation import RateLimiter
from tests.conftest import make_settings

MAIN = 900000000000000001
OWNER_ID = 1
ADMIN_ID = 2
STAFF_ID = 3
USER_ID = 4
STAFF_ROLE = 777


class FakeUser:
    def __init__(self, user_id: int, *, closed_dms: bool = False) -> None:
        self.id = user_id
        self.name = f"user{user_id}"
        self.mention = f"<@{user_id}>"
        self.bot = False
        self.closed_dms = closed_dms
        self.dms: list[dict[str, Any]] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        if self.closed_dms:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
        self.dms.append({"content": content, **kwargs})


def member(user_id: int, *, admin: bool = False, roles: tuple[int, ...] = ()) -> SimpleNamespace:
    perms = discord.Permissions(administrator=True) if admin else discord.Permissions.none()
    return SimpleNamespace(id=user_id, guild_permissions=perms, roles=[SimpleNamespace(id=r) for r in roles], bot=False)


class FakeGuild:
    def __init__(self, guild_id: int = MAIN, members: dict[int, Any] | None = None) -> None:
        self.id = guild_id
        self.name = "Waypoint HQ"
        self.member_count = 530
        self.icon = None
        self.members = members or {}

    async def fetch_member(self, user_id: int):
        if user_id not in self.members:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
        return self.members[user_id]

    def get_member(self, user_id: int):
        return self.members.get(user_id)


class FakeBot:
    """Enough of WaypointBot for services and guards."""

    def __init__(
        self,
        db,
        *,
        mode: str = "live",
        hub: HubConfig | None = None,
        runtime: RuntimeConfig | None = None,
        message_content: bool = False,
    ) -> None:
        self.db = db
        self.intents = build_intents(message_content)
        self.panels = SimpleNamespace(listings_channel=lambda: None)
        base = runtime or default_config()
        self.runtime = replace(base, hub=replace(hub or HubConfig(main_guild_id=MAIN, staff_role_ids=(STAFF_ROLE,)), mode=mode))
        self.settings = make_settings("sqlite://")
        self.staff_cache: dict = {}
        self.click_limiter = RateLimiter(100, 10)
        self.users = {uid: FakeUser(uid) for uid in (OWNER_ID, ADMIN_ID, STAFF_ID, USER_ID)}
        self.guild = FakeGuild(members={
            ADMIN_ID: member(ADMIN_ID, admin=True),
            STAFF_ID: member(STAFF_ID, roles=(STAFF_ROLE,)),
            USER_ID: member(USER_ID),
        })
        self.guilds = [self.guild]

    async def is_owner(self, user) -> bool:
        return user.id == OWNER_ID

    def get_guild(self, guild_id: int):
        return self.guild if guild_id == self.guild.id else None

    def get_user(self, user_id: int):
        return self.users.get(user_id)

    async def fetch_user(self, user_id: int):
        return self.users.setdefault(user_id, FakeUser(user_id))


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def is_done(self) -> bool:
        return bool(self.sent)

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class FakeInteraction:
    def __init__(self, bot: FakeBot, user_id: int) -> None:
        self.client = bot
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.response = FakeResponse()
        self.guild = None
        self.message = None
