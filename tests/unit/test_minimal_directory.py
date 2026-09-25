from __future__ import annotations

from bot.database.models import Listing
from bot.views.self_post import directory_card_kwargs
from bot.views.welcome import listings_panel, parley_perks_panel
from tests.fakes import FakeBot


def _labels(view):
    return [getattr(getattr(c, "item", c), "label", None) for c in view.children]


def test_public_directory_footer_is_small(db):
    bot = FakeBot(db)
    content, view = listings_panel(bot)
    assert _labels(view) == ["Post Server Ad", "My Server Listings", "Relist"]
    assert content == "# 📣 Server Directory\n*Browse servers, join communities, or request partnerships.*"
    buttons = [getattr(child, "item", child) for child in view.children]
    relist = buttons[-1]
    assert str(relist.emoji) == "🔄"
    assert {button.row for button in buttons} == {0}


def test_self_post_strip_is_not_a_second_embed(db):
    bot = FakeBot(db)
    listing = Listing(
        guild_id=123, advertisement_text="Ad", category="Gaming",
        accepting_partnerships=True, invite_url="https://discord.gg/x"
    )
    kwargs = directory_card_kwargs(
        bot, listing, guild_name="Rivals HQ", member_count=520, icon_url=None,
        ad_jump_url="https://discord.com/channels/1/2/3", include_view_ad=False, show_summary=False,
    )
    assert "embed" not in kwargs and "embeds" not in kwargs
    assert "content" not in kwargs  # user's ad stays visually dominant
    assert _labels(kwargs["view"]) == ["Join Server", "Request Partnership"]


def test_relist_pointer_is_one_line_not_an_embed(db):
    bot = FakeBot(db)
    listing = Listing(guild_id=123, advertisement_text="Ad", category="Gaming", accepting_partnerships=True)
    kwargs = directory_card_kwargs(
        bot, listing, guild_name="Rivals HQ", member_count=520, icon_url=None,
        ad_jump_url="https://discord.com/channels/1/2/3", include_view_ad=True, show_summary=True,
    )
    assert kwargs["content"].startswith("-# **Rivals HQ**")
    assert _labels(kwargs["view"])[0] == "View Ad"


def test_parley_perks_is_compact_and_points_to_setup(db):
    bot = FakeBot(db)
    bot.application_id = 123456789
    content, view = parley_perks_panel(bot)
    assert content.startswith("# ✦ Parley Perks")
    assert "You must add the Parley bot" in content
    assert "Automatic Partner Ads" in content
    labels = _labels(view)
    assert labels[0] == "Add Parley"
    # FakeBot has no configured How Parley Works channel, so the link is omitted safely.
    assert labels == ["Add Parley"]
