import asyncio
from pathlib import Path

import can
import pytest

from can_mcp.adapters.can.base import CanConfig
from can_mcp.core.models import CanFrame
from can_mcp.services.database_service import DatabaseService
from can_mcp.services.physical_bus_service import PhysicalBusService
from can_mcp.services.virtual_bus_service import VirtualBusService
from can_mcp.tools.database_tools import DatabaseTools


DBC_CONTENT = """VERSION ""
NS_ :
BS_:
BU_: Vector__XXX
BO_ 291 Example: 8 Vector__XXX
 SG_ Speed : 0|16@1+ (0.01,0) [0|655.35] "km/h" Vector__XXX
 SG_ Enabled : 16|1@1+ (1,0) [0|1] "" Vector__XXX
"""


def create_database_tools(tmp_path: Path) -> tuple[DatabaseTools, VirtualBusService]:
    dbc_path = tmp_path / "example.dbc"
    dbc_path.write_text(DBC_CONTENT, encoding="utf-8")
    database = DatabaseService()
    virtual_buses = VirtualBusService()
    tools = DatabaseTools(database, virtual_buses)
    loaded = tools.load_database(str(dbc_path))
    assert loaded["message_count"] == 1
    return tools, virtual_buses


def test_dbc_load_decode_and_encode_round_trip(tmp_path: Path) -> None:
    tools, _ = create_database_tools(tmp_path)

    decoded = tools.decode_frame(0x123, "34 12 01 00 00 00 00 00")
    encoded = tools.encode_frame(
        "Example",
        {"Speed": 46.6, "Enabled": 1},
    )

    assert decoded["message_name"] == "Example"
    assert decoded["signals"] == {"Speed": 46.6, "Enabled": 1}
    assert encoded["arbitration_id"] == 0x123
    assert encoded["data"] == "3412010000000000"


def test_send_signal_is_virtual_only_and_read_signal_decodes_value(
    tmp_path: Path,
) -> None:
    tools, virtual_buses = create_database_tools(tmp_path)
    virtual_buses.connect("signal-sender", "test-signal")
    virtual_buses.connect("signal-reader", "test-signal")
    sent = tools.send_signal(
        "signal-sender",
        "Example",
        {"Speed": 123.45, "Enabled": 1},
    )

    received = asyncio.run(
        tools.read_signal(
            "signal-reader",
            "Example",
            "Speed",
            timeout=1,
        )
    )

    assert sent["status"] == "sent"
    assert received["value"] == 123.45
    assert received["frame"]["arbitration_id"] == 0x123
    virtual_buses.disconnect("signal-sender")
    virtual_buses.disconnect("signal-reader")


def test_send_and_read_dbc_signal_on_vector_handle_require_send_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from can_mcp.tools.physical_can_feature_tools import PhysicalCanFeatureTools

    class Adapter:
        def __init__(self) -> None:
            self.sent: list[CanFrame] = []

        def connect(self):
            return object()

        def disconnect(self) -> None:
            pass

        def send(self, frame: CanFrame) -> None:
            self.sent.append(frame)

    class Notifier:
        def __init__(self, bus, listeners) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: Notifier(bus, listeners),
    )
    dbc_path = tmp_path / "example.dbc"
    dbc_path.write_text(DBC_CONTENT, encoding="utf-8")
    database = DatabaseService()
    virtual_buses = VirtualBusService()
    physical_buses = PhysicalBusService()
    tools = DatabaseTools(database, virtual_buses, physical_bus_service=physical_buses)
    tools.load_database(str(dbc_path))
    adapter = Adapter()
    physical_buses.connect(
        "vector-1",
        adapter,
        CanConfig(interface="vector", channel=0),
    )

    with pytest.raises(ValueError, match="confirm_physical_io=true"):
        tools.send_signal(
            "vector-1",
            "Example",
            {"Speed": 123.45, "Enabled": 1},
        )
    sent = tools.send_signal(
        "vector-1",
        "Example",
        {"Speed": 123.45, "Enabled": 1},
        confirm_physical_io=True,
    )
    physical_buses.get_bus("vector-1")._on_message(
        can.Message(
            arbitration_id=0x123,
            data=bytes.fromhex("3930010000000000"),
            is_extended_id=False,
            is_rx=True,
        )
    )
    received = asyncio.run(
        tools.read_signal("vector-1", "Example", "Speed", timeout=1)
    )

    assert sent["status"] == "sent"
    assert len(adapter.sent) == 1
    assert received["value"] == 123.45
    physical_buses.disconnect("vector-1")


def test_dbc_load_missing_file_and_unknown_signal_are_explicit(
    tmp_path: Path,
) -> None:
    tools, _ = create_database_tools(tmp_path)

    try:
        tools.decode_frame(0x7FF, "00")
    except KeyError:
        pass
    else:
        raise AssertionError("Unknown DBC frame ID should fail.")

    try:
        tools.encode_frame("Example", {"Missing": 1})
    except (KeyError, ValueError):
        pass
    else:
        raise AssertionError("Unknown DBC signal should fail.")


@pytest.mark.skipif(
    not Path(__file__).with_name("j1939.dbc").is_file(),
    reason="Optional large J1939 DBC fixture is not included.",
)
def test_j1939_dbc_load_decode_encode_and_virtual_signal_round_trip() -> None:
    dbc_path = Path(__file__).with_name("j1939.dbc")
    database = DatabaseService()
    virtual_buses = VirtualBusService()
    tools = DatabaseTools(database, virtual_buses)

    loaded = tools.load_database(str(dbc_path))
    assert loaded["message_count"] == 550
    assert any("ILI" in warning for warning in loaded["warnings"])

    decoded = tools.decode_frame(0x0CF00400, "00 00 00 00 00 00 00 00", True)
    assert decoded["message_name"] == "EEC1_00"
    assert decoded["signals"]["EngSpeed"] == 0.0

    signals = dict(decoded["signals"])
    signals["EngSpeed"] = 1000.0
    encoded = tools.encode_frame("EEC1_00", signals)
    assert encoded["arbitration_id"] == 0x0CF00400
    assert tools.decode_frame(
        encoded["arbitration_id"],
        encoded["data"],
        is_extended_id=True,
    )["signals"]["EngSpeed"] == 1000.0

    virtual_buses.connect("j1939-sender", "test-j1939-dbc")
    virtual_buses.connect("j1939-reader", "test-j1939-dbc")
    sent = tools.send_signal("j1939-sender", "EEC1_00", signals)
    received = asyncio.run(
        tools.read_signal(
            "j1939-reader",
            "EEC1_00",
            "EngSpeed",
            timeout=1,
        )
    )

    assert sent["status"] == "sent"
    assert received["value"] == 1000.0
    virtual_buses.disconnect("j1939-sender")
    virtual_buses.disconnect("j1939-reader")


@pytest.mark.skipif(
    not Path(__file__).with_name("j1939.dbc").is_file(),
    reason="Optional large J1939 DBC fixture is not included.",
)
def test_j1939_dbc_rejects_decode_of_out_of_range_ili_signals() -> None:
    database = DatabaseService()
    database.load(str(Path(__file__).with_name("j1939.dbc")))

    with pytest.raises(ValueError, match="outside its declared"):
        database.decode_frame(0x1CFD19FE, bytes(8), is_extended_id=True)
