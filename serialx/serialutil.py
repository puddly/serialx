"""serialx pyserial compatibility module."""

from .common import SerialException

__all__ = ["PortNotOpenError", "SerialException"]


class PortNotOpenError(SerialException):
    """The port is not open."""

    def __init__(self) -> None:
        """Initialize with pyserial's message."""
        super().__init__("Attempting to use a port that is not open")
