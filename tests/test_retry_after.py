from datetime import datetime, timezone

import httpx
import pytest

import rotor.core.exceptions as exceptions
from rotor.core.exceptions import normalize_upstream_error
from rotor.gateway.fallback import retry_after_seconds, should_fallback


def status_error(status_code: int, value: str | None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    response = httpx.Response(
        status_code,
        request=request,
        headers={"Retry-After": value} if value is not None else {},
        json={"error": {"message": "upstream failure"}},
    )
    return httpx.HTTPStatusError("upstream failure", request=request, response=response)


@pytest.mark.parametrize("status_code", [429, 503])
@pytest.mark.parametrize("value,expected", [
    ("120", 120.0), ("2.5", 2.5), ("0", 0.0), (" 12 ", 12.0),
    (None, None), ("", None), ("invalid", None), ("-1", None),
    ("-0.5", None), ("NaN", None), ("inf", None), ("-inf", None),
    ("1e309", None), ("Thu, 99 Sep 2026 04:02:00 GMT", None),
])
def test_retry_after_seconds_are_finite_and_nonnegative(status_code, value, expected):
    error = status_error(status_code, value)
    assert normalize_upstream_error(error).retry_after_seconds == expected
    assert retry_after_seconds(error) == expected


@pytest.mark.parametrize("status_code", [429, 503])
@pytest.mark.parametrize("value,expected", [
    ("Thu, 10 Sep 2026 04:02:00 GMT", 120.0),
    ("Thursday, 10-Sep-26 04:02:00 GMT", 120.0),
    ("Thu Sep 10 04:02:00 2026", 120.0),
    ("Thu, 10 Sep 2026 03:59:00 GMT", 0.0),
    ("Thu, 10 Sep 2026 04:00:00 GMT", 0.0),
])
def test_retry_after_http_dates_use_utc_and_past_dates_expire(monkeypatch, status_code, value, expected):
    now = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exceptions, "time", lambda: now)
    error = status_error(status_code, value)
    assert normalize_upstream_error(error).retry_after_seconds == expected
    assert retry_after_seconds(error) == expected


def test_retry_after_date_recomputes_remaining_delay(monkeypatch):
    now = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exceptions, "time", lambda: now)
    error = status_error(503, "Thu, 10 Sep 2026 04:02:00 GMT")
    assert retry_after_seconds(error) == 120.0
    now += 45.25
    assert retry_after_seconds(error) == 74.75


@pytest.mark.parametrize("status_code,allowed", [
    (400, False), (401, True), (402, True), (403, True), (404, True),
    (408, True), (409, True), (410, False), (422, False), (425, True),
    (429, True), (499, False), (500, True), (503, True), (599, True),
])
def test_retry_after_preserves_existing_error_classification(status_code, allowed):
    error = status_error(status_code, "30")
    fact = normalize_upstream_error(error)
    without_header = normalize_upstream_error(status_error(status_code, None))
    assert should_fallback(error) is allowed
    assert fact.retry_after_seconds == (30.0 if allowed else None)
    excluded = {"retry_after_seconds", "occurred_at"}
    assert fact.model_dump(exclude=excluded) == without_header.model_dump(exclude=excluded)
