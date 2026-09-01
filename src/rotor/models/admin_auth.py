from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from rotor.database import Base


class AdminUser(Base):
    """Administrator account for the Rotor management plane."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AdminSession(Base):
    """Server-side browser session issued after administrator login."""

    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    secret_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AdminLoginThrottle(Base):
    """Persistent failed-login window for one username and client IP."""

    __tablename__ = "admin_login_throttles"
    __table_args__ = (
        UniqueConstraint(
            "username_key",
            "client_ip",
            name="uq_admin_login_throttle_username_ip",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username_key: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    client_ip: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AdminAuthEvent(Base):
    """Append-only administrator authentication audit event."""

    __tablename__ = "admin_auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    admin_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    username: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(
        String(40), nullable=False, index=True
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    client_ip: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
