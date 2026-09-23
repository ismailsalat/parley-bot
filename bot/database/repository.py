"""Thin data-access functions. Business rules live in ``bot.services``."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    AuditLog,
    Cooldown,
    Guild,
    Listing,
    ListingContact,
    ListingStatus,
    ModerationBan,
    NetworkPost,
    NetworkSettings,
    PanelState,
    PartnershipRequest,
    RequestStatus,
    RuntimeSetting,
    SelfPostSession,
    TestMessage,
    utcnow,
)

# ---------------------------------------------------------------- guilds


async def get_guild(session: AsyncSession, guild_id: int) -> Guild | None:
    return await session.get(Guild, guild_id)


async def upsert_guild(
    session: AsyncSession,
    *,
    guild_id: int,
    name: str,
    icon_url: str | None,
    member_count: int,
    connected_by: int | None = None,
) -> Guild:
    guild = await session.get(Guild, guild_id)
    if guild is None:
        guild = Guild(guild_id=guild_id, name=name, icon_url=icon_url, member_count=member_count, connected_by=connected_by)
        session.add(guild)
    else:
        guild.name = name
        guild.icon_url = icon_url
        guild.member_count = member_count
        if connected_by is not None:
            guild.connected_by = connected_by
            guild.connected_at = utcnow()
    await session.flush()
    return guild


async def get_guilds(session: AsyncSession, guild_ids: Iterable[int]) -> dict[int, Guild]:
    ids = list(set(guild_ids))
    if not ids:
        return {}
    rows = await session.scalars(select(Guild).where(Guild.guild_id.in_(ids)))
    return {g.guild_id: g for g in rows}


# ---------------------------------------------------------------- listings


async def get_listing(session: AsyncSession, guild_id: int) -> Listing | None:
    return await session.scalar(select(Listing).where(Listing.guild_id == guild_id))


async def get_listing_by_message(session: AsyncSession, message_id: int) -> Listing | None:
    """Find the active listing whose owner-authored ad uses this Discord message."""
    return await session.scalar(
        select(Listing).where(
            Listing.message_id == message_id,
            Listing.self_posted.is_(True),
            Listing.status == ListingStatus.ACTIVE,
        )
    )


async def get_listings(session: AsyncSession, guild_ids: Iterable[int]) -> list[Listing]:
    ids = list(set(guild_ids))
    if not ids:
        return []
    return list(await session.scalars(select(Listing).where(Listing.guild_id.in_(ids))))


def _category_clause(category: str):
    return or_(
        Listing.category == category,
        Listing.category.like(f"{category},%"),
        Listing.category.like(f"%,{category}"),
        Listing.category.like(f"%,{category},%"),
    )


async def search_listings(
    session: AsyncSession,
    *,
    category: str | None,
    accepting_only: bool,
    limit: int,
    offset: int = 0,
    include_test: bool = False,
) -> tuple[list[Listing], int]:
    query = select(Listing).where(Listing.status == ListingStatus.ACTIVE)
    if not include_test:
        query = query.where(Listing.is_test.is_(False))
    if accepting_only:
        query = query.where(Listing.accepting_partnerships.is_(True))
    if category:
        query = query.where(_category_clause(category))
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(
        query.order_by(Listing.refreshed_at.desc().nulls_last(), Listing.id.desc()).limit(limit).offset(offset)
    )
    return list(rows), total


async def active_listings(
    session: AsyncSession, *, exclude_guild_id: int | None = None, include_test: bool = False
) -> list[Listing]:
    query = select(Listing).where(Listing.status == ListingStatus.ACTIVE)
    if not include_test:
        query = query.where(Listing.is_test.is_(False))
    if exclude_guild_id is not None:
        query = query.where(Listing.guild_id != exclude_guild_id)
    return list(await session.scalars(query))


async def listings_to_expire(session: AsyncSession, cutoff: datetime) -> list[Listing]:
    return list(
        await session.scalars(
            select(Listing).where(Listing.status == ListingStatus.ACTIVE, Listing.refreshed_at < cutoff)
        )
    )


async def listings_awaiting_review(session: AsyncSession, limit: int = 25) -> list[Listing]:
    return list(
        await session.scalars(
            select(Listing)
            .where(or_(Listing.status == ListingStatus.PENDING, Listing.pending_changes.is_not(None)))
            .order_by(Listing.updated_at)
            .limit(limit)
        )
    )


async def listings_with_status(session: AsyncSession, status: str, limit: int = 25) -> list[Listing]:
    return list(
        await session.scalars(
            select(Listing).where(Listing.status == status).order_by(Listing.updated_at.desc()).limit(limit)
        )
    )


async def listings_using_category(session: AsyncSession, category: str) -> list[Listing]:
    return list(
        await session.scalars(
            select(Listing).where(Listing.status != ListingStatus.REMOVED, _category_clause(category))
        )
    )


async def test_listings(session: AsyncSession) -> list[Listing]:
    return list(await session.scalars(select(Listing).where(Listing.is_test.is_(True))))


async def search_guilds(session: AsyncSession, text: str, limit: int = 10) -> list[Guild]:
    """Find known servers by exact ID or by (part of) their name."""
    text = text.strip()
    if not text:
        return []
    if text.isdigit():
        guild = await session.get(Guild, int(text))
        return [guild] if guild else []
    pattern = "%" + text.replace("\\", "").replace("%", "").replace("_", "") + "%"
    return list(
        await session.scalars(select(Guild).where(Guild.name.ilike(pattern)).order_by(Guild.name).limit(limit))
    )


# ---------------------------------------------------------------- contacts


async def get_contact_ids(session: AsyncSession, guild_id: int, *, enabled_only: bool = True) -> list[int]:
    query = select(ListingContact.user_id).where(ListingContact.guild_id == guild_id)
    if enabled_only:
        query = query.where(ListingContact.enabled.is_(True))
    return list(await session.scalars(query.order_by(ListingContact.id)))


async def replace_contacts(session: AsyncSession, guild_id: int, user_ids: Sequence[int]) -> None:
    await session.execute(delete(ListingContact).where(ListingContact.guild_id == guild_id))
    for user_id in dict.fromkeys(user_ids):  # de-duplicate, keep order
        session.add(ListingContact(guild_id=guild_id, user_id=user_id, enabled=True))
    await session.flush()


async def is_contact(session: AsyncSession, guild_id: int, user_id: int) -> bool:
    found = await session.scalar(
        select(ListingContact.id).where(
            ListingContact.guild_id == guild_id,
            ListingContact.user_id == user_id,
            ListingContact.enabled.is_(True),
        )
    )
    return found is not None


async def guild_ids_where_contact(session: AsyncSession, user_id: int) -> list[int]:
    return list(
        await session.scalars(
            select(ListingContact.guild_id).where(ListingContact.user_id == user_id, ListingContact.enabled.is_(True))
        )
    )


# ---------------------------------------------------------------- partnership requests


async def get_request(session: AsyncSession, request_id: int) -> PartnershipRequest | None:
    return await session.get(PartnershipRequest, request_id)


async def pending_request_between(session: AsyncSession, source_id: int, target_id: int) -> PartnershipRequest | None:
    return await session.scalar(
        select(PartnershipRequest).where(
            PartnershipRequest.source_guild_id == source_id,
            PartnershipRequest.target_guild_id == target_id,
            PartnershipRequest.status == RequestStatus.PENDING,
        )
    )


async def pending_target_ids(session: AsyncSession, source_guild_id: int) -> set[int]:
    """Servers this source already has an open request with (either direction)."""
    rows = await session.scalars(
        select(PartnershipRequest).where(
            PartnershipRequest.status == RequestStatus.PENDING,
            or_(
                PartnershipRequest.source_guild_id == source_guild_id,
                PartnershipRequest.target_guild_id == source_guild_id,
            ),
        )
    )
    ids = set()
    for row in rows:
        ids.add(row.target_guild_id if row.source_guild_id == source_guild_id else row.source_guild_id)
    return ids


async def accepted_partner_ids(session: AsyncSession, guild_id: int) -> set[int]:
    """Servers this one already has an accepted partnership with (either direction)."""
    rows = await session.scalars(
        select(PartnershipRequest).where(
            PartnershipRequest.status == RequestStatus.ACCEPTED,
            or_(
                PartnershipRequest.source_guild_id == guild_id,
                PartnershipRequest.target_guild_id == guild_id,
            ),
        )
    )
    return {row.target_guild_id if row.source_guild_id == guild_id else row.source_guild_id for row in rows}


async def count_pending_outgoing(session: AsyncSession, source_id: int) -> int:
    return (
        await session.scalar(
            select(func.count(PartnershipRequest.id)).where(
                PartnershipRequest.source_guild_id == source_id,
                PartnershipRequest.status == RequestStatus.PENDING,
            )
        )
        or 0
    )


async def pending_requests_for(
    session: AsyncSession, guild_ids: Iterable[int], *, incoming: bool, limit: int = 25
) -> list[PartnershipRequest]:
    ids = list(set(guild_ids))
    if not ids:
        return []
    column = PartnershipRequest.target_guild_id if incoming else PartnershipRequest.source_guild_id
    return list(
        await session.scalars(
            select(PartnershipRequest)
            .where(column.in_(ids), PartnershipRequest.status == RequestStatus.PENDING)
            .order_by(PartnershipRequest.created_at.desc())
            .limit(limit)
        )
    )


async def pending_requests_involving(session: AsyncSession, guild_id: int) -> list[PartnershipRequest]:
    return list(
        await session.scalars(
            select(PartnershipRequest).where(
                PartnershipRequest.status == RequestStatus.PENDING,
                or_(PartnershipRequest.source_guild_id == guild_id, PartnershipRequest.target_guild_id == guild_id),
            )
        )
    )


async def requests_older_than(session: AsyncSession, cutoff: datetime) -> list[PartnershipRequest]:
    return list(
        await session.scalars(
            select(PartnershipRequest).where(
                PartnershipRequest.status == RequestStatus.PENDING, PartnershipRequest.created_at < cutoff
            )
        )
    )


# ---------------------------------------------------------------- self-post crash recovery


async def get_self_post_session(
    session: AsyncSession, channel_id: int, user_id: int
) -> SelfPostSession | None:
    return await session.get(SelfPostSession, (channel_id, user_id))


async def get_self_post_session_for_listing(
    session: AsyncSession, listing_guild_id: int
) -> SelfPostSession | None:
    return await session.scalar(
        select(SelfPostSession).where(SelfPostSession.listing_guild_id == listing_guild_id)
    )


async def list_self_post_sessions(session: AsyncSession) -> list[SelfPostSession]:
    return list(await session.scalars(select(SelfPostSession).order_by(SelfPostSession.started_at)))


async def save_self_post_session(
    session: AsyncSession,
    *,
    channel_id: int,
    hub_guild_id: int,
    listing_guild_id: int,
    user_id: int,
    had_overwrite: bool,
    previous_allow: int,
    previous_deny: int,
    started_at: datetime,
    expires_at: datetime,
) -> SelfPostSession:
    row = await session.get(SelfPostSession, (channel_id, user_id))
    if row is None:
        row = SelfPostSession(channel_id=channel_id, user_id=user_id)
        session.add(row)
    row.hub_guild_id = hub_guild_id
    row.listing_guild_id = listing_guild_id
    row.had_overwrite = had_overwrite
    row.previous_allow = previous_allow
    row.previous_deny = previous_deny
    row.started_at = started_at
    row.expires_at = expires_at
    await session.flush()
    return row


async def delete_self_post_session(session: AsyncSession, channel_id: int, user_id: int) -> None:
    row = await session.get(SelfPostSession, (channel_id, user_id))
    if row is not None:
        await session.delete(row)
        await session.flush()


# ---------------------------------------------------------------- cooldowns


async def get_cooldown(session: AsyncSession, scope: str, subject: str) -> datetime | None:
    row = await session.get(Cooldown, (scope, subject))
    return row.expires_at if row else None


async def set_cooldown(session: AsyncSession, scope: str, subject: str, expires_at: datetime) -> None:
    row = await session.get(Cooldown, (scope, subject))
    if row is None:
        session.add(Cooldown(scope=scope, subject=subject, expires_at=expires_at))
    else:
        row.expires_at = expires_at
    await session.flush()


async def delete_expired_cooldowns(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(delete(Cooldown).where(Cooldown.expires_at < now))
    return result.rowcount or 0


# ---------------------------------------------------------------- moderation


async def get_ban(session: AsyncSession, target_type: str, target_id: int) -> ModerationBan | None:
    return await session.scalar(
        select(ModerationBan).where(ModerationBan.target_type == target_type, ModerationBan.target_id == target_id)
    )


async def add_ban(
    session: AsyncSession, target_type: str, target_id: int, reason: str | None, moderator_id: int | None
) -> ModerationBan:
    ban = await get_ban(session, target_type, target_id)
    if ban is None:
        ban = ModerationBan(target_type=target_type, target_id=target_id, reason=reason, moderator_id=moderator_id)
        session.add(ban)
    else:
        ban.reason = reason
        ban.moderator_id = moderator_id
    await session.flush()
    return ban


async def remove_ban(session: AsyncSession, target_type: str, target_id: int) -> bool:
    result = await session.execute(
        delete(ModerationBan).where(ModerationBan.target_type == target_type, ModerationBan.target_id == target_id)
    )
    return bool(result.rowcount)


async def list_bans(session: AsyncSession, target_type: str, limit: int = 25) -> list[ModerationBan]:
    return list(
        await session.scalars(
            select(ModerationBan)
            .where(ModerationBan.target_type == target_type)
            .order_by(ModerationBan.created_at.desc())
            .limit(limit)
        )
    )


# ---------------------------------------------------------------- network


async def get_network_settings(session: AsyncSession, guild_id: int) -> NetworkSettings | None:
    return await session.get(NetworkSettings, guild_id)


async def enabled_network_settings(session: AsyncSession) -> list[NetworkSettings]:
    return list(
        await session.scalars(
            select(NetworkSettings).where(NetworkSettings.enabled.is_(True), NetworkSettings.channel_id.is_not(None))
        )
    )


async def all_network_settings(session: AsyncSession) -> list[NetworkSettings]:
    return list(await session.scalars(select(NetworkSettings)))


async def last_shown_map(session: AsyncSession, destination_guild_id: int, since: datetime) -> dict[int, datetime]:
    rows = await session.execute(
        select(NetworkPost.source_guild_id, func.max(NetworkPost.posted_at))
        .where(NetworkPost.destination_guild_id == destination_guild_id, NetworkPost.posted_at >= since)
        .group_by(NetworkPost.source_guild_id)
    )
    # func.max() keeps the UTCDateTime column type, so values come back timezone-aware.
    return {source_id: posted_at for source_id, posted_at in rows if posted_at is not None}


async def delete_network_posts_before(session: AsyncSession, cutoff: datetime) -> int:
    result = await session.execute(delete(NetworkPost).where(NetworkPost.posted_at < cutoff))
    return result.rowcount or 0


# ---------------------------------------------------------------- panels


async def get_panel(session: AsyncSession, guild_id: int, panel_type: str) -> PanelState | None:
    return await session.scalar(
        select(PanelState).where(PanelState.guild_id == guild_id, PanelState.panel_type == panel_type)
    )


async def save_panel(
    session: AsyncSession, *, guild_id: int, channel_id: int, panel_type: str, message_id: int | None
) -> PanelState:
    panel = await get_panel(session, guild_id, panel_type)
    if panel is None:
        panel = PanelState(guild_id=guild_id, channel_id=channel_id, panel_type=panel_type, message_id=message_id)
        session.add(panel)
    else:
        panel.channel_id = channel_id
        panel.message_id = message_id
    await session.flush()
    return panel


# ---------------------------------------------------------------- test center


async def add_test_message(session: AsyncSession, *, channel_id: int, message_id: int, kind: str) -> None:
    session.add(TestMessage(channel_id=channel_id, message_id=message_id, kind=kind))
    await session.flush()


async def test_messages(session: AsyncSession) -> list[TestMessage]:
    return list(await session.scalars(select(TestMessage).order_by(TestMessage.id)))


async def delete_test_message_rows(session: AsyncSession, ids: Iterable[int]) -> None:
    ids = list(ids)
    if ids:
        await session.execute(delete(TestMessage).where(TestMessage.id.in_(ids)))


# ---------------------------------------------------------------- audit


async def add_audit(
    session: AsyncSession,
    action: str,
    *,
    actor_id: int | None = None,
    guild_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(AuditLog(action=action, actor_id=actor_id, guild_id=guild_id, details=details or {}))
    await session.flush()


async def has_audit_action(session: AsyncSession, action: str) -> bool:
    return await session.scalar(select(AuditLog.id).where(AuditLog.action == action).limit(1)) is not None


async def recent_audit(session: AsyncSession, guild_id: int, limit: int = 5) -> list[AuditLog]:
    return list(
        await session.scalars(
            select(AuditLog).where(AuditLog.guild_id == guild_id).order_by(AuditLog.timestamp.desc()).limit(limit)
        )
    )


# ---------------------------------------------------------------- runtime settings


async def runtime_overrides(session: AsyncSession) -> dict[str, Any]:
    rows = await session.scalars(select(RuntimeSetting))
    return {row.key: row.value for row in rows}


async def set_runtime_setting(session: AsyncSession, key: str, value: Any, updated_by: int | None = None) -> None:
    row = await session.get(RuntimeSetting, key)
    if row is None:
        session.add(RuntimeSetting(key=key, value=value, updated_by=updated_by))
    else:
        row.value = value
        row.updated_by = updated_by
    await session.flush()


async def delete_runtime_setting(session: AsyncSession, key: str) -> bool:
    result = await session.execute(delete(RuntimeSetting).where(RuntimeSetting.key == key))
    return bool(result.rowcount)


# ---------------------------------------------------------------- stats


async def stats(session: AsyncSession, since: datetime) -> dict[str, int]:
    async def count(query) -> int:
        return int(await session.scalar(query) or 0)

    return {
        "guilds": await count(select(func.count(Guild.guild_id))),
        "active_listings": await count(
            select(func.count(Listing.id)).where(Listing.status == ListingStatus.ACTIVE)
        ),
        "pending_listings": await count(
            select(func.count(Listing.id)).where(Listing.status == ListingStatus.PENDING)
        ),
        "suspended_listings": await count(
            select(func.count(Listing.id)).where(Listing.status == ListingStatus.SUSPENDED)
        ),
        "pending_requests": await count(
            select(func.count(PartnershipRequest.id)).where(PartnershipRequest.status == RequestStatus.PENDING)
        ),
        "accepted_partnerships": await count(
            select(func.count(PartnershipRequest.id)).where(PartnershipRequest.status == RequestStatus.ACCEPTED)
        ),
        "network_destinations": await count(
            select(func.count(NetworkSettings.guild_id)).where(NetworkSettings.enabled.is_(True))
        ),
        "network_posts_24h": await count(select(func.count(NetworkPost.id)).where(NetworkPost.posted_at >= since)),
        "banned_guilds": await count(select(func.count(ModerationBan.id)).where(ModerationBan.target_type == "guild")),
        "blocked_users": await count(select(func.count(ModerationBan.id)).where(ModerationBan.target_type == "user")),
    }
