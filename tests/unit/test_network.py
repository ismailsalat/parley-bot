"""Network distribution: opt-in, eligibility, category filtering, fair rotation."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta

import pytest

from bot.database import repository
from bot.database.models import NetworkSettings
from bot.services import moderation, network
from bot.services.errors import ValidationError
from tests.factories import NOW, make_listing

DEST = 300000000000000001
GAMING, ANIME, SOCIAL = 300000000000000010, 300000000000000011, 300000000000000012
CHANNEL = 400000000000000001


@pytest.fixture
async def listed(db, config):
    async with db.session() as session:
        await make_listing(session, config, GAMING, categories=["Gaming"])
        await make_listing(session, config, ANIME, categories=["Anime"])
        await make_listing(session, config, SOCIAL, categories=["Social"])
        await make_listing(session, config, DEST, categories=["Gaming"])  # the destination's own listing


async def set_auto_partner(db, enabled: bool, guild_id: int = None) -> None:
    async with db.session() as session:
        await network.set_auto_partner(
            session, guild_id=guild_id or DEST, enabled=enabled, actor_id=1, now=NOW
        )


async def configure(db, config, **overrides):
    values = dict(
        guild_id=DEST, channel_id=CHANNEL, categories=[], interval_minutes=180, enabled=True, actor_id=1, now=NOW
    )
    values.update(overrides)
    async with db.session() as session:
        return await network.configure(session, config, **values)


# ---------------------------------------------------------------- category filtering


@pytest.mark.parametrize(
    ("listing", "wanted", "expected"),
    [
        (["Gaming"], [], True),
        (["Gaming"], ["Any"], True),
        (["Gaming"], ["Gaming", "Anime"], True),
        (["Social"], ["Gaming", "Anime"], False),
        ([], ["Gaming"], False),
    ],
)
def test_category_matches(listing, wanted, expected):
    assert network.category_matches(listing, wanted) is expected


async def test_eligible_sources_filter_categories_and_exclude_self(db, config, listed):
    async with db.session() as session:
        everything = await network.eligible_sources(session, config, destination_guild_id=DEST, categories=[])
        gaming_anime = await network.eligible_sources(
            session, config, destination_guild_id=DEST, categories=["Gaming", "Anime"]
        )
    assert {l.guild_id for l in everything} == {GAMING, ANIME, SOCIAL}
    assert {l.guild_id for l in gaming_anime} == {GAMING, ANIME}


async def test_banned_and_inactive_listings_are_not_eligible(db, config, listed):
    async with db.session() as session:
        await moderation.ban_guild(session, guild_id=ANIME, reason="spam", moderator_id=1)
        await moderation.suspend_listing(session, guild_id=SOCIAL, reason=None, moderator_id=1)
    async with db.session() as session:
        sources = await network.eligible_sources(session, config, destination_guild_id=DEST, categories=[])
    assert [l.guild_id for l in sources] == [GAMING]


# ---------------------------------------------------------------- opt-in configuration


async def test_network_is_opt_in(db, config):
    async with db.session() as session:
        assert await repository.enabled_network_settings(session) == []
        assert await network.due_destinations(session, config, NOW) == []


async def test_configure_requires_channel_and_minimum_interval(db, config):
    with pytest.raises(ValidationError, match="choose a channel"):
        await configure(db, config, channel_id=None)
    with pytest.raises(ValidationError, match="shortest posting interval"):
        await configure(db, config, interval_minutes=5)


async def test_configure_keeps_only_known_categories(db, config):
    settings = await configure(db, config, categories=["Gaming", "Nope", "Gaming"])
    assert settings.categories == ["Gaming"]


async def test_disabled_network_is_never_due(db, config):
    await configure(db, config)
    await configure(db, config, enabled=False)
    async with db.session() as session:
        assert await network.due_destinations(session, config, NOW + timedelta(days=1)) == []


async def test_global_network_switch(db, config):
    paused = replace(config, network=replace(config.network, enabled=False))
    with pytest.raises(ValidationError, match="paused"):
        await configure(db, paused)


# ---------------------------------------------------------------- intervals and rotation


async def test_interval_is_respected(db, config, listed):
    """The tick is the Auto Partner check, so only Auto Partner servers are due."""
    await configure(db, config, interval_minutes=180)
    await set_auto_partner(db, True)
    async with db.session() as session:
        assert [s.guild_id for s in await network.due_destinations(session, config, NOW)] == [DEST]
        await network.record_post(
            session, source_guild_id=GAMING, destination_guild_id=DEST, channel_id=CHANNEL, message_id=1, now=NOW
        )
    async with db.session() as session:
        assert await network.due_destinations(session, config, NOW + timedelta(minutes=179)) == []
        assert len(await network.due_destinations(session, config, NOW + timedelta(minutes=180))) == 1


def test_is_due_without_channel():
    assert not network.is_due(NetworkSettings(guild_id=1, enabled=True, channel_id=None, interval_minutes=60), NOW)


def _listing(gid):
    from bot.database.models import Listing

    return Listing(guild_id=gid, category="Gaming", advertisement_text="x")


def test_rotation_prefers_never_shown_then_least_recent():
    listings = [_listing(1), _listing(2), _listing(3)]
    window = timedelta(hours=12)
    shown = {1: NOW - timedelta(hours=20), 2: NOW - timedelta(hours=30)}
    pick = network.pick_candidate(listings, last_shown=shown, now=NOW, repeat_window=window, rng=random.Random(1))
    assert pick.guild_id == 3  # never shown here
    shown[3] = NOW - timedelta(hours=13)
    pick = network.pick_candidate(listings, last_shown=shown, now=NOW, repeat_window=window)
    assert pick.guild_id == 2  # shown longest ago


def test_rotation_skips_recent_and_prefers_silence_over_repeats():
    listings = [_listing(1), _listing(2)]
    shown = {1: NOW - timedelta(hours=1), 2: NOW - timedelta(hours=2)}
    assert network.pick_candidate(listings, last_shown=shown, now=NOW, repeat_window=timedelta(hours=12)) is None


def test_rotation_is_fair_over_many_rounds():
    listings = [_listing(i) for i in range(1, 6)]
    shown: dict[int, object] = {}
    now = NOW
    picks = []
    for _ in range(10):
        pick = network.pick_candidate(listings, last_shown=shown, now=now, repeat_window=timedelta(0), rng=random.Random(7))
        picks.append(pick.guild_id)
        shown[pick.guild_id] = now
        now += timedelta(hours=3)
    # every listing appears exactly twice in ten rounds: nobody is starved or repeated early
    assert sorted(picks) == sorted([1, 2, 3, 4, 5] * 2)


async def test_last_shown_map_and_banned_destination(db, config, listed):
    await configure(db, config)
    async with db.session() as session:
        await network.record_post(
            session, source_guild_id=ANIME, destination_guild_id=DEST, channel_id=CHANNEL, message_id=2, now=NOW
        )
        shown = await repository.last_shown_map(session, DEST, NOW - timedelta(days=1))
        assert shown == {ANIME: NOW}
        await moderation.ban_guild(session, guild_id=DEST, reason=None, moderator_id=1)
    async with db.session() as session:
        assert await network.due_destinations(session, config, NOW + timedelta(days=2)) == []
