"""Connect-failure backoff: sustained outages cool exponentially, success resets."""

from types import SimpleNamespace

import httpx

from rotor.gateway.fallback import CONNECT_COOLDOWN_BACKOFF, connect_cooldown_for_streak
from rotor.gateway.routing import RoutingEngine


def _channel(id=7):
    return SimpleNamespace(id=id, priority=1, weight=1, enabled=True, extra={},
                           protocol="openai", models=["m1"], key="k",
                           base_url="https://x.test/v1")


def test_backoff_ladder_values():
    assert connect_cooldown_for_streak(0) == 10.0
    assert connect_cooldown_for_streak(1) == 30.0
    assert connect_cooldown_for_streak(4) == 300.0
    assert connect_cooldown_for_streak(99) == 300.0
    assert connect_cooldown_for_streak(-1) == 10.0
    assert CONNECT_COOLDOWN_BACKOFF[0] == 10.0


def test_sustained_connect_failures_back_off():
    now = [100.0]
    engine = RoutingEngine(clock=lambda: now[0])
    channel = _channel()
    cooldowns = []
    for _ in range(6):
        engine.mark_failure("m1", channel, httpx.ConnectError("eof"))
        cooldowns.append(engine.diagnose([channel], "m1")[0]["cooldown_seconds"])
        # Let the cooldown expire naturally; the streak must survive expiry.
        now[0] += 400.0
    assert cooldowns == [10.0, 30.0, 60.0, 120.0, 300.0, 300.0]


def test_success_resets_streak():
    now = [100.0]
    engine = RoutingEngine(clock=lambda: now[0])
    channel = _channel()
    engine.mark_failure("m1", channel, httpx.ConnectError("eof"))
    now[0] += 400.0
    engine.mark_failure("m1", channel, httpx.ConnectError("eof"))
    assert engine.diagnose([channel], "m1")[0]["cooldown_seconds"] == 30.0
    engine.observe_result("m1", channel, success=True, latency_ms=5)
    now[0] += 400.0
    engine.mark_failure("m1", channel, httpx.ConnectError("eof"))
    assert engine.diagnose([channel], "m1")[0]["cooldown_seconds"] == 10.0


def test_non_connect_failure_does_not_touch_streak():
    now = [100.0]
    engine = RoutingEngine(clock=lambda: now[0])
    channel = _channel()
    response = httpx.Response(500, request=httpx.Request("POST", "https://x.test"))
    engine.mark_failure(
        "m1", channel,
        httpx.HTTPStatusError("bad", request=response.request, response=response),
    )
    now[0] += 400.0
    engine.mark_failure("m1", channel, httpx.ConnectError("eof"))
    # The 500 did not consume the connect streak, so this cools 10s.
    assert engine.diagnose([channel], "m1")[0]["cooldown_seconds"] == 10.0


def test_manual_clear_resets_streak():
    engine = RoutingEngine()
    channel = _channel()
    engine.mark_failure("m1", channel, httpx.ConnectError("eof"))
    engine.clear_cooldown("m1", channel.id)
    engine.mark_failure("m1", channel, httpx.ConnectError("eof"))
    # An explicit admin clear resets the backoff to the first rung.
    assert engine.diagnose([channel], "m1")[0]["cooldown_seconds"] == 10.0
