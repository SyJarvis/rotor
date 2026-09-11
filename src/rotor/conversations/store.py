import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.observability import performance_metrics
from rotor.config import settings
from rotor.conversations.sanitizer import sanitize
from rotor.conversations.schema import utc_now_iso
from rotor.database import async_session_maker
from rotor.models.conversation import ConversationRecord
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.schemas.request import ChatCompletionRequest

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3  # 1 initial + 2 retries
_DB_BATCH_SIZE = 32


@dataclass(slots=True)
class ConversationHandle:
    """Mutable container accumulating one request's full record.

    The request path fills these fields via ``start`` / ``append_*``; only
    ``finish`` enqueues a write. File-destined data lives here so the worker
    can emit one complete JSONL line per request (no partial/ragged events).
    """

    conversation_id: str
    request_id: str
    file_path: Path
    model: str = ""
    protocol: str = ""
    provider: str = ""
    channel_id: Optional[int] = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    response: Optional[dict[str, Any]] = None
    usage: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    status: str = "started"
    latency_ms: Optional[int] = None
    started_at: str = ""
    committed: bool = False  # guards against double finish()


@dataclass(slots=True)
class _Event:
    kind: str  # "db_only" (DB upsert/update) | "commit" (JSONL line + DB update)
    file_path: Optional[Path] = None
    payload: Optional[dict[str, Any]] = None  # full record line (commit only)
    record_op: Optional[dict[str, Any]] = None  # {"action":"upsert"|"update","fields":{...}}
    record_key: Optional[tuple[str, str]] = None  # (conversation_id, request_id)
    attempt: int = 0
    file_written: bool = False
    queued_at: float | None = None


class ConversationStore:
    """Async filesystem JSONL conversation archive with a DB index.

    The request path only mutates an in-memory ``ConversationHandle`` (plus a
    couple of lightweight DB ops for observability); a single background worker
    drains the queue and performs file I/O + DB writes. One JSONL line is
    written per request, at ``finish`` time, so each line is a complete,
    training-ready sample. Failures are retried up to ``_MAX_ATTEMPTS`` times;
    events still failing are dropped and logged.
    """

    _SENTINEL: object = object()

    def __init__(self) -> None:
        self._queue: Optional[asyncio.Queue[_Event]] = None
        self._worker_task: Optional[asyncio.Task[None]] = None
        # Drop observability: queue-full and retry-exhausted counters so the
        # loss rate is visible without guessing from logs.
        self.dropped_queue_full: int = 0
        self.dropped_retry_exhausted: int = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def attach(self) -> None:
        """Start the background worker. Call once at app startup."""
        if not settings.CONVERSATION_STORE_ENABLED:
            logger.info("Conversation store disabled, worker not started")
            return
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._queue = asyncio.Queue(maxsize=settings.CONVERSATION_QUEUE_MAXSIZE)
        self._worker_task = asyncio.create_task(
            self._worker(), name="conversation-store-worker"
        )
        logger.info("Conversation store worker started")

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for queued and in-flight events, including retries, to finish."""
        if self._queue is None:
            return
        worker = self._worker_task
        if worker is None:
            raise RuntimeError("Conversation worker is not running")
        started_at = time.monotonic()
        joined = asyncio.create_task(self._queue.join())
        try:
            done, _ = await asyncio.wait(
                (joined, worker), timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if worker in done:
                await worker
                raise RuntimeError("Conversation worker stopped before drain completed")
            if joined not in done:
                raise TimeoutError("Conversation store drain timed out")
            await joined
        finally:
            joined.cancel()
            await asyncio.gather(joined, return_exceptions=True)
            performance_metrics.observe("store.drain_ms", (time.monotonic() - started_at) * 1000)

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Signal the worker to stop and wait for it to drain.

        If the queue is momentarily full, retry delivering the sentinel while
        the worker drains pending events. Cancel the worker if it does not
        exit within ``timeout`` seconds (e.g. stuck on a failing event).
        """
        if self._worker_task is None:
            return

        if self._queue is not None:
            sentinel_deadline = asyncio.get_event_loop().time() + timeout

            async def _deliver_sentinel() -> None:
                assert self._queue is not None
                while True:
                    try:
                        self._queue.put_nowait(self._SENTINEL)  # type: ignore[arg-type]
                        return
                    except asyncio.QueueFull:
                        remaining = sentinel_deadline - asyncio.get_event_loop().time()
                        if remaining <= 0:
                            return
                        await asyncio.sleep(min(0.05, remaining))

            try:
                await asyncio.wait_for(_deliver_sentinel(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        try:
            await asyncio.wait_for(self._worker_task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Conversation worker did not shut down within %.1fs, cancelling",
                timeout,
            )
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._worker_task = None
            self._queue = None

    # ------------------------------------------------------------------ #
    # Public API (request-path side; only mutates handle + light DB ops)
    # ------------------------------------------------------------------ #
    async def start(
        self,
        db: AsyncSession,  # kept for signature compatibility; unused
        *,
        conversation_id: str,
        request_id: str,
        token: Token,
        request: ChatCompletionRequest,
        protocol: str,
    ) -> ConversationHandle:
        file_path = self._file_path()
        handle = ConversationHandle(
            conversation_id=conversation_id,
            request_id=request_id,
            file_path=file_path,
            model=request.model,
            protocol=protocol,
            started_at=utc_now_iso(),
        )

        if not settings.CONVERSATION_STORE_ENABLED:
            return handle

        if settings.SAVE_CONVERSATION_BODY:
            handle.messages = [m.model_dump() for m in request.messages]

        # Record start in the DB index (idempotent upsert — survives any
        # accidental double start() on the same request id).
        self._enqueue(
            _Event(
                kind="db_only",
                record_op={
                    "action": "upsert",
                    "fields": {
                        "conversation_id": conversation_id,
                        "request_id": request_id,
                        "user_id": token.user_id,
                        "token_id": token.id,
                        "model": request.model,
                        "protocol": protocol,
                        "file_path": str(file_path),
                        "status": "started",
                    },
                },
                record_key=(conversation_id, request_id),
            )
        )

        return handle

    async def append_routing(self, handle: ConversationHandle, channel: Channel) -> None:
        if not settings.CONVERSATION_STORE_ENABLED:
            return
        handle.provider = channel.type
        handle.channel_id = channel.id
        self._enqueue(
            _Event(
                kind="db_only",
                record_op={
                    "action": "update",
                    "fields": {
                        "channel_id": channel.id,
                        "provider": channel.type,
                    },
                },
                record_key=(handle.conversation_id, handle.request_id),
            )
        )

    async def append_response(self, handle: ConversationHandle, response_data: dict[str, Any]) -> None:
        if not settings.CONVERSATION_STORE_ENABLED or not settings.SAVE_PROVIDER_RESPONSE:
            return
        sanitized = sanitize(response_data)
        try:
            if sanitized.get("object") == "response":
                message = {
                    "id": sanitized.get("id"),
                    "status": sanitized.get("status"),
                    "output": sanitized.get("output") or [],
                }
            elif sanitized.get("type") == "message":
                message = {
                    "id": sanitized.get("id"),
                    "role": sanitized.get("role"),
                    "content": sanitized.get("content") or [],
                    "stop_reason": sanitized.get("stop_reason"),
                }
            else:
                message = sanitized["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            message = None
        handle.response = message

    async def append_usage(self, handle: ConversationHandle, usage: dict[str, Any]) -> None:
        if not settings.CONVERSATION_STORE_ENABLED:
            return
        handle.usage = usage or None

    async def append_error(self, handle: ConversationHandle, error_code: str, error_message: str) -> None:
        if not settings.CONVERSATION_STORE_ENABLED:
            return
        handle.error = {"code": error_code, "message": error_message}

    async def finish(self, handle: ConversationHandle, status: str, latency_ms: Optional[int]) -> None:
        if not settings.CONVERSATION_STORE_ENABLED:
            return
        if handle.committed:
            return
        handle.committed = True
        handle.status = status
        handle.latency_ms = latency_ms

        record_key = (handle.conversation_id, handle.request_id)
        line = {
            "conversation_id": handle.conversation_id,
            "request_id": handle.request_id,
            "model": handle.model,
            "provider": handle.provider or None,
            "protocol": handle.protocol,
            "created_at": handle.started_at,
            "status": handle.status,
            "latency_ms": handle.latency_ms,
            "messages": handle.messages,
            "response": handle.response,
            "usage": handle.usage,
            "error": handle.error,
        }
        self._enqueue(
            _Event(
                kind="commit",
                file_path=handle.file_path,
                payload=line,
                record_op={
                    "action": "update",
                    "fields": {
                        "status": handle.status,
                        "provider": handle.provider or None,
                        "channel_id": handle.channel_id,
                    },
                },
                record_key=record_key,
            )
        )

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    def drop_stats(self) -> dict[str, int]:
        """Cumulative dropped-event counters (queue-full / retry-exhausted)."""
        return {
            "dropped_queue_full": self.dropped_queue_full,
            "dropped_retry_exhausted": self.dropped_retry_exhausted,
        }

    def status(self) -> dict[str, object]:
        """Runtime health snapshot for observability endpoints."""
        from rotor.config import settings as config_settings

        return {
            "enabled": config_settings.CONVERSATION_STORE_ENABLED,
            "worker_running": self._worker_task is not None
            and not self._worker_task.done(),
            "queue_size": self._queue.qsize() if self._queue is not None else 0,
            "queue_maxsize": config_settings.CONVERSATION_QUEUE_MAXSIZE,
            **self.drop_stats(),
        }

    def _enqueue(self, ev: _Event) -> None:
        assert self._queue is not None, "ConversationStore.attach() not called"
        ev.queued_at = time.monotonic()
        try:
            self._queue.put_nowait(ev)
        except asyncio.QueueFull:
            self.dropped_queue_full += 1
            logger.warning(
                "Conversation queue full (maxsize=%d), dropping kind=%s "
                "(dropped_total=%d)",
                settings.CONVERSATION_QUEUE_MAXSIZE,
                ev.kind,
                self.dropped_queue_full + self.dropped_retry_exhausted,
            )

    async def _worker(self) -> None:
        assert self._queue is not None
        pending = None
        async with async_session_maker() as db:
            while True:
                item = pending if pending is not None else await self._queue.get()
                pending = None
                if item is self._SENTINEL:
                    self._queue.task_done()
                    break
                batch = [item]
                if item.kind == "db_only":
                    while len(batch) < _DB_BATCH_SIZE:
                        try:
                            following = self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if following is self._SENTINEL or following.kind != "db_only":
                            pending = following
                            break
                        batch.append(following)
                batch_started = time.monotonic()
                performance_metrics.observe("store.batch_size", len(batch))
                for ev in batch:
                    if ev.queued_at is not None:
                        performance_metrics.observe("store.queue_wait_ms", (batch_started - ev.queued_at) * 1000)
                try:
                    if len(batch) == 1:
                        await self._handle_event(db, item)
                    else:
                        try:
                            for ev in batch:
                                await self._process(db, ev)
                            await db.commit()
                        except Exception:
                            logger.exception("Conversation batch failed; retrying events in order")
                            await self._safe_rollback(db)
                            for ev in batch:
                                ev.attempt += 1
                                await self._handle_event(db, ev)
                finally:
                    performance_metrics.observe("store.batch_ms", (time.monotonic() - batch_started) * 1000)
                    for _ in batch:
                        self._queue.task_done()

    async def _handle_event(self, db: AsyncSession, ev: _Event) -> None:
        while ev.attempt < _MAX_ATTEMPTS:
            try:
                await self._process(db, ev)
                await db.commit()
                return
            except Exception:
                logger.exception("Conversation event failed kind=%s attempt=%d", ev.kind, ev.attempt)
                await self._safe_rollback(db)
                ev.attempt += 1
        self.dropped_retry_exhausted += 1
        logger.error(
            "Dropping event kind=%s after %d attempts "
            "(dropped_total=%d)",
            ev.kind,
            _MAX_ATTEMPTS,
            self.dropped_queue_full + self.dropped_retry_exhausted,
        )

    async def _process(self, db: AsyncSession, ev: _Event) -> None:
        # File write — only on commit, one complete line per request.
        if (
            ev.kind == "commit" and not ev.file_written
            and ev.payload is not None and ev.file_path is not None
        ):
            line = json.dumps(ev.payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            await self._append_raw(ev.file_path, line)
            ev.file_written = True

        # DB op.
        if ev.record_op is not None:
            action = ev.record_op.get("action")
            fields = ev.record_op.get("fields") or {}
            if action == "upsert":
                await self._upsert_record(db, fields)
            elif action == "update" and ev.record_key is not None:
                conv_id, req_id = ev.record_key
                await db.execute(
                    update(ConversationRecord)
                    .where(
                        ConversationRecord.conversation_id == conv_id,
                        ConversationRecord.request_id == req_id,
                    )
                    .values(**fields)
                )

    async def _upsert_record(self, db: AsyncSession, fields: dict[str, Any]) -> None:
        stmt = sqlite_insert(ConversationRecord).values(**fields)
        stmt = stmt.on_conflict_do_update(
            index_elements=["conversation_id", "request_id"],
            set_={
                # Refresh mutable metadata on conflict, but never clobber a
                # terminal status (success/failed) back to "started".
                "model": stmt.excluded.model,
                "protocol": stmt.excluded.protocol,
                "file_path": stmt.excluded.file_path,
            },
        )
        await db.execute(stmt)

    async def _safe_rollback(self, db: AsyncSession) -> None:
        try:
            await db.rollback()
        except Exception:
            logger.exception("Rollback failed")

    # ------------------------------------------------------------------ #
    # File path helpers
    # ------------------------------------------------------------------ #
    async def append_raw(self, file_path: Path, payload: dict[str, Any]) -> None:
        """Direct append bypassing the worker — retained for ad-hoc use."""
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        await self._append_raw(file_path, line)

    async def _append_raw(self, file_path: Path, line: str) -> None:
        def _write() -> None:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with file_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

        await asyncio.to_thread(_write)

    def _file_path(self) -> Path:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        day = now.strftime("%Y-%m-%d")
        return Path(settings.CONVERSATION_STORE_DIR) / month / f"{day}.jsonl"

    @staticmethod
    def _safe_component(value: object) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
        return cleaned[:128] or "unknown"
