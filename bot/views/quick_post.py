"""Trust-first Free Listing wizard: category -> plain-text ad -> confirm.

Quick Posts never confer server management authority. The published ad carries
only the Discord invite destination verified by Discord's own invite endpoint.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import cooldowns
from bot.services import listings as listing_service
from bot.services import quick_post as quick_service
from bot.services.errors import ParleyError, ValidationError
from bot.utils.helpers import format_duration, utcnow
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
    additional_review: bool = False


async def show_quick_start(interaction: discord.Interaction, *, notice: str = "") -> None:
    bot = get_bot(interaction)
    now = utcnow()
    async with bot.db.session() as session:
        existing = await quick_service.my_quick_listing(session, interaction.user.id)
        stored = await repository.get_guild(session, existing.guild_id) if existing else None
        deleted_wait = await cooldowns.remaining(session, "quick_post_deleted", interaction.user.id, now)
    if existing is not None and stored is not None and stored.connected_by is not None:
        # A later verified connection wins over stale unverified submitter data.
        # Never show working edit/delete controls for a now-connected guild.
        await show_screen(
            interaction,
            "## Server connected to Parley\n"
            "This server now has verified management. Use **Connected Servers** "
            "to manage it with your current Discord permissions, or contact staff.",
            view=ExistingListingHelpView(bot, interaction.user.id),
        )
        return
    listed_wait = None
    if existing is not None and existing.refreshed_at is not None:
        ready_at = existing.refreshed_at + timedelta(minutes=bot.runtime.listings.quick_post_cooldown_minutes)
        if ready_at > now:
            listed_wait = ready_at - now
    wait = deleted_wait or listed_wait
    view = QuickStartView(
        bot, interaction.user.id, existing=existing is not None,
        status=existing.status if existing else None,
        awaiting_ad=bool(existing and existing.awaiting_ad),
        cooldown_active=bool(wait),
    )
    if existing:
        name = discord.utils.escape_markdown(stored.name) if stored else f"Server {existing.guild_id}"
        state = ("Approved — waiting for your advertisement" if existing.awaiting_ad and existing.status == ListingStatus.ACTIVE else
                 {"pending": "Waiting for review", "active": "Live", "expired": "Expired", "suspended": "Suspended", "removed": "Removed"}.get(existing.status, existing.status))
        message = (
            f"## 📣 My Free Listing · {name}\n"
            f"**Status:** {state} · Unverified\n"
            + (f"**Next post:** {format_duration(wait)}\n" if wait else "")
            + ("Your server was removed. You may correct this ad, or release the slot to advertise another server.\n"
               if existing.status == ListingStatus.REMOVED else "")
            + "\nUse the buttons below to edit, repost, switch, or delete your listing."
        )
    else:
        message = (
            "## 📣 Post a Server Free\n"
            + (f"Your next submission is available in **{format_duration(wait)}**.\n"
               if wait else "**1.** Choose a category. **2.** Paste your ad and invite.\n")
            + "No OAuth or bot installation. One unconnected listing per account."
        )
    await show_screen(interaction, f"{notice}\n\n{message}" if notice else message, view=view)


class QuickStartView(OwnedView):
    def __init__(self, bot: ParleyBot, owner_id: int, *, existing: bool = False,
                 status: str | None = None, awaiting_ad: bool = False,
                 cooldown_active: bool = False):
        super().__init__(owner_id)
        self.bot = bot
        if existing and status == ListingStatus.ACTIVE and awaiting_ad:
            ad = discord.ui.Button(label="Post Approved Ad", style=discord.ButtonStyle.success, row=0)
            ad.callback = self._approved_ad
            self.add_item(ad)
        elif existing and status == ListingStatus.REMOVED:
            corrected = discord.ui.Button(label="Submit Corrected Ad", style=discord.ButtonStyle.primary, row=0)
            corrected.callback = self._quick
            corrected.disabled = cooldown_active
            self.add_item(corrected)
        elif existing:
            repost = discord.ui.Button(label="Repost Existing Ad", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
            repost.disabled = status not in (None, ListingStatus.ACTIVE, ListingStatus.EXPIRED)
            repost.callback = self._repost
            repost.disabled = repost.disabled or cooldown_active
            self.add_item(repost)
            edit = discord.ui.Button(label="Edit Ad", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
            edit.disabled = status not in (None, ListingStatus.ACTIVE)
            edit.callback = self._edit
            self.add_item(edit)
        else:
            quick = discord.ui.Button(label="Create Free Listing", emoji="📣", style=discord.ButtonStyle.primary, row=0)
            quick.callback = self._quick
            quick.disabled = cooldown_active
            self.add_item(quick)
            if cooldown_active:
                refresh = discord.ui.Button(label="Check Cooldown", style=discord.ButtonStyle.secondary, row=1)
                refresh.callback = self._refresh
                self.add_item(refresh)
        if existing:
            switch = discord.ui.Button(label="Switch Server", style=discord.ButtonStyle.secondary, row=1)
            switch.callback = self._switch
            self.add_item(switch)
            delete = discord.ui.Button(label="Delete Listing", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
            delete.callback = self._delete
            self.add_item(delete)
        connected = discord.ui.Button(label="Connected Servers", emoji="🤝", style=discord.ButtonStyle.secondary, row=2)
        connected.callback = self._connected
        self.add_item(connected)
        install = add_bot_button(bot, row=2)
        if install is not None:
            install.label = "Install Parley (Optional)"
            self.add_item(install)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await reply(interaction, "This menu belongs to someone else.")
            return False
        return await guard(interaction, arm_watchdog=False)

    async def _approved_ad(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ApprovedAdModal(self.bot))

    async def _quick(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="**Step 1 of 2:** Choose your server category below.",
            view=QuickWizardView(self.bot, interaction.user.id),
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="**Step 1 of 2:** Choose a category for your updated ad.",
            view=QuickWizardView(self.bot, interaction.user.id, editing=True),
        )

    async def _repost(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        try:
            async with self.bot.db.session() as session:
                existing = await quick_service.my_quick_listing(session, interaction.user.id)
                if existing is None:
                    raise ValidationError("You don't have a Free Listing yet.")
                if existing.awaiting_ad:
                    raise ValidationError("Post your approved advertisement first. Your relist timer hasn't started.")
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
        await self._confirm_remove(interaction, switching=False)

    async def _switch(self, interaction: discord.Interaction) -> None:
        await self._confirm_remove(interaction, switching=True)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        await show_quick_start(interaction)

    async def _confirm_remove(self, interaction: discord.Interaction, *, switching: bool) -> None:
        """Owner-only confirmation followed by atomic DB removal and message cleanup."""
        await acknowledge(interaction)
        try:
            async with self.bot.db.session() as session:
                existing = await quick_service.my_quick_listing(session, interaction.user.id)
                stored = await repository.get_guild(session, existing.guild_id) if existing else None
            if existing is None:
                raise ValidationError("You don't have a Free Listing to remove.")
            name = discord.utils.escape_markdown(stored.name) if stored else str(existing.guild_id)
            confirm = ConfirmView(interaction.user.id, confirm_label="Remove and switch" if switching else "Delete my listing")
            await interaction.edit_original_response(
                content=(f"## {'Switch servers?' if switching else 'Delete your listing?'}\n"
                         f"Remove **{name}** from Parley. "
                         "You can list another server after the existing cooldown expires. "
                         "Removing an ad never deletes the Discord server."),
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
            await show_quick_start(
                done, notice=("✅ Your listing was removed. Select **Create Free Listing** when the cooldown expires."
                              if switching else "✅ Your listing was deleted from Parley."),
            )
        except ParleyError as exc:
            await interaction.edit_original_response(content=f"⚠️ {exc.user_message}", view=None)
        except Exception as exc:
            await handle_error(interaction, exc)

    async def _connected(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        from bot.views.listings import open_connected_picker
        await open_connected_picker(interaction)


class ExistingListingHelpView(OwnedView):
    """Offer safe next steps instead of a dead-end ownership warning.

    This UI is navigational only: it never grants ownership or edits the
    existing listing. Connected management always checks real permissions.
    """

    def __init__(self, bot: ParleyBot, user_id: int):
        super().__init__(user_id, timeout=600)
        self.bot = bot
        mine = discord.ui.Button(label="My Server Listings", style=discord.ButtonStyle.primary)
        mine.callback = self._mine
        self.add_item(mine)
        connected = discord.ui.Button(label="Connected Servers", style=discord.ButtonStyle.secondary)
        connected.callback = self._connected
        self.add_item(connected)
        support_url = bot.runtime.bot.support_url
        if support_url and support_url.startswith(("https://", "http://")):
            self.add_item(discord.ui.Button(label="Contact Staff", url=support_url))

    async def _mine(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        from bot.views.management import show_my_servers
        await show_my_servers(interaction)

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
        back = discord.ui.Button(label="Back to My Listing", style=discord.ButtonStyle.secondary, row=2)
        back.callback = self._back
        self.add_item(back)

    async def _back(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        await show_quick_start(interaction)

    async def _pick(self, interaction: discord.Interaction) -> None:
        # A category selection is the first step; open step two immediately.
        # Do not ask the member to click the dropdown and then another button.
        self.category = interaction.data["values"][0]
        await interaction.response.send_modal(QuickPostModal(self.bot, self.category, editing=self.editing))


class QuickPostModal(discord.ui.Modal):
    """Step two: one ad field in automatic mode; invite + summary for review."""
    def __init__(self, bot: ParleyBot, category: str, *, editing: bool = False):
        self.intro_only = bot.runtime.listings.approval_required and not editing
        title = "Submit Server for Review" if self.intro_only else ("Edit Server Ad" if editing else "Post Your Server Ad")
        super().__init__(title=title, timeout=600)
        self.bot, self.category, self.editing = bot, category, editing
        self.invite = None
        if self.intro_only:
            self.invite = discord.ui.TextInput(
                label="Discord invite (regular or vanity)",
                placeholder="https://discord.gg/your-server", style=discord.TextStyle.short,
                max_length=120, required=True,
            )
            self.add_item(self.invite)
        self.description = discord.ui.TextInput(
            label="Short server description" if self.intro_only else "Paste your ad (include invite)",
            placeholder=("What is your server about? Must follow #server-rules."
                         if self.intro_only else "Paste the full ad here, including discord.gg/your-server"),
            style=discord.TextStyle.paragraph,
            max_length=500 if self.intro_only else 1750,
            required=True,
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
                await show_quick_start(
                    interaction,
                    notice="You already have a Free Listing. Use its **Edit**, **Repost**, "
                           "**Switch**, or **Delete** controls instead of making a duplicate.",
                )
                return
            if self.intro_only:
                raw_invite = self.invite.value.strip()
            elif self.editing and existing:
                raw_invite = existing.invite_url
            else:
                # The advertisement itself contains the invite: no duplicate
                # URL field, no paste-twice onboarding. Each additional invite
                # is checked below against the resolved guild ID.
                invite_codes = quick_service.advertisement_invite_codes(self.description.value)
                if not invite_codes:
                    raise ValidationError("Include your Discord invite in the advertisement (discord.gg/name works too).")
                raw_invite = listing_service.canonical_invite(invite_codes[0])
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
            nsfw_level = getattr(getattr(invite.guild, "nsfw_level", None), "name", "").lower()
            if getattr(getattr(invite, "channel", None), "nsfw", False) or nsfw_level == "explicit":
                raise ValidationError("Discord marks this invite as NSFW/explicit. See #server-rules.")
            # Age restrictions alone do not prove sexual/adult content.
            # Review a server marked age_restricted instead of blanket-rejecting it.
            additional_review = nsfw_level == "age_restricted"
            if existing is not None and invite.guild.id != existing.guild_id:
                await show_quick_start(
                    interaction,
                    notice="Your account already has a Free Listing for a different server. "
                           "Use **Switch Server** to release its slot. The normal cooldown still applies.",
                )
                return
            # Catch existing listings *before* making the user finish a preview.
            # Do not infer server ownership from the public invite. A removed
            # listing may only be restored if its original submitter deleted it.
            async with bot.db.session() as session:
                previous = await repository.get_listing(session, invite.guild.id)
                can_restore = bool(
                    previous is not None
                    and await quick_service.can_restore_deleted_listing(
                        session, previous, interaction.user.id
                    )
                )
            if previous is not None and previous.quick_submitted_by != interaction.user.id and not can_restore:
                await interaction.edit_original_response(
                    content=(f"## Already listed · {discord.utils.escape_markdown(invite.guild.name)}\n"
                             f"{quick_service.existing_listing_guidance(previous)}\n\n"
                             "**No changes were made.** You can still manage your other listings."),
                    view=ExistingListingHelpView(bot, interaction.user.id),
                    allowed_mentions=safe_allowed_mentions(),
                )
                return
            icon = getattr(invite.guild, "icon", None)
            guild_info = listing_service.GuildInfo(
                invite.guild.id, invite.guild.name,
                str(icon.url) if icon else None,
                getattr(invite, "approximate_member_count", 0) or 0,
            )
            draft = QuickDraft(
                guild=guild_info, invite_url=listing_service.canonical_invite(invite.code),
                description=self.description.value if self.intro_only else "",
                category=self.category, editing=self.editing,
                raw_ad=None if self.intro_only else self.description.value,
                additional_review=additional_review,
            )
            trusted = (await quick_service.verify_advertisement_invites(
                bot, draft.raw_ad, guild_id=draft.guild.guild_id, invite_url=draft.invite_url,
            )) if draft.raw_ad is not None else frozenset()
            preview, force_review = quick_service.prepare_quick_ad(
                bot.runtime, info=draft.guild, invite_url=draft.invite_url,
                category=draft.category, description=draft.description, raw_ad=draft.raw_ad,
                verified_invite_codes=trusted,
            )
            await interaction.edit_original_response(
                content=(
                    f"## Preview · {discord.utils.escape_markdown(guild_info.name)}\n"
                    f"**Category:** {self.category}\n\n"
                    f"{preview[:1500]}\n\n"
                    f"**After confirmation:** {'Staff review, then post your ad whenever ready' if self.intro_only else ('Staff review' if force_review or additional_review or bot.runtime.listings.approval_required else 'Post to directory')}.\n"
                    "Press **Confirm** to continue."
                ),
                view=QuickConfirmView(bot, interaction.user.id, draft),
                allowed_mentions=safe_allowed_mentions(),
            )
        except ParleyError as exc:
            await interaction.edit_original_response(
                content=f"⚠️ {exc.user_message}",
                view=ExistingListingHelpView(self.bot, interaction.user.id),
            )
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
    async with bot.db.session() as session:
        already_approved = await quick_service.my_quick_listing(session, user_id)
    is_approved_ad = bool(already_approved and already_approved.awaiting_ad and
                          already_approved.status == ListingStatus.ACTIVE)
    if risky and is_approved_ad:
        raise ValidationError("Only server invite links are allowed after server approval; remove external links.")
    if ((bot.runtime.listings.approval_required and not is_approved_ad) or risky or draft.additional_review) and bot.panels.review_channel() is None:
        raise ValidationError("The private staff approvals channel is not ready. Please contact Parley staff.")
    async with bot.db.session() as session:
        waiting = await quick_service.my_quick_listing(session, user_id)
        if waiting and waiting.awaiting_ad and waiting.status == ListingStatus.ACTIVE:
            listing = await quick_service.publish_approved_ad(
                session, bot.runtime, guild_id=waiting.guild_id, actor_id=user_id,
                info=draft.guild, text=draft.raw_ad or "",
                verified_invite_codes=trusted_codes, now=utcnow(),
            )
            outcome = "published_approved"
        elif draft.editing:
            listing, outcome = await quick_service.edit_quick_listing(
                session, bot.runtime, guild_id=draft.guild.guild_id, actor_id=user_id,
                info=draft.guild, invite_url=draft.invite_url,
                description=draft.description, category=draft.category,
                now=utcnow(), raw_ad=draft.raw_ad,
                verified_invite_codes=trusted_codes,
                additional_review=draft.additional_review,
            )
        else:
            listing, outcome = await quick_service.create_quick_listing(
                session, bot.runtime, info=draft.guild, actor_id=user_id,
                invite_url=draft.invite_url, description=draft.description,
                category=draft.category, now=utcnow(),
                is_test=bot.runtime.hub.mode == "test", raw_ad=draft.raw_ad,
                verified_invite_codes=trusted_codes,
                additional_review=draft.additional_review,
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
            "✅ Your edited ad is waiting for staff review; your previous ad stays live."
            if outcome == "pending_edit" else
            ("✅ Your server application is in **#ad-approvals**. If approved, "
             "return to **My Server Listings → Post Approved Ad** any time. "
             "There is **no timed posting window**." if listing.awaiting_ad else
             "✅ Ad is awaiting staff review in **#ad-approvals**; nothing appears publicly until approval.")
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
            if outcome == "published_approved" and published is not None:
                await bot.panels.clear_approval_notice(listing.guild_id)
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
            await interaction.edit_original_response(
                content=f"⚠️ {exc.user_message}",
                view=ExistingListingHelpView(self.bot, interaction.user.id),
            )
        except Exception as exc:
            await handle_error(interaction, exc)


@register_action("quick_relist")
async def quick_relist(interaction: discord.Interaction) -> None:
    await show_quick_start(interaction)


class ApprovedAdModal(discord.ui.Modal):
    """No time window; server already approved. Paste original Markdown as text."""
    def __init__(self, bot: ParleyBot):
        super().__init__(title="Post your approved advertisement", timeout=600)
        self.bot = bot
        self.ad = discord.ui.TextInput(
            label="Your formatted server ad", style=discord.TextStyle.paragraph,
            placeholder="# Welcome!\nWhat members can expect...\nhttps://discord.gg/example",
            max_length=1750, min_length=5, required=True,
        )
        self.add_item(self.ad)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        try:
            async with self.bot.db.session() as session:
                listing = await quick_service.my_quick_listing(session, interaction.user.id)
                guild = await repository.get_guild(session, listing.guild_id) if listing else None
            if not listing or not guild or not listing.awaiting_ad or not listing.invite_url:
                raise ValidationError("This account does not have an approved server awaiting an ad.")
            info = listing_service.GuildInfo(guild.guild_id, guild.name, guild.icon_url, guild.member_count)
            codes = await quick_service.verify_advertisement_invites(
                self.bot, self.ad.value, guild_id=listing.guild_id, invite_url=listing.invite_url,
            )
            preview, unsafe = quick_service.prepare_quick_ad(
                self.bot.runtime, info=info, invite_url=listing.invite_url,
                category=listing.categories[0], raw_ad=self.ad.value, verified_invite_codes=codes,
            )
            if unsafe:
                raise ValidationError("Remove external links; approved advertisements may only contain same-server Discord invites.")
            draft = QuickDraft(info, listing.invite_url, "", listing.categories[0], raw_ad=self.ad.value)
            await interaction.edit_original_response(
                content="## Confirm your approved ad\n" + preview[:1870] +
                        "\n\n**This will publish as plain Discord text, not an embed.**",
                view=QuickConfirmView(self.bot, interaction.user.id, draft),
                allowed_mentions=safe_allowed_mentions(),
            )
        except ParleyError as exc:
            await interaction.edit_original_response(content=f"⚠️ {exc.user_message}", view=None)
        except Exception as exc:
            await handle_error(interaction, exc)
