import can
import pytest

from can_mcp.adapters.can.base import CanConfig
from can_mcp.adapters.can.factory import create_can_adapter
from can_mcp.adapters.can.pcan import PcanBusAdapter
from can_mcp.adapters.can.vector import VectorBusAdapter
from can_mcp.core.models import CanFrame


def test_factory_creates_hardware_specific_adapters() -> None:
    assert isinstance(
        create_can_adapter(CanConfig(interface="pcan", channel="PCAN_USBBUS1")),
        PcanBusAdapter,
    )
    assert isinstance(
        create_can_adapter(CanConfig(interface="vector", channel=0)),
        VectorBusAdapter,
    )


def test_factory_rejects_unsupported_interface() -> None:
    with pytest.raises(ValueError, match="Unsupported CAN interface"):
        create_can_adapter(CanConfig(interface="unknown", channel="unused"))
    with pytest.raises(ValueError, match="Unsupported CAN interface"):
        create_can_adapter(CanConfig(interface="virtual", channel="simulated"))


def test_adapter_rejects_mismatched_hardware_configuration() -> None:
    with pytest.raises(ValueError, match="requires interface 'pcan'"):
        PcanBusAdapter(CanConfig(interface="vector", channel=0))


def test_vector_adapter_passes_vector_specific_config_to_python_can(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = object()
    calls = []

    def fake_bus(**kwargs):
        calls.append(kwargs)
        return bus

    monkeypatch.setattr("can_mcp.adapters.can.vector.can.Bus", fake_bus)
    adapter = VectorBusAdapter(
        CanConfig(
            interface="vector",
            channel=2,
            bitrate=500_000,
            fd=True,
            data_bitrate=2_000_000,
            serial=123456,
            app_name="CAN MCP",
        )
    )

    assert adapter.connect() is bus
    assert calls == [
        {
            "interface": "vector",
            "channel": 2,
            "bitrate": 500_000,
            "app_name": "CAN MCP",
            "listen_only": False,
            "serial": 123456,
            "fd": True,
            "data_bitrate": 2_000_000,
        }
    ]


@pytest.mark.parametrize(
    ("adapter_class", "config", "expected"),
    [
        (
            PcanBusAdapter,
            CanConfig(
                interface="pcan",
                channel="PCAN_USBBUS1",
                listen_only=True,
            ),
            {"state": can.BusState.PASSIVE},
        ),
        (
            VectorBusAdapter,
            CanConfig(interface="vector", channel=1, listen_only=True),
            {"listen_only": True},
        ),
    ],
)
def test_hardware_adapters_enable_listen_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    adapter_class,
    config: CanConfig,
    expected: dict[str, object],
) -> None:
    calls = []
    bus = object()

    def fake_bus(**kwargs):
        calls.append(kwargs)
        return bus

    monkeypatch.setattr(adapter_class.__module__.replace(".", ".") + ".can.Bus", fake_bus)

    assert adapter_class(config).connect() is bus
    assert len(calls) == 1
    assert all(calls[0][key] == value for key, value in expected.items())


def test_vector_enumeration_omits_non_json_channel_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "can_mcp.adapters.can.vector.can.detect_available_configs",
        lambda interface: [
            {
                "interface": interface,
                "channel": 2,
                "serial": 123456,
                "channel_index": 5,
                "supports_fd": True,
                "vector_channel_config": object(),
            }
        ],
    )

    assert VectorBusAdapter.detect_channels() == [
        {
            "channel": 2,
            "serial": 123456,
            "channel_index": 5,
            "supports_fd": True,
        }
    ]


def test_pcan_fd_connection_builds_explicit_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    bus = object()

    def fake_bus(**kwargs):
        calls.append(kwargs)
        return bus

    monkeypatch.setattr("can_mcp.adapters.can.pcan.can.Bus", fake_bus)
    adapter = PcanBusAdapter(
        CanConfig(
            interface="pcan",
            channel="PCAN_USBBUS1",
            bitrate=500_000,
            fd=True,
            data_bitrate=2_000_000,
        )
    )

    assert adapter.connect() is bus
    assert len(calls) == 1
    assert calls[0]["interface"] == "pcan"
    assert calls[0]["channel"] == "PCAN_USBBUS1"
    assert calls[0]["fd"] is True
    timing = calls[0]["timing"]
    assert timing.f_clock == 80_000_000
    assert timing.nom_bitrate == 500_000
    assert timing.data_bitrate == 2_000_000


def test_pcan_fd_init_error_includes_actionable_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_init_error(**kwargs):
        raise can.CanError("A parameter contains an invalid value")

    monkeypatch.setattr("can_mcp.adapters.can.pcan.can.Bus", raise_init_error)
    adapter = PcanBusAdapter(
        CanConfig(interface="pcan", channel="PCAN_USBBUS1", fd=True)
    )

    with pytest.raises(can.CanError, match="PCAN CAN FD initialization failed"):
        adapter.connect()


@pytest.mark.parametrize("length", [0, 1, 8, 12, 16, 20, 24, 32, 48, 64])
def test_can_fd_payload_lengths_set_dlc_from_data_length(
    length: int,
) -> None:
    messages = []

    class FakeBus:
        def send(self, message) -> None:
            messages.append(message)

    adapter = PcanBusAdapter(CanConfig(interface="pcan", channel="PCAN_USBBUS1"))
    adapter._bus = FakeBus()

    adapter.send(
        CanFrame(
            arbitration_id=0x7FF,
            data=bytes(length),
            is_fd=True,
        )
    )

    assert messages[0].dlc == length
    assert len(messages[0].data) == length


@pytest.mark.parametrize("length", [9, 10, 11, 13, 63, 65])
def test_can_fd_rejects_invalid_payload_lengths(length: int) -> None:
    adapter = PcanBusAdapter(CanConfig(interface="pcan", channel="PCAN_USBBUS1"))
    adapter._bus = object()

    with pytest.raises(ValueError, match="CAN FD payload length"):
        adapter.send(CanFrame(arbitration_id=0x123, data=bytes(length), is_fd=True))
