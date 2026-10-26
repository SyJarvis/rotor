"""Admin chat through the OpenAI SDK and Rotor's normal gateway pipeline."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import time
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import PurePath
from typing import Any, Literal
from xml.etree import ElementTree

import anyio
import httpx2
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from openai import APIError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from rotor.api.v1.chat import chat_completions
from rotor.api.v1.responses import create_response
from rotor.core.openai_errors import openai_error_response
from rotor.core.exceptions import _sanitize_provider_value
from rotor.core.admin_auth import AdminActor, require_admin_access
from rotor.core.user_access import AccountPrincipal, accessible_models, resolve_account_principal
from rotor.database import async_session_maker
from rotor.schemas.request import ChatCompletionRequest
from rotor.schemas.responses import ResponsesRequest

router = APIRouter(prefix="/chat", tags=["admin-chat"])
_MAX_FILE_BYTES = 5_000_000
_MAX_TEXT_CHARS = 500_000
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".log",
    ".js", ".ts", ".jsx", ".tsx", ".py", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".css", ".html",
    ".sh", ".sql", ".toml", ".ini", ".mjs", ".cjs", ".scss", ".bash", ".cfg", ".hpp",
}


class ChatAttachment(BaseModel):
    kind: Literal["image", "file"]
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default="application/octet-stream", max_length=120)
    size: int = Field(ge=0, le=_MAX_FILE_BYTES)
    data_url: str | None = Field(default=None, max_length=6_700_000)
    content: str | None = Field(default=None, max_length=_MAX_TEXT_CHARS)
    _decoded: bytes = PrivateAttr(default=b"")

    @model_validator(mode="after")
    def validate_payload(self) -> "ChatAttachment":
        if self.content is not None and self.data_url is not None:
            raise ValueError("附件不能同时包含文本和二进制内容")
        if self.data_url is not None:
            prefix = f"data:{self.mime_type};base64,"
            if not self.data_url.startswith(prefix):
                raise ValueError("附件 data URL 与文件类型不一致")
            try:
                self._decoded = base64.b64decode(self.data_url[len(prefix):], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("附件不是有效的 Base64 内容") from exc
            if not self._decoded or len(self._decoded) > _MAX_FILE_BYTES:
                raise ValueError("附件不能为空且不能超过 5 MB")
            if len(self._decoded) != self.size:
                raise ValueError("附件大小与实际内容不一致")
        elif self.content is not None:
            self._decoded = self.content.encode("utf-8")
            if len(self._decoded) > 500_000:
                raise ValueError("文本附件不能超过 500 KB（UTF-8）")
            if len(self._decoded) != self.size:
                raise ValueError("附件大小与实际内容不一致")
            if not self.content.strip() or "\x00" in self.content:
                raise ValueError("文档必须包含有效的可读文本")
        else:
            raise ValueError("附件缺少内容，请重新上传")
        if len(self._decoded) > _MAX_FILE_BYTES:
            raise ValueError("单个附件不能超过 5 MB")
        if self.kind == "image":
            if self.mime_type not in _IMAGE_MIMES or not self.data_url:
                raise ValueError("图片支持 PNG、JPEG、WebP 和 GIF")
            signatures = {
                "image/png": self._decoded.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/jpeg": self._decoded.startswith(b"\xff\xd8\xff"),
                "image/gif": self._decoded.startswith((b"GIF87a", b"GIF89a")),
                "image/webp": self._decoded[:4] == b"RIFF" and self._decoded[8:12] == b"WEBP",
            }
            if not signatures[self.mime_type]:
                raise ValueError("图片内容与声明的类型不一致")
        elif self.data_url:
            if self.mime_type == "application/pdf":
                if not self._decoded.startswith(b"%PDF-"):
                    raise ValueError("PDF 内容无效")
            elif self.mime_type == _DOCX_MIME:
                if not zipfile.is_zipfile(io.BytesIO(self._decoded)):
                    raise ValueError("DOCX 内容无效")
            else:
                raise ValueError("二进制文档仅支持 PDF 和 DOCX")
        elif not (
            self.mime_type.startswith("text/")
            or self.mime_type in {"application/json", "application/xml", "application/yaml"}
            or PurePath(self.name).suffix.lower() in _TEXT_SUFFIXES
        ):
            raise ValueError("不支持此文档类型，请使用文本、PDF 或 DOCX")
        return self

    @property
    def actual_size(self) -> int:
        return len(self._decoded)


def _document_text(attachment: ChatAttachment) -> str:
    if attachment.content is not None:
        return attachment.content
    if attachment.mime_type == "application/pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(attachment._decoded))
            if reader.is_encrypted:
                raise ValueError("加密 PDF 暂不支持，请先解密")
            if len(reader.pages) > 100:
                raise ValueError("PDF 不能超过 100 页")
            parts: list[str] = []
            total = 0
            for page in reader.pages:
                value = page.extract_text() or ""
                total += len(value) + 1
                if total > _MAX_TEXT_CHARS:
                    raise ValueError("文档提取文本不能超过 500,000 字符")
                parts.append(value)
            text = "\n".join(parts)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("PDF 无法读取，请检查文件是否损坏") from exc
        if not text.strip():
            raise ValueError("PDF 没有可提取的文字；扫描文档请以图片上传")
        return text
    try:
        with zipfile.ZipFile(io.BytesIO(attachment._decoded)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 5_000_000 or sum(item.file_size for item in archive.infolist()) > 20_000_000:
                raise ValueError("DOCX 解压内容过大")
            document = archive.read(info)
            if b"<!DOCTYPE" in document.upper() or b"<!ENTITY" in document.upper():
                raise ValueError("DOCX 包含不支持的 XML 定义")
            root = ElementTree.fromstring(document)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        text = "\n".join(
            "".join(node.text or "" for node in paragraph.iter(f"{ns}t"))
            for paragraph in root.iter(f"{ns}p")
        )
    except ValueError:
        raise
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError, RuntimeError) as exc:
        raise ValueError("DOCX 无法读取，请检查文件是否损坏") from exc
    if not text.strip():
        raise ValueError("DOCX 没有可提取的正文文字")
    if len(text) > _MAX_TEXT_CHARS:
        raise ValueError("文档提取文本不能超过 500,000 字符")
    return text


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=100_000)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_message(self) -> "ChatMessage":
        if not self.content.strip() and not self.attachments:
            raise ValueError("消息不能为空")
        if self.role == "assistant" and self.attachments:
            raise ValueError("助手消息不能包含上传附件")
        if sum(item.actual_size for item in self.attachments) > 10_000_000:
            raise ValueError("单条消息附件不能超过 10 MB")
        return self


class ChatSettings(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    api_format: Literal["chat", "responses"] = "chat"
    system_prompt: str = Field(default="", max_length=100_000)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


class AdminChatRequest(ChatSettings):
    conversation_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_conversation(self) -> "AdminChatRequest":
        if self.messages[-1].role != "user":
            raise ValueError("最后一条消息必须来自用户")
        if sum(item.actual_size for message in self.messages for item in message.attachments) > 20_000_000:
            raise ValueError("对话附件总大小不能超过 20 MB")
        return self


async def _chat_principal(model: str, actor: AdminActor) -> AccountPrincipal:
    async with async_session_maker() as db:
        return await resolve_account_principal(db, actor.user_id, model)


@router.get("/models")
async def chat_models(actor: AdminActor = Depends(require_admin_access)) -> dict[str, Any]:
    return await models_for_user(actor)


async def models_for_user(actor: AdminActor) -> dict[str, Any]:
    try:
        async with async_session_maker() as db:
            principal = await resolve_account_principal(db, actor.user_id)
            models = await accessible_models(principal, db)
    except HTTPException as exc:
        if exc.status_code not in {403, 404, 429}:
            raise
        return {"models": [], "account_available": False, "message": str(exc.detail)}
    return {
        "models": sorted(models),
        "account_available": True,
    }


async def _sdk_payload(request: AdminChatRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    responses = request.api_format == "responses"
    for message in request.messages:
        if not message.attachments:
            messages.append({"role": message.role, "content": message.content})
            continue
        blocks: list[dict[str, Any]] = []
        text_type = "input_text" if responses else "text"
        if message.content:
            blocks.append({"type": text_type, "text": message.content})
        for attachment in message.attachments:
            if attachment.kind == "image":
                blocks.append(
                    {"type": "input_image", "image_url": attachment.data_url, "detail": "auto"}
                    if responses else
                    {"type": "image_url", "image_url": {"url": attachment.data_url, "detail": "auto"}}
                )
            else:
                text = await asyncio.to_thread(_document_text, attachment)
                blocks.append({"type": text_type, "text": json.dumps(
                    {"attached_document": attachment.name, "content": text}, ensure_ascii=False
                )})
        messages.append({"role": message.role, "content": blocks})
    payload: dict[str, Any] = {"model": request.model, "stream": True}
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if responses:
        payload["input"] = messages
        if request.system_prompt:
            payload["instructions"] = request.system_prompt
        if request.max_tokens is not None:
            payload["max_output_tokens"] = request.max_tokens
    else:
        payload["messages"] = ([{"role": "system", "content": request.system_prompt}]
                               if request.system_prompt else []) + messages
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
    return payload


async def _shield_cleanup(callback) -> None:
    # Starlette cancels the response task when the browser disconnects.
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(callback())
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
        task.result()
    if cancelled is not None:
        raise cancelled


class _GatewayStream(httpx2.AsyncByteStream):
    def __init__(self, response, session, owner):
        self.response = response
        self.session = session
        self.owner = owner
        self.closed = False

    async def __aiter__(self):
        try:
            async for chunk in self.response.body_iterator:
                yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        finally:
            await self.aclose()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        self.owner.streams.discard(self)

        async def cleanup():
            try:
                # Iterating directly bypasses AdmissionStreamingResponse.__call__.
                # Preserve its complete cleanup contract, including unstarted streams.
                try:
                    close = getattr(self.response.body_iterator, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    try:
                        errors = []
                        for callback in getattr(self.response, "close_callbacks", ()):
                            try:
                                await callback()
                            except (Exception, asyncio.CancelledError) as exc:
                                errors.append(exc)
                        if errors:
                            raise errors[0]
                    finally:
                        admission = getattr(self.response, "admission", None)
                        if admission is not None:
                            admission.engine.release_attempt(admission)
            finally:
                await self.session.__aexit__(None, None, None)
        await _shield_cleanup(cleanup)


class _GatewayTransport(httpx2.AsyncBaseTransport):
    """Stream SDK requests in process without a network hop or a forwarded key."""

    def __init__(self, http_request: Request, principal: AccountPrincipal, conversation_id: str):
        self.http_request = http_request
        self.principal = principal
        self.conversation_id = conversation_id
        self.streams: set[_GatewayStream] = set()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST" or request.url.host != "rotor.internal" or request.url.path not in {
            "/v1/chat/completions", "/v1/responses",
        }:
            raise ValueError("Unsupported internal chat request")
        payload = json.loads(await request.aread())
        gateway_request = Request({
            "type": "http", "method": "POST", "path": request.url.path,
            "headers": [(b"x-conversation-id", self.conversation_id.encode("ascii"))],
            "client": self.http_request.scope.get("client"),
            "state": {"request_origin": "user_chat" if self.http_request.url.path.startswith("/api/chat/") else "admin_chat"},
        })
        session = async_session_maker()
        db = await session.__aenter__()
        try:
            principal = self.principal
            if isinstance(principal, AccountPrincipal):
                principal = await resolve_account_principal(db, principal.owner_id, payload.get("model"))
            if request.url.path == "/v1/responses":
                response = await create_response(
                    ResponsesRequest.model_validate(payload), gateway_request, Response(), db, principal,
                )
            else:
                parsed = ChatCompletionRequest.model_validate(payload)
                for field in ("temperature", "top_p", "n", "presence_penalty", "frequency_penalty"):
                    if field not in payload:
                        setattr(parsed, field, None)
                response = await chat_completions(parsed, gateway_request, Response(), db, principal)
            if isinstance(response, StreamingResponse):
                stream = _GatewayStream(response, session, self)
                self.streams.add(stream)
                return httpx2.Response(response.status_code, headers=dict(response.headers), stream=stream)
            await session.__aexit__(None, None, None)
            return httpx2.Response(200, json=response)
        except BaseException as exc:
            await _shield_cleanup(lambda: session.__aexit__(type(exc), exc, exc.__traceback__))
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, HTTPException):
                detail = _sanitize_provider_value(exc.detail)
                error = detail.get("error", detail) if isinstance(detail, dict) else {"message": str(detail)}
                return httpx2.Response(exc.status_code, json={"error": error})
            response = openai_error_response(exc)
            return httpx2.Response(response.status_code, headers=dict(response.headers), content=response.body)

    async def aclose(self):
        for stream in tuple(self.streams):
            await stream.aclose()


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, APIError):
        return exc.message
    return str(exc) or "模型请求失败"


async def _completion_events(request: AdminChatRequest, http_request: Request, principal: AccountPrincipal):
    payload = await _sdk_payload(request)
    transport = _GatewayTransport(http_request, principal, request.conversation_id)
    # A fixed, inert credential is enough for the SDK: the account identity stays
    # inside the gateway. Neither Host nor forwarded headers determine a URL.
    async with AsyncOpenAI(
        api_key="rotor-admin-chat", base_url="http://rotor.internal/v1", max_retries=0,
        http_client=httpx2.AsyncClient(transport=transport, trust_env=False),
    ) as client:
        stream = (await client.responses.create(**payload) if request.api_format == "responses"
                  else await client.chat.completions.create(**payload))
        parts: list[str] = []
        terminal = False
        usage = None
        async with stream:
            async for event in stream:
                if request.api_format == "responses":
                    if event.type in {"response.output_text.delta", "response.refusal.delta"}:
                        delta = event.delta
                    else:
                        delta = ""
                    if event.type in {"response.failed", "response.incomplete", "error"}:
                        value = event.model_dump()
                        error = value.get("error") or (value.get("response") or {}).get("error") or {}
                        reason = (value.get("response") or {}).get("incomplete_details") or {}
                        raise RuntimeError(error.get("message") or reason.get("reason") or value.get("message") or "模型响应未完成")
                    if event.type == "response.completed":
                        terminal = True
                        if event.response.usage is not None:
                            usage = event.response.usage.model_dump(exclude_none=True)
                else:
                    delta = ""
                    if event.usage is not None:
                        usage = event.usage.model_dump(exclude_none=True)
                    if event.choices:
                        choice = event.choices[0]
                        delta = choice.delta.content or choice.delta.refusal or ""
                        if choice.finish_reason:
                            if choice.finish_reason not in {"stop", "length"}:
                                raise RuntimeError(f"模型响应未完成：{choice.finish_reason}")
                            terminal = True
                if delta:
                    parts.append(delta)
                    yield {"type": "delta", "content": delta}
        answer = "".join(parts)
        if not terminal or not answer.strip():
            raise RuntimeError("模型返回了空回复或流提前中断，请重试")
        yield {"type": "done", "answer": answer, "usage": usage}


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _safe_chat_stream(request: AdminChatRequest, http_request: Request, principal: AccountPrincipal) -> AsyncIterator[str]:
    try:
        async with aclosing(_completion_events(request, http_request, principal)) as events:
            async for event in events:
                yield _sse(event)
    except Exception as exc:
        yield _sse({"type": "error", "message": _failure_message(exc)})


@router.post("/completions")
async def admin_chat(request: AdminChatRequest, http_request: Request, actor: AdminActor = Depends(require_admin_access)) -> StreamingResponse:
    principal = await _chat_principal(request.model, actor)
    return StreamingResponse(
        _safe_chat_stream(request, http_request, principal), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/test")
async def test_connection(settings: ChatSettings, http_request: Request, actor: AdminActor = Depends(require_admin_access)) -> dict[str, Any]:
    return await test_connection_for_user(settings, http_request, actor)


async def test_connection_for_user(settings: ChatSettings, http_request: Request, actor: AdminActor) -> dict[str, Any]:
    principal = await _chat_principal(settings.model, actor)
    request = AdminChatRequest(
        **settings.model_dump(),
        conversation_id=f"connection-test-{uuid.uuid4().hex}",
        messages=[ChatMessage(role="user", content="Reply with OK only.")],
    )
    start = time.monotonic()
    try:
        async with aclosing(_completion_events(request, http_request, principal)) as events:
            async for event in events:
                if event["type"] == "done":
                    return {
                        "success": True, "model": settings.model, "api_format": settings.api_format,
                        "latency_ms": round((time.monotonic() - start) * 1000),
                        "message": event["answer"],
                    }
    except Exception as exc:
        status = exc.status_code if isinstance(exc, APIStatusError) else 502
        message = _failure_message(exc)
        if status == 401:
            status = 502
            message = f"上游认证失败（401）：{message}"
        raise HTTPException(status, message) from exc
    raise HTTPException(502, "模型未完成连接测试")
