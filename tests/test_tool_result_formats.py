import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from can_mcp.adapters.can.base import CanConfig
from can_mcp.core.models import CanFrame
from can_mcp.server import _run_streamable_http, create_server
from can_mcp.tools.bus_tools import BusTools
from can_mcp.tools.trace_tools import TraceTools


def test_receive_tool_returns_text_and_structured_content() -> None:
    frame = CanFrame(arbitration_id=0x123, data=b"\x01\x02", is_rx=True)
    tools = BusTools(SimpleNamespace(receive=lambda timeout: frame))

    result = tools.receive_can_frame()

    assert result.structuredContent == {"result": frame.to_dict()}
    assert json.loads(result.content[0].text) == result.structuredContent


def test_streamable_http_server_has_bounded_graceful_shutdown(monkeypatch) -> None:
    captured = {}

    class Config:
        def __init__(self, app, **kwargs) -> None:
            captured["app"] = app
            captured.update(kwargs)

    class Server:
        def __init__(self, config) -> None:
            captured["config"] = config

        async def serve(self) -> None:
            captured["served"] = True

    monkeypatch.setattr("can_mcp.server.uvicorn.Config", Config)
    monkeypatch.setattr("can_mcp.server.uvicorn.Server", Server)
    app = object()
    server = SimpleNamespace(
        settings=SimpleNamespace(
            host="127.0.0.1",
            port=13399,
            log_level="INFO",
        ),
        streamable_http_app=lambda: app,
    )

    asyncio.run(_run_streamable_http(server))

    assert captured["app"] is app
    assert captured["timeout_graceful_shutdown"] == 5
    assert captured["served"] is True


def test_receive_many_tool_returns_text_and_structured_content() -> None:
    frames = [
        CanFrame(arbitration_id=0x123, data=b"\x01", is_rx=True),
        CanFrame(arbitration_id=0x124, data=b"\x02", is_rx=True),
    ]
    tools = BusTools(SimpleNamespace(receive_many=lambda max_frames, timeout: frames))

    result = tools.receive_can_frames()

    assert result.structuredContent == {
        "result": [frame.to_dict() for frame in frames]
    }
    assert json.loads(result.content[0].text) == result.structuredContent


def test_trace_tool_returns_text_and_structured_content() -> None:
    frames = [CanFrame(arbitration_id=0x123, data=b"\x01", is_rx=False)]
    tools = TraceTools(
        SimpleNamespace(),
        SimpleNamespace(query=lambda frame_filter, limit: frames),
    )

    result = tools.get_trace()

    assert result.structuredContent == {"result": [frame.to_dict() for frame in frames]}
    assert json.loads(result.content[0].text) == result.structuredContent


def test_backward_compatible_tools_have_no_generated_output_schema() -> None:
    server = create_server()

    for name in ("receive_can_frame", "receive_can_frames", "get_trace"):
        tool = server._tool_manager.get_tool(name)
        assert tool is not None
        assert tool.fn_metadata.output_schema is None


def test_load_database_always_returns_structured_content_on_repeated_calls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "sample.dbc"
    (uploads / "sample.dbc").write_bytes(fixture.read_bytes())
    monkeypatch.chdir(tmp_path)

    server = create_server()
    tool = server._tool_manager.get_tool("load_database")
    assert tool is not None

    async def invoke():
        return await tool.run(
            {"path": "uploads/sample.dbc"},
            convert_result=True,
        )

    first = asyncio.run(invoke())
    second = asyncio.run(invoke())

    assert first.structuredContent is not None
    assert second.structuredContent == first.structuredContent
    assert first.structuredContent["message_count"] == 2
    assert first.structuredContent["path"] == "uploads/sample.dbc"
    assert str(uploads) not in first.content[0].text
    first_text = json.loads(first.content[0].text)
    second_text = json.loads(second.content[0].text)
    expected_text = {
        "status": first.structuredContent["status"],
        "format": first.structuredContent["format"],
        "path": first.structuredContent["path"],
        "message_count": first.structuredContent["message_count"],
        "warnings": first.structuredContent["warnings"],
        "messages": [
            message["name"] for message in first.structuredContent["messages"][:5]
        ],
    }
    assert first_text == second_text == expected_text
    assert len(first.content[0].text) < 1_000
    assert len(first.structuredContent["messages"]) == 2


@pytest.mark.skipif(
    not Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.40.cdd").is_file(),
    reason="Optional vehicle-specific CDD fixture is not included.",
)
def test_load_database_returns_cdd_catalog_in_structured_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    fixture = Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.40.cdd")
    (uploads / fixture.name).write_bytes(fixture.read_bytes())
    monkeypatch.chdir(tmp_path)

    server = create_server()
    tool = server._tool_manager.get_tool("load_database")
    assert tool is not None

    result = asyncio.run(
        tool.run(
            {"path": f"uploads/{fixture.name}"},
            convert_result=True,
        )
    )

    assert result.structuredContent is not None
    assert result.structuredContent["format"] == "cdd"
    assert result.structuredContent["data_identifier_count"] == 148
    assert result.structuredContent["service_count"] == 222
    assert len(result.structuredContent["data_identifiers_preview"]) == 5
    assert len(result.structuredContent["services_preview"]) == 5
    assert len(json.dumps(result.structuredContent)) < 5_000
    assert len(result.content[0].text) < 1_000
    text = json.loads(result.content[0].text)
    assert text["format"] == "cdd"
    assert len(text["data_identifiers_preview"]) == 5
    assert len(text["services_preview"]) == 5
    assert str(uploads) not in result.content[0].text


@pytest.mark.skipif(
    not Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.37.cdd").is_file(),
    reason="Optional vehicle-specific CDD fixture is not included.",
)
def test_cdd_did_codec_tools_are_registered_and_round_trip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    fixture = Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.37.cdd")
    (uploads / fixture.name).write_bytes(fixture.read_bytes())
    monkeypatch.chdir(tmp_path)

    server = create_server()

    async def invoke(name: str, arguments: dict):
        tool = server._tool_manager.get_tool(name)
        assert tool is not None
        return await tool.run(arguments, convert_result=True)

    async def round_trip():
        loaded = await invoke("load_database", {"path": f"uploads/{fixture.name}"})
        encoded = await invoke(
            "encode_did",
            {
                "identifier": 61831,
                "values": {"ECU_Part_Number": "0012345678"},
            },
        )
        decoded = await invoke(
            "decode_did",
            {"identifier": 61831, "data": "0012345678"},
        )
        return loaded, encoded, decoded

    loaded, encoded, decoded = asyncio.run(round_trip())

    assert loaded.structuredContent["format"] == "cdd"
    assert encoded[1]["data"] == "0012345678"
    assert decoded[1]["fields"]["ECU_Part_Number"] == "0012345678"


def test_virtual_uds_tool_is_registered() -> None:
    server = create_server()

    tool = server._tool_manager.get_tool("uds_request")

    assert tool is not None
    assert inspect.iscoroutinefunction(tool.fn)
    assert {"handle", "request_id", "response_id", "data"} <= set(
        tool.parameters["properties"]
    )


def test_forward_tool_is_async_and_exposes_drain_option() -> None:
    server = create_server()

    tool = server._tool_manager.get_tool("forward_virtual_frame")

    assert tool is not None
    assert inspect.iscoroutinefunction(tool.fn)
    assert "drain" in tool.parameters["properties"]


def test_physical_forward_mcp_tool_returns_text_and_structured_result(
    monkeypatch,
) -> None:
    class FakeAdapter:
        def __init__(self) -> None:
            self.sent = []

        def connect(self):
            return object()

        def disconnect(self) -> None:
            pass

        def send(self, frame) -> None:
            self.sent.append(frame)

    class FakeNotifier:
        def __init__(self, bus, listeners) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(
        "can_mcp.services.bus_service.can.Notifier",
        lambda bus, listeners: FakeNotifier(bus, listeners),
    )
    server = create_server()
    tool = server._tool_manager.get_tool("forward_physical_frame")
    assert tool is not None
    physical = tool.fn.__self__._physical_buses
    source_adapter = FakeAdapter()
    target_adapter = FakeAdapter()
    physical.connect(
        "source",
        source_adapter,
        CanConfig(interface="vector", channel=0),
    )
    physical.connect(
        "target",
        target_adapter,
        CanConfig(interface="vector", channel=1),
    )
    physical.get_bus("source").inject_received_frame(
        CanFrame(arbitration_id=0x123, data=b"\x01", is_rx=True)
    )

    result = asyncio.run(
        tool.run(
            {
                "source_handle": "source",
                "target_handle": "target",
                "timeout": 0,
                "confirm_physical_io": True,
            },
            convert_result=True,
        )
    )

    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "forwarded"
    assert json.loads(result.content[0].text)["status"] == "forwarded"
    assert len(target_adapter.sent) == 1
    physical.disconnect("source")
    physical.disconnect("target")


def test_requested_signal_trace_and_expectation_tools_are_registered() -> None:
    server = create_server()
    expected = {
        "decode_frame",
        "encode_frame",
        "send_signal",
        "read_signal",
        "get_trace_stats",
        "export_trace",
        "expect_frame",
        "start_virtual_trace",
        "stop_virtual_trace",
        "get_virtual_trace",
        "get_virtual_trace_stats",
        "export_virtual_trace",
        "expect_virtual_frame",
        "uds_functional_request",
        "run_scenario",
    }

    assert expected <= {tool.name for tool in server._tool_manager.list_tools()}
    uds_properties = server._tool_manager.get_tool(
        "uds_request"
    ).parameters["properties"]
    assert {"p2_ms", "p2_star_ms", "s3_ms", "suppress_positive_response"} <= set(
        uds_properties
    )


def test_virtual_fault_generator_tools_resources_and_metrics_are_registered() -> None:
    server = create_server()
    tools = {tool.name for tool in server._tool_manager.list_tools()}

    assert {
        "configure_virtual_faults",
        "disable_virtual_faults",
        "start_frame_generator",
        "decode_error_frame",
    } <= tools
    assert {
        "can://status",
        "can://recent-trace",
        "can://dbc-summary",
    } <= {str(resource.uri) for resource in server._resource_manager.list_resources()}

    route = next(
        route for route in server._custom_starlette_routes if route.path == "/metrics"
    )
    app = server.streamable_http_app()
    assert any(getattr(app_route, "path", None) == "/metrics" for app_route in app.routes)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/metrics",
            "headers": [],
            "query_string": b"",
            "server": ("localhost", 80),
            "client": ("localhost", 1234),
            "scheme": "http",
            "http_version": "1.1",
            "root_path": "",
        }
    )
    response = asyncio.run(route.endpoint(request))

    assert response.status_code == 200
    assert b"can_mcp_tx_frames_total" in response.body
    assert response.media_type.startswith("text/plain")


def test_tool_list_and_resource_descriptions_match_server_metadata() -> None:
    server = create_server()
    tools = server._tool_manager.list_tools()
    registered_names = {tool.name for tool in tools}
    documented_names = {
        line.split("|")[1].strip().strip("`")
        for line in Path("docs/tool-list.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `")
    }

    assert registered_names == documented_names
    assert all(tool.description.strip() for tool in tools)
    assert all(
        resource.description.strip()
        for resource in server._resource_manager.list_resources()
    )


def test_expect_frame_matches_data_mask_without_blocking() -> None:
    frames = iter(
        [
            CanFrame(arbitration_id=0x7E8, data=b"\x7F\x22\x11", is_rx=True),
            CanFrame(arbitration_id=0x7E8, data=b"\x62\xF1\x90", is_rx=True),
        ]
    )
    tools = BusTools(SimpleNamespace(receive=lambda timeout: next(frames)))

    result = asyncio.run(
        tools.expect_frame(
            arbitration_id=0x7E8,
            timeout=1,
            data="62 00",
            data_mask="FF 00",
        )
    )

    assert result["status"] == "matched"
    assert result["frame"]["data"] == "62f190"
