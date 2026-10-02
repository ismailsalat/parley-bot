"""First-run setup of the main Parley server.

Automatic Setup creates (or reuses, by name) Parley's core guided channel set:
#👋・start-here, #📣・server-directory, #🤝・partner-board, #📖・how-parley-works,
#💬・support and a staff-only #🛡️・parley-logs. Parley Perks is intentionally
placed separately by the owner so it can live in an existing channel or in a
new read-only #💎・parley-perks channel. Nothing here requires Administrator.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import discord

log = logging.getLogger(__name__)

# Automatic Setup and the panel service's startup upgrade both create channels.
# Serialize their find/create operations to avoid creating two #ad-approvals
# channels when setup and recovery execute at about the same time.
_CHANNEL_CREATION_LOCK = asyncio.Lock()


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
        "Operational events and audit trail (approvals are in #ad-approvals).",
        ("parley-logs", "waypoint-logs"),
    ),
    ChannelSlot(
        "review_channel_id", "📝・ad-approvals", "Ad approvals", False,
        "Private staff queue: approve or reject community submissions here.",
        ("ad-approvals", "server-approvals"),
    ),
    ChannelSlot(
        "rules_channel_id", "📜・server-rules", "Server rules", False,
        "Parley rules for advertisements, server listings and partnerships.",
        ("server-rules", "rules"),
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
    """Lock public directory posting; legacy connected self-post windows still cleanly recover."""
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
    if slot.key in ("welcome_channel_id", "listings_channel_id", "looking_channel_id", "perks_channel_id", "benefits_channel_id", "rules_channel_id"):
        result[everyone] = discord.PermissionOverwrite(
            send_messages=False, create_public_threads=False, create_private_threads=False, send_messages_in_threads=False
        )
    elif slot.key in ("log_channel_id", "review_channel_id"):
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


def _matching_channels(channels, slot: ChannelSlot) -> list[discord.TextChannel]:
    names = {slot.name, *slot.legacy_names}
    return [
        channel for channel in channels
        if channel.name in names
        and getattr(channel, "type", discord.ChannelType.text) in (discord.ChannelType.text, discord.ChannelType.news)
    ]


def find_existing(guild: discord.Guild, slot: ChannelSlot, *, preferred_id: int = 0) -> discord.TextChannel | None:
    """Reuse the recommended name or a legacy name from an older Parley install."""
    matches = _matching_channels(guild.text_channels, slot)
    if not matches:
        return None
    # Preserve a previously configured channel, especially if it already has
    # pending approvals. Otherwise use the oldest ID, not channel sidebar order.
    return next((c for c in matches if c.id == preferred_id), min(matches, key=lambda c: c.id))


async def get_or_create_channel(
    guild: discord.Guild,
    slot: ChannelSlot,
    staff_roles: list[discord.Role],
    *,
    preferred_id: int = 0,
) -> tuple[discord.TextChannel, bool]:
    """Atomically reuse a channel or create it, even if the gateway cache lags.

    Discord's REST channel list is checked just before creating when the cache
    has no match. On REST failure we *do not create*: that could duplicate a
    channel that the websocket hasn't delivered yet.
    """
    async with _CHANNEL_CREATION_LOCK:
        existing = find_existing(guild, slot, preferred_id=preferred_id)
        if existing is not None:
            return existing, False

        fetch_channels = getattr(guild, "fetch_channels", None)
        if callable(fetch_channels):
            remote = await fetch_channels()
            matches = _matching_channels(remote, slot)
            if matches:
                chosen = next(
                    (c for c in matches if c.id == preferred_id),
                    min(matches, key=lambda c: c.id),
                )
                return chosen, False

        created = await guild.create_text_channel(
            slot.name,
            topic=slot.topic,
            overwrites=overwrites_for(slot, guild, staff_roles),
            reason="Parley automatic setup",
        )
        return created, True


async def reconcile_duplicate_approvals(guild: discord.Guild, *, canonical_id: int) -> None:
    """Safely consolidate an accidentally duplicated approvals channel.

    Never erase an approval request or historic staff decision. Delete only
    channels confirmed empty; rename non-empty extras to archives so there is
    just one live `ad-approvals` destination. Archived buttons still work.
    """
    slot = SLOT_BY_KEY["review_channel_id"]
    async with _CHANNEL_CREATION_LOCK:
        channels = list(guild.text_channels)
        fetch_channels = getattr(guild, "fetch_channels", None)
        if callable(fetch_channels):
            try:
                channels = list(await fetch_channels())
            except discord.HTTPException as exc:
                log.warning("setup.approvals_duplicate_scan_failed guild_id=%s: %s", guild.id, exc)
                return  # cannot prove which channels exist; change nothing
        duplicates = [c for c in channels if c.name == slot.name and c.id != canonical_id]
        for extra in duplicates:
            try:
                # We must be able to inspect history before declaring it empty.
                history = getattr(extra, "history", None)
                if not callable(history):
                    log.warning("setup.approvals_duplicate_unchecked channel_id=%s", extra.id)
                    continue
                has_messages = False
                async for _message in history(limit=1):
                    has_messages = True
                    break
                if not has_messages:
                    await extra.delete(reason="Parley: empty duplicate ad-approvals channel")
                    log.info("setup.approvals_duplicate_removed channel_id=%s", extra.id)
                else:
                    # Retain pending approvals and audit history. Renaming only
                    # affects the display name, never the existing messages.
                    secure = getattr(extra, "set_permissions", None)
                    if callable(secure):
                        await secure(
                            guild.default_role, view_channel=False,
                            reason="Parley: keep archived approvals staff-only",
                        )
                    await extra.edit(
                        name=f"ad-approvals-archive-{extra.id % 100000:05d}",
                        reason="Parley: preserve old approval history; use configured queue",
                    )
                    log.warning("setup.approvals_duplicate_archived channel_id=%s", extra.id)
            except (discord.HTTPException, discord.Forbidden) as exc:
                log.warning("setup.approvals_duplicate_cleanup_failed channel_id=%s: %s", extra.id, exc)


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


async def automatic_setup(
    guild: discord.Guild,
    staff_roles: list[discord.Role],
    *,
    preferred_channels: dict[str, int] | None = None,
) -> SetupResult:
    """Create or reuse the recommended channels and enforce the directory lock."""
    result = SetupResult()
    for slot in AUTO_SETUP_SLOTS:
        try:
            channel, created = await get_or_create_channel(
                guild, slot, staff_roles,
                preferred_id=(preferred_channels or {}).get(slot.key, 0),
            )
        except discord.HTTPException as exc:
            log.warning("setup.create_failed channel=%s guild_id=%s: %s", slot.name, guild.id, exc)
            result.failed.append(slot.name)
            continue
        result.channels[slot.key] = channel
        if created:
            result.created.append(slot.name)
        else:
            result.reused.append(channel.name)
            await _repair_reused_channel(slot, channel, guild)
    approvals = result.channels.get("review_channel_id")
    if approvals is not None:
        await reconcile_duplicate_approvals(guild, canonical_id=approvals.id)
    log.info(
        "setup.automatic guild_id=%s created=%s reused=%s failed=%s",
        guild.id, result.created, result.reused, result.failed,
    )
    return result
