from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional
from httpx import AsyncClient, Response, Timeout
from rotor.models.channel import Channel
from rotor.schemas.request import ChatCompletionRequest
from rotor.channels.presets import channel_option, join_api_url, provider_headers


class BaseAdapter(ABC):
    """Base adapter interface for all provider adapters."""

    def __init__(self, channel: Channel, http_client: AsyncClient):
        """
        Initialize the adapter.

        Args:
            channel: Channel configuration
            http_client: HTTP client for making requests
        """
        self.channel = channel
        self.http_client = http_client

    @abstractmethod
    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """
        Get the target URL for the request.

        Args:
            request: The chat completion request

        Returns:
            The target URL string
        """
        pass

    @abstractmethod
    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """
        Set up request headers for the provider.

        Args:
            request: The chat completion request

        Returns:
            Dictionary of headers
        """
        pass

    @abstractmethod
    async def convert_request(self, request: ChatCompletionRequest) -> dict[str, Any]:
        """
        Convert the request to the provider's format.

        Args:
            request: The chat completion request

        Returns:
            Converted request dictionary
        """
        pass

    @abstractmethod
    async def convert_response(self, response: Response, request: ChatCompletionRequest) -> dict[str, Any]:
        """
        Convert the provider response to OpenAI format.

        Args:
            response: The HTTP response from the provider
            request: The original request

        Returns:
            OpenAI-format response dictionary
        """
        pass

    @abstractmethod
    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Convert streaming response chunks to OpenAI format.

        Args:
            response: The HTTP streaming response
            request: The original request

        Yields:
            OpenAI-format response chunks
        """
        pass

    async def make_request(
        self,
        request: ChatCompletionRequest,
        timeout: Optional[float] = None
    ) -> Response:
        """
        Make the HTTP request to the provider.

        Args:
            request: The chat completion request
            timeout: Request timeout in seconds

        Returns:
            The HTTP response
        """
        url = await self.get_request_url(request)
        headers = self.setup_request_headers(request)
        body = await self.convert_request(request)
        body = self.prepare_request_body(request, body)

        from rotor.config import settings
        read_timeout = timeout or settings.REQUEST_TIMEOUT
        timeout = Timeout(
            connect=settings.CONNECT_TIMEOUT,
            read=read_timeout,
            write=settings.WRITE_TIMEOUT,
            pool=settings.POOL_TIMEOUT,
        )

        if request.stream:
            http_request = self.http_client.build_request(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=timeout,
            )
            response = await self.http_client.send(http_request, stream=True)
            try:
                response.raise_for_status()
            except Exception:
                # Preserve an upstream error body for diagnostics before the
                # streaming response is closed. Accessing response.json/text
                # later would otherwise raise httpx.ResponseNotRead.
                try:
                    await response.aread()
                except Exception:
                    pass
                await response.aclose()
                raise
            return response

        response = await self.http_client.post(
            url,
            headers=headers,
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        return response

    def prepare_request_body(
        self,
        request: ChatCompletionRequest,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply protocol-wide options after provider-specific conversion."""
        return body

    def map_model_name(self, model: str) -> str:
        """
        Map user-facing model name to provider model name.

        Args:
            model: User-facing model name

        Returns:
            Provider-specific model name
        """
        mapping = self.channel.model_mapping or {}
        return mapping.get(model, model)

    def build_request_url(self, default_path: str) -> str:
        path = channel_option(
            provider=self.channel.type,
            protocol=self.channel.protocol,
            extra=self.channel.extra,
            name="request_path",
        ) or default_path
        return join_api_url(self.channel.base_url, path)

    def build_request_headers(self) -> dict[str, str]:
        auth_type = channel_option(
            provider=self.channel.type,
            protocol=self.channel.protocol,
            extra=self.channel.extra,
            name="auth_type",
        )
        return provider_headers(
            key=self.channel.key,
            auth_type=auth_type,
            extra_headers=(self.channel.extra or {}).get("headers"),
        )

class OpenAICompatibleAdapter(BaseAdapter):
    """Base adapter for OpenAI-compatible providers."""

    def prepare_request_body(
        self,
        request: ChatCompletionRequest,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if request.stream:
            # Apply this after convert_request so provider adapters that
            # override conversion still request the final usage chunk.
            body.setdefault("stream_options", {"include_usage": True})
        return body

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for OpenAI-compatible endpoints."""
        return self.build_request_url("/chat/completions")

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for OpenAI-compatible requests."""
        return self.build_request_headers()

    async def convert_request(self, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert request to OpenAI format."""
        mapped_model = self.map_model_name(request.model)

        body = {
            "model": mapped_model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": request.stream,
        }
        # Add optional parameters
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.n is not None:
            body["n"] = request.n
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.presence_penalty is not None:
            body["presence_penalty"] = request.presence_penalty
        if request.frequency_penalty is not None:
            body["frequency_penalty"] = request.frequency_penalty
        if request.stop is not None:
            body["stop"] = request.stop
        if request.tools is not None:
            body["tools"] = [t.model_dump(exclude_none=True) for t in request.tools]
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.user is not None:
            body["user"] = request.user

        return body

    async def convert_response(self, response: Response, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert OpenAI-compatible response."""
        return response.json()

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """Convert streaming OpenAI-compatible response."""
        async for line in response.aiter_lines():
            if line.strip() and line.startswith("data: "):
                data = line[6:]  # Remove "data: " prefix
                if data == "[DONE]":
                    break
                import json
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


class AnthropicCompatibleAdapter(BaseAdapter):
    """Base adapter for Anthropic-compatible providers."""

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for Anthropic-compatible endpoints."""
        return self.build_request_url("/messages")

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Anthropic-compatible requests."""
        headers = self.build_request_headers()
        # Add accept header for streaming requests
        if request.stream:
            headers["accept"] = "text/event-stream"
        return headers

    @abstractmethod
    async def convert_request(self, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert OpenAI request to Anthropic format."""
        # This needs protocol conversion
        pass

    @abstractmethod
    async def convert_response(self, response: Response, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert Anthropic response to OpenAI format."""
        # This needs protocol conversion
        pass
