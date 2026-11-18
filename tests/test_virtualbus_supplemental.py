import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from cantools.database.errors import EncodeError

from can_mcp.core.models import CanFrame
from can_mcp.core.filters import FrameFilter
from can_mcp.services.database_service import DatabaseService
from can_mcp.services.virtual_bus_service import VirtualBusService
from can_mcp.tools.database_tools import DatabaseTools
from can_mcp.tools.virtual_bus_tools import VirtualBusTools


SAMPLE_DBC = Path(__file__).parent / "fixtures" / "sample.dbc"


@pytest.fixture
def buses():
    service = VirtualBusService()
    yield service
    for item in service.list_buses():
        service.disconnect(str(item["handle"]))


def channel() -> str:
    return f"supplemental-{uuid4().hex}"


def dbc_tools(buses: VirtualBusService) -> DatabaseTools:
    database = DatabaseService()
    tools = DatabaseTools(database, buses)
    return tools


def start_and_capture(
    buses: VirtualBusService,
    *,
    handle: str,
    mode: str,
    data: str = "112233",
    **kwargs,
) -> list[bytes]:
    started = buses.start_frame_generator(
        handle=handle,
        arbitration_id=0x321,
        data=bytes.fromhex(data),
        mode=mode,
        period_ms=50,
        **kwargs,
    )
    frames = []
    try:
        for _ in range(5):
            frame = buses.receive("observer", timeout=1)
            assert frame is not None
            frames.append(frame.data)
    finally:
        assert buses.stop_cyclic(str(started["cyclic_id"]))["status"] == "stopped"
    return frames


def test_a1_to_a10_dbc_signal_layer(buses: VirtualBusService, tmp_path: Path) -> None:
    tools = dbc_tools(buses)

    loaded = tools.load_database(str(SAMPLE_DBC))
    assert loaded["message_count"] == 2
    messages = {message["name"]: message for message in loaded["messages"]}
    assert len(messages["EngineData"]["signals"]) == 3
    assert len(messages["VehicleStatus"]["signals"]) == 3

    decoded = tools.decode_frame(256, "10 27 5A 40 00 00 00 00")
    assert decoded["signals"]["EngineSpeed"] == 2500.0
    assert decoded["signals"]["CoolantTemp"] == 50.0
    assert decoded["signals"]["ThrottlePos"] == pytest.approx(25.088)

    signals = {
        "EngineSpeed": 2500,
        "CoolantTemp": 50,
        "ThrottlePos": 25.088,
    }
    encoded = tools.encode_frame("EngineData", signals)
    assert encoded["data"].startswith("10275a40")

    with pytest.raises(EncodeError, match="ThrottlePos"):
        tools.encode_frame(
            "EngineData",
            {"EngineSpeed": 2500, "CoolantTemp": 50},
        )
    unloaded = DatabaseTools(DatabaseService(), buses)
    with pytest.raises(RuntimeError, match="Load a DBC"):
        unloaded.decode_frame(256, "00" * 8)
    with pytest.raises(KeyError):
        tools.decode_frame(999, "00" * 8)
    with pytest.raises(FileNotFoundError, match="Database file does not exist"):
        tools.load_database(str(tmp_path / "nope.dbc"))

    sender, reader = "dbc-sender", "dbc-reader"
    buses.connect(sender, channel())
    buses.connect(reader, buses.list_buses()[0]["channel"])
    sent = tools.send_signal(sender, "EngineData", signals)
    received = buses.receive(reader, timeout=1)
    assert received is not None
    assert received.arbitration_id == 256
    assert received.data.hex() == encoded["data"]
    assert sent["frame"]["data"] == encoded["data"]

    async def read_signal() -> dict[str, object]:
        async def peer_send() -> None:
            await asyncio.sleep(0.03)
            buses.send(
                reader,
                CanFrame(256, bytes.fromhex("10 27 5A 40 00 00 00 00")),
            )

        peer = asyncio.create_task(peer_send())
        result = await tools.read_signal(sender, "EngineData", "EngineSpeed", 2)
        await peer
        return result

    result = asyncio.run(read_signal())
    assert result["value"] == 2500.0
    assert result["frame"]["data"] == "10275a4000000000"
    with pytest.raises(TimeoutError):
        asyncio.run(tools.read_signal(sender, "EngineData", "EngineSpeed", 0.05))


def test_b1_ramp_generator_sequence(buses: VirtualBusService) -> None:
    buses.connect("generator", channel())
    buses.connect("observer", buses.list_buses()[0]["channel"])
    frames = start_and_capture(
        buses,
        handle="generator",
        mode="ramp",
        data="000203",
        start_value=10,
        step=5,
        byte_index=0,
    )
    assert [frame[0] for frame in frames] == [0x0A, 0x0F, 0x14, 0x19, 0x1E]
    assert all(frame[1:] == b"\x02\x03" for frame in frames)


def test_b2_random_seed_is_reproducible(buses: VirtualBusService) -> None:
    buses.connect("generator", channel())
    buses.connect("observer", buses.list_buses()[0]["channel"])
    first = start_and_capture(
        buses, handle="generator", mode="random", data="00000008", seed=42
    )
    second = start_and_capture(
        buses, handle="generator", mode="random", data="00000008", seed=42
    )
    assert first == second


def test_b3_random_without_seed_runs(buses: VirtualBusService) -> None:
    buses.connect("generator", channel())
    buses.connect("observer", buses.list_buses()[0]["channel"])
    frames = start_and_capture(
        buses, handle="generator", mode="random", data="00000008"
    )
    assert len(frames) == 5
    assert all(len(frame) == 4 for frame in frames)


@pytest.mark.parametrize(
    ("test_id", "template", "start_value", "step", "expected"),
    [
        ("B4", "AA{counter}BB", 0, 1, [b"\xaa\x00\xbb", b"\xaa\x01\xbb", b"\xaa\x02\xbb", b"\xaa\x03\xbb", b"\xaa\x04\xbb"]),
        ("B5", "01{value}02", 10, 5, [b"\x01\x0a\x02", b"\x01\x0f\x02", b"\x01\x14\x02", b"\x01\x19\x02", b"\x01\x1e\x02"]),
    ],
)
def test_b4_b5_template_generator(
    buses: VirtualBusService,
    test_id: str,
    template: str,
    start_value: int,
    step: int,
    expected: list[bytes],
) -> None:
    del test_id
    buses.connect("generator", channel())
    buses.connect("observer", buses.list_buses()[0]["channel"])
    actual = start_and_capture(
        buses,
        handle="generator",
        mode="template",
        data="",
        template=template,
        start_value=start_value,
        step=step,
    )
    assert actual == expected


def test_b6_generator_rejects_period_below_minimum(buses: VirtualBusService) -> None:
    buses.connect("generator", channel())
    with pytest.raises(ValueError, match="between 10 and 60000"):
        buses.start_frame_generator(
            "generator", 0x321, b"\x00", 5, "counter"
        )


def test_b7_disconnect_cleans_up_generator_task(buses: VirtualBusService) -> None:
    buses.connect("generator", channel())
    started = buses.start_frame_generator(
        "generator", 0x321, b"\x00", 50, "counter"
    )
    assert buses.disconnect("generator")["status"] == "disconnected"
    assert "generator" not in {item["handle"] for item in buses.list_buses()}
    assert buses.stop_cyclic(str(started["cyclic_id"]))["status"] == "not_found"


def test_b8_stop_generator_prevents_later_frames(buses: VirtualBusService) -> None:
    buses.connect("generator", channel())
    buses.connect("observer", buses.list_buses()[0]["channel"])
    started = buses.start_frame_generator(
        "generator", 0x321, b"\x00", 50, "counter"
    )
    assert buses.receive("observer", timeout=1) is not None
    assert buses.stop_cyclic(str(started["cyclic_id"]))["status"] == "stopped"
    buses.receive("observer", timeout=0.1)
    time.sleep(0.12)
    assert buses.receive("observer", timeout=0) is None


def create_trace_data(buses: VirtualBusService) -> None:
    buses.connect("trace", channel(), fd=True)
    buses.start_trace("trace")
    for arbitration_id in (0x100, 0x101, 0x1FF):
        for index in range(3):
            buses.send(
                "trace",
                CanFrame(arbitration_id, bytes([index]), is_rx=False),
            )
    buses.send(
        "trace",
        CanFrame(0x1FFFFFFF, b"\x01", is_extended_id=True),
    )
    buses.send(
        "trace",
        CanFrame(0x223, b"\x01" * 12, is_fd=True),
    )


def test_c1_to_c8_virtual_trace_filters_lifecycle_and_limits(
    buses: VirtualBusService,
) -> None:
    create_trace_data(buses)
    assert len(
        buses.get_trace("trace", FrameFilter(0x100, 0xF00), limit=100)
    ) == 9
    assert len(buses.get_trace("trace", FrameFilter(0x100), limit=100)) == 3
    assert len(buses.get_trace("trace", FrameFilter(is_extended_id=True))) == 1
    assert len(buses.get_trace("trace", FrameFilter(is_fd=True))) == 1
    assert len(buses.get_trace("trace", FrameFilter(is_rx=False))) == 11
    for limit in (0, -1):
        with pytest.raises(ValueError, match="limit must be greater than zero"):
            buses.get_trace("trace", limit=limit)

    buses.connect("untraced", channel())
    with pytest.raises(Exception, match="Trace has not been started"):
        buses.get_trace("untraced")
    before = buses.get_trace("trace", limit=100)
    buses.stop_trace("trace")
    assert buses.get_trace("trace", limit=100) == before
    buses.start_trace("trace")
    assert buses.get_trace("trace", limit=100) == []


def test_c9_c10_virtual_trace_export_formats_filters_and_parent_creation(
    buses: VirtualBusService,
    tmp_path: Path,
) -> None:
    create_trace_data(buses)
    for extension in ("asc", "blf", "csv"):
        destination = tmp_path / "nested" / f"trace.{extension}"
        exported = buses.export_trace("trace", str(destination), limit=100)
        assert exported["frame_count"] == 11
        assert destination.is_file() and destination.stat().st_size > 0

    filtered = buses.export_trace(
        "trace",
        str(tmp_path / "filtered.csv"),
        FrameFilter(0x100),
        limit=100,
    )
    assert filtered["frame_count"] == 3


def test_d1_d2_d3_d6_scenario_rejects_invalid_boundaries(
    buses: VirtualBusService,
) -> None:
    buses.connect("scenario", channel())
    with pytest.raises(ValueError, match="at most 20"):
        buses.run_scenario(
            "scenario",
            [{"action": "assert", "field": "data", "equals": "00"}] * 21,
        )
    with pytest.raises(ValueError, match="unsupported action"):
        buses.run_scenario("scenario", [{"action": "shell", "cmd": "dir"}])
    with pytest.raises(ValueError, match="total wait time must not exceed 60"):
        buses.run_scenario(
            "scenario",
            [
                {"action": "wait", "arbitration_id": 1, "timeout": 35},
                {"action": "wait", "arbitration_id": 2, "timeout": 35},
            ],
        )
    invalid_id = buses.run_scenario(
        "scenario",
        [{"action": "send", "arbitration_id": 0x800, "data": "00"}],
    )
    assert invalid_id["status"] == "FAIL"
    assert invalid_id["steps"][0]["status"] == "FAIL"
    assert "invalid arbitration_id" in invalid_id["steps"][0]["error"]


def test_d3_invalid_json_is_readable_at_tool_boundary(buses: VirtualBusService) -> None:
    tools = VirtualBusTools(buses)
    with pytest.raises(ValueError, match="invalid JSON"):
        asyncio.run(tools.run_scenario("missing", "{not-json"))


def test_d4_d5_d8_scenario_reports_failures_and_passes(
    buses: VirtualBusService,
) -> None:
    buses.connect("scenario", channel())
    buses.connect("ecu", buses.list_buses()[0]["channel"])
    buses.send("ecu", CanFrame(0x7E8, b"\x59\x02"))
    assertion = buses.run_scenario(
        "scenario",
        [
            {"action": "wait", "arbitration_id": 0x7E8, "timeout": 0.5},
            {"action": "assert", "field": "data", "equals": "62 F1 90"},
        ],
    )
    assert assertion["status"] == "FAIL"
    assert assertion["steps"][-1]["step"] == 2

    timeout = buses.run_scenario(
        "scenario",
        [{"action": "wait", "arbitration_id": 0x777, "timeout": 0.02}],
    )
    assert timeout["status"] == "FAIL"
    assert timeout["steps"][0]["step"] == 1

    def scenario_responder() -> None:
        assert buses.receive("ecu", timeout=1) is not None
        buses.send("ecu", CanFrame(0x7E8, b"\x01"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        responder = pool.submit(scenario_responder)
        passed = buses.run_scenario(
            "scenario",
            [
                {"action": "send", "arbitration_id": 0x321, "data": "01"},
                {"action": "wait", "arbitration_id": 0x7E8, "timeout": 0.5},
                {"action": "assert", "field": "data", "equals": "01"},
            ],
        )
        responder.result(timeout=1)
    assert passed["status"] == "PASS"
    assert passed["passed_steps"] == passed["total_steps"] == 3


def capture_fault_trial(
    buses: VirtualBusService,
    *,
    handle_suffix: str,
    count: int,
    seed: int,
) -> tuple[list[int], int]:
    sender, receiver = f"fault-s-{handle_suffix}", f"fault-r-{handle_suffix}"
    channel_name = channel()
    buses.connect(sender, channel_name)
    buses.connect(receiver, channel_name)
    buses.configure_fault_injection(receiver, drop_rate=0.5, seed=seed)
    for index in range(count):
        buses.send(sender, CanFrame(index % 0x700, b"\x01"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = buses.fault_injection_status(receiver)
        if status["injected_dropped_frames"] + buses._get_service(receiver).get_status(
            False
        )["rx_count"] == count:
            break
        time.sleep(0.005)
    received = []
    while True:
        frame = buses.receive(receiver, timeout=0)
        if frame is None:
            break
        received.append(frame.arbitration_id)
    return received, int(
        buses.fault_injection_status(receiver)["injected_dropped_frames"]
    )


def test_e1_e2_drop_rate_count_and_seed_reproducibility(
    buses: VirtualBusService,
) -> None:
    received1, dropped1 = capture_fault_trial(
        buses, handle_suffix="one", count=100, seed=7
    )
    received2, dropped2 = capture_fault_trial(
        buses, handle_suffix="two", count=100, seed=7
    )
    assert len(received1) == len(received2)
    assert dropped1 == dropped2 == 100 - len(received1)
    assert abs(len(received1) - 50) <= 10


def test_e3_seeded_maximum_delay_arrival_time(buses: VirtualBusService) -> None:
    sender, receiver = "delay-s", "delay-r"
    name = channel()
    buses.connect(sender, name)
    buses.connect(receiver, name)
    buses.configure_fault_injection(receiver, max_delay_ms=200, seed=1)
    start = time.monotonic()
    buses.send(sender, CanFrame(1, b"\x01"))
    assert buses.receive(receiver, timeout=1) is not None
    assert time.monotonic() - start >= 0.15


def test_e4_reorder_rate_one_swaps_two_frames(buses: VirtualBusService) -> None:
    sender, receiver = "reorder-s", "reorder-r"
    name = channel()
    buses.connect(sender, name)
    buses.connect(receiver, name)
    buses.configure_fault_injection(receiver, reorder_rate=1.0, seed=7)
    buses.send(sender, CanFrame(0x101, b"\x01"))
    buses.send(sender, CanFrame(0x102, b"\x02"))
    first = buses.receive(receiver, timeout=1)
    second = buses.receive(receiver, timeout=1)
    assert first is not None and second is not None
    assert [first.arbitration_id, second.arbitration_id] == [0x102, 0x101]


def test_e5_combined_faults_e6_e7_validation_and_e8_disable(
    buses: VirtualBusService,
) -> None:
    sender, receiver = "combined-s", "combined-r"
    name = channel()
    buses.connect(sender, name)
    buses.connect(receiver, name)
    with pytest.raises(ValueError, match="0 and 60000"):
        buses.configure_fault_injection(receiver, max_delay_ms=60_001)
    with pytest.raises(ValueError, match="between 0 and 1"):
        buses.configure_fault_injection(receiver, reorder_rate=-0.1)

    buses.configure_fault_injection(
        receiver,
        drop_rate=0.5,
        max_delay_ms=100,
        reorder_rate=0.5,
        seed=7,
    )
    for index in range(30):
        buses.send(sender, CanFrame(index, b"\x01"))
    time.sleep(0.2)
    counters = buses.fault_injection_status(receiver)
    assert counters["injected_dropped_frames"] > 0
    assert counters["injected_delayed_frames"] > 0
    assert counters["injected_reordered_pairs"] > 0
    dropped_before = counters["injected_dropped_frames"]
    buses.disable_fault_injection(receiver)
    while buses.receive(receiver, timeout=0) is not None:
        pass
    buses.send(sender, CanFrame(0x333, b"\xAA"))
    restored = buses.receive(receiver, timeout=1)
    assert restored is not None and restored.arbitration_id == 0x333
    status_after_disable = buses.fault_injection_status(receiver)
    assert status_after_disable["injected_dropped_frames"] == dropped_before
    assert status_after_disable["injected_delayed_frames"] == counters[
        "injected_delayed_frames"
    ]
    assert status_after_disable["injected_reordered_pairs"] == counters[
        "injected_reordered_pairs"
    ]
    assert buses.metrics()["injected_dropped_frames"] >= dropped_before


def ecu_reply_after_delay(
    buses: VirtualBusService,
    ecu_handle: str,
    response_id: int,
    frames: list[tuple[float, bytes]],
) -> None:
    request = buses.receive(ecu_handle, timeout=1)
    assert request is not None
    for delay, payload in frames:
        time.sleep(delay)
        buses.send(ecu_handle, CanFrame(response_id, bytes([len(payload)]) + payload))


def test_f1_f3_uds_pending_final_response_and_s3_metadata(
    buses: VirtualBusService,
) -> None:
    tester, ecu = "uds-tester", "uds-ecu"
    name = channel()
    buses.connect(tester, name)
    buses.connect(ecu, name)
    with ThreadPoolExecutor(max_workers=1) as pool:
        responder = pool.submit(
            ecu_reply_after_delay,
            buses,
            ecu,
            0x7E8,
            [(0, b"\x7F\x22\x78"), (0.3, b"\x62\xF1\x90")],
        )
        result = buses.uds_request(
            tester,
            0x7E0,
            0x7E8,
            b"\x22\xF1\x90",
            timeout=2,
            p2_ms=100,
            p2_star_ms=1_000,
            s3_ms=7_000,
        )
        responder.result(timeout=2)
    assert result["response_data"] == "62f190"
    assert result["s3_ms"] == 7_000


def test_f2_uds_pending_response_obeys_p2_star_deadline(
    buses: VirtualBusService,
) -> None:
    tester, ecu = "uds-short-tester", "uds-short-ecu"
    name = channel()
    buses.connect(tester, name)
    buses.connect(ecu, name)
    with ThreadPoolExecutor(max_workers=1) as pool:
        responder = pool.submit(
            ecu_reply_after_delay,
            buses,
            ecu,
            0x7E8,
            [(0, b"\x7F\x22\x78"), (0.3, b"\x62\xF1\x90")],
        )
        with pytest.raises(TimeoutError, match="No ISO-TP response"):
            buses.uds_request(
                tester,
                0x7E0,
                0x7E8,
                b"\x22\xF1\x90",
                timeout=1,
                p2_ms=100,
                p2_star_ms=150,
            )
        responder.result(timeout=2)


def test_f4_functional_uds_collects_pending_then_positive(
    buses: VirtualBusService,
) -> None:
    tester, ecu = "functional-tester", "functional-ecu"
    name = channel()
    buses.connect(tester, name)
    buses.connect(ecu, name)

    def respond() -> None:
        assert buses.receive(ecu, timeout=1) is not None
        buses.send(ecu, CanFrame(0x7E8, b"\x03\x7F\x19\x78"))
        time.sleep(0.05)
        buses.send(ecu, CanFrame(0x7E8, b"\x04\x59\x02\xFF\x00"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(respond)
        result = buses.uds_functional_request(
            tester, 0x7DF, b"\x19\x02\xFF", p2_ms=100, p2_star_ms=500
        )
        response.result(timeout=1)
    assert result["response_count"] == 1
    assert result["responses"]["2024"]["data"] == "5902ff00"


def test_f5_f6_f7_f8_functional_and_suppression_boundaries(
    buses: VirtualBusService,
) -> None:
    tester, ecu = "bound-tester", "bound-ecu"
    name = channel()
    buses.connect(tester, name)
    buses.connect(ecu, name)
    with pytest.raises(ValueError, match="limited to 7 bytes"):
        buses.uds_functional_request(tester, 0x7DF, b"\x01" * 8)

    def receive_suppressed() -> CanFrame | None:
        return buses.receive(ecu, timeout=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        request_future = pool.submit(receive_suppressed)
        result = buses.uds_request(
            tester,
            0x7E0,
            0x7E8,
            b"\x10\x02",
            timeout=1,
            suppress_positive_response=True,
        )
        request = request_future.result(timeout=1)
    assert result["status"] == "sent_no_response_expected"
    assert request is not None and request.data == b"\x02\x10\x82"

    def respond_anyway() -> None:
        assert buses.receive(ecu, timeout=1) is not None
        buses.send(ecu, CanFrame(0x7E8, b"\x02\x50\x02"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(respond_anyway)
        result = buses.uds_request(
            tester,
            0x7E0,
            0x7E8,
            b"\x10\x02",
            timeout=1,
            suppress_positive_response=True,
        )
        response.result(timeout=1)
    assert result["status"] == "sent_no_response_expected"

    for invalid in (0, -1):
        with pytest.raises(ValueError, match="p2_ms must be a positive integer"):
            buses.uds_request(
                tester, 0x7E0, 0x7E8, b"\x22", 1, p2_ms=invalid
            )


@pytest.mark.parametrize(
    ("test_id", "actual_data", "expected_result"),
    [
        ("G1", b"\x1F\x00", "matched"),
        ("G2", b"\x20\x00", "timeout"),
    ],
)
def test_g1_g2_virtual_expect_mask_semantics(
    buses: VirtualBusService,
    test_id: str,
    actual_data: bytes,
    expected_result: str,
) -> None:
    sender, receiver = f"{test_id}-sender", f"{test_id}-receiver"
    name = channel()
    buses.connect(sender, name)
    buses.connect(receiver, name)
    buses.send(sender, CanFrame(0x100, actual_data))
    tools = VirtualBusTools(buses)
    if expected_result == "matched":
        result = asyncio.run(
            tools.expect_virtual_frame(
                receiver,
                0x100,
                timeout=0.1,
                data="10 00",
                data_mask="F0 00",
            )
        )
        assert result["status"] == "matched"
    else:
        with pytest.raises(TimeoutError):
            asyncio.run(
                tools.expect_virtual_frame(
                    receiver,
                    0x100,
                    timeout=0.03,
                    data="10 00",
                    data_mask="F0 00",
                )
            )


def test_g3_g4_g5_expect_prefix_extended_and_zero_timeout(
    buses: VirtualBusService,
) -> None:
    sender, receiver = "expect-sender", "expect-receiver"
    name = channel()
    buses.connect(sender, name)
    buses.connect(receiver, name)
    buses.send(sender, CanFrame(0x100, b"\x10\xAA"))
    prefix = buses.expect_frame(receiver, 0x100, 0.1, b"\x10")
    assert prefix.data == b"\x10\xAA"

    buses.send(sender, CanFrame(0x101, b"\x01"))
    with pytest.raises(TimeoutError):
        buses.expect_frame(receiver, 0x101, 0.03, is_extended_id=True)
    with pytest.raises(ValueError, match="timeout must be a finite number greater than zero"):
        buses.expect_frame(receiver, 0x102, 0)


def test_g6_list_virtual_buses_does_not_block_waiting_receive(
    buses: VirtualBusService,
) -> None:
    buses.connect("waiting", channel())
    with ThreadPoolExecutor(max_workers=2) as pool:
        receive = pool.submit(buses.receive, "waiting", 0.5)
        started = time.monotonic()
        listed = pool.submit(buses.list_buses).result(timeout=0.2)
        assert time.monotonic() - started < 0.2
        assert any(item["handle"] == "waiting" for item in listed)
        assert receive.result(timeout=0.7) is None


def test_h1_h2_h3_handle_limits_rebuild_and_missing_disconnect(
    buses: VirtualBusService,
) -> None:
    for index in range(16):
        buses.connect(f"handle-{index}", channel())
    with pytest.raises(ValueError, match="handle limit \\(16\\) has been reached"):
        buses.connect("handle-16", channel())
    old_status = next(item for item in buses.list_buses() if item["handle"] == "handle-0")
    buses.disconnect("handle-0")
    buses.connect("handle-0", channel())
    new_status = next(item for item in buses.list_buses() if item["handle"] == "handle-0")
    assert new_status["tx_count"] == new_status["rx_count"] == 0
    assert old_status["tx_count"] == 0

    missing = buses.disconnect("missing-handle")
    assert missing == {"status": "not_found", "handle": "missing-handle"}


def test_h4_generator_disconnect_cleans_handle_and_task(buses: VirtualBusService) -> None:
    buses.connect("generated", channel())
    started = buses.start_frame_generator(
        "generated", 0x123, b"\x00", 50, "counter"
    )
    assert buses.disconnect("generated")["status"] == "disconnected"
    assert all(item["handle"] != "generated" for item in buses.list_buses())
    assert buses.stop_cyclic(str(started["cyclic_id"]))["status"] == "not_found"


def test_h5_concurrent_receive_and_list_finish_promptly(
    buses: VirtualBusService,
) -> None:
    buses.connect("concurrent", channel())
    with ThreadPoolExecutor(max_workers=2) as pool:
        receive = pool.submit(buses.receive, "concurrent", 0.5)
        listing = pool.submit(buses.list_buses)
        assert listing.result(timeout=1)
        assert receive.result(timeout=1) is None


def test_h6_receive_buffer_drop_count_tracks_overrun(buses: VirtualBusService) -> None:
    sender, receiver = "buffer-sender", "buffer-receiver"
    name = channel()
    buses.connect(sender, name)
    buses.connect(receiver, name)
    count = 1_200
    for index in range(count):
        buses.send(sender, CanFrame(index % 0x700, b"\x01"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = next(
            item for item in buses.list_buses() if item["handle"] == receiver
        )
        if status["rx_count"] >= count:
            break
        time.sleep(0.01)
    assert status["receive_buffer_dropped_count"] > 0
    assert status["receive_buffer_dropped_count"] == status["rx_count"] - 1000
