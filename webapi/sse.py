import asyncio
import json
import queue
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterator


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

    def __init__(self) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue()

    def put(self, frame: SSEFrame) -> None:
        self._queue.put(frame.encode())

    def close(self) -> None:
        self._queue.put(None)

    def __iter__(self) -> Iterator[bytes]:
        while True:
            item = self._queue.get()
            if item is None:
                break
            yield item

    async def aiter(self) -> AsyncIterator[bytes]:
        """Async iteration that yields queue items without blocking the loop.

        Each ``queue.get()`` is offloaded to the default thread pool via
        ``asyncio.to_thread`` so the event loop stays responsive while the
        worker thread pushes frames. When the client disconnects,
        ``StreamingResponse`` closes this generator which raises
        ``GeneratorExit`` — callers can catch that (or wrap it in a
        try/finally) to notify the worker.
        """
        loop = asyncio.get_running_loop()
        while True:
            item = await loop.run_in_executor(None, self._queue.get)
            if item is None:
                break
            yield item
