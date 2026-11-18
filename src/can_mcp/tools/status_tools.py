from can_mcp.services.bus_service import BusService
from can_mcp.services.physical_bus_service import PhysicalBusService
from can_mcp.services.trace_service import TraceService
from can_mcp.services.virtual_bus_service import VirtualBusService


class StatusTools:
    def __init__(
        self,
        bus_service: BusService | PhysicalBusService,
        trace_service: TraceService | None,
        virtual_bus_service: VirtualBusService | None = None,
    ) -> None:
        self._bus = bus_service
        self._trace = trace_service
        self._virtual_buses = virtual_bus_service

    def get_status(
        self,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> dict[str, object]:
        """返回指定物理 CAN handle 的连接、Trace 状态和累计计数。

        计数自该 handle 首次连接后累计，不因断开或切换接口而清零。未连接时
        连接参数为 null。bus_state 在驱动不支持读取时为 null；
        error_frame_count 和 latest_error_frame 仅反映 python-can 实际收到的错误帧。

        Returns:
            包含 connected、interface、channel、bitrate、fd、data_bitrate、
            serial、app_name、listen_only、trace_active、tx_count、rx_count、
            receive_buffer_dropped_count、error_frame_count、latest_error_frame
            和 bus_state 的状态对象。listen_only=true 表示已启用硬件只听模式且
            MCP 发送会被拒绝。
        """
        if isinstance(self._bus, PhysicalBusService):
            return self._bus.get_status(handle)
        return self._bus.get_status(self._trace_active)

    def list_can_connections(self) -> list[dict[str, object]]:
        """列出所有曾建立的物理 CAN handle 及其连接状态和累计计数。"""
        if not isinstance(self._bus, PhysicalBusService):
            return [self._bus.get_status(self._trace_active)]
        return self._bus.list_connections()

    def decode_error_frame(
        self,
        handle: str | None = None,
        bus_handle: str | None = None,
    ) -> dict[str, object]:
        """只读报告最近观察到的错误帧及驱动可提供的总线状态。

        handle 可选择物理或虚拟连接；bus_handle 是兼容旧客户端的物理连接参数。
        同名物理和虚拟 handle 同时存在时，优先物理连接。不注入、发送或推断
        厂商专有错误位。
        """
        if handle is not None and bus_handle is not None:
            raise ValueError("Specify handle or bus_handle, not both.")
        selected_handle = handle or bus_handle or PhysicalBusService.DEFAULT_HANDLE
        if handle is not None:
            if (
                isinstance(self._bus, PhysicalBusService)
                and self._bus.has_handle(handle)
            ):
                status = self._bus.get_status(handle)
            else:
                if self._virtual_buses is None:
                    raise RuntimeError("VirtualBus diagnostics are unavailable.")
                return self._virtual_buses.decode_error_frame(handle)
        elif isinstance(self._bus, PhysicalBusService):
            status = self._bus.get_status(selected_handle)
        else:
            status = self._bus.get_status(trace_active=self._trace_active)
        frame = status["latest_error_frame"]
        return {
            "bus_state": status["bus_state"],
            "error_frame_count": status["error_frame_count"],
            "error_frame": frame,
            "interpretation": (
                "No error frame has been observed."
                if frame is None
                else "Raw python-can error-frame flags and payload are reported; "
                "vendor-specific error bits are not inferred."
            ),
        }

    @property
    def _trace_active(self) -> bool:
        if self._trace is None:
            return False
        return self._trace.active
