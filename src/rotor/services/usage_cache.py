"""Process-local channel usage snapshots, bound to the configuration fetched."""
import hashlib
import json
import time
from contextlib import contextmanager

from rotor.schemas.channel import ChannelUsageResponse

CACHE_TTL_SECONDS = 90.0

# Generation fences requests spanning local configuration changes, including ABA.
_entries: dict[int, tuple[float, ChannelUsageResponse, str | None, int]] = {}
_generations: dict[int, int] = {}
_mutating: dict[int, int] = {}


@contextmanager
def mutation(channel_id: int):
    """Fence requests throughout a local configuration transaction, even on rollback."""
    _mutating[channel_id] = _mutating.get(channel_id, 0) + 1
    invalidate(channel_id)
    try:
        yield
    finally:
        invalidate(channel_id)
        _mutating[channel_id] -= 1
        if not _mutating[channel_id]:
            del _mutating[channel_id]


def changing(channel_id: int) -> bool:
    return bool(_mutating.get(channel_id))


def generation(channel_id: int) -> int:
    return _generations.get(channel_id, 0)


def identity(channel, *, include_oauth_tokens: bool = True) -> str:
    """Fingerprint quota-relevant configuration without retaining credentials."""
    names = ("type", "auth_type", "key", "base_url", "protocol", "extra",
             "oauth_account_id", "created_at", "enabled", "models")
    if include_oauth_tokens:
        names += ("oauth_access_token", "oauth_refresh_token")
    data = [getattr(channel, name, None) for name in names]
    encoded = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def invalidate(channel_id: int) -> None:
    """Discard snapshots and fence all in-flight local requests."""
    _generations[channel_id] = generation(channel_id) + 1
    _entries.pop(channel_id, None)


def store(channel_id: int, usage: ChannelUsageResponse, *, channel=None,
          expected_generation: int | None = None, now: float | None = None) -> bool:
    """Publish only if no local invalidation occurred since the fetch began."""
    current = generation(channel_id)
    if changing(channel_id) or (expected_generation is not None and expected_generation != current):
        return False
    _entries[channel_id] = (time.time() if now is None else now, usage,
                            identity(channel) if channel is not None else None, current)
    return True


def fresh(channel_id: int, *, channel=None, now: float | None = None) -> ChannelUsageResponse | None:
    entry = _entries.get(channel_id)
    if entry is None or changing(channel_id):
        return None
    stored_at, usage, stored_identity, version = entry
    moment = time.time() if now is None else now
    if moment - stored_at >= CACHE_TTL_SECONDS or version != generation(channel_id):
        return None
    if channel is not None and stored_identity != identity(channel):
        return None
    return usage


def all_fresh(*, now: float | None = None) -> dict[int, ChannelUsageResponse]:
    """Return candidates; consumers must validate identity against current DB rows."""
    moment = time.time() if now is None else now
    return {channel_id: usage for channel_id in list(_entries)
            if (usage := fresh(channel_id, now=moment)) is not None}
