from __future__ import annotations

import os
from dataclasses import dataclass, field
from os import PathLike
from typing import Any

from dotenv import dotenv_values

from mindagent.providers.param import BaseProviderParam


@dataclass
class OpenAIProviderParam(BaseProviderParam):
    """Parameters specific to the OpenAI (and compatible) provider."""

    model: str = "gpt-4o-mini"
    api_key: str | None = None
    base_url: str | None = None
    timeout: float = 120.0
    temperature: float = 0.7
    max_tokens: int | None = None
    vision_enabled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    context_window: int | None = None  # override model context window if set
    embedding_model: str = "text-embedding-3-small"

    @classmethod
    def from_env(
        cls,
        prefix: str = "MINDAGENT",
        env_file: str | PathLike[str] | None = ".env",
    ) -> "OpenAIProviderParam":
        values: dict[str, str | None] = {}
        if env_file is not None:
            values.update(dotenv_values(env_file))
        values.update(os.environ)

        api_key = values.get(f"{prefix}_API_KEY")
        base_url = values.get(f"{prefix}_BASE_URL")
        models = values.get(f"{prefix}_MODELS", "") or ""
        model = next(
            (item.strip() for item in models.split(",") if item.strip()),
            "",
        )

        missing = [
            name
            for name, value in {
                f"{prefix}_API_KEY": api_key,
                f"{prefix}_BASE_URL": base_url,
                f"{prefix}_MODELS": model,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(
                "缺少 Provider 环境变量: " + ", ".join(missing)
            )

        return cls(
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
