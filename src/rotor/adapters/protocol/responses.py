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
from rotor.core.exceptions import UpstreamOverloaded, UpstreamProtocolError, is_overload_error_signal
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

# Some Responses providers flush lifecycle ``*.done`` notifications after the
# response terminal event.  They carry no additional response state and are
# safe to discard, while content/error events must still fail the stream.
_POST_TERMINAL_LIFECYCLE_EVENTS = frozenset(
    {
        "response.content_part.done",
        "response.output_item.done",
        "response.output_text.done",
        "response.function_call_arguments.done",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.done",
        "response.audio.done",
        "response.refusal.done",
    }
)


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


def _content_to_chat(content: Any) -> str | list[dict[str, Any]]:
    """Convert Responses message content blocks to Chat content blocks.

    Text and URL-backed images have direct Chat Completions equivalents.  A
    caller should use ``responses_required_capabilities`` before selecting a
    converted channel; unsupported blocks are therefore not silently sent as
    an incomplete prompt here.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"input_text", "output_text", "text"}:
            parts.append({"type": "text", "text": str(block.get("text") or "")})
            continue
        if block_type == "refusal":
            parts.append({
                "type": "text",
                "text": str(block.get("refusal") or ""),
            })
            continue
        if block_type != "input_image":
            continue

        image = block.get("image_url")
        if isinstance(image, str):
            url = image
            detail = block.get("detail")
        elif isinstance(image, dict):
            url = image.get("url")
            detail = image.get("detail", block.get("detail"))
        else:
            url = None
            detail = None
        if not url:
            # ``file_id`` and other provider-native image references cannot be
            # represented by Chat Completions and are guarded by routing.
            continue
        image_part: dict[str, Any] = {
            "type": "image_url",
            "image_url": {"url": str(url)},
        }
        if detail is not None:
            image_part["image_url"]["detail"] = detail
        parts.append(image_part)

    if not parts:
        return ""
    if all(part["type"] == "text" for part in parts):
        return "".join(str(part["text"]) for part in parts)
    return parts


def _is_chat_convertible_content_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    block_type = block.get("type")
    if block_type in {"input_text", "output_text", "text", "refusal"}:
        return True
    if block_type != "input_image":
        return False
    image = block.get("image_url")
    if isinstance(image, str):
        return bool(image)
    return isinstance(image, dict) and bool(image.get("url"))


def _tool_choice_requires_native(
    request: ResponsesRequest,
    converted_tools: list[Tool] | None,
) -> bool:
    """Whether filtering Responses tools would change the requested choice."""
    choice = request.tool_choice
    if choice is None:
        return False

    function_names = {
        tool.function.name for tool in (converted_tools or [])
    }
    has_hosted_tools = any(
        isinstance(tool, dict) and tool.get("type") != "function"
        for tool in (request.tools or [])
    )
    if choice == "required":
        return not function_names or has_hosted_tools
    if isinstance(choice, str) and choice in {"auto", "none"}:
        return False
    if isinstance(choice, dict):
        return (
            choice.get("type") != "function"
            or choice.get("name") not in function_names
        )
    return True


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
            content=_content_to_chat(item.get("content", "")),
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
    if any(
        isinstance(tool, dict) and tool.get("type") == "function"
        for tool in (request.tools or [])
    ) or any(
        isinstance(item, dict)
        and item.get("type") in {"function_call", "function_call_output"}
        for item in request.input if isinstance(request.input, list)
    ):
        required.add("function_call")

    native_only = bool(
        request.previous_response_id
        or request.conversation
        or request.background
        or request.reasoning
        or request.include
        or (request.text and request.text != {"format": {"type": "text"}})
        or request.parallel_tool_calls is False
        or request.truncation not in {None, "disabled"}
        or request.max_tool_calls is not None
        or request.store is True
        or request.service_tier not in {None, "auto"}
    )
    converted_tools = responses_tools_to_chat(request.tools)
    if _tool_choice_requires_native(request, converted_tools):
        # A required/explicit hosted-tool choice cannot be honored by Chat
        # Completions after hosted tools are filtered from the request.
        native_only = True
    if isinstance(request.input, list):
        for item in request.input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type not in {None, "message", "function_call", "function_call_output"}:
                native_only = True
            content = item.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "input_image"
                for block in content
            ):
                required.add("vision")
            if isinstance(content, list) and any(
                not _is_chat_convertible_content_block(block)
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
    if request.reasoning_effort is not None:
        payload["reasoning"] = {"effort": request.reasoning_effort}
    return payload


def _validate_anthropic_response_conversion(response: dict[str, Any], *, final: bool = True) -> None:
    """Reject Responses semantics that the Anthropic conversion cannot retain."""
    if (
        not isinstance(response, dict)
        or response.get("error") is not None
        or response.get("type") == "error"
        or response.get("success") is False
        or response.get("status") not in (None, "completed")
    ):
        raise UpstreamProtocolError("Responses upstream did not return a completed response")
    if final and (response.get("status") != "completed" or not isinstance(response.get("output"), list)):
        raise UpstreamProtocolError("Responses upstream is missing a completed result and output list")
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") not in {"message", "function_call"}:
            raise UpstreamProtocolError("Responses output cannot be represented faithfully as Anthropic content")
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict) or part.get("type") not in {"output_text", "input_text", "text"}:
                    raise UpstreamProtocolError("Responses content cannot be represented faithfully as Anthropic content")


def extract_responses_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return only validated usage counters; absence is not a zero snapshot."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise UpstreamProtocolError("Responses usage must be an object")
    result: dict[str, Any] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        if name == "total_tokens" and name not in usage:
            continue
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise UpstreamProtocolError("Responses usage contains invalid token counts")
        result[name] = value
    for name in ("input_tokens_details", "output_tokens_details"):
        details = usage.get(name)
        if details is None:
            continue
        if not isinstance(details, dict) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in details.values() if value is not None
        ):
            raise UpstreamProtocolError("Responses usage contains invalid token details")
        result[name] = {key: value for key, value in details.items() if value is not None}
    return result


def _responses_protocol_error(message: str, payload=None) -> UpstreamProtocolError:
    error = UpstreamProtocolError(message)
    try:
        usage = extract_responses_usage(payload)
    except UpstreamProtocolError:
        usage = None
    if usage is not None:
        error.provider_usage = usage
    return error


def validate_responses_response(response: dict[str, Any], *, allow_background: bool = False) -> dict[str, Any]:
    """Validate terminal ownership before an entry point records success."""
    if not isinstance(response, dict):
        raise _responses_protocol_error("Responses upstream returned a non-object response")
    if response.get("error") is not None or response.get("type") == "error" or response.get("success") is False:
        raise _responses_protocol_error("Responses upstream returned an error response", response)
    allowed = {"completed", "incomplete"}
    if allow_background:
        allowed.update({"queued", "in_progress"})
    if (
        response.get("object") != "response"
        or not isinstance(response.get("id"), str)
        or not response["id"].strip()
        or not isinstance(response.get("status"), str)
        or response["status"] not in allowed
        or not isinstance(response.get("output"), list)
    ):
        raise _responses_protocol_error("Responses upstream did not return a valid result state and output list", response)
    extract_responses_usage(response)
    return response


def _responses_chat_finish_reason(response: dict[str, Any], has_tool_calls: bool) -> str:
    if response.get("status") == "incomplete":
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        mapped = {"max_output_tokens": "length", "content_filter": "content_filter"}.get(reason) if isinstance(reason, str) else None
        if mapped is None:
            raise _responses_protocol_error("Responses incomplete reason cannot be represented as a Chat finish reason", response)
        return mapped
    return "tool_calls" if has_tool_calls else "stop"


def _responses_chat_usage(response: dict[str, Any]) -> dict[str, Any] | None:
    usage = extract_responses_usage(response)
    if usage is None:
        return None
    return {
        "prompt_tokens": usage["input_tokens"],
        "completion_tokens": usage["output_tokens"],
        "total_tokens": usage.get("total_tokens", usage["input_tokens"] + usage["output_tokens"]),
        "prompt_tokens_details": usage.get("input_tokens_details", {}),
        "completion_tokens_details": usage.get("output_tokens_details", {}),
    }


async def _responses_sse_frames(response: Response) -> AsyncIterator[tuple[str | None, str]]:
    data_lines: list[str] = []
    event_name = None
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            data_lines, event_name = [], None
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_name = value
    if data_lines:
        yield event_name, "\n".join(data_lines)


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
            "finish_reason": _responses_chat_finish_reason(response, bool(tool_calls)),
        }],
        "usage": _responses_chat_usage(response),
    }


def unsupported_chat_reasoning_fields(payload: dict[str, Any]) -> list[str]:
    """Identify provider reasoning extensions without a Responses mapping."""
    return [
        field for field in (
            "reasoning_content", "reasoning", "reasoning_details",
            "thinking", "thinking_content",
        )
        if payload.get(field)
        and not (field == "reasoning_content" and isinstance(payload[field], str))
    ]


def chat_response_to_responses(response: dict[str, Any]) -> dict[str, Any]:
    """Convert a Chat Completions result into Responses output items."""
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    incomplete_reason = {
        "length": "max_output_tokens",
        "content_filter": "content_filter",
    }.get(choice.get("finish_reason"))
    status = "incomplete" if incomplete_reason else "completed"
    unsupported_reasoning = unsupported_chat_reasoning_fields(message)
    output: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        output.append({
            "id": f"rs_{uuid.uuid4().hex[:24]}",
            "type": "reasoning",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": reasoning}],
            "status": status,
        })
    content = message.get("content")
    content_parts: list[dict[str, Any]] = []
    if content is not None:
        content_parts.append({
            "type": "output_text",
            "text": _content_text(content),
            "annotations": [],
        })
    if message.get("refusal"):
        content_parts.append({"type": "refusal", "refusal": message["refusal"]})
    if content_parts:
        output.append({
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": content_parts,
        })
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        output.append({
            "id": f"fc_{uuid.uuid4().hex[:24]}",
            "type": "function_call",
            "status": status,
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
    } if chat_usage else None
    return {
        "id": response.get("id") or f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": response.get("created") or int(time.time()),
        "status": "failed" if unsupported_reasoning else status,
        "error": {
            "code": "server_error",
            "message": "Upstream reasoning output requires a native Responses channel",
        } if unsupported_reasoning else None,
        "incomplete_details": (
            {"reason": incomplete_reason} if incomplete_reason else None
        ),
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

    @staticmethod
    def _stream_error_details(
        event: dict[str, Any],
        event_type: str,
    ) -> tuple[str, str | None]:
        """Extract a Responses stream error's message and provider code.

        The canonical ``type: error`` event keeps ``code`` and ``message`` at
        the top level.  ``response.failed`` normally nests them under
        ``response.error``.  Accept both forms (and the older nested
        ``error`` form used by several compatible providers).
        """
        nested_error: Any = None
        if event_type == "error":
            nested_error = event.get("error")
        elif event_type == "response.failed":
            response = event.get("response")
            if isinstance(response, dict):
                nested_error = response.get("error")
            if not isinstance(nested_error, (dict, str)):
                nested_error = event.get("error")

        if isinstance(nested_error, str):
            message = nested_error
            error_type = event.get("code")
        elif isinstance(nested_error, dict):
            message = nested_error.get("message")
            nested_type = nested_error.get("type")
            error_type = nested_type or nested_error.get("code")
            if event_type == "error" and nested_type == event_type:
                error_type = nested_error.get("code")
            # Some providers put only part of the error in the nested object.
            if not message:
                message = event.get("message")
            if not error_type:
                error_type = event.get("code")
        else:
            # Canonical Responses ``error`` events use the event itself as
            # the payload.  Its ``type`` is the discriminator ("error"), so
            # prefer the actual provider code and never report that
            # discriminator as the error type.
            message = event.get("message")
            error_type = event.get("code")
            if not error_type and event.get("type") != event_type:
                error_type = event.get("type")

        if message is None or message == "":
            message = "Responses upstream stream failed"
        return str(message), str(error_type) if error_type else None

    async def make_request(
        self,
        request: ChatCompletionRequest,
        timeout: float | None = None,
    ) -> Response:
        retry_request = request
        # Keep each compatibility fallback bounded to one retry.
        removed_retention = False
        removed_breakpoint = False
        while True:
            try:
                return await super().make_request(retry_request, timeout)
            except HTTPStatusError as exc:
                payload = retry_request.responses_payload
                if (
                    not removed_retention
                    and isinstance(payload, dict)
                    and "prompt_cache_retention" in payload
                    and self._rejects_prompt_cache_retention(exc.response)
                ):
                    await exc.response.aclose()
                    retry_request = retry_request.model_copy(deep=True)
                    retry_request.responses_payload.pop("prompt_cache_retention", None)
                    removed_retention = True
                    continue

                if (
                    not removed_breakpoint
                    and self._request_has_prompt_cache_breakpoint(retry_request)
                    and self._rejects_prompt_cache_breakpoint(exc.response)
                ):
                    await exc.response.aclose()
                    retry_request = retry_request.model_copy(deep=True)
                    self._remove_prompt_cache_breakpoints(retry_request.responses_payload)
                    # Anthropic→Responses conversion injects the nested
                    # breakpoint from this internal hint when no native
                    # payload is present. Strip it in the copied hint so
                    # conversion keeps the remaining cache options without
                    # recreating the rejected field.
                    self._remove_prompt_cache_breakpoints(
                        retry_request.responses_cacheable_system_content
                    )
                    removed_breakpoint = True
                    continue

                raise

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

    @staticmethod
    def _request_has_prompt_cache_breakpoint(request: ChatCompletionRequest) -> bool:
        return OpenAIResponsesAdapter._contains_prompt_cache_breakpoint(
            request.responses_payload
        ) or OpenAIResponsesAdapter._contains_prompt_cache_breakpoint(
            request.responses_cacheable_system_content
        )

    @staticmethod
    def _contains_prompt_cache_breakpoint(value: Any) -> bool:
        if isinstance(value, dict):
            return "prompt_cache_breakpoint" in value or any(
                OpenAIResponsesAdapter._contains_prompt_cache_breakpoint(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(
                OpenAIResponsesAdapter._contains_prompt_cache_breakpoint(item)
                for item in value
            )
        return False

    @staticmethod
    def _remove_prompt_cache_breakpoints(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("prompt_cache_breakpoint", None)
            for item in value.values():
                OpenAIResponsesAdapter._remove_prompt_cache_breakpoints(item)
        elif isinstance(value, list):
            for item in value:
                OpenAIResponsesAdapter._remove_prompt_cache_breakpoints(item)

    @staticmethod
    def _rejects_prompt_cache_breakpoint(response: Response) -> bool:
        if response.status_code != 400:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict) or error.get("code") != "invalid_parameter":
            return False
        message = error.get("message")
        param = error.get("param")
        param_matches = (
            isinstance(param, str)
            and param.strip().lower().rsplit(".", 1)[-1]
            == "prompt_cache_breakpoint"
        )
        if param_matches and not message:
            return True
        if not isinstance(message, str):
            return param_matches
        message = message.lower()
        if param_matches and "prompt_cache_breakpoint" not in message:
            return False
        return (
            "prompt_cache_breakpoint" in message
            and ("is not supported" in message or "unsupported" in message)
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
        try:
            try:
                data = response.json()
            except ValueError:
                raise UpstreamProtocolError("Responses upstream returned invalid JSON") from None
            if isinstance(data, dict) and (data.get("error") is not None or data.get("type") == "error" or data.get("success") is False):
                message, error_type = self._stream_error_details(data, "error")
                if is_overload_error_signal(error_type, message):
                    secret = getattr(self.channel, "key", None)
                    if secret:
                        message = message.replace(secret, "[redacted]")
                    error = UpstreamOverloaded(message, error_type=error_type)
                    usage = getattr(_responses_protocol_error(message, data), "provider_usage", None)
                    if usage is not None:
                        error.provider_usage = usage
                    error.upstream_status = response.status_code
                    raise error
            validate_responses_response(data, allow_background=request.responses_payload is not None)
            if request.responses_payload is not None:
                return data
            if request.anthropic_payload is not None:
                _validate_anthropic_response_conversion(data)
            return responses_response_to_chat(data, request.model)
        except UpstreamProtocolError as exc:
            exc.upstream_status = response.status_code
            raise

    async def _validated_stream_events(self, response: Response) -> AsyncIterator[dict[str, Any]]:
        pending_terminal = None
        done_seen = False
        try:
            async for event_name, data in _responses_sse_frames(response):
                data = data.strip()
                if data == "[DONE]":
                    if pending_terminal is None or done_seen or event_name:
                        raise UpstreamProtocolError("Responses stream has no valid terminal event")
                    done_seen = True
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    raise UpstreamProtocolError("Responses upstream returned malformed SSE JSON") from None
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise UpstreamProtocolError("Responses upstream returned an invalid SSE event")
                if event_name and event_name != event["type"]:
                    raise UpstreamProtocolError("Responses SSE event name conflicts with payload type")
                event_type = event["type"]
                if event_type in {"error", "response.failed", "response.cancelled"} or event.get("error") is not None or event.get("success") is False:
                    message, error_type = self._stream_error_details(event, event_type)
                    secret = getattr(self.channel, "key", None)
                    if secret:
                        message = message.replace(secret, "[redacted]")
                    payload = event.get("response") if isinstance(event.get("response"), dict) else event
                    if is_overload_error_signal(error_type, message):
                        error = UpstreamOverloaded(message, error_type=error_type)
                        try:
                            usage = extract_responses_usage(payload)
                        except UpstreamProtocolError:
                            usage = None
                        if usage is not None:
                            error.provider_usage = usage
                        raise error
                    error = _responses_protocol_error(message, payload)
                    error.error_type = error_type or "api_error"
                    raise error
                if pending_terminal is not None:
                    if event_type in _POST_TERMINAL_LIFECYCLE_EVENTS:
                        continue
                    raise UpstreamProtocolError("Responses stream contains events after its terminal event")
                if event_type in {"response.completed", "response.incomplete"}:
                    native = validate_responses_response(event.get("response"))
                    if native["status"] != event_type.removeprefix("response."):
                        raise _responses_protocol_error("Responses terminal event and response status disagree", native)
                    pending_terminal = event
                    continue
                yield event
            if pending_terminal is None:
                raise UpstreamProtocolError("Responses stream ended without a terminal event")
        except Exception as exc:
            if isinstance(exc, (UpstreamProtocolError, UpstreamOverloaded)):
                exc.upstream_status = response.status_code
            if pending_terminal is not None:
                usage = extract_responses_usage(pending_terminal["response"])
                if usage is not None:
                    exc.provider_usage = usage
            raise
        # A terminal event cannot escape before late parse/transport failures.
        yield pending_terminal

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        tool_indexes: dict[int, int] = {}
        tool_has_arguments: set[int] = set()
        saw_tool_call = False
        chunk_id = f"chatcmpl_{uuid.uuid4().hex[:24]}"

        async for event in self._validated_stream_events(response):
            event_type = event["type"]
            if request.responses_payload is not None:
                yield event
                continue
            if request.anthropic_payload is not None:
                if (
                    event_type == "response.incomplete"
                    or str(event_type).startswith(("response.reasoning", "response.refusal"))
                    or event.get("success") is False
                ):
                    raise UpstreamProtocolError("Responses stream event cannot be represented faithfully as Anthropic content")
                if event_type == "response.completed":
                    _validate_anthropic_response_conversion(event.get("response") or {})
                elif event_type in {"response.output_item.added", "response.output_item.done"}:
                    _validate_anthropic_response_conversion({"output": [event.get("item")]}, final=False)
                elif event_type in {"response.content_part.added", "response.content_part.done"}:
                    _validate_anthropic_response_conversion({"output": [{
                        "type": "message", "content": [event.get("part")],
                    }]}, final=False)
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
            elif event_type in {"response.completed", "response.incomplete"}:
                native = event["response"]
                try:
                    finish_reason = _responses_chat_finish_reason(native, saw_tool_call)
                except UpstreamProtocolError as exc:
                    exc.upstream_status = response.status_code
                    raise
                yield _chat_stream_chunk(
                    chunk_id,
                    request.model,
                    delta={},
                    finish_reason=finish_reason,
                    usage=_responses_chat_usage(native),
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
