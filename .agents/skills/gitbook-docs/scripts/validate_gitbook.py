#!/usr/bin/env python3
"""Validate a repository's basic GitBook structure and local Markdown links."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import unquote


LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SUMMARY_LINK_RE = re.compile(r"^\s*-\s+\[[^\]]+\]\(([^)]+)\)", re.MULTILINE)
PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD|FIXME)\b|\[TODO[^\]]*\]", re.IGNORECASE)


def config_value(text: str, key: str, indent: int | None = None) -> str | None:
    prefix = rf"^{' ' * indent if indent is not None else ''}{re.escape(key)}:\s*(.+?)\s*$"
    match = re.search(prefix, text, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else None


def local_target(raw: str, source: Path) -> Path | None:
    target = raw.strip().split(maxsplit=1)[0].strip("<>")
    if target.startswith(("#", "http://", "https://", "mailto:", "tel:")):
        return None
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    return (source.parent / target).resolve() if target else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--allow-orphans", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    config = root / ".gitbook.yaml"
    errors: list[str] = []
    warnings: list[str] = []

    if not config.is_file():
        errors.append("missing .gitbook.yaml")
        docs_root = root / "docs"
        summary = docs_root / "SUMMARY.md"
        readme = docs_root / "README.md"
    else:
        config_text = config.read_text(encoding="utf-8")
        configured_root = config_value(config_text, "root") or "."
        docs_root = (root / configured_root).resolve()
        summary_name = config_value(config_text, "summary", indent=2) or "SUMMARY.md"
        readme_name = config_value(config_text, "readme", indent=2) or "README.md"
        summary = docs_root / summary_name
        readme = docs_root / readme_name

    if not docs_root.is_dir():
        errors.append(f"missing documentation root: {docs_root}")
    if not readme.is_file():
        errors.append(f"missing GitBook readme: {readme}")
    if not summary.is_file():
        errors.append(f"missing GitBook summary: {summary}")

    markdown_files = sorted(docs_root.rglob("*.md")) if docs_root.is_dir() else []
    navigated: set[Path] = set()

    if summary.is_file():
        summary_text = summary.read_text(encoding="utf-8")
        for raw in SUMMARY_LINK_RE.findall(summary_text):
            target = local_target(raw, summary)
            if target is None:
                continue
            navigated.add(target)
            if not target.is_file():
                errors.append(f"missing navigation target: {raw}")

    for page in markdown_files:
        text = page.read_text(encoding="utf-8")
        if PLACEHOLDER_RE.search(text):
            warnings.append(f"placeholder marker: {page.relative_to(root)}")
        for raw in LINK_RE.findall(text):
            target = local_target(raw, page)
            if target is not None and not target.exists():
                errors.append(f"broken link in {page.relative_to(root)}: {raw}")

    expected = {page.resolve() for page in markdown_files if page != summary}
    if readme.is_file():
        navigated.add(readme.resolve())
    orphans = sorted(expected - navigated)
    if orphans and not args.allow_orphans:
        errors.extend(f"orphan page: {path.relative_to(root)}" for path in orphans)

    for message in warnings:
        print(f"WARNING: {message}")
    for message in errors:
        print(f"ERROR: {message}")
    print(
        f"Checked {len(markdown_files)} Markdown files: "
        f"{len(errors)} error(s), {len(warnings)} warning(s)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
