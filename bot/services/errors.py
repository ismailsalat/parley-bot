"""Errors whose message is safe and friendly enough to show to users.

Anything that is *not* a WaypointError is treated as a bug: it is logged with a
traceback and the user gets a generic message.
"""

from __future__ import annotations

from datetime import timedelta


class WaypointError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ValidationError(WaypointError):
    pass


class PermissionDenied(WaypointError):
    pass


class NotFound(WaypointError):
    pass


class Conflict(WaypointError):
    pass


class Banned(WaypointError):
    pass


class CooldownActive(WaypointError):
    def __init__(self, user_message: str, remaining: timedelta) -> None:
        super().__init__(user_message)
        self.remaining = remaining


MANAGE_SERVER_REQUIRED = "You need Manage Server permission."
LISTING_GONE = "This listing no longer exists."
