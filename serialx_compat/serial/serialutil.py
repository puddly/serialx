"""Compatibility shim for pyserial's ``serial.serialutil`` module."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import time

from serialx import (
    CR,
    EIGHTBITS,
    FIVEBITS,
    LF,
    PARITY_EVEN,
    PARITY_MARK,
    PARITY_NONE,
    PARITY_ODD,
    PARITY_SPACE,
    SEVENBITS,
    SIXBITS,
    STOPBITS_ONE,
    STOPBITS_ONE_POINT_FIVE,
    STOPBITS_TWO,
    BaseSerial,
    SerialException,
    SerialTimeoutException,
)
from serialx.serialutil import PortNotOpenError

__all__ = [
    "CR",
    "EIGHTBITS",
    "FIVEBITS",
    "LF",
    "PARITY_EVEN",
    "PARITY_MARK",
    "PARITY_NAMES",
    "PARITY_NONE",
    "PARITY_ODD",
    "PARITY_SPACE",
    "SEVENBITS",
    "SIXBITS",
    "STOPBITS_ONE",
    "STOPBITS_ONE_POINT_FIVE",
    "STOPBITS_TWO",
    "XOFF",
    "XON",
    "PortNotOpenError",
    "SerialBase",
    "SerialException",
    "SerialTimeoutException",
    "Timeout",
    "iterbytes",
    "to_bytes",
]

XON = b"\x11"
XOFF = b"\x13"

PARITY_NAMES = {
    PARITY_NONE: "None",
    PARITY_EVEN: "Even",
    PARITY_ODD: "Odd",
    PARITY_MARK: "Mark",
    PARITY_SPACE: "Space",
}

SerialBase = BaseSerial


def to_bytes(seq: bytes | bytearray | memoryview | Iterable[int] | str) -> bytes:
    """Convert a byte sequence or an iterable of ints to `bytes`."""
    if isinstance(seq, str):
        raise TypeError(
            f"unicode strings are not supported, please encode to bytes: {seq!r}"
        )

    return bytes(seq)


def iterbytes(data: bytes | bytearray | memoryview) -> Iterator[bytes]:
    """Iterate over the bytes of `data` as one-byte `bytes` objects."""
    for byte in bytes(data):
        yield bytes((byte,))


class Timeout:
    """A deadline helper with pyserial's `Timeout` interface.

    `None` never expires and `0` is always expired.
    """

    def __init__(self, duration: float | None) -> None:
        """Start a timeout of `duration` seconds."""
        self.is_infinite = duration is None
        self.is_non_blocking = duration == 0
        self._duration = duration
        self._deadline = None if duration is None else time.monotonic() + duration

    def expired(self) -> bool:
        """Return whether the timeout has expired."""
        time_left = self.time_left()
        return time_left is not None and time_left <= 0

    def time_left(self) -> float | None:
        """Return the seconds left, `0.0` once expired, or `None` if infinite."""
        if self._deadline is None:
            return None

        return max(0.0, self._deadline - time.monotonic())

    def restart(self, duration: float) -> None:
        """Start again with a new `duration`."""
        self._duration = duration
        self.is_infinite = False
        self.is_non_blocking = duration == 0
        self._deadline = time.monotonic() + duration
