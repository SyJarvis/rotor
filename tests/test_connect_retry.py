"""Connect-class failures retry the same channel and cool it briefly."""

from types import SimpleNamespace
from time import time

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
