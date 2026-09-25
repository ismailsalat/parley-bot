"""Settings -> Listings / Partnerships / Network: friendly numbers and switches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot.services import configuration
from bot.services.errors import ValidationError, ParleyError
from bot.views.admin.common import Field, FieldsModal, Page, on_off

if TYPE_CHECKING:
    from bot.core import ParleyBot


@dataclass(frozen=True)
class Number:
    key: str
    label: str
    unit: str
    low: int
    high: int


@dataclass(frozen=True)
class Switch:
    key: str
    label: str


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    options: tuple[tuple[str, str], ...]  # (value, label)


SECTIONS: dict[str, tuple[str, str, tuple, str]] = {
    "listings": (
        "Listings",
        "📢",
        (
            Number("listings.refresh_cooldown_minutes", "Relist cooldown", "minutes", 0, 10_080),
            Number("listings.max_ad_length", "Ad character limit", "characters", 50, 2000),
            Number("listings.max_contacts", "Maximum contacts", "people", 1, 25),
            Number("listings.expiration_days", "Listing expiration (0 = never)", "days", 0, 365),
            Switch("listings.invite_required", "Invite required"),
            Switch("listings.approval_required", "Approval required"),
            Switch("listings.reapprove_ad_edits", "Re-approve ad edits"),
            Switch("listings.reapprove_info_edits", "Re-approve info edits"),
        ),
        "listings",
    ),
    "partnerships": (
        "Partnerships",
        "🤝",
        (
            Number("partnerships.request_cooldown_seconds", "User request cooldown", "seconds", 0, 86_400),
            Number("partnerships.server_request_cooldown_seconds", "Server request cooldown", "seconds", 0, 86_400),
            Number("partnerships.decline_cooldown_hours", "Decline cooldown", "hours", 0, 720),
            Number("partnerships.max_pending_requests", "Maximum pending", "requests", 1, 100),
            Number("partnerships.request_expiration_days", "Request expiration (0 = never)", "days", 0, 90),
            Number("partnerships.max_message_length", "Request message limit", "characters", 0, 1000),
            Switch("partnerships.enforce_minimum_members", "Minimum-member check"),
        ),
        "partnerships",
    ),
    "network": (
        "Network",
        "🌐",
        (
            Switch("network.enabled", "Network ads"),
            Number("network.min_interval_minutes", "Minimum interval", "minutes", 15, 10_080),
            Number("network.default_interval_minutes", "Default interval", "minutes", 15, 10_080),
            Number("network.repeat_window_hours", "Don't repeat an ad within", "hours", 0, 720),
            Choice("network.rotation_strategy", "Rotation", (("least_recent", "Fair (least recent)"), ("random", "Random"))),
        ),
        "network",
    ),
}


def current(bot: ParleyBot, key: str):
    section, _, attr = key.partition(".")
    return getattr(getattr(bot.runtime, section), attr)


def parse_number(spec: Number, raw: str) -> int:
    text = raw.strip().replace(",", "")
    if not text.lstrip("-").isdigit():
        raise ValidationError(f"{spec.label} must be a whole number.")
    value = int(text)
    if not spec.low <= value <= spec.high:
        raise ValidationError(f"{spec.label} must be between {spec.low:,} and {spec.high:,} {spec.unit}.")
    return value


class RulesPage(Page):
    def __init__(self, bot: ParleyBot, owner_id: int, section: str, *, back) -> None:
        super().__init__(bot, owner_id, back=back)
        self.section = section
        self.title, self.emoji, self.specs, self.reset_key = SECTIONS[section]

    def content(self) -> str:
        lines = [f"## {self.emoji} {self.title}"]
        for spec in self.specs:
            value = current(self.bot, spec.key)
            if isinstance(spec, Number):
                lines.append(f"**{spec.label}:** {value:,} {spec.unit}")
            elif isinstance(spec, Switch):
                lines.append(f"**{spec.label}:** {on_off(value)}")
            else:
                lines.append(f"**{spec.label}:** {dict(spec.options).get(value, value)}")
        if self.section == "listings" and current(self.bot, "listings.approval_required"):
            lines.append("-# With approval on, edits that need re-approval keep the old ad live until staff approve.")
        return "\n".join(lines)

    def build(self) -> None:
        numbers = [s for s in self.specs if isinstance(s, Number)]
        if numbers:
            self.button("Edit Numbers", self._edit_numbers, emoji="✏️", row=0)
        row, count = 1, 0
        for spec in self.specs:
            if isinstance(spec, Switch):
                value = bool(current(self.bot, spec.key))
                # The label carries the state, so the colour stays calm and consistent.
                self.button(f"{spec.label}: {on_off(value)}", self._setter(spec.key, not value), row=row)
            elif isinstance(spec, Choice):
                values = [v for v, _ in spec.options]
                now = current(self.bot, spec.key)
                following = values[(values.index(now) + 1) % len(values)] if now in values else values[0]
                self.button(f"{spec.label}: {dict(spec.options).get(now, now)}", self._setter(spec.key, following), row=row)
            else:
                continue
            count += 1
            if count % 4 == 0:
                row += 1
        self.button("Reset to Defaults", self._reset, emoji="↩️", style=discord.ButtonStyle.danger, row=3)
        self.nav()

    def _setter(self, key: str, value):
        async def callback(interaction: discord.Interaction) -> None:
            async with self.bot.db.session() as session:
                await configuration.save(session, {key: value}, actor_id=interaction.user.id)
            await self.bot.settings_changed()
            await self.show(interaction, "✅ Saved.")

        return callback

    async def _edit_numbers(self, interaction: discord.Interaction) -> None:
        numbers = [s for s in self.specs if isinstance(s, Number)][:5]

        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            try:
                changes = {spec.key: parse_number(spec, values[spec.key]) for spec in numbers}
                async with self.bot.db.session() as session:
                    await configuration.save(session, changes, actor_id=inter.user.id)
            except ParleyError as exc:
                await self.show(inter, f"⚠️ {exc.user_message}")
                return
            await self.bot.settings_changed()
            await self.show(inter, "✅ Saved. Changes apply right away.")

        await interaction.response.send_modal(
            FieldsModal(
                f"{self.title} numbers",
                [Field(s.key, f"{s.label} ({s.unit})"[:45], str(current(self.bot, s.key)), required=True, max_length=7)
                 for s in numbers],
                submitted,
            )
        )

    async def _reset(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.settings import reset_page

        back = lambda: RulesPage(self.bot, self.owner_id, self.section, back=self.back)  # noqa: E731
        await reset_page(self.bot, self.owner_id, self.reset_key, self.title, back=back).show(interaction)
