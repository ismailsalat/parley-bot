from __future__ import annotations

from types import SimpleNamespace

from bot.database.models import Listing, ListingStatus
from bot.views.welcome import DirectoryOverviewView
from tests.fakes import FakeBot, USER_ID


def _labels(view):
    return [getattr(getattr(item, "item", item), "label", None) for item in view.children]


def test_directory_overview_is_a_multi_server_management_dashboard(db):
    bot = FakeBot(db)
    guilds = [
        SimpleNamespace(id=101, name="Alpha", member_count=120),
        SimpleNamespace(id=202, name="Beta", member_count=340),
        SimpleNamespace(id=303, name="Unlisted", member_count=20),
    ]
    listings = {
        101: Listing(
            guild_id=101,
            advertisement_text="Alpha ad",
            category="Gaming",
            accepting_partnerships=True,
            minimum_members=50,
            status=ListingStatus.ACTIVE,
            channel_id=10,
            message_id=11,
        ),
        202: Listing(
            guild_id=202,
            advertisement_text="Beta ad",
            category="Social",
            accepting_partnerships=False,
            minimum_members=0,
            status=ListingStatus.EXPIRED,
        ),
    }

    view = DirectoryOverviewView(bot, USER_ID, guilds, listings)
    embed = view.render()
    assert embed.title == "🧭 Directory Overview"
    assert "connected server" in (embed.description or "")
    assert any(field.name.endswith("Alpha") and "Open live ad" in field.value for field in embed.fields)
    assert any(field.name.endswith("Beta") and "Partnerships off" in field.value for field in embed.fields)

    select = next(item for item in view.children if getattr(item, "placeholder", None) == "Choose a listing to manage")
    assert {option.label for option in select.options} == {"Alpha", "Beta"}
    assert _labels(view)[-3:] == ["Post Server Ad", "Browse Partners", "Home"]
