from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional, List, Dict, Any, Union, Literal
from enum import Enum


class Role(str, Enum):
    """Message roles."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FunctionCall(BaseModel):
    """Function call."""
    name: str
    arguments: str


class ToolCall(BaseModel):
    """Tool call."""
    id: str
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    """Chat message."""
    role: Role
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None


class Function(BaseModel):
    """Function definition."""
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    strict: Optional[bool] = None


class Tool(BaseModel):
    """Tool definition."""
    type: str = "function"
    function: Function


class ChatCompletionRequest(BaseModel):
    """OpenAI Chat Completion request."""
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = Field(1.0, ge=0, le=2)
    top_p: Optional[float] = Field(1.0, ge=0, le=1)
    n: Optional[int] = Field(1, ge=1)
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = Field(0, ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(0, ge=-2, le=2)
    stop: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[Literal["none", "auto", "required"], Dict]] = None
    user: Optional[str] = None
    # Preserve protocol-native fields when an Anthropic request is routed to
    # an Anthropic upstream. Excluded from generic provider serialization.
    anthropic_payload: Optional[Dict[str, Any]] = Field(default=None, exclude=True)
    anthropic_headers: Optional[Dict[str, str]] = Field(default=None, exclude=True)
    # Preserve the original Responses request for a native /responses upstream.
    # Cross-protocol adapters consume the normalized messages/tools instead.
    responses_payload: Optional[Dict[str, Any]] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def move_system_messages_to_prefix(self) -> "ChatCompletionRequest":
        """Keep system instructions in a stable leading prefix for providers and caching."""
        system_messages = [
            message for message in self.messages if message.role == Role.SYSTEM
        ]
        if system_messages:
            self.messages = system_messages + [
                message for message in self.messages if message.role != Role.SYSTEM
            ]
        return self


class Usage(BaseModel):
    """Token usage."""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: Optional[Dict[str, Any]] = None


class ChatMessageResponse(BaseModel):
    """Chat message response."""
    role: Role
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None


class ChatCompletionChoice(BaseModel):
    """Chat completion choice."""
    index: int
    message: ChatMessageResponse
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    """OpenAI Chat Completion response."""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Usage


class ChatCompletionChunkDelta(BaseModel):
    """Chat completion chunk delta."""
    role: Optional[Role] = None
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None


class ChatCompletionChunkChoice(BaseModel):
    """Chat completion chunk choice."""
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    """Chat completion chunk for streaming."""
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionChunkChoice]


class ModelInfo(BaseModel):
    """Model information."""
    id: str
    object: str = "model"
    created: int
    owned_by: str


class ModelsResponse(BaseModel):
    """Models list response."""
    object: str = "list"
    data: List[ModelInfo]


class ErrorResponse(BaseModel):
    """Error response."""
    error: Dict[str, Any]


# Anthropic Protocol Models


class AnthropicMessage(BaseModel):
    """Anthropic message."""
    role: Literal["user", "assistant"]
    content: Union[str, List[Dict[str, Any]]]


class AnthropicToolUseBlock(BaseModel):
    """Anthropic tool use block."""
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]


class AnthropicToolResultBlock(BaseModel):
    """Anthropic tool result block."""
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]]]
    is_error: Optional[bool] = False


class AnthropicTextBlock(BaseModel):
    """Anthropic text block."""
    type: Literal["text"]
    text: str


class AnthropicToolDefinition(BaseModel):
    """Anthropic tool definition."""
    model_config = ConfigDict(extra="allow")

    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any]


class AnthropicMessageRequest(BaseModel):
    """Anthropic message request."""
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[AnthropicMessage]
    max_tokens: int
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    tools: Optional[List[AnthropicToolDefinition]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_system_messages(cls, data: Any) -> Any:
        """Move non-standard messages[].role=system entries to top-level system."""
        if not isinstance(data, dict):
            return data

        messages = data.get("messages")
        if not isinstance(messages, list):
            return data

        system_messages = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "system"
        ]
        if not system_messages:
            return data

        normalized = dict(data)
        normalized["messages"] = [
            message
            for message in messages
            if not (isinstance(message, dict) and message.get("role") == "system")
        ]

        system_blocks: List[Dict[str, Any]] = []

        def append_system_content(content: Any) -> None:
            if isinstance(content, str):
                if content:
                    system_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                system_blocks.extend(
                    block for block in content if isinstance(block, dict)
                )

        append_system_content(data.get("system"))
        for message in system_messages:
            append_system_content(message.get("content"))

        normalized["system"] = system_blocks or None
        return normalized


class AnthropicCountTokensRequest(BaseModel):
    """Anthropic token-counting request (``max_tokens`` is not required)."""
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[AnthropicMessage]
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    tools: Optional[List[AnthropicToolDefinition]] = None

    def provider_payload(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)


class AnthropicUsage(BaseModel):
    """Anthropic usage."""
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AnthropicMessageResponse(BaseModel):
    """Anthropic message response."""
    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    content: List[Dict[str, Any]]
    model: str
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage
