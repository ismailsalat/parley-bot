"""Settings -> Moderation: search, suspended/banned servers, blocked users, approval queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import Listing, ListingStatus
from bot.services import moderation
from bot.services.errors import ValidationError
from bot.utils.helpers import format_members, format_minimum, truncate
from bot.views.admin.common import ConfirmPage, Field, FieldsModal, Page

if TYPE_CHECKING:
    from bot.core import WaypointBot


async def listing_embed(bot: WaypointBot, guild_id: int) -> tuple[discord.Embed, Listing | None, bool]:
    """Staff view of one server: listing, contacts, ban, network, recent activity."""
    async with bot.db.session() as session:
        listing = await repository.get_listing(session, guild_id)
        guild = await repository.get_guild(session, guild_id)
        contacts = await repository.get_contact_ids(session, guild_id, enabled_only=False)
        ban = await repository.get_ban(session, moderation.GUILD, guild_id)
        net = await repository.get_network_settings(session, guild_id)
        audit = await repository.recent_audit(session, guild_id)

    embed = discord.Embed(title=guild.name if guild else f"Server {guild_id}", color=bot.runtime.bot.color_primary)
    embed.add_field(name="Guild ID", value=f"`{guild_id}`")
    embed.add_field(name="Members", value=format_members(guild.member_count if guild else 0))
    embed.add_field(name="Banned", value=f"Yes – {ban.reason or 'no reason'}" if ban else "No")
    if listing is None:
        embed.description = "No listing."
    else:
        status = listing.status + (" · edit waiting for review" if listing.pending_changes else "")
        status += " · 🧪 test" if listing.is_test else ""
        embed.add_field(name="Listing status", value=status)
        embed.add_field(name="Category", value=", ".join(listing.categories) or "—")
        embed.add_field(
            name="Partnerships",
            value=f"Accepting · {format_minimum(listing.minimum_members)}" if listing.accepting_partnerships else "Not accepting",
        )
        embed.add_field(name="Invite", value=listing.invite_url or "—", inline=False)
        embed.add_field(name="Contacts", value=", ".join(f"<@{c}>" for c in contacts) or "—", inline=False)
        embed.add_field(
            name="Dates",
            value=(
                f"Created {discord.utils.format_dt(listing.created_at, 'R')}\n"
                f"Relisted {discord.utils.format_dt(listing.refreshed_at, 'R') if listing.refreshed_at else 'never'}"
            ),
            inline=False,
        )
    if net is not None:
        embed.add_field(
            name="Network",
            value=f"{'Enabled' if net.enabled else 'Off'} · <#{net.channel_id}>" if net.channel_id else "Off",
            inline=False,
        )
    if audit:
        embed.add_field(
            name="Recent activity",
            value=truncate("\n".join(f"{discord.utils.format_dt(a.timestamp, 'R')} `{a.action}`" for a in audit), 1000),
            inline=False,
        )
    return embed, listing, ban is not None


class ModerationPage(Page):
    title = "Moderation"

    def content(self) -> str:
        return "## Moderation\nFind a server or review what's waiting. Everything asks before it changes anything."

    def build(self) -> None:
        self.button("Search Listing", self._search, emoji="🔎", row=0)
        self.button("Approval Queue", self._list("queue"), emoji="📝", row=0)
        self.button("Suspended Servers", self._list("suspended"), emoji="⛔", row=1)
        self.button("Banned Servers", self._list("banned"), emoji="🚫", row=1)
        self.button("Blocked Users", self._list("blocked"), emoji="🙅", row=1)
        self.nav()

    def _again(self) -> ModerationPage:
        return ModerationPage(self.bot, self.owner_id, back=self.back)

    def _list(self, kind: str):
        async def callback(interaction: discord.Interaction) -> None:
            await ListPage(self.bot, self.owner_id, kind, back=self._again).show(interaction)

        return callback

    async def _search(self, interaction: discord.Interaction) -> None:
        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            async with self.bot.db.session() as session:
                found = await repository.search_guilds(session, values["query"])
            if not found:
                await self.show(inter, f"No server found for **{truncate(values['query'], 50)}**.")
                return
            if len(found) == 1:
                await ServerPage(self.bot, self.owner_id, found[0].guild_id, back=self._again).show(inter)
                return
            await ListPage(self.bot, self.owner_id, "search", back=self._again, rows=[(g.guild_id, g.name) for g in found]).show(inter)

        await interaction.response.send_modal(
            FieldsModal("Search listing", [Field("query", "Server name or ID", required=True, max_length=100)], submitted)
        )


LIST_TITLES = {
    "queue": "Approval Queue",
    "suspended": "Suspended Servers",
    "banned": "Banned Servers",
    "blocked": "Blocked Users",
    "search": "Search Results",
}


class ListPage(Page):
    def __init__(self, bot: WaypointBot, owner_id: int, kind: str, *, back, rows: list[tuple[int, str]] | None = None) -> None:
        super().__init__(bot, owner_id, back=back)
        self.kind = kind
        self.rows = rows

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        if self.rows is None or self.kind != "search":
            self.rows = await self._load()
        await super().show(interaction, notice)

    async def _load(self) -> list[tuple[int, str]]:
        async with self.bot.db.session() as session:
            if self.kind == "queue":
                listings = await repository.listings_awaiting_review(session)
            elif self.kind == "suspended":
                listings = await repository.listings_with_status(session, ListingStatus.SUSPENDED)
            else:
                listings = None
            if listings is not None:
                guilds = await repository.get_guilds(session, [l.guild_id for l in listings])
                return [
                    (l.guild_id, (guilds[l.guild_id].name if l.guild_id in guilds else str(l.guild_id))
                     + (" (edit)" if l.pending_changes else ""))
                    for l in listings
                ]
            bans = await repository.list_bans(session, "guild" if self.kind == "banned" else "user")
            if self.kind == "banned":
                guilds = await repository.get_guilds(session, [b.target_id for b in bans])
                return [(b.target_id, guilds[b.target_id].name if b.target_id in guilds else str(b.target_id)) for b in bans]
            return [(b.target_id, (self.bot.get_user(b.target_id).name if self.bot.get_user(b.target_id) else str(b.target_id))) for b in bans]

    def content(self) -> str:
        lines = [f"## {LIST_TITLES[self.kind]}"]
        if not self.rows:
            lines.append("Nothing here. 🎉")
        else:
            lines += [f"• {truncate(name, 80)} (`{rid}`)" for rid, name in self.rows[:25]]
        return "\n".join(lines)

    def build(self) -> None:
        if self.rows:
            select = discord.ui.Select(
                placeholder="Choose one",
                options=[discord.SelectOption(label=truncate(name, 100), value=str(rid)) for rid, name in self.rows[:25]],
                row=0,
            )

            async def picked(interaction: discord.Interaction) -> None:
                target = int(select.values[0])
                back = lambda: ListPage(self.bot, self.owner_id, self.kind, back=self.back, rows=self.rows)  # noqa: E731
                if self.kind == "blocked":
                    await UserPage(self.bot, self.owner_id, target, back=back).show(interaction)
                else:
                    await ServerPage(self.bot, self.owner_id, target, back=back).show(interaction)

            select.callback = picked  # type: ignore[method-assign]
            self.add_item(select)
        self.nav()


class ServerPage(Page):
    def __init__(self, bot: WaypointBot, owner_id: int, guild_id: int, *, back) -> None:
        super().__init__(bot, owner_id, back=back)
        self.guild_id = guild_id
        self._embed: discord.Embed | None = None
        self.listing: Listing | None = None
        self.banned = False

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._embed, self.listing, self.banned = await listing_embed(self.bot, self.guild_id)
        await super().show(interaction, notice)

    def content(self) -> str:
        return "## Server"

    def embed(self) -> discord.Embed | None:
        return self._embed

    def _again(self) -> ServerPage:
        return ServerPage(self.bot, self.owner_id, self.guild_id, back=self.back)

    def build(self) -> None:
        from bot.views.partnership import view_ad_button

        listing = self.listing
        if listing is not None and listing.status != ListingStatus.REMOVED:
            self.add_item(view_ad_button(self.bot, self.guild_id, row=0))
        if listing is not None and listing.awaiting_review:
            self.button("Show Review", self._review, emoji="📝", style=discord.ButtonStyle.primary, row=0)
        if listing is not None and listing.status == ListingStatus.ACTIVE:
            self.button("Suspend", self._action("suspend"), emoji="⛔", style=discord.ButtonStyle.danger, row=1)
        if listing is not None and listing.status == ListingStatus.SUSPENDED:
            self.button("Restore", self._action("restore"), emoji="✅", style=discord.ButtonStyle.success, row=1)
        if listing is not None and listing.status not in (ListingStatus.REMOVED,):
            self.button("Remove", self._action("remove"), emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
        if self.banned:
            self.button("Unban", self._action("unban"), emoji="🔓", style=discord.ButtonStyle.success, row=1)
        elif self.guild_id != self.bot.runtime.hub.main_guild_id:
            self.button("Ban Server", self._action("ban"), emoji="🚫", style=discord.ButtonStyle.danger, row=1)
        self.nav()

    async def _review(self, interaction: discord.Interaction) -> None:
        from bot.views.listings import review_payload

        assert self.listing is not None
        async with self.bot.db.session() as session:
            stored = await repository.get_guild(session, self.guild_id)
        name = stored.name if stored else str(self.guild_id)
        payload = review_payload(self.bot, self.listing, name, stored.member_count if stored else None)
        await interaction.response.send_message(ephemeral=True, **payload)

    def _action(self, action: str):
        questions = {
            "suspend": ("Hide this listing until you restore it?", "Suspend"),
            "restore": ("Show this listing again?", "Restore"),
            "remove": ("Remove this listing? The owner can post a new one later.", "Remove"),
            "ban": ("Ban this server by Guild ID? Its listing is removed and new invites can't get around it.", "Ban Server"),
            "unban": ("Lift the ban? The server can list again.", "Unban"),
        }
        question, label = questions[action]

        async def apply(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await apply_staff_action(self.bot, action, self.guild_id, interaction.user.id)
            await self._again().show(interaction, f"✅ Done: {label.lower()}.")

        async def callback(interaction: discord.Interaction) -> None:
            await ConfirmPage(
                self.bot, self.owner_id, question=question, confirm_label=label, on_confirm=apply,
                back=self._again, danger=action in ("ban", "remove", "suspend"),
            ).show(interaction)

        return callback


async def apply_staff_action(bot: WaypointBot, action: str, guild_id: int, actor_id: int) -> None:
    """Shared by the Moderation page and /admin. Discord messages follow the database change."""
    if action == "ban" and guild_id == bot.runtime.hub.main_guild_id:
        raise ValidationError("You can't ban the main Waypoint server.")
    async with bot.db.session() as session:
        listing = None
        if action == "suspend":
            listing = await moderation.suspend_listing(session, guild_id=guild_id, reason=None, moderator_id=actor_id)
        elif action == "restore":
            listing = await moderation.suspend_listing(session, guild_id=guild_id, reason=None, moderator_id=actor_id, restore=True)
        elif action == "remove":
            listing = await moderation.staff_remove_listing(session, guild_id=guild_id, reason=None, moderator_id=actor_id)
        elif action == "ban":
            listing = await moderation.ban_guild(session, guild_id=guild_id, reason=None, moderator_id=actor_id)
        elif action == "unban":
            await moderation.unban_guild(session, guild_id=guild_id, moderator_id=actor_id)
    if action == "restore":
        await bot.panels.publish_listing(guild_id)
    elif action in ("suspend", "remove", "ban"):
        await bot.panels.take_down_listing(listing)
    await bot.log_event(f"🛡️ {action.title()}: server `{guild_id}` (<@{actor_id}>).")


class UserPage(Page):
    def __init__(self, bot: WaypointBot, owner_id: int, user_id: int, *, back) -> None:
        super().__init__(bot, owner_id, back=back)
        self.user_id = user_id

    def content(self) -> str:
        return f"## Blocked user\n<@{self.user_id}> (`{self.user_id}`) can't use Waypoint."

    def build(self) -> None:
        self.button("Unblock", self._unblock, emoji="🔓", style=discord.ButtonStyle.success, row=0)
        self.nav()

    async def _unblock(self, interaction: discord.Interaction) -> None:
        async def apply(inter: discord.Interaction) -> None:
            async with self.bot.db.session() as session:
                await moderation.unblock_user(session, user_id=self.user_id, moderator_id=inter.user.id)
            await self.back().show(inter, "🔓 User unblocked.")

        await ConfirmPage(
            self.bot, self.owner_id, question="Let this user use Waypoint again?", confirm_label="Unblock",
            on_confirm=apply, back=lambda: UserPage(self.bot, self.owner_id, self.user_id, back=self.back), danger=False,
        ).show(interaction)
