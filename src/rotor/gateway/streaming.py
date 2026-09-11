"""Release upstream attempt ownership across the complete ASGI response lifetime."""

import asyncio
from collections.abc import Awaitable, Callable
import logging

import anyio
from starlette.responses import StreamingResponse

from rotor.gateway.routing import AttemptAdmission


logger = logging.getLogger(__name__)


class AdmissionStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content,
        *,
        admission: AttemptAdmission | None = None,
        close_callbacks: tuple[Callable[[], Awaitable[None]], ...] = (),
        **kwargs,
    ):
        super().__init__(content, **kwargs)
        self.admission = admission
        self.close_callbacks = close_callbacks

    async def _close_resources(self) -> None:
        close_iterator = getattr(self.body_iterator, "aclose", None)
        callbacks = ((close_iterator,) if close_iterator is not None else ()) + self.close_callbacks
        for close in callbacks:
            try:
                await close()
            except (Exception, asyncio.CancelledError):
                logger.exception("Failed to close streaming response resource")

    async def __call__(self, scope, receive, send) -> None:
        response_failed = False
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            response_failed = True
            raise
        finally:
            if self.admission is not None:
                try:
                    self.admission.engine.release_attempt(self.admission)
                except Exception:
                    logger.exception("Failed to release streaming attempt")

            # An unstarted async generator does not execute its finally on
            # aclose(), so pre-created upstream resources also need callbacks.
            cancelled = None
            with anyio.CancelScope(shield=True):
                cleanup = asyncio.create_task(self._close_resources())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError as exc:
                        cancelled = cancelled or exc
                cleanup.result()
            if cancelled is not None and not response_failed:
                raise cancelled
