import can

from can_mcp.adapters.can.base import CanConfig, PythonCanBusAdapter


class VirtualBusAdapter(PythonCanBusAdapter):
    """Isolated python-can virtual bus adapter for software-only tests."""

    interface_name = "virtual"

    def _create_bus(self) -> can.BusABC:
        return can.Bus(
            interface="virtual",
            channel=self._config.channel,
            receive_own_messages=False,
            rx_queue_size=1_000,
        )
