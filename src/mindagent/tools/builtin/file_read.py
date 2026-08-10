from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mindagent.core import ActionRisk

from ..base import BaseTool, ToolContext, ToolDefinition
from ..workspace import WorkspacePathResolver, workspace_paths


class FileReadTool(BaseTool):
    definition = ToolDefinition(
        name="file_read",
        description=(
            "Read a UTF-8 text file by line range or extract a snippet "
            "around an exact anchor."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "anchor": {"type": "string"},
                "before": {"type": "integer"},
                "after": {"type": "integer"},
                "occurrence": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        risk=ActionRisk.READ_ONLY,
    )

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_bytes: int = 1_000_000,
    ):
        self.paths = WorkspacePathResolver(workspace_root)
        self.max_bytes = max_bytes

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        paths = workspace_paths(context, self.paths)
        path = paths.resolve(arguments["path"])
        if not path.is_file():
            raise ValueError(f"不是文件: {arguments['path']}")
        if path.stat().st_size > self.max_bytes:
            raise ValueError(f"文件超过读取限制: {self.max_bytes} bytes")

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("file_read 仅支持 UTF-8 文本文件") from exc

        match_line = None
        total_matches = None
        if "anchor" in arguments:
            anchor = arguments["anchor"]
            matches = [
                index
                for index, line in enumerate(lines, 1)
                if anchor in line
            ]
            total_matches = len(matches)
            occurrence = arguments.get("occurrence", 1)
            if occurrence < 1 or occurrence > len(matches):
                raise ValueError(
                    f"anchor 匹配 {len(matches)} 处，occurrence 无效"
                )
            match_line = matches[occurrence - 1]
            start = max(1, match_line - max(arguments.get("before", 5), 0))
            end = min(
                len(lines),
                match_line + max(arguments.get("after", 10), 0),
            )
        else:
            start = max(arguments.get("start_line", 1), 1)
            end = min(arguments.get("end_line", len(lines)), len(lines))
            if end < start:
                raise ValueError("end_line 不能小于 start_line")

        content = "\n".join(lines[start - 1 : end])
        relative_path = paths.relative(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "path": relative_path,
            "start_line": start,
            "end_line": end,
            "content": content,
            "match_line": match_line,
            "total_matches": total_matches,
            "truncated": start > 1 or end < len(lines),
            "source": {
                "kind": "workspace_file",
                "path": relative_path,
                "sha256": digest,
            },
            "artifact_refs": [
                {
                    "artifact_id": f"workspace:{relative_path}",
                    "uri": f"workspace://{relative_path}",
                    "media_type": "text/plain",
                    "metadata": {"sha256": digest},
                }
            ],
        }
