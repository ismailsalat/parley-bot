"""Post My Server: choose server -> category -> contacts -> paste ad -> done."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import Listing, ListingStatus
from bot.modals.advertisement import AdvertisementModal, InviteModal
from bot.services import listings as listing_service
from bot.services import moderation, permissions, verification
from bot.views.verify import verified_guild_info
from bot.services.errors import MANAGE_SERVER_REQUIRED, PermissionDenied, ValidationError, ParleyError
from bot.utils.helpers import truncate, utcnow
from bot.config import templates
from bot.utils.mentions import safe_allowed_mentions
from bot.views import self_post
from bot.views.base import (
    acknowledge,
    GENERIC_ERROR,
    ConfirmView,
    OwnedView,
    deliver_dms,
    get_bot,
    guard,
    guild_info,
    handle_error,
    home_button,
    reply,
)
from bot.views.welcome import action_button, persistent_view, register_action, show_screen

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)


@dataclass
class ListingDraft:
    categories: list[str] = field(default_factory=list)
    accepting: bool = True
    minimum: int = 0
    contacts: list[int] = field(default_factory=list)
    ad_text: str = ""
    invite_url: str | None = None
    invite_raw: str = ""
    builder_used: bool = False
    emoji_warning_seen: bool = False


# ---------------------------------------------------------------- invites & contacts


async def create_invite(guild: discord.Guild) -> str | None:
    """Create a permanent invite in a member-visible channel only."""
    me = guild.me
    everyone = guild.default_role
    candidates: list[discord.abc.GuildChannel] = []
    for channel in (guild.rules_channel, guild.system_channel):
        if channel is not None:
            candidates.append(channel)
    candidates.extend(guild.text_channels)
    for channel in candidates:
        if (
            not isinstance(channel, discord.TextChannel)
            or not channel.permissions_for(everyone).view_channel
            or not channel.permissions_for(me).create_instant_invite
        ):
            continue
        try:
            invite = await channel.create_invite(max_age=0, max_uses=0, unique=False, reason="Parley listing invite")
        except discord.HTTPException as exc:
            log.info("Could not create invite in guild %s channel %s: %s", guild.id, channel.id, exc)
            continue
        return listing_service.canonical_invite(invite.code)
    return None


async def resolve_invite(bot: ParleyBot, guild: discord.Guild, raw: str | None) -> str | None:
    """Validate a typed invite (must point at *this* guild) or create one."""
    raw = (raw or "").strip()
    if raw:
        code = listing_service.parse_invite_code(raw)
        try:
            invite = await bot.fetch_invite(code, with_counts=False)
        except discord.NotFound as exc:
            raise ValidationError("That invite link is invalid or has expired.") from exc
        if invite.guild is None or invite.guild.id != guild.id:
            raise ValidationError("That invite link points to a different server.")
        invite_channel = guild.get_channel(invite.channel.id) if invite.channel is not None else None
        if isinstance(invite_channel, discord.TextChannel) and not invite_channel.permissions_for(guild.default_role).view_channel:
            raise ValidationError("That invite points to a private channel. Use an invite from a channel @everyone can view.")
        return listing_service.canonical_invite(invite.code)

    rules = bot.runtime.listings
    if rules.auto_create_invite:
        created = await create_invite(guild)
        if created:
            return created
    if rules.invite_required:
        raise InviteMissing("Parley couldn't create an invite. Choose a channel for it or paste one.")
    return None


async def validate_contacts(guild: discord.Guild, contact_ids: list[int]) -> list[int]:
    valid = []
    for user_id in contact_ids:
        member = guild.get_member(user_id) or await permissions.fetch_member(guild, user_id)
        if member is None:
            raise ValidationError(f"<@{user_id}> isn't a member of **{guild.name}**.")
        if member.bot:
            raise ValidationError("Bots can't be partnership contacts.")
        valid.append(user_id)
    return valid


# ---------------------------------------------------------------- entry point


def _picker_status(listing: Listing | None, *, public_channel_ok: bool) -> str:
    if not public_channel_ok:
        return "Needs a member-visible Parley channel"
    if listing is None or listing.status == ListingStatus.REMOVED:
        return "Ready to list"
    if listing.status == ListingStatus.ACTIVE:
        return "Live in the directory"
    if listing.status == ListingStatus.PENDING:
        return "Waiting for review"
    if listing.status == ListingStatus.SUSPENDED:
        return "Listing suspended"
    if listing.status == ListingStatus.EXPIRED:
        return "Expired · ready to list again"
    return listing.status.title()


class PostServerPickerView(OwnedView):
    """Clean server picker for Post My Server.

    The old flow skipped the picker when only one *unlisted* server remained,
    which made users think Parley only knew about one of their servers. This
    view always shows every server the user can currently manage where Parley
    is installed, and clearly marks the listing state of each one.
    """

    PAGE_SIZE = 10

    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        guilds: list[discord.Guild],
        listings: dict[int, Listing],
        *,
        page: int = 0,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.guilds = sorted(guilds, key=lambda g: g.name.lower())
        self.listings = listings
        self.page = max(0, page)
        self._build()

    @property
    def pages(self) -> int:
        return max(1, (len(self.guilds) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _page_guilds(self) -> list[discord.Guild]:
        self.page = min(self.page, self.pages - 1)
        start = self.page * self.PAGE_SIZE
        return self.guilds[start : start + self.PAGE_SIZE]

    def _build(self) -> None:
        self.clear_items()
        page_guilds = self._page_guilds()
        options: list[discord.SelectOption] = []
        for guild in page_guilds:
            listing = self.listings.get(guild.id)
            status = _picker_status(listing, public_channel_ok=permissions.public_bot_channel(guild) is not None)
            member_count = guild.member_count or 0
            options.append(
                discord.SelectOption(
                    label=truncate(guild.name, 100),
                    description=truncate(f"{status} · {member_count:,} members", 100),
                    value=str(guild.id),
                )
            )

        select = discord.ui.Select(
            placeholder="Choose a server",
            options=options,
            row=0,
        )
        select.callback = self._picked  # type: ignore[method-assign]
        self._select = select
        self.add_item(select)

        if self.pages > 1:
            previous = discord.ui.Button(
                label="Previous",
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=self.page <= 0,
            )
            next_button = discord.ui.Button(
                label="Next",
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=self.page >= self.pages - 1,
            )
            previous.callback = self._previous  # type: ignore[method-assign]
            next_button.callback = self._next  # type: ignore[method-assign]
            self.add_item(previous)
            self.add_item(next_button)

        # Parley being installed somewhere must never be the only way to list a
        # server: any other server can still be listed through verification.
        another = discord.ui.Button(label="Refresh Servers", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
        another.callback = self._verify_another  # type: ignore[method-assign]
        self.add_item(another)

    async def _verify_another(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        from bot.views.verify import start_verification

        self.stop()
        await start_verification(interaction, force=True)

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="😊 Choose a Server",
            description="Pick the server you want to list or manage.",
            color=self.bot.runtime.bot.color_success,
        )
        if self.pages > 1:
            embed.set_footer(text=f"Page {self.page + 1} of {self.pages}")
        return embed

    async def _picked(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        guild_id = int(self._select.values[0])
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise ValidationError("Parley is no longer in that server.")

        listing = self.listings.get(guild_id)
        if listing is not None and listing.status in ListingStatus.LIVE:
            from bot.views.management import show_management

            await show_management(interaction, guild_id)
            return
        await open_listing_form(interaction, guild, edit_message=True)

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._build()
        await interaction.response.edit_message(content=None, embed=self.embed(), view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        self._build()
        await interaction.response.edit_message(content=None, embed=self.embed(), view=self)


def _verify_another_button(row: int | None = None) -> discord.ui.Button:
    """The always-available route to listing a server Parley isn't in."""
    button = discord.ui.Button(
        label="Refresh Servers", emoji="🔄", style=discord.ButtonStyle.secondary, row=row
    )

    async def callback(interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        from bot.views.verify import start_verification

        await start_verification(interaction, force=True)

    button.callback = callback  # type: ignore[method-assign]
    return button


@register_action("post", "connect")
async def start_post_flow(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    user = interaction.user
    guild = interaction.guild

    if guild is not None and guild.id != bot.runtime.hub.main_guild_id:
        # /connect or the Post Server Ad button inside a specific server should
        # stay direct: the server is already obvious in that context.
        if not isinstance(user, discord.Member) or not permissions.can_manage(user.guild_permissions):
            raise PermissionDenied(MANAGE_SERVER_REQUIRED)
        await open_listing_form(interaction, guild)
        return

    # In DMs / the Parley hub: servers where Parley is installed are offered directly,
    # and anything else goes through "Verify My Servers", which needs no bot at all.
    # Existing verified listings stay manageable even after Parley is removed.
    candidates = [
        g for g in permissions.cached_manageable_guilds(bot, user.id)
        if g.id != bot.runtime.hub.main_guild_id
    ]
    candidate_ids = {g.id for g in candidates}
    async with bot.db.session() as session:
        delegated_ids = set(await repository.guild_ids_connected_by(session, user.id))
        disconnected_ids = delegated_ids - candidate_ids
        disconnected_rows = [
            row
            for row in await repository.get_listings(session, disconnected_ids)
            if row.status != ListingStatus.REMOVED
        ]
        disconnected_guilds = await repository.get_guilds(
            session, [row.guild_id for row in disconnected_rows]
        )
        existing = {
            row.guild_id: row
            for row in await repository.get_listings(session, candidate_ids)
        }

    if not candidates and disconnected_rows:
        # Post Server Ad must never dead-end into management: listing another
        # server is the whole point of the button.
        from bot.views.management import show_management
        from bot.views.partnership import GuildPickerView

        async def picked(inter: discord.Interaction, guild_id: int) -> None:
            await show_management(inter, guild_id)

        embed = discord.Embed(
            title="😊 Choose a Server",
            description="Pick a listing to manage, or add another server.",
            color=bot.runtime.bot.color_success,
        )
        view = GuildPickerView(
            user.id,
            [
                (
                    row.guild_id,
                    disconnected_guilds[row.guild_id].name
                    if row.guild_id in disconnected_guilds
                    else f"Server {row.guild_id}",
                )
                for row in disconnected_rows
            ],
            picked,
            placeholder="Choose a listing",
        )
        view.add_item(_verify_another_button())
        await show_screen(interaction, embed=embed, view=view)
        return

    if not candidates:
        # Listing never requires installing Parley: prove Manage Server with a
        # read-only Discord login instead.
        from bot.views.verify import start_verification

        await start_verification(interaction)
        return

    view = PostServerPickerView(bot, user.id, candidates, existing)
    await show_screen(interaction, embed=view.embed(), view=view)


async def open_verified_listing_form(interaction: discord.Interaction, verified) -> None:
    """Listing form for a server Parley is *not* in, backed by OAuth verification."""
    bot = get_bot(interaction)
    user_id = interaction.user.id

    async with bot.db.session() as session:
        # Re-check against the verified set: a guild id never comes from the user.
        verified = await verification.require_verified_guild(
            session, user_id=user_id, guild_id=verified.id, now=utcnow()
        )
        await moderation.ensure_allowed(session, bot.runtime, guild_ids=[verified.id], user_id=user_id)
        existing = await repository.get_listing(session, verified.id)

    if existing is not None and existing.status in ListingStatus.LIVE:
        from bot.views.management import manage_button

        if interaction.response.is_done():
            await interaction.edit_original_response(
                content="This server is already listed.", embeds=[],
                view=persistent_view(manage_button(bot, verified.id, verified.name)),
            )
        else:
            await reply(
                interaction,
                "This server is already listed.",
                view=persistent_view(manage_button(bot, verified.id, verified.name)),
            )
        return

    draft = ListingDraft(contacts=[user_id])
    if existing is not None:
        draft.categories = [c for c in existing.categories if c in bot.runtime.listings.categories]
        draft.accepting = existing.accepting_partnerships
        draft.minimum = existing.minimum_members
        draft.ad_text = existing.advertisement_text
        draft.invite_url = existing.invite_url

    form = ListingFormView(
        bot, user_id, verified.id, verified.name, draft,
        mode="create", in_dm=interaction.guild is None, verified=verified,
    )
    if interaction.response.is_done():
        await interaction.edit_original_response(content=form.render(), embeds=[], view=form)
    else:
        await reply(interaction, form.render(), view=form)


async def open_listing_form(interaction: discord.Interaction, guild: discord.Guild, *, edit_message: bool = False) -> None:
    bot = get_bot(interaction)
    user_id = interaction.user.id
    # A hidden bot-only channel cannot be used to qualify for the partnership directory.
    permissions.require_public_bot_channel(guild)

    async with bot.db.session() as session:
        await moderation.ensure_allowed(session, bot.runtime, guild_ids=[guild.id], user_id=user_id)
        existing = await repository.get_listing(session, guild.id)

    if existing is not None and existing.status in ListingStatus.LIVE:
        from bot.views.management import manage_button  # local import: management imports this module

        manage = manage_button(bot, guild.id, guild.name)
        manage.item.label = "Manage Existing Ad"
        view = persistent_view(manage)
        message = (
            f"## Server Ad Already Exists\n"
            f"**{guild.name}** already has a Parley ad. It is the **same listing everywhere**, whether you open it "
            "from this server or the main Parley server.\n\n"
            "Use **Manage Existing Ad** to edit, relist, view, or remove it. The same cooldowns apply everywhere."
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(content=message, embeds=[], view=view)
        else:
            await reply(interaction, message, view=view)
        return

    draft = ListingDraft(contacts=[user_id])
    if existing is not None:  # a removed/expired listing: pre-fill so re-posting is quick
        draft.categories = [c for c in existing.categories if c in bot.runtime.listings.categories]
        draft.accepting = existing.accepting_partnerships
        draft.minimum = existing.minimum_members
        draft.ad_text = existing.advertisement_text
        draft.invite_url = existing.invite_url

    form = ListingFormView(bot, user_id, guild.id, guild.name, draft, mode="create", in_dm=interaction.guild is None)
    if edit_message or interaction.response.is_done():
        await interaction.edit_original_response(content=form.render(), embeds=[], view=form)
    else:
        await reply(interaction, form.render(), view=form)


# ---------------------------------------------------------------- the form


async def resolve_verified_invite(bot: ParleyBot, guild_id: int, raw: str | None) -> str | None:
    """Validate a pasted invite for a server Parley is not in.

    Parley cannot create an invite there, and cannot inspect the channel, so the
    one thing that matters is checked: the invite really points at the server
    the user verified.
    """
    raw = (raw or "").strip()
    if not raw:
        raise InviteMissing("Paste an invite link for this server.")
    code = listing_service.parse_invite_code(raw)
    try:
        invite = await bot.fetch_invite(code, with_counts=False)
    except discord.NotFound as exc:
        raise ValidationError("That invite link is invalid or has expired.") from exc
    except discord.HTTPException as exc:
        raise ValidationError("Discord couldn't check that invite. Try again in a moment.") from exc
    if invite.guild is None or invite.guild.id != guild_id:
        raise ValidationError("That invite link points to a different server.")
    return listing_service.canonical_invite(invite.code)


class PasteInviteView(OwnedView):
    """Botless listings need a pasted invite: Parley can't make one."""

    def __init__(self, form: ListingFormView, *, next_step: str = "preview") -> None:
        super().__init__(form.owner_id)
        self.form = form
        self.next_step = next_step  # "preview" or "self_post"
        paste = discord.ui.Button(label="Paste Invite", emoji="🔗", style=discord.ButtonStyle.primary, row=0)
        paste.callback = self._paste  # type: ignore[method-assign]
        self.add_item(paste)
        back = discord.ui.Button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back  # type: ignore[method-assign]
        self.add_item(back)

    def render(self, notice: str | None = None) -> str:
        text = (
            f"## Invite for {self.form.guild_name}\n"
            "Paste an invite link for this server so people can join it.\n"
            "-# Parley isn't in this server, so it can't create one for you."
        )
        return f"\u26a0\ufe0f {notice}\n\n{text}" if notice else text

    async def _paste(self, interaction: discord.Interaction) -> None:
        async def submitted(inter: discord.Interaction, raw: str) -> None:
            self.form.draft.invite_raw = raw
            self.form.draft.invite_url = None
            self.stop()
            if self.next_step == "self_post":
                await self.form.start_self_post(inter)
            else:
                await self.form.continue_to_preview(inter)

        await interaction.response.send_modal(InviteModal(on_submit=submitted))

    async def _back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await AdModeView(self.form).show(interaction)


class ListingFormView(OwnedView):
    """One screen: category, partnerships, minimum, contacts. Used for create and Edit Info."""

    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        guild_id: int,
        guild_name: str,
        draft: ListingDraft,
        *,
        mode: str,
        in_dm: bool,
        connected: bool = True,
        verified=None,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        # Set when Parley is not in the server: everything is backed by the
        # OAuth verification instead of a live guild.
        self.verified = verified
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.draft = draft
        self.mode = mode
        self.in_dm = in_dm
        self.connected = connected
        self.invite_changed = False
        self._build()

    # ---- rendering

    def render(self, notice: str | None = None) -> str:
        if self.mode == "create":
            lines = [
                f"## {self.guild_name}",
                "Choose your category and partnership settings, then press **Continue**.",
            ]
        else:
            lines = [
                f"## Edit {self.guild_name}",
                "Change what you need, then press **Save**.",
            ]
            if self.draft.accepting:
                if self.connected:
                    lines.append("-# Partnership requests go to the people selected below.")
                else:
                    lines.append("-# Contacts stay unchanged while Parley is disconnected.")
        if notice:
            lines.insert(0, f"⚠️ {notice}\n")
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        rules = self.bot.runtime.listings

        category = discord.ui.Select(
            placeholder="Choose category",
            min_values=1,
            max_values=min(rules.max_categories, len(rules.categories)),
            options=[discord.SelectOption(label=c, value=c, default=c in self.draft.categories) for c in rules.categories],
            row=0,
        )
        category.callback = self._on_category  # type: ignore[method-assign]
        self._category = category
        self.add_item(category)

        partnerships = discord.ui.Select(
            placeholder="Partnerships",
            options=[
                discord.SelectOption(label="Open to partnerships", value="yes", default=self.draft.accepting),
                discord.SelectOption(label="Not looking for partnerships", value="no", default=not self.draft.accepting),
            ],
            row=1,
        )
        partnerships.callback = self._on_partnerships  # type: ignore[method-assign]
        self._partnerships = partnerships
        self.add_item(partnerships)

        # Minimum size and contacts only matter when partnerships are open.
        if self.draft.accepting:
            minimum = discord.ui.Select(
                placeholder="Minimum server size",
                options=[
                    discord.SelectOption(
                        label=("Any server size" if v <= 0 else f"{v:,}+ members"),
                        value=str(v),
                        default=v == self.draft.minimum,
                    )
                    for v in rules.minimum_member_options
                ],
                row=2,
            )
            minimum.callback = self._on_minimum  # type: ignore[method-assign]
            self._minimum = minimum
            self.add_item(minimum)

            # On first-time setup the creator is already the default contact. Keeping
            # that advanced choice out of the wizard makes the screen much easier to
            # understand; it remains editable later under Edit Server Info.
            if self.mode != "create" and self.connected:
                contacts = discord.ui.UserSelect(
                    placeholder="Partnership contacts",
                    min_values=1,
                    max_values=rules.max_contacts,
                    default_values=[discord.Object(id=uid) for uid in self.draft.contacts[: rules.max_contacts]],
                    row=3,
                )
                contacts.callback = self._on_contacts  # type: ignore[method-assign]
                self._contacts = contacts
                self.add_item(contacts)

        if self.mode == "create":
            nxt = discord.ui.Button(label="Continue", style=discord.ButtonStyle.primary, row=4)
            nxt.callback = self._next  # type: ignore[method-assign]
            self.add_item(nxt)
        else:
            save = discord.ui.Button(label="Save", style=discord.ButtonStyle.primary, row=4)
            invite = discord.ui.Button(label="Change Invite", emoji="🔗", style=discord.ButtonStyle.primary, row=4)
            save.callback = self._save_info  # type: ignore[method-assign]
            invite.callback = self._open_invite_modal  # type: ignore[method-assign]
            self.add_item(save)
            self.add_item(invite)
            back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=4)
            back.callback = self._cancel  # type: ignore[method-assign]
            self.add_item(back)

    async def _rerender(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._build()
        await interaction.response.edit_message(content=self.render(notice), view=self)

    # ---- simple field callbacks

    async def _on_category(self, interaction: discord.Interaction) -> None:
        self.draft.categories = list(self._category.values)
        await self._rerender(interaction)

    async def _on_partnerships(self, interaction: discord.Interaction) -> None:
        self.draft.accepting = self._partnerships.values[0] == "yes"
        if not self.draft.accepting:
            self.draft.minimum = 0
        await self._rerender(interaction)

    async def _on_minimum(self, interaction: discord.Interaction) -> None:
        self.draft.minimum = int(self._minimum.values[0])
        await self._rerender(interaction)

    async def _on_contacts(self, interaction: discord.Interaction) -> None:
        chosen = [u for u in self._contacts.values if not u.bot]
        notice = "Bots can't be partnership contacts." if len(chosen) != len(self._contacts.values) else None
        self.draft.contacts = [u.id for u in chosen] or [self.owner_id]
        await self._rerender(interaction, notice)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=None)

    # ---- create

    async def _next(self, interaction: discord.Interaction) -> None:
        if not self.draft.categories:
            await self._rerender(interaction, "Please choose a category first.")
            return
        await AdModeView(self).show(interaction)

    async def open_ad_modal(self, interaction: discord.Interaction) -> None:
        rules = self.bot.runtime.listings
        await interaction.response.send_modal(
            AdvertisementModal(
                title=f"Advertisement · {self.guild_name}",
                max_length=rules.max_ad_length,
                on_submit=self._submit_create,
                default_text=self.draft.ad_text,
                ask_invite=True,
                invite_required=rules.invite_required and not rules.auto_create_invite,
                default_invite=self.draft.invite_url,
            )
        )

    async def _submit_create(self, interaction: discord.Interaction, ad_text: str, invite_raw: str) -> None:
        self.draft.ad_text = ad_text  # kept so a retry is pre-filled
        self.draft.builder_used = False
        self.draft.invite_raw = invite_raw
        await self.continue_to_preview(interaction)

    async def continue_to_preview(self, interaction: discord.Interaction) -> None:
        """Check the ad, make sure there is an invite, then show the preview."""
        try:
            listing_service.clean_advertisement(self.draft.ad_text, self.bot.runtime)
        except ParleyError as exc:
            await AdModeView(self).show(interaction, exc.user_message)
            return
        if self.verified is not None:
            await self._continue_to_preview_verified(interaction)
            return
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            await self._rerender(interaction, "Parley is no longer in that server.")
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        if not self.draft.invite_url:
            try:
                self.draft.invite_url = await resolve_invite(self.bot, guild, self.draft.invite_raw)
            except InviteMissing:
                view = InviteHelpView(self, guild)
                await interaction.edit_original_response(content=view.render(), view=view)
                return
            except ParleyError as exc:
                await AdModeView(self).show(interaction, exc.user_message)
                return
        await show_preview(interaction, self, edit_original=True)

    async def _verified_authorize(self, bot: ParleyBot, guild_id: int, user_id: int) -> None:
        """Authority for a server Parley isn't in: Discord's own OAuth answer."""
        async with bot.db.session() as session:
            await verification.require_verified_guild(
                session, user_id=user_id, guild_id=guild_id, now=utcnow()
            )

    async def _continue_to_preview_verified(self, interaction: discord.Interaction) -> None:
        """Botless: the invite must be pasted, and must point at the verified server."""
        if not interaction.response.is_done():
            await interaction.response.defer()
        if not self.draft.invite_url:
            try:
                self.draft.invite_url = await resolve_verified_invite(
                    self.bot, self.guild_id, self.draft.invite_raw
                )
            except InviteMissing:
                view = PasteInviteView(self)
                await interaction.edit_original_response(content=view.render(), view=view)
                return
            except ParleyError as exc:
                view = PasteInviteView(self)
                await interaction.edit_original_response(content=view.render(exc.user_message), view=view)
                return
        await show_preview(interaction, self, edit_original=True)

    async def start_self_post(self, interaction: discord.Interaction, *, invite_ready: bool = False) -> None:
        """Let the owner post the real advertisement message in #server-directory.

        The posting window lives in Parley's own directory channel, so Parley does
        not need to be installed in the server being listed.
        """
        botless = self.verified is not None
        guild = None if botless else self.bot.get_guild(self.guild_id)
        if not botless and guild is None:
            await self._rerender(interaction, "Parley is no longer in that server.")
            return
        guild_name = self.guild_name if botless else guild.name
        authorize = self._verified_authorize if botless else None

        if not interaction.response.is_done():
            await interaction.response.defer()

        if not invite_ready and not self.draft.invite_url:
            try:
                if botless:
                    # Parley cannot create an invite there: it must be pasted and
                    # must point at the verified server.
                    self.draft.invite_url = await resolve_verified_invite(
                        self.bot, self.guild_id, self.draft.invite_raw
                    )
                else:
                    self.draft.invite_url = await resolve_invite(self.bot, guild, self.draft.invite_raw)
            except InviteMissing:
                view = PasteInviteView(self, next_step="self_post") if botless else InviteHelpView(self, guild, next_step="self_post")
                await interaction.edit_original_response(content=view.render(), view=view)
                return
            except ParleyError as exc:
                if botless:
                    view = PasteInviteView(self, next_step="self_post")
                    await interaction.edit_original_response(content=view.render(exc.user_message), view=view)
                    return
                await interaction.edit_original_response(
                    content=f"## Can't open posting window\n{exc.user_message}",
                    view=persistent_view(home_button(self.bot)),
                )
                return

        async def accepted(text: str, message: discord.Message) -> None:
            self.draft.ad_text = text
            listing = await self._create(interaction)
            if listing.status != ListingStatus.ACTIVE:
                raise ValidationError("This ad needs review before it can be published.")
            await self.bot.panels.adopt_self_post(self.guild_id, message)
            test = self.bot.runtime.hub.mode == "test"
            await self.bot.log_event(
                f"**{guild_name}** (`{self.guild_id}`) was listed by {interaction.user.mention}{' [TEST]' if test else ''}."
            )

        async def held_for_review(text: str) -> None:
            self.draft.ad_text = text
            listing = await self._create(interaction)
            await send_for_review(self.bot, listing, guild_name)

        try:
            note = await self_post.run_submission(
                interaction,
                self.guild_id,
                guild_name,
                on_accept=accepted,
                on_review=held_for_review,
                authorize=authorize,
            )
        except ParleyError as exc:
            await interaction.edit_original_response(
                content=f"## Can't open posting window\n{exc.user_message}",
                view=persistent_view(home_button(self.bot)),
            )
            return
        except Exception:
            log.exception("listing.self_post_create_failed guild_id=%s", self.guild_id)
            await interaction.edit_original_response(
                content=f"## Something went wrong\n{GENERIC_ERROR}",
                view=persistent_view(home_button(self.bot)),
            )
            return

        self.stop()
        # One obvious place to fix/relist the new server, plus Home.
        await interaction.edit_original_response(
            content=note,
            view=persistent_view(action_button(self.bot, "servers"), home_button(self.bot)),
        )

    async def publish(self, interaction: discord.Interaction) -> None:
        """Create the listing and post it. Only reports success after Discord confirms."""
        await interaction.response.edit_message(content="Publishing your listing…", view=None)
        try:
            result = await self._create_and_publish(interaction)
        except ParleyError as exc:
            self._build()
            await interaction.edit_original_response(content=self.render(exc.user_message), view=self)
            return
        except Exception:
            log.exception("listing.create_failed guild_id=%s", self.guild_id)
            self._build()
            await interaction.edit_original_response(content=self.render(GENERIC_ERROR), view=self)
            return
        self.stop()
        await interaction.edit_original_response(content=result, view=persistent_view(home_button(self.bot)))

    async def create_only(self, interaction: discord.Interaction):
        """Save the listing without publishing it (used by direct-channel posting)."""
        return await self._create(interaction)

    async def _create(self, interaction: discord.Interaction):
        bot = self.bot
        test = bot.runtime.hub.mode == "test"

        if self.verified is not None:
            # Botless: Discord vouched for this guild, and only the verifying
            # manager can be a contact (no member list to check anyone else against).
            async with bot.db.session() as session:
                verified = await verification.require_verified_guild(
                    session, user_id=interaction.user.id, guild_id=self.guild_id, now=utcnow()
                )
            info = verified_guild_info(verified)
            contacts = [interaction.user.id]
        else:
            guild, _member = await permissions.require_manager(bot, self.guild_id, interaction.user.id)
            contacts = await validate_contacts(guild, self.draft.contacts)
            info = guild_info(guild)

        async with bot.db.session() as session:
            listing = await listing_service.create_listing(
                session,
                bot.runtime,
                guild=info,
                actor_id=interaction.user.id,
                data=listing_service.ListingInput(
                    categories=self.draft.categories,
                    accepting_partnerships=self.draft.accepting,
                    minimum_members=self.draft.minimum,
                    contact_ids=contacts,
                    advertisement_text=self.draft.ad_text,
                    invite_url=self.draft.invite_url,
                ),
                now=utcnow(),
                is_test=test,
            )
        return listing

    async def _create_and_publish(self, interaction: discord.Interaction) -> str:
        bot = self.bot
        listing = await self._create(interaction)
        guild = bot.get_guild(self.guild_id)
        name = guild.name if guild else self.guild_name
        test = bot.runtime.hub.mode == "test"
        if listing.status == ListingStatus.PENDING:
            await send_for_review(bot, listing, name)
            return templates.render(bot.runtime, "listing_pending", server_name=name)

        message = await bot.panels.publish_listing(listing.guild_id)
        await bot.log_event(
            f"📢 **{name}** (`{self.guild_id}`) was listed by {interaction.user.mention}{' 🧪' if test else ''}."
        )
        if message is None:
            return templates.render(bot.runtime, "listing_saved_unpublished", server_name=name)
        return templates.render(bot.runtime, "listing_created", server_name=name, jump_url=message.jump_url)

    # ---- edit info

    async def _open_invite_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(InviteModal(on_submit=self._submit_invite, default_invite=self.draft.invite_url))

    async def _submit_invite(self, interaction: discord.Interaction, raw: str) -> None:
        guild = self.bot.get_guild(self.guild_id)
        try:
            if guild is not None:
                self.draft.invite_url = await resolve_invite(self.bot, guild, raw)
            else:
                code = listing_service.parse_invite_code(raw)
                try:
                    invite = await self.bot.fetch_invite(code, with_counts=False)
                except discord.NotFound as exc:
                    raise ValidationError("That invite link is invalid or has expired.") from exc
                if invite.guild is None or invite.guild.id != self.guild_id:
                    raise ValidationError("That invite link points to a different server.")
                self.draft.invite_url = listing_service.canonical_invite(invite.code)
        except ParleyError as exc:
            await self._rerender(interaction, exc.user_message)
            return
        self.invite_changed = True
        await self._rerender(interaction)

    async def _save_info(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        bot = self.bot
        try:
            guild = bot.get_guild(self.guild_id)
            if guild is not None:
                await permissions.require_manager(bot, self.guild_id, interaction.user.id)
                contacts = await validate_contacts(guild, self.draft.contacts)
            else:
                async with bot.db.session() as session:
                    stored = await repository.get_guild(session, self.guild_id)
                    if stored is None or stored.connected_by != interaction.user.id:
                        raise PermissionDenied(
                            "Only the manager who originally verified this listing can manage it while Parley is disconnected."
                        )
                    contacts = await repository.get_contact_ids(session, self.guild_id)
            async with bot.db.session() as session:
                listing = await listing_service.update_info(
                    session,
                    bot.runtime,
                    guild_id=self.guild_id,
                    categories=self.draft.categories,
                    accepting_partnerships=self.draft.accepting,
                    minimum_members=self.draft.minimum,
                    contact_ids=contacts,
                    invite_url=self.draft.invite_url,
                    actor_id=interaction.user.id,
                    now=utcnow(),
                )
        except ParleyError as exc:
            self._build()
            await interaction.edit_original_response(content=self.render(exc.user_message), view=self)
            return
        notice = await after_edit(bot, listing, self.guild_name)
        self.stop()
        await interaction.edit_original_response(
            content=notice or f"✅ Saved changes for **{self.guild_name}**.",
            view=persistent_view(manage_button_for(bot, self.guild_id, self.guild_name), home_button(bot)),
        )


def manage_button_for(bot: ParleyBot, guild_id: int, name: str):
    from bot.views.management import manage_button  # local import: management imports this module

    return manage_button(bot, guild_id, name)


async def after_edit(bot: ParleyBot, listing: Listing, guild_name: str) -> str | None:
    """Route an edited listing: new review if it waits for staff, otherwise update the live message."""
    if listing.awaiting_review:
        await send_for_review(bot, listing, guild_name)
        if listing.status == ListingStatus.PENDING:
            return templates.render(bot.runtime, "listing_pending", server_name=guild_name)
        return templates.render(bot.runtime, "edit_pending", server_name=guild_name)
    if listing.status == ListingStatus.ACTIVE:
        await bot.panels.update_listing_message(listing.guild_id)
    return None


# ---------------------------------------------------------------- invite fallback


class InviteMissing(ValidationError):
    pass


class InviteHelpView(OwnedView):
    """Shown when Parley couldn't make an invite by itself: pick a channel or paste one."""

    def __init__(self, form: ListingFormView, guild: discord.Guild, *, next_step: str = "preview") -> None:
        super().__init__(form.owner_id)
        self.form = form
        self.guild = guild
        self.next_step = next_step
        channels = [c for c in guild.text_channels if isinstance(c, discord.TextChannel)][:25]
        if channels:
            select = discord.ui.Select(
                placeholder="Choose Invite Channel",
                options=[discord.SelectOption(label=f"#{truncate(c.name, 90)}", value=str(c.id)) for c in channels],
                row=0,
            )
            select.callback = self._picked  # type: ignore[method-assign]
            self._select = select
            self.add_item(select)
        paste = discord.ui.Button(label="Paste Invite", emoji="🔗", style=discord.ButtonStyle.primary, row=1)
        paste.callback = self._paste  # type: ignore[method-assign]
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back  # type: ignore[method-assign]
        self.add_item(paste)
        self.add_item(back)

    def render(self, notice: str | None = None) -> str:
        text = (
            f"## Invite for {self.guild.name}\n"
            "Parley couldn't create an invite automatically.\n"
            "**Choose a channel** people should land in, or **paste** an invite link."
        )
        return f"⚠️ {notice}\n\n{text}" if notice else text

    async def _picked(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        channel = self.guild.get_channel(int(self._select.values[0]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.edit_original_response(content=self.render("That channel no longer exists."), view=self)
            return
        if not channel.permissions_for(self.guild.me).create_instant_invite:
            await interaction.edit_original_response(
                content=self.render(f"Parley needs **Create Invite** in #{channel.name}. Give it that permission or paste an invite."),
                view=self,
            )
            return
        try:
            invite = await channel.create_invite(max_age=0, max_uses=0, unique=False, reason="Parley listing invite")
        except discord.HTTPException as exc:
            log.info("Invite creation failed guild_id=%s channel=%s: %s", self.guild.id, channel.id, exc)
            await interaction.edit_original_response(content=self.render("Discord didn't allow that invite. Try another channel."), view=self)
            return
        self.form.draft.invite_url = listing_service.canonical_invite(invite.code)
        self.stop()
        await self._continue(interaction)

    async def _paste(self, interaction: discord.Interaction) -> None:
        async def submitted(inter: discord.Interaction, raw: str) -> None:
            await acknowledge(inter)
            try:
                if not raw.strip():
                    raise ValidationError("Please paste an invite link.")
                self.form.draft.invite_url = await resolve_invite(self.form.bot, self.guild, raw)
            except ParleyError as exc:
                await inter.edit_original_response(content=self.render(exc.user_message), view=self)
                return
            self.stop()
            await self._continue(inter)

        await interaction.response.send_modal(InviteModal(on_submit=submitted))

    async def _continue(self, interaction: discord.Interaction) -> None:
        if self.next_step == "self_post":
            await self.form.start_self_post(interaction, invite_ready=True)
        else:
            await self.form.continue_to_preview(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await AdModeView(self.form).show(interaction)


# ---------------------------------------------------------------- preview


PREVIEW_HEADER = "-# 🔍 Preview · this is exactly how your ad will look.\n"


def preview_content(ad_text: str) -> str:
    return (PREVIEW_HEADER + ad_text)[:2000]


class AdModeView(OwnedView):
    """Step 2: paste your own ad, or let Parley build one."""

    def __init__(self, form: ListingFormView) -> None:
        super().__init__(form.owner_id)
        self.form = form
        paste = discord.ui.Button(label="Paste My Own Ad", style=discord.ButtonStyle.primary, row=0)
        builder = discord.ui.Button(label="Simple Ad Builder", style=discord.ButtonStyle.primary, row=0)
        paste.callback = self._paste  # type: ignore[method-assign]
        builder.callback = self._builder  # type: ignore[method-assign]
        self.add_item(paste)
        self.add_item(builder)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back  # type: ignore[method-assign]
        self.add_item(back)

    def render(self, notice: str | None = None) -> str:
        text = "## Your advertisement\nHow do you want to post it?"
        return f"{notice}\n\n{text}" if notice else text

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=self.render(notice), view=self)
        else:
            await interaction.response.edit_message(content=self.render(notice), view=self)

    async def _paste(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.form.start_self_post(interaction)

    async def _builder(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BuilderModal(self.form))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.form._rerender(interaction)


class BuilderModal(discord.ui.Modal):
    """Only asks for what Parley doesn't already know."""

    def __init__(self, form: ListingFormView) -> None:
        super().__init__(title="Simple Ad Builder", timeout=900)
        self.form = form
        self.description = discord.ui.TextInput(
            label="What is your server about?",
            placeholder="Active gaming community looking for new members and partnerships.",
            style=discord.TextStyle.paragraph,
            max_length=400,
            required=True,
        )
        self.extra = discord.ui.TextInput(
            label="Anything else? (optional)",
            placeholder="Weekly events, friendly staff…",
            style=discord.TextStyle.paragraph,
            max_length=600,
            required=False,
        )
        self.add_item(self.description)
        self.add_item(self.extra)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        form = self.form
        guild = form.bot.get_guild(form.guild_id)
        form.draft.ad_text = listing_service.build_simple_ad(
            name=guild.name if guild else form.guild_name,
            member_count=guild.member_count or 0 if guild else 0,
            categories=form.draft.categories,
            accepting=form.draft.accepting,
            minimum=form.draft.minimum,
            invite_url=form.draft.invite_url,
            description=self.description.value,
            extra=self.extra.value,
        )
        form.draft.builder_used = True
        await form.continue_to_preview(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_error(interaction, error)


class PreviewView(OwnedView):
    """Step 3: only the ad and three buttons."""

    def __init__(self, form: ListingFormView) -> None:
        super().__init__(form.owner_id)
        self.form = form
        publish = discord.ui.Button(label="Publish", style=discord.ButtonStyle.primary, row=0)
        edit = discord.ui.Button(label="Edit", style=discord.ButtonStyle.primary, row=0)
        publish.callback = self._publish  # type: ignore[method-assign]
        edit.callback = self._edit  # type: ignore[method-assign]
        self.add_item(publish)
        self.add_item(edit)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back  # type: ignore[method-assign]
        self.add_item(back)

    async def _publish(self, interaction: discord.Interaction) -> None:
        warning = unusable_custom_emoji(self.form.bot, self.form.draft.ad_text)
        if warning and not self.form.draft.emoji_warning_seen:
            self.form.draft.emoji_warning_seen = True
            self.stop()
            await EmojiWarningView(self.form, warning).show(interaction)
            return
        self.stop()
        await self.form.publish(interaction)

    async def _edit(self, interaction: discord.Interaction) -> None:
        if self.form.draft.builder_used:
            await interaction.response.send_modal(BuilderModal(self.form))
            return
        await self.form.open_ad_modal(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await AdModeView(self.form).show(interaction)


def unusable_custom_emoji(bot: ParleyBot, text: str) -> list[int]:
    """Custom emoji from other servers that Parley can't render when it reposts the ad."""
    return [eid for eid in listing_service.custom_emoji_ids(text) if bot.get_emoji(eid) is None]


class EmojiWarningView(OwnedView):
    """Rare builder edge case: warn only when Parley itself cannot render an emoji."""

    def __init__(self, form: ListingFormView, missing: list[int]) -> None:
        super().__init__(form.owner_id)
        self.form = form
        self.missing = missing
        cont = discord.ui.Button(label="Continue", style=discord.ButtonStyle.primary, row=0)
        edit = discord.ui.Button(label="Edit", style=discord.ButtonStyle.primary, row=0)
        cont.callback = self._continue  # type: ignore[method-assign]
        edit.callback = self._edit  # type: ignore[method-assign]
        self.add_item(cont)
        self.add_item(edit)

    def render(self) -> str:
        return "## Some emoji may not show\nA custom emoji in this generated ad may not work when Parley posts it."

    async def show(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=self.render(), view=self)
        else:
            await interaction.response.edit_message(content=self.render(), view=self)

    async def _continue(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.form.publish(interaction)

    async def _edit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.send_modal(BuilderModal(self.form))


async def show_preview(interaction: discord.Interaction, form: ListingFormView, *, edit_original: bool = False) -> None:
    view = PreviewView(form)
    content = preview_content(form.draft.ad_text)
    if edit_original or interaction.response.is_done():
        await interaction.edit_original_response(content=content, view=view, allowed_mentions=safe_allowed_mentions())
    else:
        await interaction.response.edit_message(content=content, view=view, allowed_mentions=safe_allowed_mentions())


# ---------------------------------------------------------------- staff review (only when approval is required)


class ReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"wp:review:(?P<action>approve|reject|ban):(?P<gid>\d+)(?::(?P<rev>\d+))?",
):
    LABELS = {
        "approve": ("Approve", "✅", discord.ButtonStyle.success),
        "reject": ("Reject", "❌", discord.ButtonStyle.danger),
        "ban": ("Ban Server", "🚫", discord.ButtonStyle.danger),
    }

    def __init__(self, guild_id: int, action: str, revision: int | None = None) -> None:
        label, emoji, style = self.LABELS[action]
        suffix = f":{revision}" if revision is not None and action != "ban" else ""
        super().__init__(
            discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"wp:review:{action}:{guild_id}{suffix}")
        )
        self.guild_id = guild_id
        self.action = action
        self.revision = revision

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        rev = match["rev"]
        return cls(int(match["gid"]), match["action"], int(rev) if rev is not None else None)

    async def callback(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        if not await guard(interaction):
            return
        try:
            if self.action == "ban":
                await ban_from_review(interaction, self.guild_id)
            else:
                await review(interaction, self.guild_id, self.action == "approve", self.revision)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)


def _info_changes(listing: Listing) -> list[str]:
    changes = listing.pending_changes or {}
    lines = []
    labels = {"category": "Category", "accepting_partnerships": "Partnerships", "minimum_members": "Minimum", "invite_url": "Invite"}
    for key, label in labels.items():
        if key in changes:
            before, after = getattr(listing, key), changes[key]
            if key == "accepting_partnerships":
                before, after = ("Yes" if before else "No"), ("Yes" if after else "No")
            lines.append(f"**{label}:** {before} → {after}")
    return lines


def review_payload(bot: ParleyBot, listing: Listing, guild_name: str, member_count: int | None) -> dict:
    """The review message: the ad exactly as it would be posted, plus a staff summary."""
    edit = listing.status != ListingStatus.PENDING
    pending = listing.pending_changes or {}
    ad_text = pending.get("advertisement_text", listing.advertisement_text)
    category = pending.get("category", listing.category)
    header = discord.Embed(
        title=("✏️ EDIT REVIEW" if edit else "📝 NEW LISTING") + f" · {truncate(guild_name, 150)}",
        color=bot.runtime.bot.color_warning,
    )
    header.add_field(name="Server", value=f"{truncate(guild_name, 100)}\n`{listing.guild_id}`")
    header.add_field(name="Members", value=f"{member_count or 0:,}")
    header.add_field(name="Category", value=category.replace(",", ", ") or "—")
    if listing.pending_submitted_by:
        header.add_field(name="Submitted by", value=f"<@{listing.pending_submitted_by}>", inline=False)
    if listing.is_test:
        header.add_field(name="Test", value="🧪 Created in TEST mode", inline=False)
    if edit:
        changed = _info_changes(listing)
        if "advertisement_text" in pending:
            changed.insert(0, "**Advertisement text** changed (new version above, current live version below)")
        header.description = "\n".join(changed) or "No visible changes."
    header.set_footer(text=f"Review #{listing.review_revision} · the advertisement is shown above this box")
    embeds = [header]
    if edit and "advertisement_text" in pending:
        embeds.append(
            discord.Embed(title="Currently live", description=truncate(listing.advertisement_text, 4000), color=0x99AAB5)
        )
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewButton(listing.guild_id, "approve", listing.review_revision))
    view.add_item(ReviewButton(listing.guild_id, "reject", listing.review_revision))
    invite = pending.get("invite_url", listing.invite_url)
    if invite:
        view.add_item(discord.ui.Button(label="View Server", emoji="👁", url=invite))
    view.add_item(ReviewButton(listing.guild_id, "ban"))
    return {"content": ad_text[:2000], "embeds": embeds, "view": view, "allowed_mentions": safe_allowed_mentions()}


async def send_for_review(bot: ParleyBot, listing: Listing, guild_name: str) -> bool:
    """Post a review in the staff log. A newer submission replaces (outdates) the previous review."""
    channel = bot.log_channel()
    if channel is None:
        log.warning("listing.review_unrouted guild_id=%s (no staff log channel set)", listing.guild_id)
        return False
    async with bot.db.session() as session:
        stored = await repository.get_guild(session, listing.guild_id)
    message = await channel.send(**review_payload(bot, listing, guild_name, stored.member_count if stored else None))
    async with bot.db.session() as session:
        old_channel, old_message = await listing_service.record_review_message(
            session, guild_id=listing.guild_id, channel_id=channel.id, message_id=message.id
        )
    if old_message and old_message != message.id:
        await mark_review_closed(bot, old_channel, old_message, "⏭️ Replaced by a newer submission (see below).")
    return True


async def mark_review_closed(bot: ParleyBot, channel_id: int | None, message_id: int | None, status: str) -> None:
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None or not message_id or not hasattr(channel, "get_partial_message"):
        return
    try:
        message = await channel.fetch_message(message_id)
        embeds = [e.copy() for e in message.embeds] or [discord.Embed()]
        embeds[0].add_field(name="Status", value=status, inline=False)
        await message.edit(embeds=embeds, view=None)
    except discord.HTTPException as exc:
        log.info("Could not update review message %s: %s", message_id, exc)


async def review(interaction: discord.Interaction, guild_id: int, approve: bool, revision: int | None) -> None:
    bot = get_bot(interaction)
    if not await permissions.is_staff(bot, interaction.user.id):
        raise PermissionDenied("Only Parley staff can review listings.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.db.session() as session:
        listing, kind = await listing_service.review_listing(
            session, guild_id=guild_id, approve=approve, moderator_id=interaction.user.id, now=utcnow(), revision=revision
        )
        contacts = await repository.get_contact_ids(session, guild_id)
        stored = await repository.get_guild(session, guild_id)
    name = stored.name if stored else str(guild_id)

    if approve and kind == "new":
        await bot.panels.publish_listing(guild_id)
    elif approve and kind == "edit":
        await bot.panels.update_listing_message(guild_id)
    key = {("new", True): "listing_approved", ("new", False): "listing_rejected",
           ("edit", True): "edit_approved", ("edit", False): "edit_rejected"}[(kind, approve)]
    await deliver_dms(bot, contacts, actor_id=interaction.user.id, content=templates.render(bot.runtime, key, server_name=name))

    verdict = f"{'✅ Approved' if approve else '❌ Rejected'} by {interaction.user.mention}"
    if interaction.message is not None:
        await mark_review_closed(bot, interaction.message.channel.id, interaction.message.id, verdict)
    await bot.log_event(f"🛡️ {'Edit to' if kind == 'edit' else 'Listing'} **{name}** {verdict.lower()}.")
    await reply(interaction, f"{verdict.split(' by ')[0]}: **{name}**.")


async def ban_from_review(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    if not await permissions.is_staff(bot, interaction.user.id):
        raise PermissionDenied("Only Parley staff can do that.")
    if guild_id == bot.runtime.hub.main_guild_id:
        raise ValidationError("You can't ban the main Parley server.")
    confirm = ConfirmView(interaction.user.id, confirm_label="Ban Server")
    await reply(interaction, "Ban this server from Parley? Its listing is removed and it can't list again.", view=confirm)
    await confirm.wait()
    if not confirm.confirmed or confirm.interaction is None:
        return
    await confirm.interaction.response.defer()
    async with bot.db.session() as session:
        listing = await moderation.ban_guild(session, guild_id=guild_id, reason="Banned from review", moderator_id=interaction.user.id)
    await bot.panels.take_down_listing(listing)
    if interaction.message is not None:
        await mark_review_closed(bot, interaction.message.channel.id, interaction.message.id, f"🚫 Server banned by {interaction.user.mention}")
    await bot.log_event(f"🛡️ Server `{guild_id}` banned from a review ({interaction.user.mention}).")
    await confirm.interaction.edit_original_response(content="🚫 Server banned.", view=None)
