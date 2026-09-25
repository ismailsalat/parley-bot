"""Runtime business settings.

Defaults live here. Any value can be overridden by a row in the
``runtime_settings`` table using a dotted key such as
``listings.refresh_cooldown_minutes``. Parley reloads these rows
periodically, so a future dashboard only has to write to that table.

Use ``python -m bot.config.cli`` to list or change values from a terminal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace
from typing import Any

from bot.utils.emoji import is_valid_emoji

# Discord hard limits that configuration may never exceed.
DISCORD_MESSAGE_LIMIT = 2000
DISCORD_SELECT_OPTION_LIMIT = 25
HARD_MIN_NETWORK_INTERVAL_MINUTES = 15

DEFAULT_BUTTONS: dict[str, dict[str, str | None]] = {
    # Label, emoji and colour of every user-facing button.
    "post": {"label": "Post Server Ad", "emoji": None, "style": "primary"},
    "connect": {"label": "Connect This Server", "emoji": None, "style": "primary"},
    "find": {"label": "Find Partners", "emoji": "🤝", "style": "success"},
    "servers": {"label": "My Server Listings", "emoji": None, "style": "primary"},
    "requests": {"label": "Requests", "emoji": None, "style": "primary"},
    "looking": {"label": "Partner Board", "emoji": None, "style": "success"},
    "partner_posts": {"label": "My Partner Posts", "emoji": None, "style": "success"},
    "network": {"label": "Setup Network", "emoji": "🤝", "style": "success"},
    "network_help": {"label": "How Network Works", "emoji": "🌐", "style": "secondary"},
    "perks": {"label": "Parley Perks", "emoji": "💎", "style": "success"},
    "join": {"label": "Join Server", "emoji": None, "style": "primary"},
    "request": {"label": "Request Partnership", "emoji": "🤝", "style": "success"},
    "view_ad": {"label": "View Server", "emoji": None, "style": "primary"},
    "next": {"label": "Next", "emoji": None, "style": "primary"},
    "edit": {"label": "Edit", "emoji": None, "style": "primary"},
    "edit_ad": {"label": "Edit Ad", "emoji": None, "style": "primary"},
    "edit_info": {"label": "Server Info", "emoji": None, "style": "primary"},
    "partnerships": {"label": "Partnerships", "emoji": "🤝", "style": "success"},
    "preview": {"label": "View Ad", "emoji": None, "style": "primary"},
    "self_post": {"label": "Paste My Own Ad", "emoji": None, "style": "primary"},
    "refresh": {"label": "Relist", "emoji": None, "style": "secondary"},
    "relist": {"label": "Relist", "emoji": None, "style": "secondary"},
    "publish": {"label": "Publish", "emoji": None, "style": "primary"},
    "remove": {"label": "Remove Listing", "emoji": None, "style": "danger"},
    "accept": {"label": "Accept", "emoji": "\u2705", "style": "success"},
    "decline": {"label": "Decline", "emoji": "\u274C", "style": "danger"},
    "add_bot": {"label": "Add Parley", "emoji": None, "style": "secondary"},
    "support": {"label": "Support", "emoji": "💬", "style": "primary"},
    "rules": {"label": "Rules", "emoji": None, "style": "primary"},
    "website": {"label": "Website", "emoji": None, "style": "primary"},
    "how": {"label": "How It Works", "emoji": None, "style": "primary"},
    "back": {"label": "Back", "emoji": "⬅️", "style": "secondary"},
    "home": {"label": "Home", "emoji": "🏠", "style": "secondary"},
    "directory": {"label": "All Listings", "emoji": None, "style": "secondary"},
    "settings": {"label": "Settings", "emoji": "\u2699\uFE0F", "style": "primary"},
}

BUTTON_STYLES = ("primary", "success", "danger", "secondary")

# Buttons an administrator may relabel in Settings -> Appearance (custom_ids never change).
CUSTOMIZABLE_BUTTONS = (
    "post", "connect", "find", "servers", "requests", "looking", "partner_posts",
    "network", "network_help", "perks", "join", "request", "view_ad", "next", "edit", "edit_ad", "edit_info",
    "partnerships", "preview", "self_post", "refresh", "relist", "publish", "remove",
    "accept", "decline", "add_bot", "support", "rules", "website", "directory",
)

MODES = ("test", "live", "off")


@dataclass(frozen=True)
class BotConfig:
    name: str = "Parley"
    status: str = "online"  # online | idle | dnd
    activity_text: str = "DM me to find partners"
    support_url: str = ""
    rules_url: str = ""
    website_url: str = ""
    # Parley uses a cooler violet accent instead of the generic Discord blurple/yellow-card look.
    color_primary: int = 0x7C5CFC
    color_success: int = 0x22C55E
    color_danger: int = 0xED4245
    color_warning: int = 0xA78BFA


@dataclass(frozen=True)
class ListingConfig:
    # Standard listings keep the existing cooldown. Connected servers get a shorter cooldown.
    refresh_cooldown_minutes: int = 30
    connected_refresh_cooldown_minutes: int = 10
    max_ad_length: int = 1800
    categories: tuple[str, ...] = ("Gaming", "Anime", "Social", "Community", "Roleplay", "Creator")
    max_categories: int = 1
    max_contacts: int = 3
    approval_required: bool = False
    reapprove_ad_edits: bool = True  # with approval on: ad text edits wait for staff
    reapprove_info_edits: bool = False
    # "Paste My Own Ad": the owner posts their own ad message (keeps foreign custom emoji)
    allow_self_post: bool = True
    self_post_timeout_seconds: int = 180  # with approval on: category/minimum/invite edits wait too
    expiration_days: int = 30  # 0 disables expiry
    invite_required: bool = True
    auto_create_invite: bool = True
    minimum_member_options: tuple[int, ...] = (0, 50, 100, 250, 500, 1000)
    find_page_size: int = 4


@dataclass(frozen=True)
class PartnershipConfig:
    request_cooldown_seconds: int = 120
    # Shared by every admin representing the same server, so two admins cannot spam at once.
    server_request_cooldown_seconds: int = 300
    max_pending_requests: int = 5
    enforce_minimum_members: bool = True
    max_message_length: int = 300
    decline_cooldown_hours: int = 24
    dm_notifications: bool = True
    request_expiration_days: int = 7
    looking_post_cooldown_minutes: int = 60
    looking_panel_debounce_seconds: int = 8
    pair_cooldown_hours: int = 168  # same two servers: 7 days


@dataclass(frozen=True)
class NetworkConfig:
    enabled: bool = True
    min_interval_minutes: int = 60
    default_interval_minutes: int = 180
    interval_options: tuple[int, ...] = (60, 120, 180, 360, 720, 1440)
    category_filtering: bool = True
    rotation_strategy: str = "least_recent"  # least_recent | random
    repeat_window_hours: int = 12
    tick_seconds: int = 60
    send_spacing_seconds: float = 1.5
    max_posts_per_tick: int = 20
    post_retention_days: int = 30


@dataclass(frozen=True)
class PanelConfig:
    listings_panel_enabled: bool = True
    listings_panel_text: str = (
        "# 📣 Server Directory\n"
        "*Live server ads, kept simple.*\n\n"
        "Browse a listing to **join the server** or **request a partnership** directly.\n"
        "Want your own server here? Use **Post Server Ad**. Already listed? Use **Relist** when your cooldown is ready.\n\n"
        "-# Your listing does not need Parley installed to stay live."
    )
    looking_panel_enabled: bool = True
    looking_panel_text: str = (
        "# 🤝 Partner Board\n"
        "*For servers actively looking for a partnership right now.*\n\n"
        "**Before you post**\n"
        "`01` Add Parley to the server you want to represent.\n"
        "`02` Check your **DMs from Parley** and finish that server's setup.\n"
        "`03` Make sure **Partnerships** are turned on for that listing.\n\n"
        "**Find Partners** browses the board. **My Partner Posts** lets you choose which of your servers to advertise.\n\n"
        "-# Manage several servers? Parley always asks which server you want to use."
    )
    welcome_panel_enabled: bool = True
    welcome_panel_text: str = (
        "# 👋 Welcome to Parley\n"
        "*Connect. Partner. Exchange ads.*\n\n"
        "**Simple setup**\n"
        "`01` **Add Parley** to the server you manage.\n"
        "`02` Press **Find Partners**. Parley checks that server's ad and Network setup for you.\n"
        "`03` Choose a server and send a partnership request.\n\n"
        "When both servers accept, Parley exchanges their ads in the partner-ad channels they chose.\n"
        "-# **Post Server Ad** is also available if you only want to manage your public directory ad."
    )
    perks_panel_enabled: bool = True
    perks_panel_text: str = (
        "# 💎 Parley Connected\n"
        "*Your listing works without the bot. Connected adds the automation.*\n\n"
        "**Without Parley**\n"
        "`✓` Keep your directory listing live\n"
        "`✓` Edit your ad and server info\n"
        "`✓` Relist on the standard cooldown\n"
        "`✓` Send and receive basic partnership requests\n\n"
        "**With Parley Connected**\n"
        "`⚡` Faster relists\n"
        "`🤝` Find Partners + Partner Board posts\n"
        "`✅` One-click request handling\n"
        "`🤖` Auto Partner tools\n"
        "`🌐` Network channel for approved partner-ad sharing\n\n"
        "**What is the Network?**\n"
        "When **both servers accept a partnership** and both enable Network, Parley can place each approved ad in the other server's chosen channel. It never exchanges ads just because a server is listed.\n\n"
        "-# To unlock Connected features: Add Parley → check your DMs → finish setup."
    )

    # Start Here may make one intentional announcement when the panel is first created.
    welcome_ping_everyone: bool = True
    welcome_ping_role_ids: tuple[int, ...] = (1552864166115549224,)
    # Kept only so older saved settings continue to load. Public panels no longer render banner art.
    panel_images_enabled: bool = False
    send_join_message: bool = True
    buttons: dict[str, dict[str, str]] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_BUTTONS.items()})


@dataclass(frozen=True)
class ModerationConfig:
    blocked_guild_ids: tuple[int, ...] = ()
    blocked_user_ids: tuple[int, ...] = ()
    blocked_words: tuple[str, ...] = ()
    # Link safety for user-written ads
    blocked_domains: tuple[str, ...] = (
        "grabify.link", "iplogger.org", "iplogger.com", "bit.do", "yip.su", "ps3cfw.com", "2no.co",
    )
    max_links: int = 5  # 0 = no limit
    link_action: str = "review"  # review | block | allow  (what to do with a suspicious link)
    button_rate_limit: int = 10
    button_rate_window_seconds: int = 10
    dm_panel_cooldown_seconds: int = 20


@dataclass(frozen=True)
class HubConfig:
    """The main Parley server. Set with /setup and /settings (0 = not set)."""

    main_guild_id: int = 0
    welcome_channel_id: int = 0
    listings_channel_id: int = 0
    looking_channel_id: int = 0
    perks_channel_id: int = 0
    support_channel_id: int = 0
    log_channel_id: int = 0
    staff_role_ids: tuple[int, ...] = ()
    mode: str = "live"  # test | live | off  (setup switches a fresh install to test)
    setup_completed: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.main_guild_id)


@dataclass(frozen=True)
class MessagesConfig:
    """User-facing texts (see bot.config.templates for placeholders)."""

    dm_home: str = "## {bot_name}\nWhat would you like to do?"
    join_message: str = (
        "**{server_name}** is connected to {bot_name}.\n\n"
        "Press **Find Partners** to get started. Parley will automatically walk you through anything missing: "
        "your server ad first, then the partner-ad channel for this server.\n\n"
        "Nothing is posted automatically just because Parley was invited, and nothing is exchanged until both servers accept a partnership. "
        "**Post Server Ad** is still available separately for ad management."
    )
    listing_created: str = "**{server_name}** is live. {jump_url}\nUse **My Listing** anytime to edit or Relist it."
    listing_saved_unpublished: str = "**{server_name}** is saved. It will appear once the server directory is ready."
    listing_pending: str = "**{server_name}** was sent to staff for review. I'll DM you when it's live."
    listing_approved: str = "✅ Your listing for **{server_name}** was approved and is now live."
    listing_rejected: str = "Your listing for **{server_name}** was not approved. Contact support if you have questions."
    edit_pending: str = "Your changes to **{server_name}** were sent to staff. Your current ad stays live until they're approved."
    edit_approved: str = "✅ Your changes to **{server_name}** were approved and are now live."
    edit_rejected: str = "Your changes to **{server_name}** were not approved. Your previous ad is still live."
    refresh_success: str = "**{server_name}** was Relisted and is back at the top of the directory. {jump_url}"
    request_received: str = "**{requester_server}** wants to partner with **{target_server}**."
    request_sent: str = "Request sent: **{requester_server}** → **{target_server}**. Their contacts can answer any time. I'll DM you when they do."
    request_accepted: str = "## Partnership accepted ✅\n**{requester_server}** 🤝 **{target_server}**\nYou can contact each other here:"
    request_declined: str = "Thanks for reaching out. **{target_server}** isn't able to partner with **{requester_server}** right now."
    network_footer: str = "-# 🌐 Shared by the {bot_name} network"
    maintenance: str = "{bot_name} is temporarily unavailable.\nPlease try again later."
    test_mode: str = "🧪 {bot_name} is being set up. Please try again soon."
    support: str = "Need help with {bot_name}? Tell staff what you were trying to do and include the server name if it is about a listing or partnership."
    network_help: str = (
        "## 🌐 How the Parley Network Works\n"
        "The Network is **not** a random advertising feed. It is the delivery layer for partnerships that both servers approved.\n\n"
        "`1` Two listed servers agree to partner.\n"
        "`2` Both servers have Parley connected and choose a Network channel.\n"
        "`3` Parley exchanges the approved server ads between those chosen channels.\n\n"
        "**Parley never intentionally posts Server A into Server B unless both sides agreed to the partnership.**"
    )
    help: str = (
        "## {bot_name}\n"
        "Connect once. Post once. Find partners. Talk to people.\n\n"
        "**Post Server Ad** - publish a server ad (needs Manage Server).\n"
        "**Browse Partners** - quickly browse partnership matches.\n"
        "**My Server Listings** - edit, view, Relist or remove server ads.\n"
        "**My Partner Posts** - post, edit or delete what each server is looking for.\n"
        "**Requests** - accept or decline partnership requests.\n\n"
        "Just DM me anytime to get these buttons."
    )


@dataclass(frozen=True)
class SystemConfig:
    maintenance_interval_minutes: int = 5


@dataclass(frozen=True)
class RuntimeConfig:
    bot: BotConfig = field(default_factory=BotConfig)
    listings: ListingConfig = field(default_factory=ListingConfig)
    partnerships: PartnershipConfig = field(default_factory=PartnershipConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    panels: PanelConfig = field(default_factory=PanelConfig)
    moderation: ModerationConfig = field(default_factory=ModerationConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    hub: HubConfig = field(default_factory=HubConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)

    def button(self, key: str) -> tuple[str, str | None]:
        spec = self.panels.buttons.get(key) or DEFAULT_BUTTONS.get(key) or {"label": key}
        label = str(spec.get("label") or DEFAULT_BUTTONS.get(key, {}).get("label") or key)[:80]
        emoji = spec.get("emoji") or None
        # never render an emoji Discord would reject: the button still works without one
        return label, emoji if emoji and is_valid_emoji(str(emoji)) else None

    def button_style(self, key: str) -> str:
        spec = self.panels.buttons.get(key) or DEFAULT_BUTTONS.get(key) or {}
        style = str(spec.get("style") or DEFAULT_BUTTONS.get(key, {}).get("style") or "primary")
        return style if style in BUTTON_STYLES else "primary"


class ConfigValueError(ValueError):
    pass


def _coerce(value: Any, current: Any) -> Any:
    """Coerce ``value`` into the type of the default ``current``."""
    if isinstance(value, str) and not isinstance(current, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigValueError(f"expected JSON for a {type(current).__name__} value") from exc

    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ConfigValueError("expected true or false")
    if isinstance(current, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ConfigValueError("expected a whole number")
        return int(value)
    if isinstance(current, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigValueError("expected a number")
        return float(value)
    if isinstance(current, str):
        if not isinstance(value, str):
            raise ConfigValueError("expected text")
        return value
    if isinstance(current, tuple):
        if not isinstance(value, (list, tuple)):
            raise ConfigValueError("expected a list")
        return tuple(value)
    if isinstance(current, dict):
        if not isinstance(value, dict):
            raise ConfigValueError("expected an object")
        merged = {k: dict(v) if isinstance(v, dict) else v for k, v in current.items()}
        for key, item in value.items():
            if isinstance(item, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **item}
            else:
                merged[key] = item
        return merged
    raise ConfigValueError("unsupported setting type")


def known_keys(config: RuntimeConfig | None = None) -> dict[str, Any]:
    config = config or RuntimeConfig()
    keys: dict[str, Any] = {}
    for section_field in fields(config):
        section = getattr(config, section_field.name)
        for item in fields(section):
            keys[f"{section_field.name}.{item.name}"] = getattr(section, item.name)
    return keys


def apply_overrides(base: RuntimeConfig, overrides: dict[str, Any]) -> tuple[RuntimeConfig, list[str]]:
    """Apply dotted-key overrides. Invalid entries are skipped and reported."""
    errors: list[str] = []
    sections = {f.name: getattr(base, f.name) for f in fields(base)}
    for key, value in overrides.items():
        section_name, _, attr = key.partition(".")
        section = sections.get(section_name)
        if section is None or not attr or attr not in {f.name for f in fields(section)}:
            errors.append(f"{key}: unknown setting")
            continue
        try:
            coerced = _coerce(value, getattr(section, attr))
        except ConfigValueError as exc:
            errors.append(f"{key}: {exc}")
            continue
        sections[section_name] = replace(section, **{attr: coerced})
    config, sanitize_notes = sanitize(RuntimeConfig(**sections))
    return config, errors + sanitize_notes


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def sanitize(config: RuntimeConfig) -> tuple[RuntimeConfig, list[str]]:
    """Force values into ranges Discord (and common sense) can handle."""
    notes: list[str] = []

    categories: list[str] = []
    for raw in config.listings.categories:
        name = str(raw).replace(",", " ").strip()[:50]
        if name and name.lower() != "any" and name not in categories:
            categories.append(name)
    categories = categories[: DISCORD_SELECT_OPTION_LIMIT - 1]  # leave room for "Any"
    if not categories:
        categories = list(ListingConfig().categories)
        notes.append("listings.categories was empty; defaults restored")

    min_options = sorted({_clamp(int(v), 0, 10_000_000) for v in config.listings.minimum_member_options} | {0})
    listings = replace(
        config.listings,
        self_post_timeout_seconds=_clamp(config.listings.self_post_timeout_seconds, 30, 900),
        categories=tuple(categories),
        max_ad_length=_clamp(config.listings.max_ad_length, 50, DISCORD_MESSAGE_LIMIT),
        max_categories=_clamp(config.listings.max_categories, 1, len(categories)),
        max_contacts=_clamp(config.listings.max_contacts, 1, 25),
        refresh_cooldown_minutes=max(0, config.listings.refresh_cooldown_minutes),
        connected_refresh_cooldown_minutes=max(0, config.listings.connected_refresh_cooldown_minutes),
        expiration_days=max(0, config.listings.expiration_days),
        minimum_member_options=tuple(min_options[:DISCORD_SELECT_OPTION_LIMIT]),
        find_page_size=_clamp(config.listings.find_page_size, 1, 4),
    )

    partnerships = replace(
        config.partnerships,
        request_cooldown_seconds=max(0, config.partnerships.request_cooldown_seconds),
        server_request_cooldown_seconds=max(0, config.partnerships.server_request_cooldown_seconds),
        max_pending_requests=max(1, config.partnerships.max_pending_requests),
        max_message_length=_clamp(config.partnerships.max_message_length, 0, 1000),
        decline_cooldown_hours=max(0, config.partnerships.decline_cooldown_hours),
        request_expiration_days=max(0, config.partnerships.request_expiration_days),
        looking_post_cooldown_minutes=max(0, config.partnerships.looking_post_cooldown_minutes),
        looking_panel_debounce_seconds=_clamp(config.partnerships.looking_panel_debounce_seconds, 1, 300),
        pair_cooldown_hours=max(0, config.partnerships.pair_cooldown_hours),
    )

    min_interval = max(HARD_MIN_NETWORK_INTERVAL_MINUTES, config.network.min_interval_minutes)
    interval_options = sorted({max(min_interval, int(v)) for v in config.network.interval_options})
    strategy = config.network.rotation_strategy
    if strategy not in {"least_recent", "random"}:
        notes.append("network.rotation_strategy must be least_recent or random; using least_recent")
        strategy = "least_recent"
    network = replace(
        config.network,
        min_interval_minutes=min_interval,
        default_interval_minutes=max(min_interval, config.network.default_interval_minutes),
        interval_options=tuple(interval_options[:DISCORD_SELECT_OPTION_LIMIT]) or (min_interval,),
        rotation_strategy=strategy,
        repeat_window_hours=max(0, config.network.repeat_window_hours),
        tick_seconds=_clamp(config.network.tick_seconds, 15, 3600),
        send_spacing_seconds=min(max(0.5, config.network.send_spacing_seconds), 30.0),
        max_posts_per_tick=_clamp(config.network.max_posts_per_tick, 1, 200),
        post_retention_days=max(1, config.network.post_retention_days),
    )

    bot = config.bot
    if bot.status not in {"online", "idle", "dnd"}:
        notes.append("bot.status must be online, idle or dnd; using online")
        bot = replace(bot, status="online")

    moderation = replace(
        config.moderation,
        blocked_words=tuple(str(w).strip().lower() for w in config.moderation.blocked_words if str(w).strip()),
        button_rate_limit=max(1, config.moderation.button_rate_limit),
        button_rate_window_seconds=max(1, config.moderation.button_rate_window_seconds),
    )
    system = replace(config.system, maintenance_interval_minutes=_clamp(config.system.maintenance_interval_minutes, 1, 60))

    for url_field in ("support_url", "rules_url", "website_url"):
        value = getattr(bot, url_field).strip()
        if value and not value.lower().startswith(("https://", "http://")):
            notes.append(f"bot.{url_field} must start with https://; ignored")
            value = ""
        bot = replace(bot, **{url_field: value})
    bot = replace(bot, name=(bot.name.strip() or "Parley")[:32], activity_text=bot.activity_text[:128])

    clean_buttons: dict[str, dict[str, str | None]] = {}
    for key, spec in config.panels.buttons.items():
        if key not in DEFAULT_BUTTONS:
            notes.append(f"panels.buttons.{key}: unknown button; ignored")
            continue
        base = dict(DEFAULT_BUTTONS[key])
        label = str(spec.get("label") or base["label"]).strip()[:40] or str(base["label"])
        emoji = spec.get("emoji")
        if emoji and not is_valid_emoji(str(emoji)):
            notes.append(f"panels.buttons.{key}: that isn't a valid Discord button emoji; using the default")
            emoji = base["emoji"]
        style = str(spec.get("style") or base["style"])
        if style not in BUTTON_STYLES:
            notes.append(f"panels.buttons.{key}: unknown button colour; using the default")
            style = str(base["style"])
        clean_buttons[key] = {"label": label, "emoji": emoji or None, "style": style}
    for key, base in DEFAULT_BUTTONS.items():
        clean_buttons.setdefault(key, dict(base))
    panels = replace(
        config.panels,
        buttons=clean_buttons,
        welcome_ping_role_ids=tuple(
            dict.fromkeys(int(role_id) for role_id in config.panels.welcome_ping_role_ids if int(role_id) > 0)
        )[:25],
    )

    if moderation.link_action not in ("review", "block", "allow"):
        notes.append("moderation.link_action must be review, block or allow; using review")
        moderation = replace(moderation, link_action="review")
    moderation = replace(
        moderation,
        max_links=_clamp(moderation.max_links, 0, 50),
        blocked_domains=tuple(dict.fromkeys(d.strip().lower().lstrip("*.") for d in moderation.blocked_domains if d.strip())),
    )

    hub = config.hub
    if hub.mode not in MODES:
        notes.append("hub.mode must be test, live or off; using live")
        hub = replace(hub, mode="live")
    hub = replace(hub, staff_role_ids=tuple(dict.fromkeys(int(r) for r in hub.staff_role_ids if int(r) > 0))[:25])

    messages = replace(
        config.messages,
        **{f.name: str(getattr(config.messages, f.name))[:1900] for f in fields(config.messages)},
    )

    return (
        replace(
            config,
            bot=bot,
            listings=listings,
            partnerships=partnerships,
            network=network,
            panels=panels,
            moderation=moderation,
            system=system,
            hub=hub,
            messages=messages,
        ),
        notes,
    )


def default_config() -> RuntimeConfig:
    return sanitize(RuntimeConfig())[0]
