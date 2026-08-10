from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .base import BaseProvider, ModelCapabilityRequest


class ProviderNotFoundError(LookupError):
    pass


class ProviderRouter:
    def __init__(
        self,
        providers: Sequence[BaseProvider] | None = None,
        *,
        default: str | None = None,
    ):
        self._providers: dict[str, BaseProvider] = {}
        self.default = default
        self._closed = False
        for provider in providers or ():
            self.register(provider)

    def register(
        self,
        provider: BaseProvider,
        *,
        name: str | None = None,
        default: bool = False,
    ) -> None:
        provider_name = name or provider.name
        if provider_name in self._providers:
            raise ValueError(f"Provider 已注册: {provider_name}")
        self._providers[provider_name] = provider
        if default or self.default is None:
            self.default = provider_name

    def get(self, name: str) -> BaseProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ProviderNotFoundError(f"Provider 不存在: {name}") from exc

    def select(
        self,
        *,
        provider_name: str | None = None,
        capabilities: ModelCapabilityRequest | None = None,
        vision: bool = False,
        tool_calling: bool = False,
    ) -> BaseProvider:
        request = capabilities or ModelCapabilityRequest(
            vision=vision,
            tool_calling=tool_calling,
        )
        if provider_name:
            candidates = [self.get(provider_name)]
        else:
            candidates = []
            if self.default and self.default in self._providers:
                candidates.append(self._providers[self.default])
            candidates.extend(
                provider
                for name, provider in self._providers.items()
                if name != self.default
            )

        for provider in candidates:
            if provider.capabilities.supports(request):
                return provider

        raise ProviderNotFoundError(
            "没有满足能力要求的 Provider: "
            f"{request.describe()}"
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for provider in self._providers.values():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, "close", None)
            if close is None:
                close = getattr(provider, "aclose", None)
            if not callable(close):
                continue
            result = close()
            if asyncio.iscoroutine(result):
                await result
