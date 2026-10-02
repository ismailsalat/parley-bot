"""Unverified invite-only submissions. A submission NEVER creates manager rights."""
from __future__ import annotations

from datetime import timedelta
from collections.abc import Collection
from urllib.parse import urlsplit

import discord

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import RuntimeConfig
from bot.database import repository
from bot.database.models import AuditLog, Listing, ListingStatus
from bot.services import cooldowns, listings, moderation
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
    verified_invite_codes: Collection[str] = (),
) -> tuple[str, bool]:
    """Validate public copy; force review for unfamiliar URLs even in automatic mode.

    Public invites are *not* proof of ownership. Never let arbitrary submitted
    content ping server members, and never treat a copied ad as a verified claim.
    """
    chosen = listings.clean_categories([category], config)
    from re import search
    if search(r"(?i)(?<![a-z0-9])(?:nsfw|18\+|adult[- ]only)(?![a-z0-9])", f"{info.name} {description} {raw_ad or ''}"):
        raise ValidationError("Adult/NSFW communities and advertisements aren't allowed on Parley.")
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
    codes = advertisement_invite_codes(clean)
    primary_code = listings.parse_invite_code(invite_url)
    trusted_codes = set(verified_invite_codes)
    for code in codes:
        if code != primary_code and code not in trusted_codes:
            raise ValidationError(
                "This ad includes an additional invite that Parley hasn't verified. "
                "Please correct the invite links and try again."
            )
    suspicious = listings.check_links(clean, config)
    extra = any(
        (urlsplit(url if '://' in url else f'https://{url}').hostname or '').lower()
        not in ('discord.gg', 'www.discord.gg', 'discord.com', 'www.discord.com',
                'discordapp.com', 'www.discordapp.com')
        for url in links
    )
    # Keep a pasted vanity/alternate invite as-is after verifying its guild ID.
    # Don't append a duplicate canonical invite when a valid same-server link
    # already exists in the ad.
    if not codes:
        clean = listings.clean_advertisement(f"{clean}\n\n{invite_url}", config)
    return clean, suspicious or extra


def advertisement_invite_codes(text: str) -> list[str]:
    """Official Discord invite links in pasted text; refuse malformed URLs.

    Non-Discord external links still use the existing moderator-review policy.
    Parsing an invite does *not* establish its server: the caller must resolve
    each non-primary code through Discord before treating it as trusted.
    """
    codes = []
    for link in listings.find_links(text):
        url = urlsplit(link if '://' in link else f'https://{link}')
        hostname = (url.hostname or '').lower()
        is_invite_host = hostname in ('discord.gg', 'www.discord.gg') or (
            hostname in ('discord.com', 'www.discord.com', 'discordapp.com', 'www.discordapp.com')
            and url.path.lower().startswith('/invite')
        )
        if not is_invite_host:
            continue
        # Never silently ignore a malformed or disguised Discord invite.
        code = listings.parse_invite_code(link)
        if code not in codes:
            codes.append(code)
    return codes


async def verify_advertisement_invites(bot, raw_ad: str, *, guild_id: int, invite_url: str) -> frozenset[str]:
    """Verify every additional invite against the intended Discord guild ID.

    Normal codes and vanity codes use the same fetch_invite API; no OAuth is
    required. Fail closed on expired/unavailable invites or Discord outages.
    """
    # Reject oversized/overlinked advertisements before making Discord API calls.
    listings.clean_advertisement(raw_ad, bot.runtime)
    listings.check_links(raw_ad, bot.runtime)
    primary_code = listings.parse_invite_code(invite_url)
    codes = advertisement_invite_codes(raw_ad)
    if len(codes) > 5:
        raise ValidationError("Please use at most five Discord invite links in an advertisement.")
    approved = set()
    for code in codes:
        if code == primary_code:
            continue  # already fetched and guild-checked by QuickPostModal
        try:
            result = await bot.fetch_invite(code, with_counts=False)
        except (discord.NotFound, discord.Forbidden) as exc:
            raise ValidationError(
                f"The invite discord.gg/{code} is expired, invalid, or inaccessible. "
                "Use a working invite for your server."
            ) from exc
        except discord.HTTPException as exc:
            raise ValidationError(
                "Discord couldn't verify every invite in your ad right now. "
                "Please try again shortly."
            ) from exc
        if result.guild is None or result.guild.id != guild_id:
            raise ValidationError(
                f"The invite discord.gg/{code} belongs to a different server. "
                "Only invites for the server you're advertising are allowed."
            )
        approved.add(code)
    return frozenset(approved)


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
    verified_invite_codes: Collection[str] = (),
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
        verified_invite_codes=verified_invite_codes,
    )
    requires_review = config.listings.approval_required or force_review
    # Staff approval is for SERVER + category + summary. Full ad comes later.
    awaiting_ad = bool(config.listings.approval_required and raw_ad is None)
    if not invite_url or not invite_url.startswith('https://discord.gg/'):
        raise ValidationError("Please use a valid Discord invite.")

    owned = await my_quick_listing(session, actor_id)
    if owned and owned.guild_id != info.guild_id:
        raise Conflict("Quick Post allows one unconnected server per account. Connect Parley to manage additional servers.")
    # Deleting a listing frees the slot, not the posting cooldown. This persists
    # across bot restarts and prevents delete/recreate cycles from flooding feeds.
    wait = await cooldowns.remaining(session, "quick_post_deleted", actor_id, now)
    if wait is not None:
        raise CooldownActive(f"You can submit another Free Listing in {format_duration(wait)}.", wait)

    # PostgreSQL locks the existing guild row during relist/replace decisions.
    previous = await session.scalar(
        select(Listing).where(Listing.guild_id == info.guild_id).with_for_update()
    )
    if previous is not None:
        if previous.quick_submitted_by != actor_id:
            # A deleted unverified listing may be restored by its ORIGINAL
            # submitter after cooldown. Audit history is durable even though
            # quick_submitted_by was cleared to free their one-listing slot.
            restore_allowed = False
            if previous.status == ListingStatus.REMOVED and previous.quick_submitted_by is None:
                last_action = await session.scalar(
                    select(AuditLog).where(AuditLog.guild_id == info.guild_id)
                    .order_by(AuditLog.id.desc()).limit(1)
                )
                stored_guild = await repository.get_guild(session, info.guild_id)
                restore_allowed = bool(
                    last_action is not None and last_action.action == "listing.quick_deleted"
                    and last_action.actor_id == actor_id
                    and stored_guild is not None and stored_guild.connected_by is None
                )
            if not restore_allowed:
                raise Conflict("This server has an existing listing. Only its verified managers can change it.")
            previous.quick_submitted_by = actor_id
        if previous.status == ListingStatus.SUSPENDED:
            raise Conflict("This server's listing is suspended. Contact Parley staff.")
        if previous.status == ListingStatus.PENDING:
            raise Conflict("Your Quick Post is already awaiting staff review.")
        if previous.awaiting_ad and previous.status == ListingStatus.ACTIVE:
            raise Conflict("Your server is approved. Open My Server Listings and press Post Approved Ad first.")
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
            previous.awaiting_ad = awaiting_ad
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
        awaiting_ad=awaiting_ad, created_at=now, updated_at=now, refreshed_at=now,
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
    verified_invite_codes: Collection[str] = (),
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
        verified_invite_codes=verified_invite_codes,
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


async def delete_quick_listing(
    session: AsyncSession, config: RuntimeConfig, *, actor_id: int, guild_id: int, now,
) -> Listing:
    """Remove only the caller's own *unverified submission*, never guild authority.

    Retain a removed tombstone for audit. Other strangers cannot reclaim it;
    the original submitter may re-submit after cooldown unless staff intervene.
    Release the submitter's one-slot limit without resetting the cooldown.
    """
    row = await session.scalar(select(Listing).where(Listing.guild_id == guild_id).with_for_update())
    if row is None or row.quick_submitted_by != actor_id or row.status == ListingStatus.REMOVED:
        raise Conflict("This Free Listing is no longer available to delete.")
    row.status = ListingStatus.REMOVED
    row.quick_submitted_by = None
    row.pending_changes = None
    row.pending_submitted_by = None
    row.review_revision = (row.review_revision or 0) + 1  # invalidate old approval buttons
    row.updated_at = now
    if row.refreshed_at is not None:
        remaining = row.refreshed_at + timedelta(minutes=config.listings.quick_post_cooldown_minutes) - now
        if remaining.total_seconds() > 0:
            await cooldowns.start(session, "quick_post_deleted", actor_id, now, remaining)
    await repository.add_audit(session, "listing.quick_deleted", actor_id=actor_id, guild_id=guild_id)
    return row


async def publish_approved_ad(
    session: AsyncSession, config: RuntimeConfig, *, guild_id: int, actor_id: int,
    info: listings.GuildInfo, text: str, verified_invite_codes: Collection[str], now,
) -> Listing:
    """One-time transition from staff-approved server to a real, formatted ad.

    The database is the truth: confirmed text is saved before any Discord API
    call. If the bot crashes afterwards, background publishing can retry safely.
    The published copy is limited to same-guild Discord invites; no external
    links, mass pings or mentions can slip past the server-only staff review.
    """
    await moderation.ensure_allowed(session, config, guild_ids=[guild_id], user_id=actor_id)
    row = await session.scalar(select(Listing).where(Listing.guild_id == guild_id).with_for_update())
    if not row or row.quick_submitted_by != actor_id or row.status != ListingStatus.ACTIVE or not row.awaiting_ad:
        raise Conflict("This server is not waiting for your approved advertisement.")
    if info.guild_id != guild_id or not row.invite_url:
        raise Conflict("The approved server no longer matches. Ask staff for help.")
    content, unsafe = prepare_quick_ad(
        config, info=info, invite_url=row.invite_url, category=row.categories[0],
        raw_ad=text, verified_invite_codes=verified_invite_codes,
    )
    if unsafe:
        raise ValidationError("Approved-server ads may contain only Discord invite links for this server. Remove external links.")
    row.advertisement_text = content
    row.awaiting_ad = False
    row.refreshed_at = now  # cooldown begins AFTER actually posting, not approval
    row.updated_at = now
    await repository.add_audit(session, "listing.quick_approved_ad_confirmed", actor_id=actor_id, guild_id=guild_id)
    return row
