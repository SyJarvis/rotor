"""Zhipu Coding Plan quota parsing and channel matching."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from rotor.services.zhipu_quota import (
    ZhipuQuotaError,
    fetch_zhipu_usage,
    is_zhipu_quota_channel,
    parse_zhipu_usage,
    zhipu_quota_host,
)


def _payload(limits, level="pro", success=True):
    return {"code": 200, "msg": "操作成功", "data": {"limits": limits, "level": level}, "success": success}


def test_quota_host_matches_data_plane_domains():
    assert zhipu_quota_host("https://open.bigmodel.cn/api/coding/paas/v4") == "https://open.bigmodel.cn"
    assert zhipu_quota_host("https://api.z.ai/api/anthropic") == "https://api.z.ai"
    assert zhipu_quota_host("https://api.openai.com/v1") is None


def test_is_zhipu_quota_channel_ignores_oauth():
    zhipu = SimpleNamespace(auth_type="api_key", base_url="https://open.bigmodel.cn/api/coding/paas/v4")
    other = SimpleNamespace(auth_type="api_key", base_url="https://api.openai.com/v1")
    oauth_zhipu = SimpleNamespace(auth_type="oauth", base_url="https://open.bigmodel.cn")
    assert is_zhipu_quota_channel(zhipu) is True
    assert is_zhipu_quota_channel(other) is False
    assert is_zhipu_quota_channel(oauth_zhipu) is False


def test_parse_classifies_windows_by_unit_not_order():
    now = 1_800_000_000.4
    payload = _payload([
        {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 8, "nextResetTime": 1_805_000_000_750},
        {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 12, "nextResetTime": 1_800_000_300_750},
    ])
    usage = parse_zhipu_usage(payload, fetched_at=now)
    assert usage.plan == "pro"
    assert usage.primary.used_percent == 12
    assert usage.primary.window_seconds == 18_000
    assert usage.primary.reset_at == pytest.approx(1_800_000_300.75)
    # fractional deltas must land in the int reset_after_seconds field
    assert usage.primary.reset_after_seconds == 300
    assert usage.secondary.reset_at == pytest.approx(1_805_000_000.75)
    assert usage.secondary.used_percent == 8
    assert usage.limit_reached is False
    assert usage.allowed is True


def test_parse_falls_back_to_credit_limit_entries():
    payload = _payload([
        {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 100, "nextResetTime": 1_800_600_000_000},
        {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 40, "nextResetTime": 1_805_000_000_000},
    ])
    usage = parse_zhipu_usage(payload, fetched_at=1_800_000_000.0)
    assert usage.limit_reached is True
    assert usage.allowed is False
    assert usage.primary.used_percent == 100


def test_parse_rejects_invalid_payloads():
    with pytest.raises(ZhipuQuotaError):
        parse_zhipu_usage({"data": {"limits": []}}, fetched_at=1.0)
    with pytest.raises(ZhipuQuotaError):
        parse_zhipu_usage({"data": "broken"}, fetched_at=1.0)


def test_fetch_uses_bare_key_and_reports_business_errors():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_payload(
            [{"type": "TOKENS_LIMIT", "unit": 3, "percentage": 5, "nextResetTime": None}]
        , success=False))

    channel = SimpleNamespace(
        auth_type="api_key",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        key="test-key",
        extra={},
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ZhipuQuotaError):
        asyncio.run(fetch_zhipu_usage(client, channel))
    # The quota endpoint rejects Bearer-prefixed keys.
    assert seen["authorization"] == "test-key"
    assert seen["url"] == "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    asyncio.run(client.aclose())
