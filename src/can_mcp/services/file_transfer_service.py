import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4


class FileTransferService:
    MAX_FILE_SIZE = 64 * 1024 * 1024
    MAX_CHUNK_SIZE = 512 * 1024
    EXPIRY_SECONDS = 24 * 60 * 60
    ALLOWED_EXTENSIONS = {".dbc", ".cdd", ".dbs", ".asc", ".blf", ".csv"}

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.cwd() / "uploads").expanduser().resolve()
        self.staging = self.root / ".staging"
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.cleanup_expired()

    def upload_begin(
        self,
        name: str,
        size: int,
        sha256: str | None = None,
        upload_id: str | None = None,
    ) -> dict[str, object]:
        safe_name = self._validate_name(name)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("size must be a non-negative integer.")
        if size > self.MAX_FILE_SIZE:
            raise ValueError("File exceeds the 64 MB upload limit.")
        expected_hash = self._validate_hash(sha256) if sha256 is not None else None
        current_id = uuid4().hex if upload_id is None else upload_id
        if not isinstance(current_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,64}", current_id
        ):
            raise ValueError("upload_id must contain 8-64 letters, digits, '_' or '-'.")

        with self._lock:
            self.cleanup_expired()
            part_path, metadata_path = self._staging_paths(current_id)
            if part_path.exists() or metadata_path.exists():
                metadata = self._read_metadata(metadata_path)
                if (
                    metadata["name"] != safe_name
                    or metadata["size"] != size
                    or metadata["sha256"] != expected_hash
                ):
                    raise ValueError("upload_id already belongs to a different upload.")
                if not part_path.is_file() or part_path.is_symlink():
                    raise ValueError("Upload staging file is invalid.")
                return {
                    "status": "resumed",
                    "upload_id": current_id,
                    "name": safe_name,
                    "offset": part_path.stat().st_size,
                    "chunk_size_limit": self.MAX_CHUNK_SIZE,
                }
            if self._destination_path(safe_name).exists():
                raise FileExistsError(f"File already exists: uploads/{safe_name}")

            part_path.touch(exist_ok=False)
            self._write_metadata(
                metadata_path,
                {"name": safe_name, "size": size, "sha256": expected_hash},
            )
            return {
                "status": "started",
                "upload_id": current_id,
                "name": safe_name,
                "offset": 0,
                "chunk_size_limit": self.MAX_CHUNK_SIZE,
            }

    def upload_chunk(
        self,
        upload_id: str,
        offset: int,
        data: str,
    ) -> dict[str, object]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        if not isinstance(data, str):
            raise ValueError("data must be a valid base64-encoded chunk.")
        try:
            chunk = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data must be a valid base64-encoded chunk.") from exc
        if not chunk:
            raise ValueError("chunk data must not be empty.")
        if len(chunk) > self.MAX_CHUNK_SIZE:
            raise ValueError("Chunk exceeds the 512 KB limit.")

        with self._lock:
            part_path, metadata_path = self._staging_paths(upload_id)
            metadata = self._read_metadata(metadata_path)
            if part_path.is_symlink() or not part_path.is_file():
                raise ValueError("Upload staging file is invalid.")
            current_size = part_path.stat().st_size
            if offset != current_size:
                raise ValueError(
                    f"Invalid offset: expected {current_size}, received {offset}."
                )
            if current_size + len(chunk) > metadata["size"]:
                raise ValueError("Chunk would exceed the declared upload size.")
            with part_path.open("ab") as stream:
                stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            os.utime(metadata_path, None)
            return {
                "status": "chunk_received",
                "upload_id": upload_id,
                "offset": current_size + len(chunk),
                "size": metadata["size"],
            }

    def upload_finish(self, upload_id: str) -> dict[str, object]:
        with self._lock:
            part_path, metadata_path = self._staging_paths(upload_id)
            metadata = self._read_metadata(metadata_path)
            if part_path.is_symlink() or not part_path.is_file():
                raise ValueError("Upload staging file is invalid.")
            if part_path.stat().st_size != metadata["size"]:
                raise ValueError(
                    f"Upload size mismatch: expected {metadata['size']}, "
                    f"received {part_path.stat().st_size}."
                )
            digest = self._sha256_file(part_path)
            expected_hash = metadata["sha256"]
            if expected_hash is not None and digest != expected_hash:
                part_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                raise ValueError("SHA-256 verification failed; incomplete file deleted.")

            destination = self._destination_path(metadata["name"])
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(
                    f"File already exists: uploads/{metadata['name']}"
                )
            os.replace(part_path, destination)
            metadata_path.unlink(missing_ok=True)
            return {
                "status": "uploaded",
                "upload_id": upload_id,
                "name": metadata["name"],
                "path": f"uploads/{metadata['name']}",
                "size": metadata["size"],
                "sha256": digest,
            }

    def download_file(
        self,
        path: str,
        offset: int = 0,
        chunk_size: int = MAX_CHUNK_SIZE,
    ) -> dict[str, object]:
        file_path, identifier = self.resolve_file(path)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or not 1 <= chunk_size <= self.MAX_CHUNK_SIZE
        ):
            raise ValueError("chunk_size must be between 1 and 512 KB.")
        size = file_path.stat().st_size
        if offset > size:
            raise ValueError(f"offset exceeds file size ({size}).")
        with file_path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(chunk_size)
        next_offset = offset + len(chunk)
        return {
            "path": identifier,
            "offset": offset,
            "next_offset": next_offset,
            "size": size,
            "sha256": self._sha256_file(file_path),
            "data": base64.b64encode(chunk).decode("ascii"),
            "eof": next_offset >= size,
        }

    def list_files(self) -> list[dict[str, object]]:
        with self._lock:
            self.cleanup_expired()
            files = [
                path
                for path in self.root.iterdir()
                if path.name != ".staging" and path.is_file() and not path.is_symlink()
            ]
            return [
                {
                    "path": f"uploads/{path.name}",
                    "size": path.stat().st_size,
                    "modified_at": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(),
                }
                for path in sorted(files, key=lambda item: item.name.casefold())
            ]

    def resolve_file(self, identifier: str) -> tuple[Path, str]:
        name = self._validate_identifier(identifier)
        with self._lock:
            self.cleanup_expired()
            path = self._destination_path(name)
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError(f"File does not exist in uploads: {identifier}")
            return path, f"uploads/{name}"

    def export_destination(self, name: str | None, default_extension: str = ".asc") -> tuple[Path, str]:
        if name is None or (isinstance(name, str) and not name.strip()):
            name = f"trace-{time.time_ns()}-{uuid4().hex[:8]}{default_extension}"
        if not isinstance(name, str):
            raise ValueError("Trace export path must be a file name.")
        safe_name = self._validate_name(name)
        if Path(safe_name).suffix.lower() not in {".asc", ".blf", ".csv"}:
            raise ValueError("Trace export must use .asc, .blf or .csv extension.")
        destination = self._destination_path(safe_name)
        if destination.is_symlink():
            raise ValueError("Trace export cannot overwrite a symbolic link.")
        return destination, f"uploads/{safe_name}"

    def cleanup_expired(self, now: float | None = None) -> None:
        current_time = time.time() if now is None else now
        for path in self.root.iterdir():
            if path.name == ".staging":
                continue
            if path.is_file() or path.is_symlink():
                self._remove_if_expired(path, current_time)
        if self.staging.exists():
            for path in self.staging.iterdir():
                if path.is_file() or path.is_symlink():
                    self._remove_if_expired(path, current_time)

    def _remove_if_expired(self, path: Path, now: float) -> None:
        try:
            if now - path.stat().st_mtime > self.EXPIRY_SECONDS:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            return

    def _validate_name(self, name: str) -> str:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("name must be a non-empty file name.")
        if (
            PureWindowsPath(name).is_absolute()
            or PureWindowsPath(name).drive
            or PurePosixPath(name).is_absolute()
            or "/" in name
            or "\\" in name
            or any(character in name for character in '<>:"|?*')
            or name in {".", ".."}
            or name.endswith((".", " "))
            or Path(name).name != name
        ):
            raise ValueError("name must be a plain file name without path components.")
        if Path(name).suffix.lower() not in self.ALLOWED_EXTENSIONS:
            raise ValueError(
                "Unsupported file extension; allowed: .dbc, .cdd, .dbs, .asc, .blf, .csv."
            )
        return name

    def _validate_identifier(self, identifier: str) -> str:
        if not isinstance(identifier, str):
            raise ValueError("path must be an uploads-relative file identifier.")
        normalized = identifier.replace("\\", "/")
        if normalized.startswith("uploads/"):
            normalized = normalized[len("uploads/") :]
        return self._validate_name(normalized)

    def _destination_path(self, name: str) -> Path:
        destination = self.root / name
        if destination.parent.resolve() != self.root:
            raise ValueError("File path must remain inside the uploads sandbox.")
        return destination

    def _staging_paths(self, upload_id: str) -> tuple[Path, Path]:
        if not isinstance(upload_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,64}", upload_id
        ):
            raise ValueError("Invalid upload_id.")
        return (
            self.staging / f"{upload_id}.part",
            self.staging / f"{upload_id}.json",
        )

    @staticmethod
    def _validate_hash(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("sha256 must be a 64-character hexadecimal digest.")
        return value.lower()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _write_metadata(path: Path, metadata: dict[str, object]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(metadata), encoding="utf-8")
        os.replace(temporary, path)

    def _read_metadata(self, path: Path) -> dict[str, object]:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("Upload session does not exist or has expired.")
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if set(metadata) != {"name", "size", "sha256"}:
                raise ValueError("invalid upload metadata")
            metadata["name"] = self._validate_name(metadata["name"])
            if (
                isinstance(metadata["size"], bool)
                or not isinstance(metadata["size"], int)
                or not 0 <= metadata["size"] <= self.MAX_FILE_SIZE
            ):
                raise ValueError("invalid upload size")
            metadata["sha256"] = self._validate_hash(metadata["sha256"])
            return metadata
        except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise ValueError("Upload session metadata is invalid.") from exc
