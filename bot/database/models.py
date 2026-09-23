"""Database models.

The Discord guild ID is the canonical identity of a server. The unique
constraint on ``listings.guild_id`` guarantees one listing per guild no matter
how many invite links are used.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Auto-increment primary keys: BIGINT on PostgreSQL, INTEGER on SQLite (required for rowid aliasing).
AutoId = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes on every backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ListingStatus:
    PENDING = "pending"  # waiting for staff approval
    ACTIVE = "active"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    REMOVED = "removed"

    LIVE = (PENDING, ACTIVE, SUSPENDED)  # these block a second "connect"


class RequestStatus:
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Guild(Base):
    __tablename__ = "guilds"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100))
    icon_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    connected_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="active")


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), unique=True, index=True
    )
    advertisement_text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(300))  # comma separated category names
    accepting_partnerships: Mapped[bool] = mapped_column(Boolean, default=True)
    minimum_members: Mapped[int] = mapped_column(Integer, default=0)
    invite_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # One advertisement edit is allowed per Relist cycle.
    last_ad_edit_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=ListingStatus.ACTIVE, index=True)
    # Created while Parley was in TEST mode: never shown to real users in LIVE mode.
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # A listing the owner posted themselves (keeps their own custom emoji).
    self_posted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # Parley's [Join Server] [Request Partnership] message under a self-posted listing.
    controls_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Independent owner-authored post in #find-partners.
    partner_ad_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    partner_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    partner_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    partner_controls_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    partner_posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Approved listings keep their live content while an edit waits for staff review.
    pending_changes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pending_submitted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Bumped on every submission; review buttons carry it so stale reviews can't approve newer content.
    review_revision: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    review_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    @property
    def categories(self) -> list[str]:
        return [c for c in (self.category or "").split(",") if c]

    @property
    def awaiting_review(self) -> bool:
        return self.status == ListingStatus.PENDING or bool(self.pending_changes)


class SelfPostSession(Base):
    """Crash-recovery record for one member's temporary directory posting window.

    Multiple different servers may post at the same time. A member can only have
    one open window in the directory, and a listed server can only have one open
    window regardless of how many admins it has.
    """

    __tablename__ = "self_post_sessions"
    __table_args__ = (UniqueConstraint("listing_guild_id", name="uq_self_post_sessions_listing_guild"),)

    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    hub_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    listing_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    had_overwrite: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    previous_allow: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    previous_deny: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class ListingContact(Base):
    __tablename__ = "listing_contacts"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", name="uq_listing_contacts_guild_user"),)

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class PartnershipRequest(Base):
    __tablename__ = "partnership_requests"
    __table_args__ = (
        CheckConstraint("source_guild_id <> target_guild_id", name="not_self"),
        # Database-level guarantee: only one *pending* request per source -> target pair.
        Index(
            "uq_partnership_requests_pending_pair",
            "source_guild_id",
            "target_guild_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("ix_partnership_requests_target_status", "target_guild_id", "status"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    source_guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_guild_id: Mapped[int] = mapped_column(BigInteger)
    requester_user_id: Mapped[int] = mapped_column(BigInteger)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=RequestStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    responded_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class NetworkSettings(Base):
    __tablename__ = "network_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=180)
    last_post_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    configured_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class NetworkPost(Base):
    __tablename__ = "network_posts"
    __table_args__ = (Index("ix_network_posts_destination_posted", "destination_guild_id", "posted_at"),)

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    source_guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    destination_guild_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    posted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PanelState(Base):
    __tablename__ = "panel_states"
    __table_args__ = (UniqueConstraint("guild_id", "panel_type", name="uq_panel_states_guild_type"),)

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    panel_type: Mapped[str] = mapped_column(String(30))
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ModerationBan(Base):
    __tablename__ = "moderation_bans"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", name="uq_moderation_bans_target"),
        CheckConstraint("target_type IN ('guild', 'user')", name="target_type"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(10))  # "guild" or "user"
    target_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    moderator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Cooldown(Base):
    """Persistent cooldowns so restarts never reset anti-spam limits."""

    __tablename__ = "cooldowns"

    scope: Mapped[str] = mapped_column(String(40), primary_key=True)
    subject: Mapped[str] = mapped_column(String(100), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class RuntimeSetting(Base):
    """Business-rule overrides (see bot.config.runtime)."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class TestMessage(Base):
    """Messages posted by the Test Center, so they can all be deleted in one click."""

    __tablename__ = "test_messages"
    __test__ = False  # not a pytest test class

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
