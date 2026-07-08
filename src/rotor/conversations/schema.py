from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def event(event_type: str, **data: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "created_at": utc_now_iso(),
        **data,
    }
