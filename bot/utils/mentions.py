"""Mention safety.

Advertisements may *show* @everyone, @here, role or user mentions, but Waypoint
must never actually ping anyone with them. Every message Waypoint sends uses
``AllowedMentions.none()``; the client-wide default is also set to none as a
second safety net (see ``bot.core``).
"""

from __future__ import annotations

import discord


def safe_allowed_mentions() -> discord.AllowedMentions:
    """No @everyone/@here, no roles, no users, no reply pings."""
    return discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=False)


def advertisement_kwargs(text: str) -> dict:
    """Keyword arguments for sending an advertisement exactly as written, without pings.

    The text is sent as normal message content (never an embed) so Markdown,
    headings, custom emoji syntax, links and spacing render like a user's message.
    """
    return {"content": text, "allowed_mentions": safe_allowed_mentions()}


def user_mention(user_id: int) -> str:
    return f"<@{user_id}>"
