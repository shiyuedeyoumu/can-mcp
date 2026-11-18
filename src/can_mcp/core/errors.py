class CanMcpError(Exception):
    """Base error exposed by the CAN MCP application."""


class BusNotConnectedError(CanMcpError):
    """Raised when an operation requires a connected CAN bus."""


class TraceNotStartedError(CanMcpError):
    """Raised when trace data is queried before capture has been started."""


class DatabaseFormatError(CanMcpError):
    """Raised when a database format is unknown or unsupported."""
