from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from mindagent.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderResponse,
    ProviderStreamChunk,
    ProviderToolCall,
    ProviderToolCallDelta,
)
from mindagent.providers.openai.param import OpenAIProviderParam


class OpenAIProvider(BaseProvider):
    name = "openai"
    capabilities = ProviderCapabilities(
        text=True,
        vision=True,
        tool_calling=True,
        streaming=True,
        embedding=True,
        json_mode=True,
        max_context_tokens=128000,
    )

    def __init__(
        self,
        param: OpenAIProviderParam,
        *,
        client: Any | None = None,
    ):
        self.param = param
        self._closed = False
        if client is not None:
            self.client = client
            return

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "使用 OpenAIProvider 需要安装 openai 包"
            ) from exc

        self.client = AsyncOpenAI(
            api_key=param.api_key,
            base_url=param.base_url,
            timeout=param.timeout,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.client, "close", None)
        if close is None:
            close = getattr(self.client, "aclose", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> ProviderResponse:
        request = self._chat_request(messages, kwargs)
        completion = await self.client.chat.completions.create(**request)
        choice = completion.choices[0]
        message = choice.message
        tool_calls = [
            ProviderToolCall(
                id=call.id,
                name=call.function.name,
                arguments=self._parse_arguments(call.function.arguments),
            )
            for call in (message.tool_calls or ())
        ]
        usage = {}
        if completion.usage is not None:
            usage = {
                "input_tokens": completion.usage.prompt_tokens,
                "output_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
            }
            details = getattr(
                completion.usage,
                "prompt_tokens_details",
                None,
            )
            cached_tokens = getattr(details, "cached_tokens", None)
            if cached_tokens is not None:
                usage["cached_input_tokens"] = cached_tokens
        return ProviderResponse(
            content=message.content,
            reasoning_summary=getattr(message, "reasoning_content", "") or "",
            tool_calls=tool_calls,
            model=completion.model,
            finish_reason=choice.finish_reason,
            usage=usage,
            raw=completion,
        )

    async def stream_chat(
        self,
        messages: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[ProviderStreamChunk]:
        request = self._chat_request(messages, kwargs)
        request["stream"] = True
        request["stream_options"] = {"include_usage": True}
        stream = await self.client.chat.completions.create(**request)

        async for chunk in stream:
            usage = self._normalize_usage(getattr(chunk, "usage", None))
            if not chunk.choices:
                yield ProviderStreamChunk(
                    model=getattr(chunk, "model", None),
                    usage=usage,
                    raw=chunk,
                )
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            tool_call_deltas = []
            for call in getattr(delta, "tool_calls", None) or ():
                function = getattr(call, "function", None)
                tool_call_deltas.append(
                    ProviderToolCallDelta(
                        index=call.index,
                        id=getattr(call, "id", None),
                        name=getattr(function, "name", None),
                        arguments_delta=(
                            getattr(function, "arguments", "") or ""
                        ),
                    )
                )
            yield ProviderStreamChunk(
                content_delta=getattr(delta, "content", "") or "",
                reasoning_delta=(
                    getattr(delta, "reasoning_content", "") or ""
                ),
                tool_call_deltas=tool_call_deltas,
                model=getattr(chunk, "model", None),
                finish_reason=choice.finish_reason,
                usage=usage,
                raw=chunk,
            )

    async def embed(
        self,
        texts: Sequence[str],
        **kwargs: Any,
    ) -> list[list[float]]:
        model = kwargs.pop("model", self.param.embedding_model)
        response = await self.client.embeddings.create(
            model=model,
            input=list(texts),
            **kwargs,
        )
        return [item.embedding for item in response.data]

    async def respond(
        self,
        input: Any,
        **kwargs: Any,
    ) -> ProviderResponse:
        request: dict[str, Any] = {
            "model": kwargs.pop("model", self.param.model),
            "input": input,
            **self.param.extra,
            **kwargs,
        }
        if self.param.max_tokens is not None:
            request.setdefault("max_output_tokens", self.param.max_tokens)

        response = await self.client.responses.create(**request)
        usage = {}
        if response.usage is not None:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            details = getattr(
                response.usage,
                "input_tokens_details",
                None,
            )
            cached_tokens = getattr(details, "cached_tokens", None)
            if cached_tokens is not None:
                usage["cached_input_tokens"] = cached_tokens
        return ProviderResponse(
            content=response.output_text,
            model=response.model,
            usage=usage,
            raw=response,
        )

    async def analyze_image(
        self,
        *,
        prompt: str,
        image_url: str,
        detail: str | None = None,
        api: str = "auto",
        **kwargs: Any,
    ) -> ProviderResponse:
        if api not in {"auto", "chat_completions", "responses"}:
            raise ValueError(f"不支持的多模态 API: {api}")
        if kwargs.get("model") is None:
            kwargs.pop("model", None)

        if api in {"auto", "chat_completions"}:
            try:
                return await self.chat(
                    self._image_chat_messages(
                        prompt,
                        image_url,
                        detail,
                    ),
                    **kwargs,
                )
            except Exception as exc:
                if api != "auto" or not self._can_fallback_to_responses(exc):
                    raise

        return await self.respond(
            self._image_response_input(prompt, image_url, detail),
            **kwargs,
        )

    @staticmethod
    def _image_chat_messages(
        prompt: str,
        image_url: str,
        detail: str | None,
    ) -> list[dict[str, Any]]:
        image: dict[str, Any] = {"url": image_url}
        if detail is not None:
            image["detail"] = detail
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": image},
                ],
            }
        ]

    @staticmethod
    def _image_response_input(
        prompt: str,
        image_url: str,
        detail: str | None,
    ) -> list[dict[str, Any]]:
        image: dict[str, Any] = {
            "type": "input_image",
            "image_url": image_url,
        }
        if detail is not None:
            image["detail"] = detail
        return [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    image,
                ],
            }
        ]

    @staticmethod
    def _can_fallback_to_responses(exc: Exception) -> bool:
        return getattr(exc, "status_code", None) in {400, 404, 405, 422}

    @staticmethod
    def _parse_arguments(arguments: str) -> dict[str, Any]:
        try:
            value = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Provider 返回了无效的 tool arguments JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("tool arguments 必须是 JSON object")
        return value

    def _chat_request(
        self,
        messages: Sequence[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": kwargs.pop("model", self.param.model),
            "messages": self._normalize_messages(messages),
            "temperature": kwargs.pop(
                "temperature",
                self.param.temperature,
            ),
            **self.param.extra,
            **kwargs,
        }
        if self.param.max_tokens is not None:
            request.setdefault("max_tokens", self.param.max_tokens)
        if request.get("tools") is None:
            request.pop("tools", None)
        if request.get("parallel_tool_calls") is None:
            request.pop("parallel_tool_calls", None)
        return request

    @staticmethod
    def _normalize_usage(usage: Any) -> dict[str, int]:
        if usage is None:
            return {}
        result = {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = getattr(details, "cached_tokens", None)
        if cached_tokens is not None:
            result["cached_input_tokens"] = cached_tokens
        return result

    @staticmethod
    def _normalize_messages(
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized = []
        for message in messages:
            item = copy.deepcopy(message)
            for tool_call in item.get("tool_calls", ()):
                arguments = tool_call.get("function", {}).get("arguments")
                if isinstance(arguments, dict):
                    tool_call["function"]["arguments"] = json.dumps(
                        arguments,
                        ensure_ascii=False,
                    )
            normalized.append(item)
        return normalized
