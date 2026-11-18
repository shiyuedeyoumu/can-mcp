import asyncio

import can
import pytest

from can_mcp.adapters.can.base import CanConfig
from can_mcp.core.models import CanFrame
from can_mcp.services.physical_bus_service import PhysicalBusService
from can_mcp.tools.physical_can_feature_tools import PhysicalCanFeatureTools


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


def _connected_tools(monkeypatch, interface="pcan"):
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    physical = PhysicalBusService()
    tools = PhysicalCanFeatureTools(physical)
    adapter = FakeAdapter()
    channel = "PCAN_USBBUS1" if interface == "pcan" else 1
    physical.connect(
        "p1",
        adapter,
        CanConfig(interface=interface, channel=channel),
    )
    return physical, tools, adapter


def test_physical_send_tools_require_explicit_confirmation(monkeypatch) -> None:
    _, tools, adapter = _connected_tools(monkeypatch)

    with pytest.raises(ValueError, match="confirm_physical_io=true"):
        tools.start_pcan_cyclic(
            "p1",
            arbitration_id=0x123,
            data="01",
            confirm_physical_io=False,
        )

    assert adapter.sent_frames == []


def test_physical_cyclic_send_is_rate_limited_and_auto_stops(monkeypatch) -> None:
    physical, tools, adapter = _connected_tools(monkeypatch)

    with pytest.raises(ValueError, match="at least 100 ms"):
        tools.start_pcan_cyclic(
            "p1",
            arbitration_id=0x123,
            data="01",
            period_ms=10,
            confirm_physical_io=True,
        )
    result = tools.start_pcan_cyclic(
        "p1",
        arbitration_id=0x123,
        data="01",
        period_ms=1_000,
        duration_seconds=1,
        confirm_physical_io=True,
    )

    assert result["status"] == "started"
    assert len(adapter.sent_frames) == 1
    assert tools.stop_pcan_cyclic("p1", str(result["cyclic_id"]))["status"] == "stopped"

    physical.disconnect("p1")
    assert adapter.disconnected


def test_pcan_generator_uses_same_physical_rate_and_duration_limits(monkeypatch) -> None:
    physical, tools, adapter = _connected_tools(monkeypatch)
    result = tools.start_pcan_frame_generator(
        "p1",
        arbitration_id=0x123,
        mode="counter",
        data="00",
        period_ms=1_000,
        duration_seconds=1,
        confirm_physical_io=True,
    )

    assert result["status"] == "started"
    assert adapter.sent_frames[0].data == b"\x00"
    assert tools.stop_pcan_cyclic("p1", str(result["cyclic_id"]))["status"] == (
        "stopped"
    )
    physical.disconnect("p1")


def test_disconnect_stops_physical_periodic_tasks(monkeypatch) -> None:
    physical, tools, adapter = _connected_tools(monkeypatch)
    result = tools.start_pcan_cyclic(
        "p1",
        arbitration_id=0x123,
        data="01",
        period_ms=1_000,
        duration_seconds=60,
        confirm_physical_io=True,
    )

    assert tools.list_pcan_feature_status()[0]["cyclic_task_count"] == 1
    physical.disconnect("p1")

    assert adapter.disconnected
    assert tools.list_pcan_feature_status() == []


def test_pcan_uds_rejects_non_read_only_services_before_sending(monkeypatch) -> None:
    _, tools, adapter = _connected_tools(monkeypatch)

    with pytest.raises(ValueError, match="only permits read-only"):
        asyncio.run(
            tools.pcan_uds_request(
                "p1",
                request_id=0x7E0,
                response_id=0x7E8,
                data="10 03",
                confirm_physical_io=True,
            )
        )

    assert adapter.sent_frames == []


@pytest.mark.parametrize("interface", ["pcan", "vector"])
def test_physical_uds_request_receives_isotp_response(monkeypatch, interface) -> None:
    physical, tools, adapter = _connected_tools(monkeypatch, interface)
    service = tools._runtime._buses["p1"][1]
    for arbitration_id in range(200):
        service._on_message(
            can.Message(
                arbitration_id=arbitration_id,
                data=b"\x00",
                is_extended_id=False,
                is_rx=True,
            )
        )

    def respond_to_request(frame: CanFrame) -> None:
        assert frame.arbitration_id == 0x7E0
        service._on_message(
            can.Message(
                arbitration_id=0x7E8,
                data=bytes.fromhex("0662f190313233"),
                is_extended_id=False,
                is_rx=True,
            )
        )

    adapter.send = respond_to_request
    result = asyncio.run(
        tools.pcan_uds_request(
            "p1",
            request_id=0x7E0,
            response_id=0x7E8,
            data="22 F1 90",
            confirm_physical_io=True,
        )
    )

    assert result["status"] == "received"
    assert result["response_data"] == "62f190313233"
    assert result["response_id"] == 0x7E8
    assert service.receive(timeout=0) is not None
    physical.disconnect("p1")


def test_pcan_drop_injection_discards_frames_without_error_placeholders(
    monkeypatch,
) -> None:
    physical, tools, _ = _connected_tools(monkeypatch)
    service = tools._runtime._buses["p1"][1]
    tools._runtime.start_trace("p1")
    tools.configure_pcan_receive_faults("p1", drop_rate=1.0, seed=7)

    service._on_message(
        can.Message(
            arbitration_id=0x123,
            data=b"\x01",
            is_extended_id=False,
            is_rx=True,
        )
    )

    assert service.receive(timeout=0) is None
    assert tools._runtime.get_trace("p1") == []
    assert tools._runtime.fault_injection_status("p1")[
        "injected_dropped_frames"
    ] == 1
    physical.disconnect("p1")


def test_pcan_scenario_requires_confirmation_and_reports_pass(monkeypatch) -> None:
    _, tools, adapter = _connected_tools(monkeypatch)

    with pytest.raises(ValueError, match="confirm_physical_io=true"):
        asyncio.run(
            tools.run_pcan_scenario(
                "p1",
                '[{"action":"send","arbitration_id":291,"data":"01"}]',
            )
        )

    result = asyncio.run(
        tools.run_pcan_scenario(
            "p1",
            '[{"action":"send","arbitration_id":291,"data":"01"}]',
            confirm_physical_io=True,
        )
    )

    assert result["status"] == "PASS"
    assert len(adapter.sent_frames) == 1


def test_pcan_handle_is_not_registered_as_virtual_and_cleans_up(monkeypatch) -> None:
    physical, tools, adapter = _connected_tools(monkeypatch)
    assert [item["handle"] for item in tools.list_pcan_feature_status()] == ["p1"]

    physical.disconnect("p1")

    assert tools.list_pcan_feature_status() == []
    assert adapter.disconnected


def test_vector_handle_supports_physical_features(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    physical = PhysicalBusService()
    tools = PhysicalCanFeatureTools(physical)
    physical.connect(
        "v1",
        FakeAdapter(),
        CanConfig(interface="vector", channel=1),
    )

    assert [item["handle"] for item in tools.list_pcan_feature_status()] == ["v1"]
    result = tools.configure_pcan_receive_faults("v1", drop_rate=1.0, seed=7)
    assert result["status"] == "configured"
    task = tools.start_pcan_cyclic(
        "v1",
        arbitration_id=0x123,
        data="01",
        duration_seconds=1,
        confirm_physical_io=True,
    )
    assert task["status"] == "started"
    assert tools.stop_pcan_cyclic("v1", str(task["cyclic_id"]))["status"] == (
        "stopped"
    )
    physical.disconnect("v1")


def test_vector_handle_rejects_physical_transmit_without_confirmation(
    monkeypatch,
) -> None:
    physical, tools, adapter = _connected_tools(monkeypatch, "vector")
    with pytest.raises(ValueError, match="confirm_physical_io=true"):
        tools.start_pcan_cyclic(
            "p1",
            arbitration_id=0x123,
            data="01",
        )
    assert adapter.sent_frames == []
    physical.disconnect("p1")


def test_physical_frame_forwarding_requires_confirmation_and_forwards_one_frame(
    monkeypatch,
) -> None:
    physical, tools, target_adapter = _connected_tools(monkeypatch)
    source_adapter = FakeAdapter()
    physical.connect(
        "p2",
        source_adapter,
        CanConfig(interface="vector", channel=1),
    )
    source = physical.get_bus("p2")
    source._on_message(
        can.Message(
            arbitration_id=0x321,
            data=bytes.fromhex("112233"),
            is_extended_id=False,
            is_rx=True,
        )
    )

    with pytest.raises(ValueError, match="confirm_physical_io=true"):
        asyncio.run(
            tools.forward_physical_frame(
                "p2",
                "p1",
                timeout=0,
            )
        )
    assert target_adapter.sent_frames == []

    result = asyncio.run(
        tools.forward_physical_frame(
            "p2",
            "p1",
            timeout=0,
            confirm_physical_io=True,
        )
    )

    forwarded = result.structuredContent
    assert forwarded["status"] == "forwarded"
    assert forwarded["received_frame"]["data"] == "112233"
    assert target_adapter.sent_frames[0].data == bytes.fromhex("112233")
    physical.disconnect("p2")
    physical.disconnect("p1")


def test_physical_forward_tool_returns_structured_timeout_result(monkeypatch) -> None:
    physical, tools, _ = _connected_tools(monkeypatch)
    physical.connect(
        "p2",
        FakeAdapter(),
        CanConfig(interface="vector", channel=1),
    )
    result = asyncio.run(
        tools.forward_physical_frame(
            "p2",
            "p1",
            timeout=0,
            confirm_physical_io=True,
        )
    )
    assert result.structuredContent == {
        "status": "timeout",
        "source_handle": "p2",
        "target_handle": "p1",
    }
    physical.disconnect("p2")
    physical.disconnect("p1")
