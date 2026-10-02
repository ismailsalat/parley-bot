"""Isolated, time-limited ad drafting channels with crash/restart cleanup.

Only text confirmed by the submitter is stored in a Listing. Temporary
channels and unconfirmed messages are disposable. Prefix is reserved for
Parley and used to recover orphan channels after DB/network failures.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.database.models import TemporaryAdSession
from bot.services.errors import Conflict, ValidationError
from bot.utils.helpers import utcnow
from bot.utils.mentions import safe_allowed_mentions

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)
PREFIX = "parley-temp-ad-"
SESSION_SECONDS = 600
MAX_ACTIVE_ROOMS = 15
_ACTIVE_USERS: set[int] = set()
# Orphans that Discord refused to delete at startup are retried on maintenance.
_ORPHAN_RETRY_IDS: set[int] = set()
_LOCK = asyncio.Lock()


class PasteDecision(discord.ui.View):
    """Short-lived confirmation. Removed with its channel at restart."""

    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.choice: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is someone else's private draft.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Ad", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self.choice = "confirm"
        self.stop()

    @discord.ui.button(label="Try Again", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self.choice = "retry"
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self.choice = "cancel"
        self.stop()


async def _delete_channel(bot: ParleyBot, channel_id: int, reason: str) -> bool:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("temp_ads.lookup_failed channel=%s error=%s", channel_id, exc)
            return False
    if not isinstance(channel, discord.TextChannel) or not channel.name.startswith(PREFIX):
        log.error("temp_ads.refused_unsafe_delete channel=%s", channel_id)
        return False
    try:
        await channel.delete(reason=reason)
        return True
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("temp_ads.delete_failed channel=%s error=%s", channel_id, exc)
        return False


async def cleanup(bot: ParleyBot, *, startup: bool = False) -> int:
    """Remove abandoned drafts on startup; only expired drafts at runtime.

    Rows remain until Discord confirms deletion; failures retry next maintenance
    pass/restart. Unknown prefix channels are swept only at *fresh* startup.
    """
    now = utcnow()
    async with bot.db.session() as session:
        query = select(TemporaryAdSession)
        if not startup:
            query = query.where(TemporaryAdSession.expires_at <= now)
        records = list(await session.scalars(query))
    deleted = 0
    for record in records:
        if await _delete_channel(bot, record.channel_id, "Parley abandoned ad draft cleanup"):
            async with bot.db.session() as session:
                row = await session.get(TemporaryAdSession, record.channel_id)
                if row is not None and (startup or row.expires_at <= now):
                    await session.delete(row)
            deleted += 1
    if startup:
        hub = bot.get_guild(bot.runtime.hub.main_guild_id)
        if hub is not None:
            known = {record.channel_id for record in records}
            for channel in hub.text_channels:
                if channel.name.startswith(PREFIX) and channel.id not in known:
                    _ORPHAN_RETRY_IDS.add(channel.id)
    for orphan_id in tuple(_ORPHAN_RETRY_IDS):
        if await _delete_channel(bot, orphan_id, "Parley orphan ad draft cleanup"):
            _ORPHAN_RETRY_IDS.discard(orphan_id)
            deleted += 1
    if deleted:
        log.info("temp_ads.cleaned count=%s startup=%s", deleted, startup)
    return deleted


async def capture_pasted_ad(
    bot: ParleyBot, *, user_id: int, guild_id: int,
    notify_channel: Callable[[discord.TextChannel], Awaitable[None]] | None = None,
    save_confirmed: Callable[[str], Awaitable[None]] | None = None,
    preview_text: Callable[[str], str] | None = None,
) -> str:
    """Return only explicitly confirmed text; always close draft room.

    A gateway outage destroys the in-memory confirmation. The saved DB row and
    channel name allow startup cleanup. No publishing happens until text has
    been confirmed and written to the listing database.
    """
    if not bot.intents.message_content:
        raise ValidationError("Paste Existing Ad requires the Message Content intent in Discord Developer Portal.")
    hub = bot.get_guild(bot.runtime.hub.main_guild_id)
    if hub is None:
        raise ValidationError("Join the main Parley server before using Paste Existing Ad.")
    member = hub.get_member(user_id)
    if member is None:
        try:
            member = await hub.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden):
            raise ValidationError("Join the main Parley server to open a private posting channel.") from None
    me = hub.me
    if me is None or not me.guild_permissions.manage_channels:
        raise ValidationError("Parley needs Manage Channels permission to create temporary private draft rooms.")
    async with _LOCK:
        if user_id in _ACTIVE_USERS:
            raise Conflict("You already have a draft room open. Finish or wait for it to expire.")
        _ACTIVE_USERS.add(user_id)
    channel = None
    expires = utcnow() + timedelta(seconds=SESSION_SECONDS)
    try:
        async with bot.db.session() as session:
            count = await session.scalar(select(func.count()).select_from(TemporaryAdSession))
            if count is not None and count >= MAX_ACTIVE_ROOMS:
                raise ValidationError("Parley's private draft rooms are busy. Please try again in a few minutes.")
            old = await session.scalar(select(TemporaryAdSession).where(TemporaryAdSession.user_id == user_id))
            if old is not None:
                if old.expires_at > utcnow():
                    raise Conflict("A previous draft is still open. Wait for it to expire or ask staff to close it.")
                if not await _delete_channel(bot, old.channel_id, "Expired Parley draft replacement"):
                    raise ValidationError("Your previous draft couldn't be closed. Ask Parley staff for help.")
                await session.delete(old)
        overwrites = {
            hub.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        for role_id in bot.runtime.hub.staff_role_ids:
            role = hub.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        channel = await hub.create_text_channel(
            f"{PREFIX}{user_id}", overwrites=overwrites,
            reason="Temporary private Parley advertisement draft (10 minute limit)",
        )
        async with bot.db.session() as session:
            session.add(TemporaryAdSession(
                channel_id=channel.id, hub_guild_id=hub.id,
                advertised_guild_id=guild_id, user_id=user_id,
                created_at=utcnow(), expires_at=expires,
            ))
            try:
                await session.flush()
            except IntegrityError as exc:
                raise Conflict("You already have a private draft session.") from exc
        if notify_channel is not None:
            await notify_channel(channel)
        await channel.send(
            f"<@{user_id}> **Your private Parley draft is ready.**\n"
            "Paste **one** existing text advertisement here within **10 minutes**. "
            "It is visible only to you and authorized staff.\n"
            "After you paste, **Confirm Ad** or **Try Again**. "
            "This channel will be deleted after submission, cancellation, timeout, or a bot restart. "
            "Never paste passwords or secrets.",
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)], roles=False, everyone=False),
        )
        for _ in range(3):
            remaining = max(0.0, (expires - utcnow()).total_seconds())
            if remaining < 15:
                raise ValidationError("The draft session expired. No advertisement was submitted.")
            try:
                message = await bot.wait_for(
                    "message",
                    check=lambda m: m.channel.id == channel.id and m.author.id == user_id,
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                raise ValidationError("The draft session expired. Nothing was submitted.") from None
            if message.attachments or message.stickers:
                await channel.send("Please paste **text only**; attachments and stickers aren't supported yet.")
                continue
            content = (message.content or "").strip()
            if not content:
                await channel.send("The message is empty. Paste your ad as text.")
                continue
            # Validation and final category binding also run inside the DB writer.
            # This preview will be exactly what will be stored (plus the guild invite if absent).
            # The caller supplies the invite and category, so validate here only the
            # text limit/mentions and defer full link checks to submission.
            from bot.services import listings as listing_service
            from bot.services.errors import ParleyError
            try:
                content = listing_service.clean_advertisement(content, bot.runtime)
                if any(token in content.lower() for token in ("@everyone", "@here", "<@", "<#!", "<@&")):
                    raise ValidationError("Advertisements cannot contain mass, member, or role mentions.")
            except ParleyError as exc:
                await channel.send(f"⚠️ {exc.user_message} Paste a corrected ad.")
                continue
            # Display the complete, server-bound advertisement, including the
            # validated invite that publication will attach. Never truncate text
            # that the user must approve.
            try:
                final_preview = preview_text(content) if preview_text else content
            except ParleyError as exc:
                await channel.send(f"⚠️ {exc.user_message} Paste a corrected ad.")
                continue
            view = PasteDecision(user_id)
            posted = await channel.send(
                "## Review your advertisement\n"
                "Everything below is what Parley will publish. Confirm to save it, "
                "or Try Again to change it. Reposting and editing follow Parley's cooldowns.",
                embed=discord.Embed(title="Advertisement preview", description=final_preview, color=0xFACC15),
                view=view,
                allowed_mentions=safe_allowed_mentions(),
            )
            try:
                await asyncio.wait_for(view.wait(), timeout=max(0.0, (expires - utcnow()).total_seconds()))
            except asyncio.TimeoutError:
                pass
            try:
                await posted.edit(view=None)
            except discord.HTTPException:
                pass
            if view.choice == "confirm":
                # Durable before cleanup. A crash after commit but before deleting
                # this channel cannot lose an approved or pending advertisement.
                if save_confirmed is not None:
                    await save_confirmed(content)
                return content
            if view.choice == "cancel":
                raise ValidationError("Draft cancelled; no advertisement was submitted.")
            if view.choice != "retry":
                raise ValidationError("Confirmation expired. Nothing was submitted.")
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await channel.send("Okay, paste your corrected advertisement below.")
        raise ValidationError("Too many attempts. Start a new draft session.")
    finally:
        try:
            if channel is not None:
                removed = await _delete_channel(bot, channel.id, "Parley private advertisement draft closed")
                if removed:
                    async with bot.db.session() as session:
                        row = await session.get(TemporaryAdSession, channel.id)
                        if row is not None:
                            await session.delete(row)
        finally:
            async with _LOCK:
                _ACTIVE_USERS.discard(user_id)
