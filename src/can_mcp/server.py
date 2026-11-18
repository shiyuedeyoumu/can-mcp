import asyncio
import json
import os
import re
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

from can_mcp import __version__
from can_mcp.config import load_config
from can_mcp.services.database_service import DatabaseService
from can_mcp.services.file_transfer_service import FileTransferService
from can_mcp.services.physical_bus_service import PhysicalBusService
from can_mcp.services.virtual_bus_service import VirtualBusService
from can_mcp.tools.bus_tools import BusTools
from can_mcp.tools.database_tools import DatabaseTools
from can_mcp.tools.device_tools import DeviceTools
from can_mcp.tools.file_tools import FileTools
from can_mcp.tools.physical_can_feature_tools import PhysicalCanFeatureTools
from can_mcp.tools.status_tools import StatusTools
from can_mcp.tools.trace_tools import TraceTools
from can_mcp.tools.virtual_bus_tools import VirtualBusTools


def _server_version() -> str:
    try:
        package_version = version("can-mcp")
    except PackageNotFoundError:
        package_version = __version__
    build_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    build_id = os.environ.get("CAN_MCP_BUILD", "unknown")
    if not re.fullmatch(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*", build_id):
        raise ValueError(
            "CAN_MCP_BUILD must contain only alphanumeric segments separated "
            "by dots or hyphens."
        )
    return f"{package_version}+{build_date}.{build_id}"


def create_server() -> FastMCP:
    config = load_config()
    physical_bus_service = PhysicalBusService()
    physical_can_feature_tools = PhysicalCanFeatureTools(physical_bus_service)
    bus_tools = BusTools(physical_bus_service)
    file_transfer_service = FileTransferService()
    trace_tools = TraceTools(
        physical_bus_service, None, file_transfer_service
    )
    database_service = DatabaseService()
    virtual_bus_service = VirtualBusService()
    database_tools = DatabaseTools(
        database_service,
        virtual_bus_service,
        file_transfer_service,
        physical_bus_service,
    )
    device_tools = DeviceTools()
    status_tools = StatusTools(
        physical_bus_service, None, virtual_bus_service
    )
    virtual_bus_tools = VirtualBusTools(
        virtual_bus_service, file_transfer_service
    )
    file_tools = FileTools(file_transfer_service)
    server = FastMCP(
        "can-mcp",
        host=config.server.host,
        port=config.server.port,
    )
    # FastMCP currently exposes no public server-version option.
    server._mcp_server.version = _server_version()

    server.tool()(bus_tools.connect_can)
    server.tool()(bus_tools.disconnect_can)
    server.tool()(bus_tools.send_can_frame)
    server.tool(structured_output=False)(bus_tools.receive_can_frame)
    server.tool(structured_output=False)(bus_tools.receive_can_frames)
    server.tool()(bus_tools.expect_frame)
    server.tool()(status_tools.get_status)
    server.tool()(status_tools.list_can_connections)
    server.tool()(physical_can_feature_tools.list_pcan_feature_status)
    server.tool()(physical_can_feature_tools.configure_pcan_receive_faults)
    server.tool()(physical_can_feature_tools.disable_pcan_receive_faults)
    server.tool()(physical_can_feature_tools.start_pcan_cyclic)
    server.tool()(physical_can_feature_tools.start_pcan_frame_generator)
    server.tool()(physical_can_feature_tools.stop_pcan_cyclic)
    server.tool()(physical_can_feature_tools.run_pcan_scenario)
    server.tool()(physical_can_feature_tools.pcan_uds_request)
    server.tool()(physical_can_feature_tools.pcan_uds_functional_request)
    server.tool(name="list_physical_can_feature_status")(
        physical_can_feature_tools.list_pcan_feature_status
    )
    server.tool(name="configure_physical_receive_faults")(
        physical_can_feature_tools.configure_pcan_receive_faults
    )
    server.tool(name="disable_physical_receive_faults")(
        physical_can_feature_tools.disable_pcan_receive_faults
    )
    server.tool(name="start_physical_cyclic")(
        physical_can_feature_tools.start_pcan_cyclic
    )
    server.tool(name="start_physical_frame_generator")(
        physical_can_feature_tools.start_pcan_frame_generator
    )
    server.tool(name="stop_physical_cyclic")(
        physical_can_feature_tools.stop_pcan_cyclic
    )
    server.tool(name="run_physical_scenario")(
        physical_can_feature_tools.run_pcan_scenario
    )
    server.tool(name="physical_uds_request")(
        physical_can_feature_tools.pcan_uds_request
    )
    server.tool(name="physical_uds_functional_request")(
        physical_can_feature_tools.pcan_uds_functional_request
    )
    server.tool(structured_output=False)(
        physical_can_feature_tools.forward_physical_frame
    )
    server.tool()(trace_tools.start_trace)
    server.tool()(trace_tools.stop_trace)
    server.tool(structured_output=False)(trace_tools.get_trace)
    server.tool()(trace_tools.get_trace_stats)
    server.tool()(trace_tools.export_trace)
    @server.tool(
        name="load_database",
        description=database_tools.load_database.__doc__,
    )
    def load_database_result(path: str) -> CallToolResult:
        result = database_tools.load_database(path)
        summary = {
            "status": result["status"],
            "format": result["format"],
            "path": result["path"],
            "warnings": result["warnings"],
        }
        if result.get("format") == "cdd":
            summary.update(
                {
                    "ecu_name": result["ecu_name"],
                    "data_identifier_count": result["data_identifier_count"],
                    "service_count": result["service_count"],
                    "data_identifiers_preview": [
                        item["name"] for item in result["data_identifiers_preview"]
                    ],
                    "services_preview": [
                        item["shortcut"] or item["qualifier"]
                        for item in result["services_preview"]
                    ],
                }
            )
        else:
            summary.update(
                {
                    "message_count": result["message_count"],
                    "messages": [
                        message["name"] for message in result["messages"][:5]
                    ],
                }
            )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(summary, ensure_ascii=False),
                )
            ],
            structuredContent=result,
        )

    server.tool()(database_tools.list_diagnostic_services)
    server.tool()(database_tools.get_did)
    server.tool()(database_tools.decode_did)
    server.tool()(database_tools.encode_did)
    server.tool()(database_tools.decode_frame)
    server.tool()(database_tools.encode_frame)
    server.tool()(database_tools.send_signal)
    server.tool()(database_tools.read_signal)
    server.tool()(device_tools.enumerate_pcan_devices)
    server.tool()(device_tools.enumerate_vector_devices)
    server.tool()(virtual_bus_tools.connect_virtual_bus)
    server.tool()(virtual_bus_tools.disconnect_virtual_bus)
    server.tool()(virtual_bus_tools.list_virtual_buses)
    server.tool()(virtual_bus_tools.send_virtual_frame)
    server.tool()(virtual_bus_tools.start_virtual_trace)
    server.tool()(virtual_bus_tools.stop_virtual_trace)
    server.tool(structured_output=False)(virtual_bus_tools.get_virtual_trace)
    server.tool()(virtual_bus_tools.get_virtual_trace_stats)
    server.tool()(virtual_bus_tools.export_virtual_trace)
    server.tool()(virtual_bus_tools.expect_virtual_frame)
    server.tool()(virtual_bus_tools.uds_request)
    server.tool()(virtual_bus_tools.uds_functional_request)
    server.tool()(virtual_bus_tools.run_scenario)
    server.tool()(virtual_bus_tools.start_cyclic)
    server.tool()(virtual_bus_tools.stop_cyclic)
    server.tool()(virtual_bus_tools.configure_virtual_faults)
    server.tool()(virtual_bus_tools.disable_virtual_faults)
    server.tool()(virtual_bus_tools.start_frame_generator)
    server.tool(structured_output=False)(virtual_bus_tools.receive_virtual_frame)
    server.tool()(virtual_bus_tools.forward_virtual_frame)
    server.tool()(status_tools.decode_error_frame)
    server.tool()(file_tools.upload_begin)
    server.tool()(file_tools.upload_chunk)
    server.tool()(file_tools.upload_finish)
    server.tool()(file_tools.download_file)
    server.tool()(file_tools.list_files)

    @server.resource(
        "can://status",
        mime_type="application/json",
        description=(
            "默认物理 CAN 连接状态、所有物理及 VirtualBus handle 的状态、计数、"
            "trace 和虚拟故障注入信息。"
        ),
    )
    def status_resource() -> str:
        return json.dumps(
            {
                "physical": status_tools.get_status(),
                "physical_buses": status_tools.list_can_connections(),
                "physical_can_features": (
                    physical_can_feature_tools.list_pcan_feature_status()
                ),
                "pcan_features": physical_can_feature_tools.list_pcan_feature_status(),
                "virtual_buses": virtual_bus_service.list_buses(),
            },
            ensure_ascii=False,
        )

    @server.resource(
        "can://recent-trace",
        mime_type="application/json",
        description="最近捕获的物理和虚拟总线 trace；每个 trace 最多返回 100 帧。",
    )
    def recent_trace_resource() -> str:
        return json.dumps(
            {
                "physical": physical_bus_service.trace_frames(limit=100),
                "virtual": virtual_bus_service.recent_traces(limit=100),
            },
            ensure_ascii=False,
        )

    @server.resource(
        "can://dbc-summary",
        mime_type="application/json",
        description="当前加载的 DBC 消息和信号摘要；未加载时返回 not_loaded 状态。",
    )
    def dbc_summary_resource() -> str:
        return json.dumps(database_service.summary(), ensure_ascii=False)

    @server.custom_route("/metrics", methods=["GET"])
    async def metrics_route(_: Request) -> Response:
        physical_metrics = physical_bus_service.metrics()
        physical_fault_metrics = physical_can_feature_tools.fault_injection_metrics()
        virtual_metrics = virtual_bus_service.metrics()
        virtual_buses = virtual_bus_service.list_buses()
        counters = {
            "can_mcp_tx_frames_total": physical_metrics["tx_count"]
            + virtual_metrics["tx_count"],
            "can_mcp_rx_frames_total": physical_metrics["rx_count"]
            + virtual_metrics["rx_count"],
            "can_mcp_error_frames_total": physical_metrics["error_frame_count"]
            + virtual_metrics["error_frame_count"],
            "can_mcp_receive_buffer_dropped_frames_total": physical_metrics[
                "receive_buffer_dropped_count"
            ]
            + virtual_metrics["receive_buffer_dropped_count"],
            "can_mcp_virtual_injected_dropped_frames_total": virtual_metrics.get(
                "injected_dropped_frames", 0
            ),
            "can_mcp_virtual_injected_delayed_frames_total": virtual_metrics.get(
                "injected_delayed_frames", 0
            ),
            "can_mcp_virtual_injected_reordered_pairs_total": virtual_metrics.get(
                "injected_reordered_pairs", 0
            ),
        }
        lines = [
            "# TYPE can_mcp_tx_frames_total counter",
            f"can_mcp_tx_frames_total {counters['can_mcp_tx_frames_total']}",
            "# TYPE can_mcp_rx_frames_total counter",
            f"can_mcp_rx_frames_total {counters['can_mcp_rx_frames_total']}",
            "# TYPE can_mcp_error_frames_total counter",
            f"can_mcp_error_frames_total {counters['can_mcp_error_frames_total']}",
            "# TYPE can_mcp_receive_buffer_dropped_frames_total counter",
            "can_mcp_receive_buffer_dropped_frames_total "
            f"{counters['can_mcp_receive_buffer_dropped_frames_total']}",
            "# TYPE can_mcp_receive_buffer_dropped_oldest_frames_total counter",
            "can_mcp_receive_buffer_dropped_oldest_frames_total "
            f"{physical_metrics['receive_buffer_dropped_oldest_count']}",
            "# TYPE can_mcp_receive_buffer_dropped_newest_frames_total counter",
            "can_mcp_receive_buffer_dropped_newest_frames_total "
            f"{physical_metrics['receive_buffer_dropped_newest_count']}",
            "# TYPE can_mcp_physical_trace_overwritten_frames gauge",
            "can_mcp_physical_trace_overwritten_frames "
            f"{physical_metrics['trace_overwritten_count']}",
            "# TYPE can_mcp_virtual_injected_dropped_frames_total counter",
            "can_mcp_virtual_injected_dropped_frames_total "
            f"{counters['can_mcp_virtual_injected_dropped_frames_total']}",
            "# TYPE can_mcp_virtual_injected_delayed_frames_total counter",
            "can_mcp_virtual_injected_delayed_frames_total "
            f"{counters['can_mcp_virtual_injected_delayed_frames_total']}",
            "# TYPE can_mcp_virtual_injected_reordered_pairs_total counter",
            "can_mcp_virtual_injected_reordered_pairs_total "
            f"{counters['can_mcp_virtual_injected_reordered_pairs_total']}",
            "# TYPE can_mcp_pcan_injected_dropped_frames_total counter",
            "can_mcp_pcan_injected_dropped_frames_total "
            f"{physical_fault_metrics['injected_dropped_frames']}",
            "# TYPE can_mcp_pcan_injected_delayed_frames_total counter",
            "can_mcp_pcan_injected_delayed_frames_total "
            f"{physical_fault_metrics['injected_delayed_frames']}",
            "# TYPE can_mcp_pcan_injected_reordered_pairs_total counter",
            "can_mcp_pcan_injected_reordered_pairs_total "
            f"{physical_fault_metrics['injected_reordered_pairs']}",
            "# TYPE can_mcp_physical_injected_dropped_frames_total counter",
            "can_mcp_physical_injected_dropped_frames_total "
            f"{physical_fault_metrics['injected_dropped_frames']}",
            "# TYPE can_mcp_physical_injected_delayed_frames_total counter",
            "can_mcp_physical_injected_delayed_frames_total "
            f"{physical_fault_metrics['injected_delayed_frames']}",
            "# TYPE can_mcp_physical_injected_reordered_pairs_total counter",
            "can_mcp_physical_injected_reordered_pairs_total "
            f"{physical_fault_metrics['injected_reordered_pairs']}",
            "# TYPE can_mcp_virtual_bus_handles gauge",
            f"can_mcp_virtual_bus_handles {len(virtual_buses)}",
            "# TYPE can_mcp_virtual_trace_frames gauge",
            "can_mcp_virtual_trace_frames "
            f"{sum(int(bus['trace_frames']) for bus in virtual_buses)}",
            "# TYPE can_mcp_physical_trace_frames gauge",
            f"can_mcp_physical_trace_frames "
            f"{physical_bus_service.trace_frame_count()}",
        ]
        return Response(
            "\n".join(lines) + "\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
    return server


async def _run_streamable_http(server: FastMCP) -> None:
    config = uvicorn.Config(
        server.streamable_http_app(),
        host=server.settings.host,
        port=server.settings.port,
        log_level=server.settings.log_level.lower(),
        timeout_graceful_shutdown=5,
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    asyncio.run(_run_streamable_http(create_server()))
