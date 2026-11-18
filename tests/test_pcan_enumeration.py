from can_mcp.adapters.can.pcan import PcanBusAdapter
from can_mcp.tools.device_tools import DeviceTools


def test_detect_pcan_channels_uses_python_can_detection(monkeypatch) -> None:
    configs = [
        {"channel": "PCAN_USBBUS1", "supports_fd": True},
        {"channel": "PCAN_USBBUS2", "supports_fd": False},
    ]
    calls = []

    def detect_available_configs(interface: str):
        calls.append(interface)
        return configs

    monkeypatch.setattr(
        "can_mcp.adapters.can.pcan.can.detect_available_configs",
        detect_available_configs,
    )

    result = DeviceTools().enumerate_pcan_devices()

    assert calls == ["pcan"]
    assert result == {
        "interface": "pcan",
        "count": 2,
        "devices": configs,
    }


def test_detect_pcan_channels_returns_empty_list_when_none_found(monkeypatch) -> None:
    monkeypatch.setattr(
        "can_mcp.adapters.can.pcan.can.detect_available_configs",
        lambda interface: [],
    )

    assert PcanBusAdapter.detect_channels() == []
