import asyncio
from types import SimpleNamespace

import pytest

from rotor.gateway.accounting import AccountingService, StreamingUsageAccumulator


def test_extract_usage_anthropic_top_level_cache_fields():
    """Anthropic input_tokens is only the uncached remainder."""
    svc = AccountingService()
    data = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 300,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 120,
                "ephemeral_1h_input_tokens": 80,
            },
        }
    }
    result = svc.extract_usage(data)
    assert result.prompt_tokens == 600
    assert result.completion_tokens == 50
    assert result.total_tokens == 650
    assert result.uncached_input_tokens == 100
    assert result.cached_tokens == 300
    assert result.cache_write_tokens == 200
    assert result.cache_write_5m_tokens == 120
    assert result.cache_write_1h_tokens == 80


def test_extract_usage_openai_nested_cached_tokens_still_works():
    """OpenAI's prompt_tokens_details.cached_tokens path must continue to work."""
    svc = AccountingService()
    data = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {
                "cached_tokens": 70,
                "cache_write_tokens": 20,
            },
        }
    }
    result = svc.extract_usage(data)
    assert result.prompt_tokens == 100
    assert result.uncached_input_tokens == 10
    assert result.cached_tokens == 70
    assert result.cache_write_tokens == 20


def test_extract_usage_deepseek_hit_and_miss_tokens() -> None:
    svc = AccountingService()
    result = svc.extract_usage({
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 40,
        }
    })

    assert result.prompt_tokens == 120
    assert result.uncached_input_tokens == 40
    assert result.cached_tokens == 80
    assert result.cache_write_tokens == 0


def test_extract_usage_anthropic_in_details_fallback():
    """Some conversion paths put cached_tokens inside input_tokens_details."""
    svc = AccountingService()
    data = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 40},
        }
    }
    result = svc.extract_usage(data)
    assert result.cached_tokens == 40


def test_streaming_usage_passes_cached_tokens():
    """streaming_usage threads cached_tokens through."""
    svc = AccountingService()
    result = svc.streaming_usage(
        prompt_tokens=100,
        completion_tokens=50,
        has_provider_usage=True,
        cached_tokens=60,
        cache_write_tokens=20,
        uncached_input_tokens=20,
    )
    assert result.cached_tokens == 60
    assert result.prompt_tokens == 100
    assert result.cache_write_tokens == 20
    assert result.uncached_input_tokens == 20


def test_streaming_usage_no_provider_usage_returns_missing():
    svc = AccountingService()
    result = svc.streaming_usage(
        prompt_tokens=0,
        completion_tokens=0,
        has_provider_usage=False,
        cached_tokens=60,
    )
    assert result.usage_source == "missing"
    assert result.cached_tokens == 0


def test_streaming_usage_replaces_prompt_breakdown_as_one_snapshot() -> None:
    """A later detailed snapshot must replace an earlier inferred cache miss."""
    svc = AccountingService()
    usage = StreamingUsageAccumulator()

    usage.observe(svc.extract_usage({
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 0,
        }
    }))
    usage.observe(svc.extract_usage({
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 40,
        }
    }))

    result = usage.to_usage_data(svc)

    assert result.prompt_tokens == 120
    assert result.completion_tokens == 30
    assert result.cached_tokens == 80
    assert result.uncached_input_tokens == 40
    assert result.cached_tokens + result.uncached_input_tokens == 120


def test_streaming_usage_keeps_input_snapshot_on_output_only_update() -> None:
    """Anthropic sends input at message_start and output at message_delta."""
    svc = AccountingService()
    usage = StreamingUsageAccumulator()

    usage.observe(svc.extract_usage({
        "usage": {
            "input_tokens": 20,
            "cache_read_input_tokens": 70,
            "cache_creation_input_tokens": 10,
        }
    }))
    usage.observe(svc.extract_usage({"usage": {"output_tokens": 12}}))

    result = usage.to_usage_data(svc)

    assert result.prompt_tokens == 100
    assert result.completion_tokens == 12
    assert result.uncached_input_tokens == 20
    assert result.cached_tokens == 70
    assert result.cache_write_tokens == 10


def test_record_success_persists_usage_v2_cache_facts() -> None:
    class FakeDb:
        def __init__(self) -> None:
            self.added = []

        def add(self, value) -> None:
            self.added.append(value)

    service = AccountingService()
    usage = service.extract_usage({
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 70,
        }
    })
    token = SimpleNamespace(
        id=1,
        user_id="user-1",
        request_count=0,
        token_count=0,
        used_quota=0,
        quota=None,
        enabled=True,
        last_used_at=None,
    )
    channel = SimpleNamespace(
        id=2,
        type="anthropic",
        protocol="anthropic",
        extra={
            "cache_scope": "anthropic/account-a/cache",
            "capacity_scope": "anthropic/account-a/capacity",
            "billing_scope": "anthropic/account-a/billing",
            "tariffs": {
                "claude-provider-test": {
                    "version": "claude-test-v1",
                    "currency": "USD",
                    "rates": {
                        "input": 2.0,
                        "cache_read": 0.2,
                        "cache_write": 2.5,
                        "output": 12.0,
                    },
                }
            }
        },
    )
    db = FakeDb()

    asyncio.run(service.record_success(
        db,
        request_id="req-cache-facts",
        conversation_id="session-1",
        request_protocol="anthropic_messages",
        token=token,
        channel=channel,
        model="claude-test",
        provider_model="claude-provider-test",
        usage=usage,
        latency_ms=100,
        client_ip="127.0.0.1",
        capacity_snapshot={"x-ratelimit-remaining-requests": "9"},
    ))

    request_log, ledger = db.added
    assert request_log.prompt_tokens == 100
    assert request_log.uncached_input_tokens == 10
    assert request_log.cached_tokens == 70
    assert request_log.cache_write_tokens == 20
    assert request_log.usage_schema_version == "2"
    assert request_log.capacity_snapshot == {
        "x-ratelimit-remaining-requests": "9"
    }
    assert request_log.cost_status == "calculated"
    assert request_log.cache_scope == "anthropic/account-a/cache"
    assert request_log.capacity_scope == "anthropic/account-a/capacity"
    assert request_log.billing_scope == "anthropic/account-a/billing"
    assert request_log.tariff_version == "claude-test-v1"
    assert request_log.cost == pytest.approx(144 / 1_000_000)
    assert ledger.prompt_tokens == 100
    assert ledger.cache_write_tokens == 20
    assert ledger.usage_schema_version == "2"
    assert ledger.capacity_snapshot == {
        "x-ratelimit-remaining-requests": "9"
    }
    assert ledger.cost_status == "calculated"
    assert ledger.cache_scope == "anthropic/account-a/cache"
    assert ledger.capacity_scope == "anthropic/account-a/capacity"
    assert ledger.billing_scope == "anthropic/account-a/billing"
    assert ledger.total_cost == pytest.approx(144 / 1_000_000)
    assert ledger.tariff_snapshot["unit"] == "per_million_tokens"
    assert token.token_count == 105
