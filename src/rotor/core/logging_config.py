from datetime import datetime
import logging
from pathlib import Path
from typing import TextIO


LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class MonthlyDailyFileHandler(logging.Handler):
    """Write records to YYYY-MM/YYYY-MM-DD.log using local time."""

    terminator = "\n"

    def __init__(self, log_dir: str | Path) -> None:
        super().__init__()
        self.log_dir = Path(log_dir).expanduser()
        self._current_path: Path | None = None
        self._stream: TextIO | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            path = self._path_for_record(record)
            if path != self._current_path:
                self._open(path)
            if self._stream is None:
                return
            self._stream.write(self.format(record) + self.terminator)
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            self.release()
            super().close()

    def _path_for_record(self, record: logging.LogRecord) -> Path:
        local_time = datetime.fromtimestamp(record.created).astimezone()
        return (
            self.log_dir
            / local_time.strftime("%Y-%m")
            / f"{local_time:%Y-%m-%d}.log"
        )

    def _open(self, path: Path) -> None:
        if self._stream is not None:
            self._stream.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a", encoding="utf-8")
        self._current_path = path


def configure_file_logging(
    log_dir: str | Path,
    level: int | str,
) -> MonthlyDailyFileHandler:
    root_logger = logging.getLogger()
    resolved_dir = Path(log_dir).expanduser().resolve()
    resolved_level = logging.getLevelNamesMapping().get(
        str(level).upper(), level
    )
    root_logger.setLevel(resolved_level)

    for handler in root_logger.handlers:
        if (
            isinstance(handler, MonthlyDailyFileHandler)
            and handler.log_dir.resolve() == resolved_dir
        ):
            handler.setLevel(resolved_level)
            return handler

    handler = MonthlyDailyFileHandler(resolved_dir)
    handler.setLevel(resolved_level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root_logger.addHandler(handler)
    return handler
