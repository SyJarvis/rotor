from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rotor.database import Base


class ChannelOAuthSession(Base):
    """Short-lived, administrator-bound browser authorization attempt."""

    __tablename__ = "channel_oauth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    oauth_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pkce_verifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_auth_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interval: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    next_poll_at: Mapped[float] = mapped_column(Float, nullable=False)
    # Existing lease columns serialize the single authorization-code exchange.
    poll_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    poll_lock_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
