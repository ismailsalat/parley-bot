"""The Parley bot: wiring between Discord, the database and the services."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import replace

import discord
from discord import app_commands
from discord.ext import commands

from bot.commands import COMMAND_MODULES
from bot.config import templates
from bot.config.runtime import HubConfig, RuntimeConfig, apply_overrides, default_config
from bot.config.settings import Settings
from bot.database import repository
from bot.database.models import ListingStatus
from bot.database.session import Database
from bot.services import configuration
from bot.services.moderation import RateLimiter
from bot.oauth_server import OAuthServer
from bot.services.panels import PanelService
from bot.tasks import BackgroundTasks
from bot.utils.mentions import safe_allowed_mentions

log = logging.getLogger(__name__)

JOIN_CHANNEL_NAMES = ("general", "start-here", "welcome", "bot-commands", "bots", "commands", "chat")
VIEW_MODULES = ("bot.views.network","bot.views.partner_posts",  "bot.views.admin.settings", "bot.views.admin.setup", "bot.views.admin.test_center")


def build_intents(message_content: bool = True) -> discord.Intents:
    """Only what Parley needs. ``members`` and ``message_content`` are privileged.

    * guilds          – servers, channels, roles
    * members         – (privileged) knowing which servers a user can manage
    * guild_messages  – notice new top-level posts in #find-partners and deleted panels
    * dm_messages     – a DM opens the control panel
    Message *content* is needed for the controlled "Paste My Own Ad" flow so
    Parley can moderate the one message the owner posts in #server-directory.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.message_content = message_content
    return intents


def persistent_items() -> list[type[discord.ui.DynamicItem]]:
    """Every button that must keep working on old messages after a restart."""
    for module in VIEW_MODULES:  # importing registers their action handlers
        importlib.import_module(module)
    from bot.views.listings import ReviewButton
    from bot.views.management import ManageButton, ManagementButton
    from bot.views.partnership import RequestPartnershipButton, RequestResponseButton, ViewAdButton
    from bot.views.welcome import ActionButton

    return [
        ActionButton,
        RequestPartnershipButton,
        ViewAdButton,
        RequestResponseButton,
        ReviewButton,
        ManageButton,
        ManagementButton,
    ]


class ParleyTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        from bot.views.base import guard

        return await guard(interaction)

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        from bot.views.base import handle_error

        await handle_error(interaction, error)


class ParleyBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.runtime: RuntimeConfig = default_config()
        self.runtime_notes: list[str] = []
        self.hub_env_fields: tuple[str, ...] = ()
        self.staff_cache: dict[int, tuple[bool, float]] = {}
        self.blocked_user_ids: set[int] = set()
        self.staff_guild_id: int | None = None
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(settings.message_content_intent),
            help_command=None,
            tree_cls=ParleyTree,
            allowed_mentions=safe_allowed_mentions(),  # second safety net: nothing Parley sends pings
            status=discord.Status.online,
        )
        moderation = self.runtime.moderation
        self.click_limiter = RateLimiter(moderation.button_rate_limit, moderation.button_rate_window_seconds)
        self.panels = PanelService(self)
        self.oauth = OAuthServer(self)  # "Verify My Servers"; a no-op unless configured
        self.background = BackgroundTasks(self)
        self._dm_panel_sent: dict[int, float] = {}
        self._interaction_ack_watchdogs: dict[int, asyncio.Task] = {}
        self._started = False

    @property
    def hub(self) -> HubConfig:
        return self.runtime.hub

    # ------------------------------------------------------------ startup

    async def setup_hook(self) -> None:
        await self.reload_runtime_config()

        items = persistent_items()
        self.add_dynamic_items(*items)
        log.info("Registered %d persistent button types", len(items))

        for module in COMMAND_MODULES:
            await self.load_extension(module)
        if self.hub.main_guild_id:
            await self.register_staff_commands(self.hub.main_guild_id, sync=False)

        await self.oauth.start()

        if self.settings.sync_commands:
            await self._sync_commands()

    async def _sync_commands(self) -> None:
        try:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands", len(synced))
        except discord.HTTPException as exc:
            log.error("Could not sync global slash commands: %s", exc)
        if self.staff_guild_id is not None:
            await self._sync_guild(self.staff_guild_id)

    async def _sync_guild(self, guild_id: int) -> None:
        try:
            synced = await self.tree.sync(guild=discord.Object(id=guild_id))
            log.info("Synced %d staff commands to the main server", len(synced))
        except discord.Forbidden:
            log.error(
                "Could not register staff commands in the main server (%s). Re-invite Parley with the "
                "applications.commands scope.", guild_id,
            )
        except discord.HTTPException as exc:
            log.error("Could not sync staff commands: %s", exc)

    async def register_staff_commands(self, guild_id: int, *, sync: bool = True) -> None:
        """Put /settings and /admin in the main server (moves them if the main server changed)."""
        cog = self.get_cog("StaffCommands")
        if cog is None:
            return
        old = self.staff_guild_id
        staff_commands = cog.get_app_commands()
        if old is not None and old != guild_id:
            for command in staff_commands:
                self.tree.remove_command(command.name, guild=discord.Object(id=old))
        for command in staff_commands:
            self.tree.add_command(command, guild=discord.Object(id=guild_id), override=True)
        self.staff_guild_id = guild_id
        if sync and self.settings.sync_commands and self.is_ready():
            if old is not None and old != guild_id:
                await self._sync_guild(old)
            await self._sync_guild(guild_id)

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Connected to Discord as %s (id %s) in %d servers", self.user, self.user.id, len(self.guilds))
        if self._started:
            return  # on_ready also fires after a reconnect; nothing to redo
        self._started = True

        await self._apply_presence()
        self.background.start()
        await self._rename_legacy_hub_channels()
        # Close any one-message posting permission that survived a crash/redeploy
        # before accepting new interactions.
        from bot.views import self_post

        recovered = await self_post.restore_abandoned_sessions(self)
        if recovered:
            log.info("Recovered %d abandoned direct-post window(s)", recovered)
        # #server-directory is always read-only except for Parley's controlled
        # three-minute, one-message posting window.
        await self.panels.ensure_directory_locked()
        try:
            await self.panels.restore_panels(force_edit=True)
        except discord.HTTPException as exc:
            log.error("Could not restore panels: %s", exc)
        try:
            refreshed, failed = await self.panels.refresh_active_listing_views()
            log.info("Refreshed %d live listing view(s)%s", refreshed, f" ({failed} failed)" if failed else "")
        except Exception:
            # Existing listings must never prevent the bot from becoming ready.
            log.exception("Could not refresh live listing buttons during startup")
        await self._startup_self_check()
        log.info("%s is ready (mode: %s)", self.runtime.bot.name, self.hub.mode.upper())

    async def _rename_legacy_hub_channels(self) -> None:
        """Rename only untouched legacy default hub channel names to the clearer v1 names.

        Custom names are never changed. If Parley lacks Manage Channels, the old names
        keep working and no setup breaks.
        """
        if not self.hub.configured:
            return
        guild = self.get_guild(self.hub.main_guild_id)
        if guild is None or guild.me is None or not guild.me.guild_permissions.manage_channels:
            return

        from bot.services.setup import SLOTS

        existing_names = {channel.name for channel in guild.text_channels}
        for slot in SLOTS:
            channel_id = getattr(self.hub, slot.key, 0)
            channel = guild.get_channel(channel_id) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                continue
            if channel.name not in slot.legacy_names or slot.name in existing_names:
                continue
            old_name = channel.name
            try:
                await channel.edit(name=slot.name, reason="Parley clearer default channel names")
            except discord.HTTPException as exc:
                log.info("Could not rename legacy channel #%s to #%s: %s", old_name, slot.name, exc)
                continue
            existing_names.discard(old_name)
            existing_names.add(slot.name)
            log.info("Renamed legacy hub channel #%s to #%s", old_name, slot.name)

    async def _startup_self_check(self) -> None:
        """Log what the Health Check would flag. Optional things only warn; nothing here crashes."""
        from bot.services import health

        if not self.hub.configured:
            log.warning("Parley isn't set up yet. In Discord, run /setup in your main server.")
            await self._send_owner_onboarding()
            return
        try:
            checks = await health.run(self)
        except Exception:  # noqa: BLE001 - a self-check must never stop startup
            log.exception("Startup self-check failed")
            return
        for check in checks:
            if check.status != health.OK:
                log.warning("Health: %s — %s", check.name, check.detail)

    async def _send_owner_onboarding(self) -> None:
        """First run: DM the application owner how to set things up (only once, ever)."""
        from bot.views.welcome import setup_invite_url

        async with self.db.session() as session:
            if await repository.has_audit_action(session, "onboarding.sent"):
                return
        try:
            app = await self.application_info()
        except discord.HTTPException as exc:
            log.info("Could not read application info for onboarding: %s", exc)
            return
        owner = app.team.owner if app.team and app.team.owner else app.owner
        url = setup_invite_url(self)
        view = discord.ui.View()
        if url:
            view.add_item(discord.ui.Button(label="Add to my main server", emoji="➕", url=url))
        try:
            await owner.send(
                f"👋 **{self.runtime.bot.name} is running!**\n"
                "1. Add me to the server that should be your Parley hub.\n"
                "2. In that server, type **/setup** and press **Automatic Setup**.\n"
                "Everything else is done with buttons.",
                view=view,
            )
        except (discord.HTTPException, AttributeError) as exc:
            log.info("Could not DM the owner the setup instructions: %s", exc)
            return
        async with self.db.session() as session:
            await repository.add_audit(session, "onboarding.sent", actor_id=owner.id)

    async def on_resumed(self) -> None:
        log.info("Discord session resumed")

    async def on_disconnect(self) -> None:
        log.info("Disconnected from Discord; discord.py will reconnect automatically")

    # ------------------------------------------------------------ runtime configuration

    async def reload_runtime_config(self) -> None:
        """Apply settings stored in the database (changed from Discord). No restart needed."""
        async with self.db.session() as session:
            overrides = await repository.runtime_overrides(session)
            user_bans = await repository.list_bans(session, "user", limit=100_000)
        config, notes = apply_overrides(default_config(), overrides)
        hub, env_fields = configuration.merge_env_hub(config.hub, self.settings)
        if hub != config.hub:
            config = replace(config, hub=hub)
        for note in sorted(set(notes) - set(self.runtime_notes)):
            log.warning("Stored setting ignored or adjusted: %s", note)
        if env_fields and env_fields != self.hub_env_fields:
            log.info("Using main-server values from environment variables: %s", ", ".join(env_fields))
        self.runtime_notes = list(notes)
        self.hub_env_fields = env_fields

        changed = config != self.runtime
        previous_mode = self.runtime.hub.mode
        self.runtime = config
        self.blocked_user_ids = set(config.moderation.blocked_user_ids) | {
            ban.target_id for ban in user_bans
        }
        self.staff_cache.clear()
        self.click_limiter.configure(config.moderation.button_rate_limit, config.moderation.button_rate_window_seconds)
        if hub.main_guild_id and self.staff_guild_id is not None and hub.main_guild_id != self.staff_guild_id:
            await self.register_staff_commands(hub.main_guild_id)
        if changed and self.is_ready():
            log.info("Settings reloaded (%d stored values)", len(overrides))
            if previous_mode != hub.mode:
                log.info("Mode is now %s", hub.mode.upper())
            await self._apply_presence()

    async def settings_changed(self, *, refresh_panels: bool = False) -> None:
        """Called by the Settings UI after a change: apply it immediately."""
        await self.reload_runtime_config()
        if self.hub.main_guild_id and self.staff_guild_id != self.hub.main_guild_id:
            await self.register_staff_commands(self.hub.main_guild_id)
        if refresh_panels and self.is_ready():
            await self.panels.restore_panels(force_edit=True)

    async def _apply_presence(self) -> None:
        bot = self.runtime.bot
        activity = discord.CustomActivity(name=bot.activity_text) if bot.activity_text else None
        status = discord.Status.idle if self.hub.mode != "live" else discord.Status(bot.status)
        await self.change_presence(status=status, activity=activity)

    # ------------------------------------------------------------ staff log channel

    def log_channel(self) -> discord.TextChannel | None:
        return self.panels.main_channel(self.hub.log_channel_id)

    async def log_event(self, text: str) -> None:
        """Post a short line to the staff log channel (if configured). Never pings."""
        channel = self.log_channel()
        if channel is None:
            return
        try:
            await channel.send(text[:2000], allowed_mentions=safe_allowed_mentions())
        except discord.HTTPException as exc:
            log.warning("Could not write to the log channel: %s", exc)

    # ------------------------------------------------------------ events

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            await self._dm_control_panel(message)
            return

        # Server Directory and Find a Partner are controlled feeds. A manager gets
        # a short one-message window; everything else is removed.
        protected = {
            cid
            for cid in (self.hub.listings_channel_id, self.hub.looking_channel_id)
            if cid
        }
        if message.channel.id in protected:
            from bot.views import self_post

            if self_post.is_active_submission(message.channel.id, message.author.id, message.id):
                return
            try:
                await message.delete()
            except discord.HTTPException as exc:
                log.warning("Could not remove unauthorized feed message %s: %s", message.id, exc)
            return

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Owner-authored ads cannot be silently changed after moderation.

        Discord lets authors edit their own message even when Send Messages is later
        denied. Parley therefore removes a direct edit and tells the owner to use
        the controlled Edit Ad flow. One successful edit is available per Relist cycle.
        """
        if after.author.bot or after.guild is None:
            return
        if before.content == after.content:
            return

        if self.hub.looking_channel_id and after.channel.id == self.hub.looking_channel_id:
            from bot.views import partner_posts

            await partner_posts.handle_direct_edit(self, before, after)
            return

        if not self.hub.listings_channel_id or after.channel.id != self.hub.listings_channel_id:
            return

        from bot.services import listings as listing_service
        from bot.views.welcome import action_button, persistent_view

        async with self.db.session() as session:
            listing = await repository.get_listing_by_message(session, after.id)
            if listing is not None:
                # Clear the pointer before deleting so the raw-delete event cannot
                # process the same owner ad a second time.
                listing.message_id = None
        if listing is None:
            return

        try:
            await after.delete()
        except discord.HTTPException as exc:
            log.warning("Could not remove directly edited listing message %s: %s", after.id, exc)
            return

        available = listing_service.ad_edit_available(listing)
        await self.panels.self_post_message_removed(listing.guild_id)
        text = (
            "Your ad was edited directly, so Parley removed it so changes can't bypass moderation.\n\n"
            "You still have **one ad edit** this Relist. Open **My Listing → Edit Ad** to post the replacement."
            if available
            else
            "Your ad was edited again after this Relist's edit was already used, so Parley removed it.\n\n"
            "Relist when the cooldown ends to unlock another ad edit."
        )
        try:
            await after.author.send(text, view=persistent_view(action_button(self, "servers")))
        except discord.HTTPException:
            pass

        log.info("listing.direct_edit_removed guild_id=%s message_id=%s", listing.guild_id, after.id)

    async def _dm_control_panel(self, message: discord.Message) -> None:
        from bot.services import moderation, permissions
        from bot.views.welcome import personalize_control_panel

        now = time.monotonic()
        last = self._dm_panel_sent.get(message.author.id)
        if last is not None and now - last < self.runtime.moderation.dm_panel_cooldown_seconds:
            return
        self._dm_panel_sent[message.author.id] = now
        if len(self._dm_panel_sent) > 10_000:
            self._dm_panel_sent.clear()
        async with self.db.session() as session:
            if await moderation.is_user_blocked(session, self.runtime, message.author.id):
                return
        staff = await permissions.is_staff_cached(self, message.author.id)
        try:
            if self.hub.mode != "live" and not staff:
                key = "maintenance" if self.hub.mode == "off" else "test_mode"
                await message.channel.send(templates.render(self.runtime, key))
                return
            content, view = await personalize_control_panel(self, message.author.id, staff=staff)
            await message.channel.send(content, view=view)
        except discord.HTTPException as exc:
            log.warning("Could not send the DM control panel to %s: %s", message.author.id, exc)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None or payload.guild_id != self.hub.main_guild_id:
            return
        await self.panels.handle_message_deleted(payload.channel_id, payload.message_id)

        # Keep owner-authored public-post state valid after manual deletion.
        if payload.channel_id == self.hub.listings_channel_id:
            async with self.db.session() as session:
                listing = await repository.get_listing_by_message(session, payload.message_id)
            if listing is not None:
                await self.panels.self_post_message_removed(listing.guild_id)
                log.info("listing.self_post_deleted guild_id=%s message_id=%s", listing.guild_id, payload.message_id)
        elif payload.channel_id == self.hub.looking_channel_id:
            from bot.views import partner_posts

            await partner_posts.handle_raw_delete(self, payload.channel_id, payload.message_id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.guild.id != self.hub.main_guild_id:
            return
        from bot.services.setup import SLOTS

        for slot in SLOTS:
            if getattr(self.hub, slot.key) == channel.id:
                log.warning("The %s channel (#%s) was deleted. Choose a new one in /settings → Channels.", slot.label, channel.name)
                await self.log_event(
                    f"⚠️ The **{slot.label}** channel `#{channel.name}` was deleted. "
                    "Use **/settings → Channels** to choose a new one."
                )

    def join_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Choose a sensible public channel for the one-time onboarding message."""
        me = guild.me
        if me is None:
            return None

        def usable(channel: discord.TextChannel | None) -> bool:
            if channel is None:
                return False
            perms = channel.permissions_for(me)
            public = channel.permissions_for(guild.default_role)
            return public.view_channel and perms.view_channel and perms.send_messages and perms.read_message_history

        # Prefer Discord's system channel, then familiar public channel names.
        if usable(guild.system_channel):
            return guild.system_channel
        for name in JOIN_CHANNEL_NAMES:
            for candidate in guild.text_channels:
                if candidate.name == name and usable(candidate):
                    return candidate

        # If there is no familiar channel, use the first channel everyone can see where
        # Parley can speak. This avoids silently doing nothing without posting in a
        # private staff channel.
        everyone = guild.default_role
        for candidate in guild.text_channels:
            if usable(candidate) and candidate.permissions_for(everyone).view_channel:
                return candidate
        return None

    async def _dm_guild_onboarding(self, guild: discord.Guild) -> None:
        """DM the server owner a one-time, network-first setup guide.

        This is separate from the public join card so the manager can always find
        the setup steps later. The audit row prevents reconnects/restarts from
        spamming the owner.
        """
        async with self.db.session() as session:
            if await repository.has_audit_action(session, "guild_onboarding.sent", guild_id=guild.id):
                return

        owner = guild.owner
        if owner is None:
            try:
                owner = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, AttributeError):
                owner = None
        if owner is None:
            log.info("Could not resolve owner for onboarding DM in guild %s", guild.id)
            return

        from bot.views.welcome import action_button, persistent_view

        embed = discord.Embed(
            title=f"Parley is ready in {guild.name}",
            description=(
                "**Start with Find Partners.** Parley checks this server and guides you through anything missing: "
                "the **server ad** first, then the **partner-ad channel**.\n\n"
                "After setup, choose a partner and send a request. When both servers accept, Parley exchanges their ads "
                "in the channels they chose.\n\n"
                "Your ad, cooldowns, partnership settings, and Network settings stay **in sync** whether you manage them "
                "here or from the main Parley server."
            ),
            color=self.runtime.bot.color_primary,
        )
        embed.set_footer(text="Only server managers can change these settings.")
        view = persistent_view(
            action_button(self, "find", style=discord.ButtonStyle.success, row=0),
            action_button(self, "post", style=discord.ButtonStyle.primary, row=0),
            action_button(self, "network_help", style=discord.ButtonStyle.secondary, row=1),
        )
        try:
            await owner.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            log.info("Could not DM guild owner onboarding for %s: %s", guild.id, exc)
            return

        async with self.db.session() as session:
            await repository.add_audit(
                session, "guild_onboarding.sent", actor_id=owner.id, guild_id=guild.id
            )

    async def maybe_dm_manager_onboarding(self, guild: discord.Guild, user: discord.abc.User) -> None:
        """Send each manager who actually uses Parley one concise setup DM per server.

        Owners still get the install-time DM. This covers moderators/partnership staff
        who do the day-to-day work without spamming them on every button click.
        """
        if user.bot:
            return
        async with self.db.session() as session:
            if await repository.has_audit_action(
                session, "manager_onboarding.sent", guild_id=guild.id, actor_id=user.id
            ):
                return

        from bot.views.welcome import action_button, persistent_view

        embed = discord.Embed(
            title=f"Managing Parley for {guild.name}",
            description=(
                "**Find Partners** is the easiest place to start. Parley checks the setup for this server and guides you "
                "through anything missing: the **server ad**, then the **partner-ad channel**.\n\n"
                "Everything stays **in sync** with the main Parley server, including the ad, Network settings, and cooldowns. "
                "When both servers accept a partnership, Parley exchanges their ads in the channels they chose."
            ),
            color=self.runtime.bot.color_primary,
        )
        embed.set_footer(text="Sent once per manager for this server.")
        view = persistent_view(
            action_button(self, "find", style=discord.ButtonStyle.success, row=0),
            action_button(self, "post", style=discord.ButtonStyle.primary, row=0),
        )
        try:
            await user.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            log.info("Could not DM manager onboarding for guild %s user %s: %s", guild.id, user.id, exc)
            return

        async with self.db.session() as session:
            await repository.add_audit(
                session, "manager_onboarding.sent", actor_id=user.id, guild_id=guild.id
            )

    async def _dm_join_fallback(self, guild: discord.Guild) -> None:
        """Explain the next step when Parley cannot speak in any server channel."""
        owner = guild.owner
        if owner is None:
            try:
                owner = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, AttributeError):
                owner = None
        if owner is None:
            log.info("No sendable onboarding channel and owner unavailable for guild %s", guild.id)
            return

        embed = discord.Embed(
            title="Parley needs a channel",
            description=(
                f"Parley was added to **{guild.name}**, but I can't send messages in any public channel yet.\n\n"
                "Give Parley **View Channel**, **Send Messages**, and **Read Message History** in a channel "
                "that **@everyone can view**, then run `/network` there to finish Network setup. Members can stay read-only.\n\n"
                "Private staff/bot-only channels do not count. Nothing has been posted automatically."
            ),
            color=self.runtime.bot.color_warning,
        )
        try:
            await owner.send(embed=embed)
        except discord.HTTPException as exc:
            log.info("Could not DM guild owner about missing send permission in %s: %s", guild.id, exc)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined server %s (%s, %d members)", guild.name, guild.id, guild.member_count or 0)
        await self.log_event(f"➕ Added to **{guild.name}** (`{guild.id}`).")
        if guild.id == self.hub.main_guild_id:
            return

        # Always give the owner a durable one-time explanation. This is audit
        # guarded, so reconnects and 24/7 restarts never resend it.
        await self._dm_guild_onboarding(guild)

        if not self.runtime.panels.send_join_message:
            return
        from bot.views.welcome import join_message

        channel = self.join_channel(guild)
        if channel is None:
            await self._dm_join_fallback(guild)
            return
        embed, view = join_message(self, guild)
        try:
            await channel.send(embed=embed, view=view, allowed_mentions=safe_allowed_mentions())
        except discord.HTTPException as exc:
            log.info("Could not send the welcome message in %s: %s", guild.id, exc)
            await self._dm_join_fallback(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        from bot.services import network

        log.info("Removed from server %s (%s)", guild.name, guild.id)
        async with self.db.session() as session:
            await network.disable(session, guild_id=guild.id, reason="Parley was removed from the server")
            listing = await repository.get_listing(session, guild.id)
            if listing is not None:
                partner_channel = listing.partner_channel_id
                partner_message = listing.partner_message_id
                partner_controls = listing.partner_controls_message_id
                listing.partner_ad_text = None
                listing.partner_channel_id = None
                listing.partner_message_id = None
                listing.partner_controls_message_id = None
                listing.partner_posted_at = None
            else:
                partner_channel = partner_message = partner_controls = None
        await self.panels.delete_listing_message(partner_channel, partner_controls)
        await self.panels.delete_listing_message(partner_channel, partner_message)
        await self.log_event(
            f"➖ Removed from **{guild.name}** (`{guild.id}`). "
            "Directory listing kept live; connected perks disabled."
        )

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.name == after.name and before.icon == after.icon:
            return
        async with self.db.session() as session:
            if await repository.get_guild(session, after.id) is not None:
                await repository.upsert_guild(
                    session,
                    guild_id=after.id,
                    name=after.name,
                    icon_url=after.icon.url if after.icon else None,
                    member_count=after.member_count or 0,
                )

    async def sync_listed_guilds(self) -> None:
        """Keep stored names/member counts current for Find Partners results."""
        async with self.db.session() as session:
            for listing in await repository.active_listings(session, include_test=True):
                guild = self.get_guild(listing.guild_id)
                if guild is not None and listing.status == ListingStatus.ACTIVE:
                    await repository.upsert_guild(
                        session,
                        guild_id=guild.id,
                        name=guild.name,
                        icon_url=guild.icon.url if guild.icon else None,
                        member_count=guild.member_count or 0,
                    )

    # ------------------------------------------------------------ shutdown

    async def close(self) -> None:
        log.info("Shutting down…")
        for task in list(self._interaction_ack_watchdogs.values()):
            task.cancel()
        if self._interaction_ack_watchdogs:
            await asyncio.gather(*self._interaction_ack_watchdogs.values(), return_exceptions=True)
            self._interaction_ack_watchdogs.clear()
        await self.oauth.stop()
        await self.background.stop()
        await self.panels.close()
        await super().close()
        log.info("Discord connection closed")
