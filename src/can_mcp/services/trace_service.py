from can_mcp.core.errors import BusNotConnectedError, TraceNotStartedError
from can_mcp.core.filters import FrameFilter
from can_mcp.core.models import CanFrame
from can_mcp.services.bus_service import BusService
from can_mcp.storage.trace_store import TraceStore


class TraceService:
    def __init__(self, store: TraceStore | None = None) -> None:
        self._store = store or TraceStore()
        self._bus: BusService | None = None
        self._active_filter: FrameFilter | None = None
        self._has_started = False

    @property
    def active(self) -> bool:
        return self._bus is not None

    @property
    def frame_count(self) -> int:
        return self._store.count

    @property
    def overwritten_count(self) -> int:
        return self._store.overwritten_count

    @property
    def has_started(self) -> bool:
        return self._has_started

    def start(self, bus: BusService, frame_filter: FrameFilter | None = None) -> str:
        if not bus.connected:
            raise BusNotConnectedError("Connect the CAN bus before starting trace.")
        if self.active:
            return "already_tracing"
        self._store.clear()
        self._active_filter = frame_filter
        self._bus = bus
        self._has_started = True
        bus.subscribe(self._record)
        return "trace_started"

    def stop(self) -> str:
        if self._bus is None:
            return "already_stopped"
        self._bus.unsubscribe(self._record)
        self._bus = None
        self._active_filter = None
        return "trace_stopped"

    def query(
        self,
        frame_filter: FrameFilter | None = None,
        limit: int = 100,
    ) -> list[CanFrame]:
        if not self._has_started:
            raise TraceNotStartedError(
                "Trace has not been started. Call start_trace before querying captured frames."
            )
        return self._store.query(frame_filter, limit)

    def _record(self, frame: CanFrame) -> None:
        if self._active_filter is None or self._active_filter.matches(frame):
            self._store.append(frame)
