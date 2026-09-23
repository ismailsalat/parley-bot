"""Background work: network rotation and periodic maintenance.

Both loops are started after the bot is ready and cancelled on shutdown. Every
iteration reads its state from the database, so a restart simply continues.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import NetworkSettings
from bot.services import listings as listing_service
from bot.services import network, partnerships, permissions
from bot.utils.helpers import utcnow
from bot.utils.mentions import advertisement_kwargs

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

def network_ad_kwargs(bot: ParleyBot, listing) -> dict:
    """A network ad: the owner's normal message + optional footer + buttons (never an embed)."""
    from bot.config import templates
    from bot.views.partnership import listing_components
    from bot.views.welcome import add_bot_button

    kwargs = advertisement_kwargs(listing.advertisement_text)
    footer = templates.render(bot.runtime, "network_footer").strip()
    if footer and len(kwargs["content"]) + len(footer) + 1 <= 2000:
        kwargs["content"] += "\n" + footer
    view = listing_components(bot, listing) or discord.ui.View(timeout=None)
    add = add_bot_button(bot)
    if add is not None and len(view.children) < 5:
        view.add_item(add)
    if view.children:
        kwargs["view"] = view
    return kwargs


class BackgroundTasks:
    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot
        self._tasks: list[asyncio.Task] = []
        self.last_run: dict[str, object] = {}

    def start(self) -> None:
        if self._tasks:
            return  # on_ready fires again after reconnects
        self._tasks = [
            asyncio.create_task(self._loop("network", self.network_tick, self._network_delay), name="waypoint-network"),
            asyncio.create_task(
                self._loop("maintenance", self.maintenance_tick, self._maintenance_delay), name="waypoint-maintenance"
            ),
        ]
        log.info("Background tasks started")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._tasks:
            log.info("Background tasks stopped")
        self._tasks = []

    def status(self) -> dict[str, object]:
        return {
            "running": bool(self._tasks) and all(not t.done() for t in self._tasks),
            "last_network_tick": self.last_run.get("network"),
            "last_maintenance": self.last_run.get("maintenance"),
        }

    def _network_delay(self) -> float:
        return float(self.bot.runtime.network.tick_seconds)

    def _maintenance_delay(self) -> float:
        return float(self.bot.runtime.system.maintenance_interval_minutes * 60)

    async def _loop(self, name: str, tick, delay) -> None:
        while True:
            try:
                await tick()
                self.last_run[name] = utcnow()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad iteration must not kill the loop; the error is logged in full.
                log.exception("Background task '%s' failed; retrying next cycle", name)
            await asyncio.sleep(delay())

    # ------------------------------------------------------------ network rotation

    async def network_tick(self) -> None:
        config = self.bot.runtime
        if config.hub.mode != "live" or not config.network.enabled or not self.bot.is_ready():
            return  # TEST and OFF mode never distribute real network ads
        async with self.bot.db.session() as session:
            due = await network.due_destinations(session, config, utcnow())
        for index, destination in enumerate(due):
            if index:
                await asyncio.sleep(config.network.send_spacing_seconds)  # stay well under rate limits
            await self._post_to(destination)

    async def _disable(self, guild_id: int, reason: str) -> None:
        async with self.bot.db.session() as session:
            await network.disable(session, guild_id=guild_id, reason=reason)

    async def _post_to(self, destination: NetworkSettings) -> None:
        bot = self.bot
        config = bot.runtime
        guild = bot.get_guild(destination.guild_id)
        channel = bot.get_channel(destination.channel_id) if destination.channel_id else None
        if guild is None:
            await self._disable(destination.guild_id, "Parley is no longer in this server")
            return
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            await self._disable(destination.guild_id, "network channel no longer exists")
            return
        missing = permissions.missing_channel_permissions(channel, guild.me)
        if missing:
            await self._disable(destination.guild_id, f"missing permissions in #{channel.name}: {', '.join(missing)}")
            return

        now = utcnow()
        async with bot.db.session() as session:
            candidates = await network.eligible_sources(
                session, config, destination_guild_id=guild.id, categories=destination.categories or []
            )
            last_shown = await repository.last_shown_map(
                session, guild.id, now - timedelta(hours=max(config.network.repeat_window_hours, 1) * 4)
            )
            listing = network.pick_candidate(
                candidates,
                last_shown=last_shown,
                now=now,
                repeat_window=timedelta(hours=config.network.repeat_window_hours),
                strategy=config.network.rotation_strategy,
            )
            if listing is None:
                # Nothing fresh to show: wait a full interval rather than repeating ads.
                await network.mark_attempt(session, guild_id=guild.id, now=now)
                return

        kwargs = network_ad_kwargs(bot, listing)
        try:
            message = await channel.send(**kwargs)
        except discord.Forbidden:
            await self._disable(guild.id, f"not allowed to post in #{channel.name}")
            return
        except discord.HTTPException as exc:
            log.warning("network.send_failed destination=%s source=%s: %s", guild.id, listing.guild_id, exc)
            async with bot.db.session() as session:
                await network.mark_attempt(session, guild_id=guild.id, now=now)
            return

        async with bot.db.session() as session:
            await network.record_post(
                session,
                source_guild_id=listing.guild_id,
                destination_guild_id=guild.id,
                channel_id=channel.id,
                message_id=message.id,
                now=now,
            )

    # ------------------------------------------------------------ maintenance

    async def maintenance_tick(self) -> None:
        bot = self.bot
        await bot.reload_runtime_config()
        now = utcnow()
        config = bot.runtime

        async with bot.db.session() as session:
            expired_listings = await listing_service.expire_listings(session, config, now)
            expired_info = [
                (listing.guild_id, listing.channel_id, listing.message_id, await repository.get_contact_ids(session, listing.guild_id))
                for listing in expired_listings
            ]
            for listing in expired_listings:
                listing.channel_id = None
                listing.message_id = None
            await partnerships.expire_requests(session, config, now)
            await repository.delete_expired_cooldowns(session, now)
            await repository.delete_network_posts_before(session, now - timedelta(days=config.network.post_retention_days))

        from bot.views.base import deliver_dms
        from bot.views.management import manage_button
        from bot.views.welcome import persistent_view

        for guild_id, channel_id, message_id, contacts in expired_info:
            await bot.panels.delete_listing_message(channel_id, message_id)
            guild = bot.get_guild(guild_id)
            name = guild.name if guild else str(guild_id)
            await deliver_dms(
                    bot,
                    contacts,
                    actor_id=None,
                    content=(
                        f"⏰ The Parley listing for **{name}** expired after "
                        f"{config.listings.expiration_days} days. Press **Manage** → **Relist** to bring it back."
                    ),
                    view=persistent_view(manage_button(bot, guild_id, name)),
                )

        if bot.is_ready():
            await bot.sync_listed_guilds()
            await bot.panels.restore_panels()
