import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from rotor.config import settings
from rotor.conversations.sanitizer import sanitize
from rotor.conversations.schema import event
from rotor.models.conversation import ConversationRecord
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.schemas.request import ChatCompletionRequest


@dataclass(slots=True)
class ConversationHandle:
    conversation_id: str
    request_id: str
    file_path: Path
    record: Optional[ConversationRecord]


class ConversationStore:
    """Filesystem JSONL conversation archive with a DB index."""

    async def start(
        self,
        db: AsyncSession,
        *,
        conversation_id: str,
        request_id: str,
        token: Token,
        request: ChatCompletionRequest,
        protocol: str,
    ) -> ConversationHandle:
        file_path = self._file_path(conversation_id, token)
        record = None

        if settings.CONVERSATION_STORE_ENABLED:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            record = ConversationRecord(
                conversation_id=conversation_id,
                request_id=request_id,
                user_id=token.user_id,
                token_id=token.id,
                model=request.model,
                protocol=protocol,
                file_path=str(file_path),
                status="started",
            )
            db.add(record)
            await self.append_raw(
                file_path,
                event(
                    "conversation_start",
                    conversation_id=conversation_id,
                    request_id=request_id,
                    user_id=token.user_id,
                    token_id=token.id,
                    protocol=protocol,
                    model=request.model,
                ),
            )
            if settings.SAVE_CONVERSATION_BODY:
                for message in request.messages:
                    await self.append_raw(
                        file_path,
                        event(
                            "message",
                            role=message.role.value,
                            content=self._message_content(message),
                        ),
                    )

        return ConversationHandle(
            conversation_id=conversation_id,
            request_id=request_id,
            file_path=file_path,
            record=record,
        )

    async def append_routing(self, handle: ConversationHandle, channel: Channel) -> None:
        if not settings.CONVERSATION_STORE_ENABLED:
            return
        await self.append_raw(
            handle.file_path,
            event(
                "routing",
                channel_id=channel.id,
                provider=channel.type,
                provider_protocol=channel.protocol,
            ),
        )
        if handle.record:
            handle.record.channel_id = channel.id
            handle.record.provider = channel.type

    async def append_response(self, handle: ConversationHandle, response_data: dict[str, Any]) -> None:
        if not settings.CONVERSATION_STORE_ENABLED or not settings.SAVE_PROVIDER_RESPONSE:
            return
        await self.append_raw(
            handle.file_path,
            event("provider_response", response=sanitize(response_data)),
        )

    async def append_usage(self, handle: ConversationHandle, usage: dict[str, Any]) -> None:
        if not settings.CONVERSATION_STORE_ENABLED:
            return
        await self.append_raw(handle.file_path, event("usage", usage=usage))

    async def append_error(self, handle: ConversationHandle, error_code: str, error_message: str) -> None:
        if not settings.CONVERSATION_STORE_ENABLED:
            return
        await self.append_raw(
            handle.file_path,
            event("error", error_code=error_code, error_message=error_message),
        )

    async def finish(self, handle: ConversationHandle, status: str, latency_ms: int | None) -> None:
        if not settings.CONVERSATION_STORE_ENABLED:
            return
        await self.append_raw(
            handle.file_path,
            event("conversation_end", status=status, latency_ms=latency_ms),
        )
        if handle.record:
            handle.record.status = status

    async def append_raw(self, file_path: Path, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        await asyncio.to_thread(self._append_line, file_path, line)

    def _append_line(self, file_path: Path, line: str) -> None:
        with file_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _file_path(self, conversation_id: str, token: Token) -> Path:
        from datetime import datetime

        day = datetime.utcnow().strftime("%Y-%m-%d")
        owner = f"user_{token.user_id}" if token.user_id else f"token_{token.id}"
        filename = f"{conversation_id}.jsonl"
        return Path(settings.CONVERSATION_STORE_DIR) / day / owner / filename

    def _message_content(self, message) -> list[dict[str, Any]]:
        if message.content is None:
            return []
        return [{"type": "text", "text": message.content}]
