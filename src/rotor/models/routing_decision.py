from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from rotor.database import Base


class RoutingDecisionRecord(Base):
    """Immutable feature and candidate snapshot used for policy evaluation."""

    __tablename__ = "routing_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    token_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    request_protocol: Mapped[str] = mapped_column(String(50), nullable=False)
    strategy: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(30), default="adaptive-v1")
    candidate_channel_ids: Mapped[list] = mapped_column(JSON, default=list)
    selected_channel_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    required_capabilities: Mapped[list] = mapped_column(JSON, default=list)
    affinity_used: Mapped[bool] = mapped_column(Boolean, default=False)
    score_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    feature_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
