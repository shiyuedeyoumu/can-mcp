import asyncio
import queue
import threading
import time

import isotp
import pytest

from can_mcp.core.filters import FrameFilter
from can_mcp.core.models import CanFrame
from can_mcp.services.virtual_bus_service import VirtualBusService


def _create_isotp_endpoint(service, txid: int, rxid: int, is_fd: bool = False):
    frames: queue.Queue[isotp.CanMessage] = queue.Queue()

    def on_frame(frame: CanFrame) -> None:
        if frame.is_rx and frame.arbitration_id == rxid:
            frames.put(
                isotp.CanMessage(
                    arbitration_id=frame.arbitration_id,
                    dlc=len(frame.data),
                    data=frame.data,
                    extended_id=frame.is_extended_id,
                    is_fd=frame.is_fd,
                    bitrate_switch=frame.bitrate_switch,
                )
            )

    def receive_frame(timeout: float) -> isotp.CanMessage | None:
        try:
            return frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def send_frame(message: isotp.CanMessage) -> None:
        service.send(
            CanFrame(
                arbitration_id=message.arbitration_id,
                data=bytes(message.data),
                is_extended_id=message.is_extended_id,
                is_fd=message.is_fd,
                bitrate_switch=message.bitrate_switch,
            )
        )

    transport = isotp.TransportLayer(
        rxfn=receive_frame,
        txfn=send_frame,
        address=isotp.Address(
            addressing_mode=isotp.AddressingMode.Normal_11bits,
            txid=txid,
            rxid=rxid,
        ),
        params={
            "blocking_send": True,
            "max_frame_size": 4095,
            "can_fd": is_fd,
            "tx_data_length": 64 if is_fd else 8,
            "bitrate_switch": is_fd,
        },
    )
    return transport, on_frame


def test_virtual_adapter_opens_only_python_can_virtual_interface(monkeypatch) -> None:
    from can_mcp.adapters.can.base import CanConfig
    from can_mcp.adapters.can.virtual import VirtualBusAdapter

    calls = []
    bus = object()

    def fake_bus(**kwargs):
        calls.append(kwargs)
        return bus

    monkeypatch.setattr("can_mcp.adapters.can.virtual.can.Bus", fake_bus)
    adapter = VirtualBusAdapter(
        CanConfig(interface="virtual", channel="simulated-network")
    )

    assert adapter.connect() is bus
    assert calls == [
        {
            "interface": "virtual",
            "channel": "simulated-network",
            "receive_own_messages": False,
            "rx_queue_size": 1_000,
        }
    ]


def test_virtual_handles_exchange_frames_on_shared_channel() -> None:
    buses = VirtualBusService()
    buses.connect("left", "test-shared")
    buses.connect("right", "test-shared")

    sent = buses.send(
        "left",
        CanFrame(arbitration_id=0x123, data=b"\x01\x02"),
    )
    received = buses.receive("right", timeout=0.2)

    assert sent.is_rx is False
    assert received is not None
    assert received.is_rx is True
    assert received.arbitration_id == 0x123
    assert received.data == b"\x01\x02"

    buses.disconnect("left")
    buses.disconnect("right")


def test_virtual_forwarding_can_discard_stale_queued_frames() -> None:
    buses = VirtualBusService()
    buses.connect("sender", "test-drain-source")
    buses.connect("gateway", "test-drain-source")
    buses.connect("target", "test-drain-target")
    buses.connect("observer", "test-drain-target")
    buses.send("sender", CanFrame(arbitration_id=0x612, data=b"\x99\x99"))

    def send_fresh_frame() -> None:
        time.sleep(0.03)
        buses.send("sender", CanFrame(arbitration_id=0x613, data=b"\xAA\xAA"))

    sender = threading.Thread(target=send_fresh_frame)
    sender.start()
    try:
        result = buses.forward(
            "gateway",
            "target",
            timeout=0.3,
            drain=True,
        )
        sender.join(timeout=0.3)
        assert result["status"] == "forwarded"
        assert result["drained_frames"] == 1
        assert result["received_frame"]["arbitration_id"] == 0x613
        received = buses.receive("observer", timeout=0.2)
        assert received is not None
        assert received.arbitration_id == 0x613
    finally:
        for handle in ("sender", "gateway", "target", "observer"):
            buses.disconnect(handle)


def test_virtual_handles_exchange_can_fd_frame() -> None:
    buses = VirtualBusService()
    buses.connect("fd-left", "test-fd-channel", fd=True)
    buses.connect("fd-right", "test-fd-channel", fd=True)
    buses.send(
        "fd-left",
        CanFrame(
            arbitration_id=0x1ABCDE,
            data=b"\xAA" * 12,
            is_extended_id=True,
            is_fd=True,
            bitrate_switch=True,
        ),
    )

    received = buses.receive("fd-right", timeout=0.2)

    assert received is not None
    assert received.is_fd
    assert received.bitrate_switch
    assert received.is_extended_id
    assert received.data == b"\xAA" * 12

    buses.disconnect("fd-left")
    buses.disconnect("fd-right")


def test_virtual_handles_on_different_channels_are_isolated() -> None:
    buses = VirtualBusService()
    buses.connect("left", "test-channel-a")
    buses.connect("right", "test-channel-b")
    buses.send("left", CanFrame(arbitration_id=0x123, data=b"\x01"))

    assert buses.receive("right", timeout=0.01) is None

    buses.disconnect("left")
    buses.disconnect("right")


def test_virtual_fault_injection_can_drop_and_reorder_frames() -> None:
    buses = VirtualBusService()
    buses.connect("sender", "test-fault-drop")
    buses.connect("receiver", "test-fault-drop")
    buses.start_trace("receiver")
    buses.configure_fault_injection("receiver", drop_rate=1.0, seed=7)
    buses.send("sender", CanFrame(arbitration_id=0x101, data=b"\x01"))
    assert buses.receive("receiver", timeout=0.05) is None
    assert buses.get_trace("receiver") == []
    assert buses.fault_injection_status("receiver")["injected_dropped_frames"] == 1
    buses.disconnect("sender")
    buses.disconnect("receiver")
    assert buses.metrics()["injected_dropped_frames"] == 1

    buses.connect("sender", "test-fault-delay")
    buses.connect("receiver", "test-fault-delay")
    buses.configure_fault_injection(
        "receiver",
        max_delay_ms=100,
        seed=7,
    )
    delay_started = time.monotonic()
    buses.send("sender", CanFrame(arbitration_id=0x103, data=b"\x03"))
    delayed = buses.receive("receiver", timeout=0.2)
    assert delayed is not None and delayed.arbitration_id == 0x103
    assert time.monotonic() - delay_started >= 0.01
    assert buses.fault_injection_status("receiver")["injected_delayed_frames"] == 1
    buses.disconnect("sender")
    buses.disconnect("receiver")
    assert buses.metrics()["injected_delayed_frames"] == 1

    buses.connect("sender", "test-fault-reorder")
    buses.connect("receiver", "test-fault-reorder")
    buses.configure_fault_injection(
        "receiver",
        max_delay_ms=100,
        reorder_rate=1.0,
        seed=7,
    )
    buses.send("sender", CanFrame(arbitration_id=0x101, data=b"\x01"))
    time.sleep(0.002)
    buses.send("sender", CanFrame(arbitration_id=0x102, data=b"\x02"))
    first = buses.receive("receiver", timeout=0.2)
    second = buses.receive("receiver", timeout=0.2)

    assert first is not None and second is not None
    assert [first.arbitration_id, second.arbitration_id] == [0x102, 0x101]
    assert buses.fault_injection_status("receiver")["injected_reordered_pairs"] == 1
    buses.disconnect("sender")
    buses.disconnect("receiver")
    assert buses.metrics()["injected_reordered_pairs"] == 1


def test_virtual_frame_generator_supports_counter_ramp_random_and_template() -> None:
    buses = VirtualBusService()
    buses.connect("generator", "test-generators")
    buses.connect("observer", "test-generators")

    modes = [
        ("counter", b"\x00", {"byte_index": 0}, b"\x00"),
        (
            "ramp",
            b"\x00\x00",
            {
                "value_byte_offset": 0,
                "value_byte_length": 2,
                "start_value": 0x1234,
            },
            b"\x12\x34",
        ),
        ("random", b"\x00\x00", {"seed": 123}, None),
        (
            "template",
            b"",
            {"template": "AA {counter} BB"},
            b"\xaa\x00\xbb",
        ),
    ]
    try:
        for index, (mode, data, options, expected) in enumerate(modes):
            result = buses.start_frame_generator(
                "generator",
                0x200 + index,
                data,
                1_000,
                mode,
                **options,
            )
            frame = buses.receive("observer", timeout=0.2)
            assert frame is not None
            assert frame.arbitration_id == 0x200 + index
            if expected is not None:
                assert frame.data == expected
            else:
                assert len(frame.data) == 2
            assert buses.stop_cyclic(result["cyclic_id"])["status"] == "stopped"
    finally:
        buses.disconnect("generator")
        buses.disconnect("observer")


def test_virtual_forwarding_moves_one_frame_between_channels() -> None:
    buses = VirtualBusService()
    buses.connect("sender", "test-ingress")
    buses.connect("gateway_in", "test-ingress")
    buses.connect("gateway_out", "test-egress")
    buses.connect("observer", "test-egress")
    buses.send("sender", CanFrame(arbitration_id=0x456, data=b"\xAA"))

    result = buses.forward("gateway_in", "gateway_out", timeout=0.2)
    observed = buses.receive("observer", timeout=0.2)

    assert result["status"] == "forwarded"
    assert observed is not None
    assert observed.arbitration_id == 0x456
    assert observed.data == b"\xAA"
    assert buses.receive("gateway_out", timeout=0.01) is None

    for handle in ("sender", "gateway_in", "gateway_out", "observer"):
        buses.disconnect(handle)


def test_virtual_forwarding_rejects_same_handle_and_reports_timeout() -> None:
    buses = VirtualBusService()
    buses.connect("left", "test-timeout")
    buses.connect("right", "test-timeout")

    with pytest.raises(ValueError, match="must be different"):
        buses.forward("left", "left", timeout=0)
    assert buses.forward("left", "right", timeout=0)["status"] == "timeout"

    buses.disconnect("left")
    buses.disconnect("right")


def test_virtual_forwarding_requires_peer_handle_to_receive_source_frame() -> None:
    buses = VirtualBusService()
    buses.connect("source", "test-source-self-send")
    buses.connect("sender", "test-source-self-send")
    buses.connect("target", "test-source-egress")
    buses.connect("observer", "test-source-egress")

    buses.send("source", CanFrame(arbitration_id=0x620, data=b"\xAA"))
    assert buses.forward("source", "target", timeout=0.02)["status"] == "timeout"
    assert buses.receive("observer", timeout=0) is None

    buses.send("sender", CanFrame(arbitration_id=0x621, data=b"\xBB"))
    result = buses.forward("source", "target", timeout=0.2)
    received = buses.receive("observer", timeout=0.2)

    assert result["status"] == "forwarded"
    assert result["received_frame"]["arbitration_id"] == 0x621
    assert received is not None
    assert received.arbitration_id == 0x621

    for handle in ("source", "sender", "target", "observer"):
        buses.disconnect(handle)


def test_virtual_trace_filters_restarts_and_exports(tmp_path) -> None:
    buses = VirtualBusService()
    buses.connect("trace", "test-virtual-trace")
    buses.connect("sender", "test-virtual-trace")
    listed = {item["handle"]: item for item in buses.list_buses()}
    assert listed["trace"]["trace_active"] is False
    assert listed["trace"]["trace_frames"] == 0

    assert buses.start_trace(
        "trace",
        FrameFilter(arbitration_id=0x120, arbitration_id_mask=0x7F0),
    )["status"] == "trace_started"
    assert next(
        item["trace_active"]
        for item in buses.list_buses()
        if item["handle"] == "trace"
    ) is True
    buses.send("sender", CanFrame(arbitration_id=0x123, data=b"\x01"))
    buses.send("sender", CanFrame(arbitration_id=0x12A, data=b"\x03"))
    buses.send("sender", CanFrame(arbitration_id=0x234, data=b"\x02"))
    deadline = time.monotonic() + 1
    first = buses.get_trace("trace", limit=10)
    while len(first) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
        first = buses.get_trace("trace", limit=10)
    assert len(first) == 2
    assert buses.get_trace("trace", limit=1)[0].arbitration_id == 0x12A
    assert buses.stop_trace("trace")["status"] == "trace_stopped"
    exported = buses.export_trace("trace", str(tmp_path / "virtual.csv"))
    assert exported["frame_count"] == 2

    assert buses.start_trace("trace")["status"] == "trace_started"
    assert buses.get_trace("trace", limit=10) == []
    buses.stop_trace("trace")
    buses.disconnect("trace")
    buses.disconnect("sender")


def test_virtual_expect_frame_matches_data_mask_and_times_out() -> None:
    buses = VirtualBusService()
    buses.connect("receiver", "test-expect")
    buses.connect("sender", "test-expect")
    buses.send("sender", CanFrame(arbitration_id=0x123, data=b"\x62\xF1\x90"))

    matched = buses.expect_frame(
        "receiver",
        arbitration_id=0x123,
        data=b"\x62\x00",
        data_mask=b"\xFF\x00",
        timeout=0.2,
    )
    assert matched.data == b"\x62\xF1\x90"
    with pytest.raises(TimeoutError, match="Expected CAN frame"):
        buses.expect_frame(
            "receiver",
            arbitration_id=0x123,
            data=b"\x7F",
            timeout=0.05,
        )
    buses.disconnect("receiver")
    buses.disconnect("sender")


def test_virtual_cyclic_sends_until_stopped() -> None:
    buses = VirtualBusService()
    buses.connect("cyclic-sender", "test-cyclic")
    buses.connect("cyclic-receiver", "test-cyclic")

    started = buses.start_cyclic(
        "cyclic-sender",
        CanFrame(arbitration_id=0x3E0, data=b"\x3E\x80"),
        period_ms=20,
    )
    try:
        first = buses.receive("cyclic-receiver", timeout=0.2)
        second = buses.receive("cyclic-receiver", timeout=0.2)
        assert first is not None
        assert second is not None
        assert first.arbitration_id == second.arbitration_id == 0x3E0
        assert first.data == second.data == b"\x3E\x80"
        assert next(
            item["cyclic_task_count"]
            for item in buses.list_buses()
            if item["handle"] == "cyclic-sender"
        ) == 1
        assert buses.stop_cyclic(started["cyclic_id"])["status"] == "stopped"
        assert next(
            item["cyclic_task_count"]
            for item in buses.list_buses()
            if item["handle"] == "cyclic-sender"
        ) == 0
        assert buses.receive("cyclic-receiver", timeout=0.05) is None
    finally:
        buses.stop_cyclic(started["cyclic_id"])
        buses.disconnect("cyclic-sender")
        buses.disconnect("cyclic-receiver")


def test_disconnecting_virtual_handle_stops_its_cyclic_tasks() -> None:
    buses = VirtualBusService()
    buses.connect("cyclic-sender", "test-cyclic-disconnect")
    started = buses.start_cyclic(
        "cyclic-sender",
        CanFrame(arbitration_id=0x123, data=b"\x01"),
        period_ms=20,
    )

    assert buses.disconnect("cyclic-sender")["status"] == "disconnected"
    assert buses.stop_cyclic(started["cyclic_id"])["status"] == "not_found"


@pytest.mark.parametrize("period_ms", [0, 9, 60_001, True])
def test_virtual_cyclic_rejects_invalid_period(period_ms) -> None:
    buses = VirtualBusService()
    buses.connect("cyclic-sender", "test-cyclic-period")

    with pytest.raises(ValueError, match="period_ms"):
        buses.start_cyclic(
            "cyclic-sender",
            CanFrame(arbitration_id=0x123, data=b"\x01"),
            period_ms=period_ms,
        )

    buses.disconnect("cyclic-sender")


def test_virtual_handles_are_unique_and_bounded() -> None:
    buses = VirtualBusService()
    buses.connect("left", "test-unique")

    with pytest.raises(ValueError, match="already exists"):
        buses.connect("left", "test-unique-2")
    with pytest.raises(KeyError, match="not connected"):
        buses.send("missing", CanFrame(arbitration_id=1, data=b""))

    buses.disconnect("left")


@pytest.mark.parametrize(
    ("is_fd", "request_size", "response_size"),
    [(False, 40, 40), (True, 80, 100)],
)
def test_virtual_uds_request_segments_and_reassembles_iso_tp_payloads(
    is_fd: bool,
    request_size: int,
    response_size: int,
) -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-uds", fd=is_fd)
    buses.connect("ecu", "test-uds", fd=is_fd)
    ecu_service = buses._get_service("ecu")
    ecu_transport, on_frame = _create_isotp_endpoint(
        ecu_service,
        txid=0x7E8,
        rxid=0x7E0,
        is_fd=is_fd,
    )
    request = bytes(index % 256 for index in range(request_size))
    response = bytes(index % 256 for index in range(80, 80 + response_size))
    responder_errors: list[Exception] = []

    def respond() -> None:
        try:
            received = ecu_transport.recv(block=True, timeout=3)
            assert received == request
            ecu_transport.send(response, send_timeout=3)
        except Exception as exc:
            responder_errors.append(exc)

    ecu_service.subscribe(on_frame)
    ecu_transport.start()
    responder = threading.Thread(target=respond)
    responder.start()
    try:
        result = buses.uds_request(
            handle="tester",
            request_id=0x7E0,
            response_id=0x7E8,
            payload=request,
            timeout=3,
            is_fd=is_fd,
            bitrate_switch=is_fd,
        )
        responder.join(timeout=3)
        assert not responder.is_alive()
        assert not responder_errors
        assert result["request_data"] == request.hex()
        assert result["response_data"] == response.hex()
    finally:
        ecu_transport.stop()
        ecu_service.unsubscribe(on_frame)
        buses.disconnect("tester")
        buses.disconnect("ecu")


def test_virtual_uds_request_times_out_without_simulated_ecu_response() -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-uds-timeout")

    with pytest.raises(TimeoutError, match="No ISO-TP response"):
        buses.uds_request(
            handle="tester",
            request_id=0x7E0,
            response_id=0x7E8,
            payload=b"\x22\xF1\x90",
            timeout=0.05,
        )

    buses.disconnect("tester")


def test_virtual_uds_timeout_reports_actual_p2_wait_window() -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-uds-p2-timeout")
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="P2 response window") as error:
        buses.uds_request(
            handle="tester",
            request_id=0x7E0,
            response_id=0x7E8,
            payload=b"\x22\xF1\x90",
            timeout=2.0,
            p2_ms=100,
        )

    elapsed = time.monotonic() - started
    assert "configured window 0.100 seconds" in str(error.value)
    assert "overall timeout 2.000 seconds" in str(error.value)
    assert elapsed < 0.5
    buses.disconnect("tester")


@pytest.mark.parametrize(
    ("sent_response_id", "expected_response"),
    [(0x7E8, "62f190313233"), (0x7E9, None)],
)
def test_virtual_uds_request_uses_total_timeout_and_filters_response_id(
    sent_response_id: int,
    expected_response: str | None,
) -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-uds-response-filter")
    buses.connect("ecu", "test-uds-response-filter")
    ecu = buses._get_service("ecu")
    responder_errors: list[Exception] = []

    def respond() -> None:
        try:
            request = ecu.receive(timeout=1)
            assert request is not None
            assert request.arbitration_id == 0x7E0
            assert request.data == b"\x03\x22\xF1\x90"
            buses.send(
                "ecu",
                CanFrame(
                    arbitration_id=sent_response_id,
                    data=b"\x06\x62\xF1\x90\x31\x32\x33",
                ),
            )
        except Exception as exc:
            responder_errors.append(exc)

    responder = threading.Thread(target=respond)
    responder.start()
    started_at = time.monotonic()
    try:
        if expected_response is None:
            with pytest.raises(TimeoutError, match="No ISO-TP response"):
                buses.uds_request(
                    handle="tester",
                    request_id=0x7E0,
                    response_id=0x7E8,
                    payload=b"\x22\xF1\x90",
                    timeout=0.3,
                )
        else:
            result = buses.uds_request(
                handle="tester",
                request_id=0x7E0,
                response_id=0x7E8,
                payload=b"\x22\xF1\x90",
                timeout=1,
            )
            assert result["response_data"] == expected_response
        assert time.monotonic() - started_at < 1.5
        responder.join(timeout=1)
        assert not responder.is_alive()
        assert not responder_errors
    finally:
        buses.disconnect("tester")
        buses.disconnect("ecu")


def test_virtual_uds_request_handles_pending_then_final_response() -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-uds-pending")
    buses.connect("ecu", "test-uds-pending")

    def respond() -> None:
        ecu = buses._get_service("ecu")
        request = ecu.receive(timeout=1)
        assert request is not None
        buses.send(
            "ecu",
            CanFrame(0x7E8, b"\x03\x7F\x22\x78"),
        )
        time.sleep(0.05)
        buses.send(
            "ecu",
            CanFrame(0x7E8, b"\x06\x62\xF1\x90\x31\x32\x33"),
        )

    responder = threading.Thread(target=respond)
    responder.start()
    try:
        result = buses.uds_request(
            "tester",
            0x7E0,
            0x7E8,
            b"\x22\xF1\x90",
            timeout=1,
            p2_ms=100,
            p2_star_ms=400,
            s3_ms=5_000,
        )
        responder.join(timeout=1)
        assert not responder.is_alive()
        assert result["response_data"] == "62f190313233"
        assert result["p2_ms"] == 100
        assert result["p2_star_ms"] == 400
        assert result["s3_ms"] == 5_000
    finally:
        buses.disconnect("tester")
        buses.disconnect("ecu")


def test_virtual_functional_uds_collects_responses_and_decodes_nrc() -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-uds-functional")
    buses.connect("ecu1", "test-uds-functional")
    buses.connect("ecu2", "test-uds-functional")
    errors: list[Exception] = []

    def respond(handle: str, arbitration_id: int, response: bytes) -> None:
        try:
            request = buses.receive(handle, timeout=1)
            assert request is not None
            assert request.arbitration_id == 0x7DF
            buses.send(handle, CanFrame(arbitration_id, response))
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(
        target=respond,
        args=("ecu1", 0x7E8, b"\x03\x7F\x22\x31"),
    )
    second = threading.Thread(
        target=respond,
        args=("ecu2", 0x7E9, b"\x03\x7F\x22\x31"),
    )
    first.start()
    second.start()
    try:
        result = buses.uds_functional_request(
            "tester",
            0x7DF,
            b"\x22\xF1\x90",
            p2_ms=200,
        )
        first.join(timeout=1)
        second.join(timeout=1)
        assert not first.is_alive()
        assert not second.is_alive()
        assert not errors
        assert result["response_count"] == 2
        assert result["responses"]["2024"]["negative_response"]["name"] == "requestOutOfRange"
        assert result["responses"]["2025"]["negative_response"]["nrc"] == 0x31
    finally:
        for handle in ("tester", "ecu1", "ecu2"):
            buses.disconnect(handle)


def test_virtual_uds_suppress_positive_response_sets_subfunction_bit() -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-uds-suppress")
    buses.connect("ecu", "test-uds-suppress")

    def receive_request() -> CanFrame | None:
        return buses.receive("ecu", timeout=1)

    result_holder: list[dict[str, object]] = []

    def send_suppressed() -> None:
        result_holder.append(
            buses.uds_request(
                "tester",
                0x7E0,
                0x7E8,
                b"\x10\x02",
                timeout=0.5,
                suppress_positive_response=True,
            )
        )

    sender = threading.Thread(target=send_suppressed)
    sender.start()
    request = receive_request()
    sender.join(timeout=1)

    assert request is not None
    assert request.data == b"\x02\x10\x82"
    assert result_holder[0]["status"] == "sent_no_response_expected"
    buses.disconnect("tester")
    buses.disconnect("ecu")


def test_virtual_scenario_runs_send_wait_assert_and_returns_report() -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-scenario")
    buses.connect("ecu", "test-scenario")

    def ecu_response() -> None:
        frame = buses.receive("ecu", timeout=1)
        assert frame is not None
        buses.send("ecu", CanFrame(0x7E8, b"\x62\xF1\x90"))

    responder = threading.Thread(target=ecu_response)
    responder.start()
    report = buses.run_scenario(
        "tester",
        [
            {"action": "send", "arbitration_id": 0x7E0, "data": "22 F1 90"},
            {
                "action": "wait",
                "arbitration_id": 0x7E8,
                "data": "62",
                "timeout": 1,
            },
            {"action": "assert", "field": "data", "equals": "62 F1 90"},
        ],
    )
    responder.join(timeout=1)

    assert not responder.is_alive()
    assert report["status"] == "PASS"
    assert report["passed_steps"] == 3
    buses.disconnect("tester")
    buses.disconnect("ecu")


def test_virtual_scenario_reports_assertion_failure_and_prevalidates_steps() -> None:
    buses = VirtualBusService()
    buses.connect("scenario", "test-scenario-failure")
    buses.connect("sender", "test-scenario-failure")
    buses.send("sender", CanFrame(0x123, b"\x00"))
    report = buses.run_scenario(
        "scenario",
        [
            {"action": "wait", "arbitration_id": 0x123, "timeout": 0.2},
            {"action": "assert", "field": "data", "equals": "FF"},
        ],
    )
    assert report["status"] == "FAIL"
    assert report["steps"][-1]["status"] == "FAIL"

    with pytest.raises(ValueError, match="unsupported action"):
        buses.run_scenario(
            "scenario",
            [
                {"action": "send", "arbitration_id": 0x123, "data": "01"},
                {"action": "exec", "command": "not supported"},
            ],
        )
    assert buses.receive("sender", timeout=0) is None
    buses.disconnect("scenario")
    buses.disconnect("sender")

def test_virtual_uds_tool_wait_does_not_block_receive_tool() -> None:
    from can_mcp.tools.virtual_bus_tools import VirtualBusTools

    buses = VirtualBusService()
    buses.connect("tester", "test-uds-concurrent")
    buses.connect("observer", "test-uds-concurrent")
    tools = VirtualBusTools(buses)

    async def exercise_tools() -> None:
        uds_task = asyncio.create_task(
            tools.uds_request(
                handle="tester",
                request_id=0x7E0,
                response_id=0x7E8,
                data="22 F1 90",
                timeout=0.5,
            )
        )
        try:
            result = await asyncio.wait_for(
                tools.receive_virtual_frame("observer", timeout=0.3),
                timeout=0.35,
            )
            frame = result.structuredContent["result"]
            assert frame is not None
            assert frame["arbitration_id"] == 0x7E0
            assert frame["data"] == "0322f190"
        finally:
            with pytest.raises(TimeoutError, match="No ISO-TP response"):
                await uds_task

    try:
        asyncio.run(exercise_tools())
    finally:
        buses.disconnect("tester")
        buses.disconnect("observer")


def test_forward_tool_wait_does_not_block_virtual_send_tool() -> None:
    from can_mcp.tools.virtual_bus_tools import VirtualBusTools

    buses = VirtualBusService()
    buses.connect("source", "test-forward-concurrent")
    buses.connect("gateway", "test-forward-concurrent")
    buses.connect("target", "test-forward-egress")
    buses.connect("observer", "test-forward-egress")
    tools = VirtualBusTools(buses)

    async def exercise_tools() -> None:
        forwarding = asyncio.create_task(
            tools.forward_virtual_frame(
                "gateway",
                "target",
                timeout=0.5,
            )
        )
        await asyncio.sleep(0.02)
        sent = tools.send_virtual_frame(
            "source",
            arbitration_id=0x610,
            data="AA",
        )
        result = await asyncio.wait_for(forwarding, timeout=0.3)
        received = await asyncio.wait_for(
            tools.receive_virtual_frame("observer", timeout=0.2),
            timeout=0.3,
        )
        assert sent["status"] == "sent"
        assert result["status"] == "forwarded"
        assert received.structuredContent["result"]["arbitration_id"] == 0x610

    try:
        asyncio.run(exercise_tools())
    finally:
        for handle in ("source", "gateway", "target", "observer"):
            buses.disconnect(handle)


def test_virtual_uds_request_rejects_fd_when_handle_is_classic() -> None:
    buses = VirtualBusService()
    buses.connect("tester", "test-uds-classic")

    with pytest.raises(ValueError, match="fd=true"):
        buses.uds_request(
            handle="tester",
            request_id=0x7E0,
            response_id=0x7E8,
            payload=b"\x22\xF1\x90",
            timeout=0.1,
            is_fd=True,
        )

    buses.disconnect("tester")
