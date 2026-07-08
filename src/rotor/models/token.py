from sqlalchemy import Column, Integer, String, Boolean, DateTime, func, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from rotor.database import Base
from typing import Optional
from datetime import datetime


class Token(Base):
    """Token model for storing user API keys."""

    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)

    # Quota management
    quota: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Token quota limit (tokens), null means unlimited"
    )
    used_quota: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="Used quota in tokens"
    )

    # Group/Channel access
    group: Mapped[str] = mapped_column(
        String(50),
        default="default",
        comment="User group for channel access control"
    )

    # Channel restrictions (optional)
    allowed_channels: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
        comment="List of channel IDs this token can access, null means all"
    )

    # Status
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expired: Mapped[bool] = mapped_column(Boolean, default=False)
    expire_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    # Statistics
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0, comment="Total tokens consumed")

    def __repr__(self) -> str:
        return f"<Token {self.key[:10]}... ({self.name})>"
