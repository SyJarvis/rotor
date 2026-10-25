"""Quota-driven channel cooldowns: reason tags, zhipu 1308, snapshot reconciliation."""

import asyncio
from time import time
from types import SimpleNamespace

import httpx
import pytest

from rotor.core.exceptions import _plan_reset_seconds, normalize_upstream_error
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.channel import ChannelUsageResponse, ChannelUsageWindow
from rotor.services import quota_refresher, usage_cache


def _channel(models, channel_id=7):
    return SimpleNamespace(
        id=channel_id,
        name="quota-test",
        priority=1,
        weight=1,
        enabled=True,
        extra={},
        protocol="openai",
        models=models,
    )


def _usage(limit_reached, reset_at=None, reset_after=None):
    return ChannelUsageResponse(
        plan="plus",
        allowed=not limit_reached,
        limit_reached=limit_reached,
        primary=ChannelUsageWindow(used_percent=100.0 if limit_reached else 7.0,
                                   window_seconds=18000, reset_at=reset_at,
                                   reset_after_seconds=reset_after),
        secondary=ChannelUsageWindow(used_percent=15.0, window_seconds=604800),
        fetched_at=time(),
    )


def _status_error(body, code=429):
    response = httpx.Response(
        code, request=httpx.Request("POST", "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"),
        json=body,
    )
    return httpx.HTTPStatusError("limit", request=response.request, response=response)


def _cooling_models(engine, channel, models):
    return [
        model for model in models
        if "cooldown" in engine.diagnose([channel], model, required_capabilities=set())[0]["reasons"]
    ]


def test_zhipu_plan_limit_code_is_recognized():
    error = _status_error({"error": {"code": "1308", "message": "已达到 5 小时的使用上限"}})
    fact = normalize_upstream_error(error)
    assert fact.plan_limit is True
    assert fact.channel_wide_cooldown is True
    assert fact.retry_after_seconds == pytest.approx(120, abs=1)
    # the OpenAI-style type detection is unchanged
    chatgpt = normalize_upstream_error(_status_error(
        {"error": {"type": "usage_limit_reached", "resets_in_seconds": 900}}))
    assert chatgpt.plan_limit is True and chatgpt.retry_after_seconds == pytest.approx(900, abs=1)
    assert _plan_reset_seconds({"error": {"code": "1308"}}) == pytest.approx(120, abs=1)
    assert _plan_reset_seconds({"error": {"code": "1113", "message": "别的错"}}) is None


def test_clear_cooldown_filters_by_reason():
    engine = RoutingEngine()
    channel = _channel(["glm-6", "gpt-6"])
    engine.mark_unavailable("glm-6", channel, 300, reason="plan_limit")
    engine.mark_unavailable("gpt-6", channel, 30)
    assert engine.clear_cooldown(None, channel.id, reason="plan_limit") == ["glm-6"]
    # transient cooldown survives a plan-limit clear...
    assert _cooling_models(engine, channel, ["gpt-6"]) == ["gpt-6"]
    assert _cooling_models(engine, channel, ["glm-6"]) == []
    # ...and a plain clear (manual operator reset) still removes everything
    assert engine.clear_cooldown(None, channel.id) == ["gpt-6"]


def test_transient_mark_extending_plan_cooldown_keeps_reason():
    engine = RoutingEngine()
    channel = _channel(["glm-6"])
    engine.mark_unavailable("glm-6", channel, 600, reason="plan_limit")
    # a later transient mark (default reason) must not downgrade the tag
    engine.mark_unavailable("glm-6", channel, 30)
    assert engine.clear_cooldown(None, channel.id, reason="plan_limit") == ["glm-6"]


def test_apply_usage_cools_and_lifts_plan_limit_only():
    from rotor.gateway.routing import routing_engine as engine

    channel = _channel(["glm-6", "glm-6-flash"])
    try:
        quota_refresher.apply_usage(channel, _usage(limit_reached=True, reset_at=time() + 1800))
        assert _cooling_models(engine, channel, ["glm-6", "glm-6-flash"]) == ["glm-6", "glm-6-flash"]
        assert usage_cache.fresh(channel.id) is not None

        # recovery lifts every plan-limit cooldown on the channel; a transient
        # mark that landed on top of one is subsumed by the longer horizon
        quota_refresher.apply_usage(channel, _usage(limit_reached=False))
        assert _cooling_models(engine, channel, ["glm-6", "glm-6-flash"]) == []

        # a transient failure after recovery is untouched by later healthy snapshots
        engine.mark_unavailable("glm-6-flash", channel, 30)
        quota_refresher.apply_usage(channel, _usage(limit_reached=False))
        assert _cooling_models(engine, channel, ["glm-6-flash"]) == ["glm-6-flash"]
        assert _cooling_models(engine, channel, ["glm-6"]) == []
    finally:
        engine.clear_cooldown(None, channel.id)
        usage_cache._entries.clear()


def test_limit_horizon_prefers_reset_at_with_clamp():
    horizon = quota_refresher._limit_horizon(_usage(limit_reached=True, reset_at=time() + 9 * 3600), time())
    assert horizon == pytest.approx(6 * 3600, abs=5)
    assert quota_refresher._limit_horizon(_usage(limit_reached=True, reset_after=900), time()) == pytest.approx(900, abs=1)
    assert quota_refresher._limit_horizon(_usage(limit_reached=True), time()) == 30.0


def test_mark_failure_schedules_refresh_for_plan_limits():
    engine = RoutingEngine()
    channel = _channel(["glm-6"], channel_id=11)
    scheduled = []
    refresher = quota_refresher.refresher

    def fake_schedule(channel_id):
        scheduled.append(channel_id)

    original = refresher.schedule
    refresher.schedule = fake_schedule
    try:
        engine.mark_failure("glm-6", channel, _status_error({"error": {"code": "1308"}}))
        engine.mark_failure("glm-6", channel, _status_error({"error": {"type": "server_error"}}))
    finally:
        refresher.schedule = original
        engine.clear_cooldown(None, channel.id)
    assert scheduled == [11]


def test_refresher_reschedules_while_limited_with_backoff(monkeypatch):
    import time as _time

    from rotor.gateway.routing import routing_engine

    refresher = quota_refresher.QuotaRefresher()
    channel = _channel(["glm-6"], channel_id=21)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, pk):
            return channel

    async def fake_fetch(ch, client):
        return _usage(limit_reached=True, reset_at=time() + 3600)

    monkeypatch.setattr(quota_refresher, "async_session_maker", lambda: _FakeSession())
    monkeypatch.setattr(quota_refresher, "fetch_usage", fake_fetch)
    try:
        asyncio.run(refresher._refresh(21))
        assert 21 in refresher._next_at
        assert 55.0 <= refresher._next_at[21] - _time.monotonic() <= 65.0
        # repeated refreshes grow the backoff up to the cap
        refresher._next_at.pop(21)
        for _ in range(6):
            asyncio.run(refresher._refresh(21))
            refresher._next_at[21] -= refresher._backoff[21]  # pretend time passed
        assert refresher._backoff[21] == quota_refresher.BACKOFF_MAX_SECONDS
    finally:
        routing_engine.clear_cooldown(None, 21, reason="plan_limit")
        usage_cache._entries.clear()


def test_schedule_applies_per_channel_floor():
    refresher = quota_refresher.QuotaRefresher()
    import time as _time
    refresher.schedule(31)
    first = refresher._next_at[31]
    assert first - _time.monotonic() == pytest.approx(quota_refresher.MIN_INTERVAL_SECONDS, abs=1)
    # a second trigger while one is pending does not pull it earlier
    refresher.schedule(31)
    assert refresher._next_at[31] == first
