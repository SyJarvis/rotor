"""Selective inbound header forwarding to upstream providers.

A channel opts in with ``extra.forward_headers``: a list of inbound header
names that may be repeated on the outbound request. Credentials, hop-by-hop
and framing headers are never forwarded, and headers already produced by the
channel (authentication, static ``extra.headers``) always take precedence.
"""

from __future__ import annotations

from typing import Any, Mapping

# Credentials, connection framing and hop-by-hop headers. These never travel
# from the client to the provider, regardless of channel configuration.
DENYLIST = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key",
    "host", "content-length", "content-type", "accept", "accept-encoding",
    "connection", "keep-alive", "proxy-authenticate", "te", "trailer",
    "transfer-encoding", "upgrade",
})

_MAX_HEADER_VALUE_LENGTH = 1024
_MAX_FORWARDED_HEADERS = 16


def _safe_header_value(value: Any) -> str | None:
    """Return the value when it is safe to place on the wire, else None."""
    if not isinstance(value, str):
        return None
    if not value or len(value) > _MAX_HEADER_VALUE_LENGTH:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


def snapshot_inbound_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Capture forwarding-safe inbound headers (lowercased, denylist stripped).

    The snapshot rides on the internal request object only; it is excluded
    from serialization and logging.
    """
    snapshot: dict[str, str] = {}
    for name, value in headers.items():
        normalized = str(name).strip().lower()
        if not normalized or normalized in DENYLIST:
            continue
        safe = _safe_header_value(value)
        if safe is not None:
            snapshot[normalized] = safe
    return snapshot


def forwarded_header_names(extra: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Read and validate the channel's ``forward_headers`` allowlist."""
    if not extra:
        return ()
    names = extra.get("forward_headers")
    if not isinstance(names, (list, tuple)):
        return ()
    allowed: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
        normalized = name.strip().lower()
        if not normalized or normalized in DENYLIST:
            continue
        allowed.append(normalized)
    return tuple(dict.fromkeys(allowed))[:_MAX_FORWARDED_HEADERS]


def select_forwarded_headers(
    extra: Mapping[str, Any] | None,
    inbound: Mapping[str, str] | None,
) -> dict[str, str]:
    """Pick the channel-allowed subset of an inbound header snapshot."""
    names = forwarded_header_names(extra)
    if not names or not inbound:
        return {}
    selected: dict[str, str] = {}
    for name in names:
        value = _safe_header_value(inbound.get(name))
        if value is not None:
            selected[name] = value
    return selected
