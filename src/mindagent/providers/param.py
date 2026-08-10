from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BaseProviderParam:
    """Common parameters shared by all LLM providers."""

    model: str = ""
    temperature: float = 0.7
    max_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)