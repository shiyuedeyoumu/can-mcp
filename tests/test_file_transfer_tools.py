import base64
import hashlib
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from can_mcp.core.models import CanFrame
from can_mcp.services.database_service import DatabaseService
from can_mcp.services.file_transfer_service import FileTransferService
from can_mcp.services.virtual_bus_service import VirtualBusService
from can_mcp.tools.database_tools import DatabaseTools
from can_mcp.tools.trace_tools import TraceTools
from can_mcp.tools.virtual_bus_tools import VirtualBusTools


@pytest.fixture
def files(tmp_path: Path) -> FileTransferService:
    return FileTransferService(tmp_path / "uploads")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _upload(
    files: FileTransferService,
    name: str,
    data: bytes,
    *,
    sha256: str | None = None,
) -> dict[str, object]:
    started = files.upload_begin(name, len(data), sha256 or hashlib.sha256(data).hexdigest())
    upload_id = str(started["upload_id"])
    for offset in range(0, len(data), files.MAX_CHUNK_SIZE):
        chunk = data[offset : offset + files.MAX_CHUNK_SIZE]
        files.upload_chunk(upload_id, offset, _b64(chunk))
    return files.upload_finish(upload_id)


def test_upload_large_file_sha256_and_database_load(
    files: FileTransferService,
) -> None:
    source = b"x" * 1_920_072
    started = files.upload_begin(
        "large.dbc",
        len(source),
        hashlib.sha256(source).hexdigest(),
    )
    upload_id = str(started["upload_id"])
    chunk_count = math.ceil(len(source) / files.MAX_CHUNK_SIZE)
    assert chunk_count == 4
    for offset in range(0, len(source), files.MAX_CHUNK_SIZE):
        chunk = source[offset : offset + files.MAX_CHUNK_SIZE]
        files.upload_chunk(upload_id, offset, _b64(chunk))

    finished = files.upload_finish(upload_id)
    assert finished["sha256"] == hashlib.sha256(source).hexdigest()
    assert finished["path"] == "uploads/large.dbc"
    assert str(files.root) not in str(finished)

    database_tools = DatabaseTools(
        DatabaseService(),
        VirtualBusService(),
        files,
    )
    sample_dbc = Path(__file__).parent / "fixtures" / "sample.dbc"
    sample_upload = _upload(files, "sample.dbc", sample_dbc.read_bytes())
    loaded = database_tools.load_database(str(sample_upload["path"]))
    assert loaded["message_count"] == 2
    assert loaded["path"] == "uploads/sample.dbc"
    assert str(files.root) not in str(loaded)


def test_sha256_mismatch_deletes_partial_upload(files: FileTransferService) -> None:
    started = files.upload_begin("bad.dbc", 4, "0" * 64)
    upload_id = str(started["upload_id"])
    files.upload_chunk(upload_id, 0, _b64(b"data"))

    with pytest.raises(ValueError, match="SHA-256 verification failed"):
        files.upload_finish(upload_id)

    assert not list(files.staging.iterdir())
    assert files.list_files() == []


def test_chunk_offset_must_match_exact_next_position(files: FileTransferService) -> None:
    started = files.upload_begin("offset.dbc", 8)
    upload_id = str(started["upload_id"])
    files.upload_chunk(upload_id, 0, _b64(b"1234"))

    with pytest.raises(ValueError, match="expected 4, received 5"):
        files.upload_chunk(upload_id, 5, _b64(b"5678"))

    assert files.upload_chunk(upload_id, 4, _b64(b"5678"))["offset"] == 8


@pytest.mark.parametrize(
    "name",
    ["../evil.dbc", "C:\\Windows\\x.dbc", "folder\\x.dbc", "/tmp/x.dbc"],
)
def test_upload_rejects_traversal_absolute_paths_and_subdirectories(
    files: FileTransferService,
    name: str,
) -> None:
    with pytest.raises(ValueError, match="plain file name"):
        files.upload_begin(name, 1)


def test_upload_extension_and_size_allowlists(files: FileTransferService) -> None:
    with pytest.raises(ValueError, match="Unsupported file extension"):
        files.upload_begin("program.exe", 1)
    with pytest.raises(ValueError, match="64 MB"):
        files.upload_begin("too-large.dbc", files.MAX_FILE_SIZE + 1)


def test_download_large_file_in_chunks_and_reject_escape(
    files: FileTransferService,
) -> None:
    payload = (Path(__file__).parent / "fixtures" / "sample.dbc").read_bytes()
    uploaded = _upload(files, "download.dbc", payload)
    downloaded = bytearray()
    offset = 0
    while True:
        result = files.download_file(str(uploaded["path"]), offset)
        downloaded.extend(base64.b64decode(str(result["data"])))
        offset = int(result["next_offset"])
        if result["eof"]:
            break

    assert bytes(downloaded) == payload
    assert hashlib.sha256(downloaded).hexdigest() == hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="plain file name"):
        files.download_file("../secret")


def test_list_files_metadata_and_24_hour_expiry(files: FileTransferService) -> None:
    uploaded = _upload(files, "expire.dbc", b"DBC")
    listed = files.list_files()
    assert listed[0]["path"] == uploaded["path"]
    assert listed[0]["size"] == 3
    assert isinstance(listed[0]["modified_at"], str)

    path = files.root / "expire.dbc"
    expired_at = time.time() - files.EXPIRY_SECONDS - 1
    os.utime(path, (expired_at, expired_at))
    assert files.list_files() == []
    assert not path.exists()


def test_upload_resumes_after_service_recreation(files: FileTransferService) -> None:
    payload = b"resumable payload"
    digest = hashlib.sha256(payload).hexdigest()
    begun = files.upload_begin("resume.dbc", len(payload), digest)
    upload_id = str(begun["upload_id"])
    files.upload_chunk(upload_id, 0, _b64(payload[:7]))

    resumed_service = FileTransferService(files.root)
    resumed = resumed_service.upload_begin(
        "resume.dbc",
        len(payload),
        digest,
        upload_id,
    )
    assert resumed["status"] == "resumed"
    assert resumed["offset"] == 7
    resumed_service.upload_chunk(upload_id, 7, _b64(payload[7:]))
    result = resumed_service.upload_finish(upload_id)
    assert result["sha256"] == digest


def test_trace_exports_default_into_downloadable_uploads_sandbox(
    files: FileTransferService,
) -> None:
    trace_tools = TraceTools(
        SimpleNamespace(),
        SimpleNamespace(query=lambda frame_filter, limit: [CanFrame(0x123, b"\x01")]),
        files,
    )
    exported = trace_tools.export_trace()
    assert str(exported["path"]).startswith("uploads/trace-")
    assert exported["format"] == "asc"
    assert files.download_file(str(exported["path"]))["eof"] is True

    class VirtualTraceStub:
        def export_trace(
            self,
            handle: str,
            path: str,
            frame_filter: object,
            limit: int,
        ) -> dict[str, object]:
            Path(path).write_text("trace", encoding="utf-8")
            return {"status": "exported", "path": path, "frame_count": 0}

    virtual_tools = VirtualBusTools(VirtualTraceStub(), files)  # type: ignore[arg-type]
    virtual_export = virtual_tools.export_virtual_trace("virtual")
    assert str(virtual_export["path"]).startswith("uploads/trace-")
    assert files.download_file(str(virtual_export["path"]))["eof"] is True


def test_file_tools_registered_and_descriptions_hide_server_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from can_mcp.server import create_server

    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    server = create_server()
    expected = {
        "upload_begin",
        "upload_chunk",
        "upload_finish",
        "download_file",
        "list_files",
    }
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    assert expected <= tools.keys()
    for name in expected:
        description = tools[name].description or ""
        assert "服务端物理路径对客户端不可见" in description
