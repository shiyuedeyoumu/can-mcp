from dataclasses import dataclass

import can

from can_mcp.core.interfaces import CanAdapter
from can_mcp.core.models import CanFrame, validate_payload_length


@dataclass(frozen=True)
class CanConfig:
    interface: str
    channel: str | int
    bitrate: int = 500_000
    fd: bool = False
    data_bitrate: int | None = None
    serial: int | None = None
    app_name: str = "CANalyzer"
    listen_only: bool = False


class PythonCanBusAdapter(CanAdapter):
    """Shared python-can bus operations for hardware-specific adapters."""

    interface_name: str

    def __init__(self, config: CanConfig) -> None:
        if not hasattr(type(self), "interface_name"):
            raise TypeError("Hardware adapters must define interface_name.")
        if config.interface.lower() != self.interface_name:
            raise ValueError(
                f"{type(self).__name__} requires interface "
                f"'{self.interface_name}', got '{config.interface}'."
            )
        self._config = config
        self._bus: can.BusABC | None = None

    @property
    def bus(self) -> can.BusABC:
        if self._bus is None:
            raise RuntimeError("CAN bus is not connected")
        return self._bus

    def connect(self) -> can.BusABC:
        if self._bus is None:
            self._bus = self._create_bus()
        return self._bus

    def _create_bus(self) -> can.BusABC:
        kwargs: dict[str, object] = {
            "interface": self.interface_name,
            "channel": self._config.channel,
            "bitrate": self._config.bitrate,
        }
        if self._config.fd:
            kwargs["fd"] = True
            if self._config.data_bitrate is not None:
                kwargs["data_bitrate"] = self._config.data_bitrate
        return can.Bus(**kwargs)

    def disconnect(self) -> None:
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None

    def send(self, frame: CanFrame) -> None:
        validate_payload_length(frame.data, frame.is_fd)
        message = can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            dlc=len(frame.data),
            is_extended_id=frame.is_extended_id,
            is_fd=frame.is_fd,
            bitrate_switch=frame.bitrate_switch,
            check=True,
        )
        self.bus.send(message)


__all__ = ["CanAdapter", "CanConfig", "PythonCanBusAdapter"]
