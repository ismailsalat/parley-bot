"""Startup: friendly errors, no secrets, no tracebacks on the console, locked dependencies."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from bot.startup_errors import describe_database_error

ROOT = Path(__file__).resolve().parents[2]


class InvalidPasswordError(Exception):
    pass


@pytest.mark.parametrize(
    ("exc", "problem"),
    [
        (ConnectionRefusedError(111, "refused"), "Database connection failed."),
        (socket.gaierror(-2, "Name or service not known"), "Database host not found."),
        (InvalidPasswordError("password authentication failed for user x"), "Database login failed."),
        (RuntimeError("wrapped"), "Database setup failed."),
    ],
)
def test_database_errors_are_described_plainly(exc, problem):
    assert describe_database_error(exc)[0] == problem


def test_wrapped_errors_are_found():
    try:
        try:
            raise ConnectionRefusedError(111, "refused")
        except ConnectionRefusedError as inner:
            raise RuntimeError("sqlalchemy wrapper") from inner
    except RuntimeError as outer:
        assert describe_database_error(outer)[0] == "Database connection failed."


def run_main(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "DISCORD_TOKEN")}
    full_env.update({"LOG_TO_FILE": "false", "PYTHONPATH": str(ROOT), **env})
    return subprocess.run(
        [sys.executable, "-m", "bot.main"], cwd=tmp_path, env=full_env, capture_output=True, text=True, timeout=120
    )


def test_unreachable_database_gives_one_clean_line(tmp_path):
    result = run_main(tmp_path, DISCORD_TOKEN="x.y.z", DATABASE_URL="postgresql://u:supersecretpw@127.0.0.1:9/db")
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[Waypoint] Database connection failed." in output
    assert "Traceback" not in output
    assert "supersecretpw" not in output and "x.y.z" not in output


def test_missing_token_is_explained(tmp_path):
    result = run_main(tmp_path, DISCORD_TOKEN="", DATABASE_URL=f"sqlite:///{tmp_path / 'a.db'}")
    assert result.returncode == 2
    assert "DISCORD_TOKEN is not set" in result.stderr and "Traceback" not in result.stderr


def test_invalid_configuration_is_explained(tmp_path):
    result = run_main(tmp_path, DISCORD_TOKEN="x", MAIN_GUILD_ID="abc")
    assert result.returncode == 2 and "[Waypoint] Configuration problem" in result.stderr


def test_lock_file_pins_every_production_dependency():
    lines = [l.strip() for l in (ROOT / "requirements.lock").read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[\w.\-+]+", l) for l in lines), lines
    locked = {l.split("==")[0].lower().split("[")[0] for l in lines}
    wanted = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            wanted.add(re.split(r"[<>=\[ ]", line)[0].lower())
    assert wanted <= locked, wanted - locked


def test_deployment_files_use_the_lock():
    assert "requirements.lock" in (ROOT / "Dockerfile").read_text()
    assert "requirements.lock" in (ROOT / "setup.bat").read_text()
    assert "requirements.lock" in (ROOT / "requirements-dev.txt").read_text()
    assert "python -m bot.main" in (ROOT / "railway.json").read_text()
    assert "python -m bot.main" in (ROOT / "start.bat").read_text()


def test_env_example_contains_no_secrets_and_only_needs_three_values():
    text = (ROOT / ".env.example").read_text()
    active = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert [l.split("=")[0] for l in active] == ["DISCORD_TOKEN", "DATABASE_URL", "ENVIRONMENT"]
    assert all(l.split("=", 1)[1] in ("", "production", "development") for l in active)


def test_gitignore_protects_secrets():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    for pattern in (".env", ".venv/", "*.db", "logs/", ".pytest_cache/"):
        assert pattern in ignored


def test_no_conversation_links_in_docs():
    for path in [ROOT / "README.md", *ROOT.glob("*.md")]:
        assert "claude.ai" not in path.read_text(encoding="utf-8"), path
