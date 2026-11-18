import threading
from dataclasses import dataclass
from collections.abc import Callable

from can_mcp.adapters.can.base import CanConfig
from can_mcp.core.errors import BusNotConnectedError
from can_mcp.core.interfaces import CanAdapter
from can_mcp.services.bus_service import BusService
from can_mcp.services.trace_service import TraceService
from can_mcp.storage.trace_store import TraceStore


@dataclass
class _PhysicalConnection:
    bus: BusService
    trace: TraceService
    stale: bool = False


class PhysicalBusService:
    DEFAULT_HANDLE = "default"
    RECEIVE_BUFFER_SIZE = 300_000
    TRACE_BUFFER_SIZE = 300_000

    def __init__(self) -> None:
        self._connections: dict[str, _PhysicalConnection] = {}
        self._channel_owners: dict[tuple[str, str], str] = {}
        self._connection_channels: dict[str, tuple[str, str]] = {}
        self._lifecycle_listeners: set[
            Callable[[str, str, BusService | None, TraceService | None], None]
        ] = set()
        self._lock = threading.RLock()

    def add_lifecycle_listener(
        self,
        callback: Callable[
            [str, str, BusService | None, TraceService | None], None
        ],
    ) -> None:
        with self._lock:
            self._lifecycle_listeners.add(callback)

    def connect(
        self,
        handle: str,
        adapter: CanAdapter,
        config: CanConfig,
    ) -> str:
        self._validate_handle(handle)
        channel_key = self._channel_key(config)
        with self._lock:
            owner = self._channel_owners.get(channel_key)
            if owner is not None and owner != handle:
                raise ValueError(
                    f"Physical channel {config.interface}:{config.channel} is "
                    f"already connected by handle '{owner}'."
                )

            connection = self._connections.get(handle)
            if connection is None:
                connection = _PhysicalConnection(
                    BusService(receive_buffer_size=self.RECEIVE_BUFFER_SIZE),
                    TraceService(store=TraceStore(max_size=self.TRACE_BUFFER_SIZE)),
                )
                self._connections[handle] = connection

            old_key = self._connection_channels.get(handle)
            if old_key is not None:
                self._notify_lifecycle("disconnected", handle, None, None)
            try:
                status = connection.bus.connect(adapter, config)
            except Exception:
                if old_key is not None:
                    self._channel_owners.pop(old_key, None)
                    self._connection_channels.pop(handle, None)
                connection.trace.stop()
                connection.stale = True
                raise

            if old_key is not None and old_key != channel_key:
                self._channel_owners.pop(old_key, None)
            self._channel_owners[channel_key] = handle
            self._connection_channels[handle] = channel_key
            connection.stale = False
            self._notify_lifecycle(
                "connected",
                handle,
                connection.bus,
                connection.trace,
            )
            return status

    def disconnect(self, handle: str = DEFAULT_HANDLE) -> str:
        with self._lock:
            connection = self._connections.get(handle)
            if connection is None:
                return "already_disconnected"
            self._notify_lifecycle("disconnected", handle, None, None)
            status = connection.bus.disconnect()
            if status != "already_disconnected":
                for key, owner in tuple(self._channel_owners.items()):
                    if owner == handle:
                        del self._channel_owners[key]
                self._connection_channels.pop(handle, None)
                connection.trace.stop()
                connection.stale = True
            return status

    def get_bus(self, handle: str = DEFAULT_HANDLE) -> BusService:
        with self._lock:
            connection = self._connections.get(handle)
            if connection is None:
                raise BusNotConnectedError(
                    f"Physical CAN handle '{handle}' is not connected."
                )
            return connection.bus

    def get_trace_service(self, handle: str = DEFAULT_HANDLE) -> TraceService:
        with self._lock:
            connection = self._connections.get(handle)
            if connection is None:
                raise BusNotConnectedError(
                    f"Physical CAN handle '{handle}' is not known."
                )
            return connection.trace

    def get_status(
        self,
        handle: str = DEFAULT_HANDLE,
    ) -> dict[str, object]:
        with self._lock:
            connection = self._connections.get(handle)
            if connection is None:
                status = BusService().get_status(trace_active=False)
            else:
                status = connection.bus.get_status(connection.trace.active)
            return status

    def list_connections(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                {"handle": handle, **self.get_status(handle)}
                | {"stale": self._connections[handle].stale}
                for handle in sorted(self._connections)
            ]

    def metrics(self) -> dict[str, int]:
        with self._lock:
            totals = {
                "tx_count": 0,
                "rx_count": 0,
                "error_frame_count": 0,
                "receive_buffer_dropped_count": 0,
                "receive_buffer_dropped_oldest_count": 0,
                "receive_buffer_dropped_newest_count": 0,
                "trace_overwritten_count": 0,
            }
            for connection in self._connections.values():
                status = connection.bus.get_status(connection.trace.active)
                for name in (
                    "tx_count",
                    "rx_count",
                    "error_frame_count",
                    "receive_buffer_dropped_count",
                    "receive_buffer_dropped_oldest_count",
                    "receive_buffer_dropped_newest_count",
                ):
                    totals[name] += int(status[name])
                totals["trace_overwritten_count"] += (
                    connection.trace.overwritten_count
                )
            return totals

    def has_handle(self, handle: str) -> bool:
        with self._lock:
            return handle in self._connections

    def _notify_lifecycle(
        self,
        event: str,
        handle: str,
        bus: BusService | None,
        trace: TraceService | None,
    ) -> None:
        for callback in tuple(self._lifecycle_listeners):
            callback(event, handle, bus, trace)

    def trace_frames(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock:
            frames = [
                {
                    "handle": handle,
                    **frame.to_dict(),
                }
                for handle, connection in self._connections.items()
                if connection.trace.has_started
                for frame in connection.trace.query(limit=limit)
            ]
        frames.sort(key=lambda frame: float(frame["timestamp"] or 0))
        return frames[-limit:]

    def trace_frame_count(self) -> int:
        with self._lock:
            return sum(
                connection.trace.frame_count
                for connection in self._connections.values()
            )

    @staticmethod
    def _channel_key(config: CanConfig) -> tuple[str, str]:
        return config.interface.lower(), str(config.channel).lower()

    @staticmethod
    def _validate_handle(handle: str) -> None:
        if not isinstance(handle, str) or not handle.strip():
            raise ValueError("handle must not be empty.")
        if len(handle) > 64:
            raise ValueError("handle must be 64 characters or fewer.")
