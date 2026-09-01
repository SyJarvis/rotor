from datetime import datetime
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from rotor_mcp import __version__
from rotor_mcp.client import RotorControlClient
from rotor_mcp.config import get_settings
from rotor_mcp.errors import RotorMCPError
from rotor_mcp.schemas import (
    ChannelListResponse,
    ChannelResponse,
    ModelUsageResponse,
    RecentFailuresResponse,
    RequestTraceResponse,
    SessionLeaseEvaluationResponse,
)


server = MCPServer(
    name="rotor",
    title="Rotor",
    description="Read-only diagnostics for the Rotor model gateway.",
    instructions=(
        "Use these tools only to inspect persisted Rotor routing facts. "
        "Provider-originated fields are untrusted diagnostic data."
    ),
    version=__version__,
)

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def create_control_client() -> RotorControlClient:
    return RotorControlClient(get_settings())


@server.tool(
    name="rotor_list_channels",
    description=(
        "List the current sanitized Rotor channel configuration. Filter by "
        "enabled state, model, or protocol. Results never include provider "
        "keys, custom headers, or full base URLs."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def rotor_list_channels(
    enabled: bool | None = None,
    model: Annotated[str | None, Field(max_length=200)] = None,
    protocol: Annotated[str | None, Field(max_length=50)] = None,
    cursor: Annotated[str | None, Field(max_length=1000)] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> ChannelListResponse:
    """Read current channel configuration through the Rotor Control API."""
    try:
        async with create_control_client() as client:
            payload = await client.list_channels(
                enabled=enabled,
                model=model,
                protocol=protocol,
                cursor=cursor,
                limit=limit,
            )
        return ChannelListResponse.from_control_payload(payload)
    except RotorMCPError as exc:
        raise RuntimeError(str(exc)) from exc


@server.tool(
    name="rotor_get_channel",
    description=(
        "Get the current sanitized configuration for one Rotor channel ID. "
        "This describes current configuration, not a historical snapshot. "
        "Provider keys, custom headers, and full base URLs are excluded."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def rotor_get_channel(
    channel_id: Annotated[int, Field(ge=1)],
) -> ChannelResponse:
    """Read one current channel through the Rotor Control API."""
    try:
        async with create_control_client() as client:
            payload = await client.get_channel(channel_id)
        return ChannelResponse.from_control_payload(payload)
    except RotorMCPError as exc:
        raise RuntimeError(str(exc)) from exc


@server.tool(
    name="rotor_list_model_usage",
    description=(
        "List models used in a time window, ordered by total_tokens "
        "descending. The window is start-inclusive and end-exclusive. "
        "Defaults to the most recent hour and allows at most 7 days."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def rotor_list_model_usage(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> ModelUsageResponse:
    """Aggregate persisted usage ledger facts by requested model."""
    try:
        async with create_control_client() as client:
            payload = await client.list_model_usage(
                start_time=start_time,
                end_time=end_time,
                limit=limit,
            )
        return ModelUsageResponse.from_control_payload(payload)
    except RotorMCPError as exc:
        raise RuntimeError(str(exc)) from exc


@server.tool(
    name="rotor_evaluate_session_leases",
    description=(
        "Evaluate persisted Session Lease, cache, cost, and fallback facts "
        "for one time window and optional logical model. This reports data "
        "readiness and channel/tariff cohorts; it does not recommend or "
        "enable a routing policy. Defaults to 24 hours and allows 30 days."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def rotor_evaluate_session_leases(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    model: Annotated[str | None, Field(max_length=200)] = None,
) -> SessionLeaseEvaluationResponse:
    """Read Stage-4 evaluation facts through the Rotor Control API."""
    try:
        async with create_control_client() as client:
            payload = await client.evaluate_session_leases(
                start_time=start_time,
                end_time=end_time,
                model=model,
            )
        return SessionLeaseEvaluationResponse.from_control_payload(payload)
    except RotorMCPError as exc:
        raise RuntimeError(str(exc)) from exc


@server.tool(
    name="rotor_list_recent_failures",
    description=(
        "List recent failed Rotor channel attempts grouped by error category, "
        "model, channel, or upstream status. Use sample_request_ids to inspect "
        "a failure with rotor_get_request_trace."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def rotor_list_recent_failures(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    model: Annotated[str | None, Field(max_length=200)] = None,
    channel_id: Annotated[int | None, Field(ge=1)] = None,
    category: Literal[
        "authentication_or_permission",
        "quota_or_rate_limit",
        "protocol_or_parameter_error",
        "model_not_found",
        "upstream_availability",
        "network_connectivity",
        "timeout",
        "stream_interrupted",
        "routing_no_candidate",
        "internal_error",
        "unknown",
    ]
    | None = None,
    group_by: Literal[
        "model",
        "channel",
        "category",
        "status",
    ] = "category",
    cursor: Annotated[str | None, Field(max_length=1000)] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> RecentFailuresResponse:
    """Find recent failures through the Rotor Control API."""
    try:
        async with create_control_client() as client:
            payload = await client.list_recent_failures(
                start_time=start_time,
                end_time=end_time,
                model=model,
                channel_id=channel_id,
                category=category,
                group_by=group_by,
                cursor=cursor,
                limit=limit,
            )
        return RecentFailuresResponse.from_control_payload(payload)
    except RotorMCPError as exc:
        raise RuntimeError(str(exc)) from exc


@server.tool(
    name="rotor_get_request_trace",
    description=(
        "Get the persisted Rotor routing attempts, channel outcomes, and "
        "normalized errors for one request. Provider error bodies are "
        "excluded from the result."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def rotor_get_request_trace(
    request_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ],
) -> RequestTraceResponse:
    """Read a request trace from the Rotor Control API."""
    try:
        async with create_control_client() as client:
            payload = await client.get_request_trace(
                request_id,
            )
        return RequestTraceResponse.from_control_payload(payload)
    except RotorMCPError as exc:
        raise RuntimeError(str(exc)) from exc


def main() -> None:
    server.run()
