"""Permission checks.

Permissions are always checked against Discord's *current* state (the live
member cache kept in sync by the gateway, or a fresh API fetch before any
change). Nothing about permissions is stored in the database.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord

from bot.services.errors import MANAGE_SERVER_REQUIRED, NotFound, PermissionDenied

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

REQUIRED_CHANNEL_PERMISSIONS = ("view_channel", "send_messages", "read_message_history")




def public_bot_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Return a text channel that ordinary members can see and Parley can use.

    A bot-only/private channel does not qualify. Read-only for members is fine;
    @everyone only needs View Channel, while Parley needs its normal message
    permissions. This closes the hidden-channel setup loophole.
    """
    me = guild.me
    if me is None:
        return None
    everyone = guild.default_role

    def usable(channel: discord.TextChannel | None) -> bool:
        if channel is None:
            return False
        bot_perms = channel.permissions_for(me)
        public_perms = channel.permissions_for(everyone)
        return bool(
            public_perms.view_channel
            and bot_perms.view_channel
            and bot_perms.send_messages
            and bot_perms.read_message_history
        )

    preferred = [guild.system_channel]
    preferred.extend(guild.text_channels)
    seen: set[int] = set()
    for channel in preferred:
        if channel is None or channel.id in seen:
            continue
        seen.add(channel.id)
        if usable(channel):
            return channel
    return None


def require_public_bot_channel(guild: discord.Guild) -> discord.TextChannel:
    channel = public_bot_channel(guild)
    if channel is None:
        raise PermissionDenied(
            "Parley needs at least one channel that **@everyone can view** and where Parley has "
            "**View Channel**, **Send Messages**, and **Read Message History**. "
            "Members do not need permission to send messages there."
        )
    return channel

def can_manage(permissions: discord.Permissions) -> bool:
    """Manage Server OR Administrator (the guild owner always has both)."""
    return permissions.administrator or permissions.manage_guild


def is_staff_member(member: discord.Member, staff_role_ids: tuple[int, ...]) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id in staff_role_ids for role in member.roles)


async def fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """Fresh member data from the API (falls back to None if they left)."""
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


async def require_manager(bot: ParleyBot, guild_id: int, user_id: int) -> tuple[discord.Guild, discord.Member]:
    """Verify, right now, that ``user_id`` can manage ``guild_id``."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise NotFound("Parley is no longer in that server. Add it back to manage this listing.")
    member = await fetch_member(guild, user_id)
    if member is None or not can_manage(member.guild_permissions):
        raise PermissionDenied(MANAGE_SERVER_REQUIRED)
    return guild, member


async def is_manager(bot: ParleyBot, guild_id: int, user_id: int) -> bool:
    try:
        await require_manager(bot, guild_id, user_id)
    except (PermissionDenied, NotFound):
        return False
    return True


def cached_manageable_guilds(bot: ParleyBot, user_id: int) -> list[discord.Guild]:
    """Guilds where the gateway-synced member cache says the user can manage."""
    result = []
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member is not None and can_manage(member.guild_permissions):
            result.append(guild)
    return sorted(result, key=lambda g: g.name.lower())


async def is_owner(bot: ParleyBot, user_id: int) -> bool:
    """The account (or team) that owns the bot application. Always has emergency control."""
    return await bot.is_owner(discord.Object(id=user_id))  # type: ignore[arg-type]


async def is_staff(bot: ParleyBot, user_id: int) -> bool:
    """Owner, administrators of the main server, or members with a staff role (set in Settings -> Staff)."""
    if await is_owner(bot, user_id):
        return True
    hub = bot.runtime.hub
    guild = bot.get_guild(hub.main_guild_id) if hub.main_guild_id else None
    if guild is None:
        return False
    member = await fetch_member(guild, user_id)
    return member is not None and is_staff_member(member, hub.staff_role_ids)


STAFF_CACHE_SECONDS = 60.0


async def is_staff_cached(bot: ParleyBot, user_id: int) -> bool:
    """is_staff for hot paths (every click in TEST/OFF mode). Cleared whenever settings change."""
    now = time.monotonic()
    cache: dict[int, tuple[bool, float]] = bot.staff_cache
    hit = cache.get(user_id)
    if hit is not None and hit[1] > now:
        return hit[0]
    result = await is_staff(bot, user_id)
    if len(cache) > 5_000:
        cache.clear()
    cache[user_id] = (result, now + STAFF_CACHE_SECONDS)
    return result


async def require_staff(bot: ParleyBot, user_id: int) -> None:
    if not await is_staff(bot, user_id):
        raise PermissionDenied("Only Parley staff can do that.")


async def require_owner(bot: ParleyBot, user_id: int) -> None:
    if not await is_owner(bot, user_id):
        raise PermissionDenied("Only the owner of the Parley bot application can do that.")


REQUIRED_PERMISSION_LABELS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "read_message_history": "Read Message History",
    "embed_links": "Embed Links",
}


def channel_permission_report(channel: discord.abc.GuildChannel, me: discord.Member) -> list[tuple[str, bool]]:
    """[(label, ok)] for the permissions Parley needs in a main-server channel."""
    perms = channel.permissions_for(me)
    return [(label, bool(getattr(perms, name))) for name, label in REQUIRED_PERMISSION_LABELS.items()]


def missing_channel_permissions(channel: discord.abc.GuildChannel, me: discord.Member) -> list[str]:
    perms = channel.permissions_for(me)
    return [name.replace("_", " ").title() for name in REQUIRED_CHANNEL_PERMISSIONS if not getattr(perms, name)]
