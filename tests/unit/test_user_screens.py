"""The screens a normal user sees: short, obvious, and never more buttons than needed."""

from __future__ import annotations

import discord

from bot.views.listings import AdModeView, EmojiWarningView, ListingDraft, ListingFormView, PreviewView, preview_content
from bot.views.management import show_management, show_my_servers
from bot.views.partnership import ANY, CategoryView, ExhaustedView, FinderView, RequestPromptView
from tests.factories import make_listing
from tests.fakes import ADMIN_ID, MAIN, FakeBot, FakeInteraction


def buttons(view) -> list[discord.ui.Button]:
    return [getattr(c, "item", c) for c in view.children if isinstance(getattr(c, "item", c), discord.ui.Button)]


def labels(view) -> list[str]:
    return [b.label for b in buttons(view)]


def sent(interaction: FakeInteraction) -> dict:
    assert interaction.response.sent, "nothing was sent"
    return interaction.response.sent[-1]


# ---------------------------------------------------------------- posting


def form(bot, *, mode="create", accepting=True) -> ListingFormView:
    draft = ListingDraft(categories=["Gaming"], accepting=accepting, contacts=[ADMIN_ID])
    return ListingFormView(bot, ADMIN_ID, MAIN, "Rivals HQ", draft, mode=mode, in_dm=False)


async def test_step_one_asks_only_what_waypoint_doesnt_know(db):
    bot = FakeBot(db)
    view = form(bot)
    placeholders = [getattr(c, "placeholder", None) for c in view.children]
    assert "Category" in placeholders and "Partnership status" in placeholders
    assert labels(view) == ["Next"]  # one obvious next step
    assert "Rivals HQ" in view.render()
    assert len(view.render().splitlines()) <= 3  # no wall of text


async def test_closed_partnerships_skips_minimum_and_contacts(db):
    bot = FakeBot(db)
    open_view, closed_view = form(bot, accepting=True), form(bot, accepting=False)
    assert len(open_view.children) == 4  # category, partnerships, partner size, Next
    assert len(closed_view.children) == 3  # category, partnerships, Next
    assert not any(isinstance(c, discord.ui.UserSelect) for c in open_view.children)  # creator is default contact


async def test_basics_controls_explain_themselves(db):
    bot = FakeBot(db)
    view = form(bot, accepting=True)
    partnership = next(c for c in view.children if getattr(c, "placeholder", None) == "Partnership status")
    minimum = next(c for c in view.children if getattr(c, "placeholder", None) == "Minimum partner size")
    assert {o.label for o in partnership.options} == {"Open to partnerships", "Not looking for partnerships"}
    assert "Any server size" in {o.label for o in minimum.options}

    edit_view = form(bot, mode="edit", accepting=True)
    contact = next(c for c in edit_view.children if isinstance(c, discord.ui.UserSelect))
    assert contact.placeholder == "Requests go to"


async def test_ad_mode_screen_offers_two_ways(db):
    bot = FakeBot(db)
    view = AdModeView(form(bot))
    assert labels(view) == ["Paste My Own Ad", "Simple Ad Builder", "Back"]
    assert "How do you want to post it?" in view.render()
    assert len(view.render().splitlines()) <= 2


async def test_preview_screen_shows_only_the_ad_and_three_buttons(db):
    bot = FakeBot(db)
    wizard = form(bot)
    wizard.draft.ad_text = "## My Server\nCome join!"
    view = PreviewView(wizard)
    assert labels(view) == ["Publish", "Edit", "Back"]
    content = preview_content(wizard.draft.ad_text)
    assert content.endswith("## My Server\nCome join!")  # the ad itself, nothing stacked under it
    assert not any(isinstance(getattr(c, "item", c), discord.ui.Select) for c in view.children)


async def test_emoji_warning_offers_continue_or_edit(db):
    bot = FakeBot(db)
    view = EmojiWarningView(form(bot), [123])
    assert labels(view) == ["Continue", "Edit"]
    assert "may not show" in view.render()


# ---------------------------------------------------------------- my listing


async def test_one_server_goes_straight_to_my_listing(db, config):
    bot = FakeBot(db)
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID)
    interaction = FakeInteraction(bot, ADMIN_ID)
    await show_my_servers(interaction)  # no "choose a server" step
    message = sent(interaction)
    assert message.get("content") is None
    assert message["embed"].title == "🧭 Listing Manager"
    assert labels(message["view"]) == [
        "Edit Ad", "Edit Server Info", "View Ad", "Partnerships", "Relist", "Remove Listing", "All Listings", "Home"
    ]


async def test_my_listing_is_a_short_summary(db, config):
    bot = FakeBot(db)
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID, members=530)
    interaction = FakeInteraction(bot, ADMIN_ID)
    await show_management(interaction, MAIN)
    message = sent(interaction)
    embed = message["embed"]
    assert embed.title == "🧭 Listing Manager"
    assert "Parley HQ" in (embed.description or "") and "Gaming" in (embed.description or "")
    assert any("Partnerships" in field.name and "Open" in field.value for field in embed.fields)
    assert any("Relist" in field.name for field in embed.fields)
    assert "Invite" not in str(embed.to_dict()) and "Contacts" not in str(embed.to_dict())


async def test_edit_reveals_its_options_progressively(db, config):
    from bot.views.management import show_edit_menu

    bot = FakeBot(db)
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID)
    interaction = FakeInteraction(bot, ADMIN_ID)
    await show_edit_menu(interaction, MAIN)
    message = sent(interaction)
    assert labels(message["view"]) == ["Edit Ad", "Edit Server Info", "Back"]


# ---------------------------------------------------------------- finding partners


async def test_category_screen_uses_buttons_for_a_few_categories(db):
    from dataclasses import replace

    bot = FakeBot(db)
    bot.runtime = replace(bot.runtime, listings=replace(bot.runtime.listings, categories=("Gaming", "Anime")))
    view = CategoryView(bot, ADMIN_ID, source_id=None)
    assert labels(view) == ["Gaming", "Anime", ANY]


async def test_many_categories_use_a_menu_instead_of_a_wall_of_buttons(db):
    from dataclasses import replace

    bot = FakeBot(db)
    many = tuple(f"Category {i}" for i in range(12))
    bot.runtime = replace(bot.runtime, listings=replace(bot.runtime.listings, categories=many))
    view = CategoryView(bot, ADMIN_ID, source_id=None)
    assert not buttons(view)
    assert len(view.children) == 1 and isinstance(view.children[0], discord.ui.Select)


async def test_one_result_at_a_time(db, config):
    bot = FakeBot(db)
    async with db.session() as session:
        await make_listing(session, config, 1, members=742)
    view = FinderView(bot, ADMIN_ID, category=ANY, source_id=None)
    interaction = FakeInteraction(bot, ADMIN_ID)
    await view.show(interaction)
    message = sent(interaction)
    assert labels(message["view"]) == ["Send Partner Request", "Next Match", "View Server Ad", "Home"]
    assert message.get("content") is None
    assert message["embed"].title == "Server 1"
    assert "Gaming • 742 members" in message["embed"].description
    assert "Partner size: Any server size" in message["embed"].description
    assert message["embed"].fields == []
    assert len(view.seen) == 1


async def test_exhausted_screen_offers_a_way_forward(db):
    bot = FakeBot(db)
    view = ExhaustedView(bot, ADMIN_ID, category="Gaming", source_id=None, seen={1})
    assert labels(view) == ["Try Another Category", "Show Again"]
    assert view.render(first=False).startswith("You've seen every new **Gaming** partner available right now.")
    assert "Gaming" in view.render(first=True)


async def test_empty_search_says_so_instead_of_looping(db, config):
    bot = FakeBot(db)
    view = FinderView(bot, ADMIN_ID, category="Gaming", source_id=None)
    interaction = FakeInteraction(bot, ADMIN_ID)
    await view.show(interaction)
    message = sent(interaction)
    assert message.get("content") is None
    assert message["embed"].title == "❌ No servers found"
    assert "No servers are looking for partners" in message["embed"].description


async def test_request_prompt_does_not_require_a_message(db):
    bot = FakeBot(db)
    finder = FinderView(bot, ADMIN_ID, category=ANY, source_id=MAIN)
    view = RequestPromptView.for_finder(finder, 5, "Night Owls")
    assert labels(view) == ["Send Request", "Add Message", "Back"]

    # From a public listing there is nothing to go back to, so Back is dropped.
    from_listing = RequestPromptView(bot, ADMIN_ID, MAIN, 5, "Night Owls")
    assert labels(from_listing) == ["Send Request", "Add Message"]


async def test_multiple_servers_use_one_picker_not_button_grid(db, config):
    from tests.fakes import member

    bot = FakeBot(db)
    second_id = MAIN + 99
    second = type(bot.guild)(second_id, members={ADMIN_ID: member(ADMIN_ID, admin=True)})
    second.name = "Second Server"
    bot.guilds.append(second)
    bot.get_guild = lambda gid: next((g for g in bot.guilds if g.id == gid), None)
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID)
        await make_listing(session, config, second_id, actor_id=ADMIN_ID)
    interaction = FakeInteraction(bot, ADMIN_ID)
    await show_my_servers(interaction)
    message = sent(interaction)
    assert message.get("content") is None
    assert message["embed"].title == "My Servers"
    assert len(message["view"].children) == 1
    assert isinstance(message["view"].children[0], discord.ui.Select)
