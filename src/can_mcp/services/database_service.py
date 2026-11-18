from pathlib import Path
from typing import Any

import cantools

from can_mcp.adapters.databases.cdd import (
    CddDataIdentifier,
    CddDatabase,
    CddParser,
)
from can_mcp.adapters.databases.cdd_codec import CddCodec
from can_mcp.adapters.databases.dbc import DbcParser
from can_mcp.adapters.databases.dbs import DbsParser
from can_mcp.core.errors import DatabaseFormatError
from can_mcp.core.interfaces import DatabaseParser


class DatabaseService:
    def __init__(self, parsers: list[DatabaseParser] | None = None) -> None:
        default_parsers: list[DatabaseParser] = [
            DbcParser(),
            CddParser(),
            DbsParser(),
        ]
        self._parsers = {parser.format_name: parser for parser in (parsers or default_parsers)}
        self._database: cantools.database.Database | None = None
        self._cdd_database: CddDatabase | None = None
        self._cdd_codec = CddCodec()
        self._loaded_path: Path | None = None
        self._display_path: str | None = None
        self._load_warnings: list[str] = []

    def load(self, path: str, display_path: str | None = None) -> Any:
        database_path = Path(path).expanduser()
        extension = database_path.suffix.lstrip(".").lower()
        parser = self._parsers.get(extension)
        if parser is None:
            supported = ", ".join(sorted(self._parsers))
            raise DatabaseFormatError(
                f"Unsupported database extension '.{extension}'. Registered formats: {supported}."
            )
        if not database_path.is_file():
            raise FileNotFoundError(f"Database file does not exist: {database_path}")
        database = parser.parse(database_path)
        if extension == "dbc":
            self._cdd_database = None
            self._database = database
            self._loaded_path = database_path.resolve()
            self._display_path = display_path or str(self._loaded_path)
            parser_warnings = getattr(parser, "warnings", [])
            self._load_warnings = list(parser_warnings)
            return self.describe()
        if extension == "cdd":
            if not isinstance(database, CddDatabase):
                raise TypeError("CDD parser must return a CddDatabase model.")
            self._database = None
            self._cdd_database = database
            self._loaded_path = database_path.resolve()
            self._display_path = display_path or str(self._loaded_path)
            self._load_warnings = []
            return database.describe() | {"path": self._display_path}
        return database

    def describe(self) -> dict[str, object]:
        if self._cdd_database is not None:
            return self._cdd_database.describe() | {
                "path": self._display_path,
            }
        database = self._require_database()
        return {
            "status": "loaded",
            "format": "dbc",
            "path": self._display_path,
            "message_count": len(database.messages),
            "warnings": list(self._load_warnings),
            "messages": [
                {
                    "name": message.name,
                    "arbitration_id": message.frame_id,
                    "is_extended_id": message.is_extended_frame,
                    "is_fd": message.is_fd,
                    "length": message.length,
                    "signals": [signal.name for signal in message.signals],
                }
                for message in database.messages
            ],
        }

    def summary(self) -> dict[str, object]:
        if self._cdd_database is not None:
            return self._cdd_database.summary() | {
                "path": self._display_path,
            }
        if self._database is None:
            return {"status": "not_loaded", "message_count": 0, "messages": []}
        return {
            "status": "loaded",
            "format": "dbc",
            "path": self._display_path,
            "message_count": len(self._database.messages),
            "warnings": list(self._load_warnings),
            "messages": [
                {
                    "name": message.name,
                    "arbitration_id": message.frame_id,
                    "signals": [signal.name for signal in message.signals],
                }
                for message in self._database.messages
            ],
        }

    def list_diagnostic_services(
        self,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        database = self._require_cdd_database()
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100.")
        if search is not None and not isinstance(search, str):
            raise ValueError("search must be a string.")

        services = list(database.services)
        if search:
            term = search.casefold()
            services = [
                service
                for service in services
                if term in service.name.casefold()
                or term in service.qualifier.casefold()
                or (service.shortcut is not None and term in service.shortcut.casefold())
            ]
        return {
            "format": "cdd",
            "search": search,
            "offset": offset,
            "limit": limit,
            "total_count": len(services),
            "services": [
                service.to_dict() for service in services[offset : offset + limit]
            ],
        }

    def get_data_identifier(self, identifier: int) -> dict[str, object]:
        database = self._require_cdd_database()
        did = self._find_data_identifier(database, identifier)
        return {"format": "cdd", "data_identifier": did.to_dict()}

    def decode_data_identifier(
        self,
        identifier: int,
        payload: bytes,
    ) -> dict[str, Any]:
        database = self._require_cdd_database()
        did = self._find_data_identifier(database, identifier)
        return self._cdd_codec.decode(did, payload)

    def encode_data_identifier(
        self,
        identifier: int,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        database = self._require_cdd_database()
        did = self._find_data_identifier(database, identifier)
        payload = self._cdd_codec.encode(did, values)
        return {
            "identifier": did.identifier,
            "identifier_hex": f"0x{did.identifier:04X}",
            "name": did.name,
            "qualifier": did.qualifier,
            "data": payload.hex(),
            "byte_length": len(payload),
        }

    def decode_frame(
        self,
        arbitration_id: int,
        data: bytes,
        is_extended_id: bool = False,
    ) -> dict[str, object]:
        database = self._require_database()
        message = database.get_message_by_frame_id(
            arbitration_id,
            force_extended_id=is_extended_id,
        )
        self._validate_message_layout(message)
        signals = database.decode_message(
            arbitration_id,
            data,
            decode_choices=False,
            scaling=True,
            allow_truncated=False,
            force_extended_id=is_extended_id,
        )
        return {
            "message_name": message.name,
            "arbitration_id": message.frame_id,
            "is_extended_id": message.is_extended_frame,
            "is_fd": message.is_fd,
            "signals": signals,
        }

    def encode_frame(
        self,
        message_name: str,
        signals: dict[str, Any],
    ) -> dict[str, object]:
        database = self._require_database()
        message = database.get_message_by_name(message_name)
        self._validate_message_layout(message)
        known_signals = {signal.name for signal in message.signals}
        unknown_signals = set(signals) - known_signals
        if unknown_signals:
            raise KeyError(
                f"Unknown signal(s) for message '{message_name}': "
                f"{', '.join(sorted(unknown_signals))}."
            )
        data = database.encode_message(
            message.name,
            signals,
            scaling=True,
            padding=False,
            strict=True,
            force_extended_id=message.is_extended_frame,
        )
        return {
            "message_name": message.name,
            "arbitration_id": message.frame_id,
            "is_extended_id": message.is_extended_frame,
            "is_fd": message.is_fd,
            "data": data.hex(),
        }

    def message(self, message_name: str) -> cantools.database.can.Message:
        return self._require_database().get_message_by_name(message_name)

    def _require_database(self) -> cantools.database.Database:
        if self._database is None:
            if self._cdd_database is not None:
                raise RuntimeError(
                    "A CDD diagnostic catalog is loaded. DBC signal decoding and "
                    "encoding require loading a DBC file."
                )
            raise RuntimeError("Load a DBC file with load_database before signal operations.")
        return self._database

    def _require_cdd_database(self) -> CddDatabase:
        if self._cdd_database is None:
            if self._database is not None:
                raise RuntimeError(
                    "A DBC signal database is loaded. Load a CDD file to query "
                    "diagnostic services and data identifiers."
                )
            raise RuntimeError(
                "Load a CDD file with load_database before querying diagnostic "
                "services and data identifiers."
            )
        return self._cdd_database

    @staticmethod
    def _find_data_identifier(
        database: CddDatabase,
        identifier: int,
    ) -> CddDataIdentifier:
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            raise ValueError("identifier must be a 16-bit integer.")
        if not 0 <= identifier <= 0xFFFF:
            raise ValueError("identifier must be a 16-bit integer.")
        for did in database.data_identifiers:
            if did.identifier == identifier:
                return did
        raise KeyError(f"CDD data identifier 0x{identifier:04X} was not found.")

    @staticmethod
    def _validate_message_layout(message: cantools.database.can.Message) -> None:
        invalid_signals = [
            signal.name
            for signal in message.signals
            if signal.start + signal.length > message.length * 8
        ]
        if invalid_signals:
            raise ValueError(
                f"DBC message '{message.name}' has signals outside its declared "
                f"{message.length}-byte payload and cannot be safely decoded or "
                f"encoded: {', '.join(invalid_signals[:5])}."
            )
