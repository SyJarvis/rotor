"""Resolve explicit provider resource boundaries for a Channel."""

from dataclasses import dataclass
import re
from typing import Any, Mapping


SCOPE_KEYS = ("cache_scope", "capacity_scope", "billing_scope")
_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}\Z")


@dataclass(frozen=True, slots=True)
class ResourceScopes:
    cache_scope: str
    capacity_scope: str
    billing_scope: str

    def as_dict(self) -> dict[str, str]:
        return {
            "cache_scope": self.cache_scope,
            "capacity_scope": self.capacity_scope,
            "billing_scope": self.billing_scope,
        }


def normalize_scope_config(extra: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize scope IDs while preserving other options."""
    normalized = dict(extra)
    for key in SCOPE_KEYS:
        if key not in normalized:
            continue
        value = normalized[key]
        if value is None:
            normalized.pop(key)
            continue
        normalized[key] = normalize_scope_id(value, field_name=key)
    return normalized


def normalize_scope_id(value: Any, *, field_name: str = "scope") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not _SCOPE_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must be 1-100 ASCII letters, digits, or ._:/-"
        )
    return normalized


def resolve_resource_scopes(channel: Any) -> ResourceScopes:
    """Return effective scopes, isolating missing or invalid config by Channel."""
    default_scope = f"channel:{channel.id}"
    extra = getattr(channel, "extra", None)
    if not isinstance(extra, Mapping):
        extra = {}

    def resolve(key: str) -> str:
        value = extra.get(key)
        if value is None:
            return default_scope
        try:
            return normalize_scope_id(value, field_name=key)
        except ValueError:
            return default_scope

    return ResourceScopes(
        cache_scope=resolve("cache_scope"),
        capacity_scope=resolve("capacity_scope"),
        billing_scope=resolve("billing_scope"),
    )
