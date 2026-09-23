"""Admin Health Check: everything important, readable without server logs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot.database import migrations, repository
from bot.services import permissions
from bot.services.panels import LISTINGS_PANEL, LOOKING_PANEL, WELCOME_PANEL
from bot.services.setup import SLOTS

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"
ICONS = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
PANEL_LABELS = {WELCOME_PANEL: "Welcome panel", LISTINGS_PANEL: "Listings panel", LOOKING_PANEL: "Looking panel"}
CHANNEL_CHECK_NAMES = {
    "welcome_channel_id": "Welcome channel",
    "listings_channel_id": "Listings channel",
    "looking_channel_id": "Looking for partners channel",
    "support_channel_id": "Support channel",
    "log_channel_id": "Staff logs channel",
}


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str = ""
    fix: str | None = None  # "setup" | "channels" | "repair" | "mode" | "staff"


def summarize(checks: list[Check]) -> str:
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    if fails:
        return f"❌ {fails} problem(s) need attention"
    if warns:
        return f"⚠️ Working, with {warns} warning(s)"
    return "✅ Everything looks good"


GROUPS = {
    "Discord": ("Discord", "Main server", "Persistent buttons", "Background tasks"),
    "Database": ("SQLite", "PostgreSQL", "Database schema", "Settings"),
    "Listings": ("Listings channel", "Welcome channel", "Looking for partners channel", "Approvals"),
    "Panels": ("Welcome panel", "Listings panel", "Looking panel"),
    "Permissions": ("Create Invite permission", "Staff logs channel", "Support channel", "Paste My Own Ad"),
    "Network": ("Network scheduler", "Mode"),
}


def _worst(statuses: list[str]) -> str:
    return FAIL if FAIL in statuses else (WARN if WARN in statuses else OK)


def render(checks: list[Check]) -> str:
    """Short, readable summary. One line per area, then what to do about problems."""
    by_name = {c.name: c for c in checks}
    lines = ["## Parley Health"]
    covered: set[str] = set()
    for group, names in GROUPS.items():
        items = [by_name[n] for n in names if n in by_name]
        covered.update(n for n in names if n in by_name)
        if not items:
            continue
        lines.append(f"{ICONS[_worst([i.status for i in items])]} {group}")
    rest = [c for c in checks if c.name not in covered]
    for check in rest:
        lines.append(f"{ICONS[check.status]} {check.name}")
    problems = [c for c in checks if c.status != OK]
    if problems:
        lines.append("")
        for check in problems[:5]:
            lines.append(f"{ICONS[check.status]} {check.name}: {check.detail or 'needs attention'}")
    return "\n".join(lines)[:1990]


def render_details(checks: list[Check]) -> str:
    """Every check, for when an admin presses Details."""
    lines = ["## Parley Health · details", summarize(checks), ""]
    for check in checks:
        line = f"{ICONS[check.status]} **{check.name}**"
        if check.detail:
            line += f" — {check.detail}"
        lines.append(line)
    return "\n".join(lines)[:1990]


async def _database_checks(bot: ParleyBot) -> list[Check]:
    backend = "SQLite" if bot.settings.uses_sqlite else "PostgreSQL"
    try:
        await bot.db.ping()
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        log.warning("health.database_failed: %s", exc)
        return [Check(FAIL, backend, "can't connect to the database")]
    checks = [Check(OK, backend)]
    try:
        current = await migrations.current_revision(bot.db.engine)
        head = migrations.head_revision(bot.settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("health.schema_check_failed: %s", exc)
        checks.append(Check(WARN, "Database schema", "couldn't read the migration version"))
    else:
        checks.append(
            Check(OK, "Database schema", f"version {current}")
            if current == head
            else Check(FAIL, "Database schema", f"at {current}, needs {head} — restart Parley to update")
        )
    return checks


def _channel_checks(bot: ParleyBot, guild: discord.Guild) -> list[Check]:
    checks: list[Check] = []
    hub = bot.runtime.hub
    for slot in SLOTS:
        channel_id = getattr(hub, slot.key)
        severity = FAIL if slot.required else WARN
        check_name = CHANNEL_CHECK_NAMES.get(slot.key, f"{slot.label} channel")
        if not channel_id:
            if slot.required:
                checks.append(Check(FAIL, check_name, "not configured", "channels"))
            else:
                checks.append(Check(WARN, check_name, "optional · not configured"))
            continue
        channel = guild.get_channel(channel_id)
        if getattr(channel, "type", None) not in (discord.ChannelType.text, discord.ChannelType.news):
            checks.append(Check(severity, check_name, "was deleted — choose a new one", "channels"))
            continue
        missing = [label for label, ok in permissions.channel_permission_report(channel, guild.me) if not ok]
        if missing:
            checks.append(
                Check(severity, check_name, f"missing {', '.join(missing)} in #{channel.name}", "permissions")
            )
        else:
            checks.append(Check(OK, check_name, f"#{channel.name}"))
    invite_ok = guild.me.guild_permissions.create_instant_invite
    checks.append(
        Check(OK, "Create Invite permission")
        if invite_ok
        else Check(WARN, "Create Invite permission", "missing: Test Invite and main-server invites won't work")
    )
    return checks


async def _panel_checks(bot: ParleyBot) -> list[Check]:
    checks = []
    for panel_type, label in PANEL_LABELS.items():
        state = await bot.panels.panel_status(panel_type)
        if state in ("ok", "disabled", "no_channel"):
            if state == "ok":
                checks.append(Check(OK, label))
            elif state == "disabled":
                checks.append(Check(OK, label, "turned off in Settings"))
            continue
        checks.append(Check(WARN, label, {"missing": "deleted", "not_posted": "not posted yet"}.get(state, state), "repair"))
    return checks


async def run(bot: ParleyBot) -> list[Check]:
    """Run every check. Never raises: problems become failed checks."""
    from bot.core import persistent_items

    checks: list[Check] = []
    if bot.is_ready():
        latency = bot.latency * 1000 if bot.latency == bot.latency else 0  # NaN-safe
        checks.append(Check(OK, "Discord", f"{latency:.0f} ms"))
    else:
        checks.append(Check(FAIL, "Discord", "not connected"))
    checks.extend(await _database_checks(bot))

    hub = bot.runtime.hub
    mode_icon = {"live": OK, "test": WARN, "off": WARN}[hub.mode]
    checks.append(Check(mode_icon, "Mode", {"live": "🟢 LIVE", "test": "🧪 TEST", "off": "🔴 OFF"}[hub.mode], "mode"))

    guild = bot.get_guild(hub.main_guild_id) if hub.main_guild_id else None
    if not hub.main_guild_id:
        checks.append(Check(FAIL, "Main server", "not set up — run /setup in your main server", "setup"))
    elif guild is None:
        checks.append(Check(FAIL, "Main server", "Parley isn't in the main server anymore", "setup"))
    else:
        checks.append(Check(OK, "Main server", guild.name))
        checks.extend(_channel_checks(bot, guild))
        checks.extend(await _panel_checks(bot))

    registered = set(bot._connection._view_store._dynamic_items.values())
    expected = set(persistent_items())
    checks.append(
        Check(OK, "Persistent buttons", f"{len(expected)} types")
        if expected <= registered
        else Check(FAIL, "Persistent buttons", "some old buttons won't respond — restart Parley")
    )

    status = bot.background.status()
    if not status["running"]:
        checks.append(Check(FAIL, "Background tasks", "stopped — restart Parley"))
    else:
        checks.append(Check(OK, "Background tasks"))
    net = bot.runtime.network
    async with bot.db.session() as session:
        destinations = len(await repository.enabled_network_settings(session))
        waiting = len(await repository.listings_awaiting_review(session))
    if hub.mode != "live":
        checks.append(Check(WARN, "Network scheduler", f"paused while {hub.mode.upper()} · {destinations} server(s) opted in"))
    elif not net.enabled:
        checks.append(Check(WARN, "Network scheduler", "network ads are switched off in Settings"))
    else:
        checks.append(Check(OK, "Network scheduler", f"{destinations} server(s) receiving ads"))

    from bot.views import self_post

    if bot.runtime.listings.allow_self_post and hub.main_guild_id:
        state = self_post.availability(bot, hub.main_guild_id)
        checks.append(
            Check(OK, "Paste My Own Ad", "owners can post their own ads")
            if state.ok
            else Check(WARN, "Paste My Own Ad", state.reason, "permissions")
        )

    if bot.runtime.listings.approval_required and not hub.log_channel_id:
        checks.append(Check(FAIL, "Approvals", "approval is on but there's no staff log channel", "channels"))
    elif waiting:
        checks.append(Check(WARN, "Approvals", f"{waiting} waiting for review"))

    if bot.runtime_notes:
        checks.append(Check(WARN, "Settings", f"{len(bot.runtime_notes)} stored value(s) were ignored: {bot.runtime_notes[0]}"))
    else:
        checks.append(Check(OK, "Settings", "loaded"))
    if bot.hub_env_fields:
        checks.append(Check(WARN, "Old .env configuration", "save it to the database in Settings → Channels", "channels"))
    return checks
