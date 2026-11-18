import can

from can_mcp.adapters.can.base import CanConfig, PythonCanBusAdapter


class PcanBusAdapter(PythonCanBusAdapter):
    """python-can adapter for PEAK-System PCAN devices."""

    interface_name = "pcan"

    def __init__(self, config: CanConfig) -> None:
        super().__init__(config)
        if not isinstance(config.channel, str):
            raise ValueError("PCAN channel must be a channel name such as PCAN_USBBUS1.")

    def _create_bus(self) -> can.BusABC:
        kwargs: dict[str, object] = {
            "interface": self.interface_name,
            "channel": self._config.channel,
            "bitrate": self._config.bitrate,
            "state": (
                can.BusState.PASSIVE
                if self._config.listen_only
                else can.BusState.ACTIVE
            ),
        }
        if self._config.fd:
            data_bitrate = self._config.data_bitrate or max(
                2_000_000,
                self._config.bitrate,
            )
            try:
                timing = can.BitTimingFd.from_sample_point(
                    f_clock=80_000_000,
                    nom_bitrate=self._config.bitrate,
                    nom_sample_point=80.0,
                    data_bitrate=data_bitrate,
                    data_sample_point=80.0,
                )
            except ValueError as error:
                raise ValueError(
                    "Unable to derive PCAN CAN FD timing for "
                    f"{self._config.bitrate} bit/s nominal and "
                    f"{data_bitrate} bit/s data rates at an 80 MHz clock: {error}"
                ) from error
            kwargs["fd"] = True
            kwargs["timing"] = timing
            try:
                return can.Bus(**kwargs)
            except can.CanError as error:
                raise can.CanError(
                    "PCAN CAN FD initialization failed. Check that the selected "
                    "channel supports FD and that the configured nominal/data bit "
                    f"rates are valid. Original driver error: {error}"
                ) from error
        return can.Bus(**kwargs)

    @staticmethod
    def detect_channels() -> list[dict[str, object]]:
        """Return PCAN channels detected by the installed python-can backend."""
        return [
            dict(config)
            for config in can.detect_available_configs("pcan")
        ]
