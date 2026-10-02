from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()
changed: list[str] = []


def read(rel: str) -> str:
    p = ROOT / rel
    if not p.exists():
        raise SystemExit(f"[STOP] Missing {rel}")
    return p.read_text(encoding="utf-8")


def write(rel: str, text: str) -> None:
    p = ROOT / rel
    old = p.read_text(encoding="utf-8")
    if old != text:
        p.write_text(text, encoding="utf-8", newline="\n")
        changed.append(rel)


# 1) Acknowledge top-level persistent buttons BEFORE database/API work.
rel = "bot/views/welcome.py"
text = read(rel)
old = '''    async def callback(self, interaction: discord.Interaction) -> None:
        handler = _HANDLERS.get(self.action)
        if handler is None:
            log.warning("Unknown action button pressed: %s", self.action)
            await reply(interaction, "That button is no longer available.")
            return
        if not await guard(interaction):
            return
        try:
            await handler(interaction)
        except Exception as exc:  # noqa: BLE001 - reported to the user and logged by handle_error
            await handle_error(interaction, exc)
'''
new = '''    async def callback(self, interaction: discord.Interaction) -> None:
        handler = _HANDLERS.get(self.action)
        if handler is None:
            log.warning("Unknown action button pressed: %s", self.action)
            await reply(interaction, "That button is no longer available.")
            return
        try:
            # Discord gives component interactions only a few seconds for the
            # initial acknowledgement. Top-level actions often read Postgres
            # before rendering, so acknowledge immediately and finish afterward.
            if not interaction.response.is_done():
                await interaction.response.defer(
                    ephemeral=interaction.guild is not None,
                    thinking=True,
                )
            if not await guard(interaction):
                return
            await handler(interaction)
        except Exception as exc:  # noqa: BLE001 - reported to the user and logged by handle_error
            await handle_error(interaction, exc)
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find ActionButton callback in bot/views/welcome.py")
    write(rel, text.replace(old, new, 1))


# 2) Remove the database query from the hot guard path in production.
rel = "bot/views/base.py"
text = read(rel)
old = '''    async with bot.db.session() as session:
        blocked = await moderation.is_user_blocked(session, bot.runtime, interaction.user.id)
    if blocked:
        await reply(interaction, "You can't use Parley.")
        return False
'''
new = '''    # Production keeps database-backed user bans in memory so a slow/cold
    # database connection cannot consume Discord's interaction response window.
    # Test doubles without the cache retain the database-backed behavior.
    if hasattr(bot, "blocked_user_ids"):
        blocked = interaction.user.id in bot.blocked_user_ids
    else:
        async with bot.db.session() as session:
            blocked = await moderation.is_user_blocked(session, bot.runtime, interaction.user.id)
    if blocked:
        await reply(interaction, "You can't use Parley.")
        return False
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find blocked-user check in bot/views/base.py")
    write(rel, text.replace(old, new, 1))


# 3) Load the blocked-user cache at startup/config reload.
rel = "bot/core.py"
text = read(rel)

old = '''        self.staff_cache: dict[int, tuple[bool, float]] = {}
        self.staff_guild_id: int | None = None
'''
new = '''        self.staff_cache: dict[int, tuple[bool, float]] = {}
        self.blocked_user_ids: set[int] = set()
        self.staff_guild_id: int | None = None
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find cache init in bot/core.py")
    text = text.replace(old, new, 1)

old = '''        async with self.db.session() as session:
            overrides = await repository.runtime_overrides(session)
        config, notes = apply_overrides(default_config(), overrides)
'''
new = '''        async with self.db.session() as session:
            overrides = await repository.runtime_overrides(session)
            user_bans = await repository.list_bans(session, "user", limit=100_000)
        config, notes = apply_overrides(default_config(), overrides)
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find runtime reload query in bot/core.py")
    text = text.replace(old, new, 1)

old = '''        self.runtime = config
        self.staff_cache.clear()
'''
new = '''        self.runtime = config
        self.blocked_user_ids = set(config.moderation.blocked_user_ids) | {
            ban.target_id for ban in user_bans
        }
        self.staff_cache.clear()
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find runtime assignment in bot/core.py")
    text = text.replace(old, new, 1)

write(rel, text)


# 4) Relist may be entered through an already-deferred top-level button.
rel = "bot/views/management.py"
text = read(rel)
old = '''    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.db.session() as session:
        await listing_service.claim_refresh(
'''
new = '''    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.db.session() as session:
        await listing_service.claim_refresh(
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find Relist defer in bot/views/management.py")
    write(rel, text.replace(old, new, 1))


# 5) Keep the in-memory block cache correct when staff block/unblock users.
rel = "bot/commands/admin.py"
text = read(rel)
old = '''        async with self.bot.db.session() as session:
            await moderation.block_user(session, user_id=user.id, reason=reason, moderator_id=interaction.user.id)
        await self._done(interaction, f"Blocked {user.mention} (`{user.id}`).")
'''
new = '''        async with self.bot.db.session() as session:
            await moderation.block_user(session, user_id=user.id, reason=reason, moderator_id=interaction.user.id)
        self.bot.blocked_user_ids.add(user.id)
        await self._done(interaction, f"Blocked {user.mention} (`{user.id}`).")
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find block-user code in bot/commands/admin.py")
    text = text.replace(old, new, 1)

old = '''        async with self.bot.db.session() as session:
            await moderation.unblock_user(session, user_id=user.id, moderator_id=interaction.user.id)
        await self._done(interaction, f"Unblocked {user.mention} (`{user.id}`).")
'''
new = '''        async with self.bot.db.session() as session:
            await moderation.unblock_user(session, user_id=user.id, moderator_id=interaction.user.id)
        self.bot.blocked_user_ids.discard(user.id)
        await self._done(interaction, f"Unblocked {user.mention} (`{user.id}`).")
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find unblock-user code in bot/commands/admin.py")
    text = text.replace(old, new, 1)

write(rel, text)


rel = "bot/views/admin/moderation.py"
text = read(rel)
old = '''            async with self.bot.db.session() as session:
                await moderation.unblock_user(session, user_id=self.user_id, moderator_id=inter.user.id)
            await self.back().show(inter, "🔓 User unblocked.")
'''
new = '''            async with self.bot.db.session() as session:
                await moderation.unblock_user(session, user_id=self.user_id, moderator_id=inter.user.id)
            self.bot.blocked_user_ids.discard(self.user_id)
            await self.back().show(inter, "🔓 User unblocked.")
'''
if new not in text:
    if old not in text:
        raise SystemExit("[STOP] Could not find moderation UI unblock code.")
    write(rel, text.replace(old, new, 1))


print("[OK] Applied Discord interaction timeout fix.")
print()
print("Changed:")
for rel in changed:
    print("  -", rel)
print()
print("Run:")
print(r"  python -m pytest -q")
print(r"  python -m pyflakes bot tests migrations")
print(r"  git diff --check")
print()
print("If green:")
print(r"  git add .")
print(r'  git commit -m "Fix Discord button interaction timeouts"')
print(r"  git push")
