"""Editable user-facing messages.

Every template lives in the runtime settings (so it can be edited in Discord
under Settings -> Messages) and may only use the placeholders documented here.
Unknown placeholders are rejected when the template is saved, and rendering
never evaluates attribute access, indexing or format specs.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.config.runtime import RuntimeConfig

MAX_TEMPLATE_LENGTH = 1900


@dataclass(frozen=True)
class TemplateSpec:
    key: str
    config_key: str  # dotted runtime-settings key
    label: str
    placeholders: tuple[str, ...]
    description: str


def _spec(key: str, config_key: str, label: str, placeholders: tuple[str, ...], description: str) -> TemplateSpec:
    return TemplateSpec(key, config_key, label, ("bot_name", *placeholders), description)


TEMPLATES: dict[str, TemplateSpec] = {
    spec.key: spec
    for spec in (
        _spec("welcome", "panels.welcome_panel_text", "Welcome panel", (), "Shown in #start-here."),
        _spec("dm_home", "messages.dm_home", "DM home", (), "Top of the DM control panel."),
        _spec("join_message", "messages.join_message", "Bot added to a server", ("server_name",), "Sent once when Parley joins a server."),
        _spec("listings_panel", "panels.listings_panel_text", "Server Directory panel", (), "Panel under the newest directory listing."),
        _spec("looking_panel", "panels.looking_panel_text", "Partner Board panel", (), "Shown in the Partner Board channel."),
        _spec("perks", "panels.perks_panel_text", "How Parley Works panel", (), "Shown in #how-parley-works."),
        _spec("listing_created", "messages.listing_created", "Listing published", ("server_name", "jump_url"), "After a server is connected and its ad is posted."),
        _spec("listing_saved_unpublished", "messages.listing_saved_unpublished", "Listing saved (no channel)", ("server_name",), "When the listings channel isn't ready."),
        _spec("listing_pending", "messages.listing_pending", "Listing waiting for review", ("server_name",), "When approval is required."),
        _spec("listing_approved", "messages.listing_approved", "Listing approved", ("server_name",), "DM to contacts."),
        _spec("listing_rejected", "messages.listing_rejected", "Listing rejected", ("server_name",), "DM to contacts."),
        _spec("edit_pending", "messages.edit_pending", "Edit waiting for review", ("server_name",), "When an edit needs approval."),
        _spec("edit_approved", "messages.edit_approved", "Edit approved", ("server_name",), "DM to contacts."),
        _spec("edit_rejected", "messages.edit_rejected", "Edit rejected", ("server_name",), "DM to contacts."),
        _spec("refresh_success", "messages.refresh_success", "Relist done", ("server_name", "jump_url"), "After Relist."),
        _spec("request_received", "messages.request_received", "Partnership request", ("requester_server", "target_server", "member_count", "category"), "DM to the target's contacts."),
        _spec("request_sent", "messages.request_sent", "Request sent", ("requester_server", "target_server"), "Confirmation to the requester."),
        _spec("request_accepted", "messages.request_accepted", "Partnership accepted", ("requester_server", "target_server"), "DM to both sides; contacts are listed below it."),
        _spec("request_declined", "messages.request_declined", "Partnership declined", ("requester_server", "target_server"), "DM to the requester."),
        _spec("network_footer", "messages.network_footer", "Network ad footer", (), "Line under network ads. Leave empty for none."),
        _spec("maintenance", "messages.maintenance", "Off-mode message", (), "What users see when Parley is OFF."),
        _spec("test_mode", "messages.test_mode", "Test-mode message", (), "What users see while Parley is in TEST mode."),
        _spec("support", "messages.support", "Support message", (), "Shown with the Support button."),
        _spec("network_help", "messages.network_help", "Network explanation", (), "Explains what the Parley Network does before setup."),
        _spec("help", "messages.help", "How it works / help", (), "/help and the How It Works button."),
    )
}


class TemplateError(ValueError):
    pass


def placeholder_names(text: str) -> list[str]:
    """Placeholders used in ``text``. Raises TemplateError for malformed syntax."""
    names = []
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise TemplateError("Curly braces don't match. Use {{ and }} for literal braces.") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name or not field_name.isidentifier():
            raise TemplateError(f"'{{{field_name}}}' isn't a valid placeholder.")
        if format_spec or conversion:
            raise TemplateError(f"Placeholders can't have formatting: {{{field_name}}}")
        names.append(field_name)
    return names


def validate(key: str, text: str) -> str:
    spec = TEMPLATES[key]
    text = text.strip("\n")
    if len(text) > MAX_TEMPLATE_LENGTH:
        raise TemplateError(f"That message is too long ({len(text)} characters, limit {MAX_TEMPLATE_LENGTH}).")
    if not text.strip():
        text = ""
    if not text and key != "network_footer":
        raise TemplateError("The message can't be empty.")
    unknown = [n for n in placeholder_names(text) if n not in spec.placeholders]
    if unknown:
        allowed = ", ".join(f"{{{p}}}" for p in spec.placeholders)
        raise TemplateError(f"Unknown placeholder {{{unknown[0]}}}. You can use: {allowed}")
    return text


def raw(config: RuntimeConfig, key: str) -> str:
    section, _, attr = TEMPLATES[key].config_key.partition(".")
    return str(getattr(getattr(config, section), attr))


def default_text(key: str) -> str:
    from bot.config.runtime import RuntimeConfig

    return raw(RuntimeConfig(), key)


class _Values(dict):
    def __missing__(self, name: str) -> str:  # a placeholder allowed but not supplied
        return ""


def render(config: RuntimeConfig, key: str, **values: Any) -> str:
    """Fill a template. Falls back to the default if a stored template is broken."""
    spec = TEMPLATES[key]
    data = _Values({name: values.get(name, "") for name in spec.placeholders})
    data["bot_name"] = values.get("bot_name", config.bot.name)
    text = raw(config, key)
    try:
        if any(n not in spec.placeholders for n in placeholder_names(text)):
            raise TemplateError("unknown placeholder")
        return text.format_map(data)
    except (TemplateError, ValueError, KeyError):
        return default_text(key).format_map(data)
