"""Read-only OpenCode Go usage, subscription and workspace balance snapshots."""
import asyncio
from datetime import datetime
import math
import re
import time
from urllib.parse import urlsplit

import httpx

from rotor.schemas.channel import (
    ChannelUsageResponse,
    ChannelUsageWindow,
    OpenCodeAccess,
    OpenCodeAllowanceWindow,
    OpenCodeBilling,
    OpenCodeSources,
    OpenCodeSubscription,
    OpenCodeUsage,
    OpenCodeUsageWindow,
    OpenCodeUsageWindows,
)

API_ORIGIN = "https://opencode.ai"
HTTP_TIMEOUT = 15.0


class OpenCodeQuotaError(Exception):
    """Safe aggregate failure with no upstream body, identity or credential."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def is_opencode_quota_channel(channel) -> bool:
    if getattr(channel, "type", None) != "opencode" or getattr(channel, "auth_type", None) != "api_key":
        return False
    raw = getattr(channel, "base_url", None)
    if not isinstance(raw, str) or any(char.isspace() for char in raw) or "?" in raw or "#" in raw:
        return False
    try:
        url = urlsplit(raw)
        return (
            url.scheme == "https" and url.hostname == "opencode.ai"
            and url.port in (None, 443) and url.username is None and url.password is None
            and url.path in ("/zen/go/v1", "/zen/go/v1/")
        )
    except ValueError:
        return False


class _InvalidResponse(Exception):
    pass


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise _InvalidResponse()
    return value


def _text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _InvalidResponse()
    return value


def _boolean(value) -> bool:
    if not isinstance(value, bool):
        raise _InvalidResponse()
    return value


def _date(data: dict, key: str, *, nullable: bool = False) -> float | None:
    if key not in data:
        raise _InvalidResponse()
    value = data[key]
    if nullable and value is None:
        return None
    value = _text(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise _InvalidResponse()
        stamp = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise _InvalidResponse() from None
    if not math.isfinite(stamp):
        raise _InvalidResponse()
    return stamp


def _money(value, *, signed: bool = False) -> str:
    pattern = r"-?[0-9]+" if signed else r"[0-9]+"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise _InvalidResponse()
    return value


def _usage_window(value) -> OpenCodeUsageWindow:
    data = _object(value)
    status, percent = data.get("status"), data.get("percent")
    if status not in ("ok", "rate-limited") or isinstance(percent, bool) or not isinstance(percent, (int, float)):
        raise _InvalidResponse()
    if not 0 <= percent <= 100:
        raise _InvalidResponse()
    return OpenCodeUsageWindow(status=status, used_percent=percent, reset_at=_date(data, "resetsAt"))


def _usage(value) -> OpenCodeUsageWindows:
    data = _object(_object(value).get("usage"))
    return OpenCodeUsageWindows(
        five_hour=_usage_window(data.get("rolling")),
        weekly=_usage_window(data.get("weekly")),
        monthly=_usage_window(data.get("monthly")),
    )


def _allowance(value, *, kind: str) -> OpenCodeAllowanceWindow:
    data = _object(value)
    return OpenCodeAllowanceWindow(
        used_microcents=_money(data.get("usedMicroCents")),
        limit_microcents=_money(data.get("limitMicroCents")),
        starts_at=_date(data, "startsAt", nullable=kind == "five_hour") if kind != "monthly" else None,
        reset_at=_date(data, "resetsAt", nullable=kind == "five_hour"),
    )


def _subscription(value) -> OpenCodeSubscription | None:
    if value is None:
        return None
    data = _object(value)
    if "access" not in data:
        raise _InvalidResponse()
    access = None
    if data["access"] is not None:
        raw = _object(data["access"])
        start, end = _date(raw, "startsAt"), _date(raw, "endsAt")
        if end <= start:
            raise _InvalidResponse()
        meters = _object(raw.get("meters"))
        access = OpenCodeAccess(
            starts_at=start, ends_at=end,
            cancel_at_period_end=_boolean(raw.get("cancelAtPeriodEnd")),
            five_hour=_allowance(meters.get("fiveHour"), kind="five_hour"),
            weekly=_allowance(meters.get("week"), kind="weekly"),
            monthly=_allowance(meters.get("month"), kind="monthly"),
        )
    return OpenCodeSubscription(
        product=_text(data.get("product")), renewal_product=_text(data.get("renewalProduct")),
        use_balance=_boolean(data.get("useBalance")),
        cancel_at_period_end=_boolean(data.get("cancelAtPeriodEnd")),
        renewal_pending=_boolean(data.get("renewalPending")),
        resumability=_text(data["resumability"]) if "resumability" in data else None,
        access=access,
    )


def _billing(value) -> OpenCodeBilling:
    data = _object(value)
    if "creditLimitMicroCents" not in data:
        raise _InvalidResponse()
    credit = data["creditLimitMicroCents"]
    return OpenCodeBilling(
        billing_mode=_text(data.get("billingMode")), mode=_text(data.get("mode")),
        balance_microcents=_money(data.get("balanceMicroCents"), signed=True),
        available_microcents=_money(data.get("availableMicroCents"), signed=True),
        credit_limit_microcents=_money(credit) if credit is not None else None,
        can_purchase_credits=_boolean(data.get("canPurchaseCredits")),
        can_enable_auto_recharge=_boolean(data.get("canEnableAutoRecharge")),
    )


def _subscription_status(subscription: OpenCodeSubscription | None, now: float) -> str:
    if subscription is None or subscription.access is None:
        return "inactive"
    if now < subscription.access.starts_at:
        return "pending"
    return "active" if now < subscription.access.ends_at else "expired"


def _shared_window(window: OpenCodeUsageWindow, seconds: int, now: float) -> ChannelUsageWindow:
    return ChannelUsageWindow(
        used_percent=window.used_percent, window_seconds=seconds, reset_at=window.reset_at,
        reset_after_seconds=max(0, round(window.reset_at - now)),
    )


async def fetch_opencode_usage(client: httpx.AsyncClient, channel) -> ChannelUsageResponse:
    if not is_opencode_quota_channel(channel):
        raise OpenCodeQuotaError("Channel is not an OpenCode Go API-key channel")
    key = getattr(channel, "key", None)
    if not isinstance(key, str) or not key.strip():
        raise OpenCodeQuotaError("OpenCode API key is missing")
    if any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise OpenCodeQuotaError("OpenCode API key has an invalid format")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    async def get(path, parse):
        try:
            response = await client.get(
                API_ORIGIN + path, headers=headers, timeout=HTTP_TIMEOUT, follow_redirects=False,
            )
        except (httpx.HTTPError, TimeoutError):
            return "unavailable", None
        if response.status_code == 401:
            return "unauthorized", None
        if response.status_code == 403:
            return "forbidden", None
        if response.status_code == 429 or response.status_code >= 500:
            return "unavailable", None
        if not response.is_success:
            return "error", None
        try:
            payload = response.json()
            if isinstance(payload, dict) and (
                ("success" in payload and payload["success"] is not True) or payload.get("error") is not None
            ):
                raise _InvalidResponse()
            return "ok", parse(payload)
        except (ValueError, UnicodeDecodeError, _InvalidResponse):
            return "invalid_response", None

    (usage_state, usage), (subscription_state, subscription), (billing_state, billing) = await asyncio.gather(
        get("/zen/go/v1/usage", _usage),
        get("/console/api/go/status", _subscription),
        get("/console/api/billing/status", _billing),
    )
    if usage_state != "ok" and subscription_state != "ok":
        if usage_state == subscription_state == "unavailable":
            raise OpenCodeQuotaError("OpenCode subscription query is temporarily unavailable", 503)
        raise OpenCodeQuotaError("OpenCode subscription query failed")
    now = time.time()
    details = OpenCodeUsage(
        sources=OpenCodeSources(usage=usage_state, subscription=subscription_state, billing=billing_state),
        usage=usage, subscription=subscription, billing=billing,
    )
    return ChannelUsageResponse(
        plan=subscription.product if subscription else None,
        subscription_status=_subscription_status(subscription, now) if subscription_state == "ok" else None,
        allowed=None,
        # Admin snapshots report whether any Go subscription window is limited;
        # this does not imply free or balance-funded models are unavailable.
        limit_reached=any(window.status == "rate-limited" for window in (
            usage.five_hour, usage.weekly, usage.monthly,
        )) if usage else None,
        primary=_shared_window(usage.five_hour, 18_000, now) if usage else None,
        secondary=_shared_window(usage.weekly, 604_800, now) if usage else None,
        opencode=details, fetched_at=now,
    )
