"""Unverified invite-only submissions. A submission NEVER creates manager rights."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import RuntimeConfig
from bot.database import repository
from bot.database.models import Listing, ListingStatus
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



def prepare_quick_ad(
    config: RuntimeConfig, *, info: listings.GuildInfo, invite_url: str,
    category: str, description: str = "", raw_ad: str | None = None,
) -> tuple[str, bool]:
    """Validate public copy; force review for unfamiliar URLs even in automatic mode.

    Public invites are *not* proof of ownership. Never let arbitrary submitted
    content ping server members, and never treat a copied ad as a verified claim.
    """
    chosen = listings.clean_categories([category], config)
    if raw_ad is None:
        clean = quick_description(description, config)
        formatted = listings.build_simple_ad(
            name=info.name, member_count=info.member_count, categories=chosen,
            accepting=False, minimum=0, invite_url=invite_url, description=clean,
        )
        if info.member_count <= 0:
            formatted = formatted.replace("**Members:** 0\n", "**Members:** Not available\n")
        return formatted, False
    clean = listings.clean_advertisement(raw_ad, config)
    lowered = clean.lower()
    if any(token in lowered for token in ("@everyone", "@here", "<@", "<#!", "<@&")):
        raise ValidationError("Advertisements cannot include user, role, or mass mentions.")
    links = listings.find_links(clean)
    # An ad may embed the canonical invite for its own community; additional
    # invites risk silently advertising a different guild under this listing.
    for link in links:
        if "discord.gg/" in link.lower() or "discord.com/invite/" in link.lower():
            if listings.parse_invite_code(link) != listings.parse_invite_code(invite_url):
                raise ValidationError("The advertisement contains an invite for a different server.")
    suspicious = listings.check_links(clean, config)
    from urllib.parse import urlparse
    extra = any(
        (urlparse(url).hostname or "").lower() not in ("discord.gg", "discord.com", "www.discord.com")
        for url in links
    )
    # Limit total message length after adding the verified target invite.
    if invite_url not in clean:
        clean = listings.clean_advertisement(f"{clean}\n\n{invite_url}", config)
    return clean, suspicious or extra


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
    raw_ad: str | None = None,
) -> tuple[Listing, str]:
    """Reserve one unverified listing, or repost the same previously approved ad.

    Unique constraints on guild_id and quick_submitted_by guard races, including
    simultaneous clicks handled by separate bot workers.
    """
    await moderation.ensure_allowed(session, config, guild_ids=[info.guild_id], user_id=actor_id)
    if info.guild_id == config.hub.main_guild_id:
        raise ValidationError("The Parley main server cannot be Quick Posted.")
    chosen = listings.clean_categories([category], config)
    formatted, force_review = prepare_quick_ad(
        config, info=info, invite_url=invite_url, category=chosen[0],
        description=description, raw_ad=raw_ad,
    )
    requires_review = config.listings.approval_required or force_review
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
            previous.advertisement_text = formatted
            previous.category = chosen[0]
            previous.invite_url = invite_url
            previous.status = ListingStatus.PENDING if requires_review else ListingStatus.ACTIVE
            previous.refreshed_at = now
            previous.updated_at = now
            previous.pending_submitted_by = actor_id if requires_review else None
            previous.review_revision = (previous.review_revision or 0) + 1
            await repository.add_audit(session, "listing.quick_resubmitted", actor_id=actor_id, guild_id=info.guild_id)
            return previous, "pending" if requires_review else "published"
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

    record = Listing(
        guild_id=info.guild_id, quick_submitted_by=actor_id,
        advertisement_text=formatted, category=chosen[0],
        accepting_partnerships=False, minimum_members=0,
        invite_url=invite_url, status=ListingStatus.PENDING if requires_review else ListingStatus.ACTIVE,
        created_at=now, updated_at=now, refreshed_at=now,
        pending_submitted_by=actor_id if requires_review else None, review_revision=1,
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
    return record, "pending" if requires_review else "published"


async def edit_quick_listing(
    session: AsyncSession, config: RuntimeConfig, *, guild_id: int, actor_id: int,
    category: str, info: listings.GuildInfo, invite_url: str,
    description: str, now, raw_ad: str | None = None,
) -> tuple[Listing, str]:
    """One edit per relist cycle. Never changes unverified guild authority or rank."""
    await moderation.ensure_allowed(session, config, guild_ids=[guild_id], user_id=actor_id)
    row = await session.scalar(select(Listing).where(Listing.guild_id == guild_id).with_for_update())
    if row is None or row.quick_submitted_by != actor_id:
        raise Conflict("Only the original unverified submitter may edit this Quick Post.")
    if row.status != ListingStatus.ACTIVE or row.pending_changes:
        raise Conflict("This advertisement is not available for editing right now.")
    if row.last_ad_edit_at is not None and row.refreshed_at is not None and row.last_ad_edit_at >= row.refreshed_at:
        raise CooldownActive("You've already edited this advertisement in its current relist cycle.", timedelta(minutes=1))
    if info.guild_id != guild_id or invite_url != row.invite_url:
        raise Conflict("Quick Post edits cannot change a server's identity or invite. Contact staff to change invites.")
    content, unsafe_links = prepare_quick_ad(
        config, info=info, invite_url=row.invite_url, category=category,
        description=description, raw_ad=raw_ad,
    )
    changed = content != row.advertisement_text or category != row.category
    if not changed:
        raise ValidationError("Your advertisement hasn't changed.")
    row.last_ad_edit_at = now
    if config.listings.approval_required or unsafe_links:
        row.pending_changes = {"advertisement_text": content, "category": category}
        row.pending_submitted_by = actor_id
        row.review_revision = (row.review_revision or 0) + 1
        outcome = "pending_edit"
    else:
        row.advertisement_text = content
        row.category = category
        row.updated_at = now
        outcome = "edited"
    await repository.add_audit(session, "listing.quick_" + outcome, actor_id=actor_id, guild_id=guild_id)
    return row, outcome
