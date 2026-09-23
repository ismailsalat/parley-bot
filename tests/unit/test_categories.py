"""Categories edited in Discord never corrupt existing listings."""

from __future__ import annotations

import pytest

from bot.config.runtime import apply_overrides, default_config
from bot.database import repository
from bot.services import categories, network
from bot.services.errors import ValidationError
from tests.factories import NOW, make_listing

A, B = 810000000000000001, 810000000000000002


async def current(db):
    async with db.session() as session:
        return apply_overrides(default_config(), await repository.runtime_overrides(session))[0]


async def test_add_and_validation(db):
    config = await current(db)
    async with db.session() as session:
        assert await categories.add(session, config, "  Music  ", actor_id=1) == "Music"
    config = await current(db)
    assert config.listings.categories[-1] == "Music"
    for bad, message in [("music", "already exists"), ("Any", "reserved"), ("", "empty"), ("a,b", "commas"), ("x" * 51, "50")]:
        with pytest.raises(ValidationError, match=message):
            async with db.session() as session:
                await categories.add(session, config, bad, actor_id=1)


async def test_rename_updates_listings_and_network_filters(db, config):
    async with db.session() as session:
        await make_listing(session, config, A, categories=["Gaming"])
        await network.configure(
            session, config, guild_id=B, channel_id=5, categories=["Gaming", "Anime"], interval_minutes=180,
            enabled=True, actor_id=1, now=NOW,
        )
    async with db.session() as session:
        await categories.rename(session, config, "Gaming", "Games", actor_id=1)
    async with db.session() as session:
        assert (await repository.get_listing(session, A)).categories == ["Games"]
        assert (await repository.get_network_settings(session, B)).categories == ["Games", "Anime"]
    assert "Games" in (await current(db)).listings.categories


async def test_remove_in_use_requires_a_destination(db, config):
    async with db.session() as session:
        await make_listing(session, config, A, categories=["Roleplay"])
    with pytest.raises(ValidationError, match="Choose where to move"):
        async with db.session() as session:
            await categories.remove(session, config, "Roleplay", move_to=None, actor_id=1)
    async with db.session() as session:
        moved = await categories.remove(session, config, "Roleplay", move_to="Social", actor_id=1)
    assert moved == 1
    async with db.session() as session:
        assert (await repository.get_listing(session, A)).categories == ["Social"]
    assert "Roleplay" not in (await current(db)).listings.categories


async def test_reorder_and_reset(db, config):
    async with db.session() as session:
        await categories.move(session, config, "Creator", -10, actor_id=1)
    assert (await current(db)).listings.categories[0] == "Creator"
    async with db.session() as session:
        await categories.reset_defaults(session, await current(db), actor_id=1)
    assert (await current(db)).listings.categories == default_config().listings.categories


async def test_reset_refuses_when_custom_category_in_use(db, config):
    async with db.session() as session:
        await categories.add(session, config, "Music", actor_id=1)
    config = await current(db)
    async with db.session() as session:
        await make_listing(session, config, A, categories=["Music"])
    with pytest.raises(ValidationError, match="Music"):
        async with db.session() as session:
            await categories.reset_defaults(session, config, actor_id=1)
