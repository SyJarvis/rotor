from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mindagent.core import ActionRisk

from ..base import BaseTool, ToolContext, ToolDefinition
from ..workspace import WorkspacePathResolver, workspace_paths


class FileEditTool(BaseTool):
    definition = ToolDefinition(
        name="file_edit",
        description=(
            "Create, overwrite, or make one exact text replacement in "
            "a UTF-8 workspace file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "operation": {
                    "type": "string",
                    "enum": ["create", "overwrite", "replace"],
                },
                "content": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "operation"],
            "additionalProperties": False,
        },
        risk=ActionRisk.WRITE,
    )

    def __init__(self, workspace_root: str | Path):
        self.paths = WorkspacePathResolver(workspace_root)

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        paths = workspace_paths(context, self.paths)
        operation = arguments["operation"]
        path = paths.resolve(
            arguments["path"],
            must_exist=operation != "create",
        )

        if operation == "create":
            if path.exists():
                raise FileExistsError(f"文件已存在: {arguments['path']}")
            content = self._required(arguments, "content")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return self._result(path, paths, created=True, content=content)

        if not path.is_file():
            raise ValueError(f"不是文件: {arguments['path']}")

        if operation == "overwrite":
            content = self._required(arguments, "content")
            path.write_text(content, encoding="utf-8")
            return self._result(path, paths, created=False, content=content)

        old_text = self._required(arguments, "old_text")
        new_text = self._required(arguments, "new_text")
        content = path.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count != 1:
            raise ValueError(f"old_text 必须唯一匹配，实际匹配 {count} 处")
        updated = content.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        return self._result(path, paths, created=False, content=updated)

    def _result(
        self,
        path: Path,
        paths: WorkspacePathResolver,
        *,
        created: bool,
        content: str,
    ) -> dict[str, Any]:
        relative_path = paths.relative(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "path": relative_path,
            "created": created,
            "bytes_written": len(content.encode("utf-8")),
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

    @staticmethod
    def _required(arguments: dict[str, Any], name: str) -> str:
        if name not in arguments:
            raise ValueError(f"{arguments['operation']} 操作需要 {name}")
        return arguments[name]
