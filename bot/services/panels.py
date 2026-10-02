"""Listing messages and bottom panels in the main server.

Discord messages can't be moved, so "keeping the panel at the bottom" means:
delete the old panel, post the new message, post a fresh panel under it and
store its message ID. All message IDs live in the database, so this works
across restarts and the panel is recreated if someone deletes it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import listings as listing_service
from bot.services import message_cleanup
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
PARLEY_PERKS_PANEL = "parley_perks"
RULES_PANEL = "server_rules"
STARTUP_PANEL_SPACING_SECONDS = 0.9
LISTING_REFRESH_SPACING_SECONDS = 0.8

PanelBuilder = Callable[["ParleyBot"], tuple[str, discord.ui.View]]


class PanelService:
    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot
        # One lock per channel: listing posts and panel moves must never interleave.
        self._listings_lock = asyncio.Lock()
        self._looking_lock = asyncio.Lock()
        # Channel creation has its own lock, but reposting static panels did
        # not. On startup + resume two repairs can both see a missing message
        # before either saves it, leaving duplicate #server-rules posts.
        self._static_panel_lock = asyncio.Lock()
        self._aux_channels_lock = asyncio.Lock()
        self._looking_task: asyncio.Task | None = None
        # Discord emits on_raw_message_delete for our own panel moves too. Keep a
        # short TTL so that self-initiated deletes are not mistaken for manual
        # deletions and recreated a second time.
        self._intentional_panel_deletes: dict[int, float] = {}

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

    def benefits_channel(self) -> discord.TextChannel | None:
        return self.main_channel(self.bot.runtime.hub.benefits_channel_id)

    def review_channel(self) -> discord.TextChannel | None:
        return self.main_channel(self.bot.runtime.hub.review_channel_id)

    def rules_channel(self) -> discord.TextChannel | None:
        return self.main_channel(self.bot.runtime.hub.rules_channel_id)

    async def ensure_aux_channels(self) -> None:
        async with self._aux_channels_lock:
            await self._ensure_aux_channels_locked()

    async def _ensure_aux_channels_locked(self) -> None:
        """Upgrade an EXISTING hub without making the owner rerun /setup.

        Only create channels if the bot already has Manage Channels. Never
        silently fall back from staff approvals to public/ordinary log channels.
        """
        hub = self.bot.runtime.hub
        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(hub.main_guild_id) if callable(get_guild) and hub.main_guild_id else None
        if guild is None or getattr(guild, "me", None) is None:
            return
        permissions = getattr(guild.me, "guild_permissions", None)
        if not getattr(permissions, "manage_channels", False):
            return
        from bot.services import configuration, setup as setup_service
        get_role = getattr(guild, "get_role", None)
        roles = [role for rid in hub.staff_role_ids
                 if callable(get_role) and (role := get_role(rid))]
        changes = {}
        for key in ("review_channel_id", "rules_channel_id"):
            slot = setup_service.SLOT_BY_KEY[key]
            configured = guild.get_channel(getattr(hub, key) or 0)
            if isinstance(configured, discord.TextChannel):
                continue
            try:
                existing, _created = await setup_service.get_or_create_channel(
                    guild, slot, roles, preferred_id=getattr(hub, key) or 0,
                )
            except discord.HTTPException as exc:
                log.error("Could not create/reuse %s: %s", slot.name, exc)
                continue
            changes[f"hub.{key}"] = existing.id
            # Repair any reused channel: approvals must be private, rules
            # must be read-only. Failure must never expose a public review queue.
            try:
                if key == "review_channel_id":
                    await existing.set_permissions(
                        guild.default_role, view_channel=False,
                        reason="Parley: staff-only ad approvals")
                    for role in roles:
                        await existing.set_permissions(
                            role, view_channel=True, read_message_history=True,
                            reason="Parley: allow staff to review ads")
                else:
                    await existing.set_permissions(
                        guild.default_role, send_messages=False,
                        create_public_threads=False, create_private_threads=False,
                        send_messages_in_threads=False,
                        reason="Parley: read-only server rules")
            except discord.HTTPException as exc:
                log.error("Cannot secure channel %s: %s", existing.id, exc)
                changes.pop(f"hub.{key}", None)
        if changes:
            async with self.bot.db.session() as session:
                await configuration.save(session, changes, actor_id=None)
            await self.bot.settings_changed(refresh_panels=False)
        # Keep exactly one active staff review channel. A duplicate containing
        # old approvals is archived, not erased, to preserve pending decisions.
        review_id = changes.get("hub.review_channel_id", hub.review_channel_id)
        if review_id:
            await setup_service.reconcile_duplicate_approvals(guild, canonical_id=review_id)

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
            cid for cid in (
                h.listings_channel_id, h.looking_channel_id, h.welcome_channel_id,
                h.perks_channel_id, h.benefits_channel_id, h.rules_channel_id
            ) if cid
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

    async def _delete(self, channel_id: int | None, message_id: int | None) -> bool:
        if not channel_id or not message_id:
            return True
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "get_partial_message"):
            return False  # retry when Discord channel cache becomes available
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            return True  # already gone
        except discord.HTTPException as exc:
            log.warning("Could not delete message %s in channel %s: %s", message_id, channel_id, exc)
            return False
        return True

    async def _delete_queued(self, channel_id: int | None, message_id: int | None) -> bool:
        """Attempt deletion and remove its durable record only on success/404."""
        if not await self._delete(channel_id, message_id):
            return False
        async with self.bot.db.session() as session:
            await message_cleanup.remove(session, channel_id, message_id)
        return True

    async def retry_pending_message_deletions(self, *, limit: int = 30) -> tuple[int, int]:
        """Bounded maintenance pass; older failures remain queued for next tick."""
        async with self.bot.db.session() as session:
            rows = await message_cleanup.pending(session, limit=limit)
            references = [(row.channel_id, row.message_id) for row in rows]
        deleted = 0
        for channel_id, message_id in references:
            try:
                if await self._delete_queued(channel_id, message_id):
                    deleted += 1
                else:
                    async with self.bot.db.session() as session:
                        await message_cleanup.mark_failed(session, channel_id, message_id)
            except Exception:
                log.exception("Unable to process pending message deletion %s/%s", channel_id, message_id)
        return deleted, len(references) - deleted

    async def delete_listing_message(self, channel_id: int | None, message_id: int | None) -> None:
        await self._delete(channel_id, message_id)

    async def take_down_listing(self, listing) -> None:
        """Delete every public post owned by a listing after removal/suspension/ban."""
        if listing is None:
            return
        await self.clear_approval_notice(listing.guild_id)
        ad_controls_ok = await self._delete(listing.channel_id, listing.controls_message_id)
        ad_ok = await self._delete(listing.channel_id, listing.message_id)
        partner_controls_ok = await self._delete(listing.partner_channel_id, listing.partner_controls_message_id)
        partner_ok = await self._delete(listing.partner_channel_id, listing.partner_message_id)
        # Persist failed-message IDs for restart-safe cleanup. A failed Discord
        # delete must not disappear from our database merely because it was tried.
        async with self.bot.db.session() as session:
            current = await repository.get_listing(session, listing.guild_id)
            if current is not None:
                if ad_controls_ok and current.controls_message_id == listing.controls_message_id:
                    current.controls_message_id = None
                if ad_ok and current.message_id == listing.message_id:
                    current.message_id = None
                    current.self_posted = False
                if current.message_id is None and current.controls_message_id is None:
                    current.channel_id = None
                if partner_controls_ok and current.partner_controls_message_id == listing.partner_controls_message_id:
                    current.partner_controls_message_id = None
                if partner_ok and current.partner_message_id == listing.partner_message_id:
                    current.partner_message_id = None
                if current.partner_message_id is None and current.partner_controls_message_id is None:
                    current.partner_channel_id = None
                    current.partner_ad_text = None
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
            PARLEY_PERKS_PANEL: (welcome.parley_perks_panel, panels.benefits_panel_enabled),
            RULES_PANEL: (welcome.server_rules_panel, True),
        }[panel_type]

    def _channel_for(self, panel_type: str) -> discord.TextChannel | None:
        return {
            LISTINGS_PANEL: self.listings_channel,
            LOOKING_PANEL: self.looking_channel,
            WELCOME_PANEL: self.welcome_channel,
            PERKS_PANEL: self.perks_channel,
            PARLEY_PERKS_PANEL: self.benefits_channel,
            RULES_PANEL: self.rules_channel,
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
        if stored is not None and stored.message_id:
            # Do not generate duplicates if Discord cannot delete the old panel.
            if not await self._delete(stored.channel_id, stored.message_id):
                log.warning("Panel %s old message could not be removed; retrying on next refresh", panel_type)
                return None
            self._intentional_panel_deletes[stored.message_id] = time.monotonic() + 60.0

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
        await self.ensure_aux_channels()
        await self._refresh_how_it_works_channel()
        async with self._static_panel_lock:
            await self.ensure_panel(RULES_PANEL, keep_at_bottom=False, force_edit=force_edit)
        async with self._listings_lock:
            results[LISTINGS_PANEL] = await self.ensure_panel(LISTINGS_PANEL, keep_at_bottom=True, force_edit=force_edit)
        async with self._looking_lock:
            results[LOOKING_PANEL] = await self.ensure_panel(LOOKING_PANEL, keep_at_bottom=False, force_edit=force_edit)
        async with self._static_panel_lock:
            results[WELCOME_PANEL] = await self.ensure_panel(WELCOME_PANEL, keep_at_bottom=False, force_edit=force_edit)
            results[PERKS_PANEL] = await self.ensure_panel(PERKS_PANEL, keep_at_bottom=False, force_edit=force_edit)
            results[PARLEY_PERKS_PANEL] = await self.ensure_panel(PARLEY_PERKS_PANEL, keep_at_bottom=False, force_edit=force_edit)
        return results

    async def refresh_entry_panels(self, *, repost: bool) -> dict[str, str]:
        """Refresh every permanent public entry panel with paced Discord writes.

        ``repost=True`` is used once per process start. It deliberately replaces
        the stored panel messages so every deploy gets brand-new Discord
        components instead of relying forever on an old button message.

        ``repost=False`` is used after gateway resumes and by periodic recovery;
        it force-edits current messages in place to avoid needless churn.
        """
        await self.ensure_aux_channels()
        await self._refresh_how_it_works_channel()
        results: dict[str, str] = {}
        ordered = (
            WELCOME_PANEL,
            LISTINGS_PANEL,
            LOOKING_PANEL,
            PERKS_PANEL,
            PARLEY_PERKS_PANEL,
            RULES_PANEL,
        )
        async def refresh_one(panel_type: str) -> str:
            channel = self._channel_for(panel_type)
            if channel is None:
                return "no_channel"
            _builder, enabled = self._builder(panel_type)
            if not enabled:
                return await self.ensure_panel(
                    panel_type,
                    keep_at_bottom=panel_type == LISTINGS_PANEL,
                    force_edit=True,
                )
            if repost:
                message = await self._repost_panel(panel_type, channel)
                return "reposted" if message is not None else "error"
            return await self.ensure_panel(
                panel_type,
                keep_at_bottom=panel_type == LISTINGS_PANEL,
                force_edit=True,
            )

        for index, panel_type in enumerate(ordered):
            if panel_type == LISTINGS_PANEL:
                async with self._listings_lock:
                    results[panel_type] = await refresh_one(panel_type)
            elif panel_type == LOOKING_PANEL:
                async with self._looking_lock:
                    results[panel_type] = await refresh_one(panel_type)
            else:
                async with self._static_panel_lock:
                    results[panel_type] = await refresh_one(panel_type)
            if index < len(ordered) - 1:
                await asyncio.sleep(STARTUP_PANEL_SPACING_SECONDS)
        log.info("Permanent panels refreshed: %s", results)
        return results

    async def cleanup_orphan_entry_panels(self, *, history_limit: int | None = 250) -> int:
        """Remove stale bot-owned entry panels left behind by older deployments.

        The database tracks the current panel message in each channel, but very
        old releases or a crash between send/delete can leave an extra public
        message behind. Users may keep clicking those old controls. On startup,
        normally scan recent bot-authored messages; staff may request a full
        paginated scan (history_limit=None) to include pre-upgrade content.
        Only messages by this exact bot account and unmistakable old panel
        signatures are eligible. A full scan never deletes advertisements or
        other users' posts. Look for messages that contain a top-level
        ``wp:act:*`` component and remove every one except the currently stored
        panel IDs. Listing/request cards use different custom-id prefixes and are
        therefore not touched.
        """
        if self.bot.user is None or not self.bot.runtime.hub.main_guild_id:
            return 0
        guild_id = self.bot.runtime.hub.main_guild_id
        async with self.bot.db.session() as session:
            current_ids = {
                panel.message_id
                for panel_type in (LISTINGS_PANEL, LOOKING_PANEL, WELCOME_PANEL, PERKS_PANEL, PARLEY_PERKS_PANEL, RULES_PANEL)
                if (panel := await repository.get_panel(session, guild_id, panel_type)) is not None
                and panel.message_id is not None
            }

        deleted = 0
        seen_channels: set[int] = set()
        for channel_id in self.panel_channel_ids():
            if channel_id in seen_channels:
                continue
            seen_channels.add(channel_id)
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                async for message in channel.history(limit=history_limit):
                    if message.author.id != self.bot.user.id or message.id in current_ids:
                        continue
                    has_entry_action = any(
                        isinstance(getattr(component, "custom_id", None), str)
                        and component.custom_id.startswith("wp:act:")
                        for row in message.components
                        for component in getattr(row, "children", ())
                    )
                    # Very old Start Here builds may predate wp:act:* but still
                    # contain the unmistakable panel heading. Clean those too so
                    # nobody can keep clicking a dead September-era panel.
                    is_legacy_welcome = (
                        channel.id == self.bot.runtime.hub.welcome_channel_id
                        and "Welcome to Parley" in (message.content or "")
                    )
                    is_old_perks = (
                        channel.id == self.bot.runtime.hub.benefits_channel_id
                        and (
                            "Parley Perks" in (message.content or "")
                            or "Connected Perks" in (message.content or "")
                            or any("Parley Perks" in (getattr(embed, "title", "") or "")
                                   for embed in getattr(message, "embeds", ()))
                            or any(getattr(child, "label", "") == "How Parley Works"
                                   for row in message.components
                                   for child in getattr(row, "children", ()))
                        )
                    )
                    # Rules are plain text with no components, so the older
                    # orphan scan could never recognize duplicate rules posts.
                    is_old_rules = (
                        channel.id == self.bot.runtime.hub.rules_channel_id
                        and (message.content or "").lstrip().startswith((
                            "# 📜 Parley Server Rules", "# 📜 Server Rules",
                            "# Parley Server Rules", "## Parley Server Rules",
                            "# Parley Rules", "## Server Rules",
                        ))
                    )
                    if not (has_entry_action or is_legacy_welcome or is_old_perks or is_old_rules):
                        continue
                    try:
                        await message.delete()
                        deleted += 1
                        await asyncio.sleep(0.25)
                    except discord.NotFound:
                        pass
                    except discord.HTTPException as exc:
                        log.warning("Could not remove stale entry panel message %s: %s", message.id, exc)
            except discord.HTTPException as exc:
                log.warning("Could not scan #%s for stale entry panels: %s", channel.name, exc)
        if deleted:
            log.info("Removed %d stale/orphan entry panel message(s)", deleted)
        return deleted

    async def post_above_panel(self, channel: discord.TextChannel, panel_type: str, **kwargs) -> discord.Message:
        """Post a message (e.g. a Test Center example) and move the panel under it."""
        lock = self._looking_lock if panel_type == LOOKING_PANEL else self._listings_lock
        async with lock:
            kwargs.setdefault("allowed_mentions", safe_allowed_mentions())
            message = await channel.send(**kwargs)
            await self._repost_panel(panel_type, channel)
        return message

    async def handle_message_deleted(self, channel_id: int, message_id: int) -> None:
        """Recreate a panel right away if someone deletes it.

        Deletes initiated by ``_repost_panel`` are ignored here; that method is
        already creating the replacement. Without this guard the raw delete event
        could race the repost and create duplicate panels / extra API traffic.
        """
        now = time.monotonic()
        expired = [mid for mid, until in self._intentional_panel_deletes.items() if until <= now]
        for mid in expired:
            self._intentional_panel_deletes.pop(mid, None)
        if self._intentional_panel_deletes.pop(message_id, None) is not None:
            return

        main_guild_id = self.bot.runtime.hub.main_guild_id
        if channel_id not in self.panel_channel_ids() or not main_guild_id:
            return
        async with self.bot.db.session() as session:
            for panel_type in (LISTINGS_PANEL, LOOKING_PANEL, WELCOME_PANEL, PERKS_PANEL, PARLEY_PERKS_PANEL, RULES_PANEL):
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

    async def notify_approval_ready(self, guild_id: int) -> bool:
        """Ping once in the directory; do not grant public posting permissions.

        Repeated calls after a restart are idempotent when message ID is saved.
        Members click My Server Listings and submit a normal Markdown message.
        """
        async with self.bot.db.session() as session:
            listing = await repository.get_listing(session, guild_id)
            if (listing is None or listing.status != ListingStatus.ACTIVE
                    or not listing.awaiting_ad or listing.quick_submitted_by is None
                    or listing.approval_notice_message_id):
                return bool(listing and listing.approval_notice_message_id)
            user_id = listing.quick_submitted_by
        channel = self.listings_channel()
        if channel is None:
            log.warning("Approved server %s waiting: directory not configured", guild_id)
            return False
        try:
            message = await channel.send(
                f"<@{user_id}> ✅ Your server has been approved for Parley! "
                "Open **My Server Listings → Post Approved Ad** whenever you're ready. "
                "There is **no deadline** to post.",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(id=user_id)], replied_user=False,
                ),
            )
        except discord.HTTPException:
            log.exception("Failed to send approval-ready notice %s; retry later", guild_id)
            return False
        async with self.bot.db.session() as session:
            current = await repository.get_listing(session, guild_id)
            if current and current.awaiting_ad:
                current.approval_notice_message_id = message.id
        return True

    async def clear_approval_notice(self, guild_id: int) -> None:
        async with self.bot.db.session() as session:
            listing = await repository.get_listing(session, guild_id)
            message_id = listing.approval_notice_message_id if listing else None
        if message_id and await self._delete(self.bot.runtime.hub.listings_channel_id, message_id):
            async with self.bot.db.session() as session:
                current = await repository.get_listing(session, guild_id)
                if current and current.approval_notice_message_id == message_id:
                    current.approval_notice_message_id = None

    async def publish_listing(self, guild_id: int, *, only_if_missing: bool = False) -> discord.Message | None:
        """(Re)post a listing at the bottom of #server-directory, then the panel under it.

        Returns None when the listing isn't active or no listings channel is set.
        """
        from bot.views.partnership import listing_message_kwargs

        channel = self.listings_channel()
        async with self._listings_lock:
            async with self.bot.db.session() as session:
                listing = await repository.get_listing(session, guild_id)
            if listing is None or listing.status != ListingStatus.ACTIVE or listing.awaiting_ad:
                return None
            if channel is None:
                log.warning("listing.not_published guild_id=%s (listings channel not configured)", guild_id)
                return None

            if only_if_missing and listing.message_id is not None:
                old_channel = self.bot.get_channel(listing.channel_id) if listing.channel_id else None
                if old_channel is not None:
                    # A partial message does not prove the Discord post exists.
                    # This matters after staff delete a message or an outage.
                    fetch = getattr(old_channel, "fetch_message", None)
                    if not callable(fetch):
                        return old_channel.get_partial_message(listing.message_id)
                    try:
                        return await fetch(listing.message_id)
                    except discord.NotFound:
                        log.info("Directory message %s missing; republishing %s", listing.message_id, guild_id)
                    except discord.HTTPException as exc:
                        log.warning("Could not verify directory message %s: %s", listing.message_id, exc)
                        return None  # cannot safely assume absence during outage

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

            # 2. atomically remember old messages for cleanup AND record the
            # replacement. A failed Discord delete must survive bot restarts.
            old_messages = [
                (listing.channel_id, mid)
                for mid in (listing.controls_message_id, listing.message_id)
                if mid and (listing.channel_id != channel.id or mid != message.id)
            ]
            try:
                async with self.bot.db.session() as session:
                    for old_channel, old_id in old_messages:
                        await message_cleanup.enqueue(session, old_channel, old_id,
                                                      guild_id=guild_id)
                    await listing_service.record_message(
                        session, guild_id=guild_id, channel_id=channel.id, message_id=message.id
                    )
                    if listing.message_id is None and listing.quick_submitted_by is not None:
                        from bot.utils.helpers import utcnow
                        current = await repository.get_listing(session, guild_id)
                        current.refreshed_at = utcnow()  # first delivery starts Free timer
            except Exception:
                # The database did not commit the new public location. Prefer
                # the old advertisement; do not leave an untracked replacement.
                await self._delete(channel.id, message.id)
                raise

            # 3. remove old copies. Failed deletes remain queued permanently
            # until a later maintenance pass succeeds.
            for old_channel, old_id in old_messages:
                await self._delete_queued(old_channel, old_id)
            # Auxiliary UI failures must not make a successfully sent ad look failed.
            try:
                await self._repost_panel(LISTINGS_PANEL, channel)
            except Exception:
                log.exception("Listing %s published; directory panel repair is delayed", guild_id)
        log.info("listing.published guild_id=%s message_id=%s", guild_id, message.id)
        if listing.message_id is None:  # first publication, not every relist
            from bot.views.admin.moderation import post_private_staff_controls
            try:
                await post_private_staff_controls(self.bot, guild_id, "Directory listing")
            except Exception:
                log.exception("Listing %s published; staff controls are delayed", guild_id)
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
            old_posts = [(old_channel, old_id) for old_id in (old_controls, old_message)
                         if old_id and not (old_channel == channel.id and old_id in (controls.id, message.id))]
            try:
                async with self.bot.db.session() as session:
                    for cid, mid in old_posts:
                        await message_cleanup.enqueue(session, cid, mid, guild_id=guild_id,
                                                      reason="self_post_replaced")
                    stored = await repository.get_listing(session, guild_id)
                    if stored is not None:
                        stored.channel_id = channel.id
                        stored.message_id = message.id
                        stored.controls_message_id = controls.id
                        stored.self_posted = True
            except Exception:
                await self._delete(channel.id, controls.id)
                raise
            for cid, mid in old_posts:
                await self._delete_queued(cid, mid)
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
            # Track the retired card before deleting; failed deletions retry
            # after restart even when the new card's ID has replaced its slot.
            try:
                async with self.bot.db.session() as session:
                    if old_controls and (listing.channel_id != channel.id or old_controls != card.id):
                        await message_cleanup.enqueue(session, listing.channel_id, old_controls,
                                                      guild_id=guild_id, reason="directory_card_replaced")
                    stored = await repository.get_listing(session, guild_id)
                    if stored is not None:
                        stored.controls_message_id = card.id
            except Exception:
                await self._delete(channel.id, card.id)
                raise
            await self._delete_queued(listing.channel_id, old_controls)
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

            # Startup can touch both a directory message and a partner-board
            # control strip for one listing. Pace each listing so a large hub
            # cannot burst PATCH requests into Discord's rate-limit bucket.
            if index < len(rows):
                await asyncio.sleep(LISTING_REFRESH_SPACING_SECONDS)

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
