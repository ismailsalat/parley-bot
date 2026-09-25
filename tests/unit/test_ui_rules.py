"""Static UI audit: valid emoji, one colour system, grey navigation kept separate."""

from __future__ import annotations

import discord
import pytest

from bot.config.runtime import BUTTON_STYLES, DEFAULT_BUTTONS, default_config
from bot.services import configuration
from bot.services.errors import ValidationError
from bot.utils.emoji import is_valid_emoji
from bot.views.welcome import BUTTON_STYLE_MAP
from tests.fakes import FakeBot
from tests.unit.test_screens import make_bot, pages

PREFERRED = {
    # Keep emoji sparse on normal user workflows. These are reserved for
    # genuinely useful status/navigation cues rather than decorating every button.
    "accept": "✅",
    "decline": "❌",
    "back": "⬅️",
    "home": "🏠",
    "support": "🛟",
    "request": "🤝",
}


@pytest.mark.parametrize("key", sorted(DEFAULT_BUTTONS))
def test_every_built_in_button_emoji_is_valid(key):
    emoji = DEFAULT_BUTTONS[key]["emoji"]
    assert emoji is None or is_valid_emoji(str(emoji)), (key, emoji)
    assert DEFAULT_BUTTONS[key]["style"] in BUTTON_STYLES


@pytest.mark.parametrize(("key", "emoji"), sorted(PREFERRED.items()))
def test_preferred_emoji_are_used(key, emoji):
    assert DEFAULT_BUTTONS[key]["emoji"] == emoji


@pytest.mark.parametrize("raw", ["←", "→", "hello", "abc", "1", " ", "🔎🔍", "<:bad:1>", ":smile:", "<@123>"])
def test_invalid_emoji_are_rejected(raw):
    assert not is_valid_emoji(raw)


@pytest.mark.parametrize("raw", ["🌐", "🔍", "♻️", "▶️", "⬅️", "✅", "❌", "🛟", "<:custom:123456789012345678>",
                                 "<a:spin:123456789012345678>", "⭐", "❗"])
def test_valid_emoji_are_accepted(raw):
    assert is_valid_emoji(raw)


async def test_saving_an_invalid_emoji_is_refused(db):
    with pytest.raises(ValidationError, match="valid Discord button emoji"):
        async with db.session() as session:
            await configuration.set_button(session, "post", label="Post", emoji="←", actor_id=1)


async def test_a_stored_invalid_emoji_is_never_rendered(db):
    """Even if a bad value reaches the database, the button still renders."""
    from bot.config.runtime import apply_overrides

    config, notes = apply_overrides(default_config(), {"panels.buttons": {"post": {"label": "Post", "emoji": "←"}}})
    assert any("valid Discord button emoji" in n for n in notes)
    assert config.button("post")[1] is None


def test_button_styles_can_be_customized_within_the_system():
    config = default_config()
    assert config.button_style("post") == "primary"
    assert config.button_style("accept") == "success"
    assert config.button_style("remove") == "danger"
    assert config.button_style("home") == "secondary"
    assert set(BUTTON_STYLE_MAP) == set(BUTTON_STYLES)


def buttons(view: discord.ui.View) -> list[discord.ui.Button]:
    return [getattr(c, "item", c) for c in view.children if isinstance(getattr(c, "item", c), discord.ui.Button)]


NAVIGATION = {"Back", "Home", "Settings", "Cancel"}
ALLOWED_SECONDARY = NAVIGATION | {"Directory Overview", "Relist", "Test Utilities"}


def test_no_screen_is_overloaded(db):
    bot = make_bot(db)
    for page in pages(bot):
        page.clear_items()
        page.build()
        items = buttons(page)
        choices = [i for i in items if i.label not in NAVIGATION]
        assert len(choices) <= 6, (type(page).__name__, [i.label for i in choices])
        rows: dict[int, list[discord.ui.Button]] = {}
        for item in items:
            rows.setdefault(item.row or 0, []).append(item)
        for row, row_items in rows.items():
            assert len(row_items) <= 5
            styles = [i.style for i in row_items]
            # grey navigation never sits between coloured actions
            for index, style in enumerate(styles):
                if style is discord.ButtonStyle.secondary:
                    assert all(s is discord.ButtonStyle.secondary for s in styles[index:]), (type(page).__name__, row)


def test_navigation_is_on_the_last_row(db):
    bot = make_bot(db)
    for page in pages(bot):
        page.clear_items()
        page.build()
        items = buttons(page)
        nav = [i for i in items if i.label in ("Back", "Home", "Settings", "Cancel")]
        if not nav or not items:
            continue
        last_row = max((i.row or 0) for i in items)
        assert all((i.row or 0) == last_row for i in nav), type(page).__name__


def test_grey_is_navigation_only(db):
    """Secondary style is reserved for Back / Home / Cancel."""
    bot = make_bot(db)
    for page in pages(bot):
        page.clear_items()
        page.build()
        for item in buttons(page):
            if item.style is discord.ButtonStyle.secondary:
                assert item.label in ALLOWED_SECONDARY, (type(page).__name__, item.label)


def test_no_built_in_button_is_grey_unless_it_navigates():
    for key, spec in DEFAULT_BUTTONS.items():
        if spec["style"] == "secondary":
            assert key in ("back", "home", "directory", "refresh", "relist"), key


def test_destructive_staff_actions_are_red():
    from bot.views.listings import ReviewButton

    assert ReviewButton(1, "ban").item.style is discord.ButtonStyle.danger
    assert ReviewButton(1, "reject", 1).item.style is discord.ButtonStyle.danger
    assert ReviewButton(1, "approve", 1).item.style is discord.ButtonStyle.success


def test_emoji_used_on_screens_are_valid(db):
    bot = make_bot(db)
    for page in pages(bot):
        page.clear_items()
        page.build()
        for item in page.children:
            emoji = getattr(getattr(item, "item", item), "emoji", None)
            if emoji is not None:
                assert is_valid_emoji(str(emoji)), (type(page).__name__, str(emoji))
            for option in getattr(getattr(item, "item", item), "options", []) or []:
                if option.emoji is not None:
                    assert is_valid_emoji(str(option.emoji))


async def test_user_screens_use_valid_emoji_and_colours(db):
    """The DM home, panels and listing buttons users actually see."""
    from bot.database.models import Listing
    from bot.views.partnership import listing_message_kwargs
    from bot.views.welcome import control_panel, listings_panel, looking_panel, welcome_panel

    bot = FakeBot(db)
    bot.application_id = 123456789012345678
    listing = Listing(guild_id=1, advertisement_text="Ad", accepting_partnerships=True, invite_url="https://discord.gg/x")
    views = [control_panel(bot)[1], control_panel(bot, staff=True)[1], listings_panel(bot)[1],
             looking_panel(bot)[1], welcome_panel(bot)[1], listing_message_kwargs(bot, listing)["view"]]
    for view in views:
        items = buttons(view)
        assert 2 <= len(items) <= 6, len(items)
        for item in items:
            if item.emoji is not None:
                assert is_valid_emoji(str(item.emoji))
            if item.style is discord.ButtonStyle.success:
                assert item.label == "Find Partners"  # the single highlighted public CTA
