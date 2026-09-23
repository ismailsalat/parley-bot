"""Install links, optional links, DM home, join message, previews."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import discord

from bot.config.runtime import HubConfig, default_config
from bot.views.welcome import PUBLIC_PERMISSIONS, SETUP_PERMISSIONS, control_panel, invite_url, join_message, optional_links, welcome_panel


class UIBot:
    def __init__(self, runtime=None, application_id: int | None = 123456789012345678) -> None:
        self.runtime = runtime or default_config()
        self.application_id = application_id


def labels(view: discord.ui.View) -> list[str]:
    return [getattr(child, "item", child).label for child in view.children]


def urls(view: discord.ui.View) -> list[str]:
    return [child.url for child in view.children if getattr(child, "url", None)]


def test_install_link_is_generated_from_the_application_id():
    url = invite_url(UIBot())
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["123456789012345678"]
    assert set(query["scope"][0].split()) == {"bot", "applications.commands"}
    assert int(query["permissions"][0]) == PUBLIC_PERMISSIONS.value
    assert invite_url(UIBot(application_id=None)) is None  # before login: no broken link


def test_permissions_are_minimal():
    assert not PUBLIC_PERMISSIONS.administrator and not SETUP_PERMISSIONS.administrator
    assert not PUBLIC_PERMISSIONS.manage_channels  # other servers never need it
    assert SETUP_PERMISSIONS.manage_channels  # only the main server, for Automatic Setup
    assert not PUBLIC_PERMISSIONS.manage_messages and not PUBLIC_PERMISSIONS.mention_everyone


async def test_dm_home_is_simple_for_users_and_has_settings_for_staff():
    bot = UIBot()
    _content, view = control_panel(bot)
    assert labels(view) == ["Post Server Ad", "Browse Partners", "My Server Listings", "My Partner Posts", "Requests"]
    _content, staff_view = control_panel(bot, staff=True)
    assert "Settings" in labels(staff_view)


async def test_start_here_panel_stays_simple_and_optional_links_still_work_elsewhere():
    runtime = default_config()
    runtime = replace(
        runtime,
        bot=replace(runtime.bot, support_url="https://discord.gg/help", website_url="https://example.com"),
        hub=HubConfig(
            main_guild_id=1, welcome_channel_id=1, listings_channel_id=2, looking_channel_id=3, perks_channel_id=4
        ),
    )
    # Start Here routes to the channels; Partner Posts live behind Find a Partner now that they are Connected-only.
    _content, view = welcome_panel(UIBot(runtime))
    assert labels(view) == ["Server Directory", "Find a Partner", "Parley Perks", "Post Server Ad", "Add Parley"]

    # Before /setup runs there are no channel links, but the router is still usable.
    _content, bare = welcome_panel(UIBot())
    assert labels(bare) == ["Post Server Ad", "Add Parley"]

    links = optional_links(UIBot(runtime))
    assert {item.url for item in links} == {"https://discord.gg/help", "https://example.com"}
    assert optional_links(UIBot()) == []


async def test_join_message_is_a_simple_connected_card():
    bot = UIBot(replace(default_config(), hub=HubConfig(main_guild_id=1)))
    embed, view = join_message(bot)
    assert embed.title == "Parley is connected"
    assert "connected to Parley" in (embed.description or "")
    assert "Nothing is posted automatically" in (embed.description or "")
    assert labels(view) == ["List This Server"]

    _embed, fresh = join_message(UIBot())  # fresh owner can set up the main hub here
    assert labels(fresh) == ["List This Server", "Set Up Parley"]
    assert "setup" in [getattr(c, "item", c).custom_id.split(":")[-1] for c in fresh.children]


async def test_custom_labels_show_everywhere():
    runtime = default_config()
    buttons = {**runtime.panels.buttons, "post": {"label": "Advertise", "emoji": "🚀"}}
    runtime = replace(runtime, panels=replace(runtime.panels, buttons=buttons))
    _content, view = control_panel(UIBot(runtime))
    assert labels(view)[0] == "Advertise"
    assert getattr(view.children[0], "item").custom_id == "wp:act:post"


def test_preview_is_the_exact_ad():
    from bot.views.listings import PREVIEW_HEADER, preview_content

    ad = "## Hi @everyone\n**bold** <:e:123>"
    assert preview_content(ad) == PREVIEW_HEADER + ad


async def test_network_ad_is_plain_message_with_footer_and_buttons():
    from bot.database.models import Listing
    from bot.tasks import network_ad_kwargs

    listing = Listing(guild_id=1, advertisement_text="## Ad @everyone", accepting_partnerships=True, invite_url="https://discord.gg/x")
    kwargs = network_ad_kwargs(UIBot(), listing)
    assert kwargs["content"].startswith("## Ad @everyone\n-# 🌐 Shared by the Parley network")
    assert "embed" not in kwargs and kwargs["allowed_mentions"].to_dict()["parse"] == []
    assert labels(kwargs["view"]) == ["Join Server", "Request Partnership", "Add Parley"]


async def test_test_listing_is_labelled():
    from bot.database.models import Listing
    from bot.views.partnership import listing_message_kwargs

    listing = Listing(guild_id=1, advertisement_text="Ad", accepting_partnerships=False, invite_url=None, is_test=True)
    assert "🧪 TEST" in listing_message_kwargs(UIBot(), listing)["content"]


def test_default_management_view_label_is_view_ad():
    assert default_config().panels.buttons["preview"]["label"] == "View Ad"
