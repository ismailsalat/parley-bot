"""Unverified invite-only submissions. A submission NEVER creates manager rights."""
from __future__ import annotations

from datetime import datetime, timedelta
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
    # "18+" or "adults only" can describe a legitimate non-sexual server.
    # Reject clear NSFW/explicit advertising, never age wording by itself.
    if search(r"(?i)(?<![a-z0-9])(?:nsfw|porn(?:ography)?|sexual[ -]content|explicit[ -]content)(?![a-z0-9])", f"{info.name} {description} {raw_ad or ''}"):
        raise ValidationError("Sexually explicit or NSFW communities and ads aren't allowed on Parley.")
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
        and not listings.is_discord_attachment_image(url)
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


async def listing_attribution(session: AsyncSession, row: Listing) -> str:
    """Identify the recorded submitter without treating them as a verified owner."""
    actor = row.quick_submitted_by
    if actor is None:
        actor = await session.scalar(select(AuditLog.actor_id).where(
            AuditLog.guild_id == row.guild_id,
            AuditLog.action.in_(("listing.created", "listing.quick_submitted", "listing.quick_resubmitted")),
            AuditLog.actor_id.is_not(None),
        ).order_by(AuditLog.id.desc()).limit(1))
    if actor is None:
        return "**Submitted by:** Not recorded on this older listing. Staff can review its history."
    return f"**Submitted by:** <@{actor}> · ID `{actor}`"


async def deleted_cooldown_remaining(session: AsyncSession, config: RuntimeConfig, actor_id: int, now):
    """Recompute deleted/switched slot timing from its durable audit timestamp.

    Cooldown rows can be cleaned up after expiry. The deletion audit survives
    that cleanup and allows a changed setting to take effect immediately.
    """
    event = await session.scalar(select(AuditLog).where(
        AuditLog.actor_id == actor_id,
        AuditLog.action.in_(("listing.quick_deleted", "listing.quick_slot_released",
                            "test.my_free_listing_reset", "test.my_free_cooldown_reset")),
    ).order_by(AuditLog.id.desc()).limit(1))
    if event is not None and event.action.startswith("test."):
        return None
    origin = None
    details = event.details or {} if event else {}
    if "cooldown_started_at" in details:
        if details["cooldown_started_at"] is None:
            return None
        origin = datetime.fromisoformat(details["cooldown_started_at"])
    elif event:
        row = await repository.get_listing(session, event.guild_id)
        if row is not None and row.status == ListingStatus.REMOVED and row.quick_submitted_by is None:
            origin = row.refreshed_at
            if origin is None:
                return None
    if origin is None:
        return await cooldowns.remaining(session, "quick_post_deleted", actor_id, now)
    ready = origin + timedelta(minutes=config.listings.quick_post_cooldown_minutes)
    return ready - now if ready > now else None


async def can_restore_deleted_listing(session: AsyncSession, row: Listing, actor_id: int) -> bool:
    """Only an original submitter may restore their own deleted Free Listing.

    An invite alone must never reclaim a listing, including a legacy or
    staff-removed record. A deletion preserves the original actor in the audit
    trail even though the one-listing slot is released in ``quick_submitted_by``.
    """
    if row.status != ListingStatus.REMOVED or row.quick_submitted_by is not None:
        return False
    latest = await session.scalar(
        select(AuditLog).where(AuditLog.guild_id == row.guild_id)
        .order_by(AuditLog.id.desc()).limit(1)
    )
    guild = await repository.get_guild(session, row.guild_id)
    return bool(
        latest is not None
        and latest.action == "listing.quick_deleted"
        and latest.actor_id == actor_id
        and guild is not None
        and guild.connected_by is None
    )


def existing_listing_guidance(row: Listing) -> str:
    """Actionable guidance when a guild is already listed."""
    if row.status == ListingStatus.SUSPENDED:
        return ("This server's listing is suspended. Ask Parley staff to review it; "
                "a new invite cannot bypass a suspension.")
    if row.quick_submitted_by is not None:
        return (
            "This Discord server already has a Free Listing submitted from another account. "
            f"Its submitter is <@{row.quick_submitted_by}> (ID `{row.quick_submitted_by}`). "
            "A different invite or vanity link still points to the same server. "
            "If you're an admin, use **Connected Servers** to verify management "
            "inside your server, or contact Parley staff for a listing review."
        )
    if row.status == ListingStatus.REMOVED:
        return (
            "This server has a previous listing that cannot be reclaimed with an invite. "
            "Ask Parley staff to review its ownership or removal."
        )
    return (
        "This Discord server already has a managed or legacy listing. "
        "If you manage the server, choose **Connected Servers** to verify your "
        "permissions; otherwise contact Parley staff."
    )


async def ensure_not_connected(session: AsyncSession, guild_id: int) -> None:
    """Protect a Connected listing if a legacy/stale Free submitter ID remains.

    A guild can gain a verified manager while a user holds an old Free Listing
    menu open. Never let that menu change or delete a now-connected ad.
    """
    guild = await repository.get_guild(session, guild_id)
    if guild is not None and guild.connected_by is not None:
        raise Conflict(
            "This server is connected to Parley now. Use **Connected Servers** "
            "with verified Manage Server permissions to change its listing."
        )


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
    additional_review: bool = False,
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
    requires_review = config.listings.approval_required or force_review or additional_review
    # Staff approval is for SERVER + category + summary. Full ad comes later.
    awaiting_ad = bool(config.listings.approval_required and raw_ad is None)
    if not invite_url or not invite_url.startswith('https://discord.gg/'):
        raise ValidationError("Please use a valid Discord invite.")

    owned = await my_quick_listing(session, actor_id)
    if owned and owned.guild_id != info.guild_id:
        raise Conflict("Quick Post allows one unconnected server per account. Connect Parley to manage additional servers.")
    # Deleting a listing frees the slot, not the posting cooldown. This persists
    # across bot restarts and prevents delete/recreate cycles from flooding feeds.
    wait = await deleted_cooldown_remaining(session, config, actor_id, now) if owned is None else None
    if wait is not None:
        raise CooldownActive(f"You can submit another Free Listing in {format_duration(wait)}.", wait)

    # PostgreSQL locks the existing guild row during relist/replace decisions.
    previous = await session.scalar(
        select(Listing).where(Listing.guild_id == info.guild_id).with_for_update()
    )
    if previous is not None:
        await ensure_not_connected(session, info.guild_id)
        if previous.quick_submitted_by != actor_id:
            if not await can_restore_deleted_listing(session, previous, actor_id):
                raise Conflict(existing_listing_guidance(previous))
            previous.quick_submitted_by = actor_id
        if previous.status == ListingStatus.SUSPENDED:
            raise Conflict("This server's listing is suspended. Contact Parley staff.")
        if previous.status == ListingStatus.PENDING:
            raise Conflict("Your Quick Post is already awaiting staff review.")
        if previous.awaiting_ad and previous.status == ListingStatus.ACTIVE:
            raise Conflict("Your server is approved. Open My Server Listings and press Post Approved Ad first.")
        remaining = listings.refresh_remaining(previous, config, now)
        if remaining is not None:
            raise CooldownActive(
                f"You can repost this Free Listing in {format_duration(remaining)}. "
                f"The Free repost setting is {config.listings.quick_post_cooldown_minutes} minutes. "
                "Use **Edit Ad** to replace its text; **Repost** keeps the saved ad.", remaining,
            )
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
    additional_review: bool = False,
) -> tuple[Listing, str]:
    """One edit per relist cycle. Never changes unverified guild authority or rank."""
    await moderation.ensure_allowed(session, config, guild_ids=[guild_id], user_id=actor_id)
    row = await session.scalar(select(Listing).where(Listing.guild_id == guild_id).with_for_update())
    if row is None or row.quick_submitted_by != actor_id:
        raise Conflict("Only the original unverified submitter may edit this Quick Post.")
    await ensure_not_connected(session, guild_id)
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
    if config.listings.approval_required or unsafe_links or additional_review:
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


async def update_quick_invite(
    session: AsyncSession, config: RuntimeConfig, *, actor_id: int,
    guild_id: int, verified_guild_id: int, new_invite_url: str, now,
) -> Listing:
    """Replace a dead invite without granting new ownership or relisting.

    The Discord invite MUST have been fetched just before this call; compare
    the resolved guild ID here again to guard the database write path.
    """
    await moderation.ensure_allowed(session, config, guild_ids=[guild_id], user_id=actor_id)
    listing = await session.scalar(select(Listing).where(Listing.guild_id == guild_id).with_for_update())
    if listing is None or listing.quick_submitted_by != actor_id:
        raise Conflict("Only the Free Listing submitter can update this invite.")
    await ensure_not_connected(session, guild_id)
    if listing.status not in (ListingStatus.ACTIVE, ListingStatus.EXPIRED) or listing.pending_changes:
        raise Conflict("This listing cannot change its invite while it is awaiting review or suspended.")
    if verified_guild_id != guild_id:
        raise Conflict("The new invite belongs to a different Discord server.")
    code = listings.parse_invite_code(new_invite_url)
    canonical = listings.canonical_invite(code)
    old_code = listings.parse_invite_code(listing.invite_url) if listing.invite_url else None
    if canonical == listing.invite_url:
        raise ValidationError("That's already your listing's current invite.")
    # Replace only the *previously stored* invite, never arbitrary other URLs.
    # Previously verified vanity/alternate links remain untouched.
    if old_code:
        import re
        invite_pattern = re.compile(
            r"(?i)(?<![\w./-])(?:https?://)?(?:www\.)?"
            r"(?:discord\.gg/|discord(?:app)?\.com/invite/)"
            + re.escape(old_code) + r"(?=$|[\s)\]}>?#.,!*])"
        )
        replacement_text = invite_pattern.sub(lambda _m: canonical, listing.advertisement_text)
    else:
        replacement_text = listing.advertisement_text
    # The permanent join button always uses canonical. If the old text omitted
    # its canonical invite, append a working link only where it fits.
    if replacement_text == listing.advertisement_text and not advertisement_invite_codes(replacement_text):
        candidate = f"{replacement_text.rstrip()}\n\n{canonical}"
        if len(candidate) <= config.listings.max_ad_length:
            replacement_text = candidate
    listing.invite_url = canonical
    listing.advertisement_text = replacement_text
    listing.updated_at = now
    # Important: don't change refreshed_at, last_ad_edit_at, or review status.
    await repository.add_audit(session, "listing.quick_invite_updated", actor_id=actor_id,
                               guild_id=guild_id, details={"invite_code": code})
    return listing


async def delete_quick_listing(
    session: AsyncSession, config: RuntimeConfig, *, actor_id: int, guild_id: int, now,
) -> Listing:
    """Remove only the caller's own *unverified submission*, never guild authority.

    Retain a removed tombstone for audit. Other strangers cannot reclaim it;
    the original submitter may re-submit after cooldown unless staff intervene.
    Release the submitter's one-slot limit without resetting the cooldown.
    """
    row = await session.scalar(select(Listing).where(Listing.guild_id == guild_id).with_for_update())
    if row is None or row.quick_submitted_by != actor_id:
        raise Conflict("This Free Listing is no longer available to delete.")
    await ensure_not_connected(session, guild_id)
    # If staff removed an ad, its submitter must be able to free their slot
    # without creating an owner-deleted tombstone that permits guild reclaims.
    was_staff_removed = row.status in (ListingStatus.REMOVED, ListingStatus.SUSPENDED)
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
    await repository.add_audit(
        session,
        "listing.quick_slot_released" if was_staff_removed else "listing.quick_deleted",
        actor_id=actor_id, guild_id=guild_id,
        details={"cooldown_started_at": row.refreshed_at.isoformat() if row.refreshed_at else None},
    )
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
    await ensure_not_connected(session, guild_id)
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
