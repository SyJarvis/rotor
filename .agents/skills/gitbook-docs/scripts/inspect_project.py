#!/usr/bin/env python3
"""Print a bounded JSON inventory for documentation planning."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


EXCLUDED = {
    ".agents",
    ".codex",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}
SIGNALS = {
    ".gitbook.yaml",
    "AGENTS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "Makefile",
    "README.md",
    "book.json",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
}
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
}


def is_excluded(path: Path, root: Path) -> bool:
    return any(part in EXCLUDED for part in path.relative_to(root).parts)


def relative_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not is_excluded(path, root)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument(
        "--max-list",
        type=int,
        default=200,
        help="maximum paths listed in each detailed category",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    if args.max_list < 1:
        parser.error("--max-list must be positive")

    files = relative_files(root)
    suffixes = Counter(path.suffix.lower() or "<none>" for path in files)
    source_files = [path for path in files if path.suffix.lower() in SOURCE_SUFFIXES]
    docs = [path for path in files if path.suffix.lower() in {".md", ".mdx", ".rst"}]
    tests = []
    for path in files:
        relative = path.relative_to(root)
        name = path.name.lower()
        if (
            any(part.lower() in {"test", "tests"} for part in relative.parts)
            or name.startswith("test_")
            or name.endswith(("_test.py", ".spec.js", ".spec.ts", ".test.js", ".test.ts"))
        ):
            tests.append(path)
    examples = [
        path
        for path in files
        if any(part.lower() in {"example", "examples", "demo", "demos"} for part in path.parts)
    ]

    def listed(paths: list[Path]) -> list[str]:
        return [str(path.relative_to(root)) for path in paths[: args.max_list]]

    inventory = {
        "root": str(root),
        "top_level": sorted(path.name for path in root.iterdir() if path.name not in EXCLUDED),
        "project_signals": sorted(
            str(path.relative_to(root))
            for path in files
            if path.name in SIGNALS or path.name.startswith("README")
        ),
        "counts": {
            "files": len(files),
            "source": len(source_files),
            "documentation": len(docs),
            "tests": len(tests),
            "examples": len(examples),
        },
        "suffix_counts": dict(sorted(suffixes.items(), key=lambda item: (-item[1], item[0]))),
        "source_files": listed(source_files),
        "documentation_files": listed(docs),
        "test_files": listed(tests),
        "example_files": listed(examples),
        "truncated_at": args.max_list,
    }
    print(json.dumps(inventory, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
