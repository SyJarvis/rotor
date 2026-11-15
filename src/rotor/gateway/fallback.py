
import httpx

from rotor.core.exceptions import normalize_upstream_error

# TCP/TLS connection failures occur before the application request is sent;
# bounded same-channel retries preserve the session's cache affinity.
CONNECT_RETRY_DELAYS = (0.5, 2.0)
# Once a channel is declared dead, let it back in quickly: plan-level cache
# entries expire within minutes, so the exile must stay shorter than that.
CONNECT_COOLDOWN_SECONDS = 10.0
# Sustained connect failures back off exponentially: a single blip cools for
# 10s, but a persistently dead upstream is kept out up to 5 minutes so
# requests are not released every 10s just to crash into the same TLS error.
CONNECT_COOLDOWN_BACKOFF = (10.0, 30.0, 60.0, 120.0, 300.0)


def connect_cooldown_for_streak(streak: int) -> float:
    """Cooldown for the Nth consecutive connect failure (0-based)."""
    if streak < 0:
        streak = 0
    if streak >= len(CONNECT_COOLDOWN_BACKOFF):
        streak = len(CONNECT_COOLDOWN_BACKOFF) - 1
    return CONNECT_COOLDOWN_BACKOFF[streak]


def is_connect_failure(exc: Exception) -> bool:
    """Whether connection establishment failed before sending the HTTP request.

    A RemoteProtocolError can follow an accepted POST, even when no response
    headers arrived, so it must not consume this safe connection-retry budget.
    """
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


class ConnectRetry:
    """Per-request budget for same-channel retries of connect failures."""

    def __init__(self, delays: tuple[float, ...] = CONNECT_RETRY_DELAYS) -> None:
        self._delays = delays
        self._used: dict[int, int] = {}

    def plan_retry(self, channel_id: int) -> float | None:
        """Consume one retry slot for the channel; return the backoff delay."""
        used = self._used.get(channel_id, 0)
        if used >= len(self._delays):
            return None
        self._used[channel_id] = used + 1
        return self._delays[used]


def should_fallback(exc: Exception) -> bool:
    """Only retry failures that indicate an unavailable provider/channel."""
    return normalize_upstream_error(exc).fallback_allowed


def retry_after_seconds(exc: Exception) -> float | None:
    return normalize_upstream_error(exc).retry_after_seconds


def set_routing_headers(
    response, channel, requested_model: str, fallback: bool, fallback_reason: str | None = None
) -> None:
    # HTTP headers are latin-1 at best; non-ASCII channel names degrade to an
    # ASCII label instead of crashing header encoding.
    name = channel.name or ""
    if not name.isascii():
        name = f"channel-{channel.id}"
    response.headers["X-Rotor-Channel"] = name
    response.headers["X-Rotor-Provider-Model"] = (
        (channel.model_mapping or {}).get(requested_model, requested_model)
    )
    if fallback:
        response.headers["X-Rotor-Fallback"] = "true"
        if fallback_reason:
            response.headers["X-Rotor-Fallback-Reason"] = fallback_reason
