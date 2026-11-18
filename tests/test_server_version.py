import re

import pytest

from can_mcp.server import create_server


def test_server_info_version_contains_package_version_date_and_build_id(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CAN_MCP_BUILD", "toolbox-006")
    server = create_server()

    server_version = server._mcp_server.create_initialization_options().server_version

    assert re.fullmatch(r"\d+\.\d+\.\d+\+\d{8}\.toolbox-006", server_version)


def test_server_info_version_rejects_invalid_build_id(monkeypatch) -> None:
    monkeypatch.setenv("CAN_MCP_BUILD", "bad build id")

    with pytest.raises(ValueError, match="CAN_MCP_BUILD"):
        create_server()
