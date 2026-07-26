from typing import Any


SENSITIVE_KEYS = {
    "authorization",
    "api-key",
    "api_key",
    "apikey",
    "x-api-key",
    "key",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "set-cookie",
}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                clean[key] = "***"
            else:
                clean[key] = sanitize(item)
        return clean
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value
