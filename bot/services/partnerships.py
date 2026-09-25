"""Partnership request rules. No Discord API calls happen here."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import RuntimeConfig
from bot.database import repository
from bot.database.models import Listing, ListingStatus, PartnershipRequest, RequestStatus
from bot.services import cooldowns, moderation
from bot.services.errors import Conflict, CooldownActive, NotFound, PermissionDenied, ValidationError
from bot.utils.helpers import format_duration

log = logging.getLogger(__name__)

ALREADY_PENDING = "A partnership request between these servers is already pending."


async def _pair_names(session: AsyncSession, source_guild_id: int, target_guild_id: int) -> tuple[str, str]:
    guilds = await repository.get_guilds(session, [source_guild_id, target_guild_id])
    source = guilds.get(source_guild_id)
    target = guilds.get(target_guild_id)
    return (
        source.name if source else f"Server {source_guild_id}",
        target.name if target else f"Server {target_guild_id}",
    )


def _same_pending_message(source_name: str, target_name: str) -> str:
    return (
        f"**{source_name}** already has a pending partnership request for **{target_name}**. "
        "Another admin may have sent it already. Check **Partnership Requests** before sending another."
    )


def _reverse_pending_message(source_name: str, target_name: str) -> str:
    return (
        f"**{target_name}** already sent **{source_name}** a partnership request. "
        "Check **Partnership Requests** to accept or decline it instead of sending a second request."
    )


@dataclass
class RequestContext:
    request: PartnershipRequest
    source: Listing
    target: Listing


async def create_request(
    session: AsyncSession,
    config: RuntimeConfig,
    *,
    source_guild_id: int,
    target_guild_id: int,
    requester_id: int,
    source_member_count: int,
    message: str | None,
    now: datetime,
) -> RequestContext:
    rules = config.partnerships

    if source_guild_id == target_guild_id:
        raise ValidationError("A server can't partner with itself.")

    await moderation.ensure_allowed(session, config, guild_ids=[source_guild_id, target_guild_id], user_id=requester_id)

    target = await repository.get_listing(session, target_guild_id)
    if target is None or target.status != ListingStatus.ACTIVE:
        raise NotFound("This listing no longer exists.")
    if not target.accepting_partnerships:
        raise ValidationError("This server is not accepting partnerships.")

    source = await repository.get_listing(session, source_guild_id)
    if source is None or source.status != ListingStatus.ACTIVE:
        raise ValidationError("Your server needs an active Parley listing before it can request partnerships.")

    if rules.enforce_minimum_members and source_member_count < target.minimum_members:
        raise ValidationError(f"This server asks for partners with at least {target.minimum_members:,} members.")

    text = (message or "").strip() or None
    if text is not None:
        if len(text) > rules.max_message_length:
            raise ValidationError(f"Your message is too long (limit {rules.max_message_length} characters).")
        moderation.ensure_clean(text, config)

    source_name, target_name = await _pair_names(session, source_guild_id, target_guild_id)
    if await repository.pending_request_between(session, target_guild_id, source_guild_id):
        raise Conflict(_reverse_pending_message(source_name, target_name))
    if await repository.pending_request_between(session, source_guild_id, target_guild_id):
        raise Conflict(_same_pending_message(source_name, target_name))

    paired = await cooldowns.remaining(
        session, cooldowns.SCOPE_PARTNER_PAIR, cooldowns.unordered_pair_key(source_guild_id, target_guild_id), now
    )
    if paired:
        raise CooldownActive(f"These servers partnered recently. Try again in {format_duration(paired)}.", paired)

    declined = await cooldowns.remaining(
        session, cooldowns.SCOPE_DECLINED_PAIR, cooldowns.pair_key(source_guild_id, target_guild_id), now
    )
    if declined:
        raise CooldownActive(
            f"This server declined recently. You can ask again in {format_duration(declined)}.", declined
        )

    waiting = await cooldowns.remaining(session, cooldowns.SCOPE_REQUEST_USER, requester_id, now)
    if waiting:
        raise CooldownActive(f"Please wait {format_duration(waiting)} before sending another request.", waiting)

    server_waiting = await cooldowns.remaining(session, cooldowns.SCOPE_REQUEST_SERVER, source_guild_id, now)
    if server_waiting:
        raise CooldownActive(
            f"**{source_name}** just sent a partnership request. Another admin may have sent it. "
            f"Try again in {format_duration(server_waiting)}.",
            server_waiting,
        )

    if await repository.count_pending_outgoing(session, source_guild_id) >= rules.max_pending_requests:
        raise ValidationError(
            f"Your server already has {rules.max_pending_requests} pending requests. Wait for some replies first."
        )

    request = PartnershipRequest(
        source_guild_id=source_guild_id,
        target_guild_id=target_guild_id,
        requester_user_id=requester_id,
        message=text,
        status=RequestStatus.PENDING,
        created_at=now,
    )
    session.add(request)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The partial unique index caught two admins sending the same pair at the same moment.
        raise Conflict(_same_pending_message(source_name, target_name)) from exc

    await cooldowns.start(
        session, cooldowns.SCOPE_REQUEST_USER, requester_id, now, timedelta(seconds=rules.request_cooldown_seconds)
    )
    await cooldowns.start(
        session, cooldowns.SCOPE_REQUEST_SERVER, source_guild_id, now,
        timedelta(seconds=rules.server_request_cooldown_seconds),
    )
    await repository.add_audit(
        session,
        "partnership.requested",
        actor_id=requester_id,
        guild_id=source_guild_id,
        details={"request_id": request.id, "target_guild_id": target_guild_id},
    )
    log.info(
        "partnership.requested id=%s source=%s target=%s requester=%s",
        request.id, source_guild_id, target_guild_id, requester_id,
    )
    return RequestContext(request=request, source=source, target=target)


async def respond(
    session: AsyncSession,
    config: RuntimeConfig,
    *,
    request_id: int,
    responder_id: int,
    responder_is_manager: bool,
    accept: bool,
    now: datetime,
) -> PartnershipRequest:
    request = await repository.get_request(session, request_id)
    if request is None:
        raise NotFound("This request no longer exists.")

    if not responder_is_manager and not await repository.is_contact(session, request.target_guild_id, responder_id):
        raise PermissionDenied("Only this server's partnership contacts or managers can answer this request.")

    if request.status != RequestStatus.PENDING:
        raise Conflict(f"This request was already {request.status}.")

    new_status = RequestStatus.ACCEPTED if accept else RequestStatus.DECLINED
    # Conditional update: if two contacts click at the same moment only one wins.
    result = await session.execute(
        update(PartnershipRequest)
        .where(PartnershipRequest.id == request_id, PartnershipRequest.status == RequestStatus.PENDING)
        .values(status=new_status, responded_at=now, responded_by=responder_id)
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        raise Conflict("Someone else already answered this request.")
    await session.refresh(request)

    if accept:
        await cooldowns.start(
            session, cooldowns.SCOPE_PARTNER_PAIR,
            cooldowns.unordered_pair_key(request.source_guild_id, request.target_guild_id),
            now, timedelta(hours=config.partnerships.pair_cooldown_hours),
        )
    else:
        await cooldowns.start(
            session, cooldowns.SCOPE_DECLINED_PAIR,
            cooldowns.pair_key(request.source_guild_id, request.target_guild_id),
            now, timedelta(hours=config.partnerships.decline_cooldown_hours),
        )

    await repository.add_audit(
        session,
        f"partnership.{new_status}",
        actor_id=responder_id,
        guild_id=request.target_guild_id,
        details={"request_id": request.id, "source_guild_id": request.source_guild_id},
    )
    log.info("partnership.%s id=%s responder=%s", new_status, request.id, responder_id)
    return request


async def expire_requests(session: AsyncSession, config: RuntimeConfig, now: datetime) -> list[PartnershipRequest]:
    days = config.partnerships.request_expiration_days
    if days <= 0:
        return []
    stale = await repository.requests_older_than(session, now - timedelta(days=days))
    for request in stale:
        request.status = RequestStatus.EXPIRED
        request.responded_at = now
    if stale:
        log.info("partnership.expired count=%s", len(stale))
    return stale


async def claim_looking_post(
    session: AsyncSession,
    config: RuntimeConfig,
    *,
    source_guild_id: int,
    actor_id: int,
    message: str | None,
    now: datetime,
) -> str | None:
    """Validate a structured looking-for-partner post and start its cooldown."""
    await moderation.ensure_allowed(session, config, guild_ids=[source_guild_id], user_id=actor_id)
    source = await repository.get_listing(session, source_guild_id)
    if source is None or source.status != ListingStatus.ACTIVE:
        raise ValidationError("Your server needs an active Parley listing to use structured posts.")

    text = (message or "").strip() or None
    if text is not None:
        if len(text) > config.partnerships.max_message_length:
            raise ValidationError(
                f"Your message is too long (limit {config.partnerships.max_message_length} characters)."
            )
        moderation.ensure_clean(text, config)

    waiting = await cooldowns.remaining(session, cooldowns.SCOPE_LOOKING_POST, source_guild_id, now)
    if waiting:
        raise CooldownActive(f"This server can post again in {format_duration(waiting)}.", waiting)

    await cooldowns.start(
        session,
        cooldowns.SCOPE_LOOKING_POST,
        source_guild_id,
        now,
        timedelta(minutes=config.partnerships.looking_post_cooldown_minutes),
    )
    await repository.add_audit(session, "looking.posted", actor_id=actor_id, guild_id=source_guild_id)
    return text
