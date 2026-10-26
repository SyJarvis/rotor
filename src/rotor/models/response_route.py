from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class ResponseRoute(Base):
    """Persistent ownership and upstream routing for native Responses objects."""

    __tablename__ = "response_routes"
    __table_args__ = (
        UniqueConstraint(
            "token_id", "response_id", name="uq_response_routes_token_response"
        ),
        UniqueConstraint("owner_id", "response_id", name="uq_response_routes_owner_response"),
        CheckConstraint("(token_id IS NOT NULL AND owner_id IS NULL) OR (token_id IS NULL AND owner_id IS NOT NULL)",
                        name="ck_response_routes_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    response_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    token_id: Mapped[int | None] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"), nullable=True, index=True
    )
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    usage_accounted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
