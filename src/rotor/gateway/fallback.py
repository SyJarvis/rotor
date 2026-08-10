from rotor.core.exceptions import normalize_upstream_error


def should_fallback(exc: Exception) -> bool:
    """Only retry failures that indicate an unavailable provider/channel."""
    return normalize_upstream_error(exc).fallback_allowed


def retry_after_seconds(exc: Exception) -> float | None:
    return normalize_upstream_error(exc).retry_after_seconds


def set_routing_headers(response, channel, requested_model: str, fallback: bool) -> None:
    response.headers["X-Rotor-Channel"] = channel.name
    response.headers["X-Rotor-Provider-Model"] = (
        (channel.model_mapping or {}).get(requested_model, requested_model)
    )
    if fallback:
        response.headers["X-Rotor-Fallback"] = "true"
