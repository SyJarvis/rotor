from rotor.config import settings


def validate_api_key_format(key: str) -> bool:
    """Validate API key format."""
    if not key:
        return False
    if not key.startswith(settings.API_KEY_PREFIX):
        return False
    return len(key) >= len(settings.API_KEY_PREFIX) + 10
