from pathlib import Path

ROOT = Path.cwd()


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8", newline="\n")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"[STOP] Could not find expected text for: {label}\nYour file may already be fixed or differ from the GitHub version.")
    return text.replace(old, new, 1)


# 1) PostgreSQL migration: renaming the table does not free the PK index name.
path = "migrations/versions/0005_concurrent_self_post_sessions.py"
s = read(path)
s = replace_once(
    s,
    '    op.rename_table("self_post_sessions", "self_post_sessions_old")\n\n    op.create_table(',
    '    op.rename_table("self_post_sessions", "self_post_sessions_old")\n'
    '    if op.get_bind().dialect.name == "postgresql":\n'
    '        op.execute(sa.text("ALTER TABLE self_post_sessions_old RENAME CONSTRAINT pk_self_post_sessions TO pk_self_post_sessions_old"))\n\n'
    '    op.create_table(',
    "0005 upgrade PK rename",
)
s = replace_once(
    s,
    '    op.rename_table("self_post_sessions", "self_post_sessions_new")\n\n    op.create_table(',
    '    op.rename_table("self_post_sessions", "self_post_sessions_new")\n'
    '    if op.get_bind().dialect.name == "postgresql":\n'
    '        op.execute(sa.text("ALTER TABLE self_post_sessions_new RENAME CONSTRAINT pk_self_post_sessions TO pk_self_post_sessions_new"))\n\n'
    '    op.create_table(',
    "0005 downgrade PK rename",
)
write(path, s)

# 2) Health Check: keep internal/setup labels friendly while using stable check names.
path = "bot/services/health.py"
s = read(path)
s = replace_once(
    s,
    'PANEL_LABELS = {WELCOME_PANEL: "Welcome panel", LISTINGS_PANEL: "Listings panel", LOOKING_PANEL: "Looking panel"}\n',
    'PANEL_LABELS = {WELCOME_PANEL: "Welcome panel", LISTINGS_PANEL: "Listings panel", LOOKING_PANEL: "Looking panel"}\n'
    'CHANNEL_CHECK_NAMES = {\n'
    '    "welcome_channel_id": "Welcome channel",\n'
    '    "listings_channel_id": "Listings channel",\n'
    '    "looking_channel_id": "Looking for partners channel",\n'
    '    "support_channel_id": "Support channel",\n'
    '    "log_channel_id": "Staff logs channel",\n'
    '}\n',
    "health channel-name map",
)
s = replace_once(
    s,
    '        severity = FAIL if slot.required else WARN\n',
    '        severity = FAIL if slot.required else WARN\n'
    '        check_name = CHANNEL_CHECK_NAMES.get(slot.key, f"{slot.label} channel")\n',
    "health check_name",
)
s = s.replace('Check(FAIL, f"{slot.label} channel",', 'Check(FAIL, check_name,')
s = s.replace('Check(OK, f"{slot.label} channel",', 'Check(OK, check_name,')
s = s.replace('Check(severity, f"{slot.label} channel",', 'Check(severity, check_name,')
write(path, s)

# 3) Health test fake: real Discord channels always point back to their guild.
path = "tests/unit/test_health.py"
s = read(path)
s = replace_once(
    s,
    '        self.guild = SimpleNamespace(id=MAIN, name="HQ", me=me, get_channel=channels.get)\n',
    '        self.guild = SimpleNamespace(id=MAIN, name="HQ", me=me, get_channel=channels.get)\n'
    '        for item in channels.values():\n'
    '            item.guild = self.guild\n',
    "health fake channel.guild",
)
write(path, s)

# 4) Panel test fake: production bot has get_guild; return None to exercise DB fallback.
path = "tests/unit/test_panels.py"
s = read(path)
s = replace_once(
    s,
    '    def get_channel(self, channel_id: int):\n        return self.channels.get(channel_id)\n',
    '    def get_channel(self, channel_id: int):\n'
    '        return self.channels.get(channel_id)\n\n'
    '    def get_guild(self, guild_id: int):\n'
    '        return None\n',
    "panel fake get_guild",
)
write(path, s)

# 5) discord.py omits disabled mention keys; parse=[] is the actual no-role/no-everyone signal.
path = "tests/unit/test_self_post.py"
s = read(path)
s = replace_once(
    s,
    '    assert mentions.get("everyone") is False\n'
    '    assert mentions.get("roles") == []\n'
    '    assert mentions.get("users") == [ADMIN_ID]\n',
    '    assert mentions.get("parse") == []\n'
    '    assert mentions.get("users") == [ADMIN_ID]\n',
    "ghost ping mention serialization",
)
write(path, s)

# 6) Setup test fake: reused channels need the permission method real TextChannels have.
path = "tests/unit/test_setup_and_modes.py"
s = read(path)
s = replace_once(
    s,
    '        self.text_channels = [SimpleNamespace(id=100 + i, name=name) for i, name in enumerate(existing)]\n',
    '        self.text_channels = [\n'
    '            SimpleNamespace(\n'
    '                id=100 + i,\n'
    '                name=name,\n'
    '                permissions_for=lambda _member: discord.Permissions(manage_roles=False),\n'
    '            )\n'
    '            for i, name in enumerate(existing)\n'
    '        ]\n',
    "setup fake reused-channel permissions",
)
write(path, s)

# 7) Windows startup intentionally runs the venv Python executable, so test the module invocation.
path = "tests/unit/test_startup.py"
s = read(path)
s = replace_once(
    s,
    '    assert "python -m bot.main" in (ROOT / "start.bat").read_text()\n',
    '    assert "-m bot.main" in (ROOT / "start.bat").read_text()\n',
    "start.bat module assertion",
)
write(path, s)

# 8) CI quality-of-life: let both DB jobs finish so one failure cannot hide the other.
path = ".github/workflows/tests.yml"
s = read(path)
if "      fail-fast: false\n" not in s:
    s = replace_once(
        s,
        '    strategy:\n      matrix:\n',
        '    strategy:\n      fail-fast: false\n      matrix:\n',
        "GitHub Actions fail-fast",
    )
# Make the PostgreSQL health probe target the database that actually exists.
s = s.replace('pg_isready -U waypoint"', 'pg_isready -U waypoint -d waypoint_test"')
write(path, s)

print("[OK] Applied Parley CI fixes.")
print("Next run:")
print("  python -m pytest -q")
print("  git diff --check")
print("  git add .")
print('  git commit -m "Fix migrations and CI tests"')
print("  git push")
