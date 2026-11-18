import logging
import queue
import threading
import time
from collections.abc import Callable

import can

from can_mcp.adapters.can.base import CanConfig
from can_mcp.core.errors import BusNotConnectedError
from can_mcp.core.interfaces import CanAdapter
from can_mcp.core.models import CanFrame


_LOGGER = logging.getLogger(__name__)


class _FrameListener(can.Listener):
    def __init__(self, callback: Callable[[can.Message], None]) -> None:
        super().__init__()
        self._callback = callback

    def on_message_received(self, message: can.Message) -> None:
        self._callback(message)


class BusService:
    def __init__(self, receive_buffer_size: int = 1_000) -> None:
        if receive_buffer_size <= 0:
            raise ValueError("receive_buffer_size must be greater than zero.")
        self._adapter: CanAdapter | None = None
        self._config: CanConfig | None = None
        self._notifier: can.Notifier | None = None
        self._frames: queue.Queue[CanFrame] = queue.Queue(maxsize=receive_buffer_size)
        self._subscribers: set[Callable[[CanFrame], None]] = set()
        self._lock = threading.RLock()
        self._tx_count = 0
        self._rx_count = 0
        self._receive_buffer_dropped_count = 0
        self._receive_buffer_dropped_oldest_count = 0
        self._receive_buffer_dropped_newest_count = 0
        self._error_frame_count = 0
        self._latest_error_frame: CanFrame | None = None
        self._receive_transform: Callable[[CanFrame], list[CanFrame]] | None = None

    @property
    def connected(self) -> bool:
        return self._notifier is not None

    @property
    def config(self) -> CanConfig | None:
        with self._lock:
            return self._config

    def connect(self, adapter: CanAdapter, config: CanConfig) -> str:
        with self._lock:
            was_connected = self.connected
            if was_connected:
                self._disconnect_locked()
                self._clear_receive_queue()
            try:
                bus = adapter.connect()
                notifier = can.Notifier(bus, [_FrameListener(self._on_message)])
            except Exception:
                adapter.disconnect()
                raise
            self._adapter = adapter
            self._config = config
            self._notifier = notifier
        return "switched" if was_connected else "connected"

    def disconnect(self) -> str:
        with self._lock:
            if self._notifier is None:
                return "already_disconnected"
            self._disconnect_locked()
        return "disconnected"

    def _disconnect_locked(self) -> None:
        notifier = self._notifier
        adapter = self._adapter
        self._notifier = None
        self._adapter = None
        self._config = None
        if notifier is not None:
            notifier.stop()
        if adapter is not None:
            adapter.disconnect()

    def _clear_receive_queue(self) -> None:
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    def send(self, frame: CanFrame) -> CanFrame:
        with self._lock:
            if not self.connected:
                raise BusNotConnectedError("Connect the CAN bus before sending a frame.")
            if self._adapter is None or self._config is None:
                raise BusNotConnectedError("CAN adapter is unavailable.")
            if self._config.listen_only:
                raise ValueError(
                    "CAN connection is in listen-only mode; disconnect and reconnect "
                    "with listen_only=false before sending frames."
                )
            if frame.is_fd and not self._config.fd:
                if len(frame.data) > 8:
                    raise ValueError(
                        "当前连接为经典 CAN（fd=false），最大 8 字节；"
                        "请先使用 fd=true 重新连接后再发送 CAN FD 帧。"
                    )
                raise ValueError(
                    "当前连接为经典 CAN（fd=false），不能发送 CAN FD 帧；"
                    "请先使用 fd=true 重新连接。"
                )
            self._adapter.send(frame)
            tx_frame = CanFrame(
                arbitration_id=frame.arbitration_id,
                data=frame.data,
                timestamp=time.time(),
                is_extended_id=frame.is_extended_id,
                is_fd=frame.is_fd,
                bitrate_switch=frame.bitrate_switch,
                is_rx=False,
                channel=self._config.channel,
            )
            self._tx_count += 1
        self._notify_subscribers(tx_frame)
        return tx_frame

    def get_status(self, trace_active: bool) -> dict[str, object]:
        with self._lock:
            config = self._config
            return {
                "connected": self.connected,
                "interface": config.interface if config else None,
                "channel": config.channel if config else None,
                "bitrate": config.bitrate if config else None,
                "fd": config.fd if config else None,
                "data_bitrate": config.data_bitrate if config else None,
                "serial": config.serial if config else None,
                "app_name": config.app_name if config else None,
                "listen_only": config.listen_only if config else None,
                "trace_active": trace_active,
                "tx_count": self._tx_count,
                "rx_count": self._rx_count,
                "receive_buffer_dropped_count": self._receive_buffer_dropped_count,
                "receive_buffer_dropped_oldest_count": (
                    self._receive_buffer_dropped_oldest_count
                ),
                "receive_buffer_dropped_newest_count": (
                    self._receive_buffer_dropped_newest_count
                ),
                "error_frame_count": self._error_frame_count,
                "latest_error_frame": (
                    self._latest_error_frame.to_dict()
                    if self._latest_error_frame is not None
                    else None
                ),
                "bus_state": self._get_bus_state(),
            }

    def receive(self, timeout: float | None = 1.0) -> CanFrame | None:
        if not self.connected:
            raise BusNotConnectedError("Connect the CAN bus before receiving a frame.")
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def receive_many(
        self,
        max_frames: int = 1_000,
        timeout: float | None = 1.0,
    ) -> list[CanFrame]:
        if isinstance(max_frames, bool) or max_frames <= 0:
            raise ValueError("max_frames must be greater than zero.")
        first_frame = self.receive(timeout)
        if first_frame is None:
            return []
        frames = [first_frame]
        while len(frames) < max_frames:
            try:
                frames.append(self._frames.get_nowait())
            except queue.Empty:
                break
        return frames

    def subscribe(self, callback: Callable[[CanFrame], None]) -> None:
        with self._lock:
            self._subscribers.add(callback)

    def unsubscribe(self, callback: Callable[[CanFrame], None]) -> None:
        with self._lock:
            self._subscribers.discard(callback)

    def set_receive_transform(
        self,
        transform: Callable[[CanFrame], list[CanFrame]] | None,
    ) -> None:
        with self._lock:
            self._receive_transform = transform

    def inject_received_frame(self, frame: CanFrame) -> None:
        self._deliver_frame(frame)

    def _on_message(self, message: can.Message) -> None:
        if message.is_error_frame:
            _LOGGER.debug("CAN driver reported error frame: %r", message)
        frame = CanFrame.from_python_can(message)
        with self._lock:
            transform = self._receive_transform
        if transform is not None:
            for transformed_frame in transform(frame):
                self._deliver_frame(transformed_frame)
            return
        self._deliver_frame(frame)

    def _deliver_frame(self, frame: CanFrame) -> None:
        with self._lock:
            self._rx_count += 1
            if frame.is_error_frame and frame.is_rx is not False:
                self._error_frame_count += 1
                self._latest_error_frame = frame
        if not frame.is_error_frame:
            try:
                self._frames.put_nowait(frame)
            except queue.Full:
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass
                else:
                    with self._lock:
                        self._receive_buffer_dropped_count += 1
                        self._receive_buffer_dropped_oldest_count += 1
                        dropped_count = self._receive_buffer_dropped_oldest_count
                    if dropped_count == 1 or dropped_count % 10_000 == 0:
                        _LOGGER.warning(
                            "Receive buffer full; discarded an old frame "
                            "(oldest_dropped=%d).",
                            dropped_count,
                        )
                try:
                    self._frames.put_nowait(frame)
                except queue.Full:
                    with self._lock:
                        self._receive_buffer_dropped_count += 1
                        self._receive_buffer_dropped_newest_count += 1
                        dropped_count = self._receive_buffer_dropped_newest_count
                    if dropped_count == 1 or dropped_count % 10_000 == 0:
                        _LOGGER.warning(
                            "Receive buffer full; discarded an incoming frame "
                            "(newest_dropped=%d).",
                            dropped_count,
                        )
        self._notify_subscribers(frame)

    def _get_bus_state(self) -> str | None:
        adapter = self._adapter
        if adapter is None:
            return None
        bus = getattr(adapter, "bus", None)
        state = getattr(bus, "state", None)
        if state is None:
            return None
        return getattr(state, "name", str(state))

    def _notify_subscribers(self, frame: CanFrame) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            callback(frame)
