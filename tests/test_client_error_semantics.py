"""Client-facing error semantics: structured 429, fallback reason, account gating."""

from time import time
from types import SimpleNamespace

import httpx
import pytest

from rotor.core.exceptions import ChannelsTemporarilyUnavailable, normalize_upstream_error
from rotor.gateway.fallback import set_routing_headers
from rotor.gateway.routing import RoutingEngine


def _channel(models=("glm-5.3",), channel_id=7, name="zhipuai"):
    return SimpleNamespace(id=channel_id, name=name, priority=1, weight=1,
                           enabled=True, extra={}, protocol="openai", models=list(models),
                           model_mapping={})


def _status_error(body, code=429):
    response = httpx.Response(
        code, request=httpx.Request("POST", "https://provider.example/v1/chat/completions"),
        json=body,
    )
    return httpx.HTTPStatusError("err", request=response.request, response=response)


def test_temporarily_unavailable_is_structured_429_with_local_reset():
    error = ChannelsTemporarilyUnavailable("glm-5.3", 42)
    payload = error.detail["error"]
    assert error.status_code == 429
    assert error.headers["Retry-After"] == "42"
    assert payload["code"] == "channels_temporarily_unavailable"
    assert payload["type"] == "rate_limit_error"
    assert payload["retry_after_seconds"] == 42
    # resets_at is the local deadline, not an upstream field
    assert payload["resets_at"] == pytest.approx(time() + 42, abs=5)


def test_plan_limited_variant_names_the_usage_window():
    payload = ChannelsTemporarilyUnavailable("glm-5.3", 1800, plan_limited=True).detail["error"]
    assert payload["type"] == "usage_limit_reached"
    assert "usage window" in payload["message"]


def test_cooldown_outlook_reports_plan_limit_domination():
    engine = RoutingEngine()
    zhipu = _channel()
    other = _channel(channel_id=8, name="backup")
    engine.mark_unavailable("glm-5.3", zhipu, 600, reason="plan_limit")
    engine.mark_unavailable("glm-5.3", other, 30)
    wait, plan_limited = engine.cooldown_outlook("glm-5.3", [zhipu, other])
    assert wait == 30  # advisory: the shortest recovery wins
    assert plan_limited is True
    engine.clear_cooldown(None, zhipu.id)
    engine.clear_cooldown(None, other.id)


def test_account_unsupported_model_400_allows_fallback():
    body = {"error": {"type": "invalid_request_error",
                      "message": "The requested model is not supported with a ChatGPT account"}}
    fact = normalize_upstream_error(_status_error(body, code=400))
    assert fact.code == "upstream_model_not_available_on_account"
    assert fact.fallback_allowed is True
    assert fact.retry_same_channel is False
    # plain 400s stay non-fallbackable
    plain = normalize_upstream_error(_status_error({"error": {"message": "bad parameter"}}, code=400))
    assert plain.fallback_allowed is False
    assert plain.code == "upstream_protocol_or_parameter_error"


def test_fallback_reason_header_set_only_on_fallback():
    response = httpx.Response(200)
    channel = _channel()
    set_routing_headers(response, channel, "glm-5.3", False, fallback_reason="zhipuai: upstream_rate_limited")
    assert "X-Rotor-Fallback-Reason" not in response.headers
    set_routing_headers(response, channel, "glm-5.3", True, fallback_reason="zhipuai: upstream_rate_limited")
    assert response.headers["X-Rotor-Fallback-Reason"] == "zhipuai: upstream_rate_limited"
    # non-ascii channel names degrade to channel-<id>; a missing reason omits the header
    wide = httpx.Response(200)
    unicode_channel = _channel(name="智谱")
    set_routing_headers(wide, unicode_channel, "glm-5.3", True,
                        fallback_reason=f"channel-{unicode_channel.id}: upstream_rate_limited")
    assert wide.headers["X-Rotor-Channel"] == "channel-7"
    assert wide.headers["X-Rotor-Fallback-Reason"] == "channel-7: upstream_rate_limited"
    bare = httpx.Response(200)
    set_routing_headers(bare, channel, "glm-5.3", True)
    assert "X-Rotor-Fallback-Reason" not in bare.headers
