from copy import deepcopy
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "models_path": "/models",
        "request_path": "/chat/completions",
        "auth_type": "bearer",
    },
    "deepseek": {
        "label": "DeepSeek",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com",
        "models_path": "/models",
        "request_path": "/chat/completions",
        "auth_type": "bearer",
    },
    "anthropic": {
        "label": "Anthropic",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models_path": "/models",
        "request_path": "/messages",
        "auth_type": "x-api-key",
    },
    "moonshot": {
        "label": "Moonshot",
        "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models_path": "/models",
        "request_path": "/chat/completions",
        "auth_type": "bearer",
    },
    "minimax": {
        "label": "MiniMax",
        "protocol": "openai",
        "base_url": "https://api.minimaxi.com/v1",
        "models_path": "/models",
        "request_path": "/chat/completions",
        "auth_type": "bearer",
    },
    "zhipu": {
        "label": "Zhipu",
        "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models_path": "/models",
        "request_path": "/chat/completions",
        "auth_type": "bearer",
    },
    "kimi": {
        "label": "Kimi",
        "protocol": "openai",
        "base_url": "https://api.kimi.com/coding/v1",
        "models_path": "/models",
        "request_path": "/chat/completions",
        "auth_type": "bearer",
    },
}


def list_provider_presets() -> list[dict[str, Any]]:
    return [
        {"id": provider, **deepcopy(config)}
        for provider, config in PROVIDER_PRESETS.items()
    ]


def provider_defaults(provider: str, protocol: str = "openai") -> dict[str, str]:
    preset = PROVIDER_PRESETS.get(provider, {})
    normalized_protocol = protocol.lower()
    fallback = {
        "models_path": "/models",
        "request_path": (
            "/messages"
            if normalized_protocol in {"anthropic", "anthropic_messages"}
            else "/responses"
            if normalized_protocol in {"responses", "openai_responses"}
            else "/chat/completions"
        ),
        "auth_type": (
            "x-api-key"
            if normalized_protocol in {"anthropic", "anthropic_messages"}
            else "bearer"
        ),
    }
    defaults = {**fallback, **preset}
    # The wire protocol selects the generation endpoint, while authentication
    # remains provider-specific. Anthropic-compatible providers such as Zhipu
    # still use their normal Bearer credentials rather than Anthropic's
    # x-api-key scheme.
    defaults["request_path"] = fallback["request_path"]
    return defaults


def channel_option(
    *,
    provider: str,
    protocol: str,
    extra: dict[str, Any] | None,
    name: str,
) -> str:
    if extra and extra.get(name):
        return str(extra[name])
    return str(provider_defaults(provider, protocol)[name])


def join_api_url(base_url: str, path: str, *, protocol: str | None = None) -> str:
    base = base_url.strip().rstrip("/")
    if not path:
        return base
    if path.startswith(("http://", "https://")):
        return path

    parsed = urlsplit(base)
    base_path = parsed.path.rstrip("/")
    normalized_path = "/" + path.lstrip("/")
    if str(protocol or "").lower() in {"anthropic", "anthropic_messages"}:
        endpoint = re.fullmatch(r"/(?:(v\d+)/)?(messages|models)/?", normalized_path)
        if endpoint:
            # Only standard operations imply the Anthropic /v1 API root.
            # Explicit versions win; custom paths retain the generic join.
            root = re.sub(r"/(v\d+)/(messages|models)$", r"/\1", base_path)
            base_version = re.search(r"/(v\d+)$", root)
            version = endpoint[1] or (base_version[1] if base_version else "v1")
            if base_version:
                root = root[:base_version.start()]
            final_path = f"{root}/{version}/{endpoint[2]}"
            return urlunsplit((parsed.scheme, parsed.netloc, final_path, "", ""))
    if base_path and normalized_path.startswith(base_path + "/"):
        final_path = normalized_path
    else:
        final_path = base_path + normalized_path
    return urlunsplit((parsed.scheme, parsed.netloc, final_path, "", ""))


def provider_headers(
    *,
    key: str,
    auth_type: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if auth_type == "x-api-key":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    elif auth_type == "api-key":
        headers["api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    if extra_headers:
        headers.update(extra_headers)
    return headers
