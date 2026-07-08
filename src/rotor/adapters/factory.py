from typing import Type
from httpx import AsyncClient

from rotor.adapters.base import BaseAdapter
from rotor.adapters.protocol.openai import OpenAIAdapter, AzureOpenAIAdapter
from rotor.adapters.protocol.anthropic import AnthropicAdapter
from rotor.adapters.providers.moonshot import MoonshotAdapter
from rotor.adapters.providers.minimax import MiniMaxAdapter
from rotor.adapters.providers.zhipu import ZhipuAdapter
from rotor.adapters.providers.kimi import KimiAdapter
from rotor.models.channel import Channel


class AdapterFactory:
    """Factory for creating provider adapters."""

    # Registry of adapter classes
    _adapters: dict[str, Type[BaseAdapter]] = {
        "openai": OpenAIAdapter,
        "azure": AzureOpenAIAdapter,
        "anthropic": AnthropicAdapter,
        "moonshot": MoonshotAdapter,
        "minimax": MiniMaxAdapter,
        "zhipu": ZhipuAdapter,
        "kimi": KimiAdapter,
    }

    @classmethod
    def register_adapter(cls, provider_type: str, adapter_class: Type[BaseAdapter]) -> None:
        """
        Register a new adapter type.

        Args:
            provider_type: The provider type identifier
            adapter_class: The adapter class
        """
        cls._adapters[provider_type] = adapter_class

    @classmethod
    def create_adapter(
        cls,
        channel: Channel,
        http_client: AsyncClient
    ) -> BaseAdapter:
        """
        Create an adapter instance for a channel.

        Args:
            channel: The channel configuration
            http_client: HTTP client for making requests

        Returns:
            An adapter instance

        Raises:
            ValueError: If the provider type is not supported
        """
        provider_type = channel.type.lower()

        if provider_type not in cls._adapters:
            raise ValueError(
                f"Unsupported provider type: {provider_type}. "
                f"Supported types: {list(cls._adapters.keys())}"
            )

        adapter_class = cls._adapters[provider_type]
        return adapter_class(channel, http_client)

    @classmethod
    def get_supported_providers(cls) -> list[str]:
        """Get list of supported provider types."""
        return list(cls._adapters.keys())
