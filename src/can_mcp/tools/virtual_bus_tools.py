import asyncio
import json
from mcp.types import CallToolResult, TextContent

from can_mcp.core.filters import FrameFilter
from can_mcp.core.models import CanFrame, validate_payload_length
from can_mcp.services.file_transfer_service import FileTransferService
from can_mcp.services.virtual_bus_service import VirtualBusService


def _make_virtual_filter(
    arbitration_id: int | None,
    arbitration_id_mask: int | None,
    is_extended_id: bool | None,
    is_fd: bool | None,
    is_rx: bool | None,
) -> FrameFilter | None:
    if all(
        value is None
        for value in (
            arbitration_id,
            arbitration_id_mask,
            is_extended_id,
            is_fd,
            is_rx,
        )
    ):
        return None
    return FrameFilter(
        arbitration_id=arbitration_id,
        arbitration_id_mask=arbitration_id_mask,
        is_extended_id=is_extended_id,
        is_fd=is_fd,
        is_rx=is_rx,
    )


class VirtualBusTools:
    def __init__(
        self,
        virtual_bus_service: VirtualBusService,
        file_transfer_service: FileTransferService | None = None,
    ) -> None:
        self._buses = virtual_bus_service
        self._files = file_transfer_service

    def connect_virtual_bus(
        self,
        handle: str,
        channel: str,
        fd: bool = False,
    ) -> dict[str, object]:
        """创建仅在进程内运行的 python-can 虚拟总线 handle。

        此工具只使用 python-can 的 virtual 后端，硬性限定为软件模拟，不会打开
        PCAN、Vector 或任何物理 CAN 设备。多个 handle 使用相同 channel 时可互相收发；使用不同 channel
        时相互隔离。先分别创建两个 handle，再使用 send_virtual_frame、
        receive_virtual_frame 或 forward_virtual_frame 做软件级联调。
        handle 必须唯一；如需重建，先断开原 handle。每个服务最多允许 16 个 handle。

        Args:
            handle: 本 MCP 服务内唯一的逻辑连接名，例如 "left" 或 "right"。
            channel: 虚拟网络名称；要互通的 handle 必须使用完全相同的名称。
            fd: 是否允许发送 CAN FD 帧；默认 false。此选项只控制本服务的发送校验。

        Returns:
            连接状态、handle、channel 和 FD 配置。
        """
        return self._buses.connect(handle, channel, fd)

    def disconnect_virtual_bus(self, handle: str) -> dict[str, str]:
        """关闭并释放指定的虚拟总线 handle，不影响其他 handle。

        Args:
            handle: 要关闭的逻辑连接名。

        Returns:
            status 为 disconnected 或 not_found。
        """
        return self._buses.disconnect(handle)

    def list_virtual_buses(self) -> list[dict[str, object]]:
        """列出当前进程内虚拟总线 handle、收发计数、trace 状态及故障注入计数。

        此列表仅包含 VirtualBus，不包含也不能操作 PCAN、Vector 等物理总线连接。

        Returns:
            每个 handle 的连接和 CAN FD 配置、channel、tx_count、rx_count
            与 cyclic_task_count。
        """
        return self._buses.list_buses()

    def configure_virtual_faults(
        self,
        handle: str,
        drop_rate: float = 0.0,
        max_delay_ms: int = 0,
        reorder_rate: float = 0.0,
        seed: int | None = None,
    ) -> dict[str, object]:
        """仅对指定 VirtualBus 接收端设置丢帧率、最大随机延迟和乱序率。

        速率范围均为 0–1，延迟上限为 0–60000 毫秒；seed 可复现随机决策。
        物理 PCAN/Vector 不受影响。
        """
        return self._buses.configure_fault_injection(
            handle, drop_rate, max_delay_ms, reorder_rate, seed
        )

    def disable_virtual_faults(self, handle: str) -> dict[str, object]:
        """关闭指定 VirtualBus 接收端的故障注入，并丢弃尚未交付的延迟帧。"""
        return self._buses.disable_fault_injection(handle)

    def start_frame_generator(
        self,
        handle: str,
        arbitration_id: int,
        mode: str = "counter",
        data: str = "00",
        period_ms: int = 100,
        is_extended_id: bool = False,
        is_fd: bool = False,
        bitrate_switch: bool = False,
        byte_index: int = 0,
        start_value: int = 0,
        step: int = 1,
        value_byte_offset: int = 0,
        value_byte_length: int = 1,
        seed: int | None = None,
        template: str | None = None,
    ) -> dict[str, object]:
        """仅在 VirtualBus 周期生成 counter/ramp/random/template 帧。

        template 可使用 {counter} 或 {value}，占位符每次替换为一个十六进制字节。
        周期范围沿用 start_cyclic 的 10–60000 毫秒限制。
        """
        payload = bytes.fromhex(data.replace(" ", ""))
        return self._buses.start_frame_generator(
            handle=handle,
            arbitration_id=arbitration_id,
            data=payload,
            period_ms=period_ms,
            mode=mode,
            is_extended_id=is_extended_id,
            is_fd=is_fd,
            bitrate_switch=bitrate_switch,
            byte_index=byte_index,
            start_value=start_value,
            step=step,
            value_byte_offset=value_byte_offset,
            value_byte_length=value_byte_length,
            seed=seed,
            template=template,
        )

    def start_virtual_trace(
        self,
        handle: str,
        arbitration_id: int | None = None,
        arbitration_id_mask: int | None = None,
        is_extended_id: bool | None = None,
        is_fd: bool | None = None,
        is_rx: bool | None = None,
    ) -> dict[str, str]:
        """开始指定 VirtualBus handle 的内存 trace；重启会清空旧缓存。

        支持按 arbitration_id 和掩码、扩展帧、CAN FD、收发方向筛选。
        """
        frame_filter = _make_virtual_filter(
            arbitration_id,
            arbitration_id_mask,
            is_extended_id,
            is_fd,
            is_rx,
        )
        return self._buses.start_trace(handle, frame_filter)

    def stop_virtual_trace(self, handle: str) -> dict[str, str]:
        """停止指定虚拟 handle 的 trace，保留缓存供查询、统计和导出。"""
        return self._buses.stop_trace(handle)

    def get_virtual_trace(
        self,
        handle: str,
        limit: int = 100,
        arbitration_id: int | None = None,
        arbitration_id_mask: int | None = None,
        is_extended_id: bool | None = None,
        is_fd: bool | None = None,
        is_rx: bool | None = None,
    ) -> CallToolResult:
        """按 ID/掩码和帧类型查询 VirtualBus trace。

        limit 必须大于 0；筛选条件仅作用于查询，不修改捕获缓存。
        """
        frame_filter = _make_virtual_filter(
            arbitration_id,
            arbitration_id_mask,
            is_extended_id,
            is_fd,
            is_rx,
        )
        frames = [
            frame.to_dict()
            for frame in self._buses.get_trace(handle, frame_filter, limit)
        ]
        content = {"result": frames}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(content))],
            structuredContent=content,
        )

    def get_virtual_trace_stats(
        self,
        handle: str,
        bitrate: int = 500_000,
    ) -> dict[str, object]:
        """统计虚拟 trace 每个 CAN ID 的帧数、周期、抖动和估算负载。

        bitrate 是估算负载使用的仲裁速率，默认 500000 bit/s；CAN FD
        双比特率和精确位填充不在此估算范围内。
        """
        return {
            "handle": handle,
            **self._buses.get_trace_stats(handle, bitrate),
        }

    def export_virtual_trace(
        self,
        handle: str,
        path: str | None = None,
        limit: int = 10_000,
        arbitration_id: int | None = None,
        arbitration_id_mask: int | None = None,
        is_extended_id: bool | None = None,
        is_fd: bool | None = None,
        is_rx: bool | None = None,
    ) -> dict[str, object]:
        """将虚拟 trace 导出到 uploads/ 下的 .asc、.blf 或 .csv 文件。

        path 仅接受文件名；省略时生成唯一的 .asc 文件。导出文件可通过
        download_file 获取，服务端物理路径对客户端不可见。

        Args:
            handle: 已开始过 trace 的虚拟总线 handle。
            path: uploads/ 沙箱内的文件名，扩展名必须为 .asc、.blf 或 .csv。
            limit: 最多导出的最新 trace 帧数，必须大于 0。
            arbitration_id: 可选 CAN ID 查询过滤条件。
            arbitration_id_mask: 可选 ID 位掩码，需配合 arbitration_id。
            is_extended_id: 可选扩展帧筛选。
            is_fd: 可选 CAN FD 筛选。
            is_rx: 可选收发方向筛选。

        Returns:
            uploads/ 相对文件标识、格式及写入帧数。
        """
        if limit <= 0:
            raise ValueError("limit must be greater than zero.")
        frame_filter = _make_virtual_filter(
            arbitration_id,
            arbitration_id_mask,
            is_extended_id,
            is_fd,
            is_rx,
        )
        if self._files is None:
            if path is None:
                raise ValueError("path is required when file sandbox is unavailable.")
            return self._buses.export_trace(handle, path, frame_filter, limit)
        destination, identifier = self._files.export_destination(path)
        result = self._buses.export_trace(
            handle, str(destination), frame_filter, limit
        )
        result["path"] = identifier
        return result

    async def expect_virtual_frame(
        self,
        handle: str,
        arbitration_id: int,
        timeout: float = 2.0,
        data: str | None = None,
        data_mask: str | None = None,
        is_extended_id: bool | None = None,
    ) -> dict[str, object]:
        """等待 VirtualBus 上匹配 ID 和可选数据/掩码的接收帧；超时明确报错。

        data 按字节前缀匹配。指定 data_mask 时逐位比较
        (actual & mask) == (data & mask)。
        """
        expected = bytes.fromhex(data.replace(" ", "")) if data is not None else None
        mask = bytes.fromhex(data_mask.replace(" ", "")) if data_mask else None
        frame = await asyncio.to_thread(
            self._buses.expect_frame,
            handle,
            arbitration_id,
            timeout,
            expected,
            mask,
            is_extended_id,
        )
        return {"status": "matched", "handle": handle, "frame": frame.to_dict()}

    def send_virtual_frame(
        self,
        handle: str,
        arbitration_id: int,
        data: str,
        is_extended_id: bool = False,
        is_fd: bool = False,
        bitrate_switch: bool = False,
    ) -> dict[str, object]:
        """        只向指定虚拟 channel 发送一帧；此工具仅支持进程内 VirtualBus，
        不接触 PCAN、Vector 或任何物理 CAN 硬件。

        Args:
            handle: 已创建的虚拟总线 handle。
            arbitration_id: CAN ID 整数值；标准帧不超过 0x7FF，扩展帧不超过
                0x1FFFFFFF。
            data: 十六进制 payload，例如 "02 10 03"；可带空格。
            is_extended_id: 是否使用 29 位扩展帧。
            is_fd: 是否发送 CAN FD 帧；handle 创建时必须设置 fd=true。
            bitrate_switch: CAN FD 的 BRS 标志。

        Returns:
            发送状态和帧内容。
        """
        frame = CanFrame(
            arbitration_id=arbitration_id,
            data=bytes.fromhex(data.replace(" ", "")),
            is_extended_id=is_extended_id,
            is_fd=is_fd,
            bitrate_switch=bitrate_switch,
        )
        validate_payload_length(frame.data, frame.is_fd)
        sent = self._buses.send(handle, frame)
        return {"status": "sent", "handle": handle, "frame": sent.to_dict()}

    async def uds_request(
        self,
        handle: str,
        request_id: int,
        response_id: int,
        data: str,
        timeout: float = 2.0,
        is_extended_id: bool = False,
        is_fd: bool = False,
        bitrate_switch: bool = False,
        p2_ms: int = 50,
        p2_star_ms: int = 5_000,
        s3_ms: int = 5_000,
        suppress_positive_response: bool = False,
    ) -> dict[str, object]:
        """通过 ISO-TP 在 VirtualBus 模拟网络上发送 UDS 请求并等待完整响应。

        此工具只使用指定的进程内 VirtualBus handle，绝不会连接或发送到 PCAN、
        Vector 或任何物理 CAN 硬件。需要先创建两个 channel 相同的虚拟 handle，
        并在对端模拟 ECU。请求和响应 CAN ID 均需显式指定；例如可发送
        data="22 F1 90" 读取模拟 ECU 的 DID。ISO-TP 自动处理单帧/多帧、流控和
        响应重组。等待响应期间不会阻塞其他 MCP 工具调用。仅支持普通物理寻址，
        不支持功能寻址及扩展寻址。P2/P2* 控制响应等待时限；S3 参数仅作为会话
        测试的配置元数据返回，不会替模拟 ECU 自动维持或切换会话。

        Args:
            handle: 已创建的虚拟总线 handle。
            request_id: 请求 CAN ID；标准 ID 范围 0x000–0x7FF，扩展 ID 范围
                0x00000000–0x1FFFFFFF。
            response_id: 模拟 ECU 返回响应时使用的 CAN ID。
            data: UDS payload 十六进制字节串，例如 "22 F1 90"，可带空格。
            timeout: ISO-TP 发送流控及接收响应的超时秒数，默认 2 秒。
            is_extended_id: 请求和响应是否使用 29 位扩展帧。
            is_fd: 是否使用 CAN FD；handle 创建时必须设置 fd=true。
            bitrate_switch: CAN FD 帧是否设置 BRS。
            p2_ms: 首个响应/NRC pending 的等待时间，毫秒，正整数，默认 50。
            p2_star_ms: 收到 NRC 0x78 后等待最终响应的时间，毫秒，默认 5000。
            s3_ms: S3 会话超时测试参数，毫秒，默认 5000；仅返回供测试使用。
            suppress_positive_response: 对 UDS 子功能字节设置抑制位 0x80；
                发送后不等待响应。

        Returns:
            完整重组后的 UDS response，response_data 为不带空格的小写十六进制。
            负响应也会原样返回，调用方可检查 7F 等 UDS 响应字节。

        Raises:
            ValueError: 请求 payload、CAN ID、timeout 或 CAN FD 配置无效。
            KeyError: handle 不存在。
            TimeoutError: 未在 timeout 内收到完整 ISO-TP 响应。
        """
        payload = bytes.fromhex(data.replace(" ", ""))
        return await asyncio.to_thread(
            self._buses.uds_request,
            handle=handle,
            request_id=request_id,
            response_id=response_id,
            payload=payload,
            timeout=timeout,
            is_extended_id=is_extended_id,
            is_fd=is_fd,
            bitrate_switch=bitrate_switch,
            p2_ms=p2_ms,
            p2_star_ms=p2_star_ms,
            s3_ms=s3_ms,
            suppress_positive_response=suppress_positive_response,
        )

    async def uds_functional_request(
        self,
        handle: str,
        data: str,
        request_id: int = 0x7DF,
        p2_ms: int = 50,
        p2_star_ms: int = 5_000,
        s3_ms: int = 5_000,
        suppress_positive_response: bool = False,
    ) -> dict[str, object]:
        """在 VirtualBus 上向模拟 ECU 广播功能寻址 UDS 请求并收集多 ECU 单帧响应。

        此工具仅用于进程内 VirtualBus 软件模拟，不会连接物理 CAN 车辆。
        当前功能寻址请求限制为经典 CAN ISO-TP 单帧（UDS payload 最多 7 字节）；
        响应收集窗口为 P2，NRC 0x78 会延长为 P2*。返回每个响应 CAN ID 的
        payload 和 NRC 名称。

        Args:
            handle: 已创建的 VirtualBus handle。
            data: UDS payload 十六进制字节串，例如 "19 02 FF"。
            request_id: 标准 CAN 广播 ID，默认 0x7DF。
            p2_ms: 首个/后续响应收集窗口，正整数毫秒。
            p2_star_ms: 收到 NRC 0x78 后的响应等待窗口，正整数毫秒。
            s3_ms: S3 会话测试参数，只作为元数据返回。
            suppress_positive_response: 设置 UDS 子功能抑制位；发送后不等待响应。

        Returns:
            response_count 和按 ECU 响应 CAN ID 索引的解码响应列表。
        """
        payload = bytes.fromhex(data.replace(" ", ""))
        return await asyncio.to_thread(
            self._buses.uds_functional_request,
            handle,
            request_id,
            payload,
            p2_ms,
            p2_star_ms,
            s3_ms,
            suppress_positive_response,
        )

    async def run_scenario(
        self,
        handle: str,
        scenario_json: str,
    ) -> dict[str, object]:
        """在 VirtualBus 上运行受限 JSON 场景，返回逐步 PASS/FAIL 报告。

        场景仅允许 send、wait、assert 三类步骤；最多 20 步、累计等待不超过
        60 秒。没有脚本、shell 命令、动态导入或物理 CAN 访问能力。

        scenario_json 示例:
        [{"action":"send","arbitration_id":291,"data":"01"},
         {"action":"wait","arbitration_id":2024,"timeout":1},
         {"action":"assert","field":"data","equals":"62 F1 90"}]

        Args:
            handle: 已创建的 VirtualBus handle。
            scenario_json: JSON 数组，每项为 send、wait 或 assert 步骤。

        Returns:
            包含总体 PASS/FAIL、通过步数及每一步结果的报告。
        """
        try:
            steps = json.loads(scenario_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"scenario_json is invalid JSON: {exc}") from exc
        if not isinstance(steps, list) or any(
            not isinstance(step, dict) for step in steps
        ):
            raise ValueError("scenario_json must be an array of step objects.")
        return await asyncio.to_thread(
            self._buses.run_scenario,
            handle,
            steps,
        )

    def start_cyclic(
        self,
        handle: str,
        arbitration_id: int,
        data: str,
        period_ms: int,
        is_extended_id: bool = False,
        is_fd: bool = False,
        bitrate_switch: bool = False,
    ) -> dict[str, object]:
        """在指定 VirtualBus handle 上持续周期发送模拟帧。

        此工具仅支持隔离的进程内 VirtualBus，不会连接或发送到 PCAN、
        Vector 或任何物理 CAN 设备。首帧立即发送，之后按 period_ms 周期发送；
        调用 stop_cyclic 停止任务，断开 handle 也会自动停止其任务。
        周期范围为 10–60000 毫秒。

        Args:
            handle: 已创建的虚拟总线 handle。
            arbitration_id: CAN ID 整数值；标准帧不超过 0x7FF，扩展帧不超过
                0x1FFFFFFF。
            data: 十六进制 payload，例如 "3E 80"；可带空格。
            period_ms: 发送周期，整数毫秒，范围 10–60000。
            is_extended_id: 是否使用 29 位扩展帧。
            is_fd: 是否发送 CAN FD 帧；handle 创建时必须设置 fd=true。
            bitrate_switch: CAN FD 的 BRS 标志。

        Returns:
            status="started"、可用于停止任务的 cyclic_id、handle、周期和帧。

        Raises:
            ValueError: 周期或报文格式无效。
            KeyError: handle 不存在。
        """
        frame = CanFrame(
            arbitration_id=arbitration_id,
            data=bytes.fromhex(data.replace(" ", "")),
            is_extended_id=is_extended_id,
            is_fd=is_fd,
            bitrate_switch=bitrate_switch,
        )
        validate_payload_length(frame.data, frame.is_fd)
        return self._buses.start_cyclic(handle, frame, period_ms)

    def stop_cyclic(self, cyclic_id: str) -> dict[str, object]:
        """停止指定的 VirtualBus 周期发送任务。

        Args:
            cyclic_id: start_cyclic 返回的任务 ID。

        Returns:
            status 为 stopped、error 或 not_found；后台发送失败时会返回错误信息。
        """
        return self._buses.stop_cyclic(cyclic_id)

    async def receive_virtual_frame(
        self,
        handle: str,
        timeout: float = 1.0,
    ) -> CallToolResult:
        """从指定虚拟 handle 接收一帧；超时返回 null。

        每个 handle 的接收队列相互独立，同一 channel 上另一个 handle 发送的
        报文会出现在此队列中。该工具只接收 VirtualBus 报文，不读取物理设备。

        Args:
            handle: 已创建的虚拟总线 handle。
            timeout: 最长等待秒数，必须大于或等于 0。

        Returns:
            同时包含 JSON 文本和结构化内容；structuredContent.result 为帧对象
            或 null。
        """
        if timeout < 0:
            raise ValueError("timeout must be greater than or equal to zero")
        frame = await asyncio.to_thread(self._buses.receive, handle, timeout)
        result = frame.to_dict() if frame is not None else None
        content = {"result": result}
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(content, ensure_ascii=False),
                )
            ],
            structuredContent=content,
        )

    async def forward_virtual_frame(
        self,
        source_handle: str,
        target_handle: str,
        timeout: float = 1.0,
        drain: bool = False,
    ) -> dict[str, object]:
        """从一个虚拟总线接收一帧，并仅转发这一帧到另一个虚拟总线。

        这是单次的软件级转发，不会创建后台转发任务；等待期间不会阻塞其他
        MCP 工具调用，可用于隔离的
        网关逻辑联调。源和目标必须是不同且已连接的 VirtualBus handle，
        只允许在 VirtualBus 之间转发，不支持也不会转发到 PCAN、Vector 或
        其他物理接口。发送由 target_handle 的 bus 完成，因此该 handle 自身
        不会收到回环副本；同一目标 channel 上其他 handle 可收到。默认按
        FIFO 转发源队列最早的帧；drain=true 会先丢弃当前排队的旧帧。源 handle
        不接收自己的发送帧，因此必须由同一源 channel 上的另一个 handle 发帧。

        Args:
            source_handle: 等待接收报文的虚拟总线 handle。
            target_handle: 发送转发报文的虚拟总线 handle。
            timeout: 等待源报文的最长秒数；超时返回 status="timeout"。
            drain: 是否先丢弃源 handle 当前排队的帧，等待并转发下一帧；
                默认 false。

        Returns:
            成功时包含原始接收帧和转发帧；超时则说明没有收到可转发报文。
        """
        if timeout < 0:
            raise ValueError("timeout must be greater than or equal to zero")
        return await asyncio.to_thread(
            self._buses.forward,
            source_handle,
            target_handle,
            timeout,
            drain,
        )
