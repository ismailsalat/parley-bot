"""Changing Waypoint's settings from Discord.

All ordinary configuration is stored in the ``runtime_settings`` table (dotted
keys, see bot.config.runtime). This module validates changes before they are
written, so the Settings UI, the import feature and the CLI share one set of
rules. Secrets (token, database URL) are never stored here.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import fields, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import templates
from bot.config.runtime import (
    BUTTON_STYLES,
    CUSTOMIZABLE_BUTTONS,
    DEFAULT_BUTTONS,
    MODES,
    HubConfig,
    RuntimeConfig,
    apply_overrides,
    default_config,
    known_keys,
)
from bot.config.settings import Settings
from bot.database import repository
from bot.services.errors import ValidationError
from bot.utils.emoji import is_valid_emoji

log = logging.getLogger(__name__)

EXPORT_VERSION = 1

# Sections that have a "Reset to Defaults" button. Channels/hub are deliberately
# not resettable in one click: that would disconnect the whole network.
RESETTABLE_SECTIONS: dict[str, tuple[str, ...]] = {
    "listings": ("listings.refresh_cooldown_minutes", "listings.max_ad_length", "listings.max_contacts",
                 "listings.expiration_days", "listings.invite_required", "listings.auto_create_invite",
                 "listings.approval_required", "listings.reapprove_ad_edits", "listings.reapprove_info_edits",
                 "listings.max_categories", "listings.minimum_member_options", "listings.find_page_size"),
    "partnerships": ("partnerships.",),
    "network": ("network.",),
    "messages": ("messages.", "panels.welcome_panel_text", "panels.listings_panel_text", "panels.looking_panel_text"),
    "appearance": ("panels.buttons", "bot.", "panels.listings_panel_enabled", "panels.looking_panel_enabled",
                   "panels.welcome_panel_enabled", "panels.send_join_message"),
    "moderation": ("moderation.",),
}

HUB_CHANNEL_FIELDS = (
    "welcome_channel_id", "listings_channel_id", "looking_channel_id", "support_channel_id", "log_channel_id"
)
_ENV_HUB_FIELDS = ("main_guild_id", *HUB_CHANNEL_FIELDS)



# ---------------------------------------------------------------- legacy .env hub values


def merge_env_hub(hub: HubConfig, settings: Settings) -> tuple[HubConfig, tuple[str, ...]]:
    """Use old MAIN_GUILD_ID/..._CHANNEL_ID/STAFF_ROLE_IDS env values where the
    database has nothing yet, so older deployments keep working unchanged.
    Returns the merged hub and the names of fields taken from the environment."""
    used: list[str] = []
    changes: dict[str, Any] = {}
    for name in _ENV_HUB_FIELDS:
        env_value = getattr(settings, name, None)
        if not getattr(hub, name) and env_value:
            changes[name] = env_value
            used.append(name)
    if not hub.staff_role_ids and settings.staff_role_ids:
        changes["staff_role_ids"] = tuple(settings.staff_role_ids)
        used.append("staff_role_ids")
    return (replace(hub, **changes) if changes else hub), tuple(used)


def env_hub_changes(hub: HubConfig, env_fields: tuple[str, ...]) -> dict[str, Any]:
    """The settings needed to save environment-provided hub values into the database."""
    changes: dict[str, Any] = {}
    for name in env_fields:
        value = getattr(hub, name)
        changes[f"hub.{name}"] = list(value) if isinstance(value, tuple) else value
    if changes:
        changes["hub.setup_completed"] = True
    return changes


# ---------------------------------------------------------------- generic writes


def _first_error(notes: list[str], keys: set[str]) -> str | None:
    for note in notes:
        key = note.split(":", 1)[0]
        if key in keys:
            return note.split(":", 1)[1].strip() if ":" in note else note
    return None


async def save(session: AsyncSession, changes: dict[str, Any], *, actor_id: int | None) -> RuntimeConfig:
    """Validate and store setting changes. Returns the resulting configuration.

    Values that are invalid raise ValidationError and nothing is written.
    """
    valid_keys = known_keys()
    for key in changes:
        if key not in valid_keys:
            raise ValidationError(f"Unknown setting: {key}")
    overrides = await repository.runtime_overrides(session)
    merged = {**overrides, **changes}
    config, notes = apply_overrides(default_config(), merged)
    problem = _first_error(notes, set(changes))
    if problem:
        raise ValidationError(f"That value isn't valid ({problem}).")
    for key, value in changes.items():
        await repository.set_runtime_setting(session, key, _jsonable(value), updated_by=actor_id)
    await repository.add_audit(session, "settings.changed", actor_id=actor_id, details={"keys": sorted(changes)})
    log.info("settings.changed keys=%s actor_id=%s", ",".join(sorted(changes)), actor_id)
    return config


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


async def reset_section(session: AsyncSession, section: str, *, actor_id: int | None) -> int:
    """Delete the overrides of one section. Returns how many were removed."""
    prefixes = RESETTABLE_SECTIONS.get(section)
    if prefixes is None:
        raise ValidationError("That section can't be reset.")
    removed = 0
    for key in list(await repository.runtime_overrides(session)):
        if any(key == p or (p.endswith(".") and key.startswith(p)) for p in prefixes):
            removed += int(await repository.delete_runtime_setting(session, key))
    await repository.add_audit(session, "settings.reset", actor_id=actor_id, details={"section": section})
    log.info("settings.reset section=%s removed=%s actor_id=%s", section, removed, actor_id)
    return removed


# ---------------------------------------------------------------- mode


async def set_mode(session: AsyncSession, mode: str, *, actor_id: int | None) -> None:
    if mode not in MODES:
        raise ValidationError("Mode must be test, live or off.")
    await save(session, {"hub.mode": mode}, actor_id=actor_id)


# ---------------------------------------------------------------- messages


async def set_template(session: AsyncSession, key: str, text: str, *, actor_id: int | None) -> str:
    if key not in templates.TEMPLATES:
        raise ValidationError("Unknown message.")
    try:
        cleaned = templates.validate(key, text)
    except templates.TemplateError as exc:
        raise ValidationError(str(exc)) from exc
    await save(session, {templates.TEMPLATES[key].config_key: cleaned}, actor_id=actor_id)
    return cleaned


async def reset_template(session: AsyncSession, key: str, *, actor_id: int | None) -> None:
    await repository.delete_runtime_setting(session, templates.TEMPLATES[key].config_key)
    await repository.add_audit(session, "settings.reset", actor_id=actor_id, details={"template": key})


# ---------------------------------------------------------------- buttons & links


def clean_emoji(raw: str) -> str:
    """One Discord emoji, or empty for no emoji. Rejects text symbols like "←"."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not is_valid_emoji(raw):
        raise ValidationError(
            "That isn't a valid Discord button emoji. Use one emoji like 📢, or a custom emoji like <:name:123456789012345678>."
        )
    return raw


async def set_button(
    session: AsyncSession, key: str, *, label: str, emoji: str, style: str | None = None, actor_id: int | None
) -> None:
    if key not in CUSTOMIZABLE_BUTTONS:
        raise ValidationError("That button can't be changed.")
    label = " ".join((label or "").split())
    if not 1 <= len(label) <= 40:
        raise ValidationError("Button labels must be 1–40 characters (short labels read best on mobile).")
    chosen = (style or DEFAULT_BUTTONS[key].get("style") or "primary").lower()
    if chosen not in BUTTON_STYLES:
        raise ValidationError("Choose one of: primary, success, danger, secondary.")
    overrides = await repository.runtime_overrides(session)
    stored = dict(overrides.get("panels.buttons") or {})
    stored[key] = {"label": label, "emoji": clean_emoji(emoji), "style": chosen}
    await save(session, {"panels.buttons": stored}, actor_id=actor_id)


async def reset_button(session: AsyncSession, key: str, *, actor_id: int | None) -> None:
    overrides = await repository.runtime_overrides(session)
    stored = dict(overrides.get("panels.buttons") or {})
    stored.pop(key, None)
    if stored:
        await save(session, {"panels.buttons": stored}, actor_id=actor_id)
    else:
        await repository.delete_runtime_setting(session, "panels.buttons")


def clean_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.fullmatch(r"https?://[^\s<>\"]{3,500}", raw):
        raise ValidationError("Links must start with https:// (for example https://discord.gg/yourserver).")
    return raw


async def set_links(
    session: AsyncSession, *, support: str, rules: str, website: str, actor_id: int | None
) -> None:
    await save(
        session,
        {"bot.support_url": clean_url(support), "bot.rules_url": clean_url(rules), "bot.website_url": clean_url(website)},
        actor_id=actor_id,
    )


# ---------------------------------------------------------------- export / import


async def export_settings(session: AsyncSession, config: RuntimeConfig) -> str:
    """Non-secret settings as JSON. Never includes the token or database URL."""
    payload = {
        "waypoint_settings_version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overrides": dict(sorted((await repository.runtime_overrides(session)).items())),
        "hub": {f.name: _jsonable(getattr(config.hub, f.name)) for f in fields(config.hub)},
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def parse_import(raw: str, *, include_hub: bool = False) -> dict[str, Any]:
    """Validate an exported settings file. Hub/channel IDs are skipped by default
    because they only make sense in the server they came from."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("That file isn't valid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("waypoint_settings_version") != EXPORT_VERSION:
        raise ValidationError("That isn't a Waypoint settings export.")
    overrides = payload.get("overrides")
    if not isinstance(overrides, dict):
        raise ValidationError("The export has no settings in it.")
    valid = known_keys()
    unknown = [k for k in overrides if k not in valid]
    if unknown:
        raise ValidationError(f"Unknown setting in file: {unknown[0]}")
    chosen = {k: v for k, v in overrides.items() if include_hub or not k.startswith("hub.")}
    _config, notes = apply_overrides(default_config(), chosen)
    problem = _first_error(notes, set(chosen))
    if problem:
        raise ValidationError(f"The file contains an invalid value ({problem}).")
    for key in ("messages.", "panels.welcome_panel_text", "panels.listings_panel_text", "panels.looking_panel_text"):
        for tkey, spec in templates.TEMPLATES.items():
            if spec.config_key in chosen and spec.config_key.startswith(key):
                try:
                    templates.validate(tkey, str(chosen[spec.config_key]))
                except templates.TemplateError as exc:
                    raise ValidationError(f"{spec.label}: {exc}") from exc
    return chosen


async def import_settings(session: AsyncSession, chosen: dict[str, Any], *, actor_id: int | None) -> int:
    if chosen:
        await save(session, chosen, actor_id=actor_id)
    await repository.add_audit(session, "settings.imported", actor_id=actor_id, details={"count": len(chosen)})
    return len(chosen)


def button_defaults(key: str) -> dict[str, str]:
    return dict(DEFAULT_BUTTONS[key])
