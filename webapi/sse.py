import asyncio
import json
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterator


logger = logging.getLogger(__name__)


# Hard cap on the per-stream SSE buffer. A long agent run with no
# reader can otherwise queue tens of thousands of frames in memory
# before the worker thread finishes — slow clients, dropped sockets
# during a 10-minute tool burst, etc. 1024 frames is roughly 1-2 MB
# of text and well above any sane interactive turn (a real chat run
# emits a few hundred frames). When we hit it we drop new frames and
# log once; the worker keeps running so the run completes server-side
# even if the client never reconnects to drain.
_DEFAULT_SSE_BUFFER_FRAMES = 1024


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class SSEFrame:
    event: str
    data: dict[str, Any]

    def encode(self) -> bytes:
        payload = json.dumps(self.data, ensure_ascii=False)
        return f"event: {self.event}\ndata: {payload}\n\n".encode("utf-8")


class SSEEmitter:
    def __init__(self, session_id: str, run_id: str | None = None):
        self.session_id = session_id
        self.run_id = run_id
        self._seq = 0

    def event(self, name: str, **payload: Any) -> SSEFrame:
        self._seq += 1
        envelope = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "seq": self._seq,
            "ts": utc_now_iso(),
        }
        envelope.update(payload)
        return SSEFrame(event=name, data=envelope)


class SSEStream:
    """Thread-safe byte queue consumed by a FastAPI StreamingResponse.

    Supports both sync (``__iter__``) and async (``aiter``) consumption.
    The async form runs the blocking ``queue.get()`` in a thread pool so
    it doesn't block the event loop, and lets the StreamingResponse
    detect client disconnect via GeneratorExit / CancelledError — the
    route handler can then call ``agent.interrupt()`` on its worker.
    """

    def __init__(self, max_frames: int = _DEFAULT_SSE_BUFFER_FRAMES) -> None:
        # Bounded queue: see ``_DEFAULT_SSE_BUFFER_FRAMES`` for the
        # rationale. ``maxsize=0`` means unbounded — we never want
        # that for an SSE buffer fed by a worker thread that can
        # outrun the client.
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=max(1, max_frames))
        self._dropped_frames = 0
        # ``_closed`` is checked by the consumer iterators after every
        # ``get()`` so the close path doesn't need to push a sentinel
        # through a saturated queue (which would deadlock the worker
        # thread on the bounded ``put()``).
        self._closed = threading.Event()

    def put(self, frame: SSEFrame) -> None:
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(frame.encode())
        except queue.Full:
            # Slow / disconnected client. Drop the frame and keep the
            # worker running so the run still completes on the server
            # (and is persisted to the session DB). The first drop is
            # logged at WARNING and subsequent drops at DEBUG so we
            # don't spam logs during a sustained backpressure event.
            if self._dropped_frames == 0:
                logger.warning(
                    "[webapi.sse] buffer full (cap=%d), dropping frame event=%s",
                    self._queue.maxsize,
                    frame.event,
                )
            else:
                logger.debug(
                    "[webapi.sse] buffer full, dropping frame event=%s (dropped=%d)",
                    frame.event,
                    self._dropped_frames + 1,
                )
            self._dropped_frames += 1

    def close(self) -> None:
        """Signal end-of-stream.

        Setting ``_closed`` is non-blocking — the consumer iterators
        race ``queue.get(timeout=...)`` against the close flag and
        terminate as soon as both the queue is empty AND the stream
        is closed. This avoids the deadlock that a sentinel-based
        close would hit when the buffer is saturated.
        """
        self._closed.set()

    @property
    def dropped_frames(self) -> int:
        """How many frames were dropped due to a full buffer."""
        return self._dropped_frames

    # Consumer side. Both iterators poll the queue with a short
    # timeout so they can periodically check the close flag without
    # busy-waiting. The poll interval is small enough (50 ms) that
    # close-to-EOF latency is imperceptible to a human reader, and
    # large enough that an idle stream uses negligible CPU.
    _POLL_INTERVAL_S = 0.05

    def __iter__(self) -> Iterator[bytes]:
        while True:
            try:
                yield self._queue.get(timeout=self._POLL_INTERVAL_S)
            except queue.Empty:
                if self._closed.is_set():
                    return

    async def aiter(self) -> AsyncIterator[bytes]:
        """Async iteration that yields queue items without blocking the loop.

        Each ``queue.get`` is offloaded to the default thread pool via
        ``loop.run_in_executor`` so the event loop stays responsive
        while the worker thread pushes frames. When the client
        disconnects, ``StreamingResponse`` closes this generator which
        raises ``GeneratorExit`` — callers can catch that (or wrap it
        in a try/finally) to notify the worker.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                item = await loop.run_in_executor(
                    None,
                    lambda: self._queue.get(timeout=self._POLL_INTERVAL_S),
                )
            except queue.Empty:
                if self._closed.is_set():
                    return
                continue
            yield item
