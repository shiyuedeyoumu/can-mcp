import json

from mcp.types import CallToolResult, TextContent

from can_mcp.core.filters import FrameFilter
from can_mcp.services.bus_service import BusService
from can_mcp.services.file_transfer_service import FileTransferService
from can_mcp.services.physical_bus_service import PhysicalBusService
from can_mcp.services.trace_analysis import calculate_trace_stats, export_trace_frames
from can_mcp.services.trace_service import TraceService


def _make_filter(
    arbitration_id: int | None,
    arbitration_id_mask: int | None,
    is_extended_id: bool | None,
    is_fd: bool | None,
    is_rx: bool | None,
) -> FrameFilter | None:
    if all(value is None for value in (
        arbitration_id, arbitration_id_mask, is_extended_id, is_fd, is_rx
    )):
        return None
    return FrameFilter(
        arbitration_id=arbitration_id,
        arbitration_id_mask=arbitration_id_mask,
        is_extended_id=is_extended_id,
        is_fd=is_fd,
        is_rx=is_rx,
    )


class TraceTools:
    def __init__(
        self,
        bus_service: BusService | PhysicalBusService,
        trace_service: TraceService | None,
        file_transfer_service: FileTransferService | None = None,
    ) -> None:
        self._bus = bus_service
        self._trace = trace_service
        self._files = file_transfer_service

    def start_trace(
        self,
        arbitration_id: int | None = None,
        arbitration_id_mask: int | None = None,
        is_extended_id: bool | None = None,
        is_fd: bool | None = None,
        is_rx: bool | None = None,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> dict[str, str]:
        """开始将指定物理 CAN handle 的报文记录到内存 trace 缓冲区。

        必须先调用 connect_can。开始时会清空上一次的 trace 内容；缓冲区最多保留
        10000 帧，达到上限后最早的帧会被新帧覆盖。可选参数在捕获时过滤，
        不匹配的帧不会保存。未指定筛选条件时记录所有报文。再次调用时不会
        重置正在运行的 trace，而是返回 already_tracing。

        Args:
            arbitration_id: 可选 CAN ID。未提供掩码时精确匹配；提供掩码时只比较
                掩码中为 1 的位。仅传掩码而不传 ID 时，掩码不生效。
            arbitration_id_mask: 可选 ID 位掩码。例如 ID=0x120、mask=0x7F0
                可匹配 0x120–0x12F。
            is_extended_id: true 仅记录扩展帧；false 仅记录标准帧；null 不过滤。
            is_fd: true 仅记录 CAN FD；false 仅记录经典 CAN；null 不过滤。
            is_rx: true 仅记录接收帧；false 仅记录发送方向帧；null 不按方向过滤。
            handle: 要捕获的物理连接名；默认 "default"。

        Returns:
            status 为 trace_started 或 already_tracing。

        Raises:
            BusNotConnectedError: 尚未连接总线。
        """
        frame_filter = _make_filter(
            arbitration_id, arbitration_id_mask, is_extended_id, is_fd, is_rx
        )
        trace = self._trace_for(handle)
        return {"status": trace.start(self._bus_for(handle), frame_filter)}

    def stop_trace(
        self,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> dict[str, str]:
        """停止指定物理 handle 的 trace 捕获，保留缓存供 get_trace 查询。

        停止 trace 不会断开 CAN 总线。未启动 trace 时返回 already_stopped。

        Returns:
            status 为 trace_stopped 或 already_stopped。
        """
        return {"status": self._trace_for(handle).stop()}

    def get_trace(
        self,
        limit: int = 100,
        arbitration_id: int | None = None,
        arbitration_id_mask: int | None = None,
        is_extended_id: bool | None = None,
        is_fd: bool | None = None,
        is_rx: bool | None = None,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> CallToolResult:
        """查询指定物理 handle 已捕获的报文；trace 停止后仍可查询。

        按捕获顺序返回最新的匹配帧。查询筛选只对本次结果生效，不会更改已保存
        内容或捕获筛选条件。尚未启动过 trace 时会报错；若已停止 trace，可继续
        查询其缓存；重新启动 trace 会清空上一轮缓存。

        Args:
            limit: 最多返回的匹配帧数，必须大于 0；默认返回最新 100 帧。
            arbitration_id: 可选 CAN ID。没有掩码时精确匹配；有掩码时执行按位匹配。
            arbitration_id_mask: 可选 ID 掩码；只有同时提供 arbitration_id 才生效。
                例如 ID=0x120、mask=0x7F0 可匹配 0x120–0x12F。
            is_extended_id: true 只查询扩展帧；false 只查询标准帧；null 不过滤。
            is_fd: true 只查询 CAN FD；false 只查询经典 CAN；null 不过滤。
            is_rx: true 只查询接收帧；false 只查询发送方向帧；null 不过滤。
            handle: 要查询的物理连接名；默认 "default"。

        Returns:
            MCP 工具结果同时包含文本 JSON 和结构化内容。structuredContent.result
            是按捕获顺序排列的帧字典列表，每帧含 arbitration_id、data、timestamp、
            is_extended_id、is_fd、bitrate_switch、is_rx 和 channel；data 为不带
            空格的小写十六进制字符串。无匹配帧时 result 是空列表。

        Raises:
            ValueError: limit 小于或等于 0。
        """
        frame_filter = _make_filter(
            arbitration_id, arbitration_id_mask, is_extended_id, is_fd, is_rx
        )
        frames = [
            frame.to_dict()
            for frame in self._trace_for(handle).query(frame_filter, limit)
        ]
        structured_content = {"result": frames}
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(structured_content, ensure_ascii=False),
                )
            ],
            structuredContent=structured_content,
        )

    def get_trace_stats(
        self,
        bitrate: int = 500_000,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> dict[str, object]:
        """统计指定物理 handle trace 中各 CAN ID 的数量、周期和抖动。

        总线负载使用帧长度及估算协议开销粗略计算，不是精确物理测量。
        必须先启动过 trace。
        """
        frames = self._trace_for(handle).query(limit=300_000)
        return calculate_trace_stats(frames, bitrate)

    def export_trace(
        self,
        path: str | None = None,
        limit: int = 10_000,
        arbitration_id: int | None = None,
        arbitration_id_mask: int | None = None,
        is_extended_id: bool | None = None,
        is_fd: bool | None = None,
        is_rx: bool | None = None,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> dict[str, object]:
        """将指定物理 handle 的 trace 导出至 uploads/，格式为 .asc、.blf 或 .csv。

        path 仅接受文件名；省略时在 uploads/ 下生成唯一的 .asc 文件。
        文件只能通过文件传输工具进出，服务端路径对客户端不可见。
        """
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        frame_filter = _make_filter(
            arbitration_id, arbitration_id_mask, is_extended_id, is_fd, is_rx
        )
        frames = self._trace_for(handle).query(frame_filter, limit)
        if self._files is None:
            if path is None:
                raise ValueError("path is required when file sandbox is unavailable.")
            return export_trace_frames(frames, path)
        destination, identifier = self._files.export_destination(path)
        result = export_trace_frames(frames, str(destination))
        result["path"] = identifier
        return result

    def _trace_for(self, handle: str) -> TraceService:
        if isinstance(self._bus, PhysicalBusService):
            return self._bus.get_trace_service(handle)
        if self._trace is None:
            raise RuntimeError("Trace service is unavailable.")
        return self._trace

    def _bus_for(self, handle: str) -> BusService:
        if isinstance(self._bus, PhysicalBusService):
            return self._bus.get_bus(handle)
        return self._bus
