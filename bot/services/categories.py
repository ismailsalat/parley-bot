"""Categories, edited from Discord (Settings -> Categories).

Renaming or removing a category also updates existing listings and network
filters, so no listing is left pointing at a category that no longer exists.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import DISCORD_SELECT_OPTION_LIMIT, ListingConfig, RuntimeConfig
from bot.database import repository
from bot.services import configuration
from bot.services.errors import ValidationError

MAX_CATEGORIES = DISCORD_SELECT_OPTION_LIMIT - 1  # one slot is reserved for "Any"
KEY = "listings.categories"


def clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise ValidationError("Category names can't be empty.")
    if len(name) > 50:
        raise ValidationError("Category names can be at most 50 characters.")
    if "," in name:
        raise ValidationError("Category names can't contain commas.")
    if name.casefold() == "any":
        raise ValidationError('"Any" is reserved: it already means every category.')
    return name


def _exists(categories: tuple[str, ...], name: str, *, ignore: str | None = None) -> bool:
    return any(c.casefold() == name.casefold() and c != ignore for c in categories)


async def _save(session: AsyncSession, categories: list[str], actor_id: int | None) -> None:
    await configuration.save(session, {KEY: categories}, actor_id=actor_id)


async def add(session: AsyncSession, config: RuntimeConfig, raw: str, *, actor_id: int | None) -> str:
    name = clean_name(raw)
    current = config.listings.categories
    if _exists(current, name):
        raise ValidationError(f"**{name}** already exists.")
    if len(current) >= MAX_CATEGORIES:
        raise ValidationError(f"Discord menus fit {MAX_CATEGORIES} categories. Remove one first.")
    await _save(session, [*current, name], actor_id)
    return name


def _replace_in_listing(category_field: str, old: str, new: str | None) -> str:
    parts = [c for c in category_field.split(",") if c]
    result: list[str] = []
    for part in parts:
        value = new if part == old else part
        if value and value not in result:
            result.append(value)
    return ",".join(result)


async def _rewrite_usage(session: AsyncSession, old: str, new: str | None) -> int:
    """Point listings and network filters from ``old`` to ``new`` (or drop it)."""
    listings = await repository.listings_using_category(session, old)
    for listing in listings:
        listing.category = _replace_in_listing(listing.category, old, new)
        if listing.pending_changes and "category" in listing.pending_changes:
            pending = dict(listing.pending_changes)
            pending["category"] = _replace_in_listing(pending["category"], old, new)
            listing.pending_changes = pending
    for settings in await repository.all_network_settings(session):
        cats = list(settings.categories or [])
        if old in cats:
            updated = [new if c == old else c for c in cats if new or c != old]
            settings.categories = list(dict.fromkeys(c for c in updated if c))
    return len(listings)


async def rename(session: AsyncSession, config: RuntimeConfig, old: str, raw: str, *, actor_id: int | None) -> str:
    current = config.listings.categories
    if old not in current:
        raise ValidationError(f"**{old}** isn't a category.")
    new = clean_name(raw)
    if new == old:
        return new
    if _exists(current, new, ignore=old):
        raise ValidationError(f"**{new}** already exists.")
    await _save(session, [new if c == old else c for c in current], actor_id)
    await _rewrite_usage(session, old, new)
    return new


async def usage(session: AsyncSession, name: str) -> int:
    return len(await repository.listings_using_category(session, name))


async def remove(
    session: AsyncSession, config: RuntimeConfig, name: str, *, move_to: str | None, actor_id: int | None
) -> int:
    """Remove a category. Listings using it are moved to ``move_to``.
    Returns how many listings were moved."""
    current = config.listings.categories
    if name not in current:
        raise ValidationError(f"**{name}** isn't a category.")
    if len(current) <= 1:
        raise ValidationError("Waypoint needs at least one category.")
    in_use = await usage(session, name)
    if in_use and (move_to is None or move_to == name or move_to not in current):
        raise ValidationError(f"{in_use} listing(s) use **{name}**. Choose where to move them first.")
    await _save(session, [c for c in current if c != name], actor_id)
    await _rewrite_usage(session, name, move_to)
    return in_use


async def move(session: AsyncSession, config: RuntimeConfig, name: str, step: int, *, actor_id: int | None) -> None:
    current = list(config.listings.categories)
    if name not in current:
        raise ValidationError(f"**{name}** isn't a category.")
    index = current.index(name)
    target = max(0, min(len(current) - 1, index + step))
    current.insert(target, current.pop(index))
    await _save(session, current, actor_id)


async def reset_defaults(session: AsyncSession, config: RuntimeConfig, *, actor_id: int | None) -> None:
    defaults = ListingConfig().categories
    blocking = [c for c in config.listings.categories if c not in defaults and await usage(session, c)]
    if blocking:
        raise ValidationError(
            f"Listings still use **{blocking[0]}**, which isn't a default category. Rename or remove it first."
        )
    await repository.delete_runtime_setting(session, KEY)
    await repository.add_audit(session, "settings.reset", actor_id=actor_id, details={"section": "categories"})
