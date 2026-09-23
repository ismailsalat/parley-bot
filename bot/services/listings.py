"""Listing business rules. No Discord API calls happen here."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import RuntimeConfig
from bot.database import repository
from bot.database.models import Listing, ListingStatus
from bot.services import moderation
from bot.services.errors import LISTING_GONE, Conflict, CooldownActive, NotFound, ValidationError
from bot.utils.helpers import format_duration

log = logging.getLogger(__name__)

ALREADY_LISTED = "This server is already listed."
_INVITE_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]{2,40})/?$",
    re.IGNORECASE,
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class GuildInfo:
    guild_id: int
    name: str
    icon_url: str | None
    member_count: int


@dataclass
class ListingInput:
    categories: list[str]
    accepting_partnerships: bool
    minimum_members: int
    contact_ids: list[int]
    advertisement_text: str
    invite_url: str | None = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- validation


URL_PATTERN = re.compile(r"https?://[^\s<>\])]+|(?<![@\w.])(?:[\w-]+\.)+[a-z]{2,}(?:/[^\s<>\])]*)?", re.IGNORECASE)
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):(\d{15,21})>")
# Link shorteners hide their destination, so an ad using one gets a staff look.
SHORTENERS = frozenset({
    "bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "is.gd", "buff.ly", "adf.ly", "shorte.st",
    "cutt.ly", "rb.gy", "rebrand.ly", "shorturl.at", "tiny.cc", "linktr.ee",
})
SAFE_HOSTS = frozenset({"discord.gg", "discord.com", "discordapp.com", "www.discord.com"})


def _host(url: str) -> str:
    host = url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") and host not in SAFE_HOSTS else host


def find_links(text: str) -> list[str]:
    return [m.group(0).rstrip(".,!?)") for m in URL_PATTERN.finditer(text or "")]


def _matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def check_links(text: str, config: RuntimeConfig) -> bool:
    """Validate the links in an ad. Returns True when staff should look at it.

    Blocked domains and too many links are refused outright. Links Parley
    can't judge (shorteners, bare IP addresses) are held for review or blocked,
    depending on ``moderation.link_action``. This is not malware detection.
    """
    rules = config.moderation
    links = find_links(text)
    if rules.max_links and len(links) > rules.max_links:
        raise ValidationError(f"That ad has {len(links)} links. Please use at most {rules.max_links}.")
    suspicious = False
    for link in links:
        host = _host(link)
        if any(_matches_domain(host, domain) for domain in rules.blocked_domains):
            raise ValidationError(f"Links to **{host}** aren't allowed here.")
        if host in SAFE_HOSTS:
            continue
        if host in SHORTENERS or re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
            if rules.link_action == "block":
                raise ValidationError(f"Shortened or hidden links (**{host}**) aren't allowed. Use the full link.")
            suspicious = rules.link_action == "review"
    return suspicious


def custom_emoji_ids(text: str) -> list[int]:
    return [int(match.group(2)) for match in CUSTOM_EMOJI_PATTERN.finditer(text or "")]


def build_simple_ad(
    *,
    name: str,
    member_count: int,
    categories: Sequence[str],
    accepting: bool,
    minimum: int,
    invite_url: str | None,
    description: str,
    extra: str = "",
) -> str:
    """The Simple Ad Builder: a clean normal message from what Parley already knows."""
    lines = [f"## {name.strip()}", "", description.strip()]
    if extra.strip():
        lines += ["", extra.strip()]
    facts = [f"**Members:** {member_count:,}"]
    if categories:
        facts.append(f"**Category:** {', '.join(categories)}")
    facts.append(f"**Partnerships:** {'Open' if accepting else 'Closed'}")
    if accepting and minimum:
        facts.append(f"**Minimum:** {minimum:,}+")
    lines += ["", *facts]
    if invite_url:
        lines += ["", invite_url]
    return "\n".join(lines)


def clean_advertisement(text: str, config: RuntimeConfig) -> str:
    """Validate an advertisement while preserving its formatting exactly.

    Only outer blank space and invisible control characters are removed;
    Markdown, spacing, line breaks, emoji syntax and Unicode are untouched.
    """
    cleaned = _CONTROL_CHARS.sub("", text or "").strip("\n\r\t ")
    if not cleaned:
        raise ValidationError("Your advertisement is empty.")
    limit = config.listings.max_ad_length
    if len(cleaned) > limit:
        raise ValidationError(f"Your advertisement is {len(cleaned)} characters. The limit is {limit}.")
    moderation.ensure_clean(cleaned, config)
    return cleaned


def clean_categories(categories: Sequence[str], config: RuntimeConfig) -> list[str]:
    allowed = config.listings.categories
    chosen = [c for c in dict.fromkeys(categories) if c in allowed]
    if not chosen:
        raise ValidationError("Please choose a category.")
    if len(chosen) > config.listings.max_categories:
        raise ValidationError(f"You can choose up to {config.listings.max_categories} categories.")
    return chosen


def clean_minimum(minimum: int, accepting: bool) -> int:
    if not accepting:
        return 0
    if minimum < 0:
        raise ValidationError("Minimum members can't be negative.")
    return minimum


def clean_contacts(contact_ids: Sequence[int], actor_id: int, config: RuntimeConfig) -> list[int]:
    contacts = list(dict.fromkeys(contact_ids)) or [actor_id]
    if len(contacts) > config.listings.max_contacts:
        raise ValidationError(f"You can choose up to {config.listings.max_contacts} partnership contacts.")
    return contacts


def parse_invite_code(raw: str) -> str:
    match = _INVITE_RE.match((raw or "").strip())
    if not match:
        raise ValidationError("That doesn't look like a Discord invite link (for example https://discord.gg/abc123).")
    return match.group(1)


def canonical_invite(code: str) -> str:
    return f"https://discord.gg/{code}"


def is_visible(listing: Listing | None) -> bool:
    return listing is not None and listing.status == ListingStatus.ACTIVE


def require_visible(listing: Listing | None) -> Listing:
    if not is_visible(listing):
        raise NotFound(LISTING_GONE)
    assert listing is not None
    return listing


# ---------------------------------------------------------------- create / edit


async def create_listing(
    session: AsyncSession,
    config: RuntimeConfig,
    *,
    guild: GuildInfo,
    actor_id: int,
    data: ListingInput,
    now: datetime,
    is_test: bool = False,
) -> Listing:
    """Create the one listing for this guild.

    A previously removed/expired listing row is reused, so a guild never has
    more than one row (enforced by a unique constraint on ``guild_id``).
    """
    await moderation.ensure_allowed(session, config, guild_ids=[guild.guild_id], user_id=actor_id)

    existing = await repository.get_listing(session, guild.guild_id)
    if existing is not None and existing.status in ListingStatus.LIVE:
        raise Conflict(ALREADY_LISTED)

    categories = clean_categories(data.categories, config)
    text = clean_advertisement(data.advertisement_text, config)
    minimum = clean_minimum(data.minimum_members, data.accepting_partnerships)
    contacts = clean_contacts(data.contact_ids, actor_id, config)
    if config.listings.invite_required and not data.invite_url:
        raise ValidationError("An invite link is required.")

    status = (
        ListingStatus.PENDING
        if config.listings.approval_required or check_links(data.advertisement_text, config)
        else ListingStatus.ACTIVE
    )
    await repository.upsert_guild(
        session,
        guild_id=guild.guild_id,
        name=guild.name,
        icon_url=guild.icon_url,
        member_count=guild.member_count,
        connected_by=actor_id,
    )

    if existing is None:
        listing = Listing(guild_id=guild.guild_id, created_at=now)
        session.add(listing)
    else:
        listing = existing
        listing.message_id = None
        listing.channel_id = None

    listing.advertisement_text = text
    listing.category = ",".join(categories)
    listing.accepting_partnerships = data.accepting_partnerships
    listing.minimum_members = minimum
    listing.invite_url = data.invite_url
    listing.status = status
    listing.refreshed_at = now
    listing.last_ad_edit_at = None
    listing.updated_at = now
    listing.is_test = is_test
    listing.pending_changes = None
    listing.pending_submitted_by = actor_id if status == ListingStatus.PENDING else None
    listing.review_revision = (listing.review_revision or 0) + 1

    try:
        await session.flush()
    except IntegrityError as exc:
        # Two setups raced: the database unique constraint rejected the duplicate.
        raise Conflict(ALREADY_LISTED) from exc

    await repository.replace_contacts(session, guild.guild_id, contacts)
    await repository.add_audit(
        session,
        "listing.created",
        actor_id=actor_id,
        guild_id=guild.guild_id,
        details={"status": status, "categories": categories, "reused_row": existing is not None},
    )
    log.info("listing.created guild_id=%s actor_id=%s status=%s", guild.guild_id, actor_id, status)
    return listing


async def get_editable_listing(session: AsyncSession, guild_id: int) -> Listing:
    listing = await repository.get_listing(session, guild_id)
    if listing is None or listing.status == ListingStatus.REMOVED:
        raise NotFound(LISTING_GONE)
    return listing


def _needs_review(listing: Listing, config: RuntimeConfig, *, ad_change: bool) -> bool:
    """Does this edit of an *approved* listing have to wait for staff?"""
    rules = config.listings
    if not rules.approval_required or listing.status != ListingStatus.ACTIVE:
        return False
    return rules.reapprove_ad_edits if ad_change else rules.reapprove_info_edits


def _stage(listing: Listing, changes: dict, actor_id: int) -> None:
    """Queue changes for review. The approved content stays live meanwhile."""
    listing.pending_changes = {**(listing.pending_changes or {}), **changes}  # new dict: JSON change tracking
    listing.pending_submitted_by = actor_id
    listing.review_revision = (listing.review_revision or 0) + 1


async def update_advertisement(
    session: AsyncSession, config: RuntimeConfig, *, guild_id: int, text: str, actor_id: int, now: datetime
) -> Listing:
    """Change the ad text.

    With approval + re-approval on, an approved listing keeps its live text and
    the new text waits in ``pending_changes`` (``listing.awaiting_review``).
    A listing still waiting for its first approval gets a new review revision.
    """
    await moderation.ensure_allowed(session, config, guild_ids=[guild_id], user_id=actor_id)
    listing = await get_editable_listing(session, guild_id)
    cleaned = clean_advertisement(text, config)
    link_review = check_links(cleaned, config) and listing.status == ListingStatus.ACTIVE
    if link_review or _needs_review(listing, config, ad_change=True):
        if cleaned == listing.advertisement_text and "advertisement_text" not in (listing.pending_changes or {}):
            return listing  # nothing actually changed
        _stage(listing, {"advertisement_text": cleaned}, actor_id)
        action = "listing.ad_edit_submitted"
    else:
        listing.advertisement_text = cleaned
        if listing.status == ListingStatus.PENDING:
            listing.review_revision = (listing.review_revision or 0) + 1
            listing.pending_submitted_by = actor_id
        action = "listing.ad_edited"
    listing.updated_at = now
    await repository.add_audit(session, action, actor_id=actor_id, guild_id=guild_id)
    log.info("%s guild_id=%s actor_id=%s", action, guild_id, actor_id)
    return listing


async def update_info(
    session: AsyncSession,
    config: RuntimeConfig,
    *,
    guild_id: int,
    categories: Sequence[str],
    accepting_partnerships: bool,
    minimum_members: int,
    contact_ids: Sequence[int],
    invite_url: str | None,
    actor_id: int,
    now: datetime,
) -> Listing:
    """Change listing info. Contacts always apply at once (they are never public)."""
    await moderation.ensure_allowed(session, config, guild_ids=[guild_id], user_id=actor_id)
    listing = await get_editable_listing(session, guild_id)
    if config.listings.invite_required and not invite_url:
        raise ValidationError("An invite link is required.")
    public = {
        "category": ",".join(clean_categories(categories, config)),
        "accepting_partnerships": accepting_partnerships,
        "minimum_members": clean_minimum(minimum_members, accepting_partnerships),
        "invite_url": invite_url,
    }
    await repository.replace_contacts(session, guild_id, clean_contacts(contact_ids, actor_id, config))

    changed = {k: v for k, v in public.items() if getattr(listing, k) != v}
    if changed and _needs_review(listing, config, ad_change=False):
        _stage(listing, changed, actor_id)
        action = "listing.info_edit_submitted"
    else:
        for key, value in public.items():
            setattr(listing, key, value)
        if changed and listing.status == ListingStatus.PENDING:
            listing.review_revision = (listing.review_revision or 0) + 1
        action = "listing.info_edited"
    listing.updated_at = now
    await repository.add_audit(session, action, actor_id=actor_id, guild_id=guild_id)
    log.info("%s guild_id=%s actor_id=%s", action, guild_id, actor_id)
    return listing


# ---------------------------------------------------------------- refresh


def ad_edit_available(listing: Listing) -> bool:
    """One ad edit is allowed during each Relist cycle.

    ``refreshed_at`` marks the current cycle. A successful edit records
    ``last_ad_edit_at``. Relisting advances ``refreshed_at`` and automatically
    unlocks one new edit without another counter or reset job.
    """
    if listing.last_ad_edit_at is None:
        return True
    if listing.refreshed_at is None:
        return False
    return listing.last_ad_edit_at < listing.refreshed_at


async def mark_ad_edit_used(session: AsyncSession, *, guild_id: int, actor_id: int, now: datetime) -> Listing:
    listing = await get_editable_listing(session, guild_id)
    listing.last_ad_edit_at = now
    listing.updated_at = now
    await repository.add_audit(session, "listing.ad_edit_cycle_used", actor_id=actor_id, guild_id=guild_id)
    return listing


def refresh_remaining(listing: Listing, config: RuntimeConfig, now: datetime, *, connected: bool = False) -> timedelta | None:
    if listing.refreshed_at is None:
        return None
    cooldown = config.listings.connected_refresh_cooldown_minutes if connected else config.listings.refresh_cooldown_minutes
    available_at = listing.refreshed_at + timedelta(minutes=cooldown)
    return available_at - now if available_at > now else None


async def claim_refresh(
    session: AsyncSession, config: RuntimeConfig, *, guild_id: int, actor_id: int, now: datetime, connected: bool = False
) -> Listing:
    """Check every Relist rule and record the Relist time *before* reposting.

    Recording first means double-clicks can't trigger two reposts.
    """
    await moderation.ensure_allowed(session, config, guild_ids=[guild_id], user_id=actor_id)
    listing = await get_editable_listing(session, guild_id)
    if listing.status == ListingStatus.SUSPENDED:
        raise ValidationError("This listing is suspended by Parley staff.")
    if listing.status == ListingStatus.PENDING:
        raise ValidationError("This listing is still waiting for staff approval.")

    if listing.status == ListingStatus.ACTIVE:
        remaining = refresh_remaining(listing, config, now, connected=connected)
        if remaining is not None:
            raise CooldownActive(f"You can Relist again in {format_duration(remaining)}.", remaining)

    listing.status = ListingStatus.ACTIVE  # an expired listing comes back on refresh
    listing.refreshed_at = now
    await repository.add_audit(session, "listing.refreshed", actor_id=actor_id, guild_id=guild_id)
    log.info("listing.refreshed guild_id=%s actor_id=%s", guild_id, actor_id)
    return listing


async def record_message(
    session: AsyncSession, *, guild_id: int, channel_id: int | None, message_id: int | None
) -> None:
    """Remember where a listing is published (a Parley-posted message)."""
    listing = await repository.get_listing(session, guild_id)
    if listing is None:
        return
    listing.channel_id = channel_id
    listing.message_id = message_id
    listing.controls_message_id = None
    listing.self_posted = False


async def remove_listing(session: AsyncSession, *, guild_id: int, actor_id: int, now: datetime) -> Listing:
    listing = await get_editable_listing(session, guild_id)
    listing.status = ListingStatus.REMOVED
    listing.pending_changes = None
    listing.updated_at = now
    for request in await repository.pending_requests_involving(session, guild_id):
        request.status = "cancelled"
        request.responded_at = now
    await repository.add_audit(session, "listing.removed", actor_id=actor_id, guild_id=guild_id)
    log.info("listing.removed guild_id=%s actor_id=%s", guild_id, actor_id)
    return listing


REVIEW_OUTDATED = "This review is outdated: a newer version was submitted. Use the latest review message."
REVIEW_KINDS = ("new", "edit")
EDITABLE_FIELDS = ("advertisement_text", "category", "accepting_partnerships", "minimum_members", "invite_url")


async def review_listing(
    session: AsyncSession,
    *,
    guild_id: int,
    approve: bool,
    moderator_id: int,
    now: datetime,
    revision: int | None = None,
) -> tuple[Listing, str]:
    """Approve or reject what is waiting. Returns (listing, "new" | "edit").

    ``revision`` comes from the review button. A stale button (the owner edited
    again after the review was posted) is refused so staff never approve
    content they haven't seen. Buttons from before revisions existed pass None
    and are only accepted for revision 0.
    """
    listing = await repository.get_listing(session, guild_id)
    if listing is None or not listing.awaiting_review:
        raise Conflict("This listing is no longer waiting for review.")
    current = listing.review_revision or 0
    if (revision is None and current > 1) or (revision is not None and revision != current):
        raise Conflict(REVIEW_OUTDATED)

    if listing.status == ListingStatus.PENDING:
        kind = "new"
        listing.status = ListingStatus.ACTIVE if approve else ListingStatus.REMOVED
        if approve:
            listing.refreshed_at = now
    else:
        kind = "edit"
        if approve:
            for key, value in (listing.pending_changes or {}).items():
                if key in EDITABLE_FIELDS:
                    setattr(listing, key, value)
            listing.updated_at = now
    listing.pending_changes = None
    listing.pending_submitted_by = None
    verdict = "approved" if approve else "rejected"
    await repository.add_audit(
        session, f"listing.{'edit_' if kind == 'edit' else ''}{verdict}", actor_id=moderator_id, guild_id=guild_id,
        details={"revision": current},
    )
    log.info("listing.review guild_id=%s kind=%s verdict=%s revision=%s", guild_id, kind, verdict, current)
    return listing, kind


async def record_review_message(
    session: AsyncSession, *, guild_id: int, channel_id: int | None, message_id: int | None
) -> tuple[int | None, int | None]:
    """Store the new review message; returns the previous one so it can be marked outdated."""
    listing = await repository.get_listing(session, guild_id)
    if listing is None:
        return None, None
    previous = (listing.review_channel_id, listing.review_message_id)
    listing.review_channel_id = channel_id
    listing.review_message_id = message_id
    return previous


async def expire_listings(session: AsyncSession, config: RuntimeConfig, now: datetime) -> list[Listing]:
    days = config.listings.expiration_days
    if days <= 0:
        return []
    expired = await repository.listings_to_expire(session, now - timedelta(days=days))
    for listing in expired:
        listing.status = ListingStatus.EXPIRED
        await repository.add_audit(session, "listing.expired", guild_id=listing.guild_id)
    if expired:
        log.info("listing.expired count=%s", len(expired))
    return expired
