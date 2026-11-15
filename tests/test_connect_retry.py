"""Connect-class failures retry the same channel and cool it briefly."""

from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from rotor.gateway.fallback import CONNECT_COOLDOWN_SECONDS, ConnectRetry, is_connect_failure
from rotor.gateway.routing import RoutingEngine


def _connect_timeout():
    return httpx.ConnectTimeout("timed out")


def _connect_error():
    return httpx.ConnectError("no route to host")


def test_is_connect_failure_classification():
    assert is_connect_failure(_connect_timeout()) is True
    assert is_connect_failure(_connect_error()) is True
    assert is_connect_failure(httpx.ReadTimeout("read timed out")) is False
    assert is_connect_failure(httpx.RemoteProtocolError("no response headers")) is False
    response = httpx.Response(429, request=httpx.Request("POST", "https://x.test"))
    assert is_connect_failure(
        httpx.HTTPStatusError("429", request=response.request, response=response)
    ) is False


def test_connect_retry_budget_is_per_channel():
    retry = ConnectRetry(delays=(0.5, 2.0))
    assert retry.plan_retry(9) == 0.5
    assert retry.plan_retry(9) == 2.0
    assert retry.plan_retry(9) is None
    assert retry.plan_retry(10) == 0.5


def test_mark_failure_gives_connect_errors_a_short_cooldown():
    engine = RoutingEngine()
    channel = SimpleNamespace(id=7, priority=1, weight=1, enabled=True, extra={}, protocol="openai",
                              models=["m1"])
    engine.mark_failure("m1", channel, _connect_timeout())
    reasons = engine.diagnose([channel], "m1", required_capabilities=set())[0]
    assert "cooldown" in reasons["reasons"]
    assert 0 < reasons["cooldown_seconds"] <= CONNECT_COOLDOWN_SECONDS


def test_connect_retry_after_expired_cooldown_is_not_blocked_by_release():
    now = [100.0]
    engine = RoutingEngine(clock=lambda: now[0])
    channel = SimpleNamespace(id=7, enabled=True, extra={"max_in_flight": 1})
    engine.mark_unavailable("model", channel, 1)
    now[0] += 1
    initial = engine.admit_attempt("model", channel)
    assert initial is not None
    retry_delay = ConnectRetry().plan_retry(channel.id)
    engine.release_attempt(initial)
    now[0] += retry_delay
    retry = engine.admit_attempt("model", channel)
    assert retry is not None
    assert engine.routing_state() == []
    engine.release_attempt(retry)


def test_channel_concurrency_cap_applies_to_different_model_aliases():
    engine = RoutingEngine()
    channel = SimpleNamespace(id=7, enabled=True, protocol="openai", extra={"max_in_flight": 1},
                              model_mapping={"alias-a": "provider", "alias-b": "provider"})
    initial = engine.admit_attempt("alias-a", channel)
    assert initial is not None
    assert engine.admit_attempt("alias-b", channel) is None
    diagnostic = engine.diagnose([channel], "alias-b")[0]
    assert diagnostic["in_flight"] == 1
    assert "concurrency_limit" in diagnostic["reasons"]
    engine.release_attempt(initial)
    engine.release_attempt(initial)
    retry = engine.admit_attempt("alias-b", channel)
    assert retry is not None
    engine.release_attempt(retry)


def test_channel_concurrency_cap_is_atomic_across_models():
    engine = RoutingEngine()
    channel = SimpleNamespace(id=7, enabled=True, extra={"max_in_flight": 3})
    with ThreadPoolExecutor(max_workers=16) as pool:
        admissions = list(pool.map(lambda i: engine.admit_attempt(f"alias-{i}", channel), range(64)))
    admitted = [value for value in admissions if value is not None]
    assert len(admitted) == 3
    for admission in admitted:
        engine.release_attempt(admission)
    assert engine._in_flight == {}
