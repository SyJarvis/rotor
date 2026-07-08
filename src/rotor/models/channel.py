from sqlalchemy import Column, Integer, String, JSON, Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from rotor.database import Base
from typing import Optional
from datetime import datetime


class Channel(Base):
    """Channel model for storing provider configurations."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Provider type: openai, anthropic, moonshot, minimax, zhipu"
    )
    key: Mapped[str] = mapped_column(String(500), nullable=False, comment="API Key for the provider")
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, comment="Base URL for the provider API")

    # Model configuration
    models: Mapped[dict] = mapped_column(JSON, default=list, comment="List of models available on this channel")
    model_mapping: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        comment="Mapping from user-facing model names to provider model names"
    )

    # Load balancing
    priority: Mapped[int] = mapped_column(Integer, default=1, comment="Channel priority (higher = preferred)")
    weight: Mapped[int] = mapped_column(Integer, default=1, comment="Weight for load balancing within same priority")

    # Status
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="Whether the channel is enabled")
    test_only: Mapped[bool] = mapped_column(Boolean, default=False, comment="Whether channel is for testing only")

    # Protocol
    protocol: Mapped[str] = mapped_column(
        String(20),
        default="openai",
        comment="Protocol: openai or anthropic"
    )

    # Rate limiting (optional)
    rpm_limit: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Requests per minute limit"
    )
    tpm_limit: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Tokens per minute limit"
    )

    # Metadata
    extra: Mapped[dict] = mapped_column(JSON, default=dict, comment="Extra configuration")

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

    # Statistics
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    success_requests: Mapped[int] = mapped_column(Integer, default=0)
    failed_requests: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return f"<Channel {self.name} ({self.type})>"
