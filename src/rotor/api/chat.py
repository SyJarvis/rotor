"""Authenticated user chat backed by canonical server-side conversations."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rotor.api.admin import chat as engine
from rotor.config import settings
from rotor.core.admin_auth import AdminActor, require_user_access
from rotor.services.user_chat_store import ChatStore, ChatStoreError, GenerationLease


router = APIRouter(prefix="/chat", tags=["chat"])


class StoredSettings(engine.ChatSettings):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="", max_length=200)


class AttachmentReference(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="", max_length=100_000)
    attachments: list[AttachmentReference] = Field(default_factory=list, max_length=6)


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="新对话", min_length=1, max_length=200)
    settings: StoredSettings = Field(default_factory=StoredSettings)
    draft: Draft = Field(default_factory=Draft)


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_draft_revision: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    settings: StoredSettings | None = None
    draft: Draft | None = None


class NewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    content: str = Field(default="", max_length=100_000)
    attachments: list[AttachmentReference] = Field(default_factory=list, max_length=6)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_revision: int = Field(ge=1)
    settings: engine.ChatSettings
    message: NewMessage


def _store() -> ChatStore:
    return ChatStore(settings.CHAT_STORE_DIR)


async def _call(function, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except ChatStoreError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except (OSError, UnicodeError):
        raise HTTPException(500, "Chat storage is unavailable") from None


@router.get("/models")
async def models(actor: AdminActor = Depends(require_user_access)):
    return await engine.models_for_user(actor)


@router.post("/test")
async def test_connection(payload: engine.ChatSettings, request: Request, actor: AdminActor = Depends(require_user_access)):
    return await engine.test_connection_for_user(payload, request, actor)


@router.get("/conversations")
async def list_conversations(actor: AdminActor = Depends(require_user_access)):
    return await _call(_store().list, actor.user_id)


@router.post("/conversations", status_code=201)
async def create_conversation(payload: ConversationCreate, actor: AdminActor = Depends(require_user_access)):
    return await _call(_store().create, actor.user_id, **payload.model_dump())


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, actor: AdminActor = Depends(require_user_access)):
    return await _call(_store().get, actor.user_id, conversation_id)


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(conversation_id: str, payload: ConversationPatch, actor: AdminActor = Depends(require_user_access)):
    changes = payload.model_dump(exclude={"expected_revision", "expected_draft_revision"}, exclude_none=True)
    return await _call(_store().patch, actor.user_id, conversation_id, payload.expected_revision, changes,
                       expected_draft=payload.expected_draft_revision)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, expected_revision: int = Query(ge=1), actor: AdminActor = Depends(require_user_access)):
    await _call(_store().delete, actor.user_id, conversation_id, expected_revision)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/attachments", status_code=201)
async def upload_attachment(conversation_id: str, payload: engine.ChatAttachment, actor: AdminActor = Depends(require_user_access)):
    store = _store()
    await _call(store.get, actor.user_id, conversation_id)
    if payload.kind == "file":
        try:
            await asyncio.to_thread(engine._document_text, payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    extensions = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
                  "application/pdf": "pdf", engine._DOCX_MIME: "docx"}
    return await _call(
        store.upload, actor.user_id, conversation_id,
        metadata={"kind": payload.kind, "name": payload.name, "mimeType": payload.mime_type, "size": payload.actual_size},
        content=payload._decoded, extension=extensions.get(payload.mime_type, "txt"), text=payload.content is not None,
    )


@router.get("/conversations/{conversation_id}/attachments/{attachment_id}")
async def get_attachment(conversation_id: str, attachment_id: str, actor: AdminActor = Depends(require_user_access)):
    return await _call(_store().attachment, actor.user_id, conversation_id, attachment_id)


@router.delete("/conversations/{conversation_id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(conversation_id: str, attachment_id: str, actor: AdminActor = Depends(require_user_access)):
    await _call(_store().delete_attachment, actor.user_id, conversation_id, attachment_id)
    return Response(status_code=204)


async def _canonical_request(store: ChatStore, user_id: int, conversation: dict) -> engine.AdminChatRequest:
    messages = []
    for message in conversation["messages"]:
        if message["role"] == "assistant" and (not message["content"] or message["status"] == "error"):
            continue
        attachments = [await _call(store.attachment, user_id, conversation["id"], item["id"])
                       for item in message.get("attachments", [])]
        messages.append(engine.ChatMessage(role=message["role"], content=message["content"], attachments=attachments))
    return engine.AdminChatRequest(
        **conversation["settings"], conversation_id=conversation["id"], messages=messages,
    )


async def _persisted_stream(store: ChatStore, lease: GenerationLease, conversation: dict, payload: engine.AdminChatRequest,
                            request: Request, principal) -> Any:
    content = ""
    persisted = ""
    terminal = False
    checkpoint_error: Exception | None = None

    async def checkpoint():
        nonlocal persisted, checkpoint_error
        try:
            while True:
                await asyncio.sleep(0.5)
                snapshot = content
                if snapshot != persisted:
                    await _call(store.checkpoint, lease, snapshot)
                    persisted = snapshot
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            checkpoint_error = exc

    checkpoint_task = asyncio.create_task(checkpoint())

    async def stop_checkpoint():
        checkpoint_task.cancel()
        try:
            await checkpoint_task
        except asyncio.CancelledError:
            pass

    try:
        yield engine._sse({"type": "start", "conversation": conversation, "revision": conversation["revision"]})
        async with aclosing(engine._completion_events(payload, request, principal)) as events:
            async for event in events:
                if checkpoint_error is not None:
                    raise RuntimeError("Chat response could not be saved")
                if event["type"] == "delta":
                    content += event["content"]
                    if len(content) > 100_000:
                        content = content[:100_000]
                        raise RuntimeError("Response exceeded the conversation message size limit")
                    yield engine._sse(event)
                elif event["type"] == "done":
                    await stop_checkpoint()
                    conversation = await _call(store.checkpoint, lease, content, status="done", usage=event.get("usage"))
                    terminal = True
                    yield engine._sse({**event, "conversation": conversation, "revision": conversation["revision"]})
    except Exception as exc:
        await stop_checkpoint()
        message = engine._failure_message(exc)
        conversation = await _call(store.checkpoint, lease, content, status="error", error=message)
        terminal = True
        yield engine._sse({"type": "error", "message": message, "conversation": conversation, "revision": conversation["revision"]})
    finally:
        async def cleanup():
            try:
                await stop_checkpoint()
                if not terminal:
                    await _call(store.checkpoint, lease, content, status="stopped")
            finally:
                lease.close()
        await engine._shield_cleanup(cleanup)


class _StoredChatResponse(StreamingResponse):
    def __init__(self, iterator, store: ChatStore, lease: GenerationLease):
        super().__init__(iterator, media_type="text/event-stream",
                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        self.store, self.lease = store, lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            async def cleanup():
                try:
                    await self.body_iterator.aclose()
                    if self.lease.fd >= 0:
                        await _call(self.store.checkpoint, self.lease, "", status="stopped")
                finally:
                    self.lease.close()
            await engine._shield_cleanup(cleanup)


async def _begin_generation(store: ChatStore, user_id: int, payload: CompletionRequest):
    pending = asyncio.create_task(_call(
        store.begin, user_id, payload.conversation_id, payload.expected_revision,
        payload.settings.model_dump(), payload.message.model_dump(),
    ))
    try:
        return await asyncio.shield(pending)
    except BaseException:
        async def cleanup():
            try:
                _, lease = await pending
            except Exception:
                return
            try:
                await _call(store.checkpoint, lease, "", status="stopped")
            finally:
                lease.close()
        await engine._shield_cleanup(cleanup)
        raise


@router.post("/completions")
async def completions(payload: CompletionRequest, request: Request, actor: AdminActor = Depends(require_user_access)):
    principal = await engine._chat_principal(payload.settings.model, actor)
    store = _store()
    conversation, lease = await _begin_generation(store, actor.user_id, payload)
    try:
        canonical = await _canonical_request(store, actor.user_id, conversation)
    except BaseException as exc:
        async def cleanup():
            try:
                await _call(store.checkpoint, lease, "", status="error", error="Conversation could not be prepared")
            finally:
                lease.close()
        await engine._shield_cleanup(cleanup)
        if isinstance(exc, ValidationError):
            raise HTTPException(400, "Conversation exceeds the supported message or attachment limits") from None
        raise
    return _StoredChatResponse(
        _persisted_stream(store, lease, conversation, canonical, request, principal), store, lease,
    )
