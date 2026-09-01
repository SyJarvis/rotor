from datetime import datetime
import logging

from rotor.core.logging_config import (
    MonthlyDailyFileHandler,
    configure_file_logging,
)


def _record(message: str, created_at: datetime) -> logging.LogRecord:
    record = logging.LogRecord(
        name="rotor.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    record.created = created_at.timestamp()
    return record


def test_file_handler_groups_logs_by_local_month_and_day(tmp_path) -> None:
    local_timezone = datetime.now().astimezone().tzinfo
    handler = MonthlyDailyFileHandler(tmp_path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.handle(_record(
            "八月日志",
            datetime(2026, 8, 31, 12, tzinfo=local_timezone),
        ))
        handler.handle(_record(
            "九月第一条",
            datetime(2026, 9, 1, 12, tzinfo=local_timezone),
        ))
        handler.handle(_record(
            "九月第二条",
            datetime(2026, 9, 1, 18, tzinfo=local_timezone),
        ))
    finally:
        handler.close()

    august = tmp_path / "2026-08" / "2026-08-31.log"
    september = tmp_path / "2026-09" / "2026-09-01.log"
    assert august.read_text(encoding="utf-8") == "八月日志\n"
    assert september.read_text(encoding="utf-8") == (
        "九月第一条\n九月第二条\n"
    )


def test_configure_file_logging_is_idempotent(tmp_path) -> None:
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    handler = configure_file_logging(tmp_path, "INFO")
    try:
        assert configure_file_logging(tmp_path, "DEBUG") is handler
        assert handler.level == logging.DEBUG
        assert root_logger.handlers.count(handler) == 1
    finally:
        root_logger.removeHandler(handler)
        handler.close()
        root_logger.setLevel(previous_level)
