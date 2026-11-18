import heapq
import logging
import random
import threading
import time
from collections.abc import Callable

from can_mcp.core.models import CanFrame


_LOGGER = logging.getLogger(__name__)


class VirtualFaultInjector:
    MAX_PENDING_FRAMES = 10_000

    def __init__(
        self,
        deliver: Callable[[CanFrame], None],
        drop_rate: float = 0.0,
        max_delay_ms: int = 0,
        reorder_rate: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self._deliver = deliver
        self._random = random.Random(seed)
        self._lock = threading.Condition()
        self._queue: list[tuple[float, int, CanFrame]] = []
        self._sequence = 0
        self._pending_reorder: CanFrame | None = None
        self._pending_reorder_due: float | None = None
        self._stop = False
        self.drop_rate = 0.0
        self.max_delay_ms = 0
        self.reorder_rate = 0.0
        self.dropped_count = 0
        self.delayed_count = 0
        self.reordered_count = 0
        self.configure(drop_rate, max_delay_ms, reorder_rate)
        self._worker = threading.Thread(
            target=self._run,
            name="can-mcp-virtual-fault-injector",
            daemon=True,
        )
        self._worker.start()

    def configure(
        self,
        drop_rate: float,
        max_delay_ms: int,
        reorder_rate: float,
    ) -> None:
        for name, rate in (("drop_rate", drop_rate), ("reorder_rate", reorder_rate)):
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise ValueError(f"{name} must be a number between 0 and 1.")
            if not 0 <= rate <= 1:
                raise ValueError(f"{name} must be between 0 and 1.")
        if (
            isinstance(max_delay_ms, bool)
            or not isinstance(max_delay_ms, int)
            or not 0 <= max_delay_ms <= 60_000
        ):
            raise ValueError("max_delay_ms must be an integer between 0 and 60000.")
        with self._lock:
            self.drop_rate = float(drop_rate)
            self.max_delay_ms = max_delay_ms
            self.reorder_rate = float(reorder_rate)
            self._lock.notify_all()

    def process(self, frame: CanFrame) -> list[CanFrame]:
        with self._lock:
            if self._stop:
                return [frame]
            if (
                self.drop_rate == 0
                and self.max_delay_ms == 0
                and self.reorder_rate == 0
            ):
                return [frame]
            if self._random.random() < self.drop_rate:
                self.dropped_count += 1
                return []

            now = time.monotonic()
            delay = self._random.uniform(0, self.max_delay_ms) / 1000
            if delay > 0:
                self.delayed_count += 1
            if (
                self._pending_reorder is None
                and self._random.random() < self.reorder_rate
            ):
                self._pending_reorder = frame
                self._pending_reorder_due = now + max(
                    delay,
                    self.max_delay_ms / 1000,
                    0.01,
                )
                self._lock.notify_all()
                return []

            if self._pending_reorder is not None:
                held = self._pending_reorder
                self._pending_reorder = None
                self._pending_reorder_due = None
                self._schedule(frame, now + delay)
                self._schedule(held, now + delay + 0.001)
                self.reordered_count += 1
                return []

            self._schedule(frame, now + delay)
            return []

    def counters(self) -> dict[str, int]:
        with self._lock:
            return {
                "injected_dropped_frames": self.dropped_count,
                "injected_delayed_frames": self.delayed_count,
                "injected_reordered_pairs": self.reordered_count,
            }

    def configuration(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "drop_rate": self.drop_rate,
                "max_delay_ms": self.max_delay_ms,
                "reorder_rate": self.reorder_rate,
            }

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            self._lock.notify_all()
        self._worker.join(timeout=1)

    def _schedule(self, frame: CanFrame, due: float) -> None:
        if len(self._queue) >= self.MAX_PENDING_FRAMES:
            self.dropped_count += 1
            if self.dropped_count == 1 or self.dropped_count % 1_000 == 0:
                _LOGGER.warning(
                    "Virtual fault-injection queue is full; discarded %s frame(s).",
                    self.dropped_count,
                )
            return
        self._sequence += 1
        heapq.heappush(self._queue, (due, self._sequence, frame))
        self._lock.notify_all()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stop:
                    self.dropped_count += len(self._queue)
                    if self._pending_reorder is not None:
                        self.dropped_count += 1
                    self._queue.clear()
                    self._pending_reorder = None
                    self._pending_reorder_due = None
                    return
                queue_due = self._queue[0][0] if self._queue else None
                reorder_due = self._pending_reorder_due
                if queue_due is None and reorder_due is None:
                    self._lock.wait()
                    continue
                if reorder_due is not None and (
                    queue_due is None or reorder_due <= queue_due
                ):
                    due = reorder_due
                    from_reorder = True
                else:
                    due = queue_due
                    from_reorder = False
                remaining = due - time.monotonic()
                if remaining > 0:
                    self._lock.wait(remaining)
                    continue
                if from_reorder:
                    frame_to_deliver = self._pending_reorder
                    self._pending_reorder = None
                    self._pending_reorder_due = None
                else:
                    frame_to_deliver = heapq.heappop(self._queue)[2]
            if frame_to_deliver is not None:
                self._deliver_safely(frame_to_deliver)

    def _deliver_safely(self, frame: CanFrame) -> None:
        try:
            self._deliver(frame)
        except Exception:
            _LOGGER.exception(
                "Failed to deliver a frame from the VirtualBus fault injector."
            )
