"""Simple listing management: Relist, Edit, View Ad, Remove."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.config import templates
from bot.config.runtime import DEFAULT_BUTTONS
from bot.database import repository
from bot.database.models import Listing, ListingStatus
from bot.modals.advertisement import AdvertisementModal
from bot.services import listings as listing_service
from bot.services import permissions
from bot.services.errors import LISTING_GONE, NotFound, ValidationError
from bot.utils.helpers import format_duration, format_members, format_minimum, listing_jump_url, truncate, utcnow
from bot.views.base import ConfirmView, OwnedView, get_bot, guard, handle_error, home_button, reply
from bot.views.listings import ListingDraft, ListingFormView
from bot.views.welcome import action_button, persistent_view, register_action, show_screen

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

STATUS_LABELS = {
    ListingStatus.ACTIVE: "Live",
    ListingStatus.PENDING: "Waiting for staff review",
    ListingStatus.SUSPENDED: "Suspended by staff",
    ListingStatus.EXPIRED: "Expired — Relist to make it visible again",
    ListingStatus.REMOVED: "Removed",
}


def status_label(listing: Listing) -> str:
    label = STATUS_LABELS.get(listing.status, listing.status)
    if listing.pending_changes:
        label += " · edit waiting for review"
    if listing.is_test:
        label += " · test"
    return label


MAIN_ACTIONS = ("relist", "edit", "preview")
EDIT_ACTIONS = ("edit_ad", "edit_info")


class ManageButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wp:manage:(?P<gid>\d+)"):
    def __init__(
        self,
        guild_id: int,
        *,
        label: str | None = None,
        emoji: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        row: int | None = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label or "Manage", emoji=emoji, style=style, custom_id=f"wp:manage:{guild_id}", row=row
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(int(match["gid"]), label=item.label, emoji=item.emoji, style=item.style)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        try:
            await show_management(interaction, self.guild_id)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)


class ManagementButton(
    discord.ui.DynamicItem[discord.ui.Button],
    # "refresh" stays accepted so buttons on older messages keep working after upgrades.
    template=r"wp:m:(?P<action>edit_ad|edit_info|partnerships|edit|preview|refresh|relist|remove|self_post):(?P<gid>\d+)",
):
    def __init__(self, action: str, guild_id: int, bot: ParleyBot | None = None, row: int | None = None) -> None:
        from bot.views.welcome import BUTTON_STYLE_MAP

        config_key = "relist" if action == "refresh" else action
        if bot is not None:
            label, emoji = bot.runtime.button(config_key)
            style = BUTTON_STYLE_MAP[bot.runtime.button_style(config_key)]
        else:
            spec = DEFAULT_BUTTONS[config_key]
            label, emoji = str(spec["label"]), spec["emoji"]
            style = BUTTON_STYLE_MAP[str(spec["style"])]
        super().__init__(
            discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"wp:m:{action}:{guild_id}", row=row)
        )
        self.action = action
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["action"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        handlers = {
            "edit": show_edit_menu,
            "self_post": start_self_post,
            "edit_ad": edit_ad,
            "edit_info": edit_info,
            "partnerships": partnership_settings,
            "preview": preview,
            "refresh": relist,
            "relist": relist,
            "remove": remove,
        }
        try:
            await handlers[self.action](interaction, self.guild_id)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)


def manage_button(bot: ParleyBot, guild_id: int, guild_name: str, row: int | None = None) -> ManageButton:
    return ManageButton(guild_id, label=truncate(f"Manage {guild_name}", 80), row=row)


# ---------------------------------------------------------------- My Listing(s)


@register_action("servers")
async def show_my_servers(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    managed = {g.id: g for g in permissions.cached_manageable_guilds(bot, interaction.user.id)}
    async with bot.db.session() as session:
        rows = [
            row
            for row in await repository.get_listings(session, managed.keys())
            if row.status != ListingStatus.REMOVED
        ]
    if not rows:
        await reply(
            interaction,
            "You don't have a listed server yet.",
            view=persistent_view(action_button(bot, "post")),
        )
        return

    if len(rows) == 1:
        await show_management(interaction, rows[0].guild_id)
        return

    rows.sort(key=lambda row: managed[row.guild_id].name.lower())
    from bot.views.partnership import GuildPickerView

    async def picked(inter: discord.Interaction, guild_id: int) -> None:
        await show_management(inter, guild_id)

    embed = discord.Embed(
        title="My Servers",
        description="Choose a server to manage.",
        color=bot.runtime.bot.color_primary,
    )
    await reply(
        interaction,
        embed=embed,
        view=GuildPickerView(
            interaction.user.id,
            [(row.guild_id, managed[row.guild_id].name) for row in rows],
            picked,
            placeholder="Choose a server",
        ),
    )


# ---------------------------------------------------------------- management panel


def management_text(bot: ParleyBot, listing: Listing, guild: discord.Guild) -> str:
    """Plain-text fallback for tests/logs. Discord uses ``management_embed``."""
    category = ", ".join(listing.categories) or "Other"
    partnership = (
        f"Open · {format_minimum(listing.minimum_members)}"
        if listing.accepting_partnerships
        else "Not accepting requests"
    )
    if listing.status != ListingStatus.ACTIVE or listing.pending_changes or listing.is_test:
        relist = status_label(listing)
    else:
        remaining = listing_service.refresh_remaining(listing, bot.runtime, utcnow())
        relist = f"Available in {format_duration(remaining)}" if remaining is not None else "Available now"
    lines = [
        "My Listing",
        guild.name,
        f"{category} · {format_members(guild.member_count or 0)}",
        f"Partnerships: {partnership}",
        f"Relist: {relist}",
    ]
    if listing.self_posted and not listing_service.ad_edit_available(listing):
        lines.append("Ad edit: available after your next Relist")
    return "\n".join(lines)


def management_embed(bot: ParleyBot, listing: Listing, guild: discord.Guild) -> discord.Embed:
    """A calm, information-first control card for one server listing."""
    category = ", ".join(listing.categories) or "Other"
    embed = discord.Embed(
        title="🧭 Listing Manager",
        description=f"**{guild.name}**\n{category} · {format_members(guild.member_count or 0)}",
        color=(bot.runtime.bot.color_warning if listing.is_test else bot.runtime.bot.color_primary),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    status = status_label(listing)
    status_icon = {
        ListingStatus.ACTIVE: "🟢",
        ListingStatus.PENDING: "🟡",
        ListingStatus.SUSPENDED: "🔴",
        ListingStatus.EXPIRED: "⚪",
        ListingStatus.REMOVED: "⚪",
    }.get(listing.status, "⚪")
    embed.add_field(name="📣 Listing", value=f"{status_icon} {status}", inline=True)

    if listing.accepting_partnerships:
        partner_value = f"Open · {format_minimum(listing.minimum_members)}"
    else:
        partner_value = "Off for this server"
    embed.add_field(name="🤝 Partnerships", value=partner_value, inline=True)

    if listing.status != ListingStatus.ACTIVE or listing.pending_changes:
        relist_value = status_label(listing)
    else:
        remaining = listing_service.refresh_remaining(listing, bot.runtime, utcnow())
        relist_value = f"In {format_duration(remaining)}" if remaining is not None else "Ready now"
    embed.add_field(name="↻ Relist", value=relist_value, inline=True)

    if listing.self_posted:
        edit_value = "Ready" if listing_service.ad_edit_available(listing) else "Unlocks after your next Relist"
    else:
        edit_value = "Ready"
    embed.add_field(name="Ad editing", value=edit_value, inline=False)

    notes = []
    if listing.pending_changes:
        notes.append("An edit is waiting for staff review")
    if listing.is_test:
        notes.append("🧪 TEST listing")
    if notes:
        embed.set_footer(text=" · ".join(notes))
    else:
        embed.set_footer(text="Changes here affect only this server")
    return embed


def management_view(bot: ParleyBot, listing: Listing, guild: discord.Guild) -> discord.ui.View:
    """Server-specific actions grouped by task, not one wall of buttons."""
    ad_url = listing_jump_url(bot.runtime.hub.main_guild_id, listing.channel_id, listing.message_id)
    all_listings = action_button(bot, "directory", style=discord.ButtonStyle.secondary, row=3)
    all_listings.item.label = "All Listings"
    all_listings.item.emoji = None
    return persistent_view(
        ManagementButton("edit_ad", guild.id, bot, row=0),
        ManagementButton("edit_info", guild.id, bot, row=0),
        discord.ui.Button(label="View Ad", url=ad_url, row=0) if ad_url else ManagementButton("preview", guild.id, bot, row=0),
        ManagementButton("partnerships", guild.id, bot, row=1),
        ManagementButton("relist", guild.id, bot, row=1),
        ManagementButton("remove", guild.id, bot, row=2),
        all_listings,
        home_button(bot, row=3),
    )


async def load_managed_listing(bot: ParleyBot, guild_id: int, user_id: int) -> tuple[discord.Guild, Listing, list[int]]:
    guild, _member = await permissions.require_manager(bot, guild_id, user_id)
    async with bot.db.session() as session:
        listing = await repository.get_listing(session, guild_id)
        if listing is None or listing.status == ListingStatus.REMOVED:
            raise NotFound(LISTING_GONE)
        contacts = await repository.get_contact_ids(session, guild_id)
    return guild, listing, contacts


async def show_management(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    guild, listing, _contacts = await load_managed_listing(bot, guild_id, interaction.user.id)
    await show_screen(interaction, view=management_view(bot, listing, guild), embed=management_embed(bot, listing, guild))


class PartnershipSettingsView(OwnedView):
    """One-server partnership switch with an explicit state before changing it."""

    def __init__(self, bot: ParleyBot, owner_id: int, guild_id: int, *, enabled: bool) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.guild_id = guild_id
        toggle = discord.ui.Button(
            label="Disable Partnerships" if enabled else "Enable Partnerships",
            emoji="🤝",
            style=discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.success,
            row=0,
        )
        toggle.callback = self._toggle  # type: ignore[method-assign]
        self.add_item(toggle)
        self.add_item(ManageButton(guild_id, label="Back", emoji=None, style=discord.ButtonStyle.secondary, row=1))

    async def _toggle(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, listing, contacts = await load_managed_listing(self.bot, self.guild_id, interaction.user.id)
        target = not listing.accepting_partnerships
        async with self.bot.db.session() as session:
            updated = await listing_service.update_info(
                session,
                self.bot.runtime,
                guild_id=self.guild_id,
                categories=listing.categories,
                accepting_partnerships=target,
                minimum_members=listing.minimum_members if target else 0,
                contact_ids=contacts,
                invite_url=listing.invite_url,
                actor_id=interaction.user.id,
                now=utcnow(),
            )
        from bot.views.listings import after_edit

        notice = await after_edit(self.bot, updated, guild.name)
        _guild, refreshed, _contacts = await load_managed_listing(self.bot, self.guild_id, interaction.user.id)
        embed = management_embed(self.bot, refreshed, guild)
        if notice:
            embed.add_field(name="Saved", value=notice, inline=False)
        else:
            state = "enabled" if refreshed.accepting_partnerships else "disabled"
            embed.add_field(name="Saved", value=f"Partnership requests are now **{state}** for this server.", inline=False)
        await interaction.edit_original_response(content=None, embed=embed, view=management_view(self.bot, refreshed, guild))


async def partnership_settings(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    guild, listing, _contacts = await load_managed_listing(bot, guild_id, interaction.user.id)
    state = "open" if listing.accepting_partnerships else "off"
    minimum = format_minimum(listing.minimum_members) if listing.accepting_partnerships else "—"
    embed = discord.Embed(
        title=f"🤝 Partnerships · {truncate(guild.name, 180)}",
        description=(
            "This switch only changes partnership requests for **this server**. "
            "Your other listings keep their own settings."
        ),
        color=bot.runtime.bot.color_primary,
    )
    embed.add_field(name="Status", value=state.title(), inline=True)
    embed.add_field(name="Minimum partner size", value=minimum, inline=True)
    if not listing.accepting_partnerships:
        embed.set_footer(text="Enable it here, then use Edit Server Info if you want a minimum member requirement")
    await show_screen(
        interaction,
        embed=embed,
        view=PartnershipSettingsView(bot, interaction.user.id, guild_id, enabled=listing.accepting_partnerships),
    )


async def show_edit_menu(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    guild, _listing, _contacts = await load_managed_listing(bot, guild_id, interaction.user.id)
    view = persistent_view(
        *(ManagementButton(action, guild_id, bot, row=0) for action in EDIT_ACTIONS),
        ManageButton(guild_id, label="Back", emoji=None, style=discord.ButtonStyle.secondary, row=1),
    )
    await show_screen(interaction, f"## Edit {guild.name}\nChoose what you want to change.", view=view)


# ---------------------------------------------------------------- direct-post ad replacement


async def start_self_post(interaction: discord.Interaction, guild_id: int) -> None:
    """Replace a self-authored ad. One successful replacement is allowed per Relist cycle."""
    from bot.views import self_post as self_post_service
    from bot.views.listings import send_for_review

    bot = get_bot(interaction)
    guild, listing, _contacts = await load_managed_listing(bot, guild_id, interaction.user.id)
    if not listing_service.ad_edit_available(listing):
        raise ValidationError("You've already edited this ad since your last Relist. Relist again to unlock another edit.")

    await interaction.response.defer()

    async def held_for_review(text: str) -> None:
        now = utcnow()
        async with bot.db.session() as session:
            updated = await listing_service.update_advertisement(
                session, bot.runtime, guild_id=guild_id, text=text, actor_id=interaction.user.id, now=now
            )
            await listing_service.mark_ad_edit_used(
                session, guild_id=guild_id, actor_id=interaction.user.id, now=now
            )
        await send_for_review(bot, updated, guild.name)

    async def accepted_edit(text: str, message: discord.Message) -> None:
        now = utcnow()
        async with bot.db.session() as session:
            current = await repository.get_listing(session, guild_id)
            if current is None:
                raise NotFound(LISTING_GONE)
            current.advertisement_text = text
            current.updated_at = now
            await listing_service.mark_ad_edit_used(
                session, guild_id=guild_id, actor_id=interaction.user.id, now=now
            )
        await bot.panels.adopt_self_post(guild_id, message)

    note = await self_post_service.run_submission(
        interaction,
        guild_id,
        guild.name,
        on_accept=accepted_edit,
        on_review=held_for_review,
    )
    await interaction.edit_original_response(
        content=note,
        view=persistent_view(manage_button(bot, guild_id, guild.name), home_button(bot)),
    )


# ---------------------------------------------------------------- actions


async def edit_ad(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    guild, listing, _contacts = await load_managed_listing(bot, guild_id, interaction.user.id)

    if listing.self_posted:
        await start_self_post(interaction, guild_id)
        return

    async def submit(inter: discord.Interaction, text: str, _invite: str) -> None:
        from bot.views.listings import after_edit

        await inter.response.defer(ephemeral=True, thinking=True)
        await permissions.require_manager(bot, guild_id, inter.user.id)
        async with bot.db.session() as session:
            updated = await listing_service.update_advertisement(
                session, bot.runtime, guild_id=guild_id, text=text, actor_id=inter.user.id, now=utcnow()
            )
        notice = await after_edit(bot, updated, guild.name)
        await reply(inter, notice or f"Advertisement updated for **{guild.name}**.", view=persistent_view(home_button(bot)))

    await interaction.response.send_modal(
        AdvertisementModal(
            title=f"Edit ad · {guild.name}",
            max_length=bot.runtime.listings.max_ad_length,
            on_submit=submit,
            default_text=listing.advertisement_text,
        )
    )


async def edit_info(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    guild, listing, contacts = await load_managed_listing(bot, guild_id, interaction.user.id)
    draft = ListingDraft(
        categories=[c for c in listing.categories if c in bot.runtime.listings.categories],
        accepting=listing.accepting_partnerships,
        minimum=listing.minimum_members,
        contacts=contacts or [interaction.user.id],
        ad_text=listing.advertisement_text,
        invite_url=listing.invite_url,
    )
    form = ListingFormView(bot, interaction.user.id, guild_id, guild.name, draft, mode="edit", in_dm=interaction.guild is None)
    await reply(interaction, form.render(), view=form)


async def preview(interaction: discord.Interaction, guild_id: int) -> None:
    """Legacy dynamic view button: always resolve to the latest real ad."""
    bot = get_bot(interaction)
    guild, listing, _contacts = await load_managed_listing(bot, guild_id, interaction.user.id)
    url = listing_jump_url(bot.runtime.hub.main_guild_id, listing.channel_id, listing.message_id)
    if url:
        embed = discord.Embed(
            title="View Ad",
            description=f"[Open the latest ad for **{guild.name}**]({url})",
            color=bot.runtime.bot.color_primary,
        )
        await reply(interaction, embed=embed)
        return
    await reply(interaction, "This ad isn't published in the directory right now.")


async def relist(interaction: discord.Interaction, guild_id: int) -> None:
    """Move a server back to the newest position. Cooldown is per server."""
    bot = get_bot(interaction)
    guild, listing, _contacts = await load_managed_listing(bot, guild_id, interaction.user.id)
    permissions.require_public_bot_channel(guild)
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.db.session() as session:
        await listing_service.claim_refresh(
            session, bot.runtime, guild_id=guild_id, actor_id=interaction.user.id, now=utcnow()
        )

    if listing.self_posted:
        message = await bot.panels.relist_self_post(guild_id)
    else:
        message = await bot.panels.publish_listing(guild_id)

    if message is None:
        await reply(interaction, templates.render(bot.runtime, "listing_saved_unpublished", server_name=guild.name))
        return
    await reply(
        interaction,
        f"## Relisted\n**{guild.name}** is back at the top of the directory.\n-# Available again in {bot.runtime.listings.refresh_cooldown_minutes} minutes.",
        view=persistent_view(home_button(bot)),
    )


# Old code/tests may still import refresh. It now means Relist.
refresh = relist


@register_action("relist")
async def relist_from_anywhere(interaction: discord.Interaction) -> None:
    """Relist from DM, the Parley hub, or the connected server itself."""
    bot = get_bot(interaction)

    # In a connected non-hub server, that server is the obvious target.
    if interaction.guild is not None and interaction.guild.id != bot.runtime.hub.main_guild_id:
        if await permissions.is_manager(bot, interaction.guild.id, interaction.user.id):
            async with bot.db.session() as session:
                listing = await repository.get_listing(session, interaction.guild.id)
            if listing is not None and listing.status != ListingStatus.REMOVED:
                await relist(interaction, interaction.guild.id)
                return

    managed = {g.id: g for g in permissions.cached_manageable_guilds(bot, interaction.user.id)}
    async with bot.db.session() as session:
        rows = [
            row for row in await repository.get_listings(session, managed.keys())
            if row.status != ListingStatus.REMOVED
        ]
    if not rows:
        await reply(interaction, "You don't have a listed server yet.", view=persistent_view(action_button(bot, "post")))
        return
    if len(rows) == 1:
        await relist(interaction, rows[0].guild_id)
        return

    from bot.views.partnership import GuildPickerView

    async def picked(inter: discord.Interaction, guild_id: int) -> None:
        await relist(inter, guild_id)

    embed = discord.Embed(
        title="Relist",
        description="Choose a server.",
        color=bot.runtime.bot.color_primary,
    )
    await reply(
        interaction,
        embed=embed,
        view=GuildPickerView(interaction.user.id, [(row.guild_id, managed[row.guild_id].name) for row in rows], picked),
    )


async def remove(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    guild, _listing, _contacts = await load_managed_listing(bot, guild_id, interaction.user.id)
    confirm = ConfirmView(interaction.user.id, confirm_label="Yes, remove it")
    await reply(
        interaction,
        f"Remove the listing for **{guild.name}**? The ad and pending requests will be removed.",
        view=confirm,
    )
    await confirm.wait()
    if not confirm.confirmed or confirm.interaction is None:
        return
    done = confirm.interaction
    await done.response.defer()
    await permissions.require_manager(bot, guild_id, done.user.id)
    async with bot.db.session() as session:
        listing = await listing_service.remove_listing(session, guild_id=guild_id, actor_id=done.user.id, now=utcnow())
    await bot.panels.take_down_listing(listing)
    await bot.log_event(f"**{guild.name}** (`{guild_id}`) removed its listing ({done.user.mention}).")
    await done.edit_original_response(content=f"The listing for **{guild.name}** was removed.", view=None)
