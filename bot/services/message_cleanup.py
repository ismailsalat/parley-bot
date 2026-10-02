"""Database-backed deletion queue used when Discord refuses message deletes.

The queue is committed *before* attempting each deletion. A later maintenance
pass can process any orphaned public ad even if its listing has been relisted.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import PendingMessageDeletion


async def enqueue(session: AsyncSession, channel_id: int | None, message_id: int | None,
                  *, guild_id: int | None = None, reason: str = "listing_replaced") -> None:
    if not channel_id or not message_id:
        return
    row = await session.get(PendingMessageDeletion, (channel_id, message_id))
    if row is None:
        session.add(PendingMessageDeletion(channel_id=channel_id, message_id=message_id,
                                           guild_id=guild_id, reason=reason))
        await session.flush()


async def remove(session: AsyncSession, channel_id: int | None, message_id: int | None) -> None:
    if channel_id and message_id:
        row = await session.get(PendingMessageDeletion, (channel_id, message_id))
        if row is not None:
            await session.delete(row)


async def pending(session: AsyncSession, limit: int = 30) -> list[PendingMessageDeletion]:
    return list(await session.scalars(
        select(PendingMessageDeletion).order_by(PendingMessageDeletion.attempts,
                                                PendingMessageDeletion.created_at,
                                                PendingMessageDeletion.channel_id,
                                                PendingMessageDeletion.message_id).limit(limit)
    ))


async def mark_failed(session: AsyncSession, channel_id: int, message_id: int) -> None:
    row = await session.get(PendingMessageDeletion, (channel_id, message_id))
    if row is not None:
        row.attempts = (row.attempts or 0) + 1
