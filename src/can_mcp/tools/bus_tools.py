import asyncio
import json
import math
import time
from typing import Literal

from mcp.types import CallToolResult, TextContent

from can_mcp.adapters.can.base import CanConfig
from can_mcp.adapters.can.factory import create_can_adapter
from can_mcp.core.errors import BusNotConnectedError
from can_mcp.core.models import CanFrame, validate_payload_length
from can_mcp.services.bus_service import BusService
from can_mcp.services.physical_bus_service import PhysicalBusService


class BusTools:
    def __init__(self, bus_service: BusService | PhysicalBusService) -> None:
        self._bus = bus_service

    def connect_can(
        self,
        interface: Literal["pcan", "vector"],
        channel: str | int,
        bitrate: int = 500_000,
        fd: bool = False,
        data_bitrate: int | None = None,
        serial: int | None = None,
        app_name: str = "CANalyzer",
        listen_only: bool = False,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> dict[str, object]:
        """打开指定 handle 对应的硬件 CAN 总线。

        必须先枚举设备并根据结果选择接口和通道。PCAN 通道是名称，例如
        PCAN_USBBUS1；Vector 通道是从 0 开始的整数索引。可创建多个不同 handle，
        同时占用设备提供的不同物理通道；同一物理通道不能被多个 handle 占用。
        再次使用同一 handle 会切换该 handle 的连接。省略 handle 时使用 "default"，
        兼容旧版调用。Vector 可通过 serial 绑定特定设备。CAN FD 是否可用取决于设备和通道能力。
        listen_only=true 时启用硬件只听模式，不发送 ACK 或 CAN 报文；MCP 也会
        拒绝此连接上的发送请求。硬件和驱动必须支持该模式。

        Args:
            interface: 必填硬件后端；只能是 pcan 或 vector。
            channel: 必填通道。PCAN 传通道名（如 PCAN_USBBUS1），Vector 传
                enumerate_vector_devices 返回的整数 channel。
            bitrate: 仲裁/名义比特率，单位 bit/s；默认 500000。
            fd: 是否以 CAN FD 模式初始化硬件；默认 false。
            data_bitrate: CAN FD 数据段比特率，单位 bit/s。仅 fd=true 时使用；
                PCAN 未指定时默认采用 max(2000000, bitrate)，Vector 使用后端默认配置。
            serial: Vector 设备序列号；多台设备时用于锁定目标设备，可从枚举结果获取。
            app_name: Vector 应用配置名称；默认 CANalyzer。PCAN 忽略此参数。
            listen_only: 是否启用硬件只听模式；默认 false。PCAN 使用 PASSIVE 状态，
                Vector 使用 listen_only 驱动选项。
            handle: 本服务内唯一的连接名；默认 "default"。

        Returns:
            连接状态、handle、接口名称和通道。status 为 connected 表示首次连接，
            switched 表示该 handle 已切换到另一个设备或通道。

        Raises:
            ValueError: 接口名称或通道类型无效。
            can.CanError: 驱动、硬件或通道初始化失败。
        """
        if interface == "pcan" and fd and data_bitrate is None:
            data_bitrate = max(2_000_000, bitrate)
        config = CanConfig(
            interface=interface,
            channel=channel,
            bitrate=bitrate,
            fd=fd,
            data_bitrate=data_bitrate,
            serial=serial,
            app_name=app_name,
            listen_only=listen_only,
        )
        adapter = create_can_adapter(config)
        if isinstance(self._bus, PhysicalBusService):
            status = self._bus.connect(handle, adapter, config)
        else:
            status = self._bus.connect(adapter, config)
        result: dict[str, object] = {
            "status": status,
            "interface": interface,
            "channel": str(channel),
            "listen_only": listen_only,
        }
        if handle != PhysicalBusService.DEFAULT_HANDLE:
            result["handle"] = handle
        return result

    def disconnect_can(
        self,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> dict[str, str]:
        """关闭指定物理 CAN handle 并停止其接收监听器。

        断开后需重新调用 connect_can 才能收发报文。未连接时返回
        already_disconnected。省略 handle 时断开 "default"。

        Returns:
            status 为 disconnected 或 already_disconnected。
        """
        if isinstance(self._bus, PhysicalBusService):
            result = {"status": self._bus.disconnect(handle)}
            if handle != PhysicalBusService.DEFAULT_HANDLE:
                result["handle"] = handle
            return result
        return {"status": self._bus.disconnect()}

    def send_can_frame(
        self,
        arbitration_id: int,
        data: str,
        is_extended_id: bool = False,
        is_fd: bool = False,
        bitrate_switch: bool = False,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
        confirm_physical_io: bool = False,
    ) -> dict[str, object]:
        """在指定物理 CAN handle 上发送一帧经典 CAN 或 CAN FD 报文。

        必须先调用 connect_can，并且显式设置 confirm_physical_io=true。
        data 使用十六进制字节字符串，可包含空格，
        例如 "02 10 03" 或 "021003"；每个字节必须由两位十六进制数字表示。
        标准帧 ID 范围为 0x000–0x7FF，扩展帧 ID 范围为 0x00000000–0x1FFFFFFF。
        经典 CAN 最多 8 字节；CAN FD 的数据长度必须为 0–8、12、16、20、24、
        32、48 或 64 字节。CAN FD 还需在 connect_can 时以 fd=true 初始化，
        否则发送会被拒绝并提示重新连接。

        Args:
            arbitration_id: CAN 标识符的整数值。例如 291 表示十六进制 ID 0x123。
            data: 报文 payload 的十六进制字节串。
            is_extended_id: true 使用 29 位扩展 ID；false 使用 11 位标准 ID。
            is_fd: true 发送 CAN FD；false 发送经典 CAN。连接配置必须启用 FD，
                否则调用会报错而不会尝试发送。
            bitrate_switch: CAN FD 的 BRS 标志；仅 is_fd=true 时有效，true 请求
                使用更高的数据段速率。
            handle: 目标物理连接名；默认 "default"。
            confirm_physical_io: 确认本次操作会真实发送到物理 CAN 总线；必须为 true。

        Returns:
            status="sent" 和 frame。发送帧的 is_rx 为 false，timestamp 是本机
            成功提交发送后的时间戳，channel 是当前连接通道；data 为不带空格
            的小写十六进制字符串。

        Raises:
            ValueError: 未确认物理 I/O。
            ValueError: data 不是有效的十六进制字节串，或帧字段超出协议范围。
            BusNotConnectedError: 尚未连接总线。
            can.CanError: 驱动发送失败。
        """
        if confirm_physical_io is not True:
            raise ValueError(
                "Physical CAN transmission requires confirm_physical_io=true."
            )
        frame = CanFrame(
            arbitration_id=arbitration_id,
            data=bytes.fromhex(data.replace(" ", "")),
            is_extended_id=is_extended_id,
            is_fd=is_fd,
            bitrate_switch=bitrate_switch,
        )
        validate_payload_length(frame.data, frame.is_fd)
        sent_frame = self._bus_for(handle).send(frame)
        result: dict[str, object] = {
            "status": "sent",
            "frame": sent_frame.to_dict(),
        }
        if handle != PhysicalBusService.DEFAULT_HANDLE:
            result["handle"] = handle
        return result

    def receive_can_frame(
        self,
        timeout: float = 1.0,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> CallToolResult:
        """从指定 handle 的接收队列取出一帧；等待超时则返回 null。

        必须先调用 connect_can。此工具与 get_trace 使用同一条接收流：
        一帧被此工具取走后，不会影响已经保存到 trace 缓冲区的副本。
        尚未启动 trace 时，收到的报文仍可由此工具读取。

        Args:
            timeout: 最长等待秒数，必须大于或等于 0；默认 1 秒。
            handle: 接收队列所属连接名；默认 "default"。

        Returns:
            MCP 工具结果同时包含文本 JSON 和结构化内容。structuredContent.result
            是帧字典，包含 arbitration_id、data、timestamp、is_extended_id、
            is_fd、bitrate_switch、is_rx 和 channel；超时为 null。data 为不带空格
            的小写十六进制字符串。

        Raises:
            ValueError: timeout 小于 0。
            BusNotConnectedError: 尚未连接总线。
        """
        if timeout < 0:
            raise ValueError("timeout must be greater than or equal to zero")
        frame = self._receive_bus_for(handle).receive(timeout)
        result = frame.to_dict() if frame is not None else None
        content = TextContent(
            type="text",
            text=json.dumps({"result": result}, ensure_ascii=False),
        )
        return CallToolResult(
            content=[content],
            structuredContent={"result": result},
        )

    def receive_can_frames(
        self,
        max_frames: int = 1_000,
        timeout: float = 1.0,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> CallToolResult:
        """从指定物理连接批量读取接收队列中的帧。

        首帧最多等待 timeout 秒；随后立即排空当前可用队列，最多返回
        max_frames 帧。批量读取降低高流量下逐次 MCP 调用的开销。
        """
        if timeout < 0:
            raise ValueError("timeout must be greater than or equal to zero")
        frames = self._receive_bus_for(handle).receive_many(max_frames, timeout)
        result = [frame.to_dict() for frame in frames]
        content = TextContent(
            type="text",
            text=json.dumps({"result": result}, ensure_ascii=False),
        )
        return CallToolResult(
            content=[content],
            structuredContent={"result": result},
        )

    async def expect_frame(
        self,
        arbitration_id: int,
        timeout: float = 2.0,
        data: str | None = None,
        data_mask: str | None = None,
        is_extended_id: bool | None = None,
        handle: str = PhysicalBusService.DEFAULT_HANDLE,
    ) -> dict[str, object]:
        """等待指定物理 CAN handle 上符合条件的接收帧；只读，不发送报文。

        data 与 data_mask 为十六进制字节串。未指定 data_mask 时，data 按前缀
        完全匹配；指定掩码后逐字节比较 (actual & mask) == (data & mask)。
        等待期间通过后台线程接收，不阻塞其他 MCP 调用。
        handle 省略时使用 "default"。
        """
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite number greater than zero.")
        max_arbitration_id = 0x1FFFFFFF if is_extended_id else 0x7FF
        if (
            isinstance(arbitration_id, bool)
            or not isinstance(arbitration_id, int)
            or not 0 <= arbitration_id <= max_arbitration_id
        ):
            raise ValueError(
                f"arbitration_id must be between 0 and 0x{max_arbitration_id:X}."
            )
        expected_data = bytes.fromhex(data.replace(" ", "")) if data is not None else None
        mask = (
            bytes.fromhex(data_mask.replace(" ", ""))
            if data_mask is not None
            else None
        )
        if mask is not None and expected_data is None:
            raise ValueError("data_mask requires data.")
        if expected_data is not None and mask is not None and len(mask) != len(expected_data):
            raise ValueError("data and data_mask must have the same length.")

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Expected CAN frame 0x{arbitration_id:X} was not received "
                    f"within {timeout} seconds."
                )
            frame = await asyncio.to_thread(
                self._receive_bus_for(handle).receive, remaining
            )
            if frame is None:
                raise TimeoutError(
                    f"Expected CAN frame 0x{arbitration_id:X} was not received "
                    f"within {timeout} seconds."
                )
            if frame.arbitration_id != arbitration_id or frame.is_rx is False:
                continue
            if is_extended_id is not None and frame.is_extended_id != is_extended_id:
                continue
            if expected_data is not None:
                if len(frame.data) < len(expected_data):
                    continue
                data_mask_bytes = mask or bytes([0xFF]) * len(expected_data)
                if any(
                    (actual & bitmask) != (expected & bitmask)
                    for actual, expected, bitmask in zip(
                        frame.data,
                        expected_data,
                        data_mask_bytes,
                    )
                ):
                    continue
            result: dict[str, object] = {
                "status": "matched",
                "frame": frame.to_dict(),
            }
            if handle != PhysicalBusService.DEFAULT_HANDLE:
                result["handle"] = handle
            return result

    def _bus_for(self, handle: str) -> BusService:
        if isinstance(self._bus, PhysicalBusService):
            return self._bus.get_bus(handle)
        return self._bus

    def _receive_bus_for(self, handle: str) -> BusService:
        try:
            return self._bus_for(handle)
        except BusNotConnectedError as error:
            raise BusNotConnectedError(
                f"Physical CAN handle '{handle}' is not connected. "
                "This receive tool only supports physical CAN connections. "
                "For VirtualBus, use receive_virtual_frame, expect_virtual_frame, "
                "or the virtual trace tools."
            ) from error
