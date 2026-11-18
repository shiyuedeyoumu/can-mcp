import asyncio
import math
import time
from typing import Any

from can_mcp.core.models import CanFrame, validate_payload_length
from can_mcp.services.database_service import DatabaseService
from can_mcp.services.file_transfer_service import FileTransferService
from can_mcp.services.physical_bus_service import PhysicalBusService
from can_mcp.services.virtual_bus_service import VirtualBusService


class DatabaseTools:
    def __init__(
        self,
        database_service: DatabaseService,
        virtual_bus_service: VirtualBusService,
        file_transfer_service: FileTransferService | None = None,
        physical_bus_service: PhysicalBusService | None = None,
    ) -> None:
        self._database = database_service
        self._virtual_buses = virtual_bus_service
        self._files = file_transfer_service
        self._physical_buses = physical_bus_service

    def load_database(self, path: str) -> Any:
        """加载 DBC 信号数据库或 CDD 诊断目录。

        DBC 使用 cantools 并支持信号编码/解码。CDD 解析 Vector CANdela XML，
        返回 ECU 信息、诊断服务、DID 和 DID 数据对象/编码元数据；使用
        encode_did/decode_did 可根据 CDD DID 定义编解码原始数据。DBS 暂无解析
        实现。DBC 与 CDD 互斥，加载其中一种会
        将其设为当前数据库。MCP 服务只接受 upload_finish 返回的 uploads/ 相对
        标识，不向客户端暴露服务端路径。

        Args:
            path: upload_finish 返回的 uploads/ 相对文件标识。

        Returns:
            DBC 的消息/信号清单，或 CDD 的服务/DID 目录及解析 warnings。

        Raises:
            FileNotFoundError: 文件不存在或不是普通文件。
            DatabaseFormatError: 文件格式未支持。
        """
        if self._files is None:
            result = self._database.load(path)
        else:
            resolved_path, identifier = self._files.resolve_file(path)
            result = self._database.load(str(resolved_path), display_path=identifier)
        if result.get("format") != "cdd":
            return result

        summary = self._database.summary()
        return {
            key: summary[key]
            for key in (
                "status",
                "format",
                "path",
                "dtd_version",
                "manufacturer",
                "ecu_name",
                "data_identifier_count",
                "service_count",
                "warnings",
            )
        } | {
            "data_identifiers_preview": [
                {
                    key: did[key]
                    for key in ("identifier", "identifier_hex", "name", "qualifier")
                }
                for did in result["data_identifiers"][:5]
            ],
            "services_preview": [
                {
                    key: service[key]
                    for key in ("name", "qualifier", "shortcut")
                }
                for service in result["services"][:5]
            ],
        }

    def list_diagnostic_services(
        self,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        """列出当前 CDD 中的诊断服务，可按名称/限定符搜索并分页。"""
        return self._database.list_diagnostic_services(search, offset, limit)

    def get_did(self, identifier: int) -> dict[str, object]:
        """按 16 位 DID 数值获取完整 CDD 数据对象和编码元数据。"""
        return self._database.get_data_identifier(identifier)

    def decode_did(self, identifier: int, data: str) -> dict[str, object]:
        """按当前 CDD 定义解码 DID 原始 payload，返回具名数据对象值。

        `data` 是不含 UDS 服务 ID 和 DID 字节的十六进制 payload。
        响应包含 raw field values；TEXTTBL 返回映射文本，LINCOMP 返回物理值，
        BCD 和 ASCII 返回字符串。字段键使用 CDD qualifier。
        """
        try:
            payload = bytes.fromhex(data.replace(" ", ""))
        except (AttributeError, ValueError) as exc:
            raise ValueError("data must be a hexadecimal byte string.") from exc
        return self._database.decode_data_identifier(identifier, payload)

    def encode_did(
        self,
        identifier: int,
        values: dict[str, Any],
    ) -> dict[str, object]:
        """将 CDD DID 的具名数据对象值编码为原始 payload；不发送 CAN 帧。"""
        return self._database.encode_data_identifier(identifier, values)

    def decode_frame(
        self,
        arbitration_id: int,
        data: str,
        is_extended_id: bool = False,
    ) -> dict[str, object]:
        """用当前已加载的 DBC 解码帧数据。

        Args:
            arbitration_id: DBC 中消息的 CAN ID。
            data: 十六进制 payload，可用空格分隔字节。
            is_extended_id: 是否使用 29 位扩展 ID。

        Returns:
            消息名、CAN ID、帧格式及按 DBC 缩放后的信号值。

        Raises:
            ValueError: DBC 消息包含超出其声明 payload 长度的信号。
        """
        return self._database.decode_frame(
            arbitration_id,
            bytes.fromhex(data.replace(" ", "")),
            is_extended_id,
        )

    def encode_frame(
        self,
        message_name: str,
        signals: dict[str, Any],
    ) -> dict[str, object]:
        """使用当前 DBC 将信号值编码为 CAN ID 和 payload；不发送。

        Args:
            message_name: DBC 消息名。
            signals: 信号名到物理值的映射；编码需要提供该消息全部必需信号。

        Returns:
            消息名、CAN ID、帧格式和十六进制 payload。
        """
        return self._database.encode_frame(message_name, signals)

    def send_signal(
        self,
        handle: str,
        message_name: str,
        signals: dict[str, Any],
        confirm_physical_io: bool = False,
    ) -> dict[str, object]:
        """将 DBC 信号编码并发送到指定 VirtualBus 或物理 CAN handle。

        Args:
            handle: 已创建的 VirtualBus、PCAN 或 Vector handle。
            message_name: DBC 消息名。
            signals: 信号名到物理值的映射；必须包含消息的全部必需信号。
            confirm_physical_io: 物理 handle 必须显式设为 true；VirtualBus 忽略此门禁。

        Returns:
            已发送的 CAN 帧和信号值。
        """
        encoded = self._database.encode_frame(message_name, signals)
        data = bytes.fromhex(str(encoded["data"]))
        is_fd = bool(encoded["is_fd"])
        validate_payload_length(data, is_fd)
        frame = CanFrame(
            arbitration_id=int(encoded["arbitration_id"]),
            data=data,
            is_extended_id=bool(encoded["is_extended_id"]),
            is_fd=is_fd,
        )
        if self._is_physical_handle(handle):
            if confirm_physical_io is not True:
                raise ValueError(
                    "Physical CAN transmission requires confirm_physical_io=true."
                )
            sent = self._physical_buses.get_bus(handle).send(frame)
        else:
            sent = self._virtual_buses.send(handle, frame)
        return {
            "status": "sent",
            "handle": handle,
            "message_name": message_name,
            "signals": signals,
            "frame": sent.to_dict(),
        }

    async def read_signal(
        self,
        handle: str,
        message_name: str,
        signal_name: str,
        timeout: float = 1.0,
    ) -> dict[str, object]:
        """从指定 VirtualBus、PCAN 或 Vector handle 等待并解码指定 DBC 信号。

        Args:
            handle: 已创建的 VirtualBus、PCAN 或 Vector handle。
            message_name: DBC 消息名；按该消息 CAN ID 等待接收帧。
            signal_name: 要返回的 DBC 信号名。
            timeout: 等待秒数，必须为有限正数。

        Returns:
            信号名、解码物理值以及对应的接收帧。
        """
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite number greater than zero.")
        message = self._database.message(message_name)
        if signal_name not in {signal.name for signal in message.signals}:
            raise KeyError(
                f"Signal '{signal_name}' is not present in message '{message_name}'."
            )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"No frame for DBC message '{message_name}' received within "
                    f"{timeout} seconds."
                )
            if self._is_physical_handle(handle):
                frame = await asyncio.to_thread(
                    self._physical_buses.get_bus(handle).receive,
                    remaining,
                )
            else:
                frame = await asyncio.to_thread(
                    self._virtual_buses.receive,
                    handle,
                    remaining,
                )
            if frame is None:
                raise TimeoutError(
                    f"No frame for DBC message '{message_name}' received within "
                    f"{timeout} seconds."
                )
            if (
                frame.arbitration_id != message.frame_id
                or frame.is_extended_id != message.is_extended_frame
            ):
                continue
            decoded = self._database.decode_frame(
                frame.arbitration_id,
                frame.data,
                frame.is_extended_id,
            )
            signals = decoded["signals"]
            if signal_name not in signals:
                raise KeyError(
                    f"Signal '{signal_name}' is not present in message '{message_name}'."
                )
            return {
                "handle": handle,
                "message_name": message_name,
                "signal_name": signal_name,
                "value": signals[signal_name],
                "frame": frame.to_dict(),
            }

    def _is_physical_handle(self, handle: str) -> bool:
        return (
            self._physical_buses is not None
            and self._physical_buses.has_handle(handle)
        )
