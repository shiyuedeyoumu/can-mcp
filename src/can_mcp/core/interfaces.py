from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from can_mcp.core.models import CanFrame


FrameCallback = Callable[[CanFrame], None]


class CanAdapter(ABC):
    @abstractmethod
    def connect(self) -> Any:
        """Open the configured CAN interface."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the CAN interface."""

    @abstractmethod
    def send(self, frame: CanFrame) -> None:
        """Send a frame."""


class DatabaseParser(ABC):
    @property
    @abstractmethod
    def format_name(self) -> str:
        """Return the file format handled by this parser."""

    @abstractmethod
    def parse(self, path: Path) -> Any:
        """Parse a database file and return its format-specific model."""
