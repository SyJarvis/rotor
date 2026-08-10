import time
import uuid
import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.api.v1.chat import (
    _handle_non_streaming_request,
    _handle_streaming_request,
    accounting_service,
    conversation_store,
)
from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.responses import (
    OpenAIResponsesAdapter,
    chat_response_to_responses,
    responses_required_capabilities,
    responses_request_to_chat,
)
from rotor.core.deps import get_available_channels, get_current_token
from rotor.application_settings import application_settings
from rotor.core.exceptions import (
    ChannelException,
    classify_error_status,
    format_error_message,
    normalize_upstream_error,
    upstream_error_payload,
)
from rotor.database import async_session_maker, get_db
from rotor.gateway.routing import routing_engine
from rotor.gateway.attempts import AttemptContext, attempt_recorder
from rotor.gateway.fallback import (
    retry_after_seconds,
    set_routing_headers,
    should_fallback,
)
from rotor.schemas.responses import ResponsesRequest
from rotor.models.channel import Channel
from rotor.models.response_route import ResponseRoute
from rotor.services.response_routes import get_response_route, save_response_route
from httpx import AsyncClient, HTTPStatusError, RequestError


router = APIRouter()
logger = logging.getLogger(__name__)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ResponsesStreamTransform:
    """Convert Chat Completion chunks into Responses semantic SSE events."""

    def __init__(self, model: str):
        self.response_id = f"resp_{uuid.uuid4().hex[:24]}"
        self.model = model
        self.sequence = 0
        self.next_output_index = 0
        self.message_item_id: str | None = None
        self.message_output_index: int | None = None
        self.text_parts: list[str] = []
        self.tool_items: dict[int, dict[str, Any]] = {}

    def _event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {
            "type": event_type,
            "sequence_number": self.sequence,
            **payload,
        }
        self.sequence += 1
        return event

    def _response(self, status: str, *, usage=None) -> dict[str, Any]:
        response = {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": self.model,
            "output": [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }
        if usage is not None:
            response["usage"] = {
                **usage,
                "input_tokens_details": {
                    "cached_tokens": 0,
                    **(usage.get("input_tokens_details") or {}),
                },
                "output_tokens_details": {
                    "reasoning_tokens": 0,
                    **(usage.get("output_tokens_details") or {}),
                },
            }
        return response

    def start(self) -> list[dict[str, Any]]:
        return [
            self._event("response.created", response=self._response("in_progress")),
            self._event("response.in_progress", response=self._response("in_progress")),
        ]

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        events: list[dict[str, Any]] = []
        text = delta.get("content")
        if text:
            if self.message_item_id is None:
                self.message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
                self.message_output_index = self.next_output_index
                self.next_output_index += 1
                item = {
                    "id": self.message_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                events.append(self._event(
                    "response.output_item.added",
                    output_index=self.message_output_index,
                    item=item,
                ))
                events.append(self._event(
                    "response.content_part.added",
                    item_id=self.message_item_id,
                    output_index=self.message_output_index,
                    content_index=0,
                    part={"type": "output_text", "text": "", "annotations": []},
                ))
            self.text_parts.append(text)
            events.append(self._event(
                "response.output_text.delta",
                item_id=self.message_item_id,
                output_index=self.message_output_index,
                content_index=0,
                delta=text,
            ))

        for fallback_index, tool_call in enumerate(delta.get("tool_calls") or []):
            tool_index = int(tool_call.get("index", fallback_index))
            function = tool_call.get("function") or {}
            state = self.tool_items.get(tool_index)
            if state is None:
                state = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "call_id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
                    "name": function.get("name") or "",
                    "arguments": [],
                    "output_index": self.next_output_index,
                }
                self.next_output_index += 1
                self.tool_items[tool_index] = state
                events.append(self._event(
                    "response.output_item.added",
                    output_index=state["output_index"],
                    item={
                        "id": state["id"],
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": state["call_id"],
                        "name": state["name"],
                        "arguments": "",
                    },
                ))
            if tool_call.get("id"):
                state["call_id"] = tool_call["id"]
            if function.get("name"):
                state["name"] = function["name"]
            arguments = function.get("arguments")
            if arguments:
                arguments = _stringify(arguments)
                state["arguments"].append(arguments)
                events.append(self._event(
                    "response.function_call_arguments.delta",
                    item_id=state["id"],
                    output_index=state["output_index"],
                    delta=arguments,
                ))
        return events

    def finish(self, usage: dict[str, int]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        indexed_output: list[tuple[int, dict[str, Any]]] = []
        if self.message_item_id is not None:
            text = "".join(self.text_parts)
            part = {"type": "output_text", "text": text, "annotations": []}
            item = {
                "id": self.message_item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [part],
            }
            events.extend([
                self._event(
                    "response.output_text.done",
                    item_id=self.message_item_id,
                    output_index=self.message_output_index,
                    content_index=0,
                    text=text,
                ),
                self._event(
                    "response.content_part.done",
                    item_id=self.message_item_id,
                    output_index=self.message_output_index,
                    content_index=0,
                    part=part,
                ),
                self._event(
                    "response.output_item.done",
                    output_index=self.message_output_index,
                    item=item,
                ),
            ])
            indexed_output.append((int(self.message_output_index or 0), item))

        for tool_index, state in self.tool_items.items():
            arguments = "".join(state["arguments"])
            item = {
                "id": state["id"],
                "type": "function_call",
                "status": "completed",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": arguments,
            }
            events.extend([
                self._event(
                    "response.function_call_arguments.done",
                    item_id=state["id"],
                    output_index=state["output_index"],
                    name=state["name"],
                    arguments=arguments,
                ),
                self._event(
                    "response.output_item.done",
                    output_index=state["output_index"],
                    item=item,
                ),
            ])
            indexed_output.append((state["output_index"], item))

        output = [item for _, item in sorted(indexed_output, key=lambda pair: pair[0])]
        completed = self._response("completed", usage=usage)
        completed["output"] = output
        events.append(self._event("response.completed", response=completed))
        return events

    def fail(self, message: str, code: str = "stream_error") -> list[dict[str, Any]]:
        failed = self._response("failed")
        failed["error"] = {"code": code, "message": message}
        failed["incomplete_details"] = None
        return [self._event("response.failed", response=failed)]


@router.post("/responses", response_model=None)
async def create_response(
    request: ResponsesRequest,
    http_request: Request,
    api_response: Response,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """Minimal OpenAI Responses API-compatible endpoint."""
    chat_request = responses_request_to_chat(request)

    previous_route = None
    if request.previous_response_id:
        previous_route = await get_response_route(
            db, request.previous_response_id, token.id
        )
        if previous_route is None:
            raise HTTPException(status_code=404, detail="Response not found")
        previous_channel = await db.get(Channel, previous_route.channel_id)
        if (
            previous_channel is None
            or str(previous_channel.protocol or "").lower()
            not in {"responses", "openai_responses"}
            or (
                token.allowed_channels
                and previous_channel.id not in token.allowed_channels
            )
        ):
            raise HTTPException(status_code=404, detail="Response not found")
        channels = [previous_channel]
    else:
        channels = await get_available_channels(chat_request.model, token, db)
    request_id = http_request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
    conversation_id = (
        http_request.headers.get("X-Conversation-Id")
        or (request.metadata or {}).get("conversation_id")
        or f"conv_{uuid.uuid4().hex[:24]}"
    )
    client_ip = http_request.client.host if http_request.client else "unknown"
    request_origin = getattr(http_request.state, "request_origin", "client")
    agent_run_id = getattr(http_request.state, "agent_run_id", None)
    start_time = time.time()

    if previous_route is not None:
        # Stateful Responses must return to the account that owns the previous
        # response; fallback to another provider would break the state chain.
        routing_candidates = channels
    else:
        affinity_used = application_settings.get().routing.affinity_enabled
        required_capabilities = responses_required_capabilities(request)
        routing_decision = routing_engine.route(
            channels,
            model=chat_request.model,
            token=token,
            request_protocol="openai_responses",
            required_capabilities=required_capabilities,
            affinity_key=conversation_id if affinity_used else None,
        )
        routing_candidates = routing_decision.candidates
        # Prefer a lossless native Responses upstream, but retain converted
        # Chat/Anthropic candidates for Codex requests containing optional
        # hosted or namespace tools that those protocols cannot represent.
        routing_candidates.sort(
            key=lambda candidate: str(candidate.protocol or "").lower()
            not in {"responses", "openai_responses"}
        )
        accounting_service.record_routing_decision(
            db,
            request_id=request_id,
            token=token,
            model=chat_request.model,
            request_protocol="openai_responses",
            decision=routing_decision,
            required_capabilities=required_capabilities,
            affinity_used=affinity_used,
            features={
                "stream": bool(chat_request.stream),
                "has_tools": bool(request.tools),
                "background": bool(request.background),
            },
        )
        await db.commit()

    if not routing_candidates:
        required_capabilities = responses_required_capabilities(request)
        required = sorted(required_capabilities)
        channel_summary = routing_engine.diagnose(
            channels,
            chat_request.model,
            required_capabilities,
        )
        logger.warning(
            "No compatible Responses channel for model=%s required=%s channels=%s",
            chat_request.model,
            required,
            channel_summary,
        )
        raise ChannelException(
            f"No compatible channel available for Responses model "
            f"'{chat_request.model}'. Required capabilities: {required}; "
            f"available channels: {channel_summary}",
            status_code=503,
        )

    http_client = AsyncClient(timeout=120.0)
    last_error = None
    conversation_handle = await conversation_store.start(
        db,
        conversation_id=conversation_id,
        request_id=request_id,
        token=token,
        request=chat_request,
        protocol="openai_responses",
    )
    try:
        for attempt, channel in enumerate(routing_candidates):
            attempt_context = AttemptContext.start(
                attempt,
                request_origin=request_origin,
                agent_run_id=agent_run_id,
            )
            try:
                routing_engine.begin_attempt(chat_request.model, channel)
                adapter = AdapterFactory.create_adapter(channel, http_client)
                await conversation_store.append_routing(conversation_handle, channel)

                if chat_request.stream:
                    response = await adapter.make_request(chat_request)
                    native_responses = bool(
                        getattr(adapter, "native_responses", False)
                        and chat_request.responses_payload is not None
                    )
                    async def persist_stream_response(
                        native_response: dict[str, Any], usage_accounted: bool
                    ) -> None:
                        async with async_session_maker() as route_db:
                            await save_response_route(
                                route_db,
                                response_id=native_response["id"],
                                token_id=token.id,
                                channel_id=channel.id,
                                conversation_id=conversation_id,
                                model=native_response.get("model") or chat_request.model,
                                status=native_response.get("status"),
                                usage_accounted=usage_accounted,
                            )
                            await route_db.commit()

                    result = await _handle_streaming_request(
                        chat_request,
                        adapter,
                        channel,
                        token,
                        db,
                        start_time,
                        http_request,
                        http_client,
                        request_id,
                        conversation_handle,
                        request_protocol="openai_responses",
                        stream_transform=(
                            None
                            if native_responses
                            else ResponsesStreamTransform(chat_request.model)
                        ),
                        response=response,
                        native_responses_stream=native_responses,
                        native_response_callback=(
                            persist_stream_response if native_responses else None
                        ),
                        attempt_context=attempt_context,
                    )
                    set_routing_headers(
                        result, channel, chat_request.model, attempt > 0
                    )
                    return result

                response_data = await _handle_non_streaming_request(
                    chat_request,
                    adapter,
                    channel,
                    token,
                    db,
                    start_time,
                    http_request,
                    request_id,
                    conversation_handle,
                    request_protocol="openai_responses",
                    attempt_context=attempt_context,
                )
                set_routing_headers(
                    api_response, channel, chat_request.model, attempt > 0
                )
                if response_data.get("object") == "response":
                    response_id = response_data.get("id")
                    if response_id:
                        await save_response_route(
                            db,
                            response_id=response_id,
                            token_id=token.id,
                            channel_id=channel.id,
                            conversation_id=conversation_id,
                            model=response_data.get("model") or chat_request.model,
                            status=response_data.get("status"),
                            usage_accounted=response_data.get("usage") is not None,
                        )
                        await db.commit()
                    return response_data
                return chat_response_to_response(response_data)

            except (HTTPStatusError, RequestError) as exc:
                last_error = exc
                latency_ms = int((time.time() - start_time) * 1000)
                error_fact = normalize_upstream_error(exc)
                await attempt_recorder.record(
                    context=attempt_context,
                    request_id=request_id,
                    channel=channel,
                    requested_model=chat_request.model,
                    provider_model=(channel.model_mapping or {}).get(
                        chat_request.model, chat_request.model
                    ),
                    request_protocol="openai_responses",
                    outcome="failed",
                    error=error_fact,
                )
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="openai_responses",
                    token=token,
                    channel=channel,
                    model=chat_request.model,
                    error_code=type(exc).__name__,
                    error_message=format_error_message(exc),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                    provider_response=upstream_error_payload(exc),
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle,
                        type(exc).__name__,
                        format_error_message(exc),
                    )
                await db.commit()
                if should_fallback(exc):
                    routing_engine.mark_unavailable(
                        chat_request.model,
                        channel,
                        retry_after_seconds(exc),
                    )
                    continue
                if conversation_handle:
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                raise

            except Exception as exc:
                last_error = exc
                latency_ms = int((time.time() - start_time) * 1000)
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="openai_responses",
                    token=token,
                    channel=channel,
                    model=chat_request.model,
                    error_code=type(exc).__name__,
                    error_message=format_error_message(exc),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle,
                        type(exc).__name__,
                        format_error_message(exc),
                    )
                await db.commit()
                if conversation_handle:
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                raise
    finally:
        if not chat_request.stream:
            await http_client.aclose()

    await conversation_store.finish(
        conversation_handle, "failed", int((time.time() - start_time) * 1000)
    )
    raise ChannelException(
        f"All channels failed for model '{chat_request.model}'",
        status_code=classify_error_status(last_error),
        original_error=format_error_message(last_error),
    )


async def _resolve_response_channel(
    db: AsyncSession,
    response_id: str,
    token,
) -> tuple[ResponseRoute, Channel]:
    route = await get_response_route(db, response_id, token.id)
    if route is None:
        raise HTTPException(status_code=404, detail="Response not found")
    channel = await db.get(Channel, route.channel_id)
    if (
        channel is None
        or str(channel.protocol or "").lower()
        not in {"responses", "openai_responses"}
        or (token.allowed_channels and channel.id not in token.allowed_channels)
    ):
        # Do not disclose whether another token owns a response or whether its
        # original channel has since been removed from this token's access.
        raise HTTPException(status_code=404, detail="Response not found")
    return route, channel


def _upstream_response(response) -> Response:
    media_type = response.headers.get("content-type", "application/json").split(";", 1)[0]
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=media_type,
    )


def _upstream_error(exc: HTTPStatusError) -> Response:
    return _upstream_response(exc.response)


async def _account_deferred_response_usage(
    db: AsyncSession,
    *,
    route: ResponseRoute,
    channel: Channel,
    token,
    payload: dict[str, Any],
    client_ip: str,
    latency_ms: int,
) -> None:
    """Account background usage once, even when a response is polled repeatedly."""
    usage = payload.get("usage")
    if usage is None or route.usage_accounted:
        return
    claimed = await db.execute(
        update(ResponseRoute)
        .where(
            ResponseRoute.id == route.id,
            ResponseRoute.usage_accounted.is_(False),
        )
        .values(usage_accounted=True)
    )
    if claimed.rowcount != 1:
        return
    route.usage_accounted = True
    model = route.model or str(payload.get("model") or "unknown")
    await accounting_service.record_success(
        db,
        request_id=f"req_{uuid.uuid4().hex}",
        conversation_id=route.conversation_id,
        request_protocol="openai_responses",
        token=token,
        channel=channel,
        model=model,
        provider_model=str(payload.get("model") or channel.model_mapping.get(model, model)),
        usage=accounting_service.extract_usage({"usage": usage}),
        latency_ms=latency_ms,
        client_ip=client_ip,
    )


async def _native_collection_request(
    *,
    body: dict[str, Any],
    operation: str,
    token,
    db: AsyncSession,
) -> Response:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=422, detail="Field 'model' is required")
    channels = await get_available_channels(model, token, db)
    candidates = routing_engine.route(
        channels,
        model=model,
        token=token,
        request_protocol="openai_responses",
        required_capabilities={"responses_native"},
    ).candidates
    if not candidates:
        raise ChannelException(
            f"No native Responses channel available for model '{model}'",
            status_code=503,
        )

    last_error = None
    async with AsyncClient(timeout=120.0) as http_client:
        for channel in candidates:
            adapter = AdapterFactory.create_adapter(channel, http_client)
            if not isinstance(adapter, OpenAIResponsesAdapter):
                continue
            try:
                if operation == "input_tokens":
                    upstream = await adapter.count_input_tokens(body)
                else:
                    upstream = await adapter.compact_response(body)
                return _upstream_response(upstream)
            except (HTTPStatusError, RequestError) as exc:
                last_error = exc
                if should_fallback(exc):
                    continue
                if isinstance(exc, HTTPStatusError):
                    return _upstream_error(exc)
                raise
    if isinstance(last_error, HTTPStatusError):
        return _upstream_error(last_error)
    raise ChannelException(
        f"All native Responses channels failed for model '{model}'",
        status_code=classify_error_status(last_error),
        original_error=format_error_message(last_error),
    )


@router.post("/responses/input_tokens", response_model=None)
async def count_response_input_tokens(
    body: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """Proxy the native Responses input-token counting endpoint."""
    return await _native_collection_request(
        body=body, operation="input_tokens", token=token, db=db
    )


@router.post("/responses/compact", response_model=None)
async def compact_response(
    body: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """Proxy native server-side conversation compaction."""
    return await _native_collection_request(
        body=body, operation="compact", token=token, db=db
    )


@router.get("/responses/{response_id}", response_model=None)
async def retrieve_response(
    response_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """Retrieve or resume-stream a response from its original upstream."""
    started_at = time.time()
    route, channel = await _resolve_response_channel(db, response_id, token)
    params = [(key, value) for key, value in http_request.query_params.multi_items()]
    stream = str(http_request.query_params.get("stream", "false")).lower() == "true"
    http_client = AsyncClient(timeout=120.0)
    adapter = AdapterFactory.create_adapter(channel, http_client)
    if not isinstance(adapter, OpenAIResponsesAdapter):
        await http_client.aclose()
        raise HTTPException(status_code=404, detail="Response not found")
    try:
        upstream = await adapter.retrieve_response(
            response_id, params=params, stream=stream
        )
    except HTTPStatusError as exc:
        await http_client.aclose()
        return _upstream_error(exc)
    except Exception:
        await http_client.aclose()
        raise

    if stream:
        client_ip = http_request.client.host if http_request.client else "unknown"

        async def relay():
            buffer = b""
            terminal_response: dict[str, Any] | None = None
            try:
                async for chunk in upstream.aiter_raw():
                    buffer += chunk
                    normalized = buffer.replace(b"\r\n", b"\n")
                    while b"\n\n" in normalized:
                        block, normalized = normalized.split(b"\n\n", 1)
                        data = b"\n".join(
                            line[5:].lstrip()
                            for line in block.split(b"\n")
                            if line.startswith(b"data:")
                        )
                        if data and data != b"[DONE]":
                            try:
                                event = json.loads(data)
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                event = None
                            if isinstance(event, dict) and event.get("type") in {
                                "response.completed",
                                "response.failed",
                                "response.cancelled",
                                "response.incomplete",
                            }:
                                terminal_response = event.get("response") or None
                    buffer = normalized
                    yield chunk
            finally:
                await upstream.aclose()
                await http_client.aclose()
                if terminal_response and terminal_response.get("id") == response_id:
                    try:
                        async with async_session_maker() as stream_db:
                            stream_route = await get_response_route(
                                stream_db, response_id, token.id
                            )
                            stream_channel = await stream_db.get(Channel, channel.id)
                            stream_token = await stream_db.get(type(token), token.id)
                            if stream_route and stream_channel and stream_token:
                                stream_route.status = (
                                    terminal_response.get("status") or stream_route.status
                                )
                                await _account_deferred_response_usage(
                                    stream_db,
                                    route=stream_route,
                                    channel=stream_channel,
                                    token=stream_token,
                                    payload=terminal_response,
                                    client_ip=client_ip,
                                    latency_ms=int((time.time() - started_at) * 1000),
                                )
                                await stream_db.commit()
                    except Exception:
                        logger.exception(
                            "Failed to persist streamed response retrieval for %s",
                            response_id,
                        )

        return StreamingResponse(relay(), media_type="text/event-stream")

    try:
        payload = upstream.json()
        if isinstance(payload, dict):
            route.status = payload.get("status") or route.status
            await _account_deferred_response_usage(
                db,
                route=route,
                channel=channel,
                token=token,
                payload=payload,
                client_ip=http_request.client.host if http_request.client else "unknown",
                latency_ms=int((time.time() - started_at) * 1000),
            )
            await db.commit()
        return _upstream_response(upstream)
    finally:
        await upstream.aclose()
        await http_client.aclose()


@router.delete("/responses/{response_id}", response_model=None)
async def delete_response(
    response_id: str,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """Delete a stored response from its upstream and forget local routing."""
    route, channel = await _resolve_response_channel(db, response_id, token)
    async with AsyncClient(timeout=120.0) as http_client:
        adapter = AdapterFactory.create_adapter(channel, http_client)
        if not isinstance(adapter, OpenAIResponsesAdapter):
            raise HTTPException(status_code=404, detail="Response not found")
        try:
            upstream = await adapter.delete_response(response_id)
        except HTTPStatusError as exc:
            return _upstream_error(exc)
        result = _upstream_response(upstream)
        await db.delete(route)
        await db.commit()
        return result


@router.post("/responses/{response_id}/cancel", response_model=None)
async def cancel_response(
    response_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """Cancel a background response on its original upstream."""
    started_at = time.time()
    route, channel = await _resolve_response_channel(db, response_id, token)
    async with AsyncClient(timeout=120.0) as http_client:
        adapter = AdapterFactory.create_adapter(channel, http_client)
        if not isinstance(adapter, OpenAIResponsesAdapter):
            raise HTTPException(status_code=404, detail="Response not found")
        try:
            upstream = await adapter.cancel_response(response_id)
        except HTTPStatusError as exc:
            return _upstream_error(exc)
        try:
            payload = upstream.json()
            if isinstance(payload, dict):
                route.status = payload.get("status") or route.status
                await _account_deferred_response_usage(
                    db,
                    route=route,
                    channel=channel,
                    token=token,
                    payload=payload,
                    client_ip=http_request.client.host if http_request.client else "unknown",
                    latency_ms=int((time.time() - started_at) * 1000),
                )
                await db.commit()
            return _upstream_response(upstream)
        finally:
            await upstream.aclose()


@router.get("/responses/{response_id}/input_items", response_model=None)
async def list_response_input_items(
    response_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """List the input items belonging to a stored response."""
    _, channel = await _resolve_response_channel(db, response_id, token)
    params = [(key, value) for key, value in http_request.query_params.multi_items()]
    async with AsyncClient(timeout=120.0) as http_client:
        adapter = AdapterFactory.create_adapter(channel, http_client)
        if not isinstance(adapter, OpenAIResponsesAdapter):
            raise HTTPException(status_code=404, detail="Response not found")
        try:
            upstream = await adapter.list_input_items(response_id, params=params)
        except HTTPStatusError as exc:
            return _upstream_error(exc)
        return _upstream_response(upstream)


def chat_response_to_response(chat_response: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible import for existing callers/tests."""
    return chat_response_to_responses(chat_response)
