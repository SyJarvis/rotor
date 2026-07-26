from httpx import HTTPStatusError, RequestError


FALLBACK_STATUS_CODES = {401, 402, 403, 404, 408, 409, 425, 429}


def should_fallback(exc: Exception) -> bool:
    """Only retry failures that indicate an unavailable provider/channel."""
    if isinstance(exc, HTTPStatusError):
        status_code = exc.response.status_code
        return status_code in FALLBACK_STATUS_CODES or status_code >= 500
    return isinstance(exc, RequestError)


def retry_after_seconds(exc: Exception) -> float | None:
    if not isinstance(exc, HTTPStatusError) or exc.response.status_code != 429:
        return None
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def set_routing_headers(response, channel, requested_model: str, fallback: bool) -> None:
    response.headers["X-Rotor-Channel"] = channel.name
    response.headers["X-Rotor-Provider-Model"] = (
        (channel.model_mapping or {}).get(requested_model, requested_model)
    )
    if fallback:
        response.headers["X-Rotor-Fallback"] = "true"
