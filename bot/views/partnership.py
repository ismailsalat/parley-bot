"""Find partners, request partnerships, answer requests, looking-for-partner posts."""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

import discord
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database import repository
from bot.database.models import Guild, Listing, ListingStatus, PartnershipRequest
from bot.modals.partnership import ShortMessageModal
from bot.services import listings as listing_service
from bot.services import partnerships, permissions
from bot.services.errors import PermissionDenied, ValidationError
from bot.services.network import ANY
from bot.utils.helpers import format_members, format_minimum, listing_jump_url, truncate, utcnow
from bot.utils.mentions import advertisement_kwargs
from bot.config import templates
from bot.views.base import (
    MENU_TIMEOUT_SECONDS,
    OwnedView,
    deliver_dms,
    get_bot,
    guard,
    guild_info,
    handle_error,
    home_button,
    reply,
    user_label,
)
from bot.views.welcome import action_button, persistent_view, register_action

if TYPE_CHECKING:
    from bot.core import WaypointBot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- shared lookups


async def represented_listings(bot: WaypointBot, session: AsyncSession, user_id: int) -> list[Listing]:
    """Active listings the user may speak for: partnership contact or current manager."""
    contact_ids = set(await repository.guild_ids_where_contact(session, user_id))
    manager_ids = {guild.id for guild in permissions.cached_manageable_guilds(bot, user_id)}
    rows = await repository.get_listings(session, contact_ids | manager_ids)
    return [row for row in rows if row.status == ListingStatus.ACTIVE]


async def can_represent(bot: WaypointBot, session: AsyncSession, guild_id: int, user_id: int) -> bool:
    if await repository.is_contact(session, guild_id, user_id):
        return True
    return await permissions.is_manager(bot, guild_id, user_id)


def guild_name(guilds: dict[int, Guild], guild_id: int) -> str:
    row = guilds.get(guild_id)
    return row.name if row else f"Server {guild_id}"


def describe(guilds: dict[int, Guild], listing: Listing | None, guild_id: int) -> str:
    row = guilds.get(guild_id)
    categories = ", ".join(listing.categories) if listing else "Unknown"
    members = format_members(row.member_count if row else 0)
    return f"**{guild_name(guilds, guild_id)}**\n{categories} · {members}"


# ---------------------------------------------------------------- listing components


def listing_components(bot: WaypointBot, listing: Listing) -> discord.ui.View | None:
    """[Join Server] [Request Partnership] shown under an advertisement."""
    items: list[discord.ui.Item] = []
    if listing.invite_url:
        label, emoji = bot.runtime.button("join")
        items.append(discord.ui.Button(label=label, emoji=emoji, url=listing.invite_url))
    if listing.accepting_partnerships:
        items.append(request_button(bot, listing.guild_id))
    return persistent_view(*items) if items else None


def listing_message_kwargs(bot: WaypointBot, listing: Listing) -> dict:
    from bot.services.testmode import TEST_LISTING_NOTE

    kwargs = advertisement_kwargs(listing.advertisement_text)
    if listing.is_test and len(kwargs["content"]) + len(TEST_LISTING_NOTE) + 1 <= 2000:
        kwargs["content"] += "\n" + TEST_LISTING_NOTE
    view = listing_components(bot, listing)
    kwargs["view"] = view if view is not None else discord.utils.MISSING
    return kwargs


# ---------------------------------------------------------------- dynamic buttons


class RequestPartnershipButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wp:req:(?P<gid>\d+)"):
    def __init__(
        self,
        guild_id: int,
        *,
        label: str | None = None,
        emoji=None,
        style: discord.ButtonStyle = discord.ButtonStyle.primary,  # a normal action, not an approval
        row: int | None = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label or "Request Partnership",
                emoji=emoji,
                style=style,
                custom_id=f"wp:req:{guild_id}",
                row=row,
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(int(match["gid"]), label=item.label, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        try:
            await start_request_flow(interaction, self.guild_id)
        except Exception as exc:  # noqa: BLE001 - handled and logged
            await handle_error(interaction, exc)


class ViewAdButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wp:ad:(?P<gid>\d+)"):
    def __init__(self, guild_id: int, *, label: str | None = None, emoji=None, row: int | None = None) -> None:
        super().__init__(
            discord.ui.Button(
                label=label or "View Ad",
                emoji=emoji,
                style=discord.ButtonStyle.primary,  # a normal action, not navigation
                custom_id=f"wp:ad:{guild_id}",
                row=row,
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(int(match["gid"]), label=item.label, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        try:
            await show_ad(interaction, self.guild_id)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)


class RequestResponseButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"wp:pr:(?P<action>accept|decline):(?P<rid>\d+)"
):
    def __init__(self, request_id: int, accept: bool, *, label: str | None = None, row: int | None = None) -> None:
        super().__init__(
            discord.ui.Button(
                label=label or ("Accept" if accept else "Decline"),
                emoji="✅" if accept else "❌",
                style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
                custom_id=f"wp:pr:{'accept' if accept else 'decline'}:{request_id}",
                row=row,
            )
        )
        self.request_id = request_id
        self.accept = accept

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(int(match["rid"]), match["action"] == "accept", label=item.label)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        try:
            await respond_to_request(interaction, self.request_id, self.accept)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)


def request_button(bot: WaypointBot, guild_id: int, row: int | None = None) -> RequestPartnershipButton:
    label, emoji = bot.runtime.button("request")
    return RequestPartnershipButton(guild_id, label=label, emoji=emoji, row=row)


def view_ad_button(bot: WaypointBot, guild_id: int, *, label: str | None = None, row: int | None = None) -> ViewAdButton:
    default_label, emoji = bot.runtime.button("view_ad")
    return ViewAdButton(guild_id, label=(label or default_label)[:80], emoji=emoji, row=row)


# ---------------------------------------------------------------- view ad


async def show_ad(interaction: discord.Interaction, guild_id: int) -> None:
    """Open the latest real advertisement when it exists; otherwise show the stored text."""
    bot = get_bot(interaction)
    async with bot.db.session() as session:
        listing = listing_service.require_visible(await repository.get_listing(session, guild_id))
    url = listing_jump_url(bot.runtime.hub.main_guild_id, listing.channel_id, listing.message_id)
    if url:
        embed = discord.Embed(
            title="View Ad",
            description=f"[Open the latest ad in the server directory]({url})",
            color=bot.runtime.bot.color_primary,
        )
        await reply(interaction, embed=embed)
        return
    await reply(interaction, listing.advertisement_text, view=listing_components(bot, listing))


# ---------------------------------------------------------------- pickers


class GuildPickerView(OwnedView):
    """Choose one of several servers, then continue."""

    def __init__(
        self,
        owner_id: int,
        options: Sequence[tuple[int, str]],
        on_pick: Callable[[discord.Interaction, int], Awaitable[None]],
        *,
        placeholder: str = "Choose a server",
    ) -> None:
        super().__init__(owner_id)
        self._on_pick = on_pick
        select = discord.ui.Select(
            placeholder=placeholder,
            options=[discord.SelectOption(label=truncate(name, 100), value=str(gid)) for gid, name in options[:25]],
        )
        select.callback = self._picked  # type: ignore[method-assign]
        self._select = select
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        await self._on_pick(interaction, int(self._select.values[0]))


# ---------------------------------------------------------------- request flow


async def start_request_flow(interaction: discord.Interaction, target_id: int) -> None:
    bot = get_bot(interaction)
    async with bot.db.session() as session:
        target = listing_service.require_visible(await repository.get_listing(session, target_id))
        if not target.accepting_partnerships:
            raise ValidationError("This server is not accepting partnerships.")
        sources = [s for s in await represented_listings(bot, session, interaction.user.id) if s.guild_id != target_id]
        guilds = await repository.get_guilds(session, [target_id, *(s.guild_id for s in sources)])

    target_name = guild_name(guilds, target_id)
    if not sources:
        await reply(
            interaction,
            "To request a partnership, your server needs a Waypoint listing first, "
            "and you need **Manage Server** there or be one of its partnership contacts.",
            view=persistent_view(action_button(bot, "post")),
        )
        return

    def modal_for(source_id: int) -> ShortMessageModal:
        async def submit(inter: discord.Interaction, message: str) -> None:
            await submit_request(inter, source_id, target_id, message)

        return ShortMessageModal(
            title=f"Partner with {target_name}",
            label="Message (optional)",
            placeholder="We're interested in partnering!",
            max_length=bot.runtime.partnerships.max_message_length or 1,
            on_submit=submit,
        )

    if len(sources) == 1:
        await interaction.response.send_modal(modal_for(sources[0].guild_id))
        return

    async def picked(inter: discord.Interaction, source_id: int) -> None:
        await inter.response.send_modal(modal_for(source_id))

    options = [(s.guild_id, guild_name(guilds, s.guild_id)) for s in sources]
    await reply(
        interaction,
        f"Which of your servers wants to partner with **{target_name}**?",
        view=GuildPickerView(interaction.user.id, options, picked, placeholder="Your server"),
    )


def request_embed(
    bot: WaypointBot,
    request: PartnershipRequest,
    guilds: dict[int, Guild],
    source: Listing | None,
    target: Listing | None,
) -> discord.Embed:
    source_row = guilds.get(request.source_guild_id)
    intro = templates.render(
        bot.runtime,
        "request_received",
        requester_server=guild_name(guilds, request.source_guild_id),
        target_server=guild_name(guilds, request.target_guild_id),
        member_count=f"{source_row.member_count:,}" if source_row else "?",
        category=", ".join(source.categories) if source else "",
    )
    embed = discord.Embed(
        title="New Partnership Request",
        description=(
            f"{intro}\n\n{describe(guilds, source, request.source_guild_id)}\n→ "
            f"{describe(guilds, target, request.target_guild_id)}"
        ),
        color=bot.runtime.bot.color_primary,
    )
    if request.message:
        embed.add_field(name="Message", value=truncate(request.message, 1000), inline=False)
    embed.add_field(name="Requested by", value=user_label(bot, request.requester_user_id), inline=False)
    embed.set_footer(text=f"Request #{request.id}")
    return embed


def request_actions(bot: WaypointBot, request: PartnershipRequest) -> discord.ui.View:
    _label, emoji = bot.runtime.button("view_ad")
    return persistent_view(
        RequestResponseButton(request.id, True),
        RequestResponseButton(request.id, False),
        ViewAdButton(request.source_guild_id, label="View Ad", emoji=emoji),
    )


async def submit_request(
    interaction: discord.Interaction, source_id: int, target_id: int, message: str, finder=None
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    bot = get_bot(interaction)
    user_id = interaction.user.id
    source_guild = bot.get_guild(source_id)
    if source_guild is None:
        raise ValidationError("Waypoint is no longer in your server. Add it back before sending partnership requests.")
    permissions.require_public_bot_channel(source_guild)

    async with bot.db.session() as session:
        if not await can_represent(bot, session, source_id, user_id):
            raise PermissionDenied("You can only send requests for servers you manage or are a partnership contact for.")
        if source_guild is not None:
            info = guild_info(source_guild)
            await repository.upsert_guild(
                session, guild_id=info.guild_id, name=info.name, icon_url=info.icon_url, member_count=info.member_count
            )
        stored = await repository.get_guild(session, source_id)
        member_count = source_guild.member_count if source_guild else (stored.member_count if stored else 0)
        context = await partnerships.create_request(
            session,
            bot.runtime,
            source_guild_id=source_id,
            target_guild_id=target_id,
            requester_id=user_id,
            source_member_count=member_count or 0,
            message=message,
            now=utcnow(),
        )
        guilds = await repository.get_guilds(session, [source_id, target_id])
        contact_ids = await repository.get_contact_ids(session, target_id)

    delivered = held = 0
    if bot.runtime.partnerships.dm_notifications:
        embed = request_embed(bot, context.request, guilds, context.source, context.target)
        delivered, held = await deliver_dms(
            bot, contact_ids, actor_id=user_id, embed=embed, view=request_actions(bot, context.request)
        )
    text = templates.render(
        bot.runtime, "request_sent",
        requester_server=guild_name(guilds, source_id), target_server=guild_name(guilds, target_id),
    )
    if contact_ids and delivered == 0 and not held:
        log.warning("partnership.undelivered id=%s target=%s", context.request.id, target_id)
        text += "\n⚠️ Their contacts have DMs closed, so they'll only see it under **Requests** when they DM me."
    if held:
        text += f"\n-# 🧪 TEST mode: {held} non-staff contact(s) were not messaged; you got a copy instead."

    if finder is not None:
        # Keep discovering: the next server is one press away.
        finder.seen.add(target_id)
        view = discord.ui.View(timeout=MENU_TIMEOUT_SECONDS)
        nxt = discord.ui.Button(label="Next Server", style=discord.ButtonStyle.primary, row=0)

        async def continue_search(inter: discord.Interaction) -> None:
            await finder.show(inter)

        nxt.callback = continue_search  # type: ignore[method-assign]
        view.add_item(nxt)
        view.add_item(home_button(bot, row=1))
        await _edit_or_reply(interaction, "Request sent ✓", view)
        return
    await reply(interaction, text, view=persistent_view(home_button(bot)))


# ---------------------------------------------------------------- answering requests


def _is_single_request_notice(message: discord.Message | None, request_id: int) -> bool:
    if message is None or not message.embeds:
        return False
    footer = message.embeds[0].footer.text or ""
    return footer == f"Request #{request_id}"


async def respond_to_request(interaction: discord.Interaction, request_id: int, accept: bool) -> None:
    await interaction.response.defer(ephemeral=True, thinking=False)
    bot = get_bot(interaction)
    user_id = interaction.user.id

    async with bot.db.session() as session:
        existing = await repository.get_request(session, request_id)
        target_id = existing.target_guild_id if existing else None
        contact = bool(target_id) and await repository.is_contact(session, target_id, user_id)
    is_manager = bool(target_id) and not contact and await permissions.is_manager(bot, target_id, user_id)

    async with bot.db.session() as session:
        request = await partnerships.respond(
            session,
            bot.runtime,
            request_id=request_id,
            responder_id=user_id,
            responder_is_manager=is_manager,
            accept=accept,
            now=utcnow(),
        )
        guilds = await repository.get_guilds(session, [request.source_guild_id, request.target_guild_id])
        source_contacts = await repository.get_contact_ids(session, request.source_guild_id)
        target_contacts = await repository.get_contact_ids(session, request.target_guild_id)

    source_name = guild_name(guilds, request.source_guild_id)
    target_name = guild_name(guilds, request.target_guild_id)
    requester_side = list(dict.fromkeys([request.requester_user_id, *source_contacts]))

    names = {"requester_server": source_name, "target_server": target_name}
    if accept:
        text = "\n".join(
            [
                templates.render(bot.runtime, "request_accepted", **names),
                "",
                f"**{target_name}**",
                "\n".join(user_label(bot, uid) for uid in target_contacts) or "—",
                "",
                f"**{source_name}**",
                "\n".join(user_label(bot, uid) for uid in requester_side) or "—",
            ]
        )
        recipients = [uid for uid in dict.fromkeys([*requester_side, *target_contacts]) if uid != user_id]
    else:
        hours = bot.runtime.partnerships.decline_cooldown_hours
        text = templates.render(bot.runtime, "request_declined", **names) + (
            f"\nYou can send another request in {hours} hours." if hours else ""
        )
        recipients = [uid for uid in requester_side if uid != user_id]

    if bot.runtime.partnerships.dm_notifications:
        await deliver_dms(bot, recipients, actor_id=user_id, content=text)

    status_line = f"{'✅ Accepted' if accept else '✖️ Declined'} by {user_label(bot, user_id)}"
    if _is_single_request_notice(interaction.message, request_id) and interaction.message is not None:
        embed = interaction.message.embeds[0].copy()
        embed.add_field(name="Status", value=status_line, inline=False)
        embed.color = bot.runtime.bot.color_success if accept else bot.runtime.bot.color_danger
        await interaction.edit_original_response(embed=embed, view=None)

    if accept:
        await reply(interaction, text)
    else:
        await reply(interaction, f"Declined. **{source_name}** has been notified politely.")


# ---------------------------------------------------------------- request inbox


@register_action("requests")
async def show_requests(interaction: discord.Interaction) -> None:
    """A calm inbox: one dropdown instead of three buttons for every request."""
    bot = get_bot(interaction)
    user_id = interaction.user.id
    async with bot.db.session() as session:
        contact_ids = set(await repository.guild_ids_where_contact(session, user_id))
        manager_ids = {g.id for g in permissions.cached_manageable_guilds(bot, user_id)}
        mine = [
            row.guild_id
            for row in await repository.get_listings(session, contact_ids | manager_ids)
            if row.status != ListingStatus.REMOVED
        ]
        incoming = await repository.pending_requests_for(session, mine, incoming=True, limit=25)
        outgoing = await repository.pending_requests_for(session, mine, incoming=False, limit=10)
        guilds = await repository.get_guilds(
            session, {g for r in incoming + outgoing for g in (r.source_guild_id, r.target_guild_id)}
        )

    if not mine:
        await reply(
            interaction,
            "You're not a manager or partnership contact for any listed server yet.",
            view=persistent_view(action_button(bot, "post")),
        )
        return

    embed = discord.Embed(title="Partnership Requests", color=bot.runtime.bot.color_primary)
    if incoming:
        embed.description = f"**{len(incoming)}** request{'s' if len(incoming) != 1 else ''} waiting for you. Choose one below."
    else:
        embed.description = "No partnership requests are waiting for you."
    if outgoing:
        lines = []
        for request in outgoing[:5]:
            target = guild_name(guilds, request.target_guild_id)
            lines.append(f"{target} · {discord.utils.format_dt(request.created_at, 'R')}")
        extra = len(outgoing) - len(lines)
        if extra > 0:
            lines.append(f"+{extra} more pending")
        embed.add_field(name="Sent", value="\n".join(lines), inline=False)

    view = RequestInboxView(bot, interaction.user.id, incoming, guilds)
    await _edit_or_reply(interaction, None, view, embed=embed)


class RequestInboxView(OwnedView):
    """Select one incoming request, then reveal only the actions for that request."""

    def __init__(
        self, bot: WaypointBot, owner_id: int, incoming: list[PartnershipRequest], guilds: dict[int, Guild]
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        if incoming:
            options = []
            for request in incoming[:25]:
                source = guild_name(guilds, request.source_guild_id)
                target = guild_name(guilds, request.target_guild_id)
                options.append(
                    discord.SelectOption(
                        label=truncate(source, 100),
                        description=truncate(f"To {target}", 100),
                        value=str(request.id),
                    )
                )
            select = discord.ui.Select(placeholder="Choose a request", options=options, row=0)
            select.callback = self._selected  # type: ignore[method-assign]
            self.add_item(select)
        self.add_item(home_button(bot, row=1))

    async def _selected(self, interaction: discord.Interaction) -> None:
        request_id = int(interaction.data["values"][0])  # type: ignore[index]
        async with self.bot.db.session() as session:
            request = await repository.get_request(session, request_id)
            if request is None:
                raise ValidationError("That request no longer exists.")
            if request.status != "pending":
                raise ValidationError("That request has already been handled.")
            allowed = await can_represent(self.bot, session, request.target_guild_id, interaction.user.id)
            if not allowed:
                raise PermissionDenied("You can no longer respond for that server.")
            guilds = await repository.get_guilds(session, [request.source_guild_id, request.target_guild_id])
            source = await repository.get_listing(session, request.source_guild_id)
            target = await repository.get_listing(session, request.target_guild_id)

        embed = request_embed(self.bot, request, guilds, source, target)
        view = discord.ui.View(timeout=MENU_TIMEOUT_SECONDS)
        view.add_item(RequestResponseButton(request.id, True, row=0))
        view.add_item(RequestResponseButton(request.id, False, row=0))
        view.add_item(view_ad_button(self.bot, request.source_guild_id, label="View Ad", row=0))
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)

        async def go_back(inter: discord.Interaction) -> None:
            await show_requests(inter)

        back.callback = go_back  # type: ignore[method-assign]
        view.add_item(back)
        await interaction.response.edit_message(content=None, embed=embed, view=view)


# ---------------------------------------------------------------- find partners


@register_action("find")
async def start_find_flow(interaction: discord.Interaction) -> None:
    """Find Partners: choose a category, then see one server at a time."""
    bot = get_bot(interaction)
    async with bot.db.session() as session:
        sources = await represented_listings(bot, session, interaction.user.id)
        guilds = await repository.get_guilds(session, [s.guild_id for s in sources])

    if len(sources) > 1:
        # Only ask when it's genuinely ambiguous.
        async def picked(inter: discord.Interaction, guild_id: int) -> None:
            await CategoryView(bot, inter.user.id, source_id=guild_id).show(inter)

        embed = discord.Embed(
            title="Find Partners",
            description="Choose which server you're finding partners for.",
            color=bot.runtime.bot.color_primary,
        )
        await reply(
            interaction,
            embed=embed,
            view=GuildPickerView(interaction.user.id, [(s.guild_id, guild_name(guilds, s.guild_id)) for s in sources], picked),
        )
        return
    source_id = sources[0].guild_id if sources else None
    await CategoryView(bot, interaction.user.id, source_id=source_id).show(interaction)


class CategoryView(OwnedView):
    """One question: what kind of server are you looking for?"""

    def __init__(self, bot: WaypointBot, owner_id: int, *, source_id: int | None) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.source_id = source_id
        categories = list(bot.runtime.listings.categories)
        if len(categories) <= 4:
            for name in categories:
                self._add_button(name, row=0)
            self._add_button(ANY, row=1)
        else:
            options = [discord.SelectOption(label=c, value=c) for c in categories[:24]]
            options.append(discord.SelectOption(label=ANY, value=ANY))
            select = discord.ui.Select(placeholder="Choose a category", options=options, row=0)
            select.callback = self._selected  # type: ignore[method-assign]
            self._select = select
            self.add_item(select)

    def _add_button(self, name: str, row: int) -> None:
        button = discord.ui.Button(label=name, style=discord.ButtonStyle.primary, row=row)

        async def callback(interaction: discord.Interaction) -> None:
            await self._start(interaction, name)

        button.callback = callback  # type: ignore[method-assign]
        self.add_item(button)

    async def _selected(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, self._select.values[0])

    async def _start(self, interaction: discord.Interaction, category: str) -> None:
        self.stop()
        await FinderView(self.bot, self.owner_id, category=category, source_id=self.source_id).show(interaction)

    async def show(self, interaction: discord.Interaction) -> None:
        text = "## Find Partners\nChoose a category."
        if interaction.response.is_done():
            await interaction.edit_original_response(content=text, embeds=[], view=self)
        elif interaction.message is not None and (interaction.guild is None or interaction.message.flags.ephemeral):
            await interaction.response.edit_message(content=text, embeds=[], view=self)
        else:
            await reply(interaction, text, view=self)


class FinderView(OwnedView):
    """One result at a time. Servers already shown are never repeated."""

    def __init__(
        self,
        bot: WaypointBot,
        owner_id: int,
        *,
        category: str,
        source_id: int | None,
        seen: set[int] | None = None,
        include_past: bool = False,
        past_only: bool = False,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.category = category
        self.source_id = source_id
        self.include_past = include_past
        self.past_only = past_only
        self.past_partners: set[int] = set()
        self.seen: set[int] = seen if seen is not None else set()
        self.current: Listing | None = None
        self.current_name = ""
        self.current_members = 0
        self.current_icon_url: str | None = None

    # ---- choosing what to show

    async def _search_context(self):
        """Load small relationship sets once; listing rows are paged separately."""
        bot = self.bot
        async with bot.db.session() as session:
            pending = await repository.pending_target_ids(session, self.source_id) if self.source_id else set()
            partners = await repository.accepted_partner_ids(session, self.source_id) if self.source_id else set()
            source_count = 0
            if self.source_id:
                source_guild = bot.get_guild(self.source_id)
                stored = await repository.get_guild(session, self.source_id)
                source_count = (source_guild.member_count if source_guild else None) or (stored.member_count if stored else 0)
        return pending, partners, source_count

    def _eligible(
        self, listing: Listing, *, pending: set[int], partners: set[int], source_count: int
    ) -> bool:
        gid = listing.guild_id
        if gid == self.source_id or gid in self.seen or gid in pending:
            return False
        is_partner = gid in partners
        if is_partner:
            self.past_partners.add(gid)
        if self.past_only:
            return is_partner
        if is_partner and not self.include_past:
            return False
        if (
            self.bot.runtime.partnerships.enforce_minimum_members
            and self.source_id
            and listing.minimum_members > source_count
        ):
            return False
        return True

    async def candidates(self) -> list[Listing]:
        """Return all eligible rows, paged so no server disappears after an arbitrary cap.

        Production ``pick_next`` uses reservoir sampling below so it does not need
        to hold the whole directory in memory. This method stays useful for tests
        and diagnostics.
        """
        bot = self.bot
        pending, partners, source_count = await self._search_context()
        self.past_partners = set()
        page_size = 100
        offset = 0
        eligible: list[Listing] = []
        while True:
            async with bot.db.session() as session:
                rows, total = await repository.search_listings(
                    session,
                    category=None if self.category == ANY else self.category,
                    accepting_only=True,
                    limit=page_size,
                    offset=offset,
                    include_test=bot.runtime.hub.mode == "test",
                )
            for listing in rows:
                if self._eligible(listing, pending=pending, partners=partners, source_count=source_count):
                    eligible.append(listing)
            offset += len(rows)
            if not rows or offset >= total:
                break
        return eligible

    async def pick_next(self) -> Listing | None:
        """Fair pick across the entire eligible directory without loading it all.

        We page through the full matching pool and use reservoir sampling. Every
        eligible unseen server therefore gets the same chance, including listings
        older than the first 200 results.
        """
        bot = self.bot
        pending, partners, source_count = await self._search_context()
        self.past_partners = set()

        page_size = 100
        async with bot.db.session() as session:
            _first, total = await repository.search_listings(
                session,
                category=None if self.category == ANY else self.category,
                accepting_only=True,
                limit=1,
                offset=0,
                include_test=bot.runtime.hub.mode == "test",
            )
        if total <= 0:
            return None

        page_count = (total + page_size - 1) // page_size
        start_page = random.randrange(page_count)
        chosen: Listing | None = None
        eligible_seen = 0

        for step in range(page_count):
            page = (start_page + step) % page_count
            async with bot.db.session() as session:
                rows, _ = await repository.search_listings(
                    session,
                    category=None if self.category == ANY else self.category,
                    accepting_only=True,
                    limit=page_size,
                    offset=page * page_size,
                    include_test=bot.runtime.hub.mode == "test",
                )
            random.shuffle(rows)
            for listing in rows:
                if not self._eligible(listing, pending=pending, partners=partners, source_count=source_count):
                    continue
                eligible_seen += 1
                if random.randrange(eligible_seen) == 0:
                    chosen = listing
        return chosen

    # ---- rendering

    def result_embed(self, notice: str | None = None) -> discord.Embed:
        """A minimal card: name, size/category, and the partnership requirement."""
        listing = self.current
        assert listing is not None

        category = ", ".join(listing.categories) or "Other"
        requirement = format_minimum(listing.minimum_members) if listing.minimum_members else "Any server size"
        lines = [f"{category} • {format_members(self.current_members)}", f"Partner size: {requirement}"]
        if notice:
            lines.insert(0, notice)
            lines.insert(1, "")
        embed = discord.Embed(
            title=self.current_name,
            description="\n".join(lines),
            color=self.bot.runtime.bot.color_primary,
        )
        if self.current_icon_url:
            embed.set_thumbnail(url=self.current_icon_url)
        return embed

    def render(self, notice: str | None = None) -> str:
        """Plain-text equivalent retained for tests and non-Discord callers."""
        embed = self.result_embed(notice)
        return f"{embed.title}\n{embed.description}"

    def build(self) -> None:
        self.clear_items()
        assert self.current is not None
        gid = self.current.guild_id
        label, _emoji = self.bot.runtime.button("request")
        request = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, row=0)
        request.callback = self._request  # type: ignore[method-assign]
        self.add_item(request)
        nxt = discord.ui.Button(label="Next Server", style=discord.ButtonStyle.primary, row=0)
        nxt.callback = self._next  # type: ignore[method-assign]
        self.add_item(nxt)
        # Keep finder actions visually consistent. The dynamic View Ad button
        # resolves the current message ID at click time, so it stays correct
        # after an edit or Relist.
        self.add_item(view_ad_button(self.bot, gid, label="View Ad", row=0))
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back  # type: ignore[method-assign]
        self.add_item(back)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        listing = await self.pick_next()
        if listing is None:
            self.stop()
            await ExhaustedView(
                self.bot, self.owner_id, category=self.category, source_id=self.source_id, seen=self.seen,
                past_partners=bool(self.past_partners) and not self.include_past,
            ).show(interaction, first=not self.seen)
            return
        self.current = listing
        self.seen.add(listing.guild_id)  # shown once = seen
        async with self.bot.db.session() as session:
            stored = await repository.get_guild(session, listing.guild_id)
        self.current_name = stored.name if stored else f"Server {listing.guild_id}"
        self.current_members = stored.member_count if stored else 0
        self.current_icon_url = stored.icon_url if stored else None
        self.build()
        await _edit_or_reply(interaction, None, self, embed=self.result_embed(notice))

    async def _next(self, interaction: discord.Interaction) -> None:
        await self.show(interaction)

    async def _request(self, interaction: discord.Interaction) -> None:
        assert self.current is not None
        if self.source_id is None:
            await reply(
                interaction,
                "To request a partnership, list your own server first.",
                view=persistent_view(action_button(self.bot, "post")),
            )
            return
        await RequestPromptView(self, self.current.guild_id, self.current_name).show(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await CategoryView(self.bot, self.owner_id, source_id=self.source_id).show(interaction)


class RequestPromptView(OwnedView):
    """A message is optional, so sending is one press."""

    def __init__(self, finder: FinderView, target_id: int, target_name: str) -> None:
        super().__init__(finder.owner_id)
        self.finder = finder
        self.target_id = target_id
        self.target_name = target_name
        send = discord.ui.Button(label="Send Request", style=discord.ButtonStyle.primary, row=0)
        add = discord.ui.Button(label="Add Message", style=discord.ButtonStyle.primary, row=0)
        send.callback = self._send_plain  # type: ignore[method-assign]
        add.callback = self._add_message  # type: ignore[method-assign]
        self.add_item(send)
        self.add_item(add)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back  # type: ignore[method-assign]
        self.add_item(back)

    async def show(self, interaction: discord.Interaction) -> None:
        await _edit_or_reply(interaction, f"## Partner with {self.target_name}\nAdd a message?", self)

    async def _send(self, interaction: discord.Interaction, message: str) -> None:
        self.stop()
        assert self.finder.source_id is not None
        await submit_request(interaction, self.finder.source_id, self.target_id, message, finder=self.finder)

    async def _send_plain(self, interaction: discord.Interaction) -> None:
        await self._send(interaction, "")

    async def _add_message(self, interaction: discord.Interaction) -> None:
        async def submitted(inter: discord.Interaction, message: str) -> None:
            await self._send(inter, message)

        await interaction.response.send_modal(
            ShortMessageModal(
                title=f"Partner with {truncate(self.target_name, 30)}",
                label="Message (optional)",
                placeholder="We're interested in partnering!",
                max_length=self.finder.bot.runtime.partnerships.max_message_length or 1,
                on_submit=submitted,
            )
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.finder.show(interaction)


class ExhaustedView(OwnedView):
    """Every eligible server has been shown. Never silently loop back to the first."""

    def __init__(
        self,
        bot: WaypointBot,
        owner_id: int,
        *,
        category: str,
        source_id: int | None,
        seen: set[int],
        past_partners: bool = False,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.category = category
        self.source_id = source_id
        self.seen = seen
        self.past_partners = past_partners
        if past_partners:
            past = discord.ui.Button(label="Show Past Partners", style=discord.ButtonStyle.primary, row=0)
            past.callback = self._past  # type: ignore[method-assign]
            self.add_item(past)
        another = discord.ui.Button(label="Try Another Category", style=discord.ButtonStyle.primary, row=0)
        again = discord.ui.Button(label="Show Again", style=discord.ButtonStyle.primary, row=0)
        another.callback = self._another  # type: ignore[method-assign]
        again.callback = self._again  # type: ignore[method-assign]
        self.add_item(another)
        self.add_item(again)

    def embed(self, first: bool) -> discord.Embed:
        category = "any category" if self.category == ANY else self.category
        if first:
            embed = discord.Embed(
                title="❌ No servers found",
                description=(
                    f"No servers are looking for partners in **{category}** right now.\n"
                    "Try another category or check again later."
                ),
                color=0xED4245,
            )
        else:
            description = f"You've seen every new **{category}** partner available right now."
            if self.past_partners:
                description += "\n\nYou can still view servers you've partnered with before."
            embed = discord.Embed(
                title="✅ You're all caught up",
                description=description,
                color=self.bot.runtime.bot.color_primary,
            )
        embed.set_footer(text="Waypoint • Partner Finder")
        return embed

    def render(self, first: bool) -> str:
        # Kept for tests/callers that need plain text, while Discord gets the polished embed.
        return self.embed(first).description or ""

    async def _past(self, interaction: discord.Interaction) -> None:
        self.stop()
        await FinderView(
            self.bot, self.owner_id, category=self.category, source_id=self.source_id,
            include_past=True, past_only=True,
        ).show(interaction)

    async def show(self, interaction: discord.Interaction, first: bool = False) -> None:
        embed = self.embed(first)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed, view=self)
        elif interaction.message is not None and (interaction.guild is None or interaction.message.flags.ephemeral):
            await interaction.response.edit_message(content=None, embed=embed, view=self)
        else:
            await reply(interaction, embed=embed, view=self)

    async def _another(self, interaction: discord.Interaction) -> None:
        self.stop()
        await CategoryView(self.bot, self.owner_id, source_id=self.source_id).show(interaction)

    async def _again(self, interaction: discord.Interaction) -> None:
        self.stop()  # reset the seen list and reshuffle
        await FinderView(self.bot, self.owner_id, category=self.category, source_id=self.source_id).show(interaction)


async def _edit_or_reply(
    interaction: discord.Interaction,
    content: str | None,
    view: discord.ui.View,
    *,
    embed: discord.Embed | None = None,
) -> None:
    if interaction.response.is_done():
        if embed is None:
            await interaction.edit_original_response(content=content, embeds=[], view=view)
        else:
            await interaction.edit_original_response(content=content, embed=embed, view=view)
    elif interaction.message is not None and (interaction.guild is None or interaction.message.flags.ephemeral):
        if embed is None:
            await interaction.response.edit_message(content=content, embeds=[], view=view)
        else:
            await interaction.response.edit_message(content=content, embed=embed, view=view)
    else:
        await reply(interaction, content, embed=embed, view=view)


# ---------------------------------------------------------------- looking for partners


@register_action("looking")
async def start_looking_post(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    channel = bot.panels.looking_channel()
    if channel is None:
        await reply(interaction, "The find-partners channel isn't set up yet.")
        return
    async with bot.db.session() as session:
        sources = await represented_listings(bot, session, interaction.user.id)
        guilds = await repository.get_guilds(session, [s.guild_id for s in sources])

    if not sources:
        await reply(
            interaction,
            f"You can simply write a message in {channel.mention} and people will reply.\n"
            "To use the structured post, list your server first.",
            view=persistent_view(action_button(bot, "post")),
        )
        return

    options = [(s.guild_id, guild_name(guilds, s.guild_id)) for s in sources]
    view = LookingPostView(interaction.user.id, bot, options)
    await reply(interaction, view.render(), view=view)


class LookingPostView(OwnedView):
    def __init__(self, owner_id: int, bot: WaypointBot, sources: list[tuple[int, str]]) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.sources = dict(sources)
        self.source_id = sources[0][0]
        self.category = ANY
        self.minimum = 0
        self._build()

    def render(self) -> str:
        return (
            "## Post Looking For Partner\n"
            f"**Your server:** {self.sources[self.source_id]}\n"
            f"**Looking for:** {self.category} servers\n"
            f"**Minimum:** {format_minimum(self.minimum)}\n\n"
            "Press **Post** to add an optional short message."
        )

    def _build(self) -> None:
        self.clear_items()
        if len(self.sources) > 1:
            source = discord.ui.Select(
                placeholder="Your server",
                options=[
                    discord.SelectOption(label=truncate(name, 100), value=str(gid), default=gid == self.source_id)
                    for gid, name in list(self.sources.items())[:25]
                ],
                row=0,
            )
            source.callback = self._make_setter(source, "source_id", int)  # type: ignore[method-assign]
            self.add_item(source)
        categories = [*self.bot.runtime.listings.categories, ANY]
        category = discord.ui.Select(
            placeholder="Category wanted",
            options=[discord.SelectOption(label=c, value=c, default=c == self.category) for c in categories],
            row=1,
        )
        category.callback = self._make_setter(category, "category", str)  # type: ignore[method-assign]
        self.add_item(category)
        minimum = discord.ui.Select(
            placeholder="Minimum members",
            options=[
                discord.SelectOption(label=format_minimum(v), value=str(v), default=v == self.minimum)
                for v in self.bot.runtime.listings.minimum_member_options
            ],
            row=2,
        )
        minimum.callback = self._make_setter(minimum, "minimum", int)  # type: ignore[method-assign]
        self.add_item(minimum)
        post = discord.ui.Button(label="Post", emoji="📝", style=discord.ButtonStyle.primary, row=3)
        post.callback = self._open_modal  # type: ignore[method-assign]
        self.add_item(post)

    def _make_setter(self, select: discord.ui.Select, attribute: str, cast: type):
        async def callback(interaction: discord.Interaction) -> None:
            setattr(self, attribute, cast(select.values[0]))
            self._build()
            await interaction.response.edit_message(content=self.render(), view=self)

        return callback

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        limit = self.bot.runtime.partnerships.max_message_length
        await interaction.response.send_modal(
            ShortMessageModal(
                title="Looking for partners",
                label="Short message (optional)",
                placeholder="e.g. New social server, happy to cross-promote!",
                max_length=limit or 1,
                on_submit=self._publish,
            )
        )

    async def _publish(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = self.bot
        channel = bot.panels.looking_channel()
        if channel is None:
            raise ValidationError("The find-partners channel isn't set up yet.")

        async with bot.db.session() as session:
            if not await can_represent(bot, session, self.source_id, interaction.user.id):
                raise PermissionDenied("You can only post for servers you manage or are a partnership contact for.")
            text = await partnerships.claim_looking_post(
                session,
                bot.runtime,
                source_guild_id=self.source_id,
                actor_id=interaction.user.id,
                message=message,
                now=utcnow(),
            )
            listing = await repository.get_listing(session, self.source_id)
            guilds = await repository.get_guilds(session, [self.source_id])

        row = guilds.get(self.source_id)
        name = guild_name(guilds, self.source_id)
        lines = [
            f"🔎 **{name}** is looking for partners",
            f"**Wanted:** {self.category} servers · {format_minimum(self.minimum)}",
            f"**About us:** {', '.join(listing.categories) if listing else '—'} · {format_members(row.member_count if row else 0)}",
            f"Posted by {interaction.user.mention}",
        ]
        if text:
            lines.append("\n".join(f"> {part}" for part in text.splitlines()))
        view = persistent_view(view_ad_button(bot, self.source_id), request_button(bot, self.source_id))
        posted = await channel.send(**advertisement_kwargs("\n".join(lines)), view=view)
        await bot.panels.move_looking_panel_now()
        log.info("looking.posted guild_id=%s user_id=%s", self.source_id, interaction.user.id)
        self.stop()
        await reply(interaction, f"✅ Posted! {posted.jump_url}")
