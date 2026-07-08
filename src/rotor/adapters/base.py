from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional
from httpx import AsyncClient, Response
from rotor.models.channel import Channel
from rotor.schemas.request import ChatCompletionRequest


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

        from rotor.config import settings
        timeout = timeout or settings.REQUEST_TIMEOUT

        response = await self.http_client.post(
            url,
            headers=headers,
            json=body,
            timeout=timeout
        )
        response.raise_for_status()
        return response

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

    async def increment_usage_stats(
        self,
        db_session: Any,
        success: bool = True
    ) -> None:
        """
        Increment channel usage statistics.

        Args:
            db_session: Database session
            success: Whether the request was successful
        """
        self.channel.total_requests += 1
        if success:
            self.channel.success_requests += 1
        else:
            self.channel.failed_requests += 1

        # Note: The caller should commit the session


class OpenAICompatibleAdapter(BaseAdapter):
    """Base adapter for OpenAI-compatible providers."""

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for OpenAI-compatible endpoints."""
        return f"{self.channel.base_url.rstrip('/')}/chat/completions"

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for OpenAI-compatible requests."""
        return {
            "Authorization": f"Bearer {self.channel.key}",
            "Content-Type": "application/json",
        }

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
        return f"{self.channel.base_url.rstrip('/')}/messages"

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Anthropic-compatible requests."""
        headers = {
            "x-api-key": self.channel.key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
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
