"""OpenAI Responses protocol adapter and protocol conversion helpers."""

from __future__ import annotations

from copy import deepcopy
import json
import time
import uuid
from typing import Any, AsyncIterator
from urllib.parse import quote

from httpx import HTTPStatusError, Response, Timeout

from rotor.adapters.base import BaseAdapter
from rotor.core.exceptions import UpstreamOverloaded, is_overload_error_signal
from rotor.schemas.request import (
    ChatCompletionRequest,
    ChatMessage,
    Function,
    FunctionCall,
    Role,
    Tool,
    ToolCall,
)
from rotor.schemas.responses import ResponsesRequest


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _supports_gpt56_prompt_cache(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized == "gpt-5.6" or normalized.startswith("gpt-5.6-")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"input_text", "output_text", "text"}:
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "refusal":
            parts.append(str(block.get("refusal") or ""))
    return "".join(parts)


def responses_tools_to_chat(tools: list[dict[str, Any]] | None) -> list[Tool] | None:
    """Convert function tools; hosted/namespace tools remain native-only features.

    A Responses client such as Codex may advertise hosted or namespace tools
    alongside ordinary local function tools. When falling back to a Chat
    upstream, keep the usable function subset instead of rejecting the whole
    request before routing.
    """
    converted: list[Tool] = []
    for tool in tools or []:
        if tool.get("type") != "function":
            continue
        converted.append(Tool(
            type="function",
            function=Function(
                name=str(tool["name"]),
                description=tool.get("description"),
                parameters=tool.get("parameters"),
                strict=tool.get("strict"),
            ),
        ))
    return converted or None


def responses_tool_choice_to_chat(choice: Any) -> Any:
    if not isinstance(choice, dict) or choice.get("type") != "function":
        return choice
    return {
        "type": "function",
        "function": {"name": choice.get("name")},
    }


def responses_input_to_chat_messages(input_data: Any) -> list[ChatMessage]:
    """Convert Responses input items into the common chat representation."""
    if isinstance(input_data, str):
        return [ChatMessage(role=Role.USER, content=input_data)]

    messages: list[ChatMessage] = []
    pending_calls: list[ToolCall] = []

    def flush_calls() -> None:
        if pending_calls:
            messages.append(ChatMessage(
                role=Role.ASSISTANT,
                content=None,
                tool_calls=list(pending_calls),
            ))
            pending_calls.clear()

    for item in input_data if isinstance(input_data, list) else []:
        if isinstance(item, str):
            flush_calls()
            messages.append(ChatMessage(role=Role.USER, content=item))
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "function_call":
            pending_calls.append(ToolCall(
                id=str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"),
                type="function",
                function=FunctionCall(
                    name=str(item.get("name") or ""),
                    arguments=_stringify(
                        "" if item.get("arguments") is None else item.get("arguments")
                    ),
                ),
            ))
            continue

        flush_calls()
        if item_type == "function_call_output":
            messages.append(ChatMessage(
                role=Role.TOOL,
                tool_call_id=str(item.get("call_id") or ""),
                content=_stringify(
                    "" if item.get("output") is None else item.get("output")
                ),
            ))
            continue

        if item_type not in {None, "message"}:
            # Non-message native items (reasoning, hosted tools, etc.) cannot be
            # represented by Chat Completions. They remain in responses_payload
            # for native upstreams and are intentionally absent here.
            continue

        role = str(item.get("role") or "user")
        if role == "developer":
            role = "system"
        messages.append(ChatMessage(
            role=Role(role),
            content=_content_text(item.get("content", "")),
        ))

    flush_calls()
    return messages or [ChatMessage(role=Role.USER, content="")]


def responses_request_to_chat(request: ResponsesRequest) -> ChatCompletionRequest:
    messages = responses_input_to_chat_messages(request.input)
    tools = responses_tools_to_chat(request.tools)
    if request.instructions:
        instructions = _content_text(request.instructions)
        if instructions:
            messages.insert(0, ChatMessage(role=Role.SYSTEM, content=instructions))
    return ChatCompletionRequest(
        model=request.model,
        messages=messages,
        max_tokens=request.max_output_tokens,
        temperature=request.temperature,
        top_p=request.top_p,
        stream=request.stream,
        tools=tools,
        tool_choice=(
            responses_tool_choice_to_chat(request.tool_choice)
            if tools is not None
            else None
        ),
        user=request.user,
        responses_payload=request.provider_payload(),
    )


def responses_required_capabilities(request: ResponsesRequest) -> set[str]:
    """Return routing capabilities needed to avoid lossy protocol conversion."""
    required: set[str] = set()
    if request.stream:
        required.add("stream")
    if request.tools or any(
        isinstance(item, dict)
        and item.get("type") in {"function_call", "function_call_output"}
        for item in request.input if isinstance(request.input, list)
    ):
        required.add("function_call")

    native_only = bool(
        request.previous_response_id
        or request.conversation
        or request.background
    )
    if isinstance(request.input, list):
        for item in request.input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type not in {None, "message", "function_call", "function_call_output"}:
                native_only = True
            content = item.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict)
                and block.get("type") not in {"input_text", "output_text", "text"}
                for block in content
            ):
                native_only = True
    if native_only:
        required.add("responses_native")
    return required


def chat_tools_to_responses(tools: list[Tool] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "name": tool.function.name,
            **(
                {"description": tool.function.description}
                if tool.function.description is not None else {}
            ),
            "parameters": tool.function.parameters or {"type": "object", "properties": {}},
            **({"strict": tool.function.strict} if tool.function.strict is not None else {}),
        }
        for tool in tools
    ]


def chat_tool_choice_to_responses(choice: Any) -> Any:
    if not isinstance(choice, dict) or choice.get("type") != "function":
        return choice
    function = choice.get("function") or {}
    return {"type": "function", "name": function.get("name")}


def chat_content_to_responses(content: Any) -> Any:
    if not isinstance(content, list):
        return content or ""
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            blocks.append({"type": "input_text", "text": item.get("text", "")})
        elif item.get("type") == "image_url":
            image = item.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                block = {"type": "input_image", "image_url": url}
                if isinstance(image, dict) and image.get("detail"):
                    block["detail"] = image["detail"]
                blocks.append(block)
    return blocks


def chat_request_to_responses_payload(request: ChatCompletionRequest) -> dict[str, Any]:
    input_items: list[dict[str, Any]] = []
    for message in request.messages:
        if message.tool_calls:
            if message.content:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": chat_content_to_responses(message.content),
                })
            for tool_call in message.tool_calls:
                input_items.append({
                    "type": "function_call",
                    "call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                })
            continue
        if message.role == Role.TOOL:
            input_items.append({
                "type": "function_call_output",
                "call_id": message.tool_call_id or "",
                "output": message.content or "",
            })
            continue
        role = "developer" if message.role == Role.SYSTEM else message.role.value
        input_items.append({
            "type": "message",
            "role": role,
            "content": chat_content_to_responses(message.content),
        })

    payload: dict[str, Any] = {
        "model": request.model,
        "input": input_items,
        "stream": bool(request.stream),
    }
    optional = {
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_output_tokens": request.max_tokens,
        "tools": chat_tools_to_responses(request.tools),
        "tool_choice": chat_tool_choice_to_responses(request.tool_choice),
        "user": request.user,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def responses_response_to_chat(response: dict[str, Any], model: str) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if item.get("type") == "message":
            text_parts.append(_content_text(item.get("content")))
        elif item.get("type") == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": _stringify(
                        "" if item.get("arguments") is None else item.get("arguments")
                    ),
                },
            })

    usage = response.get("usage") or {}
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": response.get("id") or f"chatcmpl_{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": response.get("created_at") or int(time.time()),
        "model": response.get("model") or model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "prompt_tokens_details": usage.get("input_tokens_details") or {},
            "completion_tokens_details": usage.get("output_tokens_details") or {},
        },
    }


def chat_response_to_responses(response: dict[str, Any]) -> dict[str, Any]:
    """Convert a Chat Completions result into Responses output items."""
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output: list[dict[str, Any]] = []
    content = message.get("content")
    if content is not None:
        output.append({
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": _content_text(content),
                "annotations": [],
            }],
        })
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        output.append({
            "id": f"fc_{uuid.uuid4().hex[:24]}",
            "type": "function_call",
            "status": "completed",
            "call_id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
            "name": function.get("name") or "",
            "arguments": _stringify(
                "" if function.get("arguments") is None else function.get("arguments")
            ),
        })

    chat_usage = response.get("usage") or {}
    input_tokens = int(chat_usage.get("input_tokens") or chat_usage.get("prompt_tokens") or 0)
    output_tokens = int(chat_usage.get("output_tokens") or chat_usage.get("completion_tokens") or 0)
    usage = {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": 0,
            **(
                chat_usage.get("input_tokens_details")
                or chat_usage.get("prompt_tokens_details") or {}
            ),
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {
            "reasoning_tokens": 0,
            **(
                chat_usage.get("output_tokens_details")
                or chat_usage.get("completion_tokens_details") or {}
            ),
        },
        "total_tokens": int(chat_usage.get("total_tokens") or input_tokens + output_tokens),
    }
    return {
        "id": response.get("id") or f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": response.get("created") or int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": response.get("model"),
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": usage,
    }


class OpenAIResponsesAdapter(BaseAdapter):
    """Adapter for providers exposing the OpenAI ``/responses`` protocol."""

    native_responses = True

    async def make_request(
        self,
        request: ChatCompletionRequest,
        timeout: float | None = None,
    ) -> Response:
        try:
            return await super().make_request(request, timeout)
        except HTTPStatusError as exc:
            payload = request.responses_payload
            if (
                not isinstance(payload, dict)
                or "prompt_cache_retention" not in payload
                or not self._rejects_prompt_cache_retention(exc.response)
            ):
                raise

            await exc.response.aclose()
            retry_request = request.model_copy(deep=True)
            retry_request.responses_payload.pop("prompt_cache_retention", None)
            return await super().make_request(retry_request, timeout)

    @staticmethod
    def _rejects_prompt_cache_retention(response: Response) -> bool:
        if response.status_code != 400:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        error = payload.get("error") if isinstance(payload, dict) else None
        return (
            isinstance(error, dict)
            and error.get("param") == "prompt_cache_retention"
            and error.get("code") == "invalid_parameter"
        )

    def responses_url(self, suffix: str = "") -> str:
        """Build a resource URL from the channel's configured collection path."""
        return f"{self.build_request_url('/responses').rstrip('/')}{suffix}"

    async def request_resource(
        self,
        method: str,
        suffix: str = "",
        *,
        params: list[tuple[str, str]] | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> Response:
        """Send a protocol-native request for a Responses sub-resource."""
        from rotor.config import settings

        timeout = Timeout(
            connect=settings.CONNECT_TIMEOUT,
            read=settings.REQUEST_TIMEOUT,
            write=settings.WRITE_TIMEOUT,
            pool=settings.POOL_TIMEOUT,
        )
        headers = self.build_request_headers()
        if stream:
            headers["accept"] = "text/event-stream"
        request = self.http_client.build_request(
            method,
            self.responses_url(suffix),
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )
        response = await self.http_client.send(request, stream=stream)
        try:
            response.raise_for_status()
        except Exception:
            if stream:
                await response.aread()
            await response.aclose()
            raise
        return response

    async def retrieve_response(
        self,
        response_id: str,
        *,
        params: list[tuple[str, str]] | None = None,
        stream: bool = False,
    ) -> Response:
        return await self.request_resource(
            "GET", f"/{quote(response_id, safe='')}", params=params, stream=stream
        )

    async def delete_response(self, response_id: str) -> Response:
        return await self.request_resource("DELETE", f"/{quote(response_id, safe='')}")

    async def cancel_response(self, response_id: str) -> Response:
        return await self.request_resource(
            "POST", f"/{quote(response_id, safe='')}/cancel"
        )

    async def list_input_items(
        self,
        response_id: str,
        *,
        params: list[tuple[str, str]] | None = None,
    ) -> Response:
        return await self.request_resource(
            "GET", f"/{quote(response_id, safe='')}/input_items", params=params
        )

    async def count_input_tokens(self, body: dict[str, Any]) -> Response:
        payload = deepcopy(body)
        payload["model"] = self.map_model_name(str(payload["model"]))
        return await self.request_resource("POST", "/input_tokens", json_body=payload)

    async def compact_response(self, body: dict[str, Any]) -> Response:
        payload = deepcopy(body)
        payload["model"] = self.map_model_name(str(payload["model"]))
        return await self.request_resource("POST", "/compact", json_body=payload)

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        return self.build_request_url("/responses")

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        headers = self.build_request_headers()
        if request.stream:
            headers["accept"] = "text/event-stream"
        return headers

    async def convert_request(self, request: ChatCompletionRequest) -> dict[str, Any]:
        body = deepcopy(
            request.responses_payload
            if request.responses_payload is not None
            else chat_request_to_responses_payload(request)
        )
        mapped_model = self.map_model_name(request.model)
        body["model"] = mapped_model
        body["stream"] = bool(request.stream)
        if not _supports_gpt56_prompt_cache(mapped_model):
            return body

        if request.responses_prompt_cache_key:
            body["prompt_cache_key"] = request.responses_prompt_cache_key

        cache_content = request.responses_cacheable_system_content
        if cache_content:
            for item in body.get("input") or []:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "message"
                    and item.get("role") == "developer"
                ):
                    item["content"] = deepcopy(cache_content)
                    body["prompt_cache_options"] = {
                        "mode": "implicit",
                        "ttl": "30m",
                    }
                    break
        return body

    async def convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest,
    ) -> dict[str, Any]:
        data = response.json()
        if request.responses_payload is not None:
            return data
        return responses_response_to_chat(data, request.model)

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        tool_indexes: dict[int, int] = {}
        tool_has_arguments: set[int] = set()
        saw_tool_call = False
        chunk_id = f"chatcmpl_{uuid.uuid4().hex[:24]}"

        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            if event_type == "error" or event_type == "response.failed":
                error = event.get("error") or (event.get("response") or {}).get("error") or {}
                error_message = error.get("message") or "Responses upstream stream failed"
                error_type = error.get("type") or error.get("code")
                if is_overload_error_signal(error_type, error_message):
                    raise UpstreamOverloaded(error_message, error_type=error_type)
                raise RuntimeError(error_message)
            if request.responses_payload is not None:
                yield event
                continue
            if event_type == "response.output_text.delta":
                yield _chat_stream_chunk(
                    chunk_id,
                    request.model,
                    delta={"content": event.get("delta") or ""},
                )
            elif event_type == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") != "function_call":
                    continue
                saw_tool_call = True
                output_index = int(event.get("output_index") or 0)
                tool_index = len(tool_indexes)
                tool_indexes[output_index] = tool_index
                yield _chat_stream_chunk(
                    chunk_id,
                    request.model,
                    delta={"tool_calls": [{
                        "index": tool_index,
                        "id": item.get("call_id") or item.get("id") or "",
                        "type": "function",
                        "function": {"name": item.get("name") or "", "arguments": ""},
                    }]},
                )
                if item.get("arguments"):
                    tool_has_arguments.add(output_index)
                    yield _chat_stream_chunk(
                        chunk_id,
                        request.model,
                        delta={"tool_calls": [{
                            "index": tool_index,
                            "function": {"arguments": _stringify(item["arguments"])},
                        }]},
                    )
            elif event_type == "response.function_call_arguments.delta":
                output_index = int(event.get("output_index") or 0)
                tool_has_arguments.add(output_index)
                yield _chat_stream_chunk(
                    chunk_id,
                    request.model,
                    delta={"tool_calls": [{
                        "index": tool_indexes.setdefault(output_index, len(tool_indexes)),
                        "function": {"arguments": event.get("delta") or ""},
                    }]},
                )
            elif event_type == "response.output_item.done":
                item = event.get("item") or {}
                output_index = int(event.get("output_index") or 0)
                if (
                    item.get("type") == "function_call"
                    and item.get("arguments")
                    and output_index not in tool_has_arguments
                ):
                    yield _chat_stream_chunk(
                        chunk_id,
                        request.model,
                        delta={"tool_calls": [{
                            "index": tool_indexes.setdefault(
                                output_index, len(tool_indexes)
                            ),
                            "function": {"arguments": _stringify(item["arguments"])},
                        }]},
                    )
            elif event_type == "response.completed":
                native = event.get("response") or {}
                usage = native.get("usage") or {}
                yield _chat_stream_chunk(
                    chunk_id,
                    request.model,
                    delta={},
                    finish_reason="tool_calls" if saw_tool_call else "stop",
                    usage={
                        "prompt_tokens": int(usage.get("input_tokens") or 0),
                        "completion_tokens": int(usage.get("output_tokens") or 0),
                        "total_tokens": int(usage.get("total_tokens") or 0),
                        "prompt_tokens_details": (
                            usage.get("input_tokens_details") or {}
                        ),
                        "completion_tokens_details": (
                            usage.get("output_tokens_details") or {}
                        ),
                    },
                )


def _chat_stream_chunk(
    chunk_id: str,
    model: str,
    *,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk
