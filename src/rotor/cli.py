import argparse
from urllib.parse import unquote, urlparse
from pathlib import Path

import uvicorn

from rotor.config import DEFAULT_CACHE_DIR, settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="rotor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the Rotor API server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    serve_parser.add_argument("--port", type=int, default=8000, help="Bind port")
    serve_parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload")
    serve_parser.set_defaults(func=serve)

    args = parser.parse_args()
    args.func(args)


def serve(args: argparse.Namespace) -> None:
    ensure_runtime_dirs()
    uvicorn.run(
        "rotor.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.LOG_LEVEL.lower(),
    )


def ensure_runtime_dirs() -> None:
    cache_dir = DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
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
