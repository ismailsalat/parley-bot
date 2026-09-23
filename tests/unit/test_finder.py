"""Find Partners: one server at a time, never the same one twice."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bot.database import repository
from bot.services import moderation, partnerships
from bot.views.partnership import ANY, FinderView
from tests.factories import NOW, make_listing
from tests.fakes import USER_ID, FakeBot

MINE = 830000000000000000
OTHERS = [830000000000000001, 830000000000000002, 830000000000000003, 830000000000000004]


@pytest.fixture
async def network(db, config):
    async with db.session() as session:
        await make_listing(session, config, MINE, categories=["Gaming"], members=500)
        for gid in OTHERS:
            await make_listing(session, config, gid, categories=["Gaming"], members=500, actor_id=gid)


def finder(bot, *, source=MINE, category=ANY) -> FinderView:
    return FinderView(bot, USER_ID, category=category, source_id=source)


async def seen_sequence(view: FinderView, count: int) -> list[int]:
    picks = []
    for _ in range(count):
        listing = await view.pick_next()
        if listing is None:
            break
        picks.append(listing.guild_id)
        view.seen.add(listing.guild_id)
    return picks


async def test_your_own_server_is_never_a_result(db, network):
    view = finder(FakeBot(db))
    assert MINE not in await seen_sequence(view, 10)


async def test_next_server_never_repeats_until_the_pool_is_empty(db, network):
    view = finder(FakeBot(db))
    picks = await seen_sequence(view, 10)
    assert sorted(picks) == sorted(OTHERS)  # each exactly once
    assert await view.pick_next() is None  # then the pool is exhausted


async def test_show_again_resets_the_seen_list(db, network):
    bot = FakeBot(db)
    view = finder(bot)
    await seen_sequence(view, 10)
    fresh = finder(bot)  # what Show Again builds
    assert len(await seen_sequence(fresh, 10)) == len(OTHERS)


async def test_results_are_not_always_in_the_same_order(db, network):
    """Fair rotation: no fixed 'biggest first' order."""
    bot = FakeBot(db)
    orders = {tuple(await seen_sequence(finder(bot), 10)) for _ in range(25)}
    assert len(orders) > 1


async def test_servers_with_a_pending_request_are_skipped(db, network, config):
    async with db.session() as session:
        await partnerships.create_request(
            session, config, source_guild_id=MINE, target_guild_id=OTHERS[0], requester_id=USER_ID,
            source_member_count=500, message=None, now=NOW,
        )
    picks = await seen_sequence(finder(FakeBot(db)), 10)
    assert OTHERS[0] not in picks and len(picks) == len(OTHERS) - 1


async def test_suspended_banned_and_closed_servers_are_skipped(db, network):
    async with db.session() as session:
        await moderation.suspend_listing(session, guild_id=OTHERS[0], reason=None, moderator_id=1)
        await moderation.ban_guild(session, guild_id=OTHERS[1], reason=None, moderator_id=1)
        (await repository.get_listing(session, OTHERS[2])).accepting_partnerships = False
    picks = await seen_sequence(finder(FakeBot(db)), 10)
    assert picks == [OTHERS[3]]


async def test_servers_wanting_bigger_partners_are_skipped(db, config):
    async with db.session() as session:
        await make_listing(session, config, MINE, members=50)
        await make_listing(session, config, OTHERS[0], actor_id=OTHERS[0], minimum_members=1000)
        await make_listing(session, config, OTHERS[1], actor_id=OTHERS[1], minimum_members=10)
    picks = await seen_sequence(finder(FakeBot(db)), 10)
    assert picks == [OTHERS[1]]


async def test_category_filter(db, config):
    async with db.session() as session:
        await make_listing(session, config, MINE, categories=["Gaming"])
        await make_listing(session, config, OTHERS[0], categories=["Anime"], actor_id=OTHERS[0])
        await make_listing(session, config, OTHERS[1], categories=["Gaming"], actor_id=OTHERS[1])
    bot = FakeBot(db)
    assert await seen_sequence(finder(bot, category="Anime"), 5) == [OTHERS[0]]
    assert await seen_sequence(finder(bot, category="Gaming"), 5) == [OTHERS[1]]
    assert sorted(await seen_sequence(finder(bot, category=ANY), 5)) == sorted([OTHERS[0], OTHERS[1]])


async def test_test_listings_are_hidden_from_live_searches(db, config):
    async with db.session() as session:
        await make_listing(session, config, MINE)
        await make_listing(session, config, OTHERS[0], actor_id=OTHERS[0])
        (await repository.get_listing(session, OTHERS[0])).is_test = True
    assert await seen_sequence(finder(FakeBot(db, mode="live")), 5) == []
    assert await seen_sequence(finder(FakeBot(db, mode="test")), 5) == [OTHERS[0]]


async def test_browsing_without_a_listing_still_works(db, network):
    view = finder(FakeBot(db), source=None)
    picks = await seen_sequence(view, 10)
    assert sorted(picks) == sorted([MINE, *OTHERS])  # nothing of yours to exclude yet


async def test_minimum_check_can_be_switched_off(db, config):
    async with db.session() as session:
        await make_listing(session, config, MINE, members=10)
        await make_listing(session, config, OTHERS[0], actor_id=OTHERS[0], minimum_members=1000)
    relaxed = replace(config, partnerships=replace(config.partnerships, enforce_minimum_members=False))
    bot = FakeBot(db)
    bot.runtime = replace(bot.runtime, partnerships=relaxed.partnerships)
    assert await seen_sequence(finder(bot), 5) == [OTHERS[0]]


async def accept_partnership(db, config, target: int) -> None:
    from bot.services import partnerships

    async with db.session() as session:
        context = await partnerships.create_request(
            session, config, source_guild_id=MINE, target_guild_id=target, requester_id=USER_ID,
            source_member_count=500, message=None, now=NOW,
        )
        await partnerships.respond(
            session, config, request_id=context.request.id, responder_id=target,
            responder_is_manager=True, accept=True, now=NOW,
        )


async def test_servers_you_already_partnered_with_are_hidden(db, network, config):
    await accept_partnership(db, config, OTHERS[0])
    view = finder(FakeBot(db))
    picks = await seen_sequence(view, 10)
    assert OTHERS[0] not in picks
    assert sorted(picks) == sorted(OTHERS[1:])
    assert view.past_partners == {OTHERS[0]}  # remembered, so "Show Past Partners" can offer them


async def test_show_past_partners_brings_them_back(db, network, config):
    await accept_partnership(db, config, OTHERS[0])
    view = FinderView(FakeBot(db), USER_ID, category=ANY, source_id=MINE, include_past=True)
    assert OTHERS[0] in await seen_sequence(view, 10)


async def test_exhausted_screen_offers_past_partners_only_when_there_are_some(db, network, config):
    from bot.views.partnership import ExhaustedView

    bot = FakeBot(db)
    with_past = ExhaustedView(bot, USER_ID, category=ANY, source_id=MINE, seen=set(), past_partners=True)
    without = ExhaustedView(bot, USER_ID, category=ANY, source_id=MINE, seen=set(), past_partners=False)
    labels = lambda view: [getattr(c, "item", c).label for c in view.children]  # noqa: E731
    assert labels(with_past)[0] == "Show Past Partners"
    assert "Show Past Partners" not in labels(without)
    assert labels(without) == ["Show Again"]


async def test_past_only_mode_does_not_mix_new_servers_back_in(db, network, config):
    await accept_partnership(db, config, OTHERS[0])
    view = FinderView(
        FakeBot(db), USER_ID, category=ANY, source_id=MINE,
        include_past=True, past_only=True,
    )
    picks = await seen_sequence(view, 10)
    assert picks == [OTHERS[0]]


async def test_discovery_is_not_capped_at_200_rows(db, config):
    async with db.session() as session:
        await make_listing(session, config, MINE, members=500)
        # 205 eligible servers proves rows after the old hard cap remain discoverable.
        for i in range(205):
            gid = 840000000000000000 + i
            await make_listing(session, config, gid, actor_id=gid, members=500)
    view = finder(FakeBot(db))
    candidates = await view.candidates()
    assert len(candidates) == 205
