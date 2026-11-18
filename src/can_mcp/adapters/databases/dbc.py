import re
from pathlib import Path
from typing import Any

import cantools

from can_mcp.core.interfaces import DatabaseParser


class DbcParser(DatabaseParser):
    def __init__(self) -> None:
        self.warnings: list[str] = []

    @property
    def format_name(self) -> str:
        return "dbc"

    def parse(self, path: Path) -> Any:
        self.warnings = []
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="cp1252")
            self.warnings.append(
                "DBC text is not UTF-8; decoded using Windows-1252."
            )
        unusable_message_ids = self._messages_without_in_range_signals(text)
        if unusable_message_ids:
            patterns = [
                re.compile(rf"^VAL_\s+{message_id}\s+.*(?:\r?\n|$)", re.MULTILINE)
                for message_id in unusable_message_ids
            ]
            removed_entries = 0
            for pattern in patterns:
                text, count = pattern.subn("", text)
                removed_entries += count
            if removed_entries:
                self.warnings.append(
                    f"Ignored {removed_entries} VAL_ definition(s) attached to "
                    "messages with no in-range signals; cantools cannot associate "
                    "them with a usable signal."
                )

        database = cantools.database.load_string(
            text,
            database_format="dbc",
            strict=False,
        )
        self.warnings.append(
            "DBC was loaded with cantools strict validation disabled for "
            "Vector CANdb++ compatibility."
        )
        invalid_signal_counts = {
            message.name: [
                signal.name
                for signal in message.signals
                if signal.start + signal.length > message.length * 8
            ]
            for message in database.messages
        }
        invalid_signal_counts = {
            name: signals for name, signals in invalid_signal_counts.items() if signals
        }
        for message_name, signals in invalid_signal_counts.items():
            self.warnings.append(
                f"Message '{message_name}' contains {len(signals)} signal(s) "
                "outside its declared payload length; decode and encode operations "
                "for this message are rejected."
            )
        return database

    @staticmethod
    def _messages_without_in_range_signals(text: str) -> set[int]:
        headers = list(re.finditer(r"^BO_\s+(\d+)\s+.*$", text, re.MULTILINE))
        unusable_ids: set[int] = set()
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            message_body = text[header.end() : end]
            message_header = header.group(0)
            length_match = re.search(r":\s*(\d+)\s+", message_header)
            if length_match is None:
                continue
            message_bits = int(length_match.group(1)) * 8
            signals = re.finditer(
                r"^[ \t]+SG_[ \t]+\S+[ \t]+(?:m\d+[M]?[ \t]+)?:"
                r"[ \t]*(\d+)\|(\d+)@",
                message_body,
                re.MULTILINE,
            )
            if not any(
                int(signal.group(1)) + int(signal.group(2)) <= message_bits
                for signal in signals
            ):
                unusable_ids.add(int(header.group(1)))
        return unusable_ids
