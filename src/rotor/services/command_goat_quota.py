"""Command Code subscription information from the official CLI's alpha API."""
import asyncio
from datetime import datetime
import math
import time
from urllib.parse import urlsplit

import httpx

from rotor.schemas.channel import (
    ChannelUsageResponse,
    ChannelUsageWindow,
    CommandGoatCredits,
    CommandGoatSubscription,
    CommandGoatSummary,
    CommandGoatUsage,
    CommandGoatWindow,
)

API_ORIGIN = "https://api.commandcode.ai"
HTTP_TIMEOUT = 15.0


class CommandGoatQuotaError(Exception):
    """Safe query failure; never contains an upstream body or credential."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def is_command_goat_quota_channel(channel) -> bool:
    if getattr(channel, "type", None) != "command" or getattr(channel, "auth_type", None) != "api_key":
        return False
    try:
        url = urlsplit(getattr(channel, "base_url", None) or "")
        return (
            url.scheme == "https"
            and url.hostname == "api.commandcode.ai"
            and url.port in (None, 443)
            and url.username is None
            and url.password is None
        )
    except ValueError:
        return False


def _invalid() -> CommandGoatQuotaError:
    return CommandGoatQuotaError("Command Goat returned an invalid subscription payload")


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _number(data: dict, key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid()
    try:
        number = float(value)
    except OverflowError:
        raise _invalid() from None
    if not math.isfinite(number):
        raise _invalid()
    return number


def _text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid()
    return value


def _date(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    value = _text(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid() from None
    if parsed.tzinfo is None:
        raise _invalid()
    return value


def _subscription(payload: dict) -> CommandGoatSubscription | None:
    payload = _object(payload)
    if payload.get("success") is not True or "data" not in payload:
        raise _invalid()
    if payload["data"] is None:
        return None
    data = _object(payload["data"])
    cancel = data.get("cancelAtPeriodEnd")
    if cancel is not None and not isinstance(cancel, bool):
        raise _invalid()
    return CommandGoatSubscription(
        plan_id=_text(data.get("planId")),
        status=_text(data.get("status")),
        current_period_start=_date(data, "currentPeriodStart"),
        current_period_end=_date(data, "currentPeriodEnd"),
        cancel_at_period_end=cancel,
        canceled_at=_date(data, "canceledAt"),
        ended_at=_date(data, "endedAt"),
    )


def _window(value) -> CommandGoatWindow | None:
    if value is None:
        return None
    data = _object(value)
    used, cap = _number(data, "used"), _number(data, "cap")
    if used < 0 or not isinstance(data.get("exceeded"), bool):
        raise _invalid()
    reset = None if data.get("resetAt") is None else _number(data, "resetAt")
    if reset is not None and reset < 0:
        raise _invalid()
    return CommandGoatWindow(
        used=used, cap=cap, exceeded=data["exceeded"],
        reset_at=reset / 1000 if reset else None,
    )


def _shared_window(window: CommandGoatWindow | None, seconds: int, now: float) -> ChannelUsageWindow | None:
    if window is None:
        return None
    return ChannelUsageWindow(
        used_percent=min(window.used / window.cap, 1.0) * 100 if window.cap > 0 else None,
        window_seconds=seconds,
        reset_at=window.reset_at,
        reset_after_seconds=max(0, round(window.reset_at - now)) if window.reset_at is not None else None,
    )


def parse_command_goat_usage(
    subscription_payload: dict, credits_payload: dict, summary_payload: dict, *, fetched_at: float,
) -> ChannelUsageResponse:
    subscription = _subscription(subscription_payload)
    credits_payload, summary_payload = _object(credits_payload), _object(summary_payload)
    for payload in (subscription_payload, credits_payload, summary_payload):
        if ("success" in payload and payload["success"] is not True) or payload.get("error") is not None:
            raise _invalid()
    balances = _object(credits_payload.get("credits"))
    limits = credits_payload.get("windowLimits")
    limited = None
    five_hour = weekly = None
    if limits is not None:
        limits = _object(limits)
        limited = limits.get("limited")
        if not isinstance(limited, bool):
            raise _invalid()
        five_hour, weekly = _window(limits.get("fiveHour")), _window(limits.get("weekly"))
        if limited and five_hour is None and weekly is None:
            raise _invalid()
    count = summary_payload.get("totalCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise _invalid()
    basis = summary_payload.get("periodBasis")
    details = CommandGoatUsage(
        subscription=subscription,
        credits=CommandGoatCredits(
            monthly_remaining=_number(balances, "monthlyCredits"),
            purchased_remaining=_number(balances, "purchasedCredits"),
            free_remaining=_number(balances, "freeCredits"),
        ),
        window_limited=limited,
        five_hour=five_hour,
        weekly=weekly,
        summary=CommandGoatSummary(
            total_cost=_number(summary_payload, "totalCost"),
            total_monthly_credits=_number(summary_payload, "totalMonthlyCredits"),
            total_purchased_credits=_number(summary_payload, "totalPurchasedCredits"),
            total_free_credits=_number(summary_payload, "totalFreeCredits"),
            total_count=count,
            period_start=_date(summary_payload, "periodStart"),
            period_basis=_text(basis) if basis is not None else None,
        ),
    )
    windows = [window for window in (five_hour, weekly) if window is not None]
    return ChannelUsageResponse(
        plan=subscription.plan_id if subscription else None,
        subscription_status=subscription.status if subscription else "inactive",
        allowed=None,
        limit_reached=any(window.exceeded for window in windows) if windows else None,
        primary=_shared_window(five_hour, 18_000, fetched_at),
        secondary=_shared_window(weekly, 604_800, fetched_at),
        command_goat=details,
        fetched_at=fetched_at,
    )


async def fetch_command_goat_usage(client: httpx.AsyncClient, channel) -> ChannelUsageResponse:
    if not is_command_goat_quota_channel(channel):
        raise CommandGoatQuotaError("Channel is not a Command Goat API-key channel")
    key = getattr(channel, "key", None)
    if not isinstance(key, str) or not key.strip():
        raise CommandGoatQuotaError("Command Goat API key is missing")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    async def get(path: str, **params) -> dict:
        try:
            response = await client.get(
                API_ORIGIN + path, headers=headers,
                params={key: value for key, value in params.items() if value is not None},
                timeout=HTTP_TIMEOUT, follow_redirects=False,
            )
        except (httpx.HTTPError, TimeoutError):
            raise CommandGoatQuotaError("Command Goat subscription query is temporarily unavailable", 503) from None
        if response.status_code in (401, 403):
            raise CommandGoatQuotaError("Command Goat subscription authentication failed")
        if response.status_code == 429 or response.status_code >= 500:
            raise CommandGoatQuotaError("Command Goat subscription query is temporarily unavailable", 503)
        if not response.is_success:
            raise CommandGoatQuotaError("Command Goat subscription query failed")
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            raise _invalid() from None
        payload = _object(payload)
        if ("success" in payload and payload["success"] is not True) or payload.get("error") is not None:
            raise _invalid()
        return payload

    whoami = await get("/alpha/whoami", limits="1")
    if whoami.get("success") is not True or "org" not in whoami:
        raise _invalid()
    org = whoami["org"]
    org_id = None if org is None else _text(_object(org).get("id"))
    subscription_payload, credits_payload = await asyncio.gather(
        get("/alpha/billing/subscriptions", orgId=org_id),
        get("/alpha/billing/credits", orgId=org_id),
    )
    subscription = _subscription(subscription_payload)
    summary_payload = await get(
        "/alpha/usage/summary", orgId=org_id,
        since=subscription.current_period_start if subscription else None,
    )
    return parse_command_goat_usage(
        subscription_payload, credits_payload, summary_payload, fetched_at=time.time(),
    )
