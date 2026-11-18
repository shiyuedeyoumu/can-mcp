from pathlib import Path
from typing import Any

from can_mcp.core.errors import DatabaseFormatError
from can_mcp.core.interfaces import DatabaseParser


class DbsParser(DatabaseParser):
    @property
    def format_name(self) -> str:
        return "dbs"

    def parse(self, path: Path) -> Any:
        raise DatabaseFormatError(
            f"DBS parsing is not implemented yet: {path}. "
            "Implement a parser for the target DBS dialect before loading this format."
        )
