"""Permission rules. Discord objects are replaced by minimal stand-ins: these
tests check Waypoint's *decisions*, not Discord's permission calculation."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot.services import permissions
from bot.services.errors import NotFound, PermissionDenied


def test_manage_guild_or_administrator_required():
    assert permissions.can_manage(discord.Permissions(manage_guild=True))
    assert permissions.can_manage(discord.Permissions(administrator=True))
    assert not permissions.can_manage(discord.Permissions(manage_messages=True, kick_members=True))
    assert not permissions.can_manage(discord.Permissions.none())


def test_staff_roles():
    member = SimpleNamespace(guild_permissions=discord.Permissions.none(), roles=[SimpleNamespace(id=7)])
    assert permissions.is_staff_member(member, (7,))
    assert not permissions.is_staff_member(member, (8,))
    admin = SimpleNamespace(guild_permissions=discord.Permissions(administrator=True), roles=[])
    assert permissions.is_staff_member(admin, ())


class FakeGuild:
    def __init__(self, guild_id: int, members: dict[int, discord.Permissions]) -> None:
        self.id = guild_id
        self.name = f"Guild {guild_id}"
        self._members = members
        self.fetches = 0

    async def fetch_member(self, user_id: int):
        self.fetches += 1
        if user_id not in self._members:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
        return SimpleNamespace(id=user_id, guild_permissions=self._members[user_id])

    def get_member(self, user_id: int):
        perms = self._members.get(user_id)
        return SimpleNamespace(id=user_id, guild_permissions=perms) if perms else None


class FakeBot:
    def __init__(self, *guilds: FakeGuild) -> None:
        self.guilds = list(guilds)

    def get_guild(self, guild_id: int):
        return next((g for g in self.guilds if g.id == guild_id), None)


async def test_require_manager_checks_current_state_every_time():
    guild = FakeGuild(1, {10: discord.Permissions(manage_guild=True), 11: discord.Permissions.none()})
    bot = FakeBot(guild)
    await permissions.require_manager(bot, 1, 10)
    with pytest.raises(PermissionDenied, match="You need Manage Server permission."):
        await permissions.require_manager(bot, 1, 11)
    # permission removed later: the next check sees it immediately (nothing is cached)
    guild._members[10] = discord.Permissions.none()
    with pytest.raises(PermissionDenied):
        await permissions.require_manager(bot, 1, 10)
    assert guild.fetches == 3


async def test_require_manager_when_user_left_or_bot_removed():
    bot = FakeBot(FakeGuild(1, {}))
    with pytest.raises(PermissionDenied):
        await permissions.require_manager(bot, 1, 99)
    with pytest.raises(NotFound):
        await permissions.require_manager(bot, 2, 99)
    assert await permissions.is_manager(bot, 2, 99) is False


def test_manageable_guilds_listing():
    a = FakeGuild(1, {10: discord.Permissions(administrator=True)})
    b = FakeGuild(2, {10: discord.Permissions.none()})
    assert [g.id for g in permissions.cached_manageable_guilds(FakeBot(a, b), 10)] == [1]
