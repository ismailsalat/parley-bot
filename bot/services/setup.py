"""First-run setup of the main Parley server.

Automatic Setup creates (or reuses, by name) exactly five channels:
#start-here, #server-directory, #find-partners, #support and a staff-only
#waypoint-logs. Legacy channel names are reused so upgrades never create
duplicates. It needs Manage Channels; without it the owner picks existing
channels instead. Nothing here requires Administrator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import discord

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelSlot:
    key: str  # HubConfig field name
    name: str  # default channel name
    label: str
    required: bool
    topic: str
    legacy_names: tuple[str, ...] = ()


SLOTS: tuple[ChannelSlot, ...] = (
    ChannelSlot(
        "welcome_channel_id", "start-here", "Start here", True,
        "Start here: list your server or find partners.", ("welcome",),
    ),
    ChannelSlot(
        "listings_channel_id", "server-directory", "Server directory", True,
        "Browse server ads. Posting is locked; use Post My Server to list yours and Relist to move it back to the top.",
        ("partner-listings",),
    ),
    ChannelSlot(
        "looking_channel_id", "find-partners", "Find partners", True,
        "Post a partner ad or browse matches with Parley.",
        ("looking-for-partners",),
    ),
    ChannelSlot("support_channel_id", "support", "Support", False, "Questions about Parley."),
    ChannelSlot("log_channel_id", "parley-logs", "Staff logs", False, "Parley staff log and approvals.", ("waypoint-logs",)),
)
SLOT_BY_KEY = {slot.key: slot for slot in SLOTS}


def bot_channel_permissions() -> discord.PermissionOverwrite:
    """What Parley itself needs in every channel it manages."""
    return discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True, embed_links=True, use_external_emojis=True
    )


def listings_channel_permissions() -> discord.PermissionOverwrite:
    """The listings channel also needs message and permission management, so Parley can
    open a one-off posting window for "Paste My Own Ad" and clean up afterwards."""
    overwrite = bot_channel_permissions()
    overwrite.update(manage_messages=True, manage_roles=True)
    return overwrite


def overwrites_for(
    slot: ChannelSlot, guild: discord.Guild, staff_roles: list[discord.Role]
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """Channel permissions for a newly created channel.

    * start-here / server-directory: members can read but not post (the bot manages them)
    * waypoint-logs: hidden from everyone except staff roles, admins and the bot
    """
    everyone = guild.default_role
    own = listings_channel_permissions() if slot.key in ("listings_channel_id", "looking_channel_id") else bot_channel_permissions()
    result: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {guild.me: own}
    if slot.key in ("welcome_channel_id", "listings_channel_id", "looking_channel_id"):
        result[everyone] = discord.PermissionOverwrite(send_messages=False, create_public_threads=False)
    elif slot.key == "log_channel_id":
        result[everyone] = discord.PermissionOverwrite(view_channel=False)
        for role in staff_roles:
            result[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
    return result


@dataclass
class SetupResult:
    channels: dict[str, discord.TextChannel] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(slot.key in self.channels for slot in SLOTS if slot.required)


def can_auto_setup(guild: discord.Guild) -> bool:
    return guild.me.guild_permissions.manage_channels


def find_existing(guild: discord.Guild, slot: ChannelSlot) -> discord.TextChannel | None:
    """Reuse the recommended name or a legacy name from an older Parley install."""
    names = {slot.name, *slot.legacy_names}
    for channel in guild.text_channels:
        if channel.name in names:
            return channel
    return None


async def _repair_reused_channel(slot: ChannelSlot, channel: discord.TextChannel, guild: discord.Guild) -> None:
    """Apply only Parley's essential safety overwrites to a reused default channel."""
    if guild.me is None or not channel.permissions_for(guild.me).manage_roles:
        return
    try:
        if slot.key in ("listings_channel_id", "looking_channel_id"):
            everyone = guild.default_role
            current = channel.overwrites_for(everyone)
            allow, deny = current.pair()
            locked = discord.PermissionOverwrite.from_pair(allow, deny)
            locked.send_messages = False
            if hasattr(locked, "create_public_threads"):
                locked.create_public_threads = False
            if hasattr(locked, "send_messages_in_threads"):
                locked.send_messages_in_threads = False
            await channel.set_permissions(everyone, overwrite=locked, reason="Parley: lock server directory")

            mine = channel.overwrites_for(guild.me)
            a2, d2 = mine.pair()
            bot_ow = discord.PermissionOverwrite.from_pair(a2, d2)
            bot_ow.update(
                view_channel=True, send_messages=True, read_message_history=True, embed_links=True,
                use_external_emojis=True, manage_messages=True, manage_roles=True,
            )
            await channel.set_permissions(guild.me, overwrite=bot_ow, reason="Parley: directory permissions")
        elif slot.key == "welcome_channel_id":
            everyone = guild.default_role
            current = channel.overwrites_for(everyone)
            allow, deny = current.pair()
            locked = discord.PermissionOverwrite.from_pair(allow, deny)
            locked.send_messages = False
            await channel.set_permissions(everyone, overwrite=locked, reason="Parley: lock start-here")
    except discord.HTTPException as exc:
        log.warning("setup.permission_repair_failed channel=%s guild_id=%s: %s", channel.name, guild.id, exc)


async def automatic_setup(guild: discord.Guild, staff_roles: list[discord.Role]) -> SetupResult:
    """Create or reuse the recommended channels and enforce the directory lock."""
    result = SetupResult()
    for slot in SLOTS:
        existing = find_existing(guild, slot)
        if existing is not None:
            result.channels[slot.key] = existing
            result.reused.append(existing.name)
            await _repair_reused_channel(slot, existing, guild)
            continue
        try:
            channel = await guild.create_text_channel(
                slot.name,
                topic=slot.topic,
                overwrites=overwrites_for(slot, guild, staff_roles),
                reason="Parley automatic setup",
            )
        except discord.HTTPException as exc:
            log.warning("setup.create_failed channel=%s guild_id=%s: %s", slot.name, guild.id, exc)
            result.failed.append(slot.name)
            continue
        result.channels[slot.key] = channel
        result.created.append(slot.name)
    log.info(
        "setup.automatic guild_id=%s created=%s reused=%s failed=%s",
        guild.id, result.created, result.reused, result.failed,
    )
    return result
