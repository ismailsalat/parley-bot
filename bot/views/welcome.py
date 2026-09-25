"""Top-level action buttons, panels and the DM control panel.

Every top-level button is an ``ActionButton`` whose custom_id is
``wp:act:<action>``. It is registered as a Discord *dynamic item*, so buttons on
old panels keep working after restarts without storing anything in memory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from bot.config import templates
from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import listings as listing_service
from bot.services import permissions
from bot.utils.helpers import format_duration, format_members, listing_jump_url, truncate, utcnow
from bot.utils.mentions import safe_allowed_mentions
from bot.views.base import (
    OwnedView,
    acknowledge,
    get_bot,
    guard,
    handle_error,
    home_button,
    mark_interaction_complete,
    reply,
    edit_response,
)

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

ActionHandler = Callable[[discord.Interaction], Awaitable[None]]
_HANDLERS: dict[str, ActionHandler] = {}
ACTION_HARD_TIMEOUT_SECONDS = 30.0


def register_action(*names: str) -> Callable[[ActionHandler], ActionHandler]:
    def decorator(func: ActionHandler) -> ActionHandler:
        for name in names:
            _HANDLERS[name] = func
        return func

    return decorator


def registered_actions() -> set[str]:
    return set(_HANDLERS)


class ActionButton(discord.ui.Button):
    """Stable top-level Parley action button.

    This is intentionally a normal :class:`discord.ui.Button`, not a
    ``DynamicItem``.  Parley registers one message-independent persistent router
    view for every ``wp:act:*`` custom id at startup, so buttons on old messages
    continue to work after restarts.  Keeping top-level actions out of the global
    DynamicItem registry also avoids a discord.py edge case where stopping a
    mixed transient view can unregister a shared DynamicItem template.
    """

    def __init__(
        self,
        action: str,
        *,
        label: str | None = None,
        emoji: str | discord.PartialEmoji | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        row: int | None = None,
    ) -> None:
        super().__init__(
            label=label or action,
            emoji=emoji,
            style=style,
            custom_id=f"wp:act:{action}",
            row=row,
        )
        self.action = action

    @property
    def item(self) -> "ActionButton":
        """Compatibility shim for code/tests that used DynamicItem.item."""
        return self

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = get_bot(interaction)
        if not claim_action_interaction(bot, interaction, self.action, source="static_view"):
            return
        await acknowledge(interaction)
        await dispatch_action(interaction, self.action, source="static_view")


def action_router_view() -> discord.ui.View:
    """Return the global restart-safe router for every top-level action.

    The view is registered with ``bot.add_view`` without a message id. Discord.py
    then uses it as a fallback for *any* message carrying one of these stable
    custom ids, including panels created by older deployments.  Labels/styles on
    this invisible router are irrelevant; the visible message keeps its own UI.
    """
    view = discord.ui.View(timeout=None)
    for index, action in enumerate(sorted(registered_actions())):
        view.add_item(
            ActionButton(
                action,
                label=action.replace("_", " ").title(),
                style=discord.ButtonStyle.secondary,
                row=index // 5,
            )
        )
    return view


def claim_action_interaction(
    bot: ParleyBot,
    interaction: discord.Interaction,
    action: str,
    *,
    source: str,
) -> bool:
    """Atomically claim a top-level button interaction.

    Discord.py's message/global persistent View dispatcher is the normal route.
    ``ParleyBot.on_interaction`` is an independent fallback route for the same stable custom_id. Whichever route
    gets scheduled first claims the interaction, so there is never a double action.
    """
    interaction_id = getattr(interaction, "id", None)
    if interaction_id is None:
        return True
    claimed = getattr(bot, "_claimed_action_interactions", None)
    if claimed is None:
        claimed = set()
        setattr(bot, "_claimed_action_interactions", claimed)
    if interaction_id in claimed:
        return False
    claimed.add(interaction_id)
    if len(claimed) > 20_000:
        claimed.clear()
        claimed.add(interaction_id)
    log.debug(
        "interaction.claim action=%s source=%s interaction_id=%s user_id=%s guild_id=%s",
        action,
        source,
        interaction_id,
        interaction.user.id,
        getattr(interaction, "guild_id", None),
    )
    return True


async def dispatch_action(interaction: discord.Interaction, action: str, *, source: str) -> None:
    """Run a top-level Parley action through one hardened dispatcher.

    This function is intentionally callable from both persistent View routing and the raw
    interaction event fallback. That makes an old public panel a disposable front
    door: even if Discord.py loses a message-specific View route, the stable
    ``wp:act:*`` custom_id can still reach the current action handler.
    """
    handler = _HANDLERS.get(action)
    if handler is None:
        log.warning("Unknown action button pressed: %s (source=%s)", action, source)
        await reply(interaction, "That button is no longer available. Please use a fresh Parley panel.")
        mark_interaction_complete(interaction)
        return
    started = time.monotonic()
    try:
        # acknowledge() arms the watchdog *before* the Discord HTTP request.
        await acknowledge(interaction, thinking=True)
        if not await guard(interaction):
            return

        # Prevent rapid double-clicks from running the same expensive action twice.
        bot = get_bot(interaction)
        active = getattr(bot, "_inflight_actions", None)
        if active is None:
            active = set()
            bot._inflight_actions = active
        key = (interaction.user.id, action)
        if key in active:
            await interaction.edit_original_response(
                content="That action is already running. Please use the result from your first click.",
                embeds=[],
                view=None,
            )
            return
        active.add(key)
        try:
            await asyncio.wait_for(handler(interaction), timeout=ACTION_HARD_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.error(
                "interaction.handler_timeout action=%s source=%s user_id=%s guild_id=%s limit=%.1fs",
                action,
                source,
                interaction.user.id,
                getattr(interaction, "guild_id", None),
                ACTION_HARD_TIMEOUT_SECONDS,
            )
            await interaction.edit_original_response(
                content=(
                    "Parley stopped a stuck action before it could hang indefinitely. "
                    "Please try again using the fresh buttons below."
                ),
                embeds=[],
                view=persistent_view(
                    action_button(bot, "find", row=0),
                    action_button(bot, "home", row=0),
                ),
                allowed_mentions=safe_allowed_mentions(),
            )
        finally:
            active.discard(key)
    except Exception as exc:  # noqa: BLE001 - reported to the user and logged by handle_error
        await handle_error(interaction, exc)
    finally:
        mark_interaction_complete(interaction)
        log.info(
            "interaction.complete action=%s source=%s user_id=%s guild_id=%s elapsed=%.3fs",
            action,
            source,
            interaction.user.id,
            getattr(interaction, "guild_id", None),
            time.monotonic() - started,
        )


BUTTON_STYLE_MAP = {
    "primary": discord.ButtonStyle.primary,    # main actions
    "success": discord.ButtonStyle.success,    # partnership / approve / enable
    "danger": discord.ButtonStyle.danger,      # destructive only
    "secondary": discord.ButtonStyle.secondary,  # navigation only
}

# Product-significant colours are intentionally locked so a stale database
# appearance override cannot turn the primary posting CTA grey or make
# partnership actions look unrelated. Labels/emojis remain customizable.
SEMANTIC_BUTTON_STYLES = {
    "post": discord.ButtonStyle.primary,
    "find": discord.ButtonStyle.success,
    "looking": discord.ButtonStyle.success,
    "partner_posts": discord.ButtonStyle.success,
    "request": discord.ButtonStyle.success,
    "partnerships": discord.ButtonStyle.success,
    "network": discord.ButtonStyle.success,
}


def button_style(bot: ParleyBot, key: str) -> discord.ButtonStyle:
    return SEMANTIC_BUTTON_STYLES.get(key, BUTTON_STYLE_MAP[bot.runtime.button_style(key)])


def action_button(
    bot: ParleyBot,
    action: str,
    *,
    style: discord.ButtonStyle | None = None,
    row: int | None = None,
    key: str | None = None,
) -> ActionButton:
    """A top-level button. Label, emoji and colour come from the settings."""
    label, emoji = bot.runtime.button(key or action)
    return ActionButton(action, label=label, emoji=emoji, style=style or button_style(bot, key or action), row=row)


PUBLIC_PERMISSIONS = discord.Permissions(
    view_channel=True,  # see the channels it posts in
    send_messages=True,  # post listings, panels and network ads
    embed_links=True,  # request cards and search results
    read_message_history=True,  # keep panels at the bottom and find its own messages
    create_instant_invite=True,  # make an invite for a listing when the owner doesn't paste one
    use_external_emojis=True,  # custom emoji in advertisements render correctly
)
# The main server also needs Manage Channels (Automatic Setup) plus, in the listings
# channel, Manage Messages and Manage Roles so "Paste My Own Ad" can open and close a
# one-off posting window and delete anything that fails moderation.
SETUP_PERMISSIONS = PUBLIC_PERMISSIONS | discord.Permissions(
    manage_channels=True,
    manage_messages=True,
    manage_roles=True,
    # Main Parley hub only: Start Here can intentionally announce @everyone / configured roles once.
    mention_everyone=True,
)


# Parley is a server app: it is installed *to a guild* and keeps a bot user there,
# because the Connected perks need it inside the server. `bot` is the scope that makes
# Discord perform a guild install; `applications.commands` registers the slash commands.
INSTALL_SCOPES = ("bot", "applications.commands")


def invite_url(bot: ParleyBot, *, setup: bool = False) -> str | None:
    """The one install link Parley hands out, built from the application ID.

    Every "Add Parley" button goes through here, so there is a single install path
    and no hand-typed or legacy URL can drift into the product.
    """
    if bot.application_id is None:
        return None
    permissions = SETUP_PERMISSIONS if setup else PUBLIC_PERMISSIONS
    return discord.utils.oauth_url(bot.application_id, permissions=permissions, scopes=INSTALL_SCOPES)


def setup_invite_url(bot: ParleyBot) -> str | None:
    return invite_url(bot, setup=True)


def link_button(bot: ParleyBot, key: str, url: str | None, row: int | None = None) -> discord.ui.Button | None:
    if not url:
        return None
    label, emoji = bot.runtime.button(key)
    return discord.ui.Button(label=label, emoji=emoji, url=url, row=row)


def add_bot_button(bot: ParleyBot, row: int | None = None) -> discord.ui.Button | None:
    return link_button(bot, "add_bot", invite_url(bot), row=row)


def optional_links(bot: ParleyBot, row: int | None = None) -> list[discord.ui.Button]:
    """Support / Rules / Website buttons for the links set in Settings -> Appearance."""
    links = bot.runtime.bot
    items = [
        link_button(bot, "support", links.support_url, row),
        link_button(bot, "rules", links.rules_url, row),
        link_button(bot, "website", links.website_url, row),
    ]
    return [item for item in items if item is not None]


def persistent_view(*items: discord.ui.Item | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for item in items:
        if item is not None:
            view.add_item(item)
    return view


# ---------------------------------------------------------------- public panel presentation

# Public navigation panels intentionally use normal Discord markdown instead of
# decorative embeds. This keeps them compact, theme-native and easy to customize.


# ---------------------------------------------------------------- panel builders


def control_panel(bot: ParleyBot, *, staff: bool = False) -> tuple[str, discord.ui.View]:
    """The DM home: five clear user actions, plus Settings for staff."""
    content = templates.render(bot.runtime, "dm_home")
    if staff and bot.runtime.hub.mode != "live":
        content += f"\n-# Mode: {'🧪 TEST' if bot.runtime.hub.mode == 'test' else '🔴 OFF'}"
    view = persistent_view(
        action_button(bot, "post", row=0),
        action_button(bot, "find", row=0),
        action_button(bot, "servers", row=1),
        action_button(bot, "partner_posts", row=1),
        action_button(bot, "requests", row=2),
        action_button(bot, "settings", row=3) if staff else None,
    )
    return content, view

def set_listing_button_label(view: discord.ui.View, *, multiple: bool) -> None:
    """Use My Listing for one server and My Servers when a picker is actually needed."""
    for child in view.children:
        item = getattr(child, "item", child)
        if getattr(item, "custom_id", None) == "wp:act:servers":
            item.label = "My Server Listings" if multiple else "My Server Listing"
            item.emoji = None
            return


async def personalize_control_panel(bot: ParleyBot, user_id: int, *, staff: bool = False) -> tuple[str, discord.ui.View]:
    content, view = control_panel(bot, staff=staff)
    managed_ids = {g.id for g in await permissions.manageable_guilds(bot, user_id)}
    from bot.database import repository
    from bot.database.models import ListingStatus
    async with bot.db.session() as session:
        managed_ids.update(await repository.guild_ids_connected_by(session, user_id))
        rows = [
            row for row in await repository.get_listings(session, managed_ids)
            if row.status != ListingStatus.REMOVED
        ]
    if rows:
        set_listing_button_label(view, multiple=len(rows) > 1)
    return content, view


def listings_panel(bot: ParleyBot) -> tuple[str, discord.ui.View]:
    """Server Directory controls: only server-ad actions."""
    view = persistent_view(
        action_button(bot, "post", row=0),
        action_button(bot, "servers", row=0),
        action_button(bot, "relist", row=0),
    )
    return templates.render(bot.runtime, "listings_panel"), view


def looking_panel(bot: ParleyBot) -> tuple[str, discord.ui.View]:
    """Partner Board controls: partnership actions stay green everywhere."""
    view = persistent_view(
        action_button(bot, "find", style=discord.ButtonStyle.success, row=0),
        action_button(bot, "partner_posts", style=discord.ButtonStyle.success, row=0),
    )
    return templates.render(bot.runtime, "looking_panel"), view


def perks_panel(bot: ParleyBot) -> tuple[str, discord.ui.View]:
    """Read-only How Parley Works guide with only the useful next actions."""
    directory_url = _hub_channel_url(bot, bot.runtime.hub.listings_channel_id)
    support_url = _hub_channel_url(bot, bot.runtime.hub.support_channel_id)
    view = persistent_view(
        action_button(bot, "find", style=discord.ButtonStyle.success, row=0),
        discord.ui.Button(label="Server Directory", url=directory_url, row=0) if directory_url else None,
        add_bot_button(bot, row=0),
        discord.ui.Button(label="Support", url=support_url, row=1) if support_url else None,
    )
    return templates.render(bot.runtime, "perks"), view


def parley_perks_panel(bot: ParleyBot) -> tuple[str, discord.ui.View]:
    """Read-only benefits page for servers that add Parley."""
    how_url = _hub_channel_url(bot, bot.runtime.hub.perks_channel_id)
    view = persistent_view(
        add_bot_button(bot, row=0),
        discord.ui.Button(label="How Parley Works", url=how_url, row=0) if how_url else None,
    )
    return templates.render(bot.runtime, "benefits"), view


def _hub_channel_url(bot: ParleyBot, channel_id: int | None) -> str | None:
    guild_id = bot.runtime.hub.main_guild_id
    if not guild_id or not channel_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def welcome_panel(bot: ParleyBot) -> tuple[str, discord.ui.View]:
    """Start Here: one obvious partnership CTA, with directory tools secondary."""
    directory_url = _hub_channel_url(bot, bot.runtime.hub.listings_channel_id)
    view = persistent_view(
        add_bot_button(bot, row=0),
        action_button(bot, "find", style=discord.ButtonStyle.success, row=0),
        discord.ui.Button(label="Server Directory", url=directory_url, row=0) if directory_url else None,
        action_button(bot, "post", style=discord.ButtonStyle.primary, row=1),
    )
    return templates.render(bot.runtime, "welcome"), view


@register_action("network_help")
async def network_help(interaction: discord.Interaction) -> None:
    """Explain the Network before offering setup."""
    bot = get_bot(interaction)
    connected = [
        guild for guild in await permissions.manageable_guilds(bot, interaction.user.id)
        if guild.id != bot.runtime.hub.main_guild_id
    ]
    text = templates.render(bot.runtime, "network_help")
    if not connected:
        text += (
            "\n\n**Ready to use it?**\n"
            "Add Parley to the server you want to represent, then check your DMs and finish setup first."
        )
    view = persistent_view(
        action_button(bot, "network", row=0) if connected else add_bot_button(bot, row=0),
        home_button(bot, row=1),
    )
    await show_screen(interaction, text, view=view)


def join_message(bot: ParleyBot, guild: discord.Guild | None = None) -> tuple[discord.Embed, discord.ui.View]:
    """Network-first first-run message sent when Parley is added to a server."""
    description = templates.render(
        bot.runtime, "join_message", server_name=guild.name if guild else "This server"
    )
    embed = discord.Embed(
        title="Parley is connected",
        description=description,
        color=bot.runtime.bot.color_primary,
    )
    embed.set_footer(text="Only server managers can change Network or listing settings.")

    view = discord.ui.View(timeout=None)
    find = action_button(bot, "find", style=discord.ButtonStyle.success, row=0)
    find.item.label = "Find Partners"
    find.item.emoji = "🤝"
    view.add_item(find)

    post = action_button(bot, "post", style=discord.ButtonStyle.primary, row=0)
    post.item.label = "Post Server Ad"
    post.item.emoji = None
    view.add_item(post)

    how = action_button(bot, "network_help", style=discord.ButtonStyle.secondary, row=1)
    how.item.label = "How It Works"
    how.item.emoji = None
    view.add_item(how)

    if not bot.runtime.hub.configured:
        setup = action_button(bot, "setup", style=discord.ButtonStyle.secondary, row=1)
        setup.item.label = "Set Up Parley Hub"
        setup.item.emoji = None
        view.add_item(setup)
    return embed, view


async def send_control_panel(interaction: discord.Interaction, bot: ParleyBot) -> None:
    content, view = await personalize_control_panel(
        bot, interaction.user.id, staff=await permissions.is_staff_cached(bot, interaction.user.id)
    )
    await reply(interaction, content, view=view)


def _can_edit_in_place(interaction: discord.Interaction) -> bool:
    """Menus in DMs and ephemeral menus can be replaced instead of posting a new message."""
    message = interaction.message
    if message is None:
        return False
    return interaction.guild is None or message.flags.ephemeral


async def show_screen(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    view: discord.ui.View | None = None,
    embed: discord.Embed | None = None,
) -> None:
    """Replace the current menu when possible (keeps DMs clean), else reply.

    Top-level persistent buttons are deferred immediately so they cannot hit
    Discord's short interaction deadline. A deferred menu should complete that
    same response instead of creating a second follow-up message.
    """
    kwargs = {"content": content, "view": view, "allowed_mentions": safe_allowed_mentions()}
    if embed is None:
        kwargs["embeds"] = []
    else:
        kwargs["embed"] = embed
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
        return
    if _can_edit_in_place(interaction):
        await edit_response(interaction, **kwargs)
        return
    await reply(interaction, content, embed=embed, view=view)


class DirectoryOverviewView(OwnedView):
    """Private listing dashboard for every server the opener manages.

    Directory Overview is intentionally an owner tool rather than another copy
    of Find Partners. It shows all of the user's connected/listed servers in one
    calm dashboard and routes each selection to that server's management page.
    """

    PAGE_SIZE = 5

    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        guilds: list[discord.Guild],
        listings: dict[int, object],
        *,
        page: int = 0,
        connected_ids: set[int] | None = None,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.guilds = sorted(guilds, key=lambda g: g.name.lower())
        self.listings = listings
        # Normal callers/tests pass live guilds only. The production overview
        # supplies connected_ids when it mixes stored disconnected listings in.
        self.connected_ids = set(connected_ids) if connected_ids is not None else {g.id for g in guilds}
        self.page = max(0, page)
        self._build_controls()

    @property
    def listed_guilds(self) -> list[discord.Guild]:
        return [
            guild
            for guild in self.guilds
            if (listing := self.listings.get(guild.id)) is not None
            and getattr(listing, "status", None) != ListingStatus.REMOVED
        ]

    @property
    def pages(self) -> int:
        return max(1, (len(self.listed_guilds) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _page_guilds(self) -> list[discord.Guild]:
        self.page = min(self.page, self.pages - 1)
        start = self.page * self.PAGE_SIZE
        return self.listed_guilds[start : start + self.PAGE_SIZE]

    @staticmethod
    def _status(listing) -> tuple[str, str]:
        if listing.pending_changes:
            return "🟡", "Edit waiting for review"
        return {
            ListingStatus.ACTIVE: ("🟢", "Live"),
            ListingStatus.PENDING: ("🟡", "Waiting for review"),
            ListingStatus.SUSPENDED: ("🔴", "Suspended"),
            ListingStatus.EXPIRED: ("⚪", "Expired"),
        }.get(listing.status, ("⚪", str(listing.status).title()))

    def _build_controls(self) -> None:
        self.clear_items()
        page_guilds = self._page_guilds()
        if page_guilds:
            options: list[discord.SelectOption] = []
            for guild in page_guilds:
                listing = self.listings[guild.id]
                _icon, state = self._status(listing)
                partnership = "Partnerships on" if listing.accepting_partnerships else "Partnerships off"
                options.append(
                    discord.SelectOption(
                        label=truncate(guild.name, 100),
                        description=truncate(f"{state} · {partnership}", 100),
                        value=str(guild.id),
                    )
                )
            select = discord.ui.Select(placeholder="Choose a listing to manage", options=options, row=0)
            select.callback = self._picked  # type: ignore[method-assign]
            self._select = select
            self.add_item(select)

        if self.pages > 1:
            previous = discord.ui.Button(
                label="Previous", style=discord.ButtonStyle.secondary, row=1, disabled=self.page <= 0
            )
            next_button = discord.ui.Button(
                label="Next", style=discord.ButtonStyle.secondary, row=1, disabled=self.page >= self.pages - 1
            )
            previous.callback = self._previous_page  # type: ignore[method-assign]
            next_button.callback = self._next_page  # type: ignore[method-assign]
            self.add_item(previous)
            self.add_item(next_button)

        self.add_item(action_button(self.bot, "post", row=2))
        self.add_item(action_button(self.bot, "find", row=2))
        self.add_item(action_button(self.bot, "home", style=discord.ButtonStyle.secondary, row=3))

    def render(self) -> discord.Embed:
        listings = [self.listings[g.id] for g in self.listed_guilds]
        connected = sum(1 for guild in self.guilds if guild.id in self.connected_ids)
        connected_listed = sum(1 for guild in self.listed_guilds if guild.id in self.connected_ids)
        unlisted = max(0, connected - connected_listed)
        partnership_open = sum(1 for listing in listings if listing.accepting_partnerships)
        ready_relist = sum(
            1
            for listing in listings
            if listing.status in (ListingStatus.ACTIVE, ListingStatus.EXPIRED)
            and (
                listing.status == ListingStatus.EXPIRED
                or listing_service.refresh_remaining(
                    listing, self.bot.runtime, utcnow(), connected=listing.guild_id in self.connected_ids
                ) is None
            )
        )

        description = (
            "Manage your listings from one place. Choose a server below to edit its ad, "
            "change partnership settings, relist it, or open the live ad."
        )
        if unlisted:
            suffix = "s" if unlisted != 1 else ""
            description += f"\n\nYou also manage **{unlisted}** connected server{suffix} that are not listed yet."

        embed = discord.Embed(
            title="🧭 Directory Overview",
            description=description,
            color=self.bot.runtime.bot.color_primary,
        )
        embed.add_field(name="📣 Listings", value=f"**{len(listings):,}**", inline=True)
        embed.add_field(name="🤝 Partnerships", value=f"**{partnership_open:,} open**", inline=True)
        embed.add_field(name="↻ Ready to relist", value=f"**{ready_relist:,}**", inline=True)

        page_guilds = self._page_guilds()
        if not page_guilds:
            embed.add_field(
                name="Nothing listed yet",
                value="Use **Post My Server** to publish your first server.",
                inline=False,
            )
        else:
            for guild in page_guilds:
                listing = self.listings[guild.id]
                icon, state = self._status(listing)
                categories = ", ".join(listing.categories) or "Other"
                partnership = (
                    f"🤝 Open · {listing.minimum_members:,}+ minimum"
                    if listing.accepting_partnerships and listing.minimum_members
                    else "🤝 Open · any size"
                    if listing.accepting_partnerships
                    else "Partnerships off"
                )
                if listing.status == ListingStatus.ACTIVE:
                    remaining = listing_service.refresh_remaining(
                        listing, self.bot.runtime, utcnow(), connected=listing.guild_id in self.connected_ids
                    )
                    relist = f"Relist in {format_duration(remaining)}" if remaining is not None else "Relist ready"
                elif listing.status == ListingStatus.EXPIRED:
                    relist = "Relist ready"
                else:
                    relist = state
                url = listing_jump_url(self.bot.runtime.hub.main_guild_id, listing.channel_id, listing.message_id)
                ad = f"[Open live ad]({url})" if url else "Ad is not currently published"
                embed.add_field(
                    name=f"{icon} {truncate(guild.name, 220)}",
                    value=(
                        f"{categories} · {format_members(guild.member_count or 0)}\n"
                        f"{partnership} · {relist}\n"
                        f"{ad}"
                    ),
                    inline=False,
                )

        footer = f"Page {self.page + 1} of {self.pages} · Select a server to open its controls"
        if connected:
            footer += f" · {connected} connected"
        embed.set_footer(text=footer)
        return embed

    async def _picked(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        from bot.views.management import show_management

        await show_management(interaction, int(self._select.values[0]))

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._build_controls()
        await edit_response(interaction, content=None, embed=self.render(), view=self)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        self._build_controls()
        await edit_response(interaction, content=None, embed=self.render(), view=self)


@register_action("directory")
async def directory_overview(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    live = {
        guild.id: guild
        for guild in await permissions.manageable_guilds(bot, interaction.user.id)
        if guild.id != bot.runtime.hub.main_guild_id
    }
    async with bot.db.session() as session:
        delegated_ids = set(await repository.guild_ids_connected_by(session, interaction.user.id))
        guild_ids = set(live) | delegated_ids
        rows = await repository.get_listings(session, guild_ids) if guild_ids else []
        stored = await repository.get_guilds(session, guild_ids) if guild_ids else {}

    guilds: list[object] = list(live.values())
    for guild_id in sorted(delegated_ids - set(live)):
        known = stored.get(guild_id)
        if known is None:
            continue
        guilds.append(
            SimpleNamespace(
                id=known.guild_id,
                name=known.name,
                member_count=known.member_count,
                icon=SimpleNamespace(url=known.icon_url) if known.icon_url else None,
            )
        )

    listings = {row.guild_id: row for row in rows if row.status != ListingStatus.REMOVED}
    view = DirectoryOverviewView(
        bot, interaction.user.id, guilds, listings, connected_ids=set(live)
    )
    await show_screen(interaction, embed=view.render(), view=view)


@register_action("home")
async def go_home(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    content, view = await personalize_control_panel(
        bot, interaction.user.id, staff=await permissions.is_staff_cached(bot, interaction.user.id)
    )
    await show_screen(interaction, content, view=view)


@register_action("how")
async def how_it_works(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    text = templates.render(bot.runtime, "help")
    links = optional_links(bot)
    if bot.runtime.bot.support_url:
        text += "\n" + templates.render(bot.runtime, "support")
    await reply(interaction, text, view=persistent_view(add_bot_button(bot), *links) if (links or invite_url(bot)) else None)
