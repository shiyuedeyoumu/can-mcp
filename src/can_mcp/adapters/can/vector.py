import can

from can_mcp.adapters.can.base import CanConfig, PythonCanBusAdapter


class VectorBusAdapter(PythonCanBusAdapter):
    """python-can adapter for Vector XL devices such as the VN1640A."""

    interface_name = "vector"

    def __init__(self, config: CanConfig) -> None:
        super().__init__(config)
        if isinstance(config.channel, str):
            raise ValueError("Vector channel must be an integer channel index.")

    def _create_bus(self) -> can.BusABC:
        kwargs: dict[str, object] = {
            "interface": self.interface_name,
            "channel": self._config.channel,
            "bitrate": self._config.bitrate,
            "app_name": self._config.app_name,
            "listen_only": self._config.listen_only,
        }
        if self._config.serial is not None:
            kwargs["serial"] = self._config.serial
        if self._config.fd:
            kwargs["fd"] = True
            if self._config.data_bitrate is not None:
                kwargs["data_bitrate"] = self._config.data_bitrate
        return can.Bus(**kwargs)

    @staticmethod
    def detect_channels() -> list[dict[str, object]]:
        """Return JSON-serializable Vector CAN channel information."""
        keys = (
            "channel",
            "serial",
            "channel_index",
            "hw_type",
            "hw_index",
            "hw_channel",
            "supports_fd",
        )
        return [
            {key: config[key] for key in keys if key in config}
            for config in can.detect_available_configs("vector")
        ]
