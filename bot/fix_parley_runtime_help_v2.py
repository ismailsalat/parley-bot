from pathlib import Path

p = Path(r"bot\config\runtime.py")
s = p.read_text(encoding="utf-8")

start = s.find("    help: str = (")
end_marker = "\n\n\n@dataclass(frozen=True)\nclass SystemConfig:"
end = s.find(end_marker, start)

if start == -1 or end == -1:
    raise SystemExit("[STOP] Could not locate the MessagesConfig help block safely.")

help_block = (
    '    help: str = (\n'
    '        "## {bot_name}\\n"\n'
    '        "Connect once. Post once. Find partners. Talk to people.\\n\\n"\n'
    '        "**Post Server Ad** - publish a server ad (needs Manage Server).\\n"\n'
    '        "**Browse Partners** - quickly browse partnership matches.\\n"\n'
    '        "**My Server Listings** - edit, view, Relist or remove server ads.\\n"\n'
    '        "**My Partner Posts** - post, edit or delete what each server is looking for.\\n"\n'
    '        "**Requests** - accept or decline partnership requests.\\n\\n"\n'
    '        "Just DM me anytime to get these buttons."\n'
    '    )'
)

fixed = s[:start] + help_block + s[end:]
p.write_text(fixed, encoding="utf-8", newline="\n")

print("[OK] Rebuilt the MessagesConfig help block with escaped newlines.")
