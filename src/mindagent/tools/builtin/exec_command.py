from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from mindagent.core import ActionRisk, AgentContext

from ..base import BaseTool, ToolContext, ToolDefinition
from ..workspace import WorkspacePathResolver, workspace_paths


class ExecCommandTool(BaseTool):
    definition = ToolDefinition(
        name="exec_command",
        description=(
            "Execute one command without a shell inside the workspace. "
            "The command must be an argv array."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "max_output_bytes": {"type": "integer"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        risk=ActionRisk.WRITE,
    )

    _read_only_commands = {
        "cat",
        "find",
        "git",
        "head",
        "ls",
        "pwd",
        "rg",
        "sed",
        "tail",
        "wc",
        "which",
    }
    _dangerous_commands = {
        "chmod",
        "chown",
        "dd",
        "kill",
        "mkfs",
        "mount",
        "rm",
        "shutdown",
        "sudo",
    }

    def __init__(self, workspace_root: str | Path):
        self.paths = WorkspacePathResolver(workspace_root)

    def assess_risk(
        self,
        arguments: dict[str, Any],
        context: AgentContext | None = None,
    ) -> ActionRisk:
        command = arguments.get("command") or []
        executable = Path(command[0]).name if command else ""
        if executable in self._dangerous_commands:
            return ActionRisk.DANGEROUS
        if executable in self._read_only_commands:
            if executable != "git" or self._is_read_only_git(command[1:]):
                return ActionRisk.READ_ONLY
        return ActionRisk.WRITE

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        command = arguments["command"]
        if not command:
            raise ValueError("command 不能为空")
        paths = workspace_paths(context, self.paths)
        environment = context.environment
        default_cwd = "."
        if (
            environment is not None
            and environment.working_directory is not None
        ):
            default_cwd = str(environment.working_directory)
        cwd = paths.resolve(arguments.get("cwd", default_cwd))
        if not cwd.is_dir():
            raise ValueError("cwd 必须是目录")
        timeout = min(
            max(float(arguments.get("timeout_seconds", 30)), 0.1),
            300,
        )
        max_output = min(
            max(int(arguments.get("max_output_bytes", 100_000)), 1),
            1_000_000,
        )

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=self._safe_env(context),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise

        return {
            "command": command,
            "cwd": paths.relative(cwd) or ".",
            "exit_code": process.returncode,
            "stdout": stdout[:max_output].decode("utf-8", errors="replace"),
            "stderr": stderr[:max_output].decode("utf-8", errors="replace"),
            "timed_out": timed_out,
            "output_truncated": (
                len(stdout) > max_output or len(stderr) > max_output
            ),
            "source": {
                "kind": "process",
                "argv": command,
                "cwd": paths.relative(cwd) or ".",
                "exit_code": process.returncode,
                "timed_out": timed_out,
            },
        }

    @staticmethod
    def _is_read_only_git(arguments: list[str]) -> bool:
        return bool(arguments) and arguments[0] in {
            "branch",
            "diff",
            "log",
            "rev-parse",
            "show",
            "status",
        }

    @staticmethod
    def _safe_env(context: ToolContext) -> dict[str, str]:
        allowed = {"HOME", "LANG", "LC_ALL", "PATH", "PYTHONPATH", "TMPDIR"}
        result = {
            name: value
            for name, value in os.environ.items()
            if name in allowed
        }
        environment = context.environment
        if environment is not None:
            result.update(environment.env_vars)
        return result
