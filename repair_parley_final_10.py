from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()
pending = {}
original = {}

def load(rel):
    path = ROOT / rel
    if path in pending:
        return pending[path]
    if not path.exists():
        raise SystemExit(f"[STOP] Missing file: {rel}")
    text = path.read_text(encoding="utf-8")
    original[path] = text
    pending[path] = text
    return text

def save_later(rel, text):
    pending[ROOT / rel] = text

def replace_once(rel, old, new, label):
    text = load(rel)
    if new in text and old not in text:
        return
    if old not in text:
        raise SystemExit(f"[STOP] Could not find {label} in {rel}. No files were written.")
    save_later(rel, text.replace(old, new, 1))

# 1) Rebuild DEFAULT_BUTTONS cleanly.
runtime = load("bot/config/runtime.py")
start = runtime.find("DEFAULT_BUTTONS:")
end = runtime.find('BUTTON_STYLES = ("primary", "success", "danger", "secondary")', start)
if start < 0 or end < 0:
    raise SystemExit("[STOP] Could not locate DEFAULT_BUTTONS. No files were written.")
runtime = runtime[:start] + 'DEFAULT_BUTTONS: dict[str, dict[str, str | None]] = {\n    # Label, emoji and colour of every user-facing button.\n    "post": {"label": "Post Server Ad", "emoji": None, "style": "primary"},\n    "connect": {"label": "Connect This Server", "emoji": None, "style": "primary"},\n    "find": {"label": "Browse Partners", "emoji": None, "style": "success"},\n    "servers": {"label": "My Server Listings", "emoji": None, "style": "primary"},\n    "requests": {"label": "Requests", "emoji": None, "style": "primary"},\n    "looking": {"label": "Post Partner Ad", "emoji": None, "style": "primary"},\n    "partner_posts": {"label": "My Partner Posts", "emoji": None, "style": "primary"},\n    "network": {"label": "Join Network", "emoji": "\\U0001F310", "style": "primary"},\n    "join": {"label": "Join Server", "emoji": None, "style": "primary"},\n    "request": {"label": "Request Partnership", "emoji": "🤝", "style": "primary"},\n    "view_ad": {"label": "View Server Ad", "emoji": None, "style": "primary"},\n    "next": {"label": "Next Match", "emoji": None, "style": "primary"},\n    "edit": {"label": "Edit", "emoji": None, "style": "primary"},\n    "edit_ad": {"label": "Edit Ad", "emoji": None, "style": "primary"},\n    "edit_info": {"label": "Edit Server Info", "emoji": None, "style": "primary"},\n    "partnerships": {"label": "Partnerships", "emoji": "🤝", "style": "primary"},\n    "preview": {"label": "View Ad", "emoji": None, "style": "primary"},\n    "self_post": {"label": "Paste My Own Ad", "emoji": None, "style": "primary"},\n    "refresh": {"label": "Relist", "emoji": None, "style": "secondary"},\n    "relist": {"label": "Relist", "emoji": None, "style": "secondary"},\n    "publish": {"label": "Publish", "emoji": None, "style": "primary"},\n    "remove": {"label": "Remove Listing", "emoji": None, "style": "danger"},\n    "accept": {"label": "Accept", "emoji": "\\u2705", "style": "success"},\n    "decline": {"label": "Decline", "emoji": "\\u274C", "style": "danger"},\n    "add_bot": {"label": "Add Parley", "emoji": None, "style": "primary"},\n    "support": {"label": "Support", "emoji": "\\U0001F6DF", "style": "primary"},\n    "rules": {"label": "Rules", "emoji": None, "style": "primary"},\n    "website": {"label": "Website", "emoji": None, "style": "primary"},\n    "how": {"label": "How It Works", "emoji": None, "style": "primary"},\n    "back": {"label": "Back", "emoji": "⬅️", "style": "secondary"},\n    "home": {"label": "Home", "emoji": "🏠", "style": "secondary"},\n    "directory": {"label": "Directory Overview", "emoji": None, "style": "secondary"},\n    "settings": {"label": "Settings", "emoji": "\\u2699\\uFE0F", "style": "primary"},\n}\n' + "\n" + runtime[end:]

c_start = runtime.find("CUSTOMIZABLE_BUTTONS = (")
c_end = runtime.find("\n)\n\nMODES =", c_start)
if c_start < 0 or c_end < 0:
    raise SystemExit("[STOP] Could not locate CUSTOMIZABLE_BUTTONS. No files were written.")
runtime = runtime[:c_start] + 'CUSTOMIZABLE_BUTTONS = (\n    "post", "connect", "find", "servers", "requests", "looking", "partner_posts",\n    "network", "join", "request", "view_ad", "next", "edit", "edit_ad", "edit_info",\n    "partnerships", "preview", "self_post", "refresh", "relist", "publish", "remove",\n    "accept", "decline", "add_bot", "support", "rules", "website", "directory",\n)' + runtime[c_end + 2:]
save_later("bot/config/runtime.py", runtime)

# 2) Keep ExhaustedView minimal: no Home button there.
partnership = load("bot/views/partnership.py")
a = partnership.find("class ExhaustedView")
b = partnership.find("async def _edit_or_reply(", a)
if a < 0 or b < 0:
    raise SystemExit("[STOP] Could not locate ExhaustedView. No files were written.")
segment = partnership[a:b]
segment = segment.replace("        self.add_item(home_button(self.bot, row=1))\n", "")
save_later("bot/views/partnership.py", partnership[:a] + segment + partnership[b:])

# 3) Update tests for the intentionally changed UI.
replace_once(
    "tests/unit/test_configuration.py",
    'assert config.button("post") == ("Post My Server", None) and config.button("find")[0] == "Search"',
    'assert config.button("post") == ("Post Server Ad", None) and config.button("find")[0] == "Search"',
    "new default post label test",
)

replace_once(
    "tests/unit/test_finder.py",
    'assert labels(without) == ["Try Another Category", "Show Again"]',
    'assert labels(without) == ["Show Again"]',
    "Any-category exhausted screen test",
)

replace_once(
    "tests/unit/test_links_and_ui.py",
    'assert labels(view) == ["Post My Server", "Find Partners", "My Listing", "Requests", "Add Parley"]',
    'assert labels(view) == ["Post Server Ad", "Browse Partners", "My Server Listings", "My Partner Posts", "Requests"]',
    "DM home labels test",
)

replace_once(
    "tests/unit/test_links_and_ui.py",
    '    runtime = default_config()\n    runtime = replace(runtime, bot=replace(runtime.bot, support_url="https://discord.gg/help", website_url="https://example.com"))\n    _content, view = welcome_panel(UIBot(runtime))\n    assert labels(view) == ["Find Partners", "Post My Server", "Directory Overview", "Add Parley"]\n',
    '    runtime = default_config()\n    runtime = replace(\n        runtime,\n        bot=replace(runtime.bot, support_url="https://discord.gg/help", website_url="https://example.com"),\n        hub=HubConfig(main_guild_id=1, welcome_channel_id=1, listings_channel_id=2, looking_channel_id=3),\n    )\n    _content, view = welcome_panel(UIBot(runtime))\n    assert labels(view) == ["Server Directory", "Find a Partner", "Post Server Ad", "Post Partner Ad"]\n',
    "Start Here router test",
)

replace_once(
    "tests/unit/test_user_screens.py",
    'assert labels(message["view"]) == ["Request Partnership", "Next Server", "View Ad", "Back"]',
    'assert labels(message["view"]) == ["Send Partner Request", "Next Match", "View Server Ad", "Home"]',
    "Finder result controls test",
)

# Validate before writing.
checks = {
    "bot/config/runtime.py": [
        '"post": {"label": "Post Server Ad"',
        '"find": {"label": "Browse Partners"',
        '"partner_posts": {"label": "My Partner Posts"',
        '"network": {"label": "Join Network"',
        '"join": {"label": "Join Server"',
        '"request": {"label": "Request Partnership"',
    ],
    "tests/unit/test_links_and_ui.py": [
        '"Server Directory", "Find a Partner", "Post Server Ad", "Post Partner Ad"',
    ],
    "tests/unit/test_user_screens.py": [
        '"Send Partner Request", "Next Match", "View Server Ad", "Home"',
    ],
}
for rel, needles in checks.items():
    text = load(rel)
    for needle in needles:
        if needle not in text:
            raise SystemExit(f"[STOP] Validation failed: {needle!r} missing from {rel}. No files were written.")

changed = []
for path, text in pending.items():
    old = original.get(path)
    if old == text:
        continue
    path.write_text(text, encoding="utf-8", newline="\n")
    changed.append(str(path.relative_to(ROOT)))

print()
print("[OK] Applied the final Parley UI/test repair.")
print()
print("Changed:")
for rel in sorted(changed):
    print(f"  - {rel}")
print()
print("Run now:")
print(r"  python -m py_compile bot\config\runtime.py")
print("  python -m pytest -q")
print("  python -m pyflakes bot tests migrations")
print("  git diff --check")
