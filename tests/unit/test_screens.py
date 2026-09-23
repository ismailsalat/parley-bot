"""Build and render every admin screen: catches layout errors (Discord allows
5 rows x 5 buttons) and broken content before a real admin ever clicks."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot.views.admin import categories, messages, moderation, rules, settings, setup, test_center
from bot.views.admin.common import ConfirmPage, Page
from tests.fakes import OWNER_ID, STAFF_ID, USER_ID, FakeBot, FakeInteraction


def make_bot(db):
    bot = FakeBot(db)
    bot.hub_env_fields = ()
    bot.runtime_notes = []
    guild = bot.guild
    guild.me = SimpleNamespace(guild_permissions=discord.Permissions(create_instant_invite=True, manage_channels=True))
    guild.get_channel = lambda _id: None
    guild.get_role = lambda _id: None
    guild.text_channels = []
    guild.name = "Waypoint HQ"
    return bot


def pages(bot):
    home = lambda: settings.SettingsHome(bot, OWNER_ID)  # noqa: E731
    guild = bot.guild
    yield settings.SettingsHome(bot, OWNER_ID)
    yield settings.ServerMenu(bot, OWNER_ID, back=home)
    yield settings.ToolsMenu(bot, OWNER_ID, back=home)
    yield settings.RulesMenu(bot, OWNER_ID, back=home)
    yield settings.ModePage(bot, OWNER_ID, back=home)
    yield settings.ChannelsPage(bot, OWNER_ID, back=home)
    yield settings.StaffPage(bot, OWNER_ID, back=home)
    yield settings.HealthPage(bot, OWNER_ID, back=home)
    yield messages.MessagesPage(bot, OWNER_ID, back=home)
    for key in messages.templates.TEMPLATES:
        yield messages.TemplatePage(bot, OWNER_ID, key, back=home)
    appearance = messages.AppearancePage(bot, OWNER_ID, back=home)
    yield appearance
    selected = messages.AppearancePage(bot, OWNER_ID, back=home)
    selected.selected = "post"
    yield selected
    for section in rules.SECTIONS:
        yield rules.RulesPage(bot, OWNER_ID, section, back=home)
    yield categories.CategoriesPage(bot, OWNER_ID, back=home)
    yield categories.CategoriesPage(bot, OWNER_ID, back=home, selected="Gaming")
    yield categories.RemoveCategoryPage(bot, OWNER_ID, "Gaming", 3, back=home)
    yield moderation.ModerationPage(bot, OWNER_ID, back=home)
    for kind in moderation.LIST_TITLES:
        yield moderation.ListPage(bot, OWNER_ID, kind, back=home, rows=[(1, "Example")])
    yield moderation.UserPage(bot, OWNER_ID, 5, back=home)
    yield test_center.TestCenterPage(bot, OWNER_ID, back=home)
    yield test_center.TestUtilitiesPage(bot, OWNER_ID, back=home)
    yield test_center.NetworkTestPage(bot, OWNER_ID, back=home)
    yield setup.SetupWelcome(bot, OWNER_ID, guild)
    yield setup.MoveHubPage(bot, OWNER_ID, guild, "Old HQ")
    yield setup.NoManageChannelsPage(bot, OWNER_ID, guild)
    yield setup.ChannelPicker(bot, OWNER_ID, guild, back=home)
    picker = setup.ChannelPicker(bot, OWNER_ID, guild, back=home, page=1)
    yield picker
    yield setup.ReadyPage(bot, OWNER_ID, guild)
    yield setup.GoLivePage(bot, OWNER_ID, back=home)
    yield setup.confirm_mode(bot, OWNER_ID, "off", back=home)
    yield ConfirmPage(bot, OWNER_ID, question="?", confirm_label="Yes", on_confirm=None, back=home)


async def test_every_screen_builds_within_discord_limits(db):
    bot = make_bot(db)
    count = 0
    for page in pages(bot):
        page.clear_items()
        page.build()  # discord.py raises ValueError if a row overflows
        text = page.content()
        assert text and len(text) <= 2000, type(page).__name__
        assert len(page.children) <= 25
        for child in page.children:
            label = getattr(child, "label", None)
            if label:
                assert len(label) <= 80, label
        count += 1
    assert count > 40


async def test_settings_screens_refuse_non_staff(db):
    bot = make_bot(db)
    page = settings.SettingsHome(bot, USER_ID)
    interaction = FakeInteraction(bot, USER_ID)
    assert await page.interaction_check(interaction) is False
    assert "Only Waypoint staff" in interaction.response.sent[0]["content"]


async def test_menus_belong_to_whoever_opened_them(db):
    bot = make_bot(db)
    page = settings.SettingsHome(bot, OWNER_ID)
    interaction = FakeInteraction(bot, STAFF_ID)
    assert await page.interaction_check(interaction) is False  # another admin can't click your menu
    assert await page.interaction_check(FakeInteraction(bot, OWNER_ID)) is True


@pytest.mark.parametrize("page_type", [Page])
def test_page_base_has_navigation(page_type):
    assert hasattr(page_type, "nav") and hasattr(page_type, "show")
