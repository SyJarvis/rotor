from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from mindagent.core import ActionRisk

from ..base import BaseTool, ToolContext, ToolDefinition
from ..workspace import WorkspacePathResolver, workspace_paths


class FileSearchTool(BaseTool):
    definition = ToolDefinition(
        name="file_search",
        description=(
            "Search workspace file names or text content. "
            "Returns paths, line numbers, and short matching snippets."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["content", "files"],
                },
                "regex": {"type": "boolean"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk=ActionRisk.READ_ONLY,
    )

    def __init__(self, workspace_root: str | Path):
        self.paths = WorkspacePathResolver(workspace_root)

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        paths = workspace_paths(context, self.paths)
        root = paths.resolve(arguments.get("path", "."))
        query = arguments["query"]
        mode = arguments.get("mode", "content")
        pattern = arguments.get("glob")
        limit = min(max(arguments.get("max_results", 50), 1), 500)
        matches: list[dict[str, Any]] = []

        matcher = None
        if mode == "content" and arguments.get("regex", False):
            try:
                matcher = re.compile(query)
            except re.error as exc:
                raise ValueError(f"无效正则表达式: {exc}") from exc

        for file_path in self._iter_files(root, pattern, paths):
            relative = paths.relative(file_path)
            if mode == "files":
                if query.lower() in relative.lower():
                    matches.append({"path": relative})
            else:
                for line_number, text in self._matching_lines(
                    file_path,
                    query,
                    matcher,
                ):
                    matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "text": text[:500],
                        }
                    )
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break

        return {
            "matches": matches,
            "count": len(matches),
            "truncated": len(matches) >= limit,
            "source": {
                "kind": "workspace_search",
                "root": paths.relative(root) or ".",
                "query": query,
                "mode": mode,
            },
        }

    def _iter_files(
        self,
        root: Path,
        pattern: str | None,
        paths: WorkspacePathResolver,
    ):
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            relative = paths.relative(path)
            if pattern and not fnmatch.fnmatch(relative, pattern):
                continue
            yield path

    @staticmethod
    def _matching_lines(
        path: Path,
        query: str,
        matcher: re.Pattern[str] | None,
    ):
        try:
            with path.open(encoding="utf-8", errors="strict") as file:
                for line_number, line in enumerate(file, 1):
                    text = line.rstrip("\n")
                    if (
                        matcher.search(text)
                        if matcher
                        else query.lower() in text.lower()
                    ):
                        yield line_number, text
        except (UnicodeDecodeError, OSError):
            return
