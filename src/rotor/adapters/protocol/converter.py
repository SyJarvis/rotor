from typing import Any, List, Union, Dict, Optional
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


class ProtocolConverter:
    """Converter between OpenAI and Anthropic protocols."""

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
                system_prompt = msg.content
            elif msg.role in (Role.USER, Role.ASSISTANT):
                content = msg.content or ""
                # Anthropic protocol requires content to be an array format
                # Format: [{"type": "text", "text": "..."}]
                anthropic_msg: Dict[str, Any] = {
                    "role": msg.role.value,
                    "content": [{"type": "text", "text": content}]
                }

                # Handle tool calls in assistant messages
                if msg.tool_calls:
                    content_blocks = []
                    if content:
                        content_blocks.append({"type": "text", "text": content})

                    for tool_call in msg.tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": tool_call.function.name,
                            "input": tool_call.function.arguments
                        })

                    anthropic_msg["content"] = content_blocks

                # Handle tool result messages
                if msg.role == Role.USER and msg.tool_call_id:
                    anthropic_msg["content"] = [{
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": content
                    }]

                anthropic_messages.append(anthropic_msg)

        return anthropic_messages, system_prompt

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
                anthropic_request["tool_choice"] = request.tool_choice

        return anthropic_request

    @staticmethod
    def anthropic_to_openai_usage(usage: AnthropicUsage) -> Usage:
        """Convert Anthropic usage to OpenAI format."""
        return Usage(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens
        )

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
            return {
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
            block_type = stream_event.get("content_block_type", "")

            if block_type == "text" and delta.get("type") == "text_delta":
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
            if block_type == "text" and delta.get("type") == "thinking_delta":
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

            if block_type == "tool_use" and delta.get("type") == "input_json_delta":
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

        # Handle message_stop (final chunk with finish_reason)
        if event_type == "message_stop":
            stop_reason_map = {
                "end_turn": "stop",
                "max_tokens": "length",
                "stop_sequence": "stop",
                "tool_use": "tool_calls",
            }
            message = stream_event.get("message", {})
            finish_reason = stop_reason_map.get(message.get("stop_reason", "stop"), "stop")

            return {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": message.get("created_at", 0),
                "model": request_model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }]
            }

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
