from dataclasses import dataclass

from can_mcp.core.models import CanFrame


@dataclass(frozen=True)
class FrameFilter:
    arbitration_id: int | None = None
    arbitration_id_mask: int | None = None
    is_extended_id: bool | None = None
    is_fd: bool | None = None
    is_rx: bool | None = None

    def matches(self, frame: CanFrame) -> bool:
        if self.arbitration_id is not None:
            mask = self.arbitration_id_mask
            if mask is None:
                if frame.arbitration_id != self.arbitration_id:
                    return False
            elif (frame.arbitration_id & mask) != (self.arbitration_id & mask):
                return False
        if self.is_extended_id is not None and frame.is_extended_id != self.is_extended_id:
            return False
        if self.is_fd is not None and frame.is_fd != self.is_fd:
            return False
        if self.is_rx is not None and frame.is_rx != self.is_rx:
            return False
        return True
