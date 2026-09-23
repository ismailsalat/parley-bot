"""Settings -> Test Center: try every feature without a second community.

Nothing here reaches real users: DMs go to the admin who pressed the button,
examples are labelled 🧪 TEST, and every posted message is recorded so
**Delete Test Messages** removes it again.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.config import templates
from bot.database.models import Listing
from bot.services import permissions, testmode
from bot.services.errors import PermissionDenied
from bot.services.panels import LISTINGS_PANEL
from bot.utils.mentions import advertisement_kwargs, safe_allowed_mentions
from bot.views.admin.common import ConfirmPage, Page
from bot.views.base import OwnedView, get_bot, reply
from bot.views.welcome import ActionButton, add_bot_button, persistent_view, register_action

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

EXAMPLE_SERVER = "Example Café"
EXAMPLE_TARGET = "Night Owls"


def sample_listing(invite_url: str | None = None) -> Listing:
    return Listing(
        guild_id=0, advertisement_text=testmode.SAMPLE_AD, accepting_partnerships=True, invite_url=invite_url,
        category="Community", is_test=True,
    )


async def main_invite(bot: ParleyBot) -> str | None:
    from bot.views.listings import create_invite

    guild = bot.get_guild(bot.runtime.hub.main_guild_id) if bot.runtime.hub.main_guild_id else None
    return await create_invite(guild) if guild else None


async def record(bot: ParleyBot, message: discord.Message, kind: str) -> None:
    async with bot.db.session() as session:
        await testmode.record_message(session, channel_id=message.channel.id, message_id=message.id, kind=kind)


def test_listing_view(bot: ParleyBot, invite: str | None) -> discord.ui.View:
    """Looks exactly like a real listing's buttons; Request Partnership runs the simulation."""
    items: list[discord.ui.Item] = []
    if invite:
        label, emoji = bot.runtime.button("join")
        items.append(discord.ui.Button(label=label, emoji=emoji, url=invite))
    label, emoji = bot.runtime.button("request")
    items.append(ActionButton("test_request", label=label, emoji=emoji, style=discord.ButtonStyle.success))
    return persistent_view(*items)


async def post_test_listing(bot: ParleyBot, actor_id: int) -> str:
    channel = bot.panels.listings_channel()
    if channel is None:
        return "⚠️ Choose a listings channel first (Settings → Channels)."
    invite = await main_invite(bot)
    kwargs = advertisement_kwargs(testmode.SAMPLE_AD + "\n" + testmode.TEST_LISTING_NOTE)
    try:
        message = await bot.panels.post_above_panel(channel, LISTINGS_PANEL, view=test_listing_view(bot, invite), **kwargs)
    except discord.Forbidden:
        return f"❌ Parley can't post in {channel.mention}. Check its permissions there."
    await record(bot, message, "listing")
    note = f"✅ Test listing posted: {message.jump_url} (the panel moved under it)."
    if not invite:
        note += "\n⚠️ No invite could be created, so **Join Server** is hidden. Check **Create Invite**."
    return note


async def simulate_partnership(bot: ParleyBot, user: discord.abc.User) -> str:
    """Send the admin the exact request DM, with working Accept/Decline that only simulate."""
    embed = discord.Embed(
        title="New Partnership Request",
        description=templates.render(
            bot.runtime, "request_received", requester_server=EXAMPLE_SERVER, target_server=EXAMPLE_TARGET,
            member_count="1,234", category="Community",
        ),
        color=bot.runtime.bot.color_primary,
    )
    embed.add_field(name="Message", value="We'd love to partner! 🎉", inline=False)
    embed.set_footer(text="🧪 TEST · simulated request, nobody else was contacted")
    try:
        await user.send(embed=embed, view=SimulatedRequestView(bot, user.id), allowed_mentions=safe_allowed_mentions())
    except discord.HTTPException as exc:
        log.info("Test partnership DM failed for %s: %s", user.id, exc)
        return "❌ I couldn't DM you. Allow DMs from server members (Privacy Settings) and try again."
    return "✅ Check your DMs: you received a simulated request. Press Accept or Decline there."


class SimulatedRequestView(OwnedView):
    def __init__(self, bot: ParleyBot, owner_id: int) -> None:
        super().__init__(owner_id, timeout=900)
        self.bot = bot

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        names = {"requester_server": EXAMPLE_SERVER, "target_server": EXAMPLE_TARGET}
        text = templates.render(self.bot.runtime, "request_accepted", **names)
        text += f"\n\n**{EXAMPLE_TARGET}**\n{interaction.user.mention}\n\n**{EXAMPLE_SERVER}**\n{interaction.user.mention}"
        await self._finish(interaction, "Both servers would receive:", text)

    @discord.ui.button(label="Decline", emoji="✖️", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        names = {"requester_server": EXAMPLE_SERVER, "target_server": EXAMPLE_TARGET}
        hours = self.bot.runtime.partnerships.decline_cooldown_hours
        text = templates.render(self.bot.runtime, "request_declined", **names)
        if hours:
            text += f"\nYou can send another request in {hours} hours."
        await self._finish(interaction, "The requesting server would receive:", text)

    async def _finish(self, interaction: discord.Interaction, intro: str, text: str) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)
        await interaction.followup.send(f"-# 🧪 TEST · {intro}\n{text}"[:2000], allowed_mentions=safe_allowed_mentions())


@register_action("test_request")
async def test_request_button(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    if not await permissions.is_staff(bot, interaction.user.id):
        raise PermissionDenied("This is a Parley test listing. It's only for staff testing.")
    await reply(interaction, await simulate_partnership(bot, interaction.user))


class TestCenterPage(Page):
    title = "Test Center"

    def content(self) -> str:
        return (
            "## Test Parley\n"
            "Run realistic tests without waiting on normal user timing. Test-only cleanup lives under **Test Utilities**."
        )

    def build(self) -> None:
        tests = [
            ("Test Ad", "📢", self._listing),
            ("Test Partner Search", "🔎", self._search),
            ("Test Partnership", "🤝", self._partnership),
            ("Test Network", "🌐", self._network),
            ("Health Check", "🩺", self._health),
        ]
        for label, emoji, callback in tests:
            self.button(label, callback, emoji=emoji, row=0 if len(self.children) < 3 else 1)
        self.button("Test Utilities", self._utilities, emoji="🧪", style=discord.ButtonStyle.secondary, row=2)
        self.nav()

    async def _search(self, interaction: discord.Interaction) -> None:
        from bot.views.partnership import CategoryView

        await CategoryView(self.bot, interaction.user.id, source_id=None).show(interaction)

    async def _listing(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.show(interaction, await post_test_listing(self.bot, interaction.user.id))

    async def _partnership(self, interaction: discord.Interaction) -> None:
        await self.show(interaction, await simulate_partnership(self.bot, interaction.user))

    async def _network(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self.show(interaction, "⚠️ Open the Test Center inside a server to choose where the test ad goes.")
            return
        await NetworkTestPage(
            self.bot,
            self.owner_id,
            back=lambda: TestCenterPage(self.bot, self.owner_id, back=self.back),
        ).show(interaction)

    async def _health(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.settings import HealthPage

        await HealthPage(
            self.bot,
            self.owner_id,
            back=lambda: TestCenterPage(self.bot, self.owner_id, back=self.back),
        ).show(interaction)

    async def _utilities(self, interaction: discord.Interaction) -> None:
        await TestUtilitiesPage(
            self.bot,
            self.owner_id,
            back=lambda: TestCenterPage(self.bot, self.owner_id, back=self.back),
        ).show(interaction)


class TestUtilitiesPage(Page):
    title = "Test Utilities"

    def content(self) -> str:
        return (
            "## Test Utilities\n"
            "Shortcuts for repeating flows. These tools only target **TEST** data; live listings are left alone."
        )

    def build(self) -> None:
        self.button("Reset Cooldowns", self._reset_cooldowns, emoji="⏱️", row=0)
        self.button("Delete Test Messages", self._cleanup_messages, emoji="🧹", style=discord.ButtonStyle.danger, row=0)
        self.button("Clear Test Listings", self._confirm_clear, style=discord.ButtonStyle.danger, row=1)
        self.nav()

    async def _require_test_mode(self) -> None:
        if self.bot.runtime.hub.mode != "test":
            raise PermissionDenied("Test Utilities that modify data are only available in TEST mode.")

    async def _reset_cooldowns(self, interaction: discord.Interaction) -> None:
        await self._require_test_mode()
        await interaction.response.defer()
        async with self.bot.db.session() as session:
            listings, cooldown_rows = await testmode.reset_test_cooldowns(session, actor_id=interaction.user.id)
        await self.show(
            interaction,
            f"✅ Reset relist/ad-edit timing for **{listings}** test listing(s) and cleared **{cooldown_rows}** matching cooldown row(s).",
        )

    async def _cleanup_messages(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        count = await testmode.delete_test_messages(self.bot)
        await self.bot.panels.restore_panels()
        await self.show(interaction, f"🧹 Deleted **{count}** Test Center message(s).")

    async def _confirm_clear(self, interaction: discord.Interaction) -> None:
        await self._require_test_mode()

        async def confirmed(done: discord.Interaction) -> None:
            await done.response.defer()
            count = await testmode.clear_test_listings(self.bot, actor_id=done.user.id)
            await self.bot.panels.restore_panels()
            await self.show(done, f"✅ Cleared **{count}** test listing(s). You can run the listing flow from scratch again.")

        await ConfirmPage(
            self.bot,
            self.owner_id,
            question="Delete every listing created in TEST mode? Live listings are not touched.",
            confirm_label="Clear Test Listings",
            on_confirm=confirmed,
            back=lambda: TestUtilitiesPage(self.bot, self.owner_id, back=self.back),
        ).show(interaction)


class NetworkTestPage(Page):
    title = "Test Network Ad"

    def content(self) -> str:
        return "## Test Network Ad\nChoose a channel. One example network ad will be posted there, nowhere else."

    def build(self) -> None:
        select = discord.ui.ChannelSelect(placeholder="Test destination", channel_types=[discord.ChannelType.text], row=0)

        async def picked(interaction: discord.Interaction) -> None:
            from bot.tasks import network_ad_kwargs

            channel = interaction.guild.get_channel(select.values[0].id) if interaction.guild else None
            if not isinstance(channel, discord.TextChannel):
                await self.show(interaction, "⚠️ Choose a text channel.")
                return
            try:
                invite = await main_invite(self.bot)
                kwargs = network_ad_kwargs(self.bot, sample_listing(invite))
                view = test_listing_view(self.bot, invite)
                add = add_bot_button(self.bot)
                if add is not None:
                    view.add_item(add)
                kwargs["view"] = view
                message = await channel.send(**kwargs)
            except discord.HTTPException:
                await self.show(interaction, f"❌ Parley can't post in {channel.mention}. Check its permissions.")
                return
            await record(self.bot, message, "network")
            await self.show(interaction, f"✅ Posted: {message.jump_url}")

        select.callback = picked  # type: ignore[method-assign]
        self.add_item(select)
        self.nav()
