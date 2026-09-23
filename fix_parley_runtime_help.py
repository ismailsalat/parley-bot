from pathlib import Path
import re

p = Path(r"bot\config\runtime.py")
s = p.read_text(encoding="utf-8")

replacement = '    help: str = (\n        "## {bot_name}\\n"\n        "Connect once. Post once. Find partners. Talk to people.\\n\\n"\n        "**Post Server Ad** - publish a server ad (needs Manage Server).\\n"\n        "**Browse Partners** - quickly browse partnership matches.\\n"\n        "**My Server Listings** - edit, view, Relist or remove server ads.\\n"\n        "**My Partner Posts** - post, edit or delete what each server is looking for.\\n"\n        "**Requests** - accept or decline partnership requests.\\n\\n"\n        "Just DM me anytime to get these buttons."\n    )\n'

pattern = r'(?ms)^    help: str = \(\n.*?^    \)\n'
s2, n = re.subn(pattern, replacement, s, count=1)

if n != 1:
    raise SystemExit("[STOP] Could not find exactly one MessagesConfig help block.")

p.write_text(s2, encoding="utf-8", newline="\n")
print("[OK] Fixed bot/config/runtime.py help block.")
