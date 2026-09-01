from sqlalchemy import Column, Integer, String, JSON, DateTime, func, Float
from sqlalchemy.orm import Mapped, mapped_column
from rotor.database import Base
from typing import Optional
from datetime import datetime


class RequestLog(Base):
    """Request log model for tracking API usage."""

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Request info
    token_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    channel_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    request_model: Mapped[str] = mapped_column(String(100), nullable=False, comment="Original model name in request")

    # Token usage
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    uncached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_5m_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_1h_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usage_schema_version: Mapped[str] = mapped_column(String(10), default="2")
    capacity_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    cache_scope: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    capacity_scope: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    billing_scope: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Cost calculation (optional)
    cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    cost_status: Mapped[str] = mapped_column(String(30), default="unknown")
    tariff_version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tariff_period: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tariff_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Status
    success: Mapped[bool] = mapped_column(Integer, default=True)  # Using Integer for SQLite compatibility
    error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Request/Response data (for debugging)
    request_body: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_body: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Latency
    latency: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="Request latency in seconds")

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True
    )

    # IP tracking (optional)
    ip: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    def __repr__(self) -> str:
        return f"<RequestLog {self.id} - {self.model} - {self.created_at}>"
