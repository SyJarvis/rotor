from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from rotor.gateway.accounting import UsageData
from rotor.gateway.pricing import calculate_usage_cost


def _channel(tariffs: dict) -> SimpleNamespace:
    return SimpleNamespace(extra={"tariffs": tariffs})


def _usage() -> UsageData:
    return UsageData(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        uncached_input_tokens=10,
        cached_tokens=70,
        cache_write_tokens=20,
        cache_write_5m_tokens=12,
        cache_write_1h_tokens=8,
    )


def test_static_tariff_calculates_each_cache_bucket() -> None:
    channel = _channel({
        "provider-model": {
            "version": "provider-2026-08-19",
            "currency": "USD",
            "rates": {
                "input": 2.0,
                "cache_read": 0.2,
                "cache_write": 2.5,
                "cache_write_5m": 2.5,
                "cache_write_1h": 4.0,
                "output": 12.0,
            },
        }
    })

    result = calculate_usage_cost(
        channel,
        logical_model="logical-model",
        provider_model="provider-model",
        usage=_usage(),
        at=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )

    assert result.status == "calculated"
    assert result.input_cost == pytest.approx(
        (10 * 2.0 + 70 * 0.2 + 12 * 2.5 + 8 * 4.0) / 1_000_000
    )
    assert result.output_cost == pytest.approx(5 * 12.0 / 1_000_000)
    assert result.total_cost == pytest.approx(
        result.input_cost + result.output_cost
    )
    assert result.tariff_version == "provider-2026-08-19"
    assert result.tariff_period == "default"


def test_daily_period_overrides_default_rates_in_configured_timezone() -> None:
    channel = _channel({
        "*": {
            "version": "deepseek-2026-08-19",
            "currency": "CNY",
            "timezone": "Asia/Shanghai",
            "rates": {
                "input": 4.0,
                "cache_read": 1.0,
                "cache_write": 4.0,
                "output": 8.0,
            },
            "periods": [{
                "name": "off_peak",
                "start": "00:30",
                "end": "08:30",
                "rates": {
                    "input": 2.0,
                    "cache_read": 0.5,
                    "cache_write": 2.0,
                    "output": 4.0,
                },
            }],
        }
    })

    off_peak = calculate_usage_cost(
        channel,
        logical_model="model",
        provider_model="model",
        usage=_usage(),
        at=datetime(2026, 8, 18, 17, 0, tzinfo=timezone.utc),
    )
    peak = calculate_usage_cost(
        channel,
        logical_model="model",
        provider_model="model",
        usage=_usage(),
        at=datetime(2026, 8, 19, 2, 0, tzinfo=timezone.utc),
    )

    assert off_peak.tariff_period == "off_peak"
    assert off_peak.snapshot["rates"]["input"] == 2.0
    assert peak.tariff_period == "default"
    assert peak.snapshot["rates"]["input"] == 4.0
    assert off_peak.total_cost < peak.total_cost


def test_epoch_timestamp_anchors_period_to_request_start() -> None:
    channel = _channel({
        "*": {
            "version": "anchored-v1",
            "currency": "USD",
            "timezone": "UTC",
            "rates": {
                "input": 2.0,
                "cache_read": 1.0,
                "cache_write": 2.0,
                "output": 4.0,
            },
            "periods": [{
                "name": "first_hour",
                "start": "00:00",
                "end": "01:00",
                "rates": {"input": 1.0},
            }],
        }
    })

    result = calculate_usage_cost(
        channel,
        logical_model="model",
        provider_model="model",
        usage=_usage(),
        at=datetime(2026, 8, 19, 0, 30, tzinfo=timezone.utc).timestamp(),
    )

    assert result.tariff_period == "first_hour"
    assert result.snapshot["rates"]["input"] == 1.0


def test_cross_midnight_period_is_supported() -> None:
    channel = _channel({
        "*": {
            "version": "night-v1",
            "currency": "USD",
            "timezone": "UTC",
            "rates": {"input": 2, "cache_read": 1, "cache_write": 2, "output": 4},
            "periods": [{
                "name": "night",
                "start": "22:00",
                "end": "06:00",
                "rates": {"input": 1},
            }],
        }
    })

    before_midnight = calculate_usage_cost(
        channel,
        logical_model="model",
        provider_model="model",
        usage=_usage(),
        at=datetime(2026, 8, 19, 23, 0, tzinfo=timezone.utc),
    )
    after_midnight = calculate_usage_cost(
        channel,
        logical_model="model",
        provider_model="model",
        usage=_usage(),
        at=datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc),
    )

    assert before_midnight.tariff_period == "night"
    assert after_midnight.tariff_period == "night"


def test_missing_tariff_or_required_rate_stays_unknown() -> None:
    no_tariff = calculate_usage_cost(
        SimpleNamespace(extra={}),
        logical_model="model",
        provider_model="model",
        usage=_usage(),
    )
    incomplete = calculate_usage_cost(
        _channel({
            "*": {
                "version": "incomplete-v1",
                "currency": "USD",
                "rates": {"input": 1.0},
            }
        }),
        logical_model="model",
        provider_model="model",
        usage=_usage(),
    )

    assert no_tariff.status == "unknown"
    assert incomplete.status == "incomplete_tariff"
    assert incomplete.total_cost == 0.0


def test_missing_provider_usage_is_not_treated_as_free() -> None:
    result = calculate_usage_cost(
        _channel({
            "*": {
                "version": "configured-v1",
                "currency": "USD",
                "rates": {
                    "input": 1.0,
                    "cache_read": 1.0,
                    "cache_write": 1.0,
                    "output": 1.0,
                },
            }
        }),
        logical_model="model",
        provider_model="model",
        usage=UsageData(usage_source="missing"),
    )

    assert result.status == "unknown"
    assert result.total_cost == 0.0


def test_invalid_timezone_does_not_break_request_accounting() -> None:
    result = calculate_usage_cost(
        _channel({
            "*": {
                "version": "bad-zone-v1",
                "currency": "USD",
                "timezone": "Mars/Olympus",
                "rates": {"input": 1.0, "cache_read": 1.0,
                          "cache_write": 1.0, "output": 1.0},
            }
        }),
        logical_model="model",
        provider_model="model",
        usage=_usage(),
    )

    assert result.status == "invalid_tariff"


@pytest.mark.parametrize(
    "extra",
    [
        ["not", "an", "object"],
        {"tariffs": ["not", "an", "object"]},
        {"tariffs": {"*": "not-an-object"}},
    ],
)
def test_malformed_tariff_container_is_observed_not_raised(extra) -> None:
    result = calculate_usage_cost(
        SimpleNamespace(extra=extra),
        logical_model="model",
        provider_model="model",
        usage=_usage(),
    )

    assert result.status == "invalid_tariff"
