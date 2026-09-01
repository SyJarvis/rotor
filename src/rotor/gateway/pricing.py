"""Calculate observed request cost from explicit, versioned channel tariffs."""

from dataclasses import dataclass
from datetime import datetime, time, timezone
import math
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class UsageLike(Protocol):
    completion_tokens: int
    uncached_input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    usage_source: str


@dataclass(frozen=True, slots=True)
class UsageCost:
    status: str
    input_cost: float = 0.0
    output_cost: float = 0.0
    total_cost: float = 0.0
    currency: str | None = None
    tariff_version: str | None = None
    tariff_period: str | None = None
    snapshot: dict[str, Any] | None = None


def calculate_usage_cost(
    channel: Any,
    *,
    logical_model: str,
    provider_model: str | None,
    usage: UsageLike,
    at: datetime | float | None = None,
) -> UsageCost:
    if usage.usage_source != "provider":
        return UsageCost(status="unknown")

    extra = getattr(channel, "extra", None)
    if extra is None:
        return UsageCost(status="unknown")
    if not isinstance(extra, Mapping):
        return UsageCost(status="invalid_tariff")
    tariffs = extra.get("tariffs")
    if tariffs is None:
        return UsageCost(status="unknown")
    if not isinstance(tariffs, Mapping):
        return UsageCost(status="invalid_tariff")

    tariff_key, tariff = _select_model_tariff(
        tariffs,
        logical_model,
        provider_model,
    )
    if tariff is None:
        return UsageCost(status="unknown")
    if not isinstance(tariff, Mapping):
        return UsageCost(status="invalid_tariff")

    try:
        version = _required_text(tariff, "version")
        currency = _required_text(tariff, "currency")
        timezone_name = str(tariff.get("timezone") or "UTC")
        local_timezone = ZoneInfo(timezone_name)
        rates = _rates(tariff.get("rates"))
        period_name, period_rates = _active_period(
            tariff.get("periods"),
            at=at,
            local_timezone=local_timezone,
        )
        rates.update(period_rates)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return UsageCost(status="invalid_tariff")

    snapshot = {
        "tariff_key": tariff_key,
        "version": version,
        "currency": currency,
        "timezone": timezone_name,
        "period": period_name,
        "rates": rates,
        "unit": "per_million_tokens",
    }

    cache_write_5m = max(int(usage.cache_write_5m_tokens or 0), 0)
    cache_write_1h = max(int(usage.cache_write_1h_tokens or 0), 0)
    cache_write_other = max(
        int(usage.cache_write_tokens or 0)
        - cache_write_5m
        - cache_write_1h,
        0,
    )
    buckets = (
        (max(int(usage.uncached_input_tokens or 0), 0), "input", None),
        (max(int(usage.cached_tokens or 0), 0), "cache_read", None),
        (cache_write_other, "cache_write", None),
        (cache_write_5m, "cache_write_5m", "cache_write"),
        (cache_write_1h, "cache_write_1h", "cache_write"),
    )
    input_cost = 0.0
    for tokens, rate_name, fallback_name in buckets:
        if tokens == 0:
            continue
        rate = rates.get(rate_name)
        if rate is None and fallback_name is not None:
            rate = rates.get(fallback_name)
        if rate is None:
            return UsageCost(
                status="incomplete_tariff",
                currency=currency,
                tariff_version=version,
                tariff_period=period_name,
                snapshot=snapshot,
            )
        input_cost += tokens * rate / 1_000_000

    output_tokens = max(int(usage.completion_tokens or 0), 0)
    output_rate = rates.get("output")
    if output_tokens and output_rate is None:
        return UsageCost(
            status="incomplete_tariff",
            currency=currency,
            tariff_version=version,
            tariff_period=period_name,
            snapshot=snapshot,
        )
    output_cost = output_tokens * (output_rate or 0.0) / 1_000_000
    return UsageCost(
        status="calculated",
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=input_cost + output_cost,
        currency=currency,
        tariff_version=version,
        tariff_period=period_name,
        snapshot=snapshot,
    )


def _select_model_tariff(
    tariffs: Mapping[str, Any],
    logical_model: str,
    provider_model: str | None,
) -> tuple[str, Any]:
    for key in (provider_model, logical_model, "*"):
        if key is not None and key in tariffs:
            return str(key), tariffs[key]
    return "", None


def _required_text(tariff: Mapping[str, Any], key: str) -> str:
    value = tariff.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"tariff {key} is required")
    return value.strip()


def _rates(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("tariff rates must be an object")
    normalized: dict[str, float] = {}
    for name, raw_rate in value.items():
        rate = float(raw_rate)
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("tariff rates must be finite and non-negative")
        normalized[str(name)] = rate
    return normalized


def _active_period(
    value: Any,
    *,
    at: datetime | float | None,
    local_timezone: ZoneInfo,
) -> tuple[str, dict[str, float]]:
    if value is None:
        return "default", {}
    if not isinstance(value, list):
        raise ValueError("tariff periods must be a list")
    observed_at = datetime.now(timezone.utc) if at is None else at
    if isinstance(observed_at, (int, float)):
        observed_at = datetime.fromtimestamp(observed_at, timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    local_time = observed_at.astimezone(local_timezone).time()
    for period in value:
        if not isinstance(period, Mapping):
            raise ValueError("tariff period must be an object")
        name = _required_text(period, "name")
        start = _parse_time(period.get("start"))
        end = _parse_time(period.get("end"))
        if start == end:
            raise ValueError("tariff period start and end must differ")
        active = (
            start <= local_time < end
            if start < end
            else local_time >= start or local_time < end
        )
        if active:
            return name, _rates(period.get("rates"))
    return "default", {}


def _parse_time(value: Any) -> time:
    if not isinstance(value, str):
        raise ValueError("tariff period time must be HH:MM")
    parsed = time.fromisoformat(value)
    if parsed.second or parsed.microsecond:
        raise ValueError("tariff period time must be HH:MM")
    return parsed
