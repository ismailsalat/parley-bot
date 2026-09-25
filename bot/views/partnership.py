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
    acknowledge,
    MENU_TIMEOUT_SECONDS,
    OwnedView,
    deliver_dms,
    get_bot,
    guard,
    guild_info,
    handle_error,
    home_button,
    mark_interaction_complete,
    reply,
    user_label,
    edit_response,
)
from bot.views.welcome import action_button, add_bot_button, persistent_view, register_action

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- shared lookups


async def represented_listings(bot: ParleyBot, session: AsyncSession, user_id: int) -> list[Listing]:
    """Every active listing the user may speak for, including disconnected verified listings.

    A person can represent many servers. Live servers use Discord's current
    permissions; disconnected servers keep the manager who originally verified
    the listing plus any partnership contacts.
    """
    contact_ids = set(await repository.guild_ids_where_contact(session, user_id))
    live_manager_ids = {guild.id for guild in await permissions.manageable_guilds(bot, user_id)}
    verified_ids = set(await repository.guild_ids_connected_by(session, user_id))
    disconnected_manager_ids = {gid for gid in verified_ids if not permissions.is_connected(bot, gid)}
    rows = await repository.get_listings(session, contact_ids | live_manager_ids | disconnected_manager_ids)
    return [row for row in rows if row.status == ListingStatus.ACTIVE]


async def can_represent(bot: ParleyBot, session: AsyncSession, guild_id: int, user_id: int) -> bool:
    if await repository.is_contact(session, guild_id, user_id):
        return True
    if permissions.is_connected(bot, guild_id):
        return await permissions.is_manager(bot, guild_id, user_id)
    stored = await repository.get_guild(session, guild_id)
    return bool(stored and stored.connected_by == user_id)


def guild_name(guilds: dict[int, Guild], guild_id: int) -> str:
    row = guilds.get(guild_id)
    return row.name if row else f"Server {guild_id}"


def display_guild_name(bot: ParleyBot, guilds: dict[int, Guild], guild_id: int) -> str:
    """Prefer Discord's live name when Parley is connected; fall back to stored listing metadata."""
    live = bot.get_guild(guild_id)
    return live.name if live is not None else guild_name(guilds, guild_id)


def describe(guilds: dict[int, Guild], listing: Listing | None, guild_id: int) -> str:
    row = guilds.get(guild_id)
    categories = ", ".join(listing.categories) if listing else "Unknown"
    members = format_members(row.member_count if row else 0)
    return f"**{guild_name(guilds, guild_id)}**\n{categories} · {members}"


# ---------------------------------------------------------------- listing components


def listing_components(bot: ParleyBot, listing: Listing) -> discord.ui.View | None:
    """[Join Server] [Request Partnership] shown under an advertisement."""
    items: list[discord.ui.Item] = []
    if listing.invite_url:
        label, emoji = bot.runtime.button("join")
        items.append(discord.ui.Button(label=label, emoji=emoji, url=listing.invite_url))
    if listing.accepting_partnerships:
        items.append(request_button(bot, listing.guild_id))
    return persistent_view(*items) if items else None


def listing_message_kwargs(bot: ParleyBot, listing: Listing) -> dict:
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
        style: discord.ButtonStyle = discord.ButtonStyle.success,
        row: int | None = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label or "Request Partnership",
                emoji=emoji,
                # Partnership actions are always green across every surface.
                style=discord.ButtonStyle.success,
                custom_id=f"wp:req:{guild_id}",
                row=row,
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(int(match["gid"]), label=item.label, emoji=item.emoji, style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)  # the prompt is a view, so deferring is safe
        try:
            if not await guard(interaction):
                return
            await start_request_flow(interaction, self.guild_id)
        except Exception as exc:  # noqa: BLE001 - handled and logged
            await handle_error(interaction, exc)
        finally:
            mark_interaction_complete(interaction)


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
        await acknowledge(interaction)
        try:
            if not await guard(interaction):
                return
            await show_ad(interaction, self.guild_id)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)
        finally:
            mark_interaction_complete(interaction)


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
        await acknowledge(interaction)
        try:
            if not await guard(interaction):
                return
            await respond_to_request(interaction, self.request_id, self.accept)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)
        finally:
            mark_interaction_complete(interaction)


def request_button(bot: ParleyBot, guild_id: int, row: int | None = None) -> RequestPartnershipButton:
    label, emoji = bot.runtime.button("request")
    return RequestPartnershipButton(
        guild_id, label=label, emoji=emoji, style=discord.ButtonStyle.success, row=row
    )


def view_ad_button(bot: ParleyBot, guild_id: int, *, label: str | None = None, row: int | None = None) -> ViewAdButton:
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
        options: Sequence[tuple[int, str] | tuple[int, str, str]],
        on_pick: Callable[[discord.Interaction, int], Awaitable[None]],
        *,
        placeholder: str = "Choose a server",
    ) -> None:
        super().__init__(owner_id)
        self._on_pick = on_pick
        select_options: list[discord.SelectOption] = []
        for option in options[:25]:
            gid, name = option[0], option[1]
            description = option[2] if len(option) > 2 else None
            select_options.append(
                discord.SelectOption(
                    label=truncate(name, 100),
                    value=str(gid),
                    description=truncate(description, 100) if description else None,
                )
            )
        select = discord.ui.Select(
            placeholder=placeholder,
            options=select_options,
        )
        select.callback = self._picked  # type: ignore[method-assign]
        self._select = select
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        await self._on_pick(interaction, int(self._select.values[0]))


# ---------------------------------------------------------------- request flow


async def start_request_flow(
    interaction: discord.Interaction, target_id: int, *, finder: "FinderView | None" = None
) -> None:
    """Start a request and make the represented server explicit.

    One listed server goes straight to confirmation. Multiple listed servers get
    one dropdown so admins never accidentally request from the wrong community.
    """
    bot = get_bot(interaction)
    async with bot.db.session() as session:
        target = listing_service.require_visible(await repository.get_listing(session, target_id))
        if not target.accepting_partnerships:
            raise ValidationError("This server isn't accepting partnership requests right now.")
        sources = [s for s in await represented_listings(bot, session, interaction.user.id) if s.guild_id != target_id]
        guilds = await repository.get_guilds(session, [target_id, *(s.guild_id for s in sources)])

    target_name = display_guild_name(bot, guilds, target_id)
    if not sources:
        embed = discord.Embed(
            title="🤝 List a Server First",
            description=(
                f"Partnership requests to **{target_name}** need to come from one of your own listed servers.\n\n"
                "List a server, then come back and try again."
            ),
            color=bot.runtime.bot.color_primary,
        )
        await _edit_or_reply(
            interaction,
            None,
            persistent_view(action_button(bot, "post"), home_button(bot)),
            embed=embed,
        )
        return

    def source_row(listing: Listing) -> tuple[int, str, str]:
        row = guilds.get(listing.guild_id)
        name = display_guild_name(bot, guilds, listing.guild_id)
        category = ", ".join(listing.categories) or "Other"
        members = format_members(row.member_count if row else 0)
        return listing.guild_id, name, f"{category} · {members}"

    async def open_prompt(inter: discord.Interaction, source_id: int) -> None:
        if finder is not None:
            finder.source_id = source_id
        await RequestPromptView(
            bot, inter.user.id, source_id, target_id, target_name, finder=finder
        ).show(inter)

    if len(sources) == 1:
        await open_prompt(interaction, sources[0].guild_id)
        return

    embed = discord.Embed(
        title="🤝 Choose Your Server",
        description=f"Which of your listed servers should request a partnership with **{target_name}**?",
        color=bot.runtime.bot.color_primary,
    )
    options = [source_row(s) for s in sorted(sources, key=lambda row: display_guild_name(bot, guilds, row.guild_id).lower())]
    await _edit_or_reply(
        interaction,
        None,
        GuildPickerView(interaction.user.id, options, open_prompt, placeholder="Select your server"),
        embed=embed,
    )

def request_embed(
    bot: ParleyBot,
    request: PartnershipRequest,
    guilds: dict[int, Guild],
    source: Listing | None,
    target: Listing | None,
    *,
    connected: bool = True,
) -> discord.Embed:
    source_row = guilds.get(request.source_guild_id)
    source_name = guild_name(guilds, request.source_guild_id)
    target_name = guild_name(guilds, request.target_guild_id)
    category = ", ".join(source.categories) if source else "Other"
    members = format_members(source_row.member_count if source_row else 0)
    requester = user_label(bot, request.requester_user_id)
    description = (
        f"{requester} sent you a partnership request.\n\n"
        f"**{source_name}** → **{target_name}**\n"
        f"{category} · {members}"
    )
    if not connected:
        description += (
            f"\n\nDM {requester} to start the conversation. "
            "Parley isn't connected to this server, so the request is handled through DMs. "
            "Add Parley for one-click Accept / Decline next time."
        )
    embed = discord.Embed(title="🤝 Partnership Request", description=description, color=bot.runtime.bot.color_primary)
    if request.message:
        embed.add_field(name="Note", value=truncate(request.message, 500), inline=False)
    embed.set_footer(text=f"Request #{request.id} · {source_name} → {target_name}")
    return embed

def request_actions(bot: ParleyBot, request: PartnershipRequest) -> discord.ui.View:
    _label, emoji = bot.runtime.button("view_ad")
    return persistent_view(
        RequestResponseButton(request.id, True),
        RequestResponseButton(request.id, False),
        ViewAdButton(request.source_guild_id, label="View Server", emoji=emoji),
    )


def manual_request_actions(bot: ParleyBot, request: PartnershipRequest) -> discord.ui.View:
    _label, emoji = bot.runtime.button("view_ad")
    return persistent_view(
        ViewAdButton(request.source_guild_id, label="View Server", emoji=emoji),
        add_bot_button(bot),
    )


def _partner_exchange_view(listing: Listing) -> discord.ui.View | None:
    if not listing.invite_url:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Join Server", url=listing.invite_url))
    return view


def _network_channel(bot: ParleyBot, channel_id: int | None, guild_id: int):
    """The server's Network channel, or None. Accepts text and announcement channels."""
    channel = bot.get_channel(channel_id) if channel_id else None
    if getattr(channel, "type", None) not in (discord.ChannelType.text, discord.ChannelType.news):
        return None
    return channel if getattr(channel.guild, "id", None) == guild_id else None


async def exchange_partner_ads(bot: ParleyBot, source_id: int, target_id: int) -> bool:
    from bot.services import network as network_service
    from bot.utils.mentions import advertisement_kwargs, safe_allowed_mentions
    source_guild = bot.get_guild(source_id)
    target_guild = bot.get_guild(target_id)
    if source_guild is None or target_guild is None:
        return False
    async with bot.db.session() as session:
        source = await repository.get_listing(session, source_id)
        target = await repository.get_listing(session, target_id)
        source_net = await repository.get_network_settings(session, source_id)
        target_net = await repository.get_network_settings(session, target_id)
    if not source or not target or not source_net or not target_net:
        return False
    if source.status != ListingStatus.ACTIVE or target.status != ListingStatus.ACTIVE:
        return False
    if not source_net.enabled or not target_net.enabled or not source_net.channel_id or not target_net.channel_id:
        return False
    source_channel = _network_channel(bot, source_net.channel_id, source_id)
    target_channel = _network_channel(bot, target_net.channel_id, target_id)
    if source_channel is None or target_channel is None:
        return False
    for channel in (source_channel, target_channel):
        me = channel.guild.me
        if me is None:
            return False
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.read_message_history):
            return False
    def payload(listing: Listing) -> dict:
        kwargs = advertisement_kwargs(listing.advertisement_text)
        footer = "-# 🤝 Parley partner exchange"
        if len(kwargs["content"]) + len(footer) + 1 <= 2000:
            kwargs["content"] += "\n" + footer
        view = _partner_exchange_view(listing)
        if view is not None:
            kwargs["view"] = view
        kwargs["allowed_mentions"] = safe_allowed_mentions()
        return kwargs
    first = None
    try:
        first = await source_channel.send(**payload(target))
        second = await target_channel.send(**payload(source))
    except discord.HTTPException as exc:
        if first is not None:
            try:
                await first.delete()
            except discord.HTTPException:
                pass
        log.warning("partnership.exchange_failed source=%s target=%s: %s", source_id, target_id, exc)
        return False
    now = utcnow()
    async with bot.db.session() as session:
        await network_service.record_post(session, source_guild_id=target_id, destination_guild_id=source_id, channel_id=source_channel.id, message_id=first.id, now=now)
        await network_service.record_post(session, source_guild_id=source_id, destination_guild_id=target_id, channel_id=target_channel.id, message_id=second.id, now=now)
    await bot.log_event(f"🤝 Partner ads exchanged: `{source_id}` ↔ `{target_id}`.")
    return True


async def deliver_request_notification(
    bot: ParleyBot, request: PartnershipRequest, guilds: dict[int, Guild],
    source: Listing, target: Listing, contact_ids: list[int], *, actor_id: int | None,
) -> tuple[int, int]:
    connected = permissions.is_connected(bot, request.target_guild_id)
    embed = request_embed(bot, request, guilds, source, target, connected=connected)
    if connected:
        async with bot.db.session() as session:
            settings = await repository.get_network_settings(session, request.target_guild_id)
        channel = (
            _network_channel(bot, settings.channel_id, request.target_guild_id)
            if settings and settings.enabled
            else None
        )
        if channel is not None:
            try:
                await channel.send(embed=embed, view=request_actions(bot, request), allowed_mentions=discord.AllowedMentions.none())
                return 1, 0
            except discord.HTTPException:
                log.warning("partnership.channel_delivery_failed id=%s target=%s", request.id, request.target_guild_id)
        return await deliver_dms(bot, contact_ids, actor_id=actor_id, embed=embed, view=request_actions(bot, request))
    return await deliver_dms(bot, contact_ids, actor_id=actor_id, embed=embed, view=manual_request_actions(bot, request))


async def submit_request(
    interaction: discord.Interaction, source_id: int, target_id: int, message: str, finder=None
) -> None:
    if not interaction.response.is_done():
        await acknowledge(interaction)
    bot = get_bot(interaction)
    user_id = interaction.user.id
    source_guild = bot.get_guild(source_id)
    if source_guild is not None:
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
        delivered, held = await deliver_request_notification(
            bot, context.request, guilds, context.source, context.target, contact_ids, actor_id=user_id
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
        nxt = discord.ui.Button(label="Next", style=discord.ButtonStyle.primary, row=0)

        async def continue_search(inter: discord.Interaction) -> None:
            await acknowledge(inter)
            await finder.show(inter)

        nxt.callback = continue_search  # type: ignore[method-assign]
        view.add_item(nxt)
        view.add_item(home_button(bot, row=1))
        await _edit_or_reply(interaction, "Request sent ✓", view)
        return
    await _edit_or_reply(interaction, text, persistent_view(home_button(bot)))


# ---------------------------------------------------------------- answering requests


def _is_single_request_notice(message: discord.Message | None, request_id: int) -> bool:
    if message is None or not message.embeds:
        return False
    footer = message.embeds[0].footer.text or ""
    return footer == f"Request #{request_id}"


async def respond_to_request(interaction: discord.Interaction, request_id: int, accept: bool) -> None:
    # Dynamic request buttons already acknowledge before entering here. Keep this
    # safe for direct callers without attempting a second response.
    if not interaction.response.is_done():
        await acknowledge(interaction, thinking=False)
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

    if accept:
        exchanged = await exchange_partner_ads(bot, request.source_guild_id, request.target_guild_id)
        if exchanged:
            text += "\n\n✅ Both server ads were exchanged in their Parley Network channels."
        elif permissions.is_connected(bot, request.source_guild_id) and permissions.is_connected(bot, request.target_guild_id):
            text += "\n\n-# Ads were not exchanged because both servers need an enabled Parley Network channel."

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
        manager_ids = {g.id for g in await permissions.manageable_guilds(bot, user_id)}
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
            source = guild_name(guilds, request.source_guild_id)
            target = guild_name(guilds, request.target_guild_id)
            lines.append(f"{source} → {target} · {discord.utils.format_dt(request.created_at, 'R')}")
        extra = len(outgoing) - len(lines)
        if extra > 0:
            lines.append(f"+{extra} more pending")
        embed.add_field(name="Sent", value="\n".join(lines), inline=False)

    view = RequestInboxView(bot, interaction.user.id, incoming, guilds)
    await _edit_or_reply(interaction, None, view, embed=embed)


class RequestInboxView(OwnedView):
    """Select one incoming request, then reveal only the actions for that request."""

    def __init__(
        self, bot: ParleyBot, owner_id: int, incoming: list[PartnershipRequest], guilds: dict[int, Guild]
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
        await acknowledge(interaction)
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
            await acknowledge(inter)
            await show_requests(inter)

        back.callback = go_back  # type: ignore[method-assign]
        view.add_item(back)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


# ---------------------------------------------------------------- find partners


async def _continue_find_for_guild(
    interaction: discord.Interaction,
    guild: discord.Guild,
    *,
    edit_message: bool = True,
    allow_change_server: bool | None = None,
) -> None:
    """Make Find Partners the single guided entry point for partnership setup.

    ``allow_change_server`` is true for the Parley hub/DM dashboard so the user
    can always switch the server they are acting for. Inside a customer server,
    the current guild remains the explicit target and no redundant picker is
    shown.
    """
    bot = get_bot(interaction)
    if allow_change_server is None:
        allow_change_server = (
            interaction.guild is None or interaction.guild.id == bot.runtime.hub.main_guild_id
        )
    await permissions.require_manager(bot, guild.id, interaction.user.id)
    await bot.maybe_dm_manager_onboarding(guild, interaction.user)

    async with bot.db.session() as session:
        listing = await repository.get_listing(session, guild.id)
        settings = await repository.get_network_settings(session, guild.id)

    # Step 1: every partnership needs the shared server ad. Reuse the exact same
    # listing wizard/cooldowns as Post Server Ad, then continue to Network setup.
    if listing is None or listing.status != ListingStatus.ACTIVE:
        if listing is not None and listing.status == ListingStatus.PENDING:
            await _edit_or_reply(
                interaction,
                (
                    f"## Partnership Setup · {guild.name}\n"
                    "Your server ad is waiting for review. Once it is live, press **Find Partners** again. "
                    "You do not need to create another ad."
                ),
                persistent_view(home_button(bot)),
            )
            return
        from bot.views.listings import open_listing_form

        await open_listing_form(
            interaction, guild, edit_message=edit_message or interaction.response.is_done(), return_to_network=True
        )
        return

    # Step 2: a live ad exists, so make sure this server has a delivery channel.
    if settings is None or not settings.enabled or not settings.channel_id:
        from bot.views.network import open_network_setup

        await open_network_setup(
            interaction,
            guild,
            edit_message=edit_message or interaction.response.is_done(),
            require_listing=False,
            return_to_find=True,
            notice="✅ Server ad ready. Choose the channel where approved partner ads should be delivered.",
        )
        return

    # Step 3: setup is complete. The user only has to choose what kind of
    # partner they want; no extra setup buttons or duplicate dashboards.
    await CategoryView(
        bot,
        interaction.user.id,
        source_id=guild.id,
        allow_change_server=allow_change_server,
    ).show(interaction)


@register_action("find")
async def start_find_flow(interaction: discord.Interaction) -> None:
    """Find Partners is the single, guided partnership entry point."""
    # /find can call this directly, while persistent buttons may already be deferred.
    await acknowledge(interaction)
    bot = get_bot(interaction)

    # Inside a connected server, always act on that server.
    context_guild = interaction.guild
    if context_guild is not None and context_guild.id != bot.runtime.hub.main_guild_id:
        if not await permissions.is_manager(bot, context_guild.id, interaction.user.id):
            raise PermissionDenied("You need **Manage Server** or **Administrator** to find partnerships for this server.")
        await _continue_find_for_guild(interaction, context_guild)
        return

    # In DMs / the main Parley server, show every connected server the person
    # can actually manage. Listing and Network readiness are handled after pick.
    connected = [
        guild for guild in await permissions.manageable_guilds(bot, interaction.user.id)
        if guild.id != bot.runtime.hub.main_guild_id
    ]
    if not connected:
        text = (
            "## Connect a Server First\n"
            "Add Parley to the server you manage, then press **Find Partners** again. "
            "Parley will guide you through the server ad and partner-ad channel automatically."
        )
        await _edit_or_reply(interaction, text, persistent_view(add_bot_button(bot), home_button(bot)))
        return

    async with bot.db.session() as session:
        listings = {row.guild_id: row for row in await repository.get_listings(session, [g.id for g in connected])}
        settings_rows = {
            g.id: await repository.get_network_settings(session, g.id)
            for g in connected
        }

    def status(guild: discord.Guild) -> str:
        listing = listings.get(guild.id)
        net = settings_rows.get(guild.id)
        if listing is None or listing.status != ListingStatus.ACTIVE:
            return "Setup needed · server ad"
        if net is None or not net.enabled or not net.channel_id:
            return "Setup needed · partner-ad channel"
        return "Ready to find partners"

    async def picked(inter: discord.Interaction, guild_id: int) -> None:
        chosen = bot.get_guild(guild_id)
        if chosen is None:
            raise ValidationError("Parley is no longer in that server.")
        await _continue_find_for_guild(
            inter, chosen, edit_message=True, allow_change_server=True
        )

    await _edit_or_reply(
        interaction,
        "## Find Partners\nWhich server are you finding a partner for?",
        GuildPickerView(
            interaction.user.id,
            [(g.id, g.name, status(g)) for g in connected],
            picked,
            placeholder="Select your server",
        ),
    )


class CategoryView(OwnedView):
    """One question: what kind of server are you looking for?"""

    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        *,
        source_id: int | None,
        allow_change_server: bool = False,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.source_id = source_id
        self.allow_change_server = allow_change_server
        categories = list(bot.runtime.listings.categories)
        if len(categories) <= 4:
            for name in categories:
                self._add_button(name, row=0)
            self._add_button(ANY, row=1)
        else:
            options = [discord.SelectOption(label=c, value=c) for c in categories[:24]]
            options.append(discord.SelectOption(label="Any Category", value=ANY))
            select = discord.ui.Select(placeholder="Choose a category", options=options, row=0)
            select.callback = self._selected  # type: ignore[method-assign]
            self._select = select
            self.add_item(select)

        if self.allow_change_server:
            change = discord.ui.Button(label="Change Server", style=discord.ButtonStyle.secondary, row=2)
            change.callback = self._change_server  # type: ignore[method-assign]
            self.add_item(change)

    def _add_button(self, name: str, row: int) -> None:
        label = "Any Category" if name == ANY else name
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, row=row)

        async def callback(interaction: discord.Interaction) -> None:
            await self._start(interaction, name)

        button.callback = callback  # type: ignore[method-assign]
        self.add_item(button)

    async def _selected(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, self._select.values[0])

    async def _start(self, interaction: discord.Interaction, category: str) -> None:
        await acknowledge(interaction)
        self.stop()
        await FinderView(
            self.bot,
            self.owner_id,
            category=category,
            source_id=self.source_id,
            allow_change_server=self.allow_change_server,
        ).show(interaction)

    async def _change_server(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        await start_find_flow(interaction)

    async def show(self, interaction: discord.Interaction) -> None:
        source = self.bot.get_guild(self.source_id) if self.source_id else None
        if source is not None:
            text = f"## Find Partners · {source.name}\nChoose a category."
        else:
            text = "## Find Partners\nChoose a category."
        if interaction.response.is_done():
            await interaction.edit_original_response(content=text, embeds=[], view=self)
        elif interaction.message is not None and (interaction.guild is None or interaction.message.flags.ephemeral):
            await edit_response(interaction, content=text, embeds=[], view=self)
        else:
            await reply(interaction, text, view=self)


class FinderView(OwnedView):
    """One result at a time. Servers already shown are never repeated."""

    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        *,
        category: str,
        source_id: int | None,
        allow_change_server: bool = False,
        seen: set[int] | None = None,
        include_past: bool = False,
        past_only: bool = False,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.category = category
        self.source_id = source_id
        self.allow_change_server = allow_change_server
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
        if listing.partner_ad_text:
            lines.extend(["", truncate(listing.partner_ad_text, 700)])
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
        if self.source_id:
            source = self.bot.get_guild(self.source_id)
            if source is not None:
                embed.set_footer(text=f"Finding for {source.name}")
        return embed

    def render(self, notice: str | None = None) -> str:
        """Plain-text equivalent retained for tests and non-Discord callers."""
        embed = self.result_embed(notice)
        return f"{embed.title}\n{embed.description}"

    def build(self) -> None:
        self.clear_items()
        assert self.current is not None
        gid = self.current.guild_id

        request = discord.ui.Button(label="Request Partnership", style=discord.ButtonStyle.success, row=0)
        request.callback = self._request  # type: ignore[method-assign]
        self.add_item(request)

        nxt = discord.ui.Button(label="Next", style=discord.ButtonStyle.primary, row=0)
        nxt.callback = self._next  # type: ignore[method-assign]
        self.add_item(nxt)

        partner_url = listing_jump_url(
            self.bot.runtime.hub.main_guild_id,
            self.current.partner_channel_id,
            self.current.partner_message_id,
        )
        if partner_url:
            self.add_item(discord.ui.Button(label="View Partner Post", url=partner_url, row=0))
        else:
            self.add_item(view_ad_button(self.bot, gid, label="View Server", row=0))

        if self.allow_change_server:
            change = discord.ui.Button(label="Change Server", style=discord.ButtonStyle.secondary, row=1)
            change.callback = self._change_server  # type: ignore[method-assign]
            self.add_item(change)
        self.add_item(home_button(self.bot, row=1))

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        listing = await self.pick_next()
        if listing is None:
            self.stop()
            await ExhaustedView(
                self.bot, self.owner_id, category=self.category, source_id=self.source_id, seen=self.seen,
                allow_change_server=self.allow_change_server,
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
        await acknowledge(interaction)
        await self.show(interaction)

    async def _request(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        assert self.current is not None
        if self.source_id is None:
            await start_request_flow(interaction, self.current.guild_id, finder=self)
            return
        await RequestPromptView.for_finder(self, self.current.guild_id, self.current_name).show(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        await CategoryView(
            self.bot,
            self.owner_id,
            source_id=self.source_id,
            allow_change_server=self.allow_change_server,
        ).show(interaction)

    async def _change_server(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        await start_find_flow(interaction)


class RequestPromptView(OwnedView):
    """Final confirmation: always show exactly which two servers are involved."""

    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        source_id: int,
        target_id: int,
        target_name: str,
        *,
        finder: FinderView | None = None,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.source_id = source_id
        self.finder = finder
        self.target_id = target_id
        self.target_name = target_name
        self._change_added = False

        send = discord.ui.Button(label="Send Request", emoji="🤝", style=discord.ButtonStyle.success, row=0)
        add = discord.ui.Button(label="Add Note", style=discord.ButtonStyle.secondary, row=0)
        send.callback = self._send_plain  # type: ignore[method-assign]
        add.callback = self._add_message  # type: ignore[method-assign]
        self.add_item(send)
        self.add_item(add)
        if finder is not None:
            back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
            back.callback = self._back  # type: ignore[method-assign]
            self.add_item(back)

    @classmethod
    def for_finder(cls, finder: FinderView, target_id: int, target_name: str) -> "RequestPromptView":
        assert finder.source_id is not None
        return cls(finder.bot, finder.owner_id, finder.source_id, target_id, target_name, finder=finder)

    async def _source_name_and_count(self) -> tuple[str, int]:
        async with self.bot.db.session() as session:
            guilds = await repository.get_guilds(session, [self.source_id])
            sources = [
                row for row in await represented_listings(self.bot, session, self.owner_id)
                if row.guild_id != self.target_id
            ]
        return display_guild_name(self.bot, guilds, self.source_id), len(sources)

    async def show(self, interaction: discord.Interaction) -> None:
        source_name, source_count = await self._source_name_and_count()
        if source_count > 1 and not self._change_added:
            change = discord.ui.Button(label="Change Server", style=discord.ButtonStyle.secondary, row=1)
            change.callback = self._change_server  # type: ignore[method-assign]
            self.add_item(change)
            self._change_added = True
        embed = discord.Embed(
            title="🤝 Partnership Request",
            description=(
                f"**From:** {source_name}\n"
                f"**To:** {self.target_name}\n\n"
                "Send it now, or add a short optional note."
            ),
            color=self.bot.runtime.bot.color_primary,
        )
        await _edit_or_reply(interaction, None, self, embed=embed)

    async def _send(self, interaction: discord.Interaction, message: str) -> None:
        self.stop()
        await submit_request(interaction, self.source_id, self.target_id, message, finder=self.finder)

    async def _send_plain(self, interaction: discord.Interaction) -> None:
        await self._send(interaction, "")

    async def _add_message(self, interaction: discord.Interaction) -> None:
        async def submitted(inter: discord.Interaction, message: str) -> None:
            await self._send(inter, message)

        await interaction.response.send_modal(
            ShortMessageModal(
                title=f"Partner with {truncate(self.target_name, 30)}",
                label="Optional note",
                placeholder="A quick hello or what you're looking for...",
                max_length=self.bot.runtime.partnerships.max_message_length or 1,
                on_submit=submitted,
            )
        )

    async def _change_server(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        await start_request_flow(interaction, self.target_id, finder=self.finder)

    async def _back(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        assert self.finder is not None
        await self.finder.show(interaction)


class ExhaustedView(OwnedView):
    """Every eligible server has been shown. Never silently loop back to the first."""

    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        *,
        category: str,
        source_id: int | None,
        seen: set[int],
        allow_change_server: bool = False,
        past_partners: bool = False,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.category = category
        self.source_id = source_id
        self.seen = seen
        self.allow_change_server = allow_change_server
        self.past_partners = past_partners
        if past_partners:
            past = discord.ui.Button(label="Show Past Partners", style=discord.ButtonStyle.primary, row=0)
            past.callback = self._past  # type: ignore[method-assign]
            self.add_item(past)
        if category != ANY:
            another = discord.ui.Button(label="Try Another Category", style=discord.ButtonStyle.primary, row=0)
            another.callback = self._another  # type: ignore[method-assign]
            self.add_item(another)
        again = discord.ui.Button(label="Show Again", style=discord.ButtonStyle.primary, row=0)
        again.callback = self._again  # type: ignore[method-assign]
        self.add_item(again)
        if self.allow_change_server:
            change = discord.ui.Button(label="Change Server", style=discord.ButtonStyle.secondary, row=1)
            change.callback = self._change_server  # type: ignore[method-assign]
            self.add_item(change)

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
        embed.set_footer(text="Parley • Partner Finder")
        return embed

    def render(self, first: bool) -> str:
        # Kept for tests/callers that need plain text, while Discord gets the polished embed.
        return self.embed(first).description or ""

    async def _past(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        await FinderView(
            self.bot, self.owner_id, category=self.category, source_id=self.source_id,
            allow_change_server=self.allow_change_server, include_past=True, past_only=True,
        ).show(interaction)

    async def show(self, interaction: discord.Interaction, first: bool = False) -> None:
        embed = self.embed(first)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed, view=self)
        elif interaction.message is not None and (interaction.guild is None or interaction.message.flags.ephemeral):
            await edit_response(interaction, content=None, embed=embed, view=self)
        else:
            await reply(interaction, embed=embed, view=self)

    async def _another(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        await CategoryView(
            self.bot, self.owner_id, source_id=self.source_id,
            allow_change_server=self.allow_change_server,
        ).show(interaction)

    async def _change_server(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()
        await start_find_flow(interaction)

    async def _again(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        self.stop()  # reset the seen list and reshuffle
        await FinderView(
            self.bot, self.owner_id, category=self.category, source_id=self.source_id,
            allow_change_server=self.allow_change_server,
        ).show(interaction)


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
            await edit_response(interaction, content=content, embeds=[], view=view)
        else:
            await edit_response(interaction, content=content, embed=embed, view=view)
    else:
        await reply(interaction, content, embed=embed, view=view)


# ---------------------------------------------------------------- partner posting


@register_action("looking")
async def start_looking_post(interaction: discord.Interaction) -> None:
    """Old action ID kept so existing buttons open the new partner-post manager."""
    from bot.views.partner_posts import start_partner_posts

    await start_partner_posts(interaction)

