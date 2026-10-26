"""Plan-level usage limits (ChatGPT usage_limit_reached) must pause the whole channel."""

from time import time
from types import SimpleNamespace

import httpx
import pytest

from rotor.core.exceptions import _plan_reset_seconds, normalize_upstream_error
from rotor.gateway.routing import RoutingEngine


def _limit_body(resets_at=None, resets_in=None):
    error = {
        "type": "usage_limit_reached",
        "message": "The usage limit has been reached",
        "plan_type": "plus",
        "limit_window_minutes": 300,
    }
    if resets_at is not None:
        error["resets_at"] = resets_at
    if resets_in is not None:
        error["resets_in_seconds"] = resets_in
    return {"error": error}


def _status_error(body, code=429):
    response = httpx.Response(
        code, request=httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses"), json=body
    )
    return httpx.HTTPStatusError("limit", request=response.request, response=response)


def _channel(models):
    return SimpleNamespace(
        id=7,
        priority=1,
        weight=1,
        enabled=True,
        extra={},
        protocol="openai",
        models=models,
    )


def test_plan_reset_prefers_absolute_deadline_over_relative_field():
    seconds = _plan_reset_seconds(_limit_body(resets_at=time() + 3000, resets_in=7))
    assert seconds == pytest.approx(3000, abs=5)


def test_plan_reset_falls_back_clamps_and_ignores_other_errors():
    assert _plan_reset_seconds(_limit_body(resets_in=120)) == pytest.approx(120, abs=1)
    assert _plan_reset_seconds(_limit_body(resets_in=10**9)) == 6 * 3600
    assert _plan_reset_seconds(_limit_body(resets_at=time() - 10, resets_in=90)) == pytest.approx(90, abs=1)
    assert _plan_reset_seconds({"error": {"type": "server_error"}}) is None
    assert _plan_reset_seconds(None) is None


def test_normalize_marks_plan_limit_channel_wide():
    fact = normalize_upstream_error(_status_error(_limit_body(resets_at=time() + 3000)))
    assert fact.channel_wide_cooldown is True
    assert fact.fallback_allowed is True
    assert fact.retry_same_channel is False
    assert fact.retry_after_seconds == pytest.approx(3000, abs=5)


def test_normalize_plain_rate_limit_stays_model_scoped():
    fact = normalize_upstream_error(_status_error({"error": {"type": "server_error"}}))
    assert fact.channel_wide_cooldown is False
    assert fact.retry_same_channel is True


def test_channel_wide_cooldown_covers_every_model():
    engine = RoutingEngine()
    channel = _channel(["gpt-6-sol", "gpt-6-luna"])
    engine.mark_unavailable("gpt-6-sol", channel, 3000, channel_wide=True)
    for model in ("gpt-6-sol", "gpt-6-luna"):
        reasons = engine.diagnose([channel], model, required_capabilities=set())[0]["reasons"]
        assert "cooldown" in reasons


def test_mark_failure_cools_whole_channel_on_plan_limit():
    engine = RoutingEngine()
    channel = _channel(["gpt-6-sol", "gpt-6-luna"])
    engine.mark_failure("gpt-6-sol", channel, _status_error(_limit_body(resets_at=time() + 3000)))
    reasons = engine.diagnose([channel], "gpt-6-luna", required_capabilities=set())[0]["reasons"]
    assert "cooldown" in reasons
