"""Persistent cooldowns (stored in the database so restarts don't reset them)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.database import repository

SCOPE_REQUEST_USER = "request_user"
SCOPE_REQUEST_SERVER = "request_server"
SCOPE_DECLINED_PAIR = "declined_pair"
SCOPE_PARTNER_PAIR = "partner_pair"
SCOPE_LOOKING_POST = "looking_post"


async def remaining(session: AsyncSession, scope: str, subject: str | int, now: datetime) -> timedelta | None:
    expires_at = await repository.get_cooldown(session, scope, str(subject))
    if expires_at is None or expires_at <= now:
        return None
    return expires_at - now


async def start(session: AsyncSession, scope: str, subject: str | int, now: datetime, duration: timedelta) -> None:
    if duration.total_seconds() <= 0:
        return
    await repository.set_cooldown(session, scope, str(subject), now + duration)


def pair_key(source_guild_id: int, target_guild_id: int) -> str:
    return f"{source_guild_id}:{target_guild_id}"


def unordered_pair_key(first_guild_id: int, second_guild_id: int) -> str:
    a, b = sorted((first_guild_id, second_guild_id))
    return f"{a}:{b}"
