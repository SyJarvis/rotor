"""Normalize client-specific session hints for routing and observability."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping


_MAX_IDENTIFIER_LENGTH = 100

SESSION_SOURCE_TO_CLIENT = {
    "codex-session-id": "codex",
    "claude-code-session-id": "claude_code",
    "metadata-user-session-id": "claude_code",
    "opencode-session-affinity": "opencode",
    "pi-session-id": "pi",
    "pi-opencode-session": "pi",
    "pi-prompt-cache-key": "pi",
    "grok-conv-id": "grok_build",
    "grok-session-id": "grok_build",
    "grok-prompt-cache-key": "grok_build",
    "mindcode-session-id": "mindcode",
}


def _is_pi_client(
    headers: Mapping[str, str],
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    user_agent = str(headers.get("user-agent") or "").strip().lower()
    opencode_client = str(headers.get("x-opencode-client") or "").strip().lower()
    originator = str(
        headers.get("originator")
        or (metadata or {}).get("originator")
        or ""
    ).strip().lower()
    return (
        originator == "pi"
        or opencode_client == "pi"
        or user_agent.startswith(("pi ", "pi/", "pi("))
    )


def _is_grok_client(headers: Mapping[str, str]) -> bool:
    user_agent = str(headers.get("user-agent") or "").strip().lower()
    client_identifier = (
        str(headers.get("x-grok-client-identifier") or "").strip().lower()
    )
    token_auth = str(headers.get("x-xai-token-auth") or "").strip().lower()
    branded_user_agent = any(
        user_agent == marker
        or user_agent.startswith(f"{marker}/")
        or user_agent.startswith(f"{marker} ")
        for marker in ("grok-shell", "grok-build")
    )
    has_complete_session_headers = bool(
        headers.get("x-grok-conv-id") and headers.get("x-grok-session-id")
    )
    return (
        client_identifier == "grok-shell"
        or token_auth == "xai-grok-cli"
        or user_agent.startswith("xai-grok-workspace/")
        or branded_user_agent
        or has_complete_session_headers
    )


@dataclass(frozen=True, slots=True)
class ClientSessionContext:
    session_id: str | None
    source: str | None
    thread_id: str | None = None
    cache_key: str | None = None
    client_source: str | None = None

    def affinity_key(self, token_id: int) -> str | None:
        """Namespace affinity by Rotor token so tenants cannot collide."""
        if self.session_id is None:
            return None
        return f"{token_id}:{self.session_id}"

    def routing_features(self) -> dict[str, Any]:
        return {
            "session_source": self.source,
            "client_source": self.client_source,
            "thread_id": self.thread_id,
            "cache_key_present": self.cache_key is not None,
        }


def build_responses_prompt_cache_key(
    *,
    token_id: int,
    model: str,
    session_id: str | None,
) -> str | None:
    """Build a stable, tenant-scoped Responses cache routing key."""
    normalized_session = _normalize_identifier(session_id)
    if normalized_session is None:
        return None
    digest = hashlib.sha256(
        f"{token_id}\0{model}\0{normalized_session}".encode()
    ).hexdigest()
    return f"rotor_{digest[:58]}"


def resolve_client_session(
    headers: Mapping[str, str],
    *,
    metadata: Mapping[str, Any] | None = None,
    conversation: str | Mapping[str, Any] | None = None,
    prompt_cache_key: Any = None,
    legacy_user_id: Any = None,
) -> ClientSessionContext:
    """Resolve one stable session without treating a request ID as a session.

    Explicit Rotor headers win over client-specific headers. Body fields are
    fallbacks for SDKs or proxies that do not preserve those headers.
    """
    normalized_headers = {
        str(key).lower(): value for key, value in headers.items()
    }
    is_pi = _is_pi_client(normalized_headers, metadata)
    is_grok = _is_grok_client(normalized_headers)
    has_grok_session = any(
        normalized_headers.get(name)
        for name in ("x-grok-conv-id", "x-grok-session-id")
    )
    header_candidates = [
        ("x-conversation-id", "x-conversation-id"),
        ("x-rotor-session-id", "x-rotor-session-id"),
        ("x-mindcode-session-id", "mindcode-session-id"),
    ]
    if is_grok:
        header_candidates.extend([
            ("x-grok-conv-id", "grok-conv-id"),
            ("x-grok-session-id", "grok-session-id"),
        ])
    elif has_grok_session:
        header_candidates.extend([
            ("x-grok-conv-id", "xai-conv-id"),
            ("x-grok-session-id", "xai-session-id"),
        ])
    if is_pi:
        header_candidates.extend([
            ("session_id", "pi-session-id"),
            ("x-opencode-session", "pi-opencode-session"),
        ])
    header_candidates.extend([
        ("session-id", "pi-session-id" if is_pi else "codex-session-id"),
        ("x-claude-code-session-id", "claude-code-session-id"),
        ("x-session-affinity", "opencode-session-affinity"),
    ])
    header_candidates.append((
        "x-session-id",
        "pi-session-id" if is_pi else "client-session-id",
    ))

    session_id = None
    source = None
    for header, candidate_source in header_candidates:
        session_id = _normalize_identifier(normalized_headers.get(header))
        if session_id is not None:
            source = candidate_source
            break

    metadata_session, metadata_source = _metadata_session(metadata)
    if session_id is None:
        conversation_id = _conversation_id(conversation)
        if conversation_id is not None:
            session_id = conversation_id
            source = "responses-conversation"
        elif metadata_session is not None:
            session_id = metadata_session
            source = metadata_source
        else:
            cache_session = _normalize_identifier(prompt_cache_key)
            if cache_session is not None:
                session_id = cache_session
                if is_grok:
                    source = "grok-prompt-cache-key"
                elif is_pi:
                    source = "pi-prompt-cache-key"
                else:
                    source = "prompt-cache-key"
            else:
                session_id = _normalize_identifier(legacy_user_id)
                if session_id is not None:
                    source = "legacy-user-id"

    return ClientSessionContext(
        session_id=session_id,
        source=source,
        thread_id=_normalize_identifier(
            normalized_headers.get("thread-id")
            or normalized_headers.get("x-thread-id")
        ),
        cache_key=_normalize_identifier(prompt_cache_key),
        client_source=_resolve_client_source(normalized_headers, metadata, source),
    )


def _resolve_client_source(
    headers: Mapping[str, str],
    metadata: Mapping[str, Any] | None,
    session_source: str | None,
) -> str | None:
    # Dedicated client hints also work when an explicit Rotor session wins.
    if _normalize_identifier(headers.get("x-mindcode-session-id")):
        return "mindcode"
    if _is_pi_client(headers, metadata):
        return "pi"
    if _is_grok_client(headers):
        return "grok_build"
    user_agent = str(headers.get("user-agent") or "").strip().lower()
    for client, markers in (
        ("codex", ("codex-tui", "codex desktop", "codex_cli_rs")),
        ("claude_code", ("claude-cli",)),
        ("opencode", ("opencode",)),
        ("mindcode", ("mindcode",)),
    ):
        if any(
            user_agent == marker
            or user_agent.startswith((f"{marker}/", f"{marker} "))
            for marker in markers
        ):
            return client
    return SESSION_SOURCE_TO_CLIENT.get(session_source)


def _metadata_session(
    metadata: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    if not metadata:
        return None, None
    for key in ("conversation_id", "session_id"):
        value = _normalize_identifier(metadata.get(key))
        if value is not None:
            return value, f"metadata-{key.replace('_', '-')}"

    user_id = metadata.get("user_id")
    if isinstance(user_id, Mapping):
        nested = user_id
    elif isinstance(user_id, str):
        try:
            parsed = json.loads(user_id)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        nested = parsed if isinstance(parsed, Mapping) else None
    else:
        nested = None
    if nested:
        value = _normalize_identifier(nested.get("session_id"))
        if value is not None:
            return value, "metadata-user-session-id"

    # Preserve the existing Anthropic behavior as the lowest-priority body
    # fallback. Callers should prefer an explicit session header.
    value = _normalize_identifier(user_id)
    return (value, "metadata-user-id") if value is not None else (None, None)


def _conversation_id(
    conversation: str | Mapping[str, Any] | None,
) -> str | None:
    if isinstance(conversation, Mapping):
        return _normalize_identifier(conversation.get("id"))
    return _normalize_identifier(conversation)


def _normalize_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) <= _MAX_IDENTIFIER_LENGTH:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"sid_{digest}"
