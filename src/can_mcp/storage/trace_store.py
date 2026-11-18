from collections import deque
import threading

from can_mcp.core.filters import FrameFilter
from can_mcp.core.models import CanFrame


class TraceStore:
    def __init__(self, max_size: int = 10_000) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be greater than zero")
        self._frames: deque[CanFrame] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._overwritten_count = 0

    def append(self, frame: CanFrame) -> None:
        with self._lock:
            if len(self._frames) == self._frames.maxlen:
                self._overwritten_count += 1
            self._frames.append(frame)

    def query(
        self,
        frame_filter: FrameFilter | None = None,
        limit: int = 100,
    ) -> list[CanFrame]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        with self._lock:
            snapshot = tuple(self._frames)
        frames = (
            frame for frame in snapshot
            if frame_filter is None or frame_filter.matches(frame)
        )
        return list(frames)[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._overwritten_count = 0

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._frames)

    @property
    def overwritten_count(self) -> int:
        with self._lock:
            return self._overwritten_count
