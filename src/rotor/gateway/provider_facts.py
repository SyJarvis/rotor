"""Extract provider response facts without retaining sensitive headers."""

from typing import Any, Mapping


def extract_capacity_snapshot(
    headers: Mapping[str, str],
) -> dict[str, str] | None:
    snapshot = {
        str(name).lower(): str(value)
        for name, value in headers.items()
        if "ratelimit" in str(name).lower()
        or str(name).lower() == "retry-after"
    }
    return dict(sorted(snapshot.items())) or None


def extract_capacity_snapshot_from_error(
    error: Any,
) -> dict[str, str] | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    return extract_capacity_snapshot(headers)
