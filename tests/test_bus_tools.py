import pytest

from can_mcp.adapters.can.base import CanConfig
from can_mcp.core.errors import BusNotConnectedError
from can_mcp.core.models import CanFrame
from can_mcp.tools.bus_tools import BusTools


class CapturingBusService:
    def __init__(self) -> None:
        self.config: CanConfig | None = None

    def connect(self, adapter, config: CanConfig) -> str:
        self.config = config
        return "connected"


def test_pcan_fd_connection_defaults_data_rate_to_two_megabit() -> None:
    bus_service = CapturingBusService()
    tools = BusTools(bus_service)

    result = tools.connect_can(
        interface="pcan",
        channel="PCAN_USBBUS1",
        bitrate=500_000,
        fd=True,
    )

    assert result == {
        "status": "connected",
        "interface": "pcan",
        "channel": "PCAN_USBBUS1",
        "listen_only": False,
    }
    assert bus_service.config is not None
    assert bus_service.config.data_bitrate == 2_000_000


def test_connect_can_passes_listen_only_setting() -> None:
    bus_service = CapturingBusService()
    tools = BusTools(bus_service)

    result = tools.connect_can(
        interface="vector",
        channel=1,
        listen_only=True,
    )

    assert result["listen_only"] is True
    assert bus_service.config is not None
    assert bus_service.config.listen_only is True


def test_send_can_frame_requires_explicit_physical_io_confirmation() -> None:
    class CapturingBusService:
        def __init__(self) -> None:
            self.sent = []

        def send(self, frame: CanFrame) -> CanFrame:
            self.sent.append(frame)
            return frame

    bus_service = CapturingBusService()
    tools = BusTools(bus_service)

    with pytest.raises(ValueError, match="confirm_physical_io=true"):
        tools.send_can_frame(0x123, "01")

    assert bus_service.sent == []
    result = tools.send_can_frame(0x123, "01", confirm_physical_io=True)

    assert result["status"] == "sent"
    assert len(bus_service.sent) == 1


def test_receive_can_frames_explains_virtual_bus_tooling() -> None:
    from can_mcp.services.physical_bus_service import PhysicalBusService

    tools = BusTools(PhysicalBusService())

    with pytest.raises(BusNotConnectedError, match="receive_virtual_frame"):
        tools.receive_can_frames(handle="virtual-handle")
