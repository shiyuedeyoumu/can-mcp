import can
import pytest

from can_mcp.services.bus_service import BusService
from can_mcp.adapters.can.base import CanConfig
from can_mcp.core.models import CanFrame
from can_mcp.services.trace_service import TraceService


class FakeAdapter:
    def __init__(self) -> None:
        self.bus = object()
        self.disconnected = False
        self.sent_frames = []

    def connect(self):
        return self.bus

    def disconnect(self) -> None:
        self.disconnected = True

    def send(self, frame) -> None:
        self.sent_frames.append(frame)


class FakeNotifier:
    def __init__(self, bus, listeners) -> None:
        self.bus = bus
        self.listeners = listeners
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_connect_switches_adapter_and_disconnects_previous_one(monkeypatch) -> None:
    notifiers = []

    def notifier_factory(bus, listeners):
        notifier = FakeNotifier(bus, listeners)
        notifiers.append(notifier)
        return notifier

    monkeypatch.setattr("can_mcp.services.bus_service.can.Notifier", notifier_factory)
    service = BusService()
    pcan_adapter = FakeAdapter()
    vector_adapter = FakeAdapter()

    assert service.connect(
        pcan_adapter, CanConfig(interface="pcan", channel="PCAN_USBBUS1")
    ) == "connected"
    assert service.connect(
        vector_adapter, CanConfig(interface="vector", channel=0)
    ) == "switched"
    assert pcan_adapter.disconnected
    assert notifiers[0].stopped
    assert service.connected
    assert service.disconnect() == "disconnected"
    assert vector_adapter.disconnected
    assert notifiers[1].stopped


def test_send_publishes_timestamped_tx_frame_to_trace(monkeypatch) -> None:
    def notifier_factory(bus, listeners):
        return FakeNotifier(bus, listeners)

    monkeypatch.setattr("can_mcp.services.bus_service.can.Notifier", notifier_factory)
    service = BusService()
    adapter = FakeAdapter()
    trace = TraceService()
    service.connect(
        adapter,
        CanConfig(interface="pcan", channel="PCAN_USBBUS1", fd=True),
    )
    trace.start(service)

    sent_frame = service.send(
        CanFrame(arbitration_id=0x7FE, data=b"\xAA" * 12, is_fd=True, bitrate_switch=True)
    )
    frames = trace.query()

    assert len(frames) == 1
    assert frames[0].is_rx is False
    assert frames[0].timestamp is not None
    assert frames[0].data == b"\xAA" * 12
    assert sent_frame.is_rx is False
    assert sent_frame.timestamp is not None
    assert sent_frame.channel == "PCAN_USBBUS1"
    assert service.get_status(trace.active)["tx_count"] == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\xAA" * 12, "最大 8 字节"),
        (b"\xAA" * 8, "不能发送 CAN FD 帧"),
    ],
)
def test_classic_connection_rejects_fd_frames_before_driver_send(
    monkeypatch,
    payload: bytes,
    message: str,
) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = BusService()
    adapter = FakeAdapter()
    service.connect(
        adapter,
        CanConfig(interface="pcan", channel="PCAN_USBBUS1", fd=False),
    )

    with pytest.raises(ValueError, match=message):
        service.send(CanFrame(arbitration_id=0x123, data=payload, is_fd=True))

    assert adapter.sent_frames == []


def test_listen_only_connection_rejects_all_sends(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = BusService()
    adapter = FakeAdapter()
    config = CanConfig(
        interface="vector",
        channel=1,
        listen_only=True,
    )
    service.connect(adapter, config)

    with pytest.raises(ValueError, match="listen-only mode"):
        service.send(CanFrame(arbitration_id=0x123, data=b"\x01"))

    assert adapter.sent_frames == []
    assert service.get_status(trace_active=False)["listen_only"] is True


def test_bus_status_tracks_connection_parameters_and_rx_count(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = BusService()
    config = CanConfig(
        interface="vector",
        channel=2,
        bitrate=250_000,
        fd=True,
        data_bitrate=2_000_000,
        serial=33156,
    )
    service.connect(FakeAdapter(), config)
    service._on_message(
        can.Message(arbitration_id=0x123, data=b"\x01", is_rx=True)
    )

    assert service.get_status(trace_active=True) == {
        "connected": True,
        "interface": "vector",
        "channel": 2,
        "bitrate": 250_000,
        "fd": True,
        "data_bitrate": 2_000_000,
        "serial": 33156,
        "app_name": "CANalyzer",
        "listen_only": False,
        "trace_active": True,
        "tx_count": 0,
        "rx_count": 1,
        "receive_buffer_dropped_count": 0,
        "receive_buffer_dropped_oldest_count": 0,
        "receive_buffer_dropped_newest_count": 0,
        "error_frame_count": 0,
        "latest_error_frame": None,
        "bus_state": None,
    }


def test_bus_status_preserves_read_only_error_frame_details(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = BusService()
    service.connect(FakeAdapter(), CanConfig(interface="vector", channel=1))
    message = can.Message(
        arbitration_id=0,
        data=b"\x01\x02",
        is_error_frame=True,
        check=False,
    )

    service._on_message(message)
    status = service.get_status(trace_active=False)

    assert status["error_frame_count"] == 1
    assert status["latest_error_frame"]["is_error_frame"] is True
    assert status["latest_error_frame"]["data"] == "0102"
    assert service.receive(timeout=0) is None


def test_transmit_echo_error_frames_do_not_increment_count_or_enter_rx_queue(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = BusService()
    service.connect(FakeAdapter(), CanConfig(interface="pcan", channel="PCAN_USBBUS1"))

    for value in range(100):
        service.send(CanFrame(arbitration_id=0x123, data=value.to_bytes(4, "big")))
        service._on_message(
            can.Message(
                arbitration_id=0,
                data=value.to_bytes(4, "big"),
                is_error_frame=True,
                is_rx=False,
                check=False,
            )
        )

    status = service.get_status(trace_active=False)
    assert status["tx_count"] == 100
    assert status["error_frame_count"] == 0
    assert service.receive(timeout=0) is None


def test_receive_error_flag_and_counter_follow_python_can_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = BusService()
    service.connect(FakeAdapter(), CanConfig(interface="vector", channel=1))
    service._on_message(
        can.Message(
            arbitration_id=0x008,
            data=bytes.fromhex("00190080"),
            is_rx=True,
        )
    )

    received = service.receive(timeout=0)

    assert received is not None
    assert received.is_error_frame is False
    assert service.get_status(trace_active=False)["error_frame_count"] == 0


def test_receive_many_drains_available_queue_and_reports_drop_reasons(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = BusService(receive_buffer_size=3)
    service.connect(FakeAdapter(), CanConfig(interface="pcan", channel="PCAN_USBBUS1"))
    frames = [
        CanFrame(arbitration_id=arbitration_id, data=b"\x01")
        for arbitration_id in range(5)
    ]
    for frame in frames:
        service.inject_received_frame(frame)

    received = service.receive_many(max_frames=2, timeout=0)
    status = service.get_status(trace_active=False)

    assert [frame.arbitration_id for frame in received] == [2, 3]
    assert status["receive_buffer_dropped_count"] == 2
    assert status["receive_buffer_dropped_oldest_count"] == 2
    assert status["receive_buffer_dropped_newest_count"] == 0


def test_physical_sized_receive_queue_holds_sixty_seconds_at_five_khz(
    monkeypatch,
) -> None:
    from can_mcp.services.physical_bus_service import PhysicalBusService

    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    service = PhysicalBusService()
    service.connect(
        "p1",
        FakeAdapter(),
        CanConfig(interface="pcan", channel="PCAN_USBBUS1"),
    )
    bus = service.get_bus("p1")
    total_frames = 5_000 * 60

    for index in range(total_frames):
        bus.inject_received_frame(
            CanFrame(arbitration_id=index & 0x7FF, data=b"\x01")
        )

    status = service.get_status("p1")
    assert status["rx_count"] == total_frames
    assert status["receive_buffer_dropped_count"] == 0
    assert len(bus.receive_many(max_frames=total_frames, timeout=0)) == total_frames
