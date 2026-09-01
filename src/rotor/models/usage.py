from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from rotor.database import Base


class UsageLedger(Base):
    """Append-only usage ledger for accounting and audits."""

    __tablename__ = "usage_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)

    user_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    token_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    channel_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    provider_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    request_protocol: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_protocol: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    uncached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_5m_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_1h_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    input_audio_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_audio_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usage_source: Mapped[str] = mapped_column(String(30), default="provider")
    usage_schema_version: Mapped[str] = mapped_column(String(10), default="2")
    capacity_snapshot: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
    )
    cache_scope: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    capacity_scope: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    billing_scope: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    input_cost: Mapped[float] = mapped_column(Float, default=0.0)
    output_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    cost_status: Mapped[str] = mapped_column(String(30), default="unknown")
    tariff_version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tariff_period: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tariff_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(30), default="success", index=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
