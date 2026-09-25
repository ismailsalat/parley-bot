"""Background work: network rotation and periodic maintenance.

Both loops are started after the bot is ready and cancelled on shutdown. Every
iteration reads its state from the database, so a restart simply continues.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import NetworkSettings
from bot.services import listings as listing_service
from bot.services import network, partnerships, permissions
from bot.services.errors import ParleyError
from bot.utils.helpers import utcnow
from bot.utils.mentions import advertisement_kwargs

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

EVENT_LOOP_CHECK_SECONDS = 1.0
EVENT_LOOP_WARN_SECONDS = 1.25
EVENT_LOOP_CRITICAL_SECONDS = 2.50
LISTING_CONTROL_REFRESH_SECONDS = 60 * 60
ENTRY_PANEL_REPOST_SECONDS = 30 * 60

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
            asyncio.create_task(self._event_loop_watchdog(), name="parley-event-loop-watchdog"),
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
            "last_listing_control_refresh": self.last_run.get("listing_controls"),
            "last_event_loop_lag": self.last_run.get("event_loop_lag"),
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

    async def _event_loop_watchdog(self) -> None:
        """Detect the one failure a normal interaction watchdog cannot fix: loop stalls.

        If Python itself stops scheduling coroutines for several seconds, both the
        button callback and its acknowledgement timer are delayed. Recording that
        lag makes a future Discord timeout immediately diagnosable instead of
        looking like a random broken button.
        """
        expected = time.monotonic() + EVENT_LOOP_CHECK_SECONDS
        latency_check = 0
        while True:
            try:
                await asyncio.sleep(EVENT_LOOP_CHECK_SECONDS)
                now = time.monotonic()
                lag = max(0.0, now - expected)
                self.last_run["event_loop_lag"] = round(lag, 3)
                if lag >= EVENT_LOOP_CRITICAL_SECONDS:
                    log.error("event_loop.lag critical=%.3fs", lag)
                elif lag >= EVENT_LOOP_WARN_SECONDS:
                    log.warning("event_loop.lag warning=%.3fs", lag)
                expected = now + EVENT_LOOP_CHECK_SECONDS

                # No network I/O: continuously verify that stopping a transient
                # Discord view has not removed one of Parley's persistent routes.
                if latency_check % 10 == 0:
                    self.bot._ensure_interaction_routing(reason="routing_watchdog")

                latency_check += 1
                if latency_check >= 30:
                    latency_check = 0
                    latency = float(getattr(self.bot, "latency", 0.0) or 0.0)
                    if latency >= 2.0:
                        log.warning("discord.gateway_latency high=%.3fs", latency)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Monitoring must never be able to kill the bot.
                log.exception("Event-loop watchdog failed; continuing")
                expected = time.monotonic() + EVENT_LOOP_CHECK_SECONDS

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
            await self._auto_pair(destination)

    async def _disable(self, guild_id: int, reason: str) -> None:
        async with self.bot.db.session() as session:
            await network.disable(session, guild_id=guild_id, reason=reason)

    async def _auto_pair(self, destination: NetworkSettings) -> None:
        """One Auto Partner attempt. Random network ads are intentionally gone."""
        bot = self.bot
        config = bot.runtime
        source_guild = bot.get_guild(destination.guild_id)
        source_channel = bot.get_channel(destination.channel_id) if destination.channel_id else None
        now = utcnow()
        if source_guild is None:
            await self._disable(destination.guild_id, "Parley is no longer in this server")
            return
        if not isinstance(source_channel, discord.TextChannel) or source_channel.guild.id != source_guild.id:
            await self._disable(destination.guild_id, "network channel no longer exists")
            return
        missing = permissions.missing_channel_permissions(source_channel, source_guild.me)
        if missing:
            await self._disable(destination.guild_id, f"missing permissions in #{source_channel.name}: {', '.join(missing)}")
            return
        if not destination.configured_by:
            async with bot.db.session() as session:
                await network.mark_attempt(session, guild_id=destination.guild_id, now=now)
            return
        async with bot.db.session() as session:
            candidates = await network.eligible_sources(session, config, destination_guild_id=destination.guild_id, categories=destination.categories or [])
        # Prefer fresh partners: shuffle so the same pair isn't retried first every tick.
        random_candidates = list(candidates)
        random.shuffle(random_candidates)
        for candidate in random_candidates:
            target_guild = bot.get_guild(candidate.guild_id)
            if target_guild is None:
                continue
            async with bot.db.session() as session:
                target_net = await repository.get_network_settings(session, candidate.guild_id)
                if target_net is None or not target_net.enabled or not target_net.channel_id:
                    continue
                if await repository.pending_request_between(session, destination.guild_id, candidate.guild_id):
                    continue
                if await repository.pending_request_between(session, candidate.guild_id, destination.guild_id):
                    continue
                try:
                    context = await partnerships.create_request(
                        session, config, source_guild_id=destination.guild_id, target_guild_id=candidate.guild_id,
                        requester_id=destination.configured_by, source_member_count=source_guild.member_count or 0,
                        message=None, now=now,
                    )
                    guilds = await repository.get_guilds(session, [destination.guild_id, candidate.guild_id])
                    contacts = await repository.get_contact_ids(session, candidate.guild_id)
                except ParleyError:
                    continue
            from bot.views.partnership import deliver_request_notification, exchange_partner_ads
            if target_net.auto_partner and target_net.configured_by:
                manager = await permissions.is_manager(bot, candidate.guild_id, target_net.configured_by)
                async with bot.db.session() as session:
                    contact = await repository.is_contact(session, candidate.guild_id, target_net.configured_by)
                if manager or contact:
                    async with bot.db.session() as session:
                        await partnerships.respond(
                            session, config, request_id=context.request.id, responder_id=target_net.configured_by,
                            responder_is_manager=manager, accept=True, now=now,
                        )
                    await exchange_partner_ads(bot, destination.guild_id, candidate.guild_id)
                    await bot.log_event(f"🤖 Auto Partner matched `{destination.guild_id}` ↔ `{candidate.guild_id}`.")
                else:
                    await deliver_request_notification(bot, context.request, guilds, context.source, context.target, contacts, actor_id=destination.configured_by)
            else:
                await deliver_request_notification(bot, context.request, guilds, context.source, context.target, contacts, actor_id=destination.configured_by)
            async with bot.db.session() as session:
                await network.mark_attempt(session, guild_id=destination.guild_id, now=now)
            return
        async with bot.db.session() as session:
            await network.mark_attempt(session, guild_id=destination.guild_id, now=now)

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
            # Re-render permanent controls every maintenance pass. Every six
            # hours, replace the public panel messages entirely so a 24/7 bot
            # never depends on an indefinitely old Discord component message.
            last_panel_repost = self.last_run.get("entry_panels_reposted_monotonic")
            monotonic_now = time.monotonic()
            should_repost = (
                not isinstance(last_panel_repost, float)
                or monotonic_now - last_panel_repost >= ENTRY_PANEL_REPOST_SECONDS
            )
            await bot.panels.refresh_entry_panels(repost=should_repost)
            if should_repost:
                await bot.panels.cleanup_orphan_entry_panels()
                self.last_run["entry_panels_reposted_monotonic"] = monotonic_now
                self.last_run["entry_panels_reposted"] = utcnow()

            # Refresh every live listing/partner control strip at least hourly.
            # The panel service spaces Discord writes to stay below rate limits.
            last = self.last_run.get("listing_controls_monotonic")
            if not isinstance(last, float) or monotonic_now - last >= LISTING_CONTROL_REFRESH_SECONDS:
                refreshed, failed = await bot.panels.refresh_active_listing_views()
                self.last_run["listing_controls_monotonic"] = monotonic_now
                self.last_run["listing_controls"] = utcnow()
                log.info(
                    "Periodic listing control refresh: %d refreshed%s",
                    refreshed,
                    f" ({failed} failed)" if failed else "",
                )
