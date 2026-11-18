import logging
import math
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

import isotp

from can_mcp.adapters.can.base import CanConfig
from can_mcp.adapters.can.virtual import VirtualBusAdapter
from can_mcp.core.filters import FrameFilter
from can_mcp.core.models import CanFrame, validate_payload_length
from can_mcp.services.bus_service import BusService
from can_mcp.services.trace_analysis import calculate_trace_stats, export_trace_frames
from can_mcp.services.trace_service import TraceService
from can_mcp.services.virtual_fault_injector import VirtualFaultInjector


_LOGGER = logging.getLogger(__name__)
UDS_NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceivedResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
    0x81: "rpmTooHigh",
    0x82: "rpmTooLow",
    0x83: "engineIsRunning",
    0x84: "engineIsNotRunning",
    0x85: "engineRunTimeTooLow",
    0x86: "temperatureTooHigh",
    0x87: "temperatureTooLow",
    0x88: "vehicleSpeedTooHigh",
    0x89: "vehicleSpeedTooLow",
    0x8A: "throttleOrPedalTooHigh",
    0x8B: "throttleOrPedalTooLow",
    0x8C: "transmissionRangeNotInNeutral",
    0x8D: "transmissionRangeNotInGear",
    0x8F: "brakeSwitchNotClosed",
    0x90: "shifterLeverNotInPark",
    0x91: "torqueConverterClutchLocked",
    0x92: "voltageTooHigh",
    0x93: "voltageTooLow",
}


def _decode_uds_payload(payload: bytes) -> dict[str, object]:
    result: dict[str, object] = {"data": payload.hex()}
    if len(payload) >= 3 and payload[0] == 0x7F:
        nrc = payload[2]
        result["negative_response"] = {
            "request_service_id": payload[1],
            "nrc": nrc,
            "name": UDS_NRC_NAMES.get(nrc, "unknownNrc"),
        }
    return result


@dataclass
class _CyclicTask:
    task_id: str
    handle: str
    frame: CanFrame
    period_seconds: float
    frame_factory: Callable[[int], CanFrame] | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    error: str | None = None


class VirtualBusService:
    MAX_HANDLES = 16
    MAX_CYCLIC_TASKS = 64
    MIN_CYCLIC_PERIOD_MS = 10
    MAX_CYCLIC_PERIOD_MS = 60_000

    def __init__(self) -> None:
        self._buses: dict[str, tuple[str, BusService]] = {}
        self._traces: dict[str, TraceService] = {}
        self._cyclic_tasks: dict[str, _CyclicTask] = {}
        self._uds_locks: dict[str, threading.Lock] = {}
        self._fault_injectors: dict[str, VirtualFaultInjector] = {}
        self._external_handles: set[str] = set()
        self._retired_fault_counters: dict[str, dict[str, int]] = {}
        self._retired_metrics = {
            "tx_count": 0,
            "rx_count": 0,
            "error_frame_count": 0,
            "receive_buffer_dropped_count": 0,
            "injected_dropped_frames": 0,
            "injected_delayed_frames": 0,
            "injected_reordered_pairs": 0,
        }
        self._lock = threading.RLock()

    def connect(self, handle: str, channel: str, fd: bool = False) -> dict[str, object]:
        self._validate_name("handle", handle, 64)
        self._validate_name("channel", channel, 128)
        with self._lock:
            if handle in self._buses:
                raise ValueError(f"Virtual bus handle '{handle}' already exists.")
            if len(self._buses) >= self.MAX_HANDLES:
                raise ValueError(
                    f"Virtual bus handle limit ({self.MAX_HANDLES}) has been reached."
                )
            config = CanConfig(
                interface="virtual",
                channel=channel,
                fd=fd,
            )
            service = BusService()
            service.connect(VirtualBusAdapter(config), config)
            self._buses[handle] = (channel, service)
            self._traces[handle] = TraceService()
            self._uds_locks[handle] = threading.Lock()
            self._retired_fault_counters[handle] = {
                "injected_dropped_frames": 0,
                "injected_delayed_frames": 0,
                "injected_reordered_pairs": 0,
            }
        return {"status": "connected", "handle": handle, "channel": channel, "fd": fd}

    def register_external_bus(
        self,
        handle: str,
        channel: str,
        service: BusService,
        trace: TraceService,
    ) -> None:
        self._validate_name("handle", handle, 64)
        self._validate_name("channel", channel, 128)
        with self._lock:
            if handle in self._buses:
                raise ValueError(f"Bus handle '{handle}' is already registered.")
            self._buses[handle] = (channel, service)
            self._traces[handle] = trace
            self._uds_locks[handle] = threading.Lock()
            self._retired_fault_counters[handle] = {
                "injected_dropped_frames": 0,
                "injected_delayed_frames": 0,
                "injected_reordered_pairs": 0,
            }
            self._external_handles.add(handle)

    def unregister_external_bus(self, handle: str) -> None:
        with self._lock:
            if handle not in self._external_handles:
                return
        self._remove_handle(handle, disconnect_bus=False)

    def disconnect(self, handle: str) -> dict[str, str]:
        with self._lock:
            if handle in self._external_handles:
                raise ValueError(
                    "External bus handles must be disconnected by their owner."
                )
        return self._remove_handle(handle, disconnect_bus=True)

    def _remove_handle(
        self,
        handle: str,
        disconnect_bus: bool,
    ) -> dict[str, str]:
        with self._lock:
            uds_lock = self._uds_locks.get(handle)
        if uds_lock is None:
            return {"status": "not_found", "handle": handle}
        with uds_lock:
            with self._lock:
                tasks = [
                    task
                    for task in self._cyclic_tasks.values()
                    if task.handle == handle
                ]
                for task in tasks:
                    task.stop_event.set()
                for task in tasks:
                    if task.thread is not None:
                        task.thread.join()
                entry = self._buses.pop(handle, None)
                self._uds_locks.pop(handle, None)
                trace = self._traces.pop(handle, None)
                injector = self._fault_injectors.pop(handle, None)
                self._retired_fault_counters.pop(handle, None)
                self._external_handles.discard(handle)
                for task in tasks:
                    self._cyclic_tasks.pop(task.task_id, None)
                if entry is None:
                    return {"status": "not_found", "handle": handle}
                if trace is not None:
                    trace.stop()
                if injector is not None:
                    if entry is not None:
                        entry[1].set_receive_transform(None)
                    injector.stop()
                    self._add_retired_metrics(injector.counters())
                if entry is not None and disconnect_bus:
                    status = entry[1].get_status(trace_active=False)
                    self._add_retired_metrics(
                        {
                            name: int(status[name])
                            for name in (
                                "tx_count",
                                "rx_count",
                                "error_frame_count",
                                "receive_buffer_dropped_count",
                            )
                        }
                    )
                if disconnect_bus:
                    entry[1].disconnect()
        return {"status": "disconnected", "handle": handle}

    def uds_request(
        self,
        handle: str,
        request_id: int,
        response_id: int,
        payload: bytes,
        timeout: float,
        is_extended_id: bool = False,
        is_fd: bool = False,
        bitrate_switch: bool = False,
        p2_ms: int = 50,
        p2_star_ms: int = 5_000,
        s3_ms: int = 5_000,
        suppress_positive_response: bool = False,
    ) -> dict[str, object]:
        if not payload:
            raise ValueError("UDS request payload must not be empty.")
        self._validate_uds_timing(p2_ms, p2_star_ms, s3_ms)
        if not isinstance(suppress_positive_response, bool):
            raise ValueError("suppress_positive_response must be a boolean.")
        if suppress_positive_response:
            if len(payload) < 2:
                raise ValueError(
                    "Suppress-positive-response requires a UDS sub-function byte."
                )
            payload = payload[:1] + bytes([payload[1] | 0x80]) + payload[2:]
        if len(payload) > 4095:
            raise ValueError("UDS payload must not exceed 4095 bytes.")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite number greater than zero.")
        max_id = 0x1FFFFFFF if is_extended_id else 0x7FF
        for field_name, arbitration_id in (
            ("request_id", request_id),
            ("response_id", response_id),
        ):
            if (
                isinstance(arbitration_id, bool)
                or not isinstance(arbitration_id, int)
                or not 0 <= arbitration_id <= max_id
            ):
                raise ValueError(
                    f"{field_name} must be between 0 and 0x{max_id:X}."
                )

        with self._lock:
            uds_lock = self._uds_locks.get(handle)
        if uds_lock is None:
            raise KeyError(f"Virtual bus handle '{handle}' is not connected.")

        with uds_lock:
            service = self._get_service(handle)
            status = service.get_status(trace_active=False)
            if is_fd and not status["fd"]:
                raise ValueError(
                    "CAN FD ISO-TP requires a VirtualBus handle created with fd=true."
                )

            received_messages: queue.Queue[isotp.CanMessage] = queue.Queue()
            transport_errors: list[Exception] = []
            deadline = time.monotonic() + timeout

            def on_frame(frame: CanFrame) -> None:
                if (
                    not frame.is_error_frame
                    and frame.is_rx
                    and frame.arbitration_id == response_id
                    and frame.is_extended_id == is_extended_id
                ):
                    received_messages.put(
                        isotp.CanMessage(
                            arbitration_id=frame.arbitration_id,
                            dlc=len(frame.data),
                            data=frame.data,
                            extended_id=frame.is_extended_id,
                            is_fd=frame.is_fd,
                            bitrate_switch=frame.bitrate_switch,
                        )
                    )

            def receive_isotp_frame(
                read_timeout: float,
            ) -> isotp.CanMessage | None:
                try:
                    return received_messages.get(timeout=read_timeout)
                except queue.Empty:
                    return None

            def send_isotp_frame(message: isotp.CanMessage) -> None:
                service.send(
                    CanFrame(
                        arbitration_id=message.arbitration_id,
                        data=bytes(message.data),
                        is_extended_id=message.is_extended_id,
                        is_fd=message.is_fd,
                        bitrate_switch=message.bitrate_switch,
                    )
                )

            def on_transport_error(error: Exception) -> None:
                transport_errors.append(error)

            address_mode = (
                isotp.AddressingMode.Normal_29bits
                if is_extended_id
                else isotp.AddressingMode.Normal_11bits
            )
            transport = isotp.TransportLayer(
                rxfn=receive_isotp_frame,
                txfn=send_isotp_frame,
                address=isotp.Address(
                    addressing_mode=address_mode,
                    txid=request_id,
                    rxid=response_id,
                ),
                error_handler=on_transport_error,
                params={
                    "can_fd": is_fd,
                    "tx_data_length": 64 if is_fd else 8,
                    "bitrate_switch": bitrate_switch,
                    "blocking_send": True,
                    "max_frame_size": 4095,
                    "rx_flowcontrol_timeout": max(1, int(timeout * 1000)),
                    "rx_consecutive_frame_timeout": max(1, int(timeout * 1000)),
                },
            )
            service.subscribe(on_frame)
            try:
                transport.start()
                transport.send(
                    payload,
                    send_timeout=max(0.0, deadline - time.monotonic()),
                )
                if suppress_positive_response:
                    return {
                        "status": "sent_no_response_expected",
                        "handle": handle,
                        "request_id": request_id,
                        "response_id": response_id,
                        "request_data": payload.hex(),
                        "p2_ms": p2_ms,
                        "p2_star_ms": p2_star_ms,
                        "s3_ms": s3_ms,
                    }
                response = None
                response_deadline = min(
                    deadline,
                    time.monotonic() + p2_ms / 1000,
                )
                response_window = response_deadline - time.monotonic()
                response_window_name = "P2"
                response_started = time.monotonic()
                while response is None:
                    remaining = response_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    response = transport.recv(
                        block=True,
                        timeout=min(remaining, 0.05),
                    )
                    if (
                        response is not None
                        and len(response) >= 3
                        and response[0] == 0x7F
                        and response[2] == 0x78
                    ):
                        response = None
                        response_deadline = min(
                            deadline,
                            time.monotonic() + p2_star_ms / 1000,
                        )
                        response_window = response_deadline - time.monotonic()
                        response_window_name = "P2*"
                if response is None:
                    if transport_errors:
                        _LOGGER.warning(
                            "ISO-TP errors while waiting for response on handle %s: %s",
                            handle,
                            "; ".join(str(error) for error in transport_errors),
                        )
                    response_waited = time.monotonic() - response_started
                    raise TimeoutError(
                        f"No ISO-TP response received within "
                        f"{response_waited:.3f} seconds "
                        f"({response_window_name} response window; "
                        f"configured window {response_window:.3f} seconds, "
                        f"overall timeout {timeout:.3f} seconds)."
                    )
            finally:
                transport.stop()
                service.unsubscribe(on_frame)

        return {
            "status": "received",
            "handle": handle,
            "request_id": request_id,
            "response_id": response_id,
            "is_extended_id": is_extended_id,
            "is_fd": is_fd,
            "request_data": payload.hex(),
            "response_data": bytes(response).hex(),
            **_decode_uds_payload(bytes(response)),
            "p2_ms": p2_ms,
            "p2_star_ms": p2_star_ms,
            "s3_ms": s3_ms,
        }

    def uds_functional_request(
        self,
        handle: str,
        request_id: int,
        payload: bytes,
        p2_ms: int = 50,
        p2_star_ms: int = 5_000,
        s3_ms: int = 5_000,
        suppress_positive_response: bool = False,
    ) -> dict[str, object]:
        if not payload:
            raise ValueError("UDS request payload must not be empty.")
        self._validate_uds_timing(p2_ms, p2_star_ms, s3_ms)
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise ValueError("Functional request_id must be an integer.")
        if not 0 <= request_id <= 0x7FF:
            raise ValueError("Functional request_id must be a standard CAN ID.")
        if not isinstance(suppress_positive_response, bool):
            raise ValueError("suppress_positive_response must be a boolean.")
        if len(payload) > 7:
            raise ValueError(
                "Functional UDS requests are limited to 7 bytes (ISO-TP single frame)."
            )
        if suppress_positive_response:
            if len(payload) < 2:
                raise ValueError(
                    "Suppress-positive-response requires a UDS sub-function byte."
                )
            payload = payload[:1] + bytes([payload[1] | 0x80]) + payload[2:]

        service = self._get_service(handle)
        responses: dict[int, bytes] = {}
        response_event = threading.Event()
        response_lock = threading.Lock()
        started_at = time.monotonic()
        last_response_at = started_at

        def on_frame(frame: CanFrame) -> None:
            nonlocal last_response_at
            if (
                not frame.is_rx
                or frame.is_extended_id
                or frame.arbitration_id == request_id
                or not frame.data
            ):
                return
            pci_type = frame.data[0] >> 4
            if pci_type != 0:
                return
            payload_length = frame.data[0] & 0x0F
            if payload_length == 0 and len(frame.data) >= 2:
                payload_length = frame.data[1]
                payload_start = 2
            else:
                payload_start = 1
            if payload_length == 0 or payload_length > len(frame.data) - payload_start:
                return
            if payload_start == 2:
                response_data = frame.data[2 : 2 + payload_length]
            else:
                response_data = frame.data[1 : 1 + payload_length]
            if not response_data:
                return
            with response_lock:
                responses[frame.arbitration_id] = bytes(response_data)
                last_response_at = time.monotonic()
            response_event.set()

        service.subscribe(on_frame)
        try:
            request_frame = CanFrame(
                arbitration_id=request_id,
                data=bytes([len(payload)]) + payload,
            )
            sent = service.send(request_frame)
            if not suppress_positive_response:
                while True:
                    with response_lock:
                        has_responses = bool(responses)
                        has_pending = any(
                            len(response) >= 3
                            and response[0] == 0x7F
                            and response[2] == 0x78
                            for response in responses.values()
                        )
                        remaining_window = (
                            (p2_star_ms if has_pending else p2_ms) / 1000
                            - (
                                time.monotonic()
                                - (last_response_at if has_responses else started_at)
                            )
                        )
                    if remaining_window <= 0:
                        break
                    response_event.wait(min(remaining_window, 0.05))
                    response_event.clear()
                if not responses:
                    raise TimeoutError(
                        f"No functional UDS response received within {p2_ms} ms."
                    )
        finally:
            service.unsubscribe(on_frame)

        decoded = {
            str(arbitration_id): _decode_uds_payload(response)
            for arbitration_id, response in sorted(responses.items())
        }
        return {
            "status": "sent_no_response_expected"
            if suppress_positive_response
            else "responses_received",
            "handle": handle,
            "request_id": request_id,
            "request_data": payload.hex(),
            "tx_frame": sent.to_dict(),
            "response_count": len(decoded),
            "responses": decoded,
            "p2_ms": p2_ms,
            "p2_star_ms": p2_star_ms,
            "s3_ms": s3_ms,
        }

    @staticmethod
    def _validate_uds_timing(p2_ms: int, p2_star_ms: int, s3_ms: int) -> None:
        for name, value in (
            ("p2_ms", p2_ms),
            ("p2_star_ms", p2_star_ms),
            ("s3_ms", s3_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")

    def list_buses(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                {
                    **service.get_status(trace_active=False),
                    "handle": handle,
                    "channel": channel,
                    "cyclic_task_count": sum(
                        task.handle == handle
                        and task.thread is not None
                        and task.thread.is_alive()
                        and not task.stop_event.is_set()
                        for task in self._cyclic_tasks.values()
                    ),
                    "trace_active": self._traces[handle].active,
                    "trace_frames": self._traces[handle].frame_count,
                    "fault_injection": self.fault_injection_status(handle),
                }
                for handle, (channel, service) in self._buses.items()
            ]

    def has_handle(self, handle: str) -> bool:
        with self._lock:
            return handle in self._buses

    def fault_injection_status(self, handle: str) -> dict[str, object]:
        with self._lock:
            injector = self._fault_injectors.get(handle)
            retired_counters = dict(
                self._retired_fault_counters.get(
                    handle,
                    {
                        "injected_dropped_frames": 0,
                        "injected_delayed_frames": 0,
                        "injected_reordered_pairs": 0,
                    },
                )
            )
        if injector is None:
            return {
                "drop_rate": 0.0,
                "max_delay_ms": 0,
                "reorder_rate": 0.0,
                **retired_counters,
            }
        counters = injector.counters()
        for name, value in retired_counters.items():
            counters[name] += value
        return {
            **injector.configuration(),
            **counters,
        }

    def configure_fault_injection(
        self,
        handle: str,
        drop_rate: float = 0.0,
        max_delay_ms: int = 0,
        reorder_rate: float = 0.0,
        seed: int | None = None,
    ) -> dict[str, object]:
        service = self._get_service(handle)
        with self._lock:
            injector = self._fault_injectors.get(handle)
            if injector is None:
                injector = VirtualFaultInjector(
                    service.inject_received_frame,
                    drop_rate=drop_rate,
                    max_delay_ms=max_delay_ms,
                    reorder_rate=reorder_rate,
                    seed=seed,
                )
                self._fault_injectors[handle] = injector
                service.set_receive_transform(injector.process)
            else:
                if seed is not None:
                    raise ValueError(
                        "seed can only be set when fault injection is first configured."
                    )
                injector.configure(drop_rate, max_delay_ms, reorder_rate)
        return {
            "status": "configured",
            "handle": handle,
            **self.fault_injection_status(handle),
        }

    def disable_fault_injection(self, handle: str) -> dict[str, object]:
        service = self._get_service(handle)
        with self._lock:
            injector = self._fault_injectors.pop(handle, None)
            service.set_receive_transform(None)
        if injector is not None:
            injector.stop()
            counters = injector.counters()
            with self._lock:
                retired = self._retired_fault_counters.setdefault(
                    handle,
                    {
                        "injected_dropped_frames": 0,
                        "injected_delayed_frames": 0,
                        "injected_reordered_pairs": 0,
                    },
                )
                for name, value in counters.items():
                    retired[name] += value
            self._add_retired_metrics(counters)
        return {"status": "disabled", "handle": handle}

    def metrics(self) -> dict[str, int]:
        with self._lock:
            totals = dict(self._retired_metrics)
            for _, service in self._buses.values():
                status = service.get_status(trace_active=False)
                for name in (
                    "tx_count",
                    "rx_count",
                    "error_frame_count",
                    "receive_buffer_dropped_count",
                ):
                    totals[name] += int(status[name])
            for injector in self._fault_injectors.values():
                for name, value in injector.counters().items():
                    totals[name] += value
            return totals

    def _add_retired_metrics(self, values: dict[str, int]) -> None:
        with self._lock:
            for name, value in values.items():
                self._retired_metrics[name] += value

    def recent_traces(self, limit: int = 100) -> dict[str, list[dict[str, object]]]:
        with self._lock:
            traces = tuple(self._traces.items())
        return {
            handle: [frame.to_dict() for frame in trace.query(limit=limit)]
            for handle, trace in traces
            if trace.has_started
        }

    def decode_error_frame(self, handle: str) -> dict[str, object]:
        status = self._get_service(handle).get_status(trace_active=False)
        frame = status["latest_error_frame"]
        return {
            "handle": handle,
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

    def start_frame_generator(
        self,
        handle: str,
        arbitration_id: int,
        data: bytes,
        period_ms: int,
        mode: str,
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
        if mode not in {"counter", "ramp", "random", "template"}:
            raise ValueError("mode must be counter, ramp, random, or template.")
        if isinstance(byte_index, bool) or not isinstance(byte_index, int):
            raise ValueError("byte_index must be an integer.")
        if (
            isinstance(start_value, bool)
            or not isinstance(start_value, int)
            or start_value < 0
        ):
            raise ValueError("start_value must be a non-negative integer.")
        if isinstance(step, bool) or not isinstance(step, int):
            raise ValueError("step must be an integer.")
        if mode == "counter" and not 0 <= byte_index < len(data):
            raise ValueError("byte_index must identify a byte in the payload.")
        if mode == "ramp":
            if (
                isinstance(value_byte_offset, bool)
                or not isinstance(value_byte_offset, int)
                or isinstance(value_byte_length, bool)
                or not isinstance(value_byte_length, int)
                or value_byte_length < 1
                or value_byte_length > 4
                or value_byte_offset < 0
                or value_byte_offset + value_byte_length > len(data)
            ):
                raise ValueError(
                    "Ramp value byte range must fit within 1-4 payload bytes."
                )
        if mode == "template":
            if template is None:
                raise ValueError("template mode requires template text.")
            self._render_frame_template(template, 0)

        base_frame = CanFrame(
            arbitration_id=arbitration_id,
            data=data,
            is_extended_id=is_extended_id,
            is_fd=is_fd,
            bitrate_switch=bitrate_switch,
        )
        validate_payload_length(data, is_fd)
        rng = random.Random(seed)

        def make_frame(index: int) -> CanFrame:
            current_value = start_value + index * step
            if mode == "counter":
                payload = bytearray(data)
                payload[byte_index] = current_value & 0xFF
                generated = bytes(payload)
            elif mode == "ramp":
                payload = bytearray(data)
                value_bits = value_byte_length * 8
                value = current_value % (1 << value_bits)
                start = value_byte_offset
                end = start + value_byte_length
                payload[start:end] = value.to_bytes(value_byte_length, "big")
                generated = bytes(payload)
            elif mode == "random":
                generated = bytes(rng.getrandbits(8) for _ in data)
            else:
                generated = self._render_frame_template(template or "", current_value)
            validate_payload_length(generated, is_fd)
            return CanFrame(
                arbitration_id=arbitration_id,
                data=generated,
                is_extended_id=is_extended_id,
                is_fd=is_fd,
                bitrate_switch=bitrate_switch,
            )

        return self._start_cyclic_task(
            handle,
            base_frame,
            period_ms,
            lambda index: make_frame(index),
            mode=mode,
        )

    @staticmethod
    def _render_frame_template(template: str, value: int) -> bytes:
        rendered = template.replace("{counter}", f"{value & 0xFF:02x}").replace(
            "{value}", f"{value & 0xFF:02x}"
        )
        try:
            payload = bytes.fromhex(rendered.replace(" ", ""))
        except ValueError as exc:
            raise ValueError(
                "template must be hex bytes with optional {counter}/{value} "
                "byte placeholders."
            ) from exc
        validate_payload_length(payload, is_fd=True)
        return payload

    def start_trace(
        self,
        handle: str,
        frame_filter: FrameFilter | None = None,
    ) -> dict[str, str]:
        service = self._get_service(handle)
        trace = self._get_trace_service(handle)
        return {
            "status": trace.start(service, frame_filter),
            "handle": handle,
        }

    def stop_trace(self, handle: str) -> dict[str, str]:
        trace = self._get_trace_service(handle)
        return {"status": trace.stop(), "handle": handle}

    def get_trace(
        self,
        handle: str,
        frame_filter: FrameFilter | None = None,
        limit: int = 100,
    ) -> list[CanFrame]:
        return self._get_trace_service(handle).query(frame_filter, limit)

    def get_trace_stats(
        self,
        handle: str,
        bitrate: int = 500_000,
    ) -> dict[str, object]:
        frames = self._get_trace_service(handle).query(limit=10_000)
        return calculate_trace_stats(frames, bitrate)

    def export_trace(
        self,
        handle: str,
        path: str,
        frame_filter: FrameFilter | None = None,
        limit: int = 10_000,
    ) -> dict[str, object]:
        frames = self._get_trace_service(handle).query(frame_filter, limit)
        result = export_trace_frames(frames, path)
        result["handle"] = handle
        return result

    def expect_frame(
        self,
        handle: str,
        arbitration_id: int,
        timeout: float,
        data: bytes | None = None,
        data_mask: bytes | None = None,
        is_extended_id: bool | None = None,
    ) -> CanFrame:
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
        if data_mask is not None and data is None:
            raise ValueError("data_mask requires data.")
        if data is not None and data_mask is not None and len(data) != len(data_mask):
            raise ValueError("data and data_mask must have the same length.")

        service = self._get_service(handle)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Expected CAN frame 0x{arbitration_id:X} was not received "
                    f"within {timeout} seconds."
                )
            frame = service.receive(remaining)
            if frame is None:
                raise TimeoutError(
                    f"Expected CAN frame 0x{arbitration_id:X} was not received "
                    f"within {timeout} seconds."
                )
            if frame.arbitration_id != arbitration_id:
                continue
            if is_extended_id is not None and frame.is_extended_id != is_extended_id:
                continue
            if data is not None:
                if len(frame.data) < len(data):
                    continue
                mask = data_mask or bytes([0xFF]) * len(data)
                if any(
                    (actual & bitmask) != (expected & bitmask)
                    for actual, expected, bitmask in zip(frame.data, data, mask)
                ):
                    continue
            return frame

    def run_scenario(
        self,
        handle: str,
        steps: list[dict[str, object]],
    ) -> dict[str, object]:
        if not steps:
            raise ValueError("Scenario must contain at least one step.")
        if len(steps) > 20:
            raise ValueError("Scenario supports at most 20 steps.")
        total_wait_seconds = 0.0
        for index, step in enumerate(steps):
            action = step.get("action")
            if action not in {"send", "wait", "assert"}:
                raise ValueError(
                    f"Scenario step {index + 1} has unsupported action '{action}'."
                )
            if action == "wait":
                timeout = step.get("timeout", 1.0)
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout)
                    or timeout <= 0
                ):
                    raise ValueError(
                        f"Scenario step {index + 1} timeout must be finite and positive."
                    )
                total_wait_seconds += timeout
                arbitration_id = step.get("arbitration_id")
                extended = step.get("is_extended_id")
                if extended is not None and not isinstance(extended, bool):
                    raise ValueError(
                        f"Scenario step {index + 1} is_extended_id must be a boolean."
                    )
                max_id = 0x1FFFFFFF if extended is True else 0x7FF
                if (
                    isinstance(arbitration_id, bool)
                    or not isinstance(arbitration_id, int)
                    or not 0 <= arbitration_id <= max_id
                ):
                    raise ValueError(
                        f"Scenario step {index + 1} has an invalid arbitration_id."
                    )
                if step.get("data_mask") is not None and step.get("data") is None:
                    raise ValueError(
                        f"Scenario step {index + 1} data_mask requires data."
                    )
                if step.get("data") is not None:
                    try:
                        expected_data = bytes.fromhex(str(step["data"]).replace(" ", ""))
                        mask = (
                            bytes.fromhex(str(step["data_mask"]).replace(" ", ""))
                            if step.get("data_mask") is not None
                            else None
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"Scenario step {index + 1} has invalid data hex."
                        ) from exc
                    if mask is not None and len(mask) != len(expected_data):
                        raise ValueError(
                            f"Scenario step {index + 1} data and data_mask "
                            "must have the same length."
                        )
            elif action == "send":
                if step.get("is_extended_id", False) is not False and not isinstance(
                    step.get("is_extended_id"), bool
                ):
                    raise ValueError(
                        f"Scenario step {index + 1} is_extended_id must be a boolean."
                    )
            elif action == "assert":
                if step.get("field") not in {"arbitration_id", "data"}:
                    raise ValueError(
                        f"Scenario step {index + 1} assert field must be "
                        "'arbitration_id' or 'data'."
                    )
                if "equals" not in step:
                    raise ValueError(
                        f"Scenario step {index + 1} requires 'equals'."
                    )
                if (
                    step.get("field") == "arbitration_id"
                    and (
                        isinstance(step["equals"], bool)
                        or not isinstance(step["equals"], int)
                    )
                ):
                    raise ValueError(
                        f"Scenario step {index + 1} arbitration_id assertion "
                        "must be an integer."
                    )
                if step.get("field") == "data":
                    try:
                        bytes.fromhex(str(step["equals"]).replace(" ", ""))
                    except ValueError as exc:
                        raise ValueError(
                            f"Scenario step {index + 1} data assertion must be hex."
                        ) from exc
        if total_wait_seconds > 60:
            raise ValueError("Scenario total wait time must not exceed 60 seconds.")

        service = self._get_service(handle)
        report: list[dict[str, object]] = []
        last_frame: CanFrame | None = None
        for index, step in enumerate(steps):
            action = step["action"]
            try:
                if action == "send":
                    self._validate_scenario_send_step(index, step)
                    frame = CanFrame(
                        arbitration_id=int(step["arbitration_id"]),
                        data=bytes.fromhex(str(step["data"]).replace(" ", "")),
                        is_extended_id=bool(step.get("is_extended_id", False)),
                        is_fd=bool(step.get("is_fd", False)),
                        bitrate_switch=bool(step.get("bitrate_switch", False)),
                    )
                    validate_payload_length(frame.data, frame.is_fd)
                    sent = service.send(frame)
                    report.append(
                        {
                            "step": index + 1,
                            "action": action,
                            "status": "PASS",
                            "frame": sent.to_dict(),
                        }
                    )
                elif action == "wait":
                    last_frame = self.expect_frame(
                        handle=handle,
                        arbitration_id=int(step["arbitration_id"]),
                        timeout=float(step.get("timeout", 1.0)),
                        data=(
                            bytes.fromhex(str(step["data"]).replace(" ", ""))
                            if step.get("data") is not None
                            else None
                        ),
                        data_mask=(
                            bytes.fromhex(str(step["data_mask"]).replace(" ", ""))
                            if step.get("data_mask") is not None
                            else None
                        ),
                        is_extended_id=step.get("is_extended_id"),
                    )
                    report.append(
                        {
                            "step": index + 1,
                            "action": action,
                            "status": "PASS",
                            "frame": last_frame.to_dict(),
                        }
                    )
                else:
                    if last_frame is None:
                        raise ValueError("assert requires a preceding successful wait.")
                    field_name = str(step["field"])
                    actual: object = (
                        last_frame.arbitration_id
                        if field_name == "arbitration_id"
                        else last_frame.data.hex()
                    )
                    expected: object = step["equals"]
                    if field_name == "data":
                        expected = bytes.fromhex(str(expected).replace(" ", "")).hex()
                    if actual != expected:
                        raise AssertionError(
                            f"Expected {field_name}={expected!r}, got {actual!r}."
                        )
                    report.append(
                        {
                            "step": index + 1,
                            "action": action,
                            "status": "PASS",
                            "field": field_name,
                            "actual": actual,
                        }
                    )
            except Exception as exc:
                report.append(
                    {
                        "step": index + 1,
                        "action": action,
                        "status": "FAIL",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                return {
                    "status": "FAIL",
                    "handle": handle,
                    "passed_steps": index,
                    "total_steps": len(steps),
                    "steps": report,
                }
        return {
            "status": "PASS",
            "handle": handle,
            "passed_steps": len(report),
            "total_steps": len(steps),
            "steps": report,
        }

    @staticmethod
    def _validate_scenario_send_step(
        index: int,
        step: dict[str, object],
    ) -> None:
        arbitration_id = step.get("arbitration_id")
        is_extended_id = step.get("is_extended_id", False)
        if not isinstance(is_extended_id, bool):
            raise ValueError(
                f"Scenario step {index + 1} is_extended_id must be a boolean."
            )
        max_id = 0x1FFFFFFF if is_extended_id else 0x7FF
        if (
            isinstance(arbitration_id, bool)
            or not isinstance(arbitration_id, int)
            or not 0 <= arbitration_id <= max_id
        ):
            raise ValueError(
                f"Scenario step {index + 1} has an invalid arbitration_id."
            )
        data = step.get("data")
        if not isinstance(data, str):
            raise ValueError(f"Scenario step {index + 1} data must be a string.")
        try:
            payload = bytes.fromhex(data.replace(" ", ""))
            validate_payload_length(payload, bool(step.get("is_fd", False)))
        except ValueError as exc:
            raise ValueError(f"Scenario step {index + 1}: {exc}") from exc

    def _get_trace_service(self, handle: str) -> TraceService:
        with self._lock:
            trace = self._traces.get(handle)
        if trace is None:
            raise KeyError(f"Virtual bus handle '{handle}' is not connected.")
        return trace

    def send(self, handle: str, frame: CanFrame) -> CanFrame:
        return self._get_service(handle).send(frame)

    def start_cyclic(
        self,
        handle: str,
        frame: CanFrame,
        period_ms: int,
    ) -> dict[str, object]:
        return self._start_cyclic_task(handle, frame, period_ms)

    def _start_cyclic_task(
        self,
        handle: str,
        frame: CanFrame,
        period_ms: int,
        frame_factory: Callable[[int], CanFrame] | None = None,
        mode: str = "fixed",
    ) -> dict[str, object]:
        if isinstance(period_ms, bool) or not isinstance(period_ms, int):
            raise ValueError("period_ms must be an integer.")
        if not self.MIN_CYCLIC_PERIOD_MS <= period_ms <= self.MAX_CYCLIC_PERIOD_MS:
            raise ValueError(
                f"period_ms must be between {self.MIN_CYCLIC_PERIOD_MS} and "
                f"{self.MAX_CYCLIC_PERIOD_MS}."
            )
        with self._lock:
            service = self._get_service(handle)
            if len(self._cyclic_tasks) >= self.MAX_CYCLIC_TASKS:
                raise ValueError(
                    f"Virtual cyclic task limit ({self.MAX_CYCLIC_TASKS}) has been reached."
                )
            initial_frame = frame_factory(0) if frame_factory is not None else frame
            service.send(initial_frame)
            task_id = uuid4().hex
            task = _CyclicTask(
                task_id=task_id,
                handle=handle,
                frame=initial_frame,
                period_seconds=period_ms / 1000,
                frame_factory=frame_factory,
            )
            thread = threading.Thread(
                target=self._run_cyclic_task,
                args=(task, service),
                name=f"can-mcp-cyclic-{task_id[:8]}",
                daemon=True,
            )
            task.thread = thread
            self._cyclic_tasks[task_id] = task
            thread.start()
        return {
            "status": "started",
            "cyclic_id": task_id,
            "handle": handle,
            "period_ms": period_ms,
            "mode": mode,
            "frame": initial_frame.to_dict(),
        }

    def stop_cyclic(self, cyclic_id: str) -> dict[str, object]:
        with self._lock:
            task = self._cyclic_tasks.get(cyclic_id)
            if task is None:
                return {"status": "not_found", "cyclic_id": cyclic_id}
            task.stop_event.set()
        if task.thread is not None:
            task.thread.join()
        with self._lock:
            self._cyclic_tasks.pop(cyclic_id, None)
        result: dict[str, object] = {
            "status": "error" if task.error else "stopped",
            "cyclic_id": cyclic_id,
            "handle": task.handle,
        }
        if task.error is not None:
            result["error"] = task.error
        return result

    def receive(self, handle: str, timeout: float) -> CanFrame | None:
        return self._get_service(handle).receive(timeout)

    def forward(
        self,
        source_handle: str,
        target_handle: str,
        timeout: float,
        drain: bool = False,
    ) -> dict[str, object]:
        if source_handle == target_handle:
            raise ValueError("Source and target handles must be different.")
        source = self._get_service(source_handle)
        target = self._get_service(target_handle)
        drained_frames = 0
        if drain:
            while source.receive(timeout=0) is not None:
                drained_frames += 1
        received = source.receive(timeout)
        if received is None:
            return {
                "status": "timeout",
                "source_handle": source_handle,
                "target_handle": target_handle,
                "drained_frames": drained_frames,
            }
        forwarded = target.send(
            CanFrame(
                arbitration_id=received.arbitration_id,
                data=received.data,
                is_extended_id=received.is_extended_id,
                is_fd=received.is_fd,
                bitrate_switch=received.bitrate_switch,
            )
        )
        return {
            "status": "forwarded",
            "source_handle": source_handle,
            "target_handle": target_handle,
            "drained_frames": drained_frames,
            "received_frame": received.to_dict(),
            "forwarded_frame": forwarded.to_dict(),
        }

    def _get_service(self, handle: str) -> BusService:
        with self._lock:
            entry = self._buses.get(handle)
        if entry is None:
            raise KeyError(f"Virtual bus handle '{handle}' is not connected.")
        return entry[1]

    def _run_cyclic_task(self, task: _CyclicTask, service: BusService) -> None:
        frame_index = 1
        while not task.stop_event.wait(task.period_seconds):
            try:
                frame = (
                    task.frame_factory(frame_index)
                    if task.frame_factory is not None
                    else task.frame
                )
                service.send(frame)
                frame_index += 1
            except Exception as exc:
                task.error = f"{type(exc).__name__}: {exc}"
                _LOGGER.exception(
                    "Virtual cyclic task %s failed on handle %s.",
                    task.task_id,
                    task.handle,
                )
                task.stop_event.set()

    @staticmethod
    def _validate_name(field: str, value: str, max_length: int) -> None:
        if not value or not value.strip():
            raise ValueError(f"{field} must not be empty.")
        if len(value) > max_length:
            raise ValueError(f"{field} must be at most {max_length} characters.")
        if any(ord(character) < 32 for character in value):
            raise ValueError(f"{field} must not contain control characters.")
