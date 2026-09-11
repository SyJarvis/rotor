"""Request semantics that must survive channel selection."""

from rotor.schemas.request import AnthropicMessageRequest, ChatCompletionRequest, Role


def chat_required_capabilities(request: ChatCompletionRequest) -> set[str]:
    required = {"stream"} if request.stream else set()
    if request.tools or request.tool_choice not in (None, "none", "auto"):
        required.add("function_call")
    if any(
        getattr(request, field, None) is not None
        for field in ("response_format", "parallel_tool_calls", "max_completion_tokens")
    ):
        required.add("openai_chat_native")
    if request.reasoning_effort is not None:
        required.add("reasoning_effort")
    for message in request.messages:
        if message.tool_calls or message.tool_call_id or message.role == Role.TOOL:
            required.add("function_call")
        if getattr(message, "reasoning_content", None) is not None:
            required.add("openai_chat_native")
        if isinstance(message.content, list):
            for block in message.content:
                if block.get("type") == "image_url":
                    required.add("vision")
    return required


def anthropic_required_capabilities(request: AnthropicMessageRequest) -> set[str]:
    required = {"stream"} if request.stream else set()
    if request.stop_sequences:
        required.add("stop_sequences")
    if request.tools or (request.tool_choice or {}).get("type") in {"any", "tool"}:
        required.add("function_call")
    if any(
        getattr(request, field, None) is not None
        for field in ("thinking", "top_k", "context_management", "output_config")
    ):
        required.add("anthropic_native")
    if (request.tool_choice or {}).get("disable_parallel_tool_use") is not None:
        required.add("anthropic_native")

    def inspect_blocks(content, allowed: set[str]) -> None:
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                required.add("anthropic_native")
                continue
            kind = block.get("type")
            if kind not in allowed:
                required.add("anthropic_native")
            if kind == "image":
                required.add("vision")
                source = block.get("source") or {}
                if not isinstance(source, dict) or source.get("type") not in {"url", "base64"}:
                    required.add("anthropic_native")
            elif kind in {"tool_use", "tool_result"}:
                required.add("function_call")
                if kind == "tool_result":
                    if block.get("is_error"):
                        required.add("anthropic_native")
                    # The cross-protocol converter only preserves text results.
                    inspect_blocks(block.get("content"), {"text"})

    inspect_blocks(request.system, {"text"})
    for message in request.messages:
        allowed = {"text", "image", "tool_result"} if message.role == "user" else {"text", "tool_use"}
        inspect_blocks(message.content, allowed)
    return required
