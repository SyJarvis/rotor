from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from rotor.database import Base


class SessionLease(Base):
    """Persistent channel ownership for one tenant session and logical model."""

    __tablename__ = "session_leases"
    __table_args__ = (
        UniqueConstraint(
            "token_id",
            "session_id",
            "logical_model",
            name="uq_session_leases_token_session_model",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    token_id: Mapped[int] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    logical_model: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SessionLeaseEvent(Base):
    """Append-only explanation of a Session Lease state transition."""

    __tablename__ = "session_lease_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('assigned', 'renewed', 'migrated', 'expired')",
            name="ck_session_lease_events_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    lease_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    token_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    logical_model: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )
    previous_channel_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    channel_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
