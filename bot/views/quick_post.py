"""Trust-first UI: invite -> preview -> staff review -> listed, no OAuth."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.services import listings as listing_service
from bot.services import quick_post as quick_service
from bot.services.errors import ParleyError, ValidationError
from bot.utils.helpers import utcnow
from bot.utils.mentions import safe_allowed_mentions
from bot.views.base import OwnedView, acknowledge, get_bot, guard, handle_error, reply
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


async def show_quick_start(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    async with bot.db.session() as session:
        existing = await quick_service.my_quick_listing(session, interaction.user.id)
        stored = await repository.get_guild(session, existing.guild_id) if existing else None
    view = QuickStartView(bot, interaction.user.id)
    if existing:
        name = discord.utils.escape_markdown(stored.name) if stored else f"Server {existing.guild_id}"
        state = {"pending": "Awaiting review", "active": "Listed", "expired": "Expired", "suspended": "Suspended", "removed": "Removed"}.get(existing.status, existing.status)
        message = (
            "## 📣 Your Free Listing\n"
            f"**{name}** · {state}\n"
            "Quick Posts require no Discord authorization and no bot installation. "
            "You can request a repost of your approved listing once every 24 hours.\n\n"
            "**Want multiple servers, faster relisting, or approved partnerships?** "
            "Connect Parley in the servers you manage."
        )
    else:
        message = (
            "## 📣 Advertise your server for free\n"
            "**No OAuth. No bot installation. No server permissions.**\n\n"
            "Paste a Discord invite, choose one category, and describe the community. "
            "Your submission will be reviewed before it appears in the directory.\n\n"
            "**Quick Post:** one unconnected server per account, repost once every 24 hours.\n"
            "**Connected Network:** manage multiple verified servers, relist sooner, and exchange ads only with mutual consent.\n\n"
            "-# Invite links identify servers; they do not prove who owns them."
        )
    await show_screen(interaction, message, view=view)


class QuickStartView(OwnedView):
    def __init__(self, bot: ParleyBot, owner_id: int):
        super().__init__(owner_id)
        self.bot = bot
        quick = discord.ui.Button(label="Post / Repost Free", emoji="📣", style=discord.ButtonStyle.primary, row=0)
        quick.callback = self._quick
        connected = discord.ui.Button(label="Manage Connected Servers", emoji="🤝", style=discord.ButtonStyle.secondary, row=1)
        connected.callback = self._connected
        self.add_item(quick)
        self.add_item(connected)
        install = add_bot_button(bot, row=1)
        if install is not None:
            install.label = "Install Parley (Optional)"
            self.add_item(install)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Do not arm the defer watchdog for a modal-opening button: a modal must
        # be the first response. The live-mode ban check is cached and fast.
        if interaction.user.id != self.owner_id:
            await reply(interaction, "This menu belongs to someone else.")
            return False
        return await guard(interaction, arm_watchdog=False)

    async def _quick(self, interaction: discord.Interaction):
        # Modal must be the INITIAL callback response; never defer here.
        if not interaction.response.is_done():
            await interaction.response.send_modal(QuickPostModal(self.bot))
        else:
            await reply(interaction, "Please open the Post Server menu again and choose Quick Post.")

    async def _connected(self, interaction: discord.Interaction):
        await acknowledge(interaction)
        from bot.views.listings import open_connected_picker
        await open_connected_picker(interaction)


class QuickPostModal(discord.ui.Modal):
    def __init__(self, bot: ParleyBot):
        super().__init__(title="Parley · Free Server Listing", timeout=600)
        self.bot = bot
        self.invite = discord.ui.TextInput(
            label="Discord server invite", placeholder="https://discord.gg/example",
            style=discord.TextStyle.short, max_length=120,
        )
        self.category = discord.ui.TextInput(
            label="Category", placeholder=", ".join(bot.runtime.listings.categories)[:100],
            style=discord.TextStyle.short, max_length=30,
        )
        self.description = discord.ui.TextInput(
            label="Describe your community", placeholder="What can new members expect?",
            style=discord.TextStyle.paragraph, max_length=550,
        )
        self.add_item(self.invite)
        self.add_item(self.category)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        try:
            bot = self.bot
            if bot.log_channel() is None:
                raise ValidationError("Quick Post is temporarily unavailable: a Parley staff review channel must be configured.")
            category = next(
                (c for c in bot.runtime.listings.categories if c.lower() == self.category.value.strip().lower()), None
            )
            if category is None:
                raise ValidationError("Choose one valid category: " + ", ".join(bot.runtime.listings.categories))
            description = quick_service.quick_description(self.description.value, bot.runtime)
            code = listing_service.parse_invite_code(self.invite.value)
            try:
                invitation = await bot.fetch_invite(code, with_counts=True)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise ValidationError("That invite is invalid, expired, or inaccessible. Please use a working public invite.") from exc
            except discord.HTTPException as exc:
                raise ValidationError("Discord couldn't verify the invite. Please try again shortly.") from exc
            guild = invitation.guild
            if guild is None or not getattr(guild, "id", None):
                raise ValidationError("Discord did not return server details for that invite.")
            if guild.id == bot.runtime.hub.main_guild_id:
                raise ValidationError("You cannot submit Parley's main server as a Quick Post.")
            name = getattr(guild, "name", None) or f"Server {guild.id}"
            icon = getattr(guild, "icon", None)
            icon_url = str(icon.url) if icon is not None else None
            size = getattr(invitation, "approximate_member_count", None)
            info = listing_service.GuildInfo(guild.id, name, icon_url, size or 0)
            draft = QuickDraft(info, listing_service.canonical_invite(invitation.code), description, category)
            await interaction.edit_original_response(
                content=(
                    f"## Preview · {discord.utils.escape_markdown(name)}\n"
                    f"**Category:** {category}\n"
                    f"**Invite:** {draft.invite_url}\n"
                    f"**Description:** {description}\n\n"
                    "**Important:** This is an **unverified community listing**, not an ownership claim. "
                    "New ads require staff approval. If this is your existing listing, "
                    "only the **previously approved text** is reposted (changes to this form are ignored)."
                ),
                view=QuickConfirmView(bot, interaction.user.id, draft),
                allowed_mentions=safe_allowed_mentions(),
            )
        except ParleyError as exc:
            await interaction.edit_original_response(
                content=f"⚠️ {exc.user_message}\n\nUse **Post a Server Free** to try again.",
                view=None, allowed_mentions=safe_allowed_mentions(),
            )
        except Exception as exc:
            await handle_error(interaction, exc)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_error(interaction, error)


class QuickConfirmView(OwnedView):
    def __init__(self, bot: ParleyBot, owner_id: int, draft: QuickDraft):
        super().__init__(owner_id, timeout=300)
        self.bot, self.draft = bot, draft
        submit = discord.ui.Button(label="Submit Listing", emoji="✅", style=discord.ButtonStyle.success)
        submit.callback = self._submit
        self.add_item(submit)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _cancel(self, interaction: discord.Interaction):
        await acknowledge(interaction)
        self.stop()
        await interaction.edit_original_response(content="Quick Post cancelled. Nothing was submitted.", view=None)

    async def _submit(self, interaction: discord.Interaction):
        await acknowledge(interaction)
        try:
            bot = self.bot
            if bot.log_channel() is None:
                raise ValidationError("A staff review channel is required before Quick Posts can be submitted.")
            # The preview is short-lived, but refetch at publish time so an expired
            # or reassigned invite cannot be accepted based on stale metadata.
            code = listing_service.parse_invite_code(self.draft.invite_url)
            try:
                live = await bot.fetch_invite(code, with_counts=False)
            except discord.HTTPException as exc:
                raise ValidationError("Your invite is no longer valid. Please reopen Quick Post.") from exc
            if live.guild is None or live.guild.id != self.draft.guild.guild_id:
                raise ValidationError("The invite no longer matches this community. Please start again.")
            async with bot.db.session() as session:
                listing, outcome = await quick_service.create_quick_listing(
                    session, bot.runtime, info=self.draft.guild, actor_id=interaction.user.id,
                    invite_url=self.draft.invite_url, description=self.draft.description,
                    category=self.draft.category, now=utcnow(), is_test=bot.runtime.hub.mode == "test",
                )
            if outcome == "pending":
                from bot.views.listings import send_for_review
                try:
                    routed = await send_for_review(bot, listing, self.draft.guild.name)
                except discord.HTTPException:
                    log.exception("Quick Post saved but couldn't send staff review guild_id=%s", listing.guild_id)
                    routed = False
                message = (
                    "## ✅ Submitted for review\n"
                    f"**{discord.utils.escape_markdown(self.draft.guild.name)}** has been saved. "
                    "It will only appear publicly if Parley staff approve it."
                ) if routed else (
                    "## Submission saved — staff notification unavailable\n"
                    "Parley saved your ad for manual review but couldn't notify staff. "
                    "Please contact Parley support if it isn't reviewed."
                )
            else:
                posted = await bot.panels.publish_listing(listing.guild_id)
                message = (
                    f"## ✅ Reposted {discord.utils.escape_markdown(self.draft.guild.name)}\n"
                    "Your existing approved listing was refreshed. Next free repost: 24 hours."
                    if posted is not None else
                    "Your repost was recorded, but the directory is temporarily unavailable. Contact staff."
                )
            self.stop()
            await interaction.edit_original_response(
                content=message + "\n\n**Want multiple servers and faster relisting?** Connect Parley to a server you manage.",
                view=persistent_view(add_bot_button(bot)),
                allowed_mentions=safe_allowed_mentions(),
            )
        except ParleyError as exc:
            await interaction.edit_original_response(
                content=f"⚠️ {exc.user_message}", view=None,
                allowed_mentions=safe_allowed_mentions(),
            )
        except Exception as exc:
            await handle_error(interaction, exc)


@register_action("quick_relist")
async def quick_relist(interaction: discord.Interaction) -> None:
    """Persistent shortcut to the same invite/repost flow; never bypass ownership rules."""
    await show_quick_start(interaction)
