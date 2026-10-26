"""Zhipu GLM Coding Plan subscription quota (rolling 5h/weekly windows).

Endpoint: GET {host}/api/monitor/usage/quota/limit — same host as the data
plane. Authentication sends the bare API key in ``Authorization`` (no Bearer
prefix; the quota endpoint rejects the prefixed form). Team plans require
``?type=2`` plus ``bigmodel-organization``/``bigmodel-project`` headers, which
Rotor channels express through ``extra.headers``.

``data.limits`` entries carry ``percentage`` as *used* percent and identify
their window by ``unit``: 3 = rolling 5-hour window, 6 = weekly window. Plans
report either TOKENS_LIMIT or CREDIT_LIMIT entries, so unit classification
must be tried on both; ordering by reset time is wrong because near the end of
a cycle the weekly window resets before the 5-hour one.
"""
import time

import httpx

from rotor.schemas.channel import ChannelUsageResponse, ChannelUsageWindow

QUOTA_PATH = "/api/monitor/usage/quota/limit"
HTTP_TIMEOUT = 15.0
# unit -> (window kind, window duration seconds)
_WINDOW_UNITS = {3: ("primary", 18_000), 6: ("secondary", 604_800)}


class ZhipuQuotaError(Exception):
    """Zhipu quota endpoint failed; ``status_code`` maps to the admin API."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def zhipu_quota_host(base_url: str | None) -> str | None:
    """Resolve the quota host from a channel's data-plane base URL."""
    host = (base_url or "").lower()
    if "bigmodel.cn" in host:
        return "https://open.bigmodel.cn"
    if "z.ai" in host:
        return "https://api.z.ai"
    return None


def is_zhipu_quota_channel(channel) -> bool:
    """True when the channel's base URL points at Zhipu's API hosts."""
    return channel.auth_type != "oauth" and zhipu_quota_host(getattr(channel, "base_url", None)) is not None


def _window(item: dict, now: float) -> ChannelUsageWindow:
    unit_kind = _WINDOW_UNITS.get(item.get("unit"))
    reset_at = item.get("nextResetTime")
    reset_at_seconds = reset_at / 1000 if isinstance(reset_at, (int, float)) and reset_at > 10**12 else None
    return ChannelUsageWindow(
        used_percent=item.get("percentage"),
        window_seconds=unit_kind[1] if unit_kind else None,
        reset_after_seconds=max(0, round(reset_at_seconds - now)) if reset_at_seconds is not None else None,
        reset_at=reset_at_seconds,
    )


def parse_zhipu_usage(payload: dict, *, fetched_at: float) -> ChannelUsageResponse:
    """Map the Zhipu quota payload onto the shared channel usage schema."""
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("limits"), list):
        raise ZhipuQuotaError("Zhipu returned an invalid quota payload")
    windows: dict[str, ChannelUsageWindow] = {}
    for preferred in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
        for item in data["limits"]:
            if not isinstance(item, dict) or item.get("type") != preferred:
                continue
            kind = _WINDOW_UNITS.get(item.get("unit"), (None, None))[0]
            if kind is None or kind in windows:
                continue
            windows[kind] = _window(item, fetched_at)
        if "primary" in windows or "secondary" in windows:
            break
    if not windows:
        raise ZhipuQuotaError("Zhipu quota payload contains no token-limit window")
    limit_reached = any(
        window.used_percent is not None and window.used_percent >= 100
        for window in windows.values()
    )
    return ChannelUsageResponse(
        plan=str(data.get("level")) if data.get("level") else None,
        allowed=not limit_reached,
        limit_reached=limit_reached,
        primary=windows.get("primary"),
        secondary=windows.get("secondary"),
        fetched_at=fetched_at,
    )


async def fetch_zhipu_usage(client: httpx.AsyncClient, channel) -> ChannelUsageResponse:
    """Fetch the Coding Plan rolling-window usage for an API-key Zhipu channel."""
    host = zhipu_quota_host(channel.base_url)
    headers = {"Authorization": channel.key, "Accept": "application/json"}
    for name, value in ((channel.extra or {}).get("headers") or {}).items():
        if name.lower() in ("bigmodel-organization", "bigmodel-project"):
            headers[name] = value
    if headers.get("bigmodel-organization"):
        host += QUOTA_PATH + "?type=2"
    else:
        host += QUOTA_PATH
    try:
        response = await client.get(host, headers=headers, timeout=HTTP_TIMEOUT, follow_redirects=False)
    except (httpx.HTTPError, TimeoutError):
        raise ZhipuQuotaError("Zhipu quota is temporarily unavailable; try again", 503) from None
    if response.status_code in (401, 403):
        raise ZhipuQuotaError("Zhipu quota authentication failed", 502)
    if not response.is_success:
        raise ZhipuQuotaError(f"Zhipu quota returned HTTP {response.status_code}", 502)
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        raise ZhipuQuotaError("Zhipu returned an invalid quota payload", 502) from None
    if payload.get("success") is False:
        raise ZhipuQuotaError(f"Zhipu quota error: {payload.get('msg') or 'unknown'}", 502)
    return parse_zhipu_usage(payload, fetched_at=time.time())
