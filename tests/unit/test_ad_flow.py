"""Posting an ad: simple builder, pasted ads kept intact, link moderation, emoji warning."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bot.config.runtime import default_config
from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import listings as listing_service
from bot.services.errors import ValidationError
from tests.factories import NOW, guild, listing_input, make_listing

GID = 820000000000000001

PASTED_AD = (
    "# <:crown:123456789012345678> __ RIVALS HQ __\n\n"
    "## `10£` giveaway\n"
    "- **Nitro** drops\n"
    "- @everyone welcome\n"
    "- spacing    kept ✨ 日本語\n"
    "```py\nprint('hi')\n```\n"
    "https://discord.gg/example"
)


async def test_pasted_ad_is_stored_exactly(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID, advertisement_text=PASTED_AD)
    async with db.session() as session:
        assert (await repository.get_listing(session, GID)).advertisement_text == PASTED_AD


def test_simple_builder_makes_a_normal_message():
    ad = listing_service.build_simple_ad(
        name="Rivals HQ", member_count=530, categories=["Gaming"], accepting=True, minimum=100,
        invite_url="https://discord.gg/example", description="Active gaming community.",
    )
    assert ad.startswith("## Rivals HQ")
    assert "**Members:** 530" in ad and "**Category:** Gaming" in ad
    assert "**Partnerships:** Open" in ad and "**Minimum:** 100+" in ad
    assert ad.rstrip().endswith("https://discord.gg/example")
    assert "embed" not in ad.lower()


def test_builder_skips_partnership_details_when_closed():
    ad = listing_service.build_simple_ad(
        name="Quiet Server", member_count=12, categories=[], accepting=False, minimum=0,
        invite_url=None, description="A small place.",
    )
    assert "**Partnerships:** Closed" in ad and "Minimum" not in ad and "Category" not in ad


def test_everyone_text_is_preserved_but_never_pings():
    from bot.utils.mentions import advertisement_kwargs

    kwargs = advertisement_kwargs(PASTED_AD)
    assert "@everyone" in kwargs["content"]  # the words stay visible
    assert kwargs["allowed_mentions"].to_dict()["parse"] == []  # the bot pings nobody
    assert "embed" not in kwargs


def test_custom_emoji_are_detected():
    assert listing_service.custom_emoji_ids(PASTED_AD) == [123456789012345678]
    assert listing_service.custom_emoji_ids("no emoji here") == []


async def test_custom_emoji_warning_only_for_emoji_the_bot_cannot_use(db):
    from bot.views.listings import unusable_custom_emoji
    from tests.fakes import FakeBot

    bot = FakeBot(db)
    bot.get_emoji = lambda _id: None
    assert unusable_custom_emoji(bot, PASTED_AD) == [123456789012345678]
    bot.get_emoji = lambda _id: object()  # Waypoint is in that server
    assert unusable_custom_emoji(bot, PASTED_AD) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Free nitro http://grabify.link/abc", "grabify.link"),
        ("visit iplogger.org/x", "iplogger.org"),
        (" ".join(f"https://site{i}.com" for i in range(7)), "at most 5"),
    ],
)
def test_dangerous_or_spammy_links_are_refused(text, message):
    with pytest.raises(ValidationError, match=message):
        listing_service.check_links(text, default_config())


def test_shortened_links_are_held_for_review_or_blocked():
    config = default_config()
    assert listing_service.check_links("join bit.ly/abc", config) is True  # review
    assert listing_service.check_links("join https://discord.gg/abc", config) is False
    blocking = replace(config, moderation=replace(config.moderation, link_action="block"))
    with pytest.raises(ValidationError, match="Shortened"):
        listing_service.check_links("join bit.ly/abc", blocking)
    allowing = replace(config, moderation=replace(config.moderation, link_action="allow"))
    assert listing_service.check_links("join bit.ly/abc", allowing) is False


async def test_a_listing_with_a_suspicious_link_waits_for_staff(db, config):
    async with db.session() as session:
        listing = await listing_service.create_listing(
            session, config, guild=guild(GID), actor_id=1,
            data=listing_input(advertisement_text="Join us! bit.ly/rivals"), now=NOW,
        )
    assert listing.status == ListingStatus.PENDING  # held for review, not published


async def test_a_clean_listing_publishes_immediately(db, config):
    async with db.session() as session:
        listing = await make_listing(session, config, GID)
    assert listing.status == ListingStatus.ACTIVE


async def test_blocked_domains_are_configurable(db, config):
    strict = replace(config, moderation=replace(config.moderation, blocked_domains=("example.com",)))
    with pytest.raises(ValidationError, match="example.com"):
        async with db.session() as session:
            await make_listing(session, strict, GID, advertisement_text="see https://example.com/deal")
