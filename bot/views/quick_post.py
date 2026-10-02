"""Trust-first Free Listing wizard: category -> write or private paste -> confirm.

Quick Posts never confer server management authority. The published ad carries
only the Discord invite destination verified by Discord's own invite endpoint.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import listings as listing_service
from bot.services import quick_post as quick_service
from bot.services.errors import ParleyError, ValidationError
from bot.utils.helpers import utcnow
from bot.utils.mentions import safe_allowed_mentions
from bot.views.base import ConfirmView, OwnedView, acknowledge, get_bot, guard, handle_error, reply
from bot.views.welcome import add_bot_button, persistent_view, register_action, show_screen

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuickDraft:
    guild: listing_service.GuildInfo
    invite_url: str
    description: str
    category: str
    raw_ad: str | None = None
    editing: bool = False


async def show_quick_start(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    async with bot.db.session() as session:
        existing = await quick_service.my_quick_listing(session, interaction.user.id)
        stored = await repository.get_guild(session, existing.guild_id) if existing else None
    view = QuickStartView(
        bot, interaction.user.id, existing=existing is not None,
        status=existing.status if existing else None,
    )
    if existing:
        name = discord.utils.escape_markdown(stored.name) if stored else f"Server {existing.guild_id}"
        state = {"pending": "Awaiting staff review", "active": "Listed", "expired": "Expired", "suspended": "Suspended", "removed": "Removed — submit corrected copy after cooldown"}.get(existing.status, existing.status)
        message = (
            f"## 📣 My Server Listing — {name}\n"
            f"**Type:** ⚪ Unverified · Free Listing\n**Status:** {state}\n\n"
            "**Edit Ad** — change the text or category (once per repost cycle).\n"
            "**Repost** — refresh an approved ad every 24 hours.\n"
            "**Delete Listing** — remove your ad from Parley, including pending reviews.\n\n"
            "Quick Post is unverified and does not grant server permissions. "
            "Install Parley only if you want connected tools and partnerships."
        )
    else:
        message = (
            "## 📣 Post your Discord server for free\n"
            "**No OAuth. No bot installation. No server permissions.**\n\n"
            "1. Choose **exactly one** category from the dropdown.\n"
            "2. **Write a short ad** or **paste your existing Discord advertisement** in a private room.\n"
            "   Different invite links and vanity URLs are okay if they lead to the same server.\n"
            "3. Preview and confirm.\n\n"
            f"**Current publishing mode:** {'Staff approval' if bot.runtime.listings.approval_required else 'Automatic after safety checks'}.\n"
            "**Free limit:** one unconnected community per account, repost every 24 hours.\n"
            "-# Public invites identify servers, but never prove who owns them."
        )
    await show_screen(interaction, message, view=view)


class QuickStartView(OwnedView):
    def __init__(self, bot: ParleyBot, owner_id: int, *, existing: bool = False, status: str | None = None):
        super().__init__(owner_id)
        self.bot = bot
        if existing and status == ListingStatus.REMOVED:
            corrected = discord.ui.Button(label="Submit Corrected Ad", style=discord.ButtonStyle.primary, row=0)
            corrected.callback = self._quick
            self.add_item(corrected)
        elif existing:
            repost = discord.ui.Button(label="Repost Existing Ad", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
            repost.disabled = status not in (None, ListingStatus.ACTIVE, ListingStatus.EXPIRED)
            repost.callback = self._repost
            self.add_item(repost)
            edit = discord.ui.Button(label="Edit Ad", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
            edit.disabled = status not in (None, ListingStatus.ACTIVE)
            edit.callback = self._edit
            self.add_item(edit)
            delete = discord.ui.Button(label="Delete Listing", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
            delete.callback = self._delete
            self.add_item(delete)
        else:
            quick = discord.ui.Button(label="Create Free Listing", emoji="📣", style=discord.ButtonStyle.primary, row=0)
            quick.callback = self._quick
            self.add_item(quick)
        connected = discord.ui.Button(label="Manage Connected Servers", emoji="🤝", style=discord.ButtonStyle.secondary, row=1)
        connected.callback = self._connected
        self.add_item(connected)
        install = add_bot_button(bot, row=1)
        if install is not None:
            install.label = "Install Parley (Optional)"
            self.add_item(install)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await reply(interaction, "This menu belongs to someone else.")
            return False
        return await guard(interaction, arm_watchdog=False)

    async def _quick(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="## Choose ONE category\nSelect your category, then choose how you'd like to create your ad.",
            view=QuickWizardView(self.bot, interaction.user.id),
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="## Edit your Free Listing\nOne edit is permitted per repost cycle. Choose **one** category and your editing method.",
            view=QuickWizardView(self.bot, interaction.user.id, editing=True),
        )

    async def _repost(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        try:
            async with self.bot.db.session() as session:
                existing = await quick_service.my_quick_listing(session, interaction.user.id)
                if existing is None:
                    raise ValidationError("You don't have a Free Listing yet.")
                if existing.status not in (ListingStatus.ACTIVE, ListingStatus.EXPIRED):
                    raise ValidationError("Your listing cannot be reposted in its current status.")
                row = await repository.get_guild(session, existing.guild_id)
            if row is None or not existing.invite_url:
                raise ValidationError("The listing is missing server information. Contact staff.")
            code = listing_service.parse_invite_code(existing.invite_url)
            try:
                invite = await self.bot.fetch_invite(code, with_counts=False)
            except discord.HTTPException as exc:
                raise ValidationError("Your invite is unavailable. Ask Parley staff to help update it.") from exc
            if invite.guild is None or invite.guild.id != existing.guild_id:
                raise ValidationError("Your invite no longer points to the listed server.")
            info = listing_service.GuildInfo(row.guild_id, row.name, row.icon_url, row.member_count)
            # Existing approved content is preserved; this action is not an edit.
            async with self.bot.db.session() as session:
                listing, result = await quick_service.create_quick_listing(
                    session, self.bot.runtime, info=info, actor_id=interaction.user.id,
                    invite_url=existing.invite_url, category=existing.categories[0],
                    description="Existing approved listing", now=utcnow(),
                    is_test=self.bot.runtime.hub.mode == "test",
                )
            published = await self.bot.panels.publish_listing(listing.guild_id)
            await interaction.edit_original_response(
                content=("✅ Your approved ad was reposted. The next Free relist is in 24 hours."
                         if published is not None else
                         "✅ Relist saved, but the directory is temporarily unavailable. "
                         "Parley will retry automatically; the 24-hour timer remains in effect."),
                view=None, allowed_mentions=safe_allowed_mentions(),
            )
        except ParleyError as exc:
            await interaction.edit_original_response(content=f"⚠️ {exc.user_message}", view=None)
        except Exception as exc:
            await handle_error(interaction, exc)

    async def _delete(self, interaction: discord.Interaction) -> None:
        """Owner-only confirmation followed by atomic DB removal and message cleanup."""
        await acknowledge(interaction)
        try:
            async with self.bot.db.session() as session:
                existing = await quick_service.my_quick_listing(session, interaction.user.id)
                stored = await repository.get_guild(session, existing.guild_id) if existing else None
            if existing is None or existing.status == ListingStatus.REMOVED:
                raise ValidationError("You don't have an active Free Listing to delete.")
            name = discord.utils.escape_markdown(stored.name) if stored else str(existing.guild_id)
            confirm = ConfirmView(interaction.user.id, confirm_label="Yes, delete my ad")
            await interaction.edit_original_response(
                content=(f"## Delete your Free Listing?\n**{name}** will be removed from Parley's directory "
                         "and partnership board (if present). You can submit again after the cooldown. "
                         "Your posting cooldown still applies.\n\n"
                         "This removes only your Parley ad, not the Discord server itself."),
                view=confirm, allowed_mentions=safe_allowed_mentions(),
            )
            await confirm.wait()
            if not confirm.confirmed or confirm.interaction is None:
                return
            done = confirm.interaction
            await acknowledge(done, thinking=False)
            async with self.bot.db.session() as session:
                removed = await quick_service.delete_quick_listing(
                    session, self.bot.runtime, actor_id=done.user.id,
                    guild_id=existing.guild_id, now=utcnow(),
                )
            # DB removal commits first: if Discord is offline, maintenance retries
            # deleting the public messages using their persisted IDs.
            try:
                await self.bot.panels.take_down_listing(removed)
            except Exception:
                log.exception("Free Listing deletion cleanup delayed for %s", removed.guild_id)
            await done.edit_original_response(
                content="✅ Your Free Listing has been removed from Parley. "
                        "Any Discord message that couldn't be deleted immediately will be retried automatically. "
                        "Your 24-hour posting cooldown still applies.",
                view=None,
            )
        except ParleyError as exc:
            await interaction.edit_original_response(content=f"⚠️ {exc.user_message}", view=None)
        except Exception as exc:
            await handle_error(interaction, exc)

    async def _connected(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        from bot.views.listings import open_connected_picker
        await open_connected_picker(interaction)


class QuickWizardView(OwnedView):
    """Dropdown prevents ambiguous comma-separated or invented category values."""

    def __init__(self, bot: ParleyBot, user_id: int, *, editing: bool = False):
        super().__init__(user_id, timeout=600)
        self.bot = bot
        self.editing = editing
        self.category: str | None = None
        options = [discord.SelectOption(label=c, value=c) for c in bot.runtime.listings.categories[:25]]
        category = discord.ui.Select(placeholder="Choose exactly ONE category (required)", min_values=1, max_values=1, options=options, row=0)
        category.callback = self._pick
        self.add_item(category)
        written = discord.ui.Button(label="Write an Ad", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
        written.callback = self._written
        self.add_item(written)
        paste = discord.ui.Button(label="Paste Existing Ad", emoji="📋", style=discord.ButtonStyle.secondary, row=1)
        paste.callback = self._paste
        self.add_item(paste)
        back = discord.ui.Button(label="Back to My Listing", style=discord.ButtonStyle.secondary, row=2)
        back.callback = self._back
        self.add_item(back)

    async def _back(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        await show_quick_start(interaction)

    async def _pick(self, interaction: discord.Interaction) -> None:
        self.category = interaction.data["values"][0]
        await interaction.response.edit_message(
            content=f"✅ Selected category: **{discord.utils.escape_markdown(self.category)}** (one category only).\n"
                    "Next, choose **Write an Ad** or **Paste Existing Ad**.",
            view=self,
        )

    async def _modal(self, interaction: discord.Interaction, *, paste: bool) -> None:
        if not self.category:
            await reply(interaction, "Choose one category from the dropdown before continuing.")
            return
        if not interaction.response.is_done():
            await interaction.response.send_modal(QuickPostModal(self.bot, self.category, paste=paste, editing=self.editing))

    async def _written(self, interaction: discord.Interaction) -> None:
        await self._modal(interaction, paste=False)

    async def _paste(self, interaction: discord.Interaction) -> None:
        await self._modal(interaction, paste=True)


class QuickPostModal(discord.ui.Modal):
    def __init__(self, bot: ParleyBot, category: str, *, paste: bool = False, editing: bool = False):
        super().__init__(title=("Edit Free Listing" if editing else "Parley · Free Server Listing")[:45], timeout=600)
        self.bot, self.category, self.paste, self.editing = bot, category, paste, editing
        self.invite = discord.ui.TextInput(
            label="Discord server invite" + (" (existing invite)" if editing else ""),
            placeholder="https://discord.gg/example", style=discord.TextStyle.short,
            max_length=120, required=not editing,
        )
        self.add_item(self.invite)
        if not paste:
            self.description = discord.ui.TextInput(
                label="Your ad (not a comma-separated list)",
                placeholder="Tell people what makes your server interesting...",
                style=discord.TextStyle.paragraph, max_length=550,
            )
            self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        try:
            bot = self.bot
            async with bot.db.session() as session:
                existing = await quick_service.my_quick_listing(session, interaction.user.id)
            if self.editing and existing is None:
                raise ValidationError("Your original Free Listing no longer exists.")
            if not self.editing and existing is not None and existing.status != ListingStatus.REMOVED:
                raise ValidationError("You already have a Free Listing. Choose **Edit Ad** or **Repost Existing Ad**.")
            raw_invite = self.invite.value.strip() or (existing.invite_url if existing else "")
            code = listing_service.parse_invite_code(raw_invite)
            try:
                invite = await bot.fetch_invite(code, with_counts=True)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise ValidationError("Your invite is expired, invalid, or inaccessible.") from exc
            except discord.HTTPException as exc:
                raise ValidationError("Discord couldn't verify the invite. Try again later.") from exc
            if invite.guild is None or not invite.guild.id:
                raise ValidationError("Discord did not return server details for this invite.")
            if invite.guild.id == bot.runtime.hub.main_guild_id:
                raise ValidationError("You cannot advertise Parley's main server as a Quick Post.")
            if existing is not None and invite.guild.id != existing.guild_id:
                raise ValidationError("You can only edit the server already listed on your account.")
            icon = getattr(invite.guild, "icon", None)
            guild_info = listing_service.GuildInfo(
                invite.guild.id, invite.guild.name,
                str(icon.url) if icon else None,
                getattr(invite, "approximate_member_count", 0) or 0,
            )
            draft = QuickDraft(
                guild=guild_info, invite_url=listing_service.canonical_invite(invite.code),
                description="" if self.paste else self.description.value,
                category=self.category, editing=self.editing,
            )
            if self.paste:
                from bot.views.temp_ads import capture_pasted_ad
                async def notify(channel):
                    await interaction.edit_original_response(
                        content=f"## Your private draft channel is ready\nGo to {channel.mention} and paste your ad. "
                                "Confirm it there within 10 minutes. This room disappears after use or restart.",
                        view=None, allowed_mentions=safe_allowed_mentions(),
                    )
                async def save_confirmed(raw_ad: str):
                    confirmed = QuickDraft(
                        guild_info, draft.invite_url, "", self.category,
                        raw_ad=raw_ad, editing=self.editing,
                    )
                    await _submit_draft(bot, confirmed, interaction.user.id, interaction=interaction)
                async def preview_text(text: str) -> str:
                    trusted_codes = await quick_service.verify_advertisement_invites(
                        bot, text, guild_id=guild_info.guild_id, invite_url=draft.invite_url,
                    )
                    final, _ = quick_service.prepare_quick_ad(
                        bot.runtime, info=guild_info, invite_url=draft.invite_url,
                        category=self.category, raw_ad=text,
                        verified_invite_codes=trusted_codes,
                    )
                    return final
                await capture_pasted_ad(
                    bot, user_id=interaction.user.id, guild_id=guild_info.guild_id,
                    notify_channel=notify, save_confirmed=save_confirmed,
                    preview_text=preview_text,
                )
            else:
                preview, force_review = quick_service.prepare_quick_ad(
                    bot.runtime, info=draft.guild, invite_url=draft.invite_url,
                    category=draft.category, description=draft.description,
                )
                await interaction.edit_original_response(
                    content=(
                        f"## Preview · {discord.utils.escape_markdown(guild_info.name)}\n"
                        f"**Category (one only):** {self.category}\n\n"
                        f"{preview[:1300]}\n\n"
                        f"**Publishing:** {'Staff review' if bot.runtime.listings.approval_required or force_review else 'Automatic after safety checks'}. "
                        "This is an unverified listing, not proof of ownership.\n"
                        "**Confirm** only if this is the exact ad you want Parley to use."
                    ),
                    view=QuickConfirmView(bot, interaction.user.id, draft),
                    allowed_mentions=safe_allowed_mentions(),
                )
        except ParleyError as exc:
            await interaction.edit_original_response(content=f"⚠️ {exc.user_message}\n\nOpen Free Listing to try again.", view=None)
        except Exception as exc:
            await handle_error(interaction, exc)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_error(interaction, error)


async def _submit_draft(bot: ParleyBot, draft: QuickDraft, user_id: int, *, interaction: discord.Interaction) -> None:
    """Save confirmed copy atomically; public publishing follows the DB transaction."""
    from bot.views.listings import send_for_review
    trusted_codes = (
        await quick_service.verify_advertisement_invites(
            bot, draft.raw_ad, guild_id=draft.guild.guild_id, invite_url=draft.invite_url,
        ) if draft.raw_ad is not None else frozenset()
    )
    _, risky = quick_service.prepare_quick_ad(
        bot.runtime, info=draft.guild, invite_url=draft.invite_url,
        category=draft.category, description=draft.description, raw_ad=draft.raw_ad,
        verified_invite_codes=trusted_codes,
    )
    if (bot.runtime.listings.approval_required or risky) and bot.log_channel() is None:
        raise ValidationError("Manual review is needed but the staff review channel is not configured. Contact Parley staff.")
    async with bot.db.session() as session:
        if draft.editing:
            listing, outcome = await quick_service.edit_quick_listing(
                session, bot.runtime, guild_id=draft.guild.guild_id, actor_id=user_id,
                info=draft.guild, invite_url=draft.invite_url,
                description=draft.description, category=draft.category,
                now=utcnow(), raw_ad=draft.raw_ad,
                verified_invite_codes=trusted_codes,
            )
        else:
            listing, outcome = await quick_service.create_quick_listing(
                session, bot.runtime, info=draft.guild, actor_id=user_id,
                invite_url=draft.invite_url, description=draft.description,
                category=draft.category, now=utcnow(),
                is_test=bot.runtime.hub.mode == "test", raw_ad=draft.raw_ad,
                verified_invite_codes=trusted_codes,
            )
    if outcome in ("pending", "pending_edit"):
        # If sending fails, the DB keeps the pending submission and the
        # maintenance task retries notifying staff. Never auto-publish on error.
        try:
            notified = await send_for_review(bot, listing, draft.guild.name)
        except discord.HTTPException:
            log.exception("Review notification failed for %s", listing.guild_id)
            notified = False
        message = (
            "✅ Saved for **staff approval**. Your existing advertisement stays live while an edit is reviewed."
            if outcome == "pending_edit" else
            "✅ Advertisement submitted for **staff review**. It will appear only after approval."
        )
        if not notified:
            message += " Staff notification is delayed; Parley will retry automatically."
    else:
        try:
            if outcome == "edited":
                await bot.panels.update_listing_message(listing.guild_id)
            else:
                published = await bot.panels.publish_listing(listing.guild_id)
            message = "✅ Your advertisement is live in the Parley directory."
            if outcome != "edited" and published is None:
                message = ("✅ Your ad is safely saved but **not yet visible**. "
                           "The directory channel needs attention; Parley will retry automatically.")
            if outcome == "edited":
                message = "✅ Your advertisement was updated without resetting its repost cooldown."
        except discord.HTTPException:
            log.exception("Directory publication failed for %s", listing.guild_id)
            message = "✅ Ad saved. The directory is currently unavailable; staff may need to republish it."
    await interaction.edit_original_response(
        content=message + "\n\nConnected servers get additional management and approved partnership tools; installation is optional.",
        view=persistent_view(add_bot_button(bot)),
        allowed_mentions=safe_allowed_mentions(),
    )


class QuickConfirmView(OwnedView):
    def __init__(self, bot: ParleyBot, owner_id: int, draft: QuickDraft):
        super().__init__(owner_id, timeout=300)
        self.bot, self.draft = bot, draft
        submit = discord.ui.Button(label="Confirm Ad", emoji="✅", style=discord.ButtonStyle.success)
        submit.callback = self._submit
        self.add_item(submit)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        await interaction.edit_original_response(content="Cancelled. Your listing was not changed.", view=None)

    async def _submit(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        try:
            await _submit_draft(self.bot, self.draft, interaction.user.id, interaction=interaction)
            self.stop()
        except ParleyError as exc:
            await interaction.edit_original_response(content=f"⚠️ {exc.user_message}", view=None)
        except Exception as exc:
            await handle_error(interaction, exc)


@register_action("quick_relist")
async def quick_relist(interaction: discord.Interaction) -> None:
    await show_quick_start(interaction)
