"""Listing messages and bottom panels in the main server.

Discord messages can't be moved, so "keeping the panel at the bottom" means:
delete the old panel, post the new message, post a fresh panel under it and
store its message ID. All message IDs live in the database, so this works
across restarts and the panel is recreated if someone deletes it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import listings as listing_service
from bot.services.errors import ValidationError
from bot.utils.helpers import listing_jump_url
from bot.utils.mentions import safe_allowed_mentions

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

LISTINGS_PANEL = "listings"
LOOKING_PANEL = "looking"
WELCOME_PANEL = "welcome"
PERKS_PANEL = "perks"

PanelBuilder = Callable[["ParleyBot"], tuple[str, discord.ui.View]]


class PanelService:
    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot
        # One lock per channel: listing posts and panel moves must never interleave.
        self._listings_lock = asyncio.Lock()
        self._looking_lock = asyncio.Lock()
        self._looking_task: asyncio.Task | None = None

    # ------------------------------------------------------------ channels

    def main_channel(self, channel_id: int | None) -> discord.TextChannel | None:
        main_guild_id = self.bot.runtime.hub.main_guild_id
        if not channel_id or not main_guild_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != main_guild_id:
            return None
        return channel

    def listings_channel(self) -> discord.TextChannel | None:
        return self.main_channel(self.bot.runtime.hub.listings_channel_id)

    def looking_channel(self) -> discord.TextChannel | None:
        return self.main_channel(self.bot.runtime.hub.looking_channel_id)

    def welcome_channel(self) -> discord.TextChannel | None:
        return self.main_channel(self.bot.runtime.hub.welcome_channel_id)

    def perks_channel(self) -> discord.TextChannel | None:
        return self.main_channel(self.bot.runtime.hub.perks_channel_id)

    async def _refresh_how_it_works_channel(self) -> None:
        """Rename the old Parley Perks channel in place on upgrade.

        The database key stays ``perks_channel_id`` for backward compatibility,
        but the user-facing channel is now the clearer ``#📖・how-parley-works``.
        This runs during normal panel restoration so existing hubs upgrade without
        requiring owners to rerun /setup.
        """
        channel = self.perks_channel()
        if channel is None or channel.guild.me is None:
            return
        desired_name = "📖・how-parley-works"
        desired_topic = (
            "A simple guide to Server Directory listings, Network setup, Find Partners, "
            "requests, Relist, and ad exchange."
        )
        if channel.name == desired_name and getattr(channel, "topic", None) == desired_topic:
            return
        permissions_for = getattr(channel, "permissions_for", None)
        if not callable(permissions_for) or not permissions_for(channel.guild.me).manage_channels:
            return
        edit = getattr(channel, "edit", None)
        if not callable(edit):
            return
        try:
            await edit(name=desired_name, topic=desired_topic, reason="Parley: refresh How Parley Works channel")
        except discord.HTTPException as exc:
            log.info("Could not refresh How Parley Works channel %s: %s", channel.id, exc)

    def panel_channel_ids(self) -> set[int]:
        h = self.bot.runtime.hub
        return {
            cid for cid in (h.listings_channel_id, h.looking_channel_id, h.welcome_channel_id, h.perks_channel_id) if cid
        }

    async def ensure_directory_locked(self) -> bool:
        """Keep both public feeds read-only except for Parley's short posting windows."""
        channels = [self.listings_channel(), self.looking_channel()]
        found = False
        all_ok = True
        for channel in channels:
            if channel is None or channel.guild.me is None:
                continue
            found = True
            me = channel.guild.me
            perms = channel.permissions_for(me)
            if not perms.manage_roles:
                all_ok = False
                continue
            everyone = channel.guild.default_role
            current = channel.overwrites_for(everyone)
            allow, deny = current.pair()
            updated = discord.PermissionOverwrite.from_pair(allow, deny)
            updated.send_messages = False
            if hasattr(updated, "create_public_threads"):
                updated.create_public_threads = False
            if hasattr(updated, "send_messages_in_threads"):
                updated.send_messages_in_threads = False
            try:
                await channel.set_permissions(everyone, overwrite=updated, reason="Parley: lock managed feed")
            except discord.HTTPException as exc:
                log.warning("Could not lock #%s: %s", channel.name, exc)
                all_ok = False
        return found and all_ok

    # ------------------------------------------------------------ low-level message helpers

    async def _delete(self, channel_id: int | None, message_id: int | None) -> None:
        if not channel_id or not message_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "get_partial_message"):
            return
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass  # already gone: that's the goal
        except discord.HTTPException as exc:
            log.warning("Could not delete message %s in channel %s: %s", message_id, channel_id, exc)

    async def delete_listing_message(self, channel_id: int | None, message_id: int | None) -> None:
        await self._delete(channel_id, message_id)

    async def take_down_listing(self, listing) -> None:
        """Delete every public post owned by a listing after removal/suspension/ban."""
        if listing is None:
            return
        await self._delete(listing.channel_id, listing.controls_message_id)
        await self._delete(listing.channel_id, listing.message_id)
        await self._delete(listing.partner_channel_id, listing.partner_controls_message_id)
        await self._delete(listing.partner_channel_id, listing.partner_message_id)
        async with self.bot.db.session() as session:
            await listing_service.record_message(session, guild_id=listing.guild_id, channel_id=None, message_id=None)
            current = await repository.get_listing(session, listing.guild_id)
            if current is not None:
                current.partner_ad_text = None
                current.partner_channel_id = None
                current.partner_message_id = None
                current.partner_controls_message_id = None
                current.partner_posted_at = None

    async def _is_newest(self, channel: discord.TextChannel, message_id: int) -> bool:
        async for newest in channel.history(limit=1):
            return newest.id == message_id
        return False

    # ------------------------------------------------------------ panels

    def _builder(self, panel_type: str) -> tuple[PanelBuilder, bool]:
        from bot.views import welcome

        panels = self.bot.runtime.panels
        return {
            LISTINGS_PANEL: (welcome.listings_panel, panels.listings_panel_enabled),
            LOOKING_PANEL: (welcome.looking_panel, panels.looking_panel_enabled),
            WELCOME_PANEL: (welcome.welcome_panel, panels.welcome_panel_enabled),
            PERKS_PANEL: (welcome.perks_panel, panels.perks_panel_enabled),
        }[panel_type]

    def _channel_for(self, panel_type: str) -> discord.TextChannel | None:
        return {
            LISTINGS_PANEL: self.listings_channel,
            LOOKING_PANEL: self.looking_channel,
            WELCOME_PANEL: self.welcome_channel,
            PERKS_PANEL: self.perks_channel,
        }[panel_type]()

    def _welcome_mentions(self) -> str | None:
        """Configured announcement mentions shown above Start Here."""
        panels = self.bot.runtime.panels
        parts: list[str] = []
        if panels.welcome_ping_everyone:
            parts.append("@everyone")
        parts.extend(f"<@&{role_id}>" for role_id in panels.welcome_ping_role_ids)
        return " ".join(parts) or None

    def _panel_payload(self, panel_type: str) -> tuple[str | None, discord.Embed | None, discord.ui.View]:
        """Render public panels as clean Discord-native markdown, never decorative image embeds."""
        builder, _enabled = self._builder(panel_type)
        text, view = builder(self.bot)
        if panel_type == WELCOME_PANEL:
            mentions = self._welcome_mentions()
            if mentions:
                text = f"{mentions}\n{text}"
        return text, None, view

    def _panel_mentions(self, panel_type: str, *, notify: bool) -> discord.AllowedMentions:
        """Only Start Here may intentionally ping, and only on its first creation."""
        if panel_type != WELCOME_PANEL or not notify:
            return safe_allowed_mentions()
        panels = self.bot.runtime.panels
        roles = [discord.Object(id=role_id) for role_id in panels.welcome_ping_role_ids]
        return discord.AllowedMentions(
            everyone=panels.welcome_ping_everyone,
            roles=roles,
            users=False,
            replied_user=False,
        )

    async def _repost_panel(self, panel_type: str, channel: discord.TextChannel) -> discord.Message | None:
        """Delete the stored panel and post a new one at the bottom of ``channel``."""
        guild_id = channel.guild.id
        async with self.bot.db.session() as session:
            stored = await repository.get_panel(session, guild_id, panel_type)
        if stored is not None:
            await self._delete(stored.channel_id, stored.message_id)

        _builder, enabled = self._builder(panel_type)
        message: discord.Message | None = None
        if enabled:
            content, embed, view = self._panel_payload(panel_type)
            # A public @everyone/role announcement is useful once, not on every repair.
            notify = stored is None
            try:
                message = await channel.send(
                    content=content,
                    embed=embed,
                    view=view,
                    allowed_mentions=self._panel_mentions(panel_type, notify=notify),
                )
            except discord.HTTPException as exc:
                log.error("Could not post the %s panel in #%s: %s", panel_type, channel.name, exc)
        async with self.bot.db.session() as session:
            await repository.save_panel(
                session,
                guild_id=guild_id,
                channel_id=channel.id,
                panel_type=panel_type,
                message_id=message.id if message else None,
            )
        return message

    async def ensure_panel(self, panel_type: str, *, keep_at_bottom: bool, force_edit: bool = False) -> str:
        """Make sure the panel exists (and optionally is the newest message).

        Existing panels are edited in place so text/button changes apply after
        a restart without creating new messages. Returns what happened:
        "ok", "created", "moved", "edited", "disabled", "no_channel" or "error".
        """
        channel = self._channel_for(panel_type)
        if channel is None:
            return "no_channel"
        _builder, enabled = self._builder(panel_type)
        async with self.bot.db.session() as session:
            stored = await repository.get_panel(session, channel.guild.id, panel_type)

        if not enabled:
            if stored is not None and stored.message_id:
                await self._repost_panel(panel_type, channel)  # deletes it and stores "no message"
            return "disabled"

        if stored is None or stored.message_id is None or stored.channel_id != channel.id:
            return "created" if await self._repost_panel(panel_type, channel) else "error"
        try:
            message = await channel.fetch_message(stored.message_id)
        except discord.NotFound:
            log.info("The %s panel was deleted; recreating it", panel_type)
            return "created" if await self._repost_panel(panel_type, channel) else "error"
        except discord.HTTPException as exc:
            log.warning("Could not check the %s panel: %s", panel_type, exc)
            return "error"

        if keep_at_bottom and not await self._is_newest(channel, message.id):
            return "moved" if await self._repost_panel(panel_type, channel) else "error"
        content, embed, view = self._panel_payload(panel_type)
        if force_edit or message.content != content:
            await message.edit(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=self._panel_mentions(panel_type, notify=False),
            )
            return "edited"
        return "ok"

    async def panel_status(self, panel_type: str) -> str:
        """Read-only check used by the Health Check: ok | missing | not_posted | disabled | no_channel | error."""
        channel = self._channel_for(panel_type)
        if channel is None:
            return "no_channel"
        _builder, enabled = self._builder(panel_type)
        if not enabled:
            return "disabled"
        async with self.bot.db.session() as session:
            stored = await repository.get_panel(session, channel.guild.id, panel_type)
        if stored is None or stored.message_id is None or stored.channel_id != channel.id:
            return "not_posted"
        try:
            await channel.fetch_message(stored.message_id)
        except discord.NotFound:
            return "missing"
        except discord.HTTPException:
            return "error"
        return "ok"

    async def restore_panels(self, *, force_edit: bool = False) -> dict[str, str]:
        """Called on startup, periodically and by Repair Panels. Safe to run any number of times.

        ``force_edit`` re-renders existing panels (after text or button changes in Settings).
        """
        results: dict[str, str] = {}
        await self._refresh_how_it_works_channel()
        async with self._listings_lock:
            results[LISTINGS_PANEL] = await self.ensure_panel(LISTINGS_PANEL, keep_at_bottom=True, force_edit=force_edit)
        async with self._looking_lock:
            results[LOOKING_PANEL] = await self.ensure_panel(LOOKING_PANEL, keep_at_bottom=False, force_edit=force_edit)
        results[WELCOME_PANEL] = await self.ensure_panel(WELCOME_PANEL, keep_at_bottom=False, force_edit=force_edit)
        results[PERKS_PANEL] = await self.ensure_panel(PERKS_PANEL, keep_at_bottom=False, force_edit=force_edit)
        return results

    async def post_above_panel(self, channel: discord.TextChannel, panel_type: str, **kwargs) -> discord.Message:
        """Post a message (e.g. a Test Center example) and move the panel under it."""
        lock = self._looking_lock if panel_type == LOOKING_PANEL else self._listings_lock
        async with lock:
            kwargs.setdefault("allowed_mentions", safe_allowed_mentions())
            message = await channel.send(**kwargs)
            await self._repost_panel(panel_type, channel)
        return message

    async def handle_message_deleted(self, channel_id: int, message_id: int) -> None:
        """Recreate a panel right away if someone deletes it."""
        main_guild_id = self.bot.runtime.hub.main_guild_id
        if channel_id not in self.panel_channel_ids() or not main_guild_id:
            return
        async with self.bot.db.session() as session:
            for panel_type in (LISTINGS_PANEL, LOOKING_PANEL, WELCOME_PANEL, PERKS_PANEL):
                stored = await repository.get_panel(session, main_guild_id, panel_type)
                if stored is not None and stored.message_id == message_id:
                    break
            else:
                return
        log.info("The %s panel was deleted; recreating it", panel_type)
        lock = self._looking_lock if panel_type == LOOKING_PANEL else self._listings_lock
        async with lock:
            await self.ensure_panel(panel_type, keep_at_bottom=panel_type == LISTINGS_PANEL)

    # ------------------------------------------------------------ listings

    async def publish_listing(self, guild_id: int) -> discord.Message | None:
        """(Re)post a listing at the bottom of #server-directory, then the panel under it.

        Returns None when the listing isn't active or no listings channel is set.
        """
        from bot.views.partnership import listing_message_kwargs

        channel = self.listings_channel()
        async with self._listings_lock:
            async with self.bot.db.session() as session:
                listing = await repository.get_listing(session, guild_id)
            if listing is None or listing.status != ListingStatus.ACTIVE:
                return None
            if channel is None:
                log.warning("listing.not_published guild_id=%s (listings channel not configured)", guild_id)
                return None

            # 1. publish the replacement FIRST. If Discord refuses the send, the
            # current live listing stays untouched and the user can try again.
            try:
                message = await channel.send(**listing_message_kwargs(self.bot, listing))
            except discord.HTTPException as exc:
                log.error("listing.publish_failed guild_id=%s: %s", guild_id, exc)
                if isinstance(exc, discord.Forbidden):
                    raise ValidationError(
                        f"Parley needs **Send Messages** in #{channel.name}. Ask a Parley admin to run the Health Check."
                    ) from exc
                raise

            # 2. now that a replacement exists, remove the previous copy/controls.
            await self._delete(listing.channel_id, listing.controls_message_id)
            if listing.message_id and listing.message_id != message.id:
                await self._delete(listing.channel_id, listing.message_id)

            # 3. store the new message ID.
            async with self.bot.db.session() as session:
                await listing_service.record_message(
                    session, guild_id=guild_id, channel_id=channel.id, message_id=message.id
                )
            # 4. panel underneath (reposts/deletes the old panel safely).
            await self._repost_panel(LISTINGS_PANEL, channel)
        log.info("listing.published guild_id=%s message_id=%s", guild_id, message.id)
        return message

    async def adopt_self_post(self, guild_id: int, message: discord.Message) -> None:
        """Adopt the owner's real ad and place a clean, bot-owned directory card under it."""
        from bot.views.self_post import directory_card_kwargs

        channel = self.listings_channel()
        if channel is None or message.channel.id != channel.id:
            return
        async with self._listings_lock:
            async with self.bot.db.session() as session:
                listing = await repository.get_listing(session, guild_id)
                stored_guild = await repository.get_guild(session, guild_id)
                if listing is None:
                    return
                old_channel, old_message, old_controls = listing.channel_id, listing.message_id, listing.controls_message_id

            live_guild = self.bot.get_guild(guild_id)
            guild_name = (live_guild.name if live_guild else None) or (stored_guild.name if stored_guild else f"Server {guild_id}")
            member_count = (live_guild.member_count if live_guild else None) or (stored_guild.member_count if stored_guild else 0)
            icon_url = (live_guild.icon.url if live_guild and live_guild.icon else None) or (stored_guild.icon_url if stored_guild else None)

            # Build the new card before deleting the previous live version. This
            # keeps the old listing intact if Discord rejects the new card send.
            controls = await channel.send(
                **directory_card_kwargs(
                    self.bot, listing, guild_name=guild_name, member_count=member_count,
                    icon_url=icon_url, ad_jump_url=message.jump_url, include_view_ad=False, show_summary=False,
                )
            )
            await self._delete(old_channel, old_controls)
            if old_message and old_message != message.id:
                await self._delete(old_channel, old_message)
            async with self.bot.db.session() as session:
                stored = await repository.get_listing(session, guild_id)
                if stored is not None:
                    stored.channel_id = channel.id
                    stored.message_id = message.id
                    stored.controls_message_id = controls.id
                    stored.self_posted = True
            await self._repost_panel(LISTINGS_PANEL, channel)
        log.info("listing.self_posted guild_id=%s message_id=%s", guild_id, message.id)

    async def relist_self_post(self, guild_id: int) -> discord.Message | None:
        """Move only Parley's directory card; never impersonate/repost the owner's ad."""
        from bot.views.self_post import directory_card_kwargs

        channel = self.listings_channel()
        if channel is None:
            return None
        async with self._listings_lock:
            async with self.bot.db.session() as session:
                listing = await repository.get_listing(session, guild_id)
                stored_guild = await repository.get_guild(session, guild_id)
            if listing is None or listing.status != ListingStatus.ACTIVE or not listing.self_posted:
                return None

            old_controls = listing.controls_message_id
            live_guild = self.bot.get_guild(guild_id)
            guild_name = (live_guild.name if live_guild else None) or (stored_guild.name if stored_guild else f"Server {guild_id}")
            member_count = (live_guild.member_count if live_guild else None) or (stored_guild.member_count if stored_guild else 0)
            icon_url = (live_guild.icon.url if live_guild and live_guild.icon else None) or (stored_guild.icon_url if stored_guild else None)
            ad_jump_url = listing_jump_url(channel.guild.id, listing.channel_id, listing.message_id)
            card = await channel.send(
                **directory_card_kwargs(
                    self.bot, listing, guild_name=guild_name, member_count=member_count,
                    icon_url=icon_url, ad_jump_url=ad_jump_url,
                )
            )
            # Only remove the old directory card after the new one exists.
            await self._delete(listing.channel_id, old_controls)
            async with self.bot.db.session() as session:
                stored = await repository.get_listing(session, guild_id)
                if stored is not None:
                    stored.controls_message_id = card.id
            await self._repost_panel(LISTINGS_PANEL, channel)
        log.info("listing.relisted_card guild_id=%s message_id=%s", guild_id, card.id)
        return card

    async def self_post_message_removed(self, guild_id: int) -> None:
        """Keep the clean directory card usable after an owner directly edits/deletes their ad."""
        from bot.views.self_post import directory_card_kwargs

        channel = self.listings_channel()
        if channel is None:
            return
        async with self._listings_lock:
            async with self.bot.db.session() as session:
                listing = await repository.get_listing(session, guild_id)
                stored_guild = await repository.get_guild(session, guild_id)
                if listing is None:
                    return
                listing.message_id = None
                listing.channel_id = channel.id
            await self._delete(channel.id, listing.controls_message_id)
            live_guild = self.bot.get_guild(guild_id)
            guild_name = (live_guild.name if live_guild else None) or (stored_guild.name if stored_guild else f"Server {guild_id}")
            member_count = (live_guild.member_count if live_guild else None) or (stored_guild.member_count if stored_guild else 0)
            icon_url = (live_guild.icon.url if live_guild and live_guild.icon else None) or (stored_guild.icon_url if stored_guild else None)
            card = await channel.send(
                **directory_card_kwargs(
                    self.bot, listing, guild_name=guild_name, member_count=member_count,
                    icon_url=icon_url, ad_jump_url=None,
                )
            )
            async with self.bot.db.session() as session:
                stored = await repository.get_listing(session, guild_id)
                if stored is not None:
                    stored.controls_message_id = card.id
            await self._repost_panel(LISTINGS_PANEL, channel)

    async def update_self_post_card(self, guild_id: int) -> None:
        """Update a self-posted listing's bot-owned card without moving it."""
        from bot.views.self_post import directory_card_kwargs

        channel = self.listings_channel()
        if channel is None:
            return
        async with self.bot.db.session() as session:
            listing = await repository.get_listing(session, guild_id)
            stored_guild = await repository.get_guild(session, guild_id)
        if listing is None or not listing.self_posted:
            return
        if not listing.controls_message_id:
            await self.relist_self_post(guild_id)
            return
        live_guild = self.bot.get_guild(guild_id)
        guild_name = (live_guild.name if live_guild else None) or (stored_guild.name if stored_guild else f"Server {guild_id}")
        member_count = (live_guild.member_count if live_guild else None) or (stored_guild.member_count if stored_guild else 0)
        icon_url = (live_guild.icon.url if live_guild and live_guild.icon else None) or (stored_guild.icon_url if stored_guild else None)
        ad_jump_url = listing_jump_url(channel.guild.id, listing.channel_id, listing.message_id)
        kwargs = directory_card_kwargs(
            self.bot, listing, guild_name=guild_name, member_count=member_count,
            icon_url=icon_url, ad_jump_url=ad_jump_url,
        )
        try:
            await channel.get_partial_message(listing.controls_message_id).edit(**kwargs)
        except discord.NotFound:
            await self.relist_self_post(guild_id)
        except discord.HTTPException as exc:
            log.warning("Could not update directory card for guild %s: %s", guild_id, exc)

    async def refresh_active_listing_views(self) -> tuple[int, int]:
        """Re-render persistent listing/partnership controls after a deploy.

        Dynamic custom IDs make old buttons callable after a restart, but Discord
        keeps the old label/colour until its message is edited. Refresh each live
        listing independently so one deleted/forbidden message can never block
        startup or the rest of the directory. The small cooperative yield keeps a
        large directory from monopolizing the event loop.
        """
        include_test = self.bot.runtime.hub.mode == "test"
        async with self.bot.db.session() as session:
            rows = await repository.active_listings(session, include_test=include_test)

        refreshed = 0
        failed = 0
        from bot.views.partner_posts import partner_post_controls

        for index, listing in enumerate(rows, start=1):
            try:
                await self.update_listing_message(listing.guild_id)

                # Partner-board posts use a separate action strip. Refresh that
                # strip too so Request Partnership stays green on old posts.
                if listing.partner_channel_id and listing.partner_controls_message_id:
                    channel = self.bot.get_channel(listing.partner_channel_id)
                    if channel is not None and hasattr(channel, "get_partial_message"):
                        await channel.get_partial_message(listing.partner_controls_message_id).edit(
                            content="-# 🤝 Partner Board actions",
                            view=partner_post_controls(self.bot, listing.guild_id),
                            allowed_mentions=safe_allowed_mentions(),
                        )
                refreshed += 1
            except discord.NotFound:
                # update_listing_message self-repairs directory posts; a missing
                # partner control strip is non-fatal and can be recreated on repost.
                failed += 1
                log.info("listing.view_missing guild_id=%s", listing.guild_id)
            except discord.HTTPException as exc:
                failed += 1
                log.warning("Could not refresh listing controls for guild %s: %s", listing.guild_id, exc)
            except Exception:
                failed += 1
                log.exception("Unexpected error refreshing listing controls for guild %s", listing.guild_id)

            if index % 25 == 0:
                await asyncio.sleep(0)

        return refreshed, failed

    async def update_listing_message(self, guild_id: int) -> None:
        """Edit the listing in place (edits don't move it). Reposts if it vanished."""
        from bot.views.partnership import listing_message_kwargs

        async with self.bot.db.session() as session:
            listing = await repository.get_listing(session, guild_id)
        if listing is None or listing.status != ListingStatus.ACTIVE:
            return
        if listing.self_posted:
            # Never convert an owner-authored ad into a bot-authored message. Public
            # info changes only update Parley's small directory card in place.
            await self.update_self_post_card(guild_id)
            return
        channel = self.bot.get_channel(listing.channel_id) if listing.channel_id else None
        if channel is None or not hasattr(channel, "get_partial_message") or listing.message_id is None:
            await self.publish_listing(guild_id)
            return
        kwargs = listing_message_kwargs(self.bot, listing)
        if kwargs.get("view") is discord.utils.MISSING:
            kwargs["view"] = None  # e.g. stopped accepting partnerships: remove the button
        try:
            await channel.get_partial_message(listing.message_id).edit(**kwargs)
        except discord.NotFound:
            log.info("listing.message_missing guild_id=%s; reposting", guild_id)
            await self.publish_listing(guild_id)

    # ------------------------------------------------------------ looking for partners

    async def move_looking_panel_now(self) -> None:
        channel = self.looking_channel()
        if channel is None:
            return
        async with self._looking_lock:
            await self._repost_panel(LOOKING_PANEL, channel)

    def schedule_looking_panel_move(self) -> None:
        """Debounced move after a human's top-level post: a burst of posts moves it once."""
        if self._looking_task is not None and not self._looking_task.done():
            self._looking_task.cancel()
        self._looking_task = asyncio.create_task(self._delayed_looking_move(), name="waypoint-looking-panel")

    async def _delayed_looking_move(self) -> None:
        try:
            await asyncio.sleep(self.bot.runtime.partnerships.looking_panel_debounce_seconds)
            await self.move_looking_panel_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Could not move the find-partners panel")

    async def close(self) -> None:
        if self._looking_task is not None and not self._looking_task.done():
            self._looking_task.cancel()
            try:
                await self._looking_task
            except asyncio.CancelledError:
                pass
