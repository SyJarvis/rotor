from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mindagent.core import ActionRisk

from ..base import BaseTool, ToolContext, ToolDefinition


class TimeNowTool(BaseTool):
    definition = ToolDefinition(
        name="time_now",
        description="Return the current time in an IANA timezone.",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone, for example Asia/Shanghai.",
                }
            },
            "additionalProperties": False,
        },
        risk=ActionRisk.READ_ONLY,
    )

    async def execute(
        self,
        arguments: dict,
        context: ToolContext,
    ) -> dict[str, object]:
        timezone_name = arguments.get("timezone", "UTC")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区: {timezone_name}") from exc

        now = datetime.now(timezone)
        return {
            "datetime": now.isoformat(),
            "timezone": timezone_name,
            "unix_timestamp": now.timestamp(),
        }
