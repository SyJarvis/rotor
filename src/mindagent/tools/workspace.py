from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ToolContext


class WorkspacePathError(ValueError):
    pass


class WorkspacePathResolver:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"workspace 不存在或不是目录: {self.root}")

    def resolve(
        self,
        path: str | Path,
        *,
        must_exist: bool = True,
    ) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=must_exist)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError(
                f"路径超出 workspace: {path}"
            ) from exc
        return resolved

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.root))


def workspace_paths(
    context: ToolContext,
    fallback: WorkspacePathResolver,
) -> WorkspacePathResolver:
    environment = context.environment
    if environment is None or environment.workspace_root is None:
        return fallback
    return WorkspacePathResolver(environment.workspace_root)
