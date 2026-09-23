"""Controlled owner-authored advertisements for #server-directory.

"Paste My Own Ad" means exactly that: the verified server manager posts one
real Discord message in #server-directory. Waypoint opens a short one-message
window, closes it immediately after the first message, moderates the content,
and keeps the channel locked for everyone else.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from bot.database import repository
from bot.database.models import Listing
from bot.services import listings as listing_service
from bot.services import permissions
from bot.services.errors import ValidationError
from bot.utils.helpers import format_members, utcnow
from bot.utils.mentions import safe_allowed_mentions

if TYPE_CHECKING:
    from bot.core import WaypointBot

log = logging.getLogger(__name__)


@dataclass
class ActiveWindow:
    user_id: int
    listing_guild_id: int
    first_message_id: int | None = None


# Separate windows may coexist in the same directory channel. The key includes
# the member because Discord permission overwrites are per member.
_ACTIVE: dict[tuple[int, int], ActiveWindow] = {}
_ACTIVE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str = ""


def active_window(channel_id: int, user_id: int) -> ActiveWindow | None:
    return _ACTIVE.get((channel_id, user_id))


def is_active_submission(channel_id: int, user_id: int, message_id: int | None = None) -> bool:
    window = _ACTIVE.get((channel_id, user_id))
    if window is None:
        return False
    if message_id is None:
        return True
    if window.first_message_id is None:
        window.first_message_id = message_id
        return True
    return window.first_message_id == message_id


def availability(bot: WaypointBot, guild_id: int) -> Availability:
    """Whether direct posting can be offered safely right now."""
    rules = bot.runtime.listings
    if not rules.allow_self_post:
        return Availability(False, "Posting your own ad is switched off by Waypoint staff.")
    if not bot.intents.message_content:
        return Availability(
            False,
            "Waypoint can't read the ad to check it. Enable the **Message Content** intent for the bot first.",
        )
    channel = bot.panels.listings_channel()
    if channel is None:
        return Availability(False, "The server directory isn't set up yet.")
    me = channel.guild.me
    perms = channel.permissions_for(me)
    if not (perms.manage_roles and perms.manage_messages):
        return Availability(
            False,
            f"Waypoint needs **Manage Permissions** and **Manage Messages** in #{channel.name} to open a "
            "one-message posting window safely.",
        )
    return Availability(True)


def _instructions(channel: discord.TextChannel, seconds: int, user: discord.abc.User) -> str:
    minutes = max(1, seconds // 60)
    url = f"https://discord.com/channels/{channel.guild.id}/{channel.id}"
    return (
        "## Your posting window is open\n"
        f"**Go to:** [#{channel.name}]({url})\n"
        f"**Time limit:** {minutes} minute{'s' if minutes != 1 else ''}\n\n"
        "Send your advertisement as **one message**. As soon as you post, Waypoint locks your "
        "posting permission again automatically.\n"
        "-# I also pinged you in the directory so the channel is easy to find."
    )


def _previous_overwrite(channel: discord.TextChannel, member: discord.Member) -> discord.PermissionOverwrite | None:
    """Return the exact member overwrite if one already exists."""
    try:
        for target, overwrite in channel.overwrites.items():
            if getattr(target, "id", None) == member.id:
                return overwrite
    except (AttributeError, TypeError):
        pass
    return None


async def _grant(
    channel: discord.TextChannel, member: discord.Member, previous: discord.PermissionOverwrite | None
) -> None:
    temporary = discord.PermissionOverwrite()
    if previous is not None:
        allow, deny = previous.pair()
        temporary = discord.PermissionOverwrite.from_pair(allow, deny)
    temporary.send_messages = True
    # The direct-post path exists partly so the member's own custom emoji can render.
    if hasattr(temporary, "use_external_emojis"):
        temporary.use_external_emojis = True
    elif hasattr(temporary, "external_emojis"):
        temporary.external_emojis = True
    await channel.set_permissions(
        member, overwrite=temporary, reason="Waypoint: one-message advertisement window"
    )


async def _restore(
    channel: discord.TextChannel, member: discord.Member, previous: discord.PermissionOverwrite | None
) -> bool:
    """Restore exactly what the member had before the temporary posting window."""
    try:
        await channel.set_permissions(member, overwrite=previous, reason="Waypoint: advertisement window closed")
        return True
    except discord.HTTPException as exc:
        log.warning("self_post.restore_failed guild=%s member=%s: %s", channel.guild.id, member.id, exc)
        return False


def _overwrite_bits(previous: discord.PermissionOverwrite | None) -> tuple[bool, int, int]:
    if previous is None:
        return False, 0, 0
    allow, deny = previous.pair()
    return True, allow.value, deny.value


def _overwrite_from_bits(had_overwrite: bool, allow_value: int, deny_value: int) -> discord.PermissionOverwrite | None:
    if not had_overwrite:
        return None
    allow = discord.Permissions(allow_value)
    deny = discord.Permissions(deny_value)
    return discord.PermissionOverwrite.from_pair(allow, deny)


async def restore_abandoned_sessions(bot: WaypointBot) -> int:
    """Close any posting window left behind by a crash/redeploy.

    A persisted row exists before Waypoint grants Send Messages. On a clean close
    the row is removed. Therefore every row found at startup is abandoned and can
    be restored immediately, even if its original three-minute timer has not ended.
    """
    async with bot.db.session() as session:
        rows = await repository.list_self_post_sessions(session)
    if not rows:
        return 0

    restored_count = 0
    for row in rows:
        channel = bot.get_channel(row.channel_id)
        if channel is None or not hasattr(channel, "set_permissions"):
            # The channel vanished, so there is no overwrite left to restore.
            async with bot.db.session() as session:
                await repository.delete_self_post_session(session, row.channel_id, row.user_id)
            _ACTIVE.pop((row.channel_id, row.user_id), None)
            restored_count += 1
            continue

        member = channel.guild.get_member(row.user_id)
        if member is None:
            try:
                member = await channel.guild.fetch_member(row.user_id)
            except discord.NotFound:
                # A departed member cannot retain a usable member overwrite.
                async with bot.db.session() as session:
                    await repository.delete_self_post_session(session, row.channel_id, row.user_id)
                _ACTIVE.pop((row.channel_id, row.user_id), None)
                restored_count += 1
                continue
            except discord.HTTPException as exc:
                log.warning("self_post.recovery_member_failed channel=%s user=%s: %s", row.channel_id, row.user_id, exc)
                _ACTIVE[(row.channel_id, row.user_id)] = ActiveWindow(row.user_id, row.listing_guild_id)
                continue

        previous = _overwrite_from_bits(row.had_overwrite, row.previous_allow, row.previous_deny)
        if await _restore(channel, member, previous):
            async with bot.db.session() as session:
                await repository.delete_self_post_session(session, row.channel_id, row.user_id)
            _ACTIVE.pop((row.channel_id, row.user_id), None)
            restored_count += 1
            log.info("self_post.recovered channel=%s user=%s", row.channel_id, row.user_id)
        else:
            # Block new windows until a later restart/repair can restore safely.
            _ACTIVE[(row.channel_id, row.user_id)] = ActiveWindow(row.user_id, row.listing_guild_id)

    return restored_count


async def _delete(message: discord.Message) -> None:
    try:
        await message.delete()
    except discord.HTTPException as exc:
        log.info("self_post.delete_failed message=%s: %s", message.id, exc)


async def _ghost_ping(channel: discord.TextChannel, member: discord.Member) -> None:
    """Ping only the posting member, then immediately remove the ping message.

    Waypoint normally suppresses every mention. This is the one intentional
    exception: the user explicitly opened a short posting window, and the ping
    is scoped to that exact member (never roles, @here or @everyone).
    """
    try:
        ping = await channel.send(
            content=f"<@{member.id}>",
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[discord.Object(id=member.id)], replied_user=False
            ),
        )
        await _delete(ping)
    except discord.HTTPException as exc:
        # A notification helper must never break the actual posting flow.
        log.info("self_post.ghost_ping_failed channel=%s user=%s: %s", channel.id, member.id, exc)


async def run_submission(
    interaction: discord.Interaction,
    guild_id: int,
    guild_name: str,
    *,
    draft_contacts=None,
    on_created=None,
    on_accept: Callable[[str, discord.Message], Awaitable[None]] | None = None,
    on_review: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Open one member's three-minute, one-message posting window.

    Different servers can post concurrently. We only block a second window from
    the same member or another admin trying to post the same server at once.
    """
    bot: WaypointBot = interaction.client  # type: ignore[assignment]
    state = availability(bot, guild_id)
    if not state.ok:
        raise ValidationError(state.reason)
    channel = bot.panels.listings_channel()
    assert channel is not None
    member = channel.guild.get_member(interaction.user.id)
    if member is None:
        raise ValidationError("Join the Waypoint server first, then try again.")
    await permissions.require_manager(bot, guild_id, interaction.user.id)
    if not channel.permissions_for(member).view_channel:
        raise ValidationError(
            f"You need permission to view #{channel.name} before Waypoint can open a posting window for you."
        )

    key = (channel.id, member.id)
    async with _ACTIVE_LOCK:
        if key in _ACTIVE:
            raise ValidationError(
                f"You already have a posting window open in #{channel.name}. Finish it or wait for it to expire."
            )
        if any(w.listing_guild_id == guild_id for w in _ACTIVE.values()):
            raise ValidationError("Someone from your server already has a posting window open. Try again when it closes.")
        window = ActiveWindow(member.id, guild_id)
        _ACTIVE[key] = window

    seconds = bot.runtime.listings.self_post_timeout_seconds
    previous = _previous_overwrite(channel, member)
    had_overwrite, previous_allow, previous_deny = _overwrite_bits(previous)
    started_at = utcnow()
    granted = False
    restored = False
    session_saved = False
    try:
        async with bot.db.session() as session:
            await repository.save_self_post_session(
                session,
                channel_id=channel.id,
                hub_guild_id=channel.guild.id,
                listing_guild_id=guild_id,
                user_id=member.id,
                had_overwrite=had_overwrite,
                previous_allow=previous_allow,
                previous_deny=previous_deny,
                started_at=started_at,
                expires_at=started_at + timedelta(seconds=seconds),
            )
        session_saved = True

        await _grant(channel, member, previous)
        granted = True
        await _ghost_ping(channel, member)
        await interaction.edit_original_response(content=_instructions(channel, seconds, interaction.user), view=None)

        def check(message: discord.Message) -> bool:
            if message.channel.id != channel.id or message.author.id != member.id:
                return False
            if window.first_message_id is None:
                window.first_message_id = message.id
                return True
            return window.first_message_id == message.id

        try:
            message = await bot.wait_for("message", check=check, timeout=seconds)
        except asyncio.TimeoutError:
            log.info("self_post.timeout guild=%s user=%s", guild_id, member.id)
            return "## Posting expired\nNothing was changed. You can try again whenever you're ready."

        # Close this member's permission immediately. Other active posting windows
        # in the same channel stay open because each uses its own overwrite.
        restored = await _restore(channel, member, previous)
        if not restored:
            await _delete(message)
            raise ValidationError(
                "Waypoint couldn't close your posting window safely. Your ad was removed; try again after staff checks the channel."
            )

        if getattr(message, "attachments", None) or getattr(message, "stickers", None):
            await _delete(message)
            return "## Ad not posted\nListings are text-only right now."

        text = (message.content or "").strip()
        try:
            cleaned = listing_service.clean_advertisement(text, bot.runtime)
            needs_review = listing_service.check_links(cleaned, bot.runtime) or bot.runtime.listings.approval_required
        except ValidationError as exc:
            await _delete(message)
            log.info("self_post.rejected guild=%s user=%s: %s", guild_id, member.id, exc.user_message)
            return f"## Ad not posted\n{exc.user_message}\n\nYou can try again from **Post My Server** or **My Listing**."

        if needs_review:
            await _delete(message)
            if on_review is not None:
                await on_review(cleaned)
            else:
                await _store_text(bot, guild_id, cleaned, interaction.user.id)
            if on_created is not None:
                await on_created(cleaned, None)
            return "## Ad sent for review\nStaff will review it before it goes live."

        try:
            if on_accept is not None:
                await on_accept(cleaned, message)
            else:
                await _publish(bot, guild_id, cleaned, message, interaction.user.id)
        except Exception:
            await _delete(message)
            raise

        if on_created is not None:
            await on_created(cleaned, message)
        log.info("self_post.published guild=%s message=%s", guild_id, message.id)
        return "## Ad posted\nYour server is live in the directory."
    finally:
        async with _ACTIVE_LOCK:
            _ACTIVE.pop(key, None)
        if granted and not restored:
            restored = await _restore(channel, member, previous)
        # Keep the recovery row only when permission restoration failed.
        if session_saved and (not granted or restored):
            async with bot.db.session() as session:
                await repository.delete_self_post_session(session, channel.id, member.id)


async def _store_text(bot: WaypointBot, guild_id: int, text: str, actor_id: int) -> None:
    async with bot.db.session() as session:
        await listing_service.update_advertisement(
            session, bot.runtime, guild_id=guild_id, text=text, actor_id=actor_id, now=utcnow()
        )


async def _publish(bot: WaypointBot, guild_id: int, text: str, message: discord.Message, actor_id: int) -> None:
    """Adopt the owner's message, then place Waypoint's clean directory card under it."""
    async with bot.db.session() as session:
        listing = await repository.get_listing(session, guild_id)
        if listing is not None:
            listing.advertisement_text = text
            listing.updated_at = utcnow()
    await bot.panels.adopt_self_post(guild_id, message)


def directory_card_view(
    bot: WaypointBot,
    listing: Listing,
    *,
    ad_jump_url: str | None,
    include_view_ad: bool = True,
) -> discord.ui.View:
    """Compact public actions. The advertisement remains the visual focus."""
    from bot.views.partnership import request_button
    from bot.views.welcome import persistent_view

    items: list[discord.ui.Item] = []
    if include_view_ad and ad_jump_url:
        items.append(discord.ui.Button(label="View Ad", url=ad_jump_url))
    if listing.invite_url:
        items.append(discord.ui.Button(label="Join Server", url=listing.invite_url))
    if listing.accepting_partnerships:
        items.append(request_button(bot, listing.guild_id))
    return persistent_view(*items)


def directory_card_kwargs(
    bot: WaypointBot,
    listing: Listing,
    *,
    guild_name: str,
    member_count: int,
    icon_url: str | None,
    ad_jump_url: str | None,
    include_view_ad: bool = True,
    show_summary: bool = True,
) -> dict:
    """Minimal controls under an ad, or a tiny summary pointer after Relist."""
    view = directory_card_view(bot, listing, ad_jump_url=ad_jump_url, include_view_ad=include_view_ad)
    kwargs: dict = {"allowed_mentions": safe_allowed_mentions()}
    if show_summary:
        category = ", ".join(listing.categories) or "Other"
        kwargs["content"] = f"-# **{guild_name}** · {category} · {format_members(member_count)}"
    if view.children:
        kwargs["view"] = view
    return kwargs


# Backwards-compatible names used by older tests/callers. New code uses the
# directory-card helpers above.
def controls_view(bot: WaypointBot, guild_id: int, invite_url: str | None, accepting: bool) -> discord.ui.View:
    from bot.views.partnership import request_button
    from bot.views.welcome import persistent_view

    items: list[discord.ui.Item] = []
    if invite_url:
        items.append(discord.ui.Button(label="Join Server", url=invite_url))
    if accepting:
        items.append(request_button(bot, guild_id))
    return persistent_view(*items)


def controls_kwargs(bot: WaypointBot, guild_id: int, invite_url: str | None, accepting: bool) -> dict:
    view = controls_view(bot, guild_id, invite_url, accepting)
    kwargs: dict = {"content": "-# Server listing", "allowed_mentions": safe_allowed_mentions()}
    if view.children:
        kwargs["view"] = view
    return kwargs
