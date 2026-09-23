from __future__ import annotations

from bot.database.models import Listing
from bot.views.self_post import directory_card_kwargs
from bot.views.welcome import listings_panel
from tests.fakes import FakeBot


def _labels(view):
    return [getattr(getattr(c, "item", c), "label", None) for c in view.children]


def test_public_directory_footer_is_small(db):
    bot = FakeBot(db)
    _content, view = listings_panel(bot)
    assert _labels(view) == ["Find Partners", "Post My Server", "Directory Overview", "Relist"]


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
