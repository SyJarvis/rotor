from typing import Any, List, Union, Dict, Mapping, Optional
from rotor.schemas.request import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionChunk,
    ChatMessage,
    Role,
    ToolCall,
    Tool,
    Usage,
    ChatCompletionChoice,
    ChatMessageResponse,
    AnthropicMessageRequest,
    AnthropicMessage,
    AnthropicMessageResponse,
    AnthropicUsage,
    AnthropicTextBlock,
    AnthropicToolUseBlock,
    AnthropicToolResultBlock,
)


def _anthropic_usage_to_openai(usage: Mapping[str, Any]) -> Dict[str, Any]:
    uncached_tokens = int(usage.get("input_tokens") or 0)
    cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
    cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    cache_creation = usage.get("cache_creation") or {}
    cache_write_5m_tokens = int(
        cache_creation.get("ephemeral_5m_input_tokens") or 0
    )
    cache_write_1h_tokens = int(
        cache_creation.get("ephemeral_1h_input_tokens") or 0
    )
    cache_write_tokens = max(
        cache_write_tokens,
        cache_write_5m_tokens + cache_write_1h_tokens,
    )
    output_tokens = int(usage.get("output_tokens") or 0)
    prompt_tokens = uncached_tokens + cache_read_tokens + cache_write_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
        "prompt_tokens_details": {
            "cached_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_write_5m_tokens": cache_write_5m_tokens,
            "cache_write_1h_tokens": cache_write_1h_tokens,
            "uncached_tokens": uncached_tokens,
        },
    }


class ProtocolConverter:
    """Converter between OpenAI and Anthropic protocols."""

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    @staticmethod
    def _openai_content_to_anthropic(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        if not isinstance(content, list):
            return []

        blocks: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                blocks.append({"type": "text", "text": str(item.get("text", ""))})
                continue
            if item.get("type") != "image_url":
                continue
            image = item.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str) or not url:
                continue
            if url.startswith("data:image/") and ";base64," in url:
                header, data = url.split(",", 1)
                media_type = header[5:].split(";", 1)[0]
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                })
            else:
                blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        return blocks

    @staticmethod
    def openai_to_anthropic_messages(messages: List[ChatMessage]) -> tuple[List[Dict], Optional[str]]:
        """
        Convert OpenAI messages to Anthropic format.

        Returns:
            Tuple of (messages list, system prompt)
        """
        anthropic_messages = []
        system_prompt = None

        for msg in messages:
            if msg.role == Role.SYSTEM:
                system_prompt = "\n".join(
                    part
                    for part in (
                        system_prompt,
                        ProtocolConverter._content_text(msg.content),
                    )
                    if part
                )
            elif msg.role in (Role.USER, Role.ASSISTANT):
                content = msg.content or ""
                anthropic_msg: Dict[str, Any] = {
                    "role": msg.role.value,
                    "content": ProtocolConverter._openai_content_to_anthropic(content),
                }
                if not anthropic_msg["content"] and not msg.tool_calls:
                    anthropic_msg["content"] = [{"type": "text", "text": ""}]

                # Handle tool calls in assistant messages
                if msg.tool_calls:
                    content_blocks = []
                    content_blocks.extend(
                        ProtocolConverter._openai_content_to_anthropic(content)
                    )

                    for tool_call in msg.tool_calls:
                        import json
                        try:
                            tool_input = json.loads(tool_call.function.arguments)
                        except (json.JSONDecodeError, TypeError):
                            tool_input = {}
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": tool_call.function.name,
                            "input": tool_input,
                        })

                    anthropic_msg["content"] = content_blocks

                ProtocolConverter._append_anthropic_message(
                    anthropic_messages,
                    anthropic_msg,
                )
            elif msg.role == Role.TOOL:
                ProtocolConverter._append_anthropic_message(
                    anthropic_messages,
                    {
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id or "",
                            "content": ProtocolConverter._content_text(msg.content),
                        }],
                    },
                )

        return anthropic_messages, system_prompt

    @staticmethod
    def _append_anthropic_message(messages: List[Dict], message: Dict) -> None:
        """Merge adjacent equal roles as required by the Anthropic protocol."""
        if messages and messages[-1]["role"] == message["role"]:
            messages[-1]["content"].extend(message["content"])
        else:
            messages.append(message)

    @staticmethod
    def openai_tools_to_anthropic(tools: List[Tool]) -> List[Dict[str, Any]]:
        """Convert OpenAI tools to Anthropic format."""
        anthropic_tools = []

        for tool in tools:
            anthropic_tools.append({
                "name": tool.function.name,
                "description": tool.function.description or "",
                "input_schema": tool.function.parameters or {
                    "type": "object",
                    "properties": {},
                }
            })

        return anthropic_tools

    @staticmethod
    def openai_to_anthropic(request: ChatCompletionRequest) -> Dict[str, Any]:
        """
        Convert OpenAI chat completion request to Anthropic format.

        Args:
            request: OpenAI format request

        Returns:
            Anthropic format request dictionary
        """
        messages, system = ProtocolConverter.openai_to_anthropic_messages(request.messages)

        anthropic_request: Dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or 4096,
            "stream": request.stream or False,
        }

        if system:
            anthropic_request["system"] = system

        if request.temperature is not None:
            anthropic_request["temperature"] = request.temperature

        if request.top_p is not None:
            anthropic_request["top_p"] = request.top_p

        if request.stop:
            if isinstance(request.stop, str):
                anthropic_request["stop_sequences"] = [request.stop]
            else:
                anthropic_request["stop_sequences"] = request.stop

        if request.tools:
            anthropic_request["tools"] = ProtocolConverter.openai_tools_to_anthropic(request.tools)

        # Map tool_choice
        if request.tool_choice:
            if request.tool_choice == "auto":
                anthropic_request["tool_choice"] = {"type": "auto"}
            elif request.tool_choice == "required":
                anthropic_request["tool_choice"] = {"type": "any"}
            elif request.tool_choice == "none":
                # No tool_choice parameter needed for "none" in Anthropic
                pass
            elif isinstance(request.tool_choice, dict):
                function = request.tool_choice.get("function") or {}
                if (
                    request.tool_choice.get("type") == "function"
                    and function.get("name")
                ):
                    anthropic_request["tool_choice"] = {
                        "type": "tool",
                        "name": function["name"],
                    }
                else:
                    anthropic_request["tool_choice"] = request.tool_choice

        return anthropic_request

    @staticmethod
    def anthropic_to_openai_usage(usage: AnthropicUsage) -> Usage:
        """Convert Anthropic usage to OpenAI format."""
        return Usage(**_anthropic_usage_to_openai(usage.model_dump()))

    @staticmethod
    def anthropic_content_to_openai(content: List[Dict[str, Any]]) -> tuple[Optional[str], List[ToolCall]]:
        """
        Convert Anthropic content blocks to OpenAI format.

        Returns:
            Tuple of (text content, tool calls)
        """
        text_parts = []
        tool_calls = []

        for block in content:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                import json
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    type="function",
                    function={
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}))
                    }
                ))

        content_text = "\n".join(text_parts) if text_parts else None

        return content_text, tool_calls

    @staticmethod
    def anthropic_to_openai(
        anthropic_response: AnthropicMessageResponse,
        request_model: str
    ) -> Dict[str, Any]:
        """
        Convert Anthropic message response to OpenAI format.

        Args:
            anthropic_response: Anthropic format response
            request_model: The model name from the request

        Returns:
            OpenAI format response dictionary
        """
        import time
        import uuid

        content_text, tool_calls = ProtocolConverter.anthropic_content_to_openai(anthropic_response.content)

        # Map stop reason
        stop_reason_map = {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
        }
        finish_reason = stop_reason_map.get(anthropic_response.stop_reason, "stop")

        choice = {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content_text,
            },
            "finish_reason": finish_reason,
        }

        if tool_calls:
            choice["message"]["tool_calls"] = [tc.model_dump() for tc in tool_calls]

        usage = ProtocolConverter.anthropic_to_openai_usage(anthropic_response.usage)

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request_model,
            "choices": [choice],
            "usage": usage.model_dump()
        }

    @staticmethod
    def anthropic_stream_to_openai(
        stream_event: Dict[str, Any],
        request_model: str,
        chunk_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Convert Anthropic streaming event to OpenAI format.

        Args:
            stream_event: Anthropic streaming event
            request_model: The model name from the request
            chunk_id: The chunk ID to use

        Returns:
            OpenAI format chunk dictionary, or None for events that don't map
        """
        event_type = stream_event.get("type")

        # Handle message_start (send initial chunk)
        if event_type == "message_start":
            import time
            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": request_model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": None,
                }]
            }
            usage = (stream_event.get("message") or {}).get("usage") or {}
            if usage:
                chunk["usage"] = _anthropic_usage_to_openai(usage)
            return chunk

        # Handle content_block_start (tool_use start)
        if event_type == "content_block_start":
            block = stream_event.get("content_block", {})
            if block.get("type") == "tool_use":
                import json
                return {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": stream_event.get("message", {}).get("created_at", 0),
                    "model": request_model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": stream_event.get("index", 0),
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": ""
                                }
                            }]
                        },
                        "finish_reason": None,
                    }]
                }

        # Handle content_block_delta (text delta or tool arguments)
        if event_type == "content_block_delta":
            delta = stream_event.get("delta", {})
            delta_type = delta.get("type")

            if delta_type == "text_delta":
                return {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": stream_event.get("message", {}).get("created_at", 0),
                    "model": request_model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": delta.get("text", "")},
                        "finish_reason": None,
                    }]
                }

            # Handle thinking_delta (GLM-specific thinking process)
            if delta_type == "thinking_delta":
                thinking = delta.get("thinking", "")
                if thinking:
                    return {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": stream_event.get("message", {}).get("created_at", 0),
                        "model": request_model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": thinking},
                            "finish_reason": None,
                        }]
                    }

            if delta_type == "input_json_delta":
                return {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": stream_event.get("message", {}).get("created_at", 0),
                    "model": request_model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": stream_event.get("index", 0),
                                "function": {
                                    "arguments": delta.get("partial_json", "")
                                }
                            }]
                        },
                        "finish_reason": None,
                    }]
                }

        # Anthropic sends the stop reason and final usage in message_delta.
        if event_type == "message_delta":
            stop_reason_map = {
                "end_turn": "stop",
                "max_tokens": "length",
                "stop_sequence": "stop",
                "tool_use": "tool_calls",
            }
            delta = stream_event.get("delta", {})
            finish_reason = stop_reason_map.get(
                delta.get("stop_reason", "end_turn"),
                "stop",
            )
            usage = stream_event.get("usage") or {}

            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": request_model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }]
            }
            if usage:
                chunk["usage"] = _anthropic_usage_to_openai(usage)
            return chunk

        # Ignore other event types
        return None


class OpenAIProtocolConverter:
    """Helper class for OpenAI protocol conversions."""

    @staticmethod
    def create_streaming_chunk(
        chunk_id: str,
        model: str,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict]] = None,
        finish_reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create an OpenAI streaming chunk."""
        import time

        delta: Dict[str, Any] = {}
        if content:
            delta["content"] = content
        if tool_calls:
            delta["tool_calls"] = tool_calls
        if not delta and not finish_reason:
            delta = {"role": "assistant"}

        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }]
        }
