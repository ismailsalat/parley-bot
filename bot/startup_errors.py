"""Turn startup exceptions into one clear sentence (details go to the log file)."""

from __future__ import annotations

import socket
from collections.abc import Iterator


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        orig = getattr(current, "orig", None)  # SQLAlchemy wraps driver errors
        if isinstance(orig, BaseException):
            yield orig
        current = current.__cause__ or current.__context__


def describe_database_error(exc: BaseException) -> tuple[str, str]:
    """(problem, what to do). Never includes the URL, username or password."""
    for item in _chain(exc):
        name = type(item).__name__
        text = str(item).lower()
        if name in ("InvalidPasswordError", "InvalidAuthorizationSpecificationError") or "password authentication" in text:
            return "Database login failed.", "Check the username and password in DATABASE_URL."
        if name == "InvalidCatalogNameError" or ("database" in text and "does not exist" in text):
            return "The database doesn't exist.", "Check the database name at the end of DATABASE_URL."
        if isinstance(item, socket.gaierror):
            return "Database host not found.", "Check the host name in DATABASE_URL."
        if isinstance(item, (ConnectionRefusedError, TimeoutError)) or name in ("ConnectionDoesNotExistError",):
            return "Database connection failed.", "Check DATABASE_URL and that the database is running, then try again."
        if "unable to open database file" in text:
            return "Could not open the local database file.", "Check that the Waypoint folder is writable."
        if isinstance(item, OSError):
            return "Database connection failed.", "Check DATABASE_URL and that the database is running, then try again."
    return "Database setup failed.", "Check DATABASE_URL and try again."
