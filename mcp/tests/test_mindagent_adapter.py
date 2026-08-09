import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from mcp import Client

from mindagent.core import AgentContext
from mindagent.tools import MCPConnectionError, MCPToolSet, ToolContext
import rotor_mcp.server as server_module


class FakeControlClient:
    async def __aenter__(self) -> "FakeControlClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_request_trace(
        self,
        request_id: str,
        *,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "request_id": "control-1",
            "data": {
                "request_id": request_id,
                "attempts": [
                    {
                        "id": "attempt-1",
                        "attempt_index": 0,
                        "channel_id": 1,
                        "outcome": "failed",
                        "error": {
                            "category": "upstream_availability",
                            "code": "upstream_503",
                        },
                        "started_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                ],
            },
            "meta": {},
        }


def test_mindagent_adapter_discovers_and_calls_rotor_tool(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        monkeypatch.setattr(
            server_module,
            "create_control_client",
            FakeControlClient,
        )
        tool_set = MCPToolSet(
            Client(server_module.server),
            allowed_tools={"rotor_get_request_trace"},
        )
        tools = await tool_set.open()
        try:
            result = await tools[0].execute(
                {"request_id": "req-123"},
                ToolContext(
                    AgentContext(
                        user_input="diagnose",
                        run_id="run-123",
                    )
                ),
            )
        finally:
            await tool_set.close()

        assert tools[0].definition.name == "rotor_get_request_trace"
        assert result["data"]["request_id"] == "req-123"
        assert result["data"]["attempts"][0]["error"]["category"] == (
            "upstream_availability"
        )

    asyncio.run(exercise())


def test_mindagent_adapter_fails_closed_when_allowed_tool_is_missing() -> None:
    async def exercise() -> None:
        tool_set = MCPToolSet(
            Client(server_module.server),
            allowed_tools={"rotor_missing_tool"},
        )
        try:
            await tool_set.open()
        except MCPConnectionError as exc:
            assert "rotor_missing_tool" in str(exc)
        else:
            raise AssertionError("missing allowlisted tool must fail")

    asyncio.run(exercise())


def test_mindagent_adapter_connects_to_rotor_mcp_over_stdio() -> None:
    async def exercise() -> None:
        command = Path(sys.executable).with_name("rotor-mcp")
        tool_set = MCPToolSet.stdio(
            command=str(command),
            env={"ROTOR_CONTROL_API_TOKEN": "test-control-token"},
            allowed_tools={"rotor_get_request_trace"},
        )
        tools = await tool_set.open()
        await tool_set.close()

        assert [tool.definition.name for tool in tools] == [
            "rotor_get_request_trace"
        ]

    asyncio.run(exercise())
