"""Background refresher that keeps channel plan-usage snapshots current.

Quota state — not a deadline guessed when an error arrives — drives
channel-wide cooldowns: a fresh snapshot reporting an exhausted window cools
every model on the channel until the window resets, and a healthy snapshot
clears plan-limit cooldowns immediately, so rolling windows that recover
early (Zhipu ages usage out instead of resetting on a fixed boundary) are
never held hostage by a stale horizon.
"""
import asyncio
import logging
import time

import httpx

from rotor.database import async_session_maker
from rotor.models.channel import Channel
from rotor.services import usage_cache
from rotor.services.chatgpt_oauth import codex_usage_snapshot, fetch_codex_usage, resolve_oauth_credentials
from rotor.services.zhipu_quota import fetch_zhipu_usage, is_zhipu_quota_channel

logger = logging.getLogger("rotor.quota_refresher")

# Per-channel floor between upstream quota calls, even under error storms.
MIN_INTERVAL_SECONDS = 60.0
# While a plan is limited: first recheck, then exponential, capped.
BACKOFF_START_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 600.0
# Safety-net horizon when a limited snapshot carries no usable reset time.
LIMITED_HORIZON_FALLBACK_SECONDS = 30.0
LIMIT_HORIZON_MAX_SECONDS = 6 * 3600.0
# Recheck shortly after the snapshot's own reset deadline (wall clock).
RESET_MARGIN_SECONDS = 30.0
TICK_SECONDS = 5.0


def _limit_horizon(usage, now: float) -> float:
    """How long the plan-limit cooldown should last, as a safety net."""
    primary = usage.primary
    if primary is None:
        return LIMITED_HORIZON_FALLBACK_SECONDS
    if primary.reset_at:
        return min(max(primary.reset_at - now, 0.0), LIMIT_HORIZON_MAX_SECONDS)
    if primary.reset_after_seconds is not None:
        return min(max(primary.reset_after_seconds, 0.0), LIMIT_HORIZON_MAX_SECONDS)
    return LIMITED_HORIZON_FALLBACK_SECONDS


def apply_usage(channel, usage, *, now: float | None = None) -> None:
    """Publish a snapshot and reconcile channel-wide cooldowns with it."""
    from rotor.gateway.routing import routing_engine  # local import: routing imports this module

    moment = time.time() if now is None else now
    usage_cache.store(channel.id, usage, now=moment)
    if usage.limit_reached:
        horizon = _limit_horizon(usage, moment)
        for model in {*(channel.models or [])}:
            routing_engine.mark_unavailable(model, channel, horizon, reason="plan_limit")
        logger.info(
            "channel %s (%s) plan-limited: cooling %d model(s) for %.0fs",
            channel.id, channel.name, len(channel.models or []), horizon,
        )
    else:
        cleared = routing_engine.clear_cooldown(None, channel.id, reason="plan_limit")
        if cleared:
            logger.info(
                "channel %s (%s) plan healthy: cleared %d plan-limit cooldown(s)",
                channel.id, channel.name, len(cleared),
            )


async def fetch_usage(channel, client: httpx.AsyncClient):
    """Fetch the quota snapshot for a channel; None when it has no provider."""
    if channel.auth_type == "oauth":
        credential = await resolve_oauth_credentials(channel, client)
        payload = await fetch_codex_usage(client, credential)
        return codex_usage_snapshot(payload, fetched_at=time.time())
    if is_zhipu_quota_channel(channel):
        return await fetch_zhipu_usage(client, channel)
    return None


class QuotaRefresher:
    """Schedules and runs per-channel quota refreshes with bounded backoff."""

    def __init__(self) -> None:
        self._next_at: dict[int, float] = {}
        self._backoff: dict[int, float] = {}
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task | None = None

    def schedule(self, channel_id: int) -> None:
        """Request a refresh; the per-channel floor still applies."""
        now = time.monotonic()
        if self._next_at.get(channel_id, 0.0) > now:
            return  # a refresh is already pending
        self._next_at[channel_id] = now + MIN_INTERVAL_SECONDS
        self._wakeup.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="quota-refresher")
            logger.info("quota refresher started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("quota refresher stopped")

    async def _run(self) -> None:
        while True:
            now = time.monotonic()
            pending = [at for at in self._next_at.values() if at > now]
            delay = min(pending) - now if pending else TICK_SECONDS
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=max(delay, 0.01))
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()
            for channel_id, due_at in sorted(self._next_at.items()):
                if due_at > time.monotonic():
                    continue
                self._next_at.pop(channel_id, None)
                try:
                    await self._refresh(channel_id)
                except Exception:
                    # A failed refresh must not strand a limited channel until
                    # its safety-net horizon; retry on the current backoff.
                    logger.exception("quota refresh for channel %s failed", channel_id)
                    self._next_at[channel_id] = (
                        time.monotonic() + self._backoff.get(channel_id, BACKOFF_START_SECONDS)
                    )

    async def _refresh(self, channel_id: int) -> None:
        async with async_session_maker() as db:
            channel = await db.get(Channel, channel_id)
        if channel is None or not channel.enabled:
            self._backoff.pop(channel_id, None)
            return
        async with httpx.AsyncClient(timeout=15.0) as client:
            usage = await fetch_usage(channel, client)
        if usage is None:
            return
        apply_usage(channel, usage)
        if not usage.limit_reached:
            self._backoff.pop(channel_id, None)
            return
        # Still limited: recheck with backoff, but never later than shortly
        # after the window's own reset deadline so recovery is detected fast.
        wait = min(self._backoff.get(channel_id, BACKOFF_START_SECONDS), BACKOFF_MAX_SECONDS)
        self._backoff[channel_id] = min(wait * 2, BACKOFF_MAX_SECONDS)
        target = time.monotonic() + wait
        primary = usage.primary
        if primary is not None and primary.reset_at:
            reset_in = primary.reset_at - time.time()
            if 0.0 < reset_in < LIMIT_HORIZON_MAX_SECONDS:
                target = min(target, time.monotonic() + reset_in + RESET_MARGIN_SECONDS)
        self._next_at[channel_id] = max(target, time.monotonic() + MIN_INTERVAL_SECONDS)


refresher = QuotaRefresher()
