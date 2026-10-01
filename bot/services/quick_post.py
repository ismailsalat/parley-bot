"""Unverified invite-only submissions. A submission NEVER creates manager rights."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import RuntimeConfig
from bot.database import repository
from bot.database.models import Guild, Listing, ListingStatus
from bot.services import listings, moderation
from bot.services.errors import Conflict, CooldownActive, ValidationError
from bot.utils.helpers import format_duration


def quick_description(text: str, config: RuntimeConfig) -> str:
    """Unverified ads must be short, link-free, and not impersonate announcements."""
    cleaned = listings.clean_advertisement(text, config)
    if len(cleaned) > 550:
        raise ValidationError("Quick Post descriptions must be 550 characters or shorter.")
    if listings.find_links(cleaned):
        raise ValidationError("Keep Quick Post descriptions free of links. Your server invite is attached automatically.")
    if '@everyone' in cleaned.lower() or '@here' in cleaned.lower() or '<@' in cleaned:
        raise ValidationError("Quick Post descriptions cannot contain mentions.")
    return cleaned


async def my_quick_listing(session: AsyncSession, actor_id: int) -> Listing | None:
    return await session.scalar(select(Listing).where(Listing.quick_submitted_by == actor_id))


async def create_quick_listing(
    session: AsyncSession,
    config: RuntimeConfig,
    *,
    info: listings.GuildInfo,
    actor_id: int,
    invite_url: str,
    description: str,
    category: str,
    now,
    is_test: bool = False,
) -> tuple[Listing, str]:
    """Reserve one unverified listing, or repost the same previously approved ad.

    Unique constraints on guild_id and quick_submitted_by guard races, including
    simultaneous clicks handled by separate bot workers.
    """
    await moderation.ensure_allowed(session, config, guild_ids=[info.guild_id], user_id=actor_id)
    if info.guild_id == config.hub.main_guild_id:
        raise ValidationError("The Parley main server cannot be Quick Posted.")
    chosen = listings.clean_categories([category], config)
    clean = quick_description(description, config)
    if not invite_url or not invite_url.startswith('https://discord.gg/'):
        raise ValidationError("Please use a valid Discord invite.")

    owned = await my_quick_listing(session, actor_id)
    if owned and owned.guild_id != info.guild_id:
        raise Conflict("Quick Post allows one unconnected server per account. Connect Parley to manage additional servers.")

    # PostgreSQL locks the existing guild row during relist/replace decisions.
    previous = await session.scalar(
        select(Listing).where(Listing.guild_id == info.guild_id).with_for_update()
    )
    if previous is not None:
        if previous.quick_submitted_by != actor_id:
            raise Conflict("This server has an existing listing. Only its verified managers can change it.")
        if previous.status == ListingStatus.SUSPENDED:
            raise Conflict("This server's listing is suspended. Contact Parley staff.")
        if previous.status == ListingStatus.PENDING:
            raise Conflict("Your Quick Post is already awaiting staff review.")
        if previous.refreshed_at is not None:
            available = previous.refreshed_at + timedelta(minutes=config.listings.quick_post_cooldown_minutes)
            if now < available:
                remaining = available - now
                raise CooldownActive(f"You can repost in {format_duration(remaining)}.", remaining)
        # A repost does not let an unverified poster rewrite the approved ad or
        # change its invite. Edits require a new submission and staff approval.
        if previous.status == ListingStatus.ACTIVE:
            previous.refreshed_at = now
            previous.updated_at = now
            await repository.add_audit(
                session, "listing.quick_reposted", actor_id=actor_id, guild_id=info.guild_id
            )
            return previous, "reposted"
        if previous.status == ListingStatus.REMOVED:
            # Rejected listings can be corrected and reviewed again the next day.
            # Banned guilds were already denied above by ensure_allowed().
            previous.advertisement_text = listings.build_simple_ad(
                name=info.name, member_count=info.member_count, categories=chosen,
                accepting=False, minimum=0, invite_url=invite_url, description=clean,
            )
            previous.category = chosen[0]
            previous.invite_url = invite_url
            previous.status = ListingStatus.PENDING
            previous.refreshed_at = now
            previous.updated_at = now
            previous.pending_submitted_by = actor_id
            previous.review_revision = (previous.review_revision or 0) + 1
            await repository.add_audit(session, "listing.quick_resubmitted", actor_id=actor_id, guild_id=info.guild_id)
            return previous, "pending"
        if previous.status == ListingStatus.EXPIRED:
            previous.refreshed_at = now
            previous.status = ListingStatus.ACTIVE
            previous.updated_at = now
            await repository.add_audit(
                session, "listing.quick_restored", actor_id=actor_id, guild_id=info.guild_id
            )
            return previous, "reposted"

    existing_guild = await repository.get_guild(session, info.guild_id)
    if existing_guild is not None and existing_guild.connected_by is not None:
        raise Conflict("This community has a verified owner in Parley. Ask its admins to manage the listing.")
    await repository.upsert_guild(
        session, guild_id=info.guild_id, name=info.name,
        icon_url=info.icon_url, member_count=info.member_count,
        # Deliberately do NOT pass connected_by: an invite grants ZERO management rights.
    )
    formatted = listings.build_simple_ad(
        name=info.name, member_count=info.member_count,
        categories=chosen, accepting=False, minimum=0,
        invite_url=invite_url, description=clean,
    )
    if info.member_count <= 0:
        formatted = formatted.replace("**Members:** 0\n", "**Members:** Not available\n")
    record = Listing(
        guild_id=info.guild_id, quick_submitted_by=actor_id,
        advertisement_text=formatted, category=chosen[0],
        accepting_partnerships=False, minimum_members=0,
        invite_url=invite_url, status=ListingStatus.PENDING,
        created_at=now, updated_at=now, refreshed_at=now,
        pending_submitted_by=actor_id, review_revision=1,
        is_test=is_test,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("You already have a Quick Post or this server was just listed. Please reopen the menu.") from exc
    # Quick posts are NOT partnership contacts, and have no represent/manager rights.
    await repository.add_audit(
        session, "listing.quick_submitted", actor_id=actor_id, guild_id=info.guild_id,
        details={"unverified": True, "category": chosen[0]},
    )
    return record, "pending"
