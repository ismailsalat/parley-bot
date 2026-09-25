"""First-run setup of the main Parley server.

Automatic Setup creates (or reuses, by name) Parley's core guided channel set:
#👋・start-here, #📣・server-directory, #🤝・partner-board, #📖・how-parley-works,
#💬・support and a staff-only #🛡️・parley-logs. Parley Perks is intentionally
placed separately by the owner so it can live in an existing channel or in a
new read-only #💎・parley-perks channel. Nothing here requires Administrator.
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
        "welcome_channel_id", "👋・start-here", "Start here", True,
        "Welcome to Parley. Start here to list a server, browse communities, and learn how partnerships work.",
        ("start-here", "welcome"),
    ),
    ChannelSlot(
        "listings_channel_id", "📣・server-directory", "Server directory", True,
        "Browse live server ads. Use the buttons on listings to join or request a partnership.",
        ("server-directory", "partner-listings"),
    ),
    ChannelSlot(
        "looking_channel_id", "🤝・partner-board", "Partner board", True,
        "Servers here are actively looking for partnerships. Use Parley to find a match or manage your own partner post.",
        ("partner-board", "find-partners", "looking-for-partners"),
    ),
    ChannelSlot(
        "perks_channel_id", "📖・how-parley-works", "How Parley Works", False,
        "A simple guide to Server Directory listings, Network setup, Find Partners, requests, Relist, and ad exchange.",
        ("how-parley-works",),
    ),
    ChannelSlot(
        "benefits_channel_id", "💎・parley-perks", "Parley Perks", False,
        "Benefits unlocked by adding Parley to your server. Read-only guide with quick setup links.",
        ("parley-perks",),
    ),
    ChannelSlot(
        "support_channel_id", "💬・support", "Support", False,
        "Questions, setup help, or problems with listings and partnerships.",
        ("support",),
    ),
    ChannelSlot(
        "log_channel_id", "🛡️・parley-logs", "Staff logs", False,
        "Parley staff logs and approvals.",
        ("parley-logs", "waypoint-logs"),
    ),
)

SLOT_BY_KEY = {slot.key: slot for slot in SLOTS}

# Parley Perks is intentionally excluded from Automatic Setup. The setup wizard
# asks where the owner wants the panel before creating/posting anything.
AUTO_SETUP_SLOTS: tuple[ChannelSlot, ...] = tuple(
    slot for slot in SLOTS if slot.key != "benefits_channel_id"
)


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
    * how-parley-works / parley-perks: read-only guides for members
    * parley-logs: hidden from everyone except staff roles, admins and the bot
    """
    everyone = guild.default_role
    own = listings_channel_permissions() if slot.key in ("listings_channel_id", "looking_channel_id") else bot_channel_permissions()
    result: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {guild.me: own}
    if slot.key in ("welcome_channel_id", "listings_channel_id", "looking_channel_id", "perks_channel_id", "benefits_channel_id"):
        result[everyone] = discord.PermissionOverwrite(
            send_messages=False, create_public_threads=False, create_private_threads=False, send_messages_in_threads=False
        )
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
    """Bring a reused legacy channel up to Parley's current safe defaults."""
    if guild.me is None:
        return

    if channel.name != slot.name and channel.permissions_for(guild.me).manage_channels:
        try:
            await channel.edit(name=slot.name, topic=slot.topic, reason="Parley: refresh channel name and topic")
        except discord.HTTPException as exc:
            log.info("setup.channel_rename_failed channel=%s wanted=%s: %s", channel.name, slot.name, exc)

    if not channel.permissions_for(guild.me).manage_roles:
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
            await channel.set_permissions(everyone, overwrite=locked, reason="Parley: lock managed feed")

            mine = channel.overwrites_for(guild.me)
            a2, d2 = mine.pair()
            bot_ow = discord.PermissionOverwrite.from_pair(a2, d2)
            bot_ow.update(
                view_channel=True, send_messages=True, read_message_history=True, embed_links=True,
                use_external_emojis=True, manage_messages=True, manage_roles=True,
            )
            await channel.set_permissions(guild.me, overwrite=bot_ow, reason="Parley: managed feed permissions")
        elif slot.key in ("welcome_channel_id", "perks_channel_id", "benefits_channel_id"):
            everyone = guild.default_role
            current = channel.overwrites_for(everyone)
            allow, deny = current.pair()
            locked = discord.PermissionOverwrite.from_pair(allow, deny)
            locked.send_messages = False
            if hasattr(locked, "create_public_threads"):
                locked.create_public_threads = False
            if hasattr(locked, "send_messages_in_threads"):
                locked.send_messages_in_threads = False
            await channel.set_permissions(everyone, overwrite=locked, reason="Parley: lock read-only guide")
    except discord.HTTPException as exc:
        log.warning("setup.permission_repair_failed channel=%s guild_id=%s: %s", channel.name, guild.id, exc)


async def automatic_setup(guild: discord.Guild, staff_roles: list[discord.Role]) -> SetupResult:
    """Create or reuse the recommended channels and enforce the directory lock."""
    result = SetupResult()
    for slot in AUTO_SETUP_SLOTS:
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
