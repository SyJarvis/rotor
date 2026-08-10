from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol


CONVERSATION_SCHEMA_VERSION = 1


class ConversationStore(Protocol):
    def load(self, session_id: str) -> list[dict[str, Any]]: ...

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None: ...


class LocalConversationStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def load(self, session_id: str) -> list[dict[str, Any]]:
        path = self._path(session_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if payload.get("schema_version") != CONVERSATION_SCHEMA_VERSION:
            raise ValueError("不支持的 conversation schema_version")
        if payload.get("session_id") != session_id:
            raise ValueError("conversation session_id 不匹配")
        messages = payload.get("messages")
        if not isinstance(messages, list) or any(
            not isinstance(message, dict) for message in messages
        ):
            raise ValueError("conversation messages 必须是对象列表")
        return _conversation_messages(messages)

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        path = self._path(session_id)
        temporary = path.with_suffix(".tmp")
        payload = {
            "schema_version": CONVERSATION_SCHEMA_VERSION,
            "session_id": session_id,
            "messages": _conversation_messages(messages),
        }
        try:
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(
                    payload,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _path(self, session_id: str) -> Path:
        if not session_id:
            raise ValueError("session_id 不能为空")
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"


def _conversation_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    transcript: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        if "tool_calls" in message or "tool_call_id" in message:
            continue
        content = message.get("content")
        if not isinstance(content, (str, list)):
            continue
        if _is_internal_message(content):
            continue
        transcript.append({"role": role, "content": copy.deepcopy(content)})
    return transcript


def _is_internal_message(content: str | list[Any]) -> bool:
    if not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("type")
        in {
            "completion_rejected",
            "continuation_checkpoint",
        }
    )
