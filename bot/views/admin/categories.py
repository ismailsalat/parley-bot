"""Settings -> Categories: add, rename, remove, reorder, reset."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.services import categories as category_service
from bot.services.errors import WaypointError
from bot.views.admin.common import ConfirmPage, Field, FieldsModal, Page

if TYPE_CHECKING:
    from bot.core import WaypointBot


class CategoriesPage(Page):
    title = "Categories"

    def __init__(self, bot: WaypointBot, owner_id: int, *, back, selected: str | None = None) -> None:
        super().__init__(bot, owner_id, back=back)
        self.selected = selected

    def _again(self, selected: str | None = None) -> CategoriesPage:
        return CategoriesPage(self.bot, self.owner_id, back=self.back, selected=selected)

    def content(self) -> str:
        names = self.bot.runtime.listings.categories
        lines = ["## Categories"]
        lines += [f"{'▶️' if n == self.selected else '•'} {n}" for n in names]
        lines.append(f"-# {len(names)}/{category_service.MAX_CATEGORIES} · \"Any\" is always added for searching.")
        return "\n".join(lines)

    def build(self) -> None:
        names = self.bot.runtime.listings.categories
        select = discord.ui.Select(
            placeholder="Choose a category to rename, remove or move",
            options=[discord.SelectOption(label=n, value=n, default=n == self.selected) for n in names],
            row=0,
        )

        async def picked(interaction: discord.Interaction) -> None:
            self.selected = select.values[0]
            await self.show(interaction)

        select.callback = picked  # type: ignore[method-assign]
        self.add_item(select)
        none = self.selected is None
        self.button("Add Category", self._add, emoji="➕", row=1)
        self.button("Rename", self._rename, emoji="✏️", disabled=none, row=1)
        self.button("Move Up", self._move(-1), emoji="⬆️", disabled=none, row=1)
        self.button("Move Down", self._move(1), emoji="⬇️", disabled=none, row=1)
        self.button("Remove", self._remove, emoji="🗑️", disabled=none, style=discord.ButtonStyle.danger, row=2)
        self.button("Reset Defaults", self._reset, emoji="↩️", style=discord.ButtonStyle.danger, row=2)
        self.nav()

    async def _saved(self, interaction: discord.Interaction, notice: str, selected: str | None = None) -> None:
        await self.bot.settings_changed()
        await self._again(selected).show(interaction, notice)

    async def _add(self, interaction: discord.Interaction) -> None:
        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            try:
                async with self.bot.db.session() as session:
                    name = await category_service.add(session, self.bot.runtime, values["name"], actor_id=inter.user.id)
            except WaypointError as exc:
                await self.show(inter, f"⚠️ {exc.user_message}")
                return
            await self._saved(inter, f"✅ Added **{name}**.", name)

        await interaction.response.send_modal(
            FieldsModal("Add category", [Field("name", "Category name", required=True, max_length=50)], submitted)
        )

    async def _rename(self, interaction: discord.Interaction) -> None:
        old = self.selected
        assert old is not None

        async def submitted(inter: discord.Interaction, values: dict[str, str]) -> None:
            try:
                async with self.bot.db.session() as session:
                    new = await category_service.rename(session, self.bot.runtime, old, values["name"], actor_id=inter.user.id)
            except WaypointError as exc:
                await self.show(inter, f"⚠️ {exc.user_message}")
                return
            await self._saved(inter, f"✅ Renamed **{old}** to **{new}**. Existing listings were updated.", new)

        await interaction.response.send_modal(
            FieldsModal("Rename category", [Field("name", "New name", old, required=True, max_length=50)], submitted)
        )

    async def _remove(self, interaction: discord.Interaction) -> None:
        name = self.selected
        assert name is not None
        async with self.bot.db.session() as session:
            used = await category_service.usage(session, name)
        await RemoveCategoryPage(self.bot, self.owner_id, name, used, back=lambda: self._again(name)).show(interaction)

    def _move(self, step: int):
        async def callback(interaction: discord.Interaction) -> None:
            assert self.selected is not None
            async with self.bot.db.session() as session:
                await category_service.move(session, self.bot.runtime, self.selected, step, actor_id=interaction.user.id)
            await self._saved(interaction, "✅ Order saved.", self.selected)

        return callback

    async def _reset(self, interaction: discord.Interaction) -> None:
        async def apply(inter: discord.Interaction) -> None:
            try:
                async with self.bot.db.session() as session:
                    await category_service.reset_defaults(session, self.bot.runtime, actor_id=inter.user.id)
            except WaypointError as exc:
                await self._again().show(inter, f"⚠️ {exc.user_message}")
                return
            await self._saved(inter, "↩️ Default categories restored.")

        await ConfirmPage(
            self.bot, self.owner_id, question="Restore the default categories?", confirm_label="Reset Defaults",
            on_confirm=apply, back=lambda: self._again(self.selected),
        ).show(interaction)


class RemoveCategoryPage(Page):
    """Removing a category in use: ask where its listings should go."""

    def __init__(self, bot: WaypointBot, owner_id: int, name: str, used: int, *, back) -> None:
        super().__init__(bot, owner_id, back=back)
        self.name = name
        self.used = used
        self.move_to: str | None = None

    def content(self) -> str:
        if not self.used:
            return f"## Remove {self.name}?\nNo listings use it."
        target = f"**{self.move_to}**" if self.move_to else "choose below"
        return f"## Remove {self.name}?\n**{self.used}** listing(s) use it. Move them to: {target}"

    def build(self) -> None:
        if self.used:
            others = [c for c in self.bot.runtime.listings.categories if c != self.name]
            select = discord.ui.Select(
                placeholder="Move its listings to…",
                options=[discord.SelectOption(label=c, value=c, default=c == self.move_to) for c in others],
                row=0,
            )

            async def picked(interaction: discord.Interaction) -> None:
                self.move_to = select.values[0]
                await self.show(interaction)

            select.callback = picked  # type: ignore[method-assign]
            self.add_item(select)
        self.button(
            "Remove", self._confirm, style=discord.ButtonStyle.danger,
            disabled=bool(self.used) and self.move_to is None, row=1,
        )
        self.button("Cancel", self._go_back, style=discord.ButtonStyle.secondary, row=2)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        async with self.bot.db.session() as session:
            moved = await category_service.remove(
                session, self.bot.runtime, self.name, move_to=self.move_to, actor_id=interaction.user.id
            )
        await self.bot.settings_changed()
        page = CategoriesPage(self.bot, self.owner_id, back=self.back().back)
        note = f"🗑️ Removed **{self.name}**." + (f" {moved} listing(s) moved to **{self.move_to}**." if moved else "")
        await page.show(interaction, note)
