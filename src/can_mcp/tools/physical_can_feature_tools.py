import asyncio
import json
import math
import threading
import time

from mcp.types import CallToolResult, TextContent

from can_mcp.core.models import CanFrame, validate_payload_length
from can_mcp.services.bus_service import BusService
from can_mcp.services.physical_bus_service import PhysicalBusService
from can_mcp.services.trace_service import TraceService
from can_mcp.services.virtual_bus_service import VirtualBusService


class PhysicalCanFeatureTools:
    MAX_TASK_DURATION_SECONDS = 60
    MIN_PHYSICAL_PERIOD_MS = 100
    READ_ONLY_UDS_SERVICES = frozenset({0x19, 0x22})

    def __init__(self, physical_buses: PhysicalBusService) -> None:
        self._physical_buses = physical_buses
        self._runtime = VirtualBusService()
        self._task_timers: dict[str, tuple[str, threading.Timer]] = {}
        self._lock = threading.RLock()
        physical_buses.add_lifecycle_listener(self._on_physical_lifecycle)

    def list_pcan_feature_status(self) -> list[dict[str, object]]:
        """列出已连接物理 CAN handle 的周期任务、trace 和本地接收故障注入状态。"""
        return self._runtime.list_buses()

    def fault_injection_metrics(self) -> dict[str, int]:
        metrics = self._runtime.metrics()
        return {
            name: metrics[name]
            for name in (
                "injected_dropped_frames",
                "injected_delayed_frames",
                "injected_reordered_pairs",
            )
        }

    def configure_pcan_receive_faults(
        self,
        handle: str,
        drop_rate: float = 0.0,
        max_delay_ms: int = 0,
        reorder_rate: float = 0.0,
        seed: int | None = None,
    ) -> dict[str, object]:
        """在指定物理 CAN handle 的本地接收视图注入丢帧、延迟或乱序。

        只影响本 MCP 服务向工具和 trace 提供的 RX 帧，不会在真实 CAN 线路
        注入错误、丢弃其他节点收到的帧或改变物理总线。
        """
        return self._runtime.configure_fault_injection(
            handle, drop_rate, max_delay_ms, reorder_rate, seed
        )

    def disable_pcan_receive_faults(self, handle: str) -> dict[str, object]:
        """关闭物理 CAN handle 的本地接收视图故障注入。"""
        return self._runtime.disable_fault_injection(handle)

    def start_pcan_cyclic(
        self,
        handle: str,
        arbitration_id: int,
        data: str,
        period_ms: int = 100,
        duration_seconds: int = 10,
        is_extended_id: bool = False,
        is_fd: bool = False,
        bitrate_switch: bool = False,
        confirm_physical_io: bool = False,
    ) -> dict[str, object]:
        """在真实 PCAN 或 Vector 总线上限时周期发送帧。

        每次调用必须显式设置 confirm_physical_io=true。周期不短于 100 ms，
        duration_seconds 限于 1–60 秒，到期自动停止；可提前调用
        stop_pcan_cyclic 停止。确认前请确保是隔离台架且 ID/payload 安全。
        """
        self._require_confirmation(confirm_physical_io)
        self._validate_duration(duration_seconds)
        if period_ms < self.MIN_PHYSICAL_PERIOD_MS:
            raise ValueError(
                f"Physical cyclic period must be at least "
                f"{self.MIN_PHYSICAL_PERIOD_MS} ms."
            )
        self._ensure_connected(handle)
        frame = self._make_frame(
            arbitration_id, data, is_extended_id, is_fd, bitrate_switch
        )
        task = self._runtime.start_cyclic(handle, frame, period_ms)
        self._schedule_expiry(handle, str(task["cyclic_id"]), duration_seconds)
        return {**task, "duration_seconds": duration_seconds}

    def start_pcan_frame_generator(
        self,
        handle: str,
        arbitration_id: int,
        mode: str = "counter",
        data: str = "00",
        period_ms: int = 100,
        duration_seconds: int = 10,
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
        confirm_physical_io: bool = False,
    ) -> dict[str, object]:
        """限时在真实 PCAN 或 Vector 总线上生成 counter/ramp/random/template 报文。

        每次调用必须显式设置 confirm_physical_io=true。发送周期至少 100 ms
        （最多 10 帧/秒），持续时间限于 1–60 秒，到期自动停止。
        """
        self._require_confirmation(confirm_physical_io)
        self._validate_duration(duration_seconds)
        if period_ms < self.MIN_PHYSICAL_PERIOD_MS:
            raise ValueError(
                f"Physical frame-generator period must be at least "
                f"{self.MIN_PHYSICAL_PERIOD_MS} ms."
            )
        self._ensure_connected(handle)
        result = self._runtime.start_frame_generator(
            handle=handle,
            arbitration_id=arbitration_id,
            data=bytes.fromhex(data.replace(" ", "")),
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
        self._schedule_expiry(handle, str(result["cyclic_id"]), duration_seconds)
        return {**result, "duration_seconds": duration_seconds}

    def stop_pcan_cyclic(
        self,
        handle: str,
        cyclic_id: str,
    ) -> dict[str, object]:
        """停止指定物理 CAN handle 上的限时周期/帧生成任务。"""
        with self._lock:
            entry = self._task_timers.get(cyclic_id)
            if entry is None or entry[0] != handle:
                return {"status": "not_found", "cyclic_id": cyclic_id}
            self._task_timers.pop(cyclic_id)
            entry[1].cancel()
        return self._runtime.stop_cyclic(cyclic_id)

    async def run_pcan_scenario(
        self,
        handle: str,
        scenario_json: str,
        confirm_physical_io: bool = False,
    ) -> dict[str, object]:
        """在真实 PCAN 或 Vector 上运行受限 send/wait/assert JSON 场景，最多 20 步/60 秒。

        每次调用都需要 confirm_physical_io=true。场景中的 send 步骤会真实
        发帧；仅可在隔离台架使用。
        """
        self._require_confirmation(confirm_physical_io)
        self._ensure_connected(handle)
        try:
            steps = json.loads(scenario_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"scenario_json is invalid JSON: {exc}") from exc
        if not isinstance(steps, list) or any(
            not isinstance(step, dict) for step in steps
        ):
            raise ValueError("scenario_json must be an array of step objects.")
        return await asyncio.to_thread(
            self._runtime.run_scenario,
            handle,
            steps,
        )

    async def forward_physical_frame(
        self,
        source_handle: str,
        target_handle: str,
        timeout: float = 1.0,
        drain: bool = False,
        confirm_physical_io: bool = False,
    ) -> CallToolResult:
        """将一帧从 PCAN/Vector 物理接收 handle 单次转发到另一个物理 handle。

        需要显式确认真实总线发送；不创建后台转发任务。error frame 不会转发。
        """
        self._require_confirmation(confirm_physical_io)
        if source_handle == target_handle:
            raise ValueError("source_handle and target_handle must be different.")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite number greater than or equal to zero.")
        self._ensure_connected(source_handle)
        self._ensure_connected(target_handle)
        source = self._physical_buses.get_bus(source_handle)
        target = self._physical_buses.get_bus(target_handle)
        result = await asyncio.to_thread(
            self._forward_one_frame,
            source,
            target,
            source_handle,
            target_handle,
            timeout,
            drain,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False),
                )
            ],
            structuredContent=result,
        )

    async def pcan_uds_request(
        self,
        handle: str,
        request_id: int,
        response_id: int,
        data: str,
        timeout: float = 2.0,
        is_extended_id: bool = False,
        is_fd: bool = False,
        bitrate_switch: bool = False,
        p2_ms: int = 1_000,
        p2_star_ms: int = 5_000,
        s3_ms: int = 5_000,
        confirm_physical_io: bool = False,
    ) -> dict[str, object]:
        """通过 PCAN 或 Vector/ISO-TP 发送 UDS 只读请求；需显式确认实体总线发送。

        仅允许 ReadDTCInformation (0x19) 与 ReadDataByIdentifier (0x22)，
        不支持 ECU 解锁、会话切换、写入、例程控制或复位服务。
        """
        self._require_confirmation(confirm_physical_io)
        payload = bytes.fromhex(data.replace(" ", ""))
        self._validate_read_only_uds(payload)
        self._ensure_connected(handle)
        return await asyncio.to_thread(
            self._runtime.uds_request,
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
        )

    async def pcan_uds_functional_request(
        self,
        handle: str,
        data: str,
        request_id: int = 0x7DF,
        p2_ms: int = 1_000,
        p2_star_ms: int = 5_000,
        s3_ms: int = 5_000,
        confirm_physical_io: bool = False,
    ) -> dict[str, object]:
        """通过 PCAN 或 Vector 向真实 CAN 总线发功能寻址 UDS 只读请求并收集响应。

        每次调用需显式确认；仅允许 UDS 0x19/0x22，payload 最多 7 字节。
        功能寻址可能触达多个 ECU，只能在隔离台架上使用。
        """
        self._require_confirmation(confirm_physical_io)
        payload = bytes.fromhex(data.replace(" ", ""))
        self._validate_read_only_uds(payload)
        self._ensure_connected(handle)
        return await asyncio.to_thread(
            self._runtime.uds_functional_request,
            handle,
            request_id,
            payload,
            p2_ms,
            p2_star_ms,
            s3_ms,
        )

    def _on_physical_lifecycle(
        self,
        event: str,
        handle: str,
        bus: BusService | None,
        trace: TraceService | None,
    ) -> None:
        if event == "disconnected":
            with self._lock:
                expired = [
                    cyclic_id
                    for cyclic_id, (task_handle, timer) in self._task_timers.items()
                    if task_handle == handle
                ]
                for cyclic_id in expired:
                    _, timer = self._task_timers.pop(cyclic_id)
                    timer.cancel()
            self._runtime.unregister_external_bus(handle)
            return
        if event != "connected" or bus is None or trace is None:
            raise ValueError(f"Unsupported physical bus lifecycle event: {event}.")
        config = bus.config
        if config is None:
            raise RuntimeError("Connected physical bus has no configuration.")
        if config.interface.lower() not in {"pcan", "vector"}:
            return
        self._runtime.register_external_bus(
            handle,
            str(config.channel),
            bus,
            trace,
        )

    def _ensure_connected(self, handle: str) -> None:
        if not self._runtime.has_handle(handle):
            raise KeyError(
                f"Physical CAN feature handle '{handle}' is not connected. "
                "Connect it with connect_can first."
            )
        status = self._physical_buses.get_status(handle)
        if not status["connected"] or status["interface"] not in {"pcan", "vector"}:
            raise ValueError(
                f"Handle '{handle}' is not a connected PCAN or Vector bus."
            )

    def _schedule_expiry(
        self,
        handle: str,
        cyclic_id: str,
        duration_seconds: int,
    ) -> None:
        timer = threading.Timer(
            duration_seconds,
            self._expire_task,
            args=(handle, cyclic_id),
        )
        timer.daemon = True
        with self._lock:
            self._task_timers[cyclic_id] = (handle, timer)
        timer.start()

    @staticmethod
    def _forward_one_frame(
        source: BusService,
        target: BusService,
        source_handle: str,
        target_handle: str,
        timeout: float,
        drain: bool,
    ) -> dict[str, object]:
        if not isinstance(drain, bool):
            raise ValueError("drain must be a boolean.")
        if drain:
            while source.receive(timeout=0) is not None:
                pass
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            frame = source.receive(timeout=remaining)
            if frame is None:
                return {
                    "status": "timeout",
                    "source_handle": source_handle,
                    "target_handle": target_handle,
                }
            if frame.is_rx is False or frame.is_error_frame:
                if remaining <= 0:
                    return {
                        "status": "timeout",
                        "source_handle": source_handle,
                        "target_handle": target_handle,
                    }
                continue
            sent = target.send(
                CanFrame(
                    arbitration_id=frame.arbitration_id,
                    data=frame.data,
                    is_extended_id=frame.is_extended_id,
                    is_fd=frame.is_fd,
                    bitrate_switch=frame.bitrate_switch,
                )
            )
            return {
                "status": "forwarded",
                "source_handle": source_handle,
                "target_handle": target_handle,
                "received_frame": frame.to_dict(),
                "sent_frame": sent.to_dict(),
            }

    def _expire_task(self, handle: str, cyclic_id: str) -> None:
        with self._lock:
            entry = self._task_timers.pop(cyclic_id, None)
        if entry is not None and entry[0] == handle:
            self._runtime.stop_cyclic(cyclic_id)

    @staticmethod
    def _make_frame(
        arbitration_id: int,
        data: str,
        is_extended_id: bool,
        is_fd: bool,
        bitrate_switch: bool,
    ) -> CanFrame:
        payload = bytes.fromhex(data.replace(" ", ""))
        validate_payload_length(payload, is_fd)
        return CanFrame(
            arbitration_id=arbitration_id,
            data=payload,
            is_extended_id=is_extended_id,
            is_fd=is_fd,
            bitrate_switch=bitrate_switch,
        )

    @staticmethod
    def _require_confirmation(confirmed: bool) -> None:
        if confirmed is not True:
            raise ValueError(
                "Physical CAN transmission requires confirm_physical_io=true."
            )

    @classmethod
    def _validate_duration(cls, duration_seconds: int) -> None:
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or not 1 <= duration_seconds <= cls.MAX_TASK_DURATION_SECONDS
        ):
            raise ValueError(
                f"duration_seconds must be an integer between 1 and "
                f"{cls.MAX_TASK_DURATION_SECONDS}."
            )

    @classmethod
    def _validate_read_only_uds(cls, payload: bytes) -> None:
        if not payload:
            raise ValueError("UDS request payload must not be empty.")
        if payload[0] not in cls.READ_ONLY_UDS_SERVICES:
            allowed = ", ".join(
                f"0x{service_id:02X}"
                for service_id in sorted(cls.READ_ONLY_UDS_SERVICES)
            )
            raise ValueError(
                f"Physical UDS only permits read-only service IDs: {allowed}."
            )
