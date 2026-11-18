import pytest

from can_mcp.adapters.can.base import CanConfig
from can_mcp.core.models import CanFrame
from can_mcp.services.physical_bus_service import PhysicalBusService
from can_mcp.tools.status_tools import StatusTools


class FakeAdapter:
    def __init__(self) -> None:
        self.disconnected = False
        self.sent_frames: list[CanFrame] = []

    def connect(self):
        return object()

    def disconnect(self) -> None:
        self.disconnected = True

    def send(self, frame: CanFrame) -> None:
        self.sent_frames.append(frame)


class FakeNotifier:
    def __init__(self, bus, listeners) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _connect(
    service: PhysicalBusService,
    handle: str,
    channel: str | int,
) -> FakeAdapter:
    adapter = FakeAdapter()
    service.connect(
        handle,
        adapter,
        CanConfig(interface="pcan", channel=channel),
    )
    return adapter


def test_distinct_physical_channels_can_be_connected_concurrently(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = PhysicalBusService()
    left = _connect(service, "left", "PCAN_USBBUS1")
    right = _connect(service, "right", "PCAN_USBBUS2")
    service.get_trace_service("left").start(service.get_bus("left"))
    service.get_trace_service("right").start(service.get_bus("right"))

    service.get_bus("left").send(CanFrame(arbitration_id=0x123, data=b"\x01"))
    service.get_bus("right").send(CanFrame(arbitration_id=0x456, data=b"\x02"))

    assert len(left.sent_frames) == 1
    assert len(right.sent_frames) == 1
    assert service.get_status("left")["tx_count"] == 1
    assert service.get_status("right")["tx_count"] == 1
    assert {
        (frame["handle"], frame["arbitration_id"])
        for frame in service.trace_frames()
    } == {("left", 0x123), ("right", 0x456)}
    assert {item["handle"] for item in service.list_connections()} == {
        "left",
        "right",
    }


def test_physical_channel_cannot_be_claimed_by_two_handles(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = PhysicalBusService()
    _connect(service, "left", "PCAN_USBBUS1")

    with pytest.raises(ValueError, match="already connected by handle 'left'"):
        service.connect(
            "right",
            FakeAdapter(),
            CanConfig(interface="pcan", channel="pcan_usbbus1"),
        )


def test_disconnect_releases_channel_and_preserves_trace(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = PhysicalBusService()
    adapter = _connect(service, "left", "PCAN_USBBUS1")
    bus = service.get_bus("left")
    trace = service.get_trace_service("left")
    trace.start(bus)
    bus.send(CanFrame(arbitration_id=0x123, data=b"\x01"))

    assert service.disconnect("left") == "disconnected"
    assert adapter.disconnected
    assert not trace.active
    assert trace.query()[0].arbitration_id == 0x123
    assert service.list_connections()[0]["stale"] is True

    _connect(service, "right", "PCAN_USBBUS1")
    assert service.get_status("right")["connected"] is True


def test_decode_error_frame_accepts_physical_handle(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = PhysicalBusService()
    _connect(service, "p1", "PCAN_USBBUS1")
    service.get_bus("p1").inject_received_frame(
        CanFrame(
            arbitration_id=0,
            data=b"\x01",
            is_error_frame=True,
        )
    )

    result = StatusTools(service, None).decode_error_frame(handle="p1")

    assert result["error_frame_count"] == 1
    assert result["error_frame"]["is_error_frame"] is True
