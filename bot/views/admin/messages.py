"""Settings -> Messages (editable texts) and Settings -> Appearance (buttons, links, panels)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.config import templates
from bot.config.runtime import CUSTOMIZABLE_BUTTONS, DEFAULT_BUTTONS
from bot.services import configuration
from bot.utils.helpers import truncate
from bot.views.admin.common import ConfirmPage, Field, FieldsModal, Page, on_off

if TYPE_CHECKING:
    from bot.core import ParleyBot

EXAMPLE_VALUES = {
    "server_name": "Example Café",
    "member_count": "1,234",
    "category": "Community",
    "requester_server": "Example Café",
    "target_server": "Night Owls",
    "jump_url": "https://discord.com/channels/…",
}


def example(bot: ParleyBot, key: str, text: str | None = None) -> str:
    """Render a template (or unsaved text) with example values."""
    spec = templates.TEMPLATES[key]
    values = {name: EXAMPLE_VALUES.get(name, "") for name in spec.placeholders}
    values["bot_name"] = bot.runtime.bot.name
    source = templates.raw(bot.runtime, key) if text is None else text
    try:
        return source.format_map(templates._Values(values))
    except (ValueError, KeyError):
        return source


class MessagesPage(Page):
    title = "Messages"

    def content(self) -> str:
        return (
            "## Messages\n"
            "Choose a message to see it, change it or reset it.\n"
            "-# You can use placeholders like {bot_name} or {server_name}; each message lists the ones it supports."
        )

    def build(self) -> None:
        select = discord.ui.Select(
            placeholder="Choose a message",
            options=[
                discord.SelectOption(label=spec.label[:100], value=key, description=spec.description[:100])
                for key, spec in list(templates.TEMPLATES.items())[:25]
            ],
            row=0,
        )

        async def picked(interaction: discord.Interaction) -> None:
            back = lambda: MessagesPage(self.bot, self.owner_id, back=self.back)  # noqa: E731
            await TemplatePage(self.bot, self.owner_id, select.values[0], back=back).show(interaction)

        select.callback = picked  # type: ignore[method-assign]
        self.add_item(select)
        self.button("Reset All Messages", self._reset, emoji="↩️", style=discord.ButtonStyle.danger, row=1)
        self.nav()

    async def _reset(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.settings import reset_page

        back = lambda: MessagesPage(self.bot, self.owner_id, back=self.back)  # noqa: E731
        await reset_page(self.bot, self.owner_id, "messages", "All messages", back=back).show(interaction)


class TemplatePage(Page):
    def __init__(self, bot: ParleyBot, owner_id: int, key: str, *, back) -> None:
        super().__init__(bot, owner_id, back=back)
        self.key = key
        self.show_preview = False

    def content(self) -> str:
        spec = templates.TEMPLATES[self.key]
        current = templates.raw(self.bot.runtime, self.key) or "*(empty)*"
        is_default = current == templates.default_text(self.key)
        placeholders = " ".join(f"`{{{p}}}`" for p in spec.placeholders)
        lines = [
            f"## {spec.label}",
            f"-# {spec.description} {'(default)' if is_default else '(customized)'}",
            f"**Placeholders:** {placeholders}",
            "**Current message:**",
            "```\n" + truncate(current.replace("```", "ˋˋˋ"), 1200) + "\n```",
        ]
        if self.show_preview:
            lines += ["**Preview:**", truncate(example(self.bot, self.key), 600)]
        return "\n".join(lines)

    def build(self) -> None:
        self.button("Edit", self._edit, emoji="✏️", row=0)
        self.button("Hide Preview" if self.show_preview else "Preview", self._toggle_preview, emoji="🔍", row=0)
        self.button("Reset Default", self._reset, emoji="↩️", style=discord.ButtonStyle.danger, row=0)
        self.nav()

    async def _edit(self, interaction: discord.Interaction) -> None:
        spec = templates.TEMPLATES[self.key]

        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            try:
                async with self.bot.db.session() as session:
                    await configuration.set_template(session, self.key, values["text"], actor_id=inter.user.id)
            except Exception as exc:  # noqa: BLE001 - shown on the page (ParleyError) or handled
                from bot.services.errors import ParleyError

                if isinstance(exc, ParleyError):
                    await self.show(inter, f"⚠️ {exc.user_message}")
                    return
                raise
            await self.bot.settings_changed(refresh_panels=spec.config_key.startswith("panels."))
            self.show_preview = True
            await self.show(inter, "✅ Saved. Here's how it looks:")

        await interaction.response.send_modal(
            FieldsModal(
                f"Edit: {spec.label}",
                [Field("text", "Message", templates.raw(self.bot.runtime, self.key), paragraph=True,
                       max_length=templates.MAX_TEMPLATE_LENGTH, required=self.key != "network_footer")],
                submitted,
            )
        )

    async def _toggle_preview(self, interaction: discord.Interaction) -> None:
        self.show_preview = not self.show_preview
        await self.show(interaction)

    async def _reset(self, interaction: discord.Interaction) -> None:
        async def apply(inter: discord.Interaction) -> None:
            async with self.bot.db.session() as session:
                await configuration.reset_template(session, self.key, actor_id=inter.user.id)
            await self.bot.settings_changed(refresh_panels=True)
            await TemplatePage(self.bot, self.owner_id, self.key, back=self.back).show(inter, "↩️ Back to the default text.")

        await ConfirmPage(
            self.bot, self.owner_id, question=f"Reset **{templates.TEMPLATES[self.key].label}** to the default text?",
            confirm_label="Reset", on_confirm=apply,
            back=lambda: TemplatePage(self.bot, self.owner_id, self.key, back=self.back),
        ).show(interaction)


# ---------------------------------------------------------------- appearance


PANEL_TOGGLES = (
    ("panels.welcome_panel_enabled", "Welcome panel"),
    ("panels.listings_panel_enabled", "Listings panel"),
    ("panels.looking_panel_enabled", "Partner Board panel"),
    ("panels.perks_panel_enabled", "How Parley Works panel"),
    ("panels.benefits_panel_enabled", "Parley Perks panel"),
    ("panels.send_join_message", "Join message"),
)


class AppearancePage(Page):
    """A small menu: each area gets its own focused screen."""

    title = "Appearance"

    def content(self) -> str:
        return "## Appearance\nWhat do you want to change?"

    def build(self) -> None:
        back = lambda: AppearancePage(self.bot, self.owner_id, back=self.back)  # noqa: E731
        self.button("Buttons", self._open(lambda: ButtonsPage(self.bot, self.owner_id, back=back)), row=0)
        self.button("Messages", self._open(lambda: MessagesPage(self.bot, self.owner_id, back=back)), row=0)
        self.button("Links", self._open(lambda: LinksPage(self.bot, self.owner_id, back=back)), row=0)
        self.button("Announcements", self._open(lambda: AnnouncementsPage(self.bot, self.owner_id, back=back)), row=1)
        self.button("Theme", self._open(lambda: ThemePage(self.bot, self.owner_id, back=back)), row=1)
        self.nav()

    def _open(self, factory):
        async def callback(interaction: discord.Interaction) -> None:
            await factory().show(interaction)

        return callback


class ButtonsPage(Page):
    """Label, emoji and colour of every customizable button, paged under Discord's 25-option limit."""

    title = "Buttons"
    PAGE_SIZE = 20

    def __init__(self, bot: ParleyBot, owner_id: int, *, back, page: int = 0, selected: str | None = None) -> None:
        super().__init__(bot, owner_id, back=back)
        self.page = max(0, page)
        self.selected = selected

    @property
    def pages(self) -> int:
        return max(1, (len(CUSTOMIZABLE_BUTTONS) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _page_keys(self) -> tuple[str, ...]:
        self.page = min(self.page, self.pages - 1)
        start = self.page * self.PAGE_SIZE
        return CUSTOMIZABLE_BUTTONS[start : start + self.PAGE_SIZE]

    def content(self) -> str:
        if not self.selected:
            return f"## Buttons\nPick a button to change its label, emoji or colour.\n-# Page {self.page + 1} of {self.pages}"
        label, emoji = self.bot.runtime.button(self.selected)
        style = self.bot.runtime.button_style(self.selected)
        return f"## Buttons\n**{emoji or ''} {label}** · {style}\n-# Page {self.page + 1} of {self.pages}"

    def build(self) -> None:
        cfg = self.bot.runtime
        options = []
        for key in self._page_keys():
            label, emoji = cfg.button(key)
            options.append(
                discord.SelectOption(
                    label=label[:100], value=key, emoji=emoji or None, default=key == self.selected,
                    description=f"Default: {DEFAULT_BUTTONS[key]['label']}"[:100],
                )
            )
        select = discord.ui.Select(placeholder="Choose a button", options=options, row=0)

        async def picked(interaction: discord.Interaction) -> None:
            self.selected = select.values[0]
            await self.show(interaction)

        select.callback = picked  # type: ignore[method-assign]
        self.add_item(select)
        self.button("Edit", self._edit, emoji="✏️", disabled=self.selected is None, row=1)
        self.button("Reset", self._reset, emoji="↩️", disabled=self.selected is None, row=1)
        if self.pages > 1:
            self.button("Previous", self._previous, style=discord.ButtonStyle.secondary, row=2, disabled=self.page <= 0)
            self.button("Next", self._next, style=discord.ButtonStyle.secondary, row=2, disabled=self.page >= self.pages - 1)
        self.nav()

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self.selected = None
        await self.show(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        self.selected = None
        await self.show(interaction)

    async def _saved(self, interaction: discord.Interaction, notice: str) -> None:
        await self.bot.settings_changed(refresh_panels=True)
        await self.show(interaction, notice)

    async def _edit(self, interaction: discord.Interaction) -> None:
        key = self.selected
        assert key is not None
        label, emoji = self.bot.runtime.button(key)
        style = self.bot.runtime.button_style(key)

        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            from bot.services.errors import ParleyError

            await inter.response.defer()
            try:
                async with self.bot.db.session() as session:
                    await configuration.set_button(
                        session, key, label=values["label"], emoji=values["emoji"],
                        style=values["style"] or None, actor_id=inter.user.id,
                    )
            except ParleyError as exc:
                await self.show(inter, f"⚠️ {exc.user_message}")
                return
            await self._saved(inter, "✅ Button updated.")

        await interaction.response.send_modal(
            FieldsModal("Edit button", [
                Field("label", "Label", label, required=True, max_length=40),
                Field("emoji", "Emoji (optional)", emoji or "", placeholder="📢 or <:name:123456789012345678>", max_length=64),
                Field("style", "Colour", style, placeholder="primary, success, danger or secondary", max_length=12),
            ], submitted)
        )

    async def _reset(self, interaction: discord.Interaction) -> None:
        assert self.selected is not None
        await interaction.response.defer()
        async with self.bot.db.session() as session:
            await configuration.reset_button(session, self.selected, actor_id=interaction.user.id)
        await self._saved(interaction, "↩️ Button reset to default.")


class AnnouncementsPage(Page):
    """Control the intentional Start Here announcement ping."""

    title = "Announcements"

    def content(self) -> str:
        cfg = self.bot.runtime.panels
        roles = ", ".join(f"<@&{role_id}>" for role_id in cfg.welcome_ping_role_ids) or "None"
        return (
            "## Announcements\n"
            "The Start Here panel can notify members **once when it is first created**. "
            "Repairs and restarts do not intentionally ping everyone again.\n\n"
            f"**@everyone:** {on_off(cfg.welcome_ping_everyone)}\n"
            f"**Ping roles:** {roles}\n"
            "-# Panel text is edited under Appearance → Messages. Button labels/colours are edited under Appearance → Buttons."
        )

    def build(self) -> None:
        cfg = self.bot.runtime.panels
        self.button(
            f"@everyone: {on_off(cfg.welcome_ping_everyone)}",
            self._toggle_everyone,
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.button("Edit Ping Roles", self._edit_roles, emoji="🏷️", row=0)
        self.nav()

    async def _save(self, interaction: discord.Interaction, changes: dict) -> None:
        await interaction.response.defer()
        async with self.bot.db.session() as session:
            await configuration.save(session, changes, actor_id=interaction.user.id)
        await self.bot.settings_changed(refresh_panels=True)
        await self.show(interaction, "✅ Announcement settings saved.")

    async def _toggle_everyone(self, interaction: discord.Interaction) -> None:
        await self._save(
            interaction,
            {"panels.welcome_ping_everyone": not self.bot.runtime.panels.welcome_ping_everyone},
        )

    async def _edit_roles(self, interaction: discord.Interaction) -> None:
        current = ", ".join(str(role_id) for role_id in self.bot.runtime.panels.welcome_ping_role_ids)

        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            raw = values["roles"].replace("<@&", "").replace(">", "")
            parts = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
            if any(not part.isdigit() for part in parts):
                await self.show(inter, "⚠️ Use Discord role IDs separated by commas.")
                return
            ids = tuple(dict.fromkeys(int(part) for part in parts if int(part) > 0))[:25]
            await self._save(inter, {"panels.welcome_ping_role_ids": ids})

        await interaction.response.send_modal(
            FieldsModal(
                "Start Here ping roles",
                [
                    Field(
                        "roles",
                        "Role IDs (comma separated)",
                        current,
                        placeholder="1552864166115549224",
                        max_length=500,
                    )
                ],
                submitted,
            )
        )


class LinksPage(Page):
    title = "Links"

    def content(self) -> str:
        cfg = self.bot.runtime.bot
        return (
            "## Links\n"
            f"**Support:** {cfg.support_url or '—'}\n"
            f"**Rules:** {cfg.rules_url or '—'}\n"
            f"**Website:** {cfg.website_url or '—'}\n"
            "-# Empty links hide their button."
        )

    def build(self) -> None:
        self.button("Edit Links", self._edit, emoji="🔗", row=0)
        self.nav()

    async def _edit(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.runtime.bot

        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            from bot.services.errors import ParleyError

            await inter.response.defer()
            try:
                async with self.bot.db.session() as session:
                    await configuration.set_links(
                        session, support=values["support"], rules=values["rules"], website=values["website"],
                        actor_id=inter.user.id,
                    )
            except ParleyError as exc:
                await self.show(inter, f"⚠️ {exc.user_message}")
                return
            await self.bot.settings_changed(refresh_panels=True)
            await self.show(inter, "✅ Links saved.")

        await interaction.response.send_modal(
            FieldsModal("Links", [
                Field("support", "Support link", cfg.support_url, placeholder="https://discord.gg/yoursupport", max_length=500),
                Field("rules", "Rules link (optional)", cfg.rules_url, placeholder="https://…", max_length=500),
                Field("website", "Website (optional)", cfg.website_url, placeholder="https://…", max_length=500),
            ], submitted)
        )


class ThemePage(Page):
    """Bot name, status text and which panels are shown."""

    title = "Theme"

    def content(self) -> str:
        cfg = self.bot.runtime
        return f"## Theme\n**Name:** {cfg.bot.name}\n**Status:** {cfg.bot.activity_text or '—'}"

    def build(self) -> None:
        cfg = self.bot.runtime
        self.button("Name & Status", self._name, emoji="🏷️", row=0)
        for key, label in PANEL_TOGGLES:
            section, _, attr = key.partition(".")
            value = getattr(getattr(cfg, section), attr)
            self.button(f"{label}: {on_off(value)}", self._toggle(key, not value), row=1)
        self.button("Reset Appearance", self._reset_all, emoji="↩️", style=discord.ButtonStyle.danger, row=2)
        self.nav()

    def _toggle(self, key: str, value: bool):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            async with self.bot.db.session() as session:
                await configuration.save(session, {key: value}, actor_id=interaction.user.id)
            await self.bot.settings_changed(refresh_panels=True)
            await self.show(interaction, "✅ Saved.")

        return callback

    async def _name(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.runtime.bot

        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            await inter.response.defer()
            async with self.bot.db.session() as session:
                await configuration.save(
                    session, {"bot.name": values["name"].strip() or "Parley", "bot.activity_text": values["status"].strip()},
                    actor_id=inter.user.id,
                )
            await self.bot.settings_changed(refresh_panels=True)
            await self.show(inter, "✅ Saved. Messages now use this name.")

        await interaction.response.send_modal(
            FieldsModal("Name & status", [
                Field("name", "Name used in messages", cfg.name, required=True, max_length=32),
                Field("status", "Status text", cfg.activity_text, max_length=128),
            ], submitted)
        )

    async def _reset_all(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.settings import reset_page

        back = lambda: ThemePage(self.bot, self.owner_id, back=self.back)  # noqa: E731
        await reset_page(self.bot, self.owner_id, "appearance", "Appearance", back=back).show(interaction)
