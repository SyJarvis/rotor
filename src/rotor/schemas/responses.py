"""Pydantic models for the OpenAI Responses API surface.

The Responses API intentionally has an extensible item/tool model.  The
gateway validates the stable routing fields while preserving newer fields for
native Responses upstreams via ``extra="allow"``.
"""

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    model: str
    input: str | list[dict[str, Any]]
    instructions: str | list[dict[str, Any]] | None = None
    max_output_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("max_output_tokens", "max_tokens"),
    )
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    previous_response_id: str | None = None
    conversation: str | dict[str, Any] | None = None
    reasoning: dict[str, Any] | None = None
    text: dict[str, Any] | None = None
    store: bool | None = None
    truncation: str | None = None
    user: str | None = None
    include: list[str] | None = None
    service_tier: str | None = None
    background: bool | None = None
    max_tool_calls: int | None = None

    def provider_payload(self) -> dict[str, Any]:
        """Return the protocol-native body, including future extra fields."""
        return self.model_dump(exclude_none=True, by_alias=False)


class ResponsesUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_tokens_details: dict[str, Any] | None = None
    output_tokens_details: dict[str, Any] | None = None

