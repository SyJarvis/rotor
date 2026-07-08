from pydantic import BaseModel, Field
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
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None


class Function(BaseModel):
    """Function definition."""
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None


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


class Usage(BaseModel):
    """Token usage."""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


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
    name: str
    description: str
    input_schema: Dict[str, Any]


class AnthropicMessageRequest(BaseModel):
    """Anthropic message request."""
    model: str
    messages: List[AnthropicMessage]
    max_tokens: int
    system: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    tools: Optional[List[AnthropicToolDefinition]] = None
    tool_choice: Optional[Dict[str, Any]] = None


class AnthropicUsage(BaseModel):
    """Anthropic usage."""
    input_tokens: int
    output_tokens: int


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
