from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()
STATE: dict[Path, str] = {}
ORIGINAL: dict[Path, str] = {}


def fp(path: str) -> Path:
    return ROOT / Path(path)


def get(path: str) -> str:
    file = fp(path)
    if file in STATE:
        return STATE[file]
    if not file.exists():
        raise SystemExit(f"[STOP] Missing expected file: {path}")
    text = file.read_text(encoding="utf-8")
    ORIGINAL[file] = text
    STATE[file] = text
    return text


def put(path: str, text: str) -> None:
    STATE[fp(path)] = text


core = get("bot/core.py")
BOT_CLASS = "ParleyBot" if "class ParleyBot" in core else "WaypointBot"


def brand(text: str) -> str:
    return text.replace("__BOT_CLASS__", BOT_CLASS)


def replace_once(path: str, old: str, new: str, label: str) -> None:
    old, new = brand(old), brand(new)
    text = get(path)
    if new in text and old not in text:
        return
    if old not in text:
        raise SystemExit(f"[STOP] Could not find {label} in {path}. No files were written.")
    put(path, text.replace(old, new, 1))


def insert_after(path: str, anchor: str, addition: str, marker: str, label: str) -> None:
    anchor, addition, marker = brand(anchor), brand(addition), brand(marker)
    text = get(path)
    if marker in text:
        return
    if anchor not in text:
        raise SystemExit(f"[STOP] Could not find anchor for {label} in {path}. No files were written.")
    put(path, text.replace(anchor, anchor + addition, 1))


def replace_block(path: str, start: str, end: str | None, new: str, label: str) -> None:
    start, new = brand(start), brand(new)
    if end is not None:
        end = brand(end)
    text = get(path)
    a = text.find(start)
    if a < 0:
        # If the distinctive new content is already present, treat it as already repaired.
        if new.strip()[:80] in text:
            return
        raise SystemExit(f"[STOP] Could not find start of {label} in {path}. No files were written.")
    if end is None:
        b = len(text)
        suffix = ""
    else:
        b = text.find(end, a)
        if b < 0:
            raise SystemExit(f"[STOP] Could not find end of {label} in {path}. No files were written.")
        suffix = text[b:]
    put(path, text[:a] + new.rstrip() + "\n\n" + suffix)


SIMPLE = [('bot/config/runtime.py', '"post": {"label": "Post My Server", "emoji": None, "style": "primary"}', '"post": {"label": "Post Server Ad", "emoji": None, "style": "primary"}', 'post label'), ('bot/config/runtime.py', '"find": {"label": "Find Partners", "emoji": None, "style": "success"}', '"find": {"label": "Browse Partners", "emoji": None, "style": "success"}', 'find label'), ('bot/config/runtime.py', '"servers": {"label": "My Listing", "emoji": None, "style": "primary"}', '"servers": {"label": "My Server Listings", "emoji": None, "style": "primary"}', 'servers label'), ('bot/config/runtime.py', '"looking": {"label": "Post Looking For Partner", "emoji": "\\U0001F4DD", "style": "primary"}', '"looking": {"label": "Post Partner Ad", "emoji": None, "style": "primary"}', 'looking label'), ('bot/config/runtime.py', '"request": {"label": "Request Partnership", "emoji": "🤝", "style": "primary"}', '"request": {"label": "Send Partner Request", "emoji": "🤝", "style": "primary"}', 'request label'), ('bot/config/runtime.py', '"view_ad": {"label": "View Ad", "emoji": None, "style": "primary"}', '"view_ad": {"label": "View Server Ad", "emoji": None, "style": "primary"}', 'view ad label'), ('bot/config/runtime.py', '"next": {"label": "Next Server", "emoji": None, "style": "primary"}', '"next": {"label": "Next Match", "emoji": None, "style": "primary"}', 'next label'), ('bot/config/runtime.py', '    "post", "connect", "find", "servers", "requests", "looking", "network", "join", "request",\n', '    "post", "connect", "find", "servers", "requests", "looking", "partner_posts", "network", "join", "request",\n', 'partner_posts customizable button'), ('bot/config/runtime.py', '**Post My Server** – list your server (needs Manage Server).', '**Post Server Ad** – publish a server ad (needs Manage Server).', 'help post'), ('bot/config/runtime.py', '**Find Partners** – browse servers and request a partnership.', '**Browse Partners** – quickly browse partnership matches.', 'help find'), ('bot/config/runtime.py', '**My Listing** – Relist, edit, view or remove your listing.', '**My Server Listings** – edit, view, Relist or remove server ads.', 'help listings'), ('bot/views/self_post.py', 'def availability(bot: __BOT_CLASS__, guild_id: int) -> Availability:\n', 'def availability(bot: __BOT_CLASS__, guild_id: int, *, channel: discord.TextChannel | None = None) -> Availability:\n', 'generic availability signature'), ('bot/views/self_post.py', '    channel = bot.panels.listings_channel()\n    if channel is None:\n        return Availability(False, "The server directory isn\'t set up yet.")\n', '    channel = channel or bot.panels.listings_channel()\n    if channel is None:\n        return Availability(False, "The destination channel isn\'t set up yet.")\n', 'generic availability channel'), ('bot/views/self_post.py', '    *,\n    draft_contacts=None,\n    on_created=None,\n    on_accept: Callable[[str, discord.Message], Awaitable[None]] | None = None,\n    on_review: Callable[[str], Awaitable[None]] | None = None,\n) -> str:\n', '    *,\n    channel: discord.TextChannel | None = None,\n    post_title: str = "Ad",\n    retry_hint: str = "Post Server Ad or My Server Listings",\n    allow_review: bool = True,\n    respect_approval_required: bool = True,\n    success_text: str | None = None,\n    draft_contacts=None,\n    on_created=None,\n    on_accept: Callable[[str, discord.Message], Awaitable[None]] | None = None,\n    on_review: Callable[[str], Awaitable[None]] | None = None,\n) -> str:\n', 'generic run_submission options'), ('bot/views/self_post.py', '    state = availability(bot, guild_id)\n    if not state.ok:\n        raise ValidationError(state.reason)\n    channel = bot.panels.listings_channel()\n    assert channel is not None\n', '    channel = channel or bot.panels.listings_channel()\n    state = availability(bot, guild_id, channel=channel)\n    if not state.ok:\n        raise ValidationError(state.reason)\n    assert channel is not None\n', 'generic run_submission destination'), ('bot/views/self_post.py', '        await interaction.edit_original_response(content=_instructions(channel, seconds, interaction.user), view=None)', '        await interaction.edit_original_response(\n            content=_instructions(channel, seconds, interaction.user, post_title=post_title), view=None\n        )', 'generic instructions call'), ('bot/views/self_post.py', '            return "## Ad not posted\\nListings are text-only right now."', '            return f"## {post_title} not posted\\nPosts are text-only right now."', 'generic attachment error'), ('bot/views/self_post.py', '            needs_review = listing_service.check_links(cleaned, bot.runtime) or bot.runtime.listings.approval_required', '            needs_review = listing_service.check_links(cleaned, bot.runtime) or (\n                respect_approval_required and bot.runtime.listings.approval_required\n            )', 'generic review condition'), ('bot/views/self_post.py', '            return f"## Ad not posted\\n{exc.user_message}\\n\\nYou can try again from **Post My Server** or **My Listing**."', '            return f"## {post_title} not posted\\n{exc.user_message}\\n\\nYou can try again from **{retry_hint}**."', 'generic retry error'), ('bot/views/self_post.py', '        if needs_review:\n            await _delete(message)\n            if on_review is not None:\n                await on_review(cleaned)\n            else:\n                await _store_text(bot, guild_id, cleaned, interaction.user.id)\n            if on_created is not None:\n                await on_created(cleaned, None)\n            return "## Ad sent for review\\nStaff will review it before it goes live."\n', '        if needs_review:\n            await _delete(message)\n            if not allow_review:\n                return (\n                    f"## {post_title} not posted\\n"\n                    "This post contains a link that requires staff review. Remove that link and try again."\n                )\n            if on_review is not None:\n                await on_review(cleaned)\n            else:\n                await _store_text(bot, guild_id, cleaned, interaction.user.id)\n            if on_created is not None:\n                await on_created(cleaned, None)\n            return f"## {post_title} sent for review\\nStaff will review it before it goes live."\n', 'generic review branch'), ('bot/views/self_post.py', '        return "## Ad posted\\nYour server is live in the directory."', '        return success_text or "## Ad posted\\nYour server is live in the directory."', 'generic success message'), ('bot/views/partnership.py', '        lines = [f"{category} • {format_members(self.current_members)}", f"Partner size: {requirement}"]\n        if notice:\n', '        lines = [f"{category} • {format_members(self.current_members)}", f"Partner size: {requirement}"]\n        if listing.partner_ad_text:\n            lines.extend(["", truncate(listing.partner_ad_text, 700)])\n        if notice:\n', 'finder partner text'), ('bot/views/partnership.py', '        if past_partners:\n            past = discord.ui.Button(label="Show Past Partners", style=discord.ButtonStyle.primary, row=0)\n            past.callback = self._past  # type: ignore[method-assign]\n            self.add_item(past)\n        another = discord.ui.Button(label="Try Another Category", style=discord.ButtonStyle.primary, row=0)\n        again = discord.ui.Button(label="Show Again", style=discord.ButtonStyle.primary, row=0)\n        another.callback = self._another  # type: ignore[method-assign]\n        again.callback = self._again  # type: ignore[method-assign]\n        self.add_item(another)\n        self.add_item(again)\n', '        if past_partners:\n            past = discord.ui.Button(label="Show Past Partners", style=discord.ButtonStyle.primary, row=0)\n            past.callback = self._past  # type: ignore[method-assign]\n            self.add_item(past)\n        if category != ANY:\n            another = discord.ui.Button(label="Try Another Category", style=discord.ButtonStyle.primary, row=0)\n            another.callback = self._another  # type: ignore[method-assign]\n            self.add_item(another)\n        again = discord.ui.Button(label="Show Again", style=discord.ButtonStyle.primary, row=0)\n        again.callback = self._again  # type: ignore[method-assign]\n        self.add_item(again)\n        self.add_item(home_button(self.bot, row=1))\n', 'finder exhausted controls'), ('bot/views/welcome.py', '            item.label = "My Servers" if multiple else "My Listing"\n', '            item.label = "My Server Listings" if multiple else "My Server Listing"\n', 'personalized listing label'), ('bot/services/setup.py', '"Talk about partnerships or use Parley to discover a server.",', '"Post a partner ad or browse matches with Parley.",', 'find-partners topic'), ('bot/services/setup.py', 'ChannelSlot("log_channel_id", "waypoint-logs", "Staff logs", False, "Parley staff log and approvals."),', 'ChannelSlot("log_channel_id", "parley-logs", "Staff logs", False, "Parley staff log and approvals.", ("waypoint-logs",)),', 'parley logs'), ('bot/services/setup.py', '    own = listings_channel_permissions() if slot.key == "listings_channel_id" else bot_channel_permissions()', '    own = listings_channel_permissions() if slot.key in ("listings_channel_id", "looking_channel_id") else bot_channel_permissions()', 'find-partners bot perms'), ('bot/services/setup.py', '    if slot.key in ("welcome_channel_id", "listings_channel_id"):\n', '    if slot.key in ("welcome_channel_id", "listings_channel_id", "looking_channel_id"):\n', 'find-partners member lock'), ('bot/services/setup.py', '        if slot.key == "listings_channel_id":\n', '        if slot.key in ("listings_channel_id", "looking_channel_id"):\n', 'repair find-partners lock'), ('bot/core.py', '        if not self.hub.listings_channel_id or after.channel.id != self.hub.listings_channel_id:\n            return\n        if before.content == after.content:\n            return\n\n        from bot.services import listings as listing_service\n', '        if before.content == after.content:\n            return\n\n        if self.hub.looking_channel_id and after.channel.id == self.hub.looking_channel_id:\n            from bot.views import partner_posts\n\n            await partner_posts.handle_direct_edit(self, before, after)\n            return\n\n        if not self.hub.listings_channel_id or after.channel.id != self.hub.listings_channel_id:\n            return\n\n        from bot.services import listings as listing_service\n', 'protect direct partner edits'), ('bot/views/management.py', '    await bot.panels.delete_listing_message(listing.channel_id, listing.controls_message_id)\n    await bot.panels.delete_listing_message(listing.channel_id, listing.message_id)\n    async with bot.db.session() as session:\n        await listing_service.record_message(session, guild_id=guild_id, channel_id=None, message_id=None)\n', '    await bot.panels.take_down_listing(listing)\n', 'listing removal cleans partner post')]
INSERTS = [('bot/config/runtime.py', '    "looking": {"label": "Post Partner Ad", "emoji": None, "style": "primary"},\n', '    "partner_posts": {"label": "My Partner Posts", "emoji": None, "style": "primary"},\n', '"partner_posts":', 'partner_posts button'), ('bot/database/models.py', '    controls_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)\n', '    # Independent owner-authored post in #find-partners.\n    partner_ad_text: Mapped[str | None] = mapped_column(Text, nullable=True)\n    partner_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)\n    partner_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)\n    partner_controls_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)\n    partner_posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)\n', 'partner_message_id:', 'partner post model fields'), ('bot/database/repository.py', '\n\nasync def get_listings(session: AsyncSession, guild_ids: Iterable[int]) -> list[Listing]:\n', '\n\nasync def get_listing_by_partner_message(session: AsyncSession, message_id: int) -> Listing | None:\n    """Find the active listing whose owner-authored partner post uses this message."""\n    return await session.scalar(\n        select(Listing).where(\n            Listing.partner_message_id == message_id,\n            Listing.status == ListingStatus.ACTIVE,\n        )\n    )\n', 'get_listing_by_partner_message', 'partner message repository lookup'), ('bot/core.py', 'VIEW_MODULES = ("bot.views.network",', '"bot.views.partner_posts", ', 'bot.views.partner_posts', 'load partner_posts module'), ('bot/services/testmode.py', '        refs += [\n            (listing.review_channel_id, listing.review_message_id)\n            for listing in rows\n            if listing.review_channel_id and listing.review_message_id\n        ]\n', '        refs += [\n            (listing.partner_channel_id, listing.partner_message_id)\n            for listing in rows\n            if listing.partner_channel_id and listing.partner_message_id\n        ]\n        refs += [\n            (listing.partner_channel_id, listing.partner_controls_message_id)\n            for listing in rows\n            if listing.partner_channel_id and listing.partner_controls_message_id\n        ]\n', 'listing.partner_controls_message_id', 'test partner post cleanup')]
BLOCKS = [('bot/config/runtime.py', '    listings_panel_text: str = (\n', '    send_join_message: bool = True\n', '    listings_panel_text: str = (\n        "## Server Directory\\n"\n        "Post or manage a server ad."\n    )\n    looking_panel_enabled: bool = True\n    looking_panel_text: str = (\n        "## Find a Partner\\n"\n        "Post what your server is looking for or browse matches."\n    )\n    welcome_panel_enabled: bool = True\n    welcome_panel_text: str = (\n        "## Welcome to Parley\\n"\n        "Choose where you want to go."\n    )\n', 'public panel copy'), ('bot/views/self_post.py', 'def _instructions(channel: discord.TextChannel, seconds: int, user: discord.abc.User) -> str:\n', 'def _previous_overwrite(', 'def _instructions(\n    channel: discord.TextChannel,\n    seconds: int,\n    user: discord.abc.User,\n    *,\n    post_title: str = "ad",\n) -> str:\n    minutes = max(1, seconds // 60)\n    url = f"https://discord.com/channels/{channel.guild.id}/{channel.id}"\n    return (\n        "## Your posting window is open\\n"\n        f"**Go to:** [#{channel.name}]({url})\\n"\n        f"**Time limit:** {minutes} minute{\'s\' if minutes != 1 else \'\'}\\n\\n"\n        f"Send your {post_title.lower()} as **one message**. As soon as you post, Parley locks your "\n        "posting permission again automatically.\\n"\n        "-# I also pinged only you in the channel so it is easy to find."\n    )\n', 'generic posting instructions'), ('bot/views/partnership.py', '@register_action("find")\nasync def start_find_flow', 'class CategoryView(OwnedView):', '@register_action("find")\nasync def start_find_flow(interaction: discord.Interaction) -> None:\n    """Browse matches immediately; ask for a source server only when the user has several."""\n    bot = get_bot(interaction)\n    async with bot.db.session() as session:\n        sources = await represented_listings(bot, session, interaction.user.id)\n        guilds = await repository.get_guilds(session, [s.guild_id for s in sources])\n\n    if len(sources) > 1:\n        async def picked(inter: discord.Interaction, guild_id: int) -> None:\n            await FinderView(bot, inter.user.id, category=ANY, source_id=guild_id).show(inter)\n\n        embed = discord.Embed(\n            title="Browse Partners",\n            description="Which server are you finding a partner for?",\n            color=bot.runtime.bot.color_primary,\n        )\n        await reply(\n            interaction,\n            embed=embed,\n            view=GuildPickerView(\n                interaction.user.id,\n                [(s.guild_id, guild_name(guilds, s.guild_id)) for s in sources],\n                picked,\n                placeholder="Choose your server",\n            ),\n        )\n        return\n\n    source_id = sources[0].guild_id if sources else None\n    await FinderView(bot, interaction.user.id, category=ANY, source_id=source_id).show(interaction)\n', 'simple find entry'), ('bot/views/partnership.py', '    def build(self) -> None:\n        self.clear_items()\n        assert self.current is not None\n', '    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:\n', '    def build(self) -> None:\n        self.clear_items()\n        assert self.current is not None\n        gid = self.current.guild_id\n\n        request = discord.ui.Button(label="Send Partner Request", style=discord.ButtonStyle.success, row=0)\n        request.callback = self._request  # type: ignore[method-assign]\n        self.add_item(request)\n\n        nxt = discord.ui.Button(label="Next Match", style=discord.ButtonStyle.primary, row=0)\n        nxt.callback = self._next  # type: ignore[method-assign]\n        self.add_item(nxt)\n\n        partner_url = listing_jump_url(\n            self.bot.runtime.hub.main_guild_id,\n            self.current.partner_channel_id,\n            self.current.partner_message_id,\n        )\n        if partner_url:\n            self.add_item(discord.ui.Button(label="View Partner Post", url=partner_url, row=0))\n        else:\n            self.add_item(view_ad_button(self.bot, gid, label="View Server Ad", row=0))\n\n        self.add_item(home_button(self.bot, row=1))\n', 'finder distinct buttons'), ('bot/views/partnership.py', '# ---------------------------------------------------------------- looking for partners\n', None, '# ---------------------------------------------------------------- partner posting\n\n\n@register_action("looking")\nasync def start_looking_post(interaction: discord.Interaction) -> None:\n    """Old action ID kept so existing buttons open the new partner-post manager."""\n    from bot.views.partner_posts import start_partner_posts\n\n    await start_partner_posts(interaction)\n', 'replace old looking post flow'), ('bot/views/welcome.py', 'def control_panel(', 'def set_listing_button_label(', 'def control_panel(bot: __BOT_CLASS__, *, staff: bool = False) -> tuple[str, discord.ui.View]:\n    """The DM home: five clear user actions, plus Settings for staff."""\n    content = templates.render(bot.runtime, "dm_home")\n    if staff and bot.runtime.hub.mode != "live":\n        content += f"\\n-# Mode: {\'🧪 TEST\' if bot.runtime.hub.mode == \'test\' else \'🔴 OFF\'}"\n    view = persistent_view(\n        action_button(bot, "post", row=0),\n        action_button(bot, "find", row=0),\n        action_button(bot, "servers", row=1),\n        action_button(bot, "partner_posts", row=1),\n        action_button(bot, "requests", row=2),\n        action_button(bot, "settings", row=3) if staff else None,\n    )\n    return content, view\n', 'clean DM panel'), ('bot/views/welcome.py', 'def listings_panel(', 'def join_message(', 'def listings_panel(bot: __BOT_CLASS__) -> tuple[str, discord.ui.View]:\n    """Server Directory controls: only server-ad actions."""\n    view = persistent_view(\n        action_button(bot, "post", row=0),\n        action_button(bot, "servers", row=0),\n        action_button(bot, "relist", row=1),\n    )\n    return templates.render(bot.runtime, "listings_panel"), view\n\n\ndef looking_panel(bot: __BOT_CLASS__) -> tuple[str, discord.ui.View]:\n    """Find-a-Partner controls: deliberately different from Server Directory."""\n    view = persistent_view(\n        action_button(bot, "find", row=0),\n        action_button(bot, "looking", row=0),\n        action_button(bot, "partner_posts", row=1),\n    )\n    return templates.render(bot.runtime, "looking_panel"), view\n\n\ndef _hub_channel_url(bot: __BOT_CLASS__, channel_id: int | None) -> str | None:\n    guild_id = bot.runtime.hub.main_guild_id\n    if not guild_id or not channel_id:\n        return None\n    return f"https://discord.com/channels/{guild_id}/{channel_id}"\n\n\ndef welcome_panel(bot: __BOT_CLASS__) -> tuple[str, discord.ui.View]:\n    """Start Here is a tiny router, not another management dashboard."""\n    directory_url = _hub_channel_url(bot, bot.runtime.hub.listings_channel_id)\n    partner_url = _hub_channel_url(bot, bot.runtime.hub.looking_channel_id)\n    view = persistent_view(\n        discord.ui.Button(label="Server Directory", url=directory_url, row=0) if directory_url else None,\n        discord.ui.Button(label="Find a Partner", url=partner_url, row=0) if partner_url else None,\n        action_button(bot, "post", row=1),\n        action_button(bot, "looking", row=1),\n    )\n    return templates.render(bot.runtime, "welcome"), view\n', 'separate public panels'), ('bot/services/panels.py', '    async def ensure_directory_locked(self) -> bool:\n', '    # ------------------------------------------------------------ low-level message helpers\n', '    async def ensure_directory_locked(self) -> bool:\n        """Keep both public feeds read-only except for Parley\'s short posting windows."""\n        channels = [self.listings_channel(), self.looking_channel()]\n        found = False\n        all_ok = True\n        for channel in channels:\n            if channel is None or channel.guild.me is None:\n                continue\n            found = True\n            me = channel.guild.me\n            perms = channel.permissions_for(me)\n            if not perms.manage_roles:\n                all_ok = False\n                continue\n            everyone = channel.guild.default_role\n            current = channel.overwrites_for(everyone)\n            allow, deny = current.pair()\n            updated = discord.PermissionOverwrite.from_pair(allow, deny)\n            updated.send_messages = False\n            if hasattr(updated, "create_public_threads"):\n                updated.create_public_threads = False\n            if hasattr(updated, "send_messages_in_threads"):\n                updated.send_messages_in_threads = False\n            try:\n                await channel.set_permissions(everyone, overwrite=updated, reason="Parley: lock managed feed")\n            except discord.HTTPException as exc:\n                log.warning("Could not lock #%s: %s", channel.name, exc)\n                all_ok = False\n        return found and all_ok\n', 'lock both managed feeds'), ('bot/services/panels.py', '    async def take_down_listing(self, listing) -> None:\n', '    async def _is_newest(', '    async def take_down_listing(self, listing) -> None:\n        """Delete every public post owned by a listing after removal/suspension/ban."""\n        if listing is None:\n            return\n        await self._delete(listing.channel_id, listing.controls_message_id)\n        await self._delete(listing.channel_id, listing.message_id)\n        await self._delete(listing.partner_channel_id, listing.partner_controls_message_id)\n        await self._delete(listing.partner_channel_id, listing.partner_message_id)\n        async with self.bot.db.session() as session:\n            await listing_service.record_message(session, guild_id=listing.guild_id, channel_id=None, message_id=None)\n            current = await repository.get_listing(session, listing.guild_id)\n            if current is not None:\n                current.partner_ad_text = None\n                current.partner_channel_id = None\n                current.partner_message_id = None\n                current.partner_controls_message_id = None\n                current.partner_posted_at = None\n', 'remove partner post with listing'), ('bot/core.py', '        # #server-directory is not a chat channel.', '    async def on_message_edit(', '        # Server Directory and Find a Partner are controlled feeds. A manager gets\n        # a short one-message window; everything else is removed.\n        protected = {\n            cid\n            for cid in (self.hub.listings_channel_id, self.hub.looking_channel_id)\n            if cid\n        }\n        if message.channel.id in protected:\n            from bot.views import self_post\n\n            if self_post.is_active_submission(message.channel.id, message.author.id, message.id):\n                return\n            try:\n                await message.delete()\n            except discord.HTTPException as exc:\n                log.warning("Could not remove unauthorized feed message %s: %s", message.id, exc)\n            return\n', 'protect both public feeds'), ('bot/core.py', '        # If an owner deletes their own direct-post ad,', '    async def on_guild_channel_delete(', '        # Keep owner-authored public-post state valid after manual deletion.\n        if payload.channel_id == self.hub.listings_channel_id:\n            async with self.db.session() as session:\n                listing = await repository.get_listing_by_message(session, payload.message_id)\n            if listing is not None:\n                await self.panels.self_post_message_removed(listing.guild_id)\n                log.info("listing.self_post_deleted guild_id=%s message_id=%s", listing.guild_id, payload.message_id)\n        elif payload.channel_id == self.hub.looking_channel_id:\n            from bot.views import partner_posts\n\n            await partner_posts.handle_raw_delete(self, payload.channel_id, payload.message_id)\n', 'partner raw delete')]

for args in SIMPLE:
    replace_once(*args)

for args in INSERTS:
    insert_after(*args)

# Optional help-copy update: older local versions differ here, so never block the repair.
path = "bot/config/runtime.py"
text = get(path)
if "**My Partner Posts**" not in text:
    request_text = "**Requests** – accept or decline partnership requests."
    if request_text in text:
        text = text.replace(
            request_text,
            "**My Partner Posts** – post, edit or delete what each server is looking for.\n" + request_text,
            1,
        )
        put(path, text)

for args in BLOCKS:
    replace_block(*args)

# Preserve the green style of partner-request buttons after a restart.
path = "bot/views/partnership.py"
text = get(path)
a = text.find("class RequestPartnershipButton")
b = text.find("class ViewAdButton", a)
if a >= 0 and b > a:
    segment = text[a:b]
    old = '        return cls(int(match["gid"]), label=item.label, emoji=item.emoji)\n'
    new = '        return cls(int(match["gid"]), label=item.label, emoji=item.emoji, style=item.style)\n'
    if old in segment:
        segment = segment.replace(old, new, 1)
        put(path, text[:a] + segment + text[b:])

# Make the newly added partner-post module use the renamed internal bot class too.
path = "bot/views/partner_posts.py"
text = get(path)
if BOT_CLASS == "ParleyBot" and "WaypointBot" in text:
    put(path, text.replace("WaypointBot", "ParleyBot"))

# Required migration should already have been created by the first updater.
migration = fp("migrations/versions/0006_partner_posts.py")
if not migration.exists():
    raise SystemExit("[STOP] migrations/versions/0006_partner_posts.py is missing. No files were written.")

# Validate the important repair points before writing.
checks = {
    "bot/config/runtime.py": ['"find": {"label": "Browse Partners"', '"partner_posts":', '"post": {"label": "Post Server Ad"'],
    "bot/database/models.py": ["partner_message_id:", "partner_controls_message_id:"],
    "bot/database/repository.py": ["get_listing_by_partner_message"],
    "bot/views/welcome.py": ['label="Server Directory"', 'action_button(bot, "partner_posts"'],
    "bot/services/setup.py": ['"parley-logs"', 'slot.key in ("listings_channel_id", "looking_channel_id")'],
    "bot/core.py": ["bot.views.partner_posts", "self.hub.looking_channel_id"],
    "bot/views/partnership.py": ["Send Partner Request", "Next Match", "start_partner_posts"],
    "bot/views/self_post.py": ['post_title: str = "Ad"', "channel: discord.TextChannel | None = None"],
}
for file, needles in checks.items():
    built = get(file)
    for needle in needles:
        if needle not in built:
            raise SystemExit(f"[STOP] Validation failed: {needle!r} missing from {file}. No files were written.")

changed = []
for file, text in STATE.items():
    old = ORIGINAL.get(file)
    if old == text:
        continue
    file.write_text(text, encoding="utf-8", newline="\n")
    changed.append(str(file.relative_to(ROOT)))

print()
print("[OK] Parley repair applied.")
print("This fixes the Windows path-normalization bug from the first updater.")
print()
print("Changed:")
for item in sorted(changed):
    print(f"  - {item}")
print()
print("Run:")
print("  python -m pytest -q")
print("  python -m pyflakes bot tests migrations")
print("  git diff --check")
print()
print("If pytest is green:")
print("  git add .")
print('  git commit -m "Add partner posts and complete Parley rebrand"')
print("  git push")
