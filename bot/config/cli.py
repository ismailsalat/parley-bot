"""View or change runtime business settings stored in the database.

    python -m bot.config.cli list
    python -m bot.config.cli set listings.refresh_cooldown_minutes 30
    python -m bot.config.cli set listings.categories '["Gaming", "Anime", "Art"]'
    python -m bot.config.cli unset listings.refresh_cooldown_minutes

The running bot picks changes up within a few minutes (system.maintenance_interval_minutes).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from bot.config.runtime import apply_overrides, default_config, known_keys
from bot.config.settings import SettingsError, load_settings
from bot.database import repository
from bot.database.session import Database


def _show(value: object) -> str:
    if isinstance(value, tuple):
        value = list(value)
    return json.dumps(value, ensure_ascii=False)


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.database_url, settings.database_connect_args)
    try:
        async with db.session() as session:
            overrides = await repository.runtime_overrides(session)
            if args.command == "list":
                effective, notes = apply_overrides(default_config(), overrides)
                for key, value in known_keys(effective).items():
                    marker = "*" if key in overrides else " "
                    print(f"{marker} {key} = {_show(value)}")
                for note in notes:
                    print(f"! {note}")
                print("\n(* = changed from the default)")
                return 0

            if args.key not in known_keys():
                print(f"Unknown setting: {args.key}. Run 'list' to see every setting.", file=sys.stderr)
                return 1
            if args.command == "unset":
                removed = await repository.delete_runtime_setting(session, args.key)
                print("Reset to default." if removed else "That setting already uses the default.")
                return 0

            try:
                value = json.loads(args.value)
            except json.JSONDecodeError:
                value = args.value  # plain text
            _config, notes = apply_overrides(default_config(), {**overrides, args.key: value})
            problems = [n for n in notes if n.startswith(f"{args.key}:")]
            if problems:
                print(f"Invalid value: {problems[0]}", file=sys.stderr)
                return 1
            await repository.set_runtime_setting(session, args.key, value)
            print(f"Saved {args.key} = {_show(value)}")
            for note in notes:
                print(f"! {note}")
            return 0
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.config.cli", description="Waypoint runtime settings")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every setting and its current value")
    set_cmd = sub.add_parser("set", help="change a setting")
    set_cmd.add_argument("key")
    set_cmd.add_argument("value", help="JSON value (numbers, true/false, lists) or plain text")
    unset_cmd = sub.add_parser("unset", help="reset a setting to its default")
    unset_cmd.add_argument("key")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
