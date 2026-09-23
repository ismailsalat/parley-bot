"""Opt-in network distribution: configuration, eligibility and fair rotation."""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import RuntimeConfig
from bot.database import repository
from bot.database.models import Listing, NetworkPost, NetworkSettings
from bot.services import moderation
from bot.services.errors import ValidationError

log = logging.getLogger(__name__)

ANY = "Any"


def category_matches(listing_categories: Sequence[str], wanted: Sequence[str]) -> bool:
    """Empty filter (or 'Any') accepts everything."""
    wanted = [w for w in wanted if w]
    if not wanted or ANY in wanted:
        return True
    return any(category in wanted for category in listing_categories)


def is_due(settings: NetworkSettings, now: datetime) -> bool:
    if not settings.enabled or settings.channel_id is None:
        return False
    if settings.last_post_at is None:
        return True
    return now - settings.last_post_at >= timedelta(minutes=settings.interval_minutes)


def pick_candidate(
    listings: Sequence[Listing],
    *,
    last_shown: dict[int, datetime],
    now: datetime,
    repeat_window: timedelta,
    strategy: str = "least_recent",
    rng: random.Random | None = None,
) -> Listing | None:
    """Choose the next advertisement for one destination.

    1. drop listings shown to this destination within ``repeat_window``
    2. least_recent: prefer listings never shown here (random among them),
       otherwise the one shown longest ago. random: uniform choice.
    Returns None when everything was shown recently: silence beats repetition.
    """
    rng = rng or random.Random()
    fresh = [
        listing
        for listing in listings
        if listing.guild_id not in last_shown or now - last_shown[listing.guild_id] >= repeat_window
    ]
    if not fresh:
        return None
    if strategy == "random":
        return rng.choice(fresh)
    never_shown = [listing for listing in fresh if listing.guild_id not in last_shown]
    if never_shown:
        return rng.choice(never_shown)
    return min(fresh, key=lambda listing: last_shown[listing.guild_id])


async def eligible_sources(
    session: AsyncSession, config: RuntimeConfig, *, destination_guild_id: int, categories: Sequence[str]
) -> list[Listing]:
    """Active, non-banned listings from other servers that pass the category filter."""
    result = []
    for listing in await repository.active_listings(session, exclude_guild_id=destination_guild_id):
        if not category_matches(listing.categories, categories):
            continue
        if await moderation.is_guild_banned(session, config, listing.guild_id):
            continue
        result.append(listing)
    return result


async def configure(
    session: AsyncSession,
    config: RuntimeConfig,
    *,
    guild_id: int,
    channel_id: int | None,
    categories: Sequence[str],
    interval_minutes: int,
    enabled: bool,
    actor_id: int,
    now: datetime,
) -> NetworkSettings:
    rules = config.network
    await moderation.ensure_allowed(session, config, guild_ids=[guild_id], user_id=actor_id)
    if enabled and not rules.enabled:
        raise ValidationError("The Parley network is paused right now. Please try again later.")
    if enabled and channel_id is None:
        raise ValidationError("Please choose a channel first.")
    if interval_minutes < rules.min_interval_minutes:
        raise ValidationError(f"The shortest posting interval is {rules.min_interval_minutes} minutes.")

    clean_categories = [] if not rules.category_filtering else [
        c for c in dict.fromkeys(categories) if c in config.listings.categories
    ]

    settings = await repository.get_network_settings(session, guild_id)
    if settings is None:
        settings = NetworkSettings(guild_id=guild_id)
        session.add(settings)
    settings.enabled = enabled
    settings.channel_id = channel_id
    settings.categories = clean_categories
    settings.interval_minutes = interval_minutes
    settings.configured_by = actor_id
    settings.updated_at = now
    await session.flush()
    await repository.add_audit(
        session,
        "network.enabled" if enabled else "network.disabled",
        actor_id=actor_id,
        guild_id=guild_id,
        details={"channel_id": channel_id, "categories": clean_categories, "interval": interval_minutes},
    )
    log.info("network.%s guild_id=%s channel_id=%s", "enabled" if enabled else "disabled", guild_id, channel_id)
    return settings


async def disable(session: AsyncSession, *, guild_id: int, reason: str) -> None:
    settings = await repository.get_network_settings(session, guild_id)
    if settings is None or not settings.enabled:
        return
    settings.enabled = False
    await repository.add_audit(session, "network.auto_disabled", guild_id=guild_id, details={"reason": reason})
    log.warning("network.auto_disabled guild_id=%s reason=%s", guild_id, reason)


async def due_destinations(session: AsyncSession, config: RuntimeConfig, now: datetime) -> list[NetworkSettings]:
    due = []
    for settings in await repository.enabled_network_settings(session):
        if is_due(settings, now) and not await moderation.is_guild_banned(session, config, settings.guild_id):
            due.append(settings)
    due.sort(key=lambda s: s.last_post_at or datetime.min.replace(tzinfo=now.tzinfo))
    return due[: config.network.max_posts_per_tick]


async def mark_attempt(session: AsyncSession, *, guild_id: int, now: datetime) -> None:
    settings = await repository.get_network_settings(session, guild_id)
    if settings is not None:
        settings.last_post_at = now


async def record_post(
    session: AsyncSession,
    *,
    source_guild_id: int,
    destination_guild_id: int,
    channel_id: int,
    message_id: int | None,
    now: datetime,
) -> None:
    session.add(
        NetworkPost(
            source_guild_id=source_guild_id,
            destination_guild_id=destination_guild_id,
            channel_id=channel_id,
            message_id=message_id,
            posted_at=now,
        )
    )
    await mark_attempt(session, guild_id=destination_guild_id, now=now)
    await session.flush()
    log.info("network.posted source=%s destination=%s", source_guild_id, destination_guild_id)
