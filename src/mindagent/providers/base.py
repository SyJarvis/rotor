from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderCapabilities:
    text: bool = True
    vision: bool = False
    tool_calling: bool = False
    streaming: bool = False
    embedding: bool = False
    json_mode: bool = False
    max_context_tokens: int = 8192

    def supports(self, request: ModelCapabilityRequest) -> bool:
        return (
            (not request.text or self.text)
            and (not request.vision or self.vision)
            and (not request.tool_calling or self.tool_calling)
            and (not request.streaming or self.streaming)
            and (not request.embedding or self.embedding)
            and (not request.json_mode or self.json_mode)
            and self.max_context_tokens >= request.min_context_tokens
        )


@dataclass(frozen=True)
class ModelCapabilityRequest:
    text: bool = True
    vision: bool = False
    tool_calling: bool = False
    streaming: bool = False
    embedding: bool = False
    json_mode: bool = False
    min_context_tokens: int = 0

    def __post_init__(self) -> None:
        if self.min_context_tokens < 0:
            raise ValueError("min_context_tokens 不能小于 0")

    def describe(self) -> str:
        names = [
            name
            for name in (
                "text",
                "vision",
                "tool_calling",
                "streaming",
                "embedding",
                "json_mode",
            )
            if getattr(self, name)
        ]
        if self.min_context_tokens:
            names.append(f"context>={self.min_context_tokens}")
        return ", ".join(names) or "none"


@dataclass
class ProviderToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""


@dataclass
class ProviderStreamChunk:
    content_delta: str = ""
    reasoning_delta: str = ""
    tool_call_deltas: list[ProviderToolCallDelta] = field(default_factory=list)
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


@dataclass
class ProviderResponse:
    content: str | None = None
    reasoning_summary: str = ""
    tool_calls: list[ProviderToolCall] = field(default_factory=list)
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class BaseProvider(ABC):
    name = "base"
    capabilities = ProviderCapabilities()

    @abstractmethod
    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> ProviderResponse:
        raise NotImplementedError

    async def stream_chat(
        self,
        messages: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[ProviderStreamChunk]:
        raise NotImplementedError(f"{self.name} 不支持流式输出")
        yield

    async def respond(
        self,
        input: Any,
        **kwargs: Any,
    ) -> ProviderResponse:
        raise NotImplementedError(f"{self.name} 不支持 Responses API")

    async def analyze_image(
        self,
        *,
        prompt: str,
        image_url: str,
        detail: str | None = None,
        api: str = "auto",
        **kwargs: Any,
    ) -> ProviderResponse:
        raise NotImplementedError(f"{self.name} 不支持图像理解")

    async def embed(
        self,
        texts: Sequence[str],
        **kwargs: Any,
    ) -> list[list[float]]:
        raise NotImplementedError(f"{self.name} 不支持 embedding")
