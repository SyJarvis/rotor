from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from rotor.database import Base


class RequestAttempt(Base):
    """Append-only record of one upstream channel attempt."""

    __tablename__ = "request_attempts"
    __table_args__ = (
        UniqueConstraint(
            "request_id",
            "attempt_index",
            name="uq_request_attempt_index",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    attempt_index: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    requested_model: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    provider_model: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    request_protocol: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_protocol: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(
        String(30), nullable=False, index=True
    )

    upstream_status: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    error_category: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, index=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True
    )
    retryable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    retry_same_channel: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    fallback_allowed: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    retry_after_seconds: Mapped[Optional[float]] = mapped_column(
        nullable=True
    )
    sanitized_error: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    provider_request_ids: Mapped[dict[str, str]] = mapped_column(
        JSON, default=dict, nullable=False
    )

    request_origin: Mapped[str] = mapped_column(
        String(30), default="client", nullable=False, index=True
    )
    agent_run_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
