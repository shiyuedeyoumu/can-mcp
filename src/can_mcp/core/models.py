from dataclasses import dataclass
from typing import Any


CAN_FD_PAYLOAD_LENGTHS = frozenset({0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64})


def validate_payload_length(data: bytes, is_fd: bool) -> None:
    payload_length = len(data)
    if is_fd:
        if payload_length not in CAN_FD_PAYLOAD_LENGTHS:
            allowed = ", ".join(str(length) for length in sorted(CAN_FD_PAYLOAD_LENGTHS))
            raise ValueError(
                f"CAN FD payload length must be one of [{allowed}] bytes; "
                f"got {payload_length}."
            )
    elif payload_length > 8:
        raise ValueError(
            f"Classic CAN payload length must be between 0 and 8 bytes; "
            f"got {payload_length}."
        )


@dataclass(frozen=True)
class CanFrame:
    arbitration_id: int
    data: bytes
    timestamp: float | None = None
    is_extended_id: bool = False
    is_fd: bool = False
    bitrate_switch: bool = False
    is_rx: bool | None = None
    channel: str | int | None = None
    is_error_frame: bool = False

    @classmethod
    def from_python_can(cls, message: Any) -> "CanFrame":
        return cls(
            arbitration_id=message.arbitration_id,
            data=bytes(message.data),
            timestamp=message.timestamp,
            is_extended_id=message.is_extended_id,
            is_fd=message.is_fd,
            bitrate_switch=message.bitrate_switch,
            is_rx=message.is_rx,
            channel=message.channel,
            is_error_frame=message.is_error_frame,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "arbitration_id": self.arbitration_id,
            "data": self.data.hex(),
            "timestamp": self.timestamp,
            "is_extended_id": self.is_extended_id,
            "is_fd": self.is_fd,
            "bitrate_switch": self.bitrate_switch,
            "is_rx": self.is_rx,
            "channel": self.channel,
            "is_error_frame": self.is_error_frame,
        }
