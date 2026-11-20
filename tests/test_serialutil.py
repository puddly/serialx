"""Tests for `serialx.serialutil`."""

from serialx import SerialException
from serialx.serialutil import PortNotOpenError


def test_port_not_open_error() -> None:
    """`PortNotOpenError` is a `SerialException` with pyserial's message."""
    error = PortNotOpenError()
    assert isinstance(error, SerialException)
    assert str(error) == "Attempting to use a port that is not open"
