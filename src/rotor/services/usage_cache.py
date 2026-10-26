"""Shared in-memory cache of channel plan-usage snapshots.

The admin usage endpoint stores snapshots here after talking to the
provider; the gateway usage endpoint reads them so agents can observe
upstream plan windows without the gateway path ever calling a provider.
"""
import time

from rotor.schemas.channel import ChannelUsageResponse

CACHE_TTL_SECONDS = 90.0

_entries: dict[int, tuple[float, ChannelUsageResponse]] = {}


def store(channel_id: int, usage: ChannelUsageResponse, *, now: float | None = None) -> None:
    """Cache a fresh snapshot for a channel."""
    _entries[channel_id] = (time.time() if now is None else now, usage)


def fresh(channel_id: int, *, now: float | None = None) -> ChannelUsageResponse | None:
    """Return the cached snapshot for a channel while it is still fresh."""
    entry = _entries.get(channel_id)
    if entry is None:
        return None
    stored_at, usage = entry
    moment = time.time() if now is None else now
    return usage if moment - stored_at < CACHE_TTL_SECONDS else None


def all_fresh(*, now: float | None = None) -> dict[int, ChannelUsageResponse]:
    """Return every still-fresh snapshot, keyed by channel id."""
    moment = time.time() if now is None else now
    return {
        channel_id: usage
        for channel_id, (stored_at, usage) in _entries.items()
        if moment - stored_at < CACHE_TTL_SECONDS
    }
