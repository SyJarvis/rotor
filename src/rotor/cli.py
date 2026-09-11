import argparse
from urllib.parse import unquote, urlparse
from pathlib import Path

import uvicorn

from rotor.config import DEFAULT_CACHE_DIR, settings
from rotor.core.logging_config import configure_file_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="rotor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the Rotor API server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    serve_parser.add_argument("--port", type=int, default=8000, help="Bind port")
    serve_parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload")
    serve_parser.set_defaults(func=serve)

    cleanup_parser = subparsers.add_parser(
        "cleanup-empty-requests",
        help="Inspect or archive the known empty-stream request batch",
    )
    cleanup_parser.add_argument(
        "--database",
        type=Path,
        help="SQLite database path (defaults to the configured Rotor database)",
    )
    cleanup_parser.add_argument(
        "--archive",
        type=Path,
        help="Archive path to create when applying the cleanup",
    )
    cleanup_mode = cleanup_parser.add_mutually_exclusive_group()
    cleanup_mode.add_argument(
        "--apply",
        action="store_true",
        help="Archive and delete matching rows (requires --yes)",
    )
    cleanup_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matching counts without writing (the default)",
    )
    cleanup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly confirm the destructive cleanup",
    )
    cleanup_parser.set_defaults(func=cleanup_empty_requests_command)

    args = parser.parse_args()
    args.func(args)


def serve(args: argparse.Namespace) -> None:
    ensure_runtime_dirs()
    configure_file_logging(settings.ROTOR_LOG_DIR, settings.LOG_LEVEL)
    uvicorn.run(
        "rotor.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.LOG_LEVEL.lower(),
    )


def cleanup_empty_requests_command(args: argparse.Namespace) -> None:
    """Run the guarded empty-request maintenance operation."""
    from rotor.maintenance.empty_requests import (
        EmptyRequestCleanupError,
        cleanup_empty_requests,
    )

    database_path = args.database or sqlite_path_from_url(settings.DATABASE_URL)
    if database_path is None:
        raise SystemExit("cleanup-empty-requests requires a SQLite database")
    try:
        report = cleanup_empty_requests(
            database_path,
            archive_path=args.archive,
            apply=bool(args.apply),
            confirm=bool(args.yes),
        )
    except EmptyRequestCleanupError as exc:
        raise SystemExit(f"cleanup-empty-requests failed: {exc}") from exc

    mode = "applied" if args.apply else "dry-run"
    print(f"mode: {mode}")
    print(f"matched usage_ledger rows: {report.matched_usage_ledger}")
    print(f"mapped request_logs rows: {report.matched_request_logs}")
    for table, count in report.related_rows.items():
        print(f"related {table} rows: {count}")
    if report.archived_path is not None:
        print(f"archive: {report.archived_path}")
        for table, count in report.deleted_rows.items():
            print(f"deleted {table} rows: {count}")


def ensure_runtime_dirs() -> None:
    cache_dir = DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    Path(settings.ROTOR_LOG_DIR).expanduser().mkdir(parents=True, exist_ok=True)
    Path(settings.CONVERSATION_STORE_DIR).expanduser().mkdir(parents=True, exist_ok=True)

    db_path = sqlite_path_from_url(settings.DATABASE_URL)
    if db_path:
        db_path.expanduser().parent.mkdir(parents=True, exist_ok=True)


def sqlite_path_from_url(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite"):
        return None

    parsed = urlparse(database_url)
    if parsed.path in ("", "/"):
        return None

    if parsed.netloc:
        return Path(f"//{parsed.netloc}{unquote(parsed.path)}")
    return Path(unquote(parsed.path))


if __name__ == "__main__":
    main()
