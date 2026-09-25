from __future__ import annotations

import discord

from bot.database.models import Listing
from bot.views.partner_posts import PartnerPostManager, partner_post_controls
from tests.fakes import USER_ID, FakeBot


def _labels(view):
    return [getattr(getattr(c, "item", c), "label", None) for c in view.children]


def test_partner_post_public_actions_are_distinct_from_server_directory(db):
    bot = FakeBot(db)
    view = partner_post_controls(bot, 123)
    assert _labels(view) == ["View Server", "Request Partnership"]
    buttons = [getattr(c, "item", c) for c in view.children]
    assert buttons[1].style is discord.ButtonStyle.success


def test_partner_post_manager_switches_between_post_and_edit_delete(db):
    bot = FakeBot(db)
    guild = bot.guild

    empty = Listing(guild_id=guild.id, advertisement_text="server ad", category="Gaming")
    assert _labels(PartnerPostManager(bot, USER_ID, guild, empty)) == ["Post to Partner Board", "Home"]

    live = Listing(
        guild_id=guild.id,
        advertisement_text="server ad",
        category="Gaming",
        partner_ad_text="Looking for anime communities",
        partner_channel_id=10,
        partner_message_id=11,
    )
    assert _labels(PartnerPostManager(bot, USER_ID, guild, live)) == [
        "Edit Post", "View Post", "Delete Post", "Home"
    ]

def test_partner_board_actions_stay_simple(db):
    from bot.views.welcome import looking_panel

    bot = FakeBot(db)
    content, view = looking_panel(bot)
    assert "Partner Board" in content
    assert _labels(view) == ["Find Partners", "My Partner Posts"]
