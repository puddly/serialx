"""pyserial's module-level names import from the compat `serial` package."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import serial
import serial.serialutil

import serialx

# Everything pyserial's `serial` package and `serial.serialutil` module export,
# except `protocol_handler_packages`, which serialx has no equivalent for
PYSERIAL_NAMES = [
    "CR",
    "LF",
    "XON",
    "XOFF",
    "FIVEBITS",
    "SIXBITS",
    "SEVENBITS",
    "EIGHTBITS",
    "PARITY_NONE",
    "PARITY_EVEN",
    "PARITY_ODD",
    "PARITY_MARK",
    "PARITY_SPACE",
    "PARITY_NAMES",
    "STOPBITS_ONE",
    "STOPBITS_ONE_POINT_FIVE",
    "STOPBITS_TWO",
    "PortNotOpenError",
    "SerialBase",
    "SerialException",
    "SerialTimeoutException",
    "Timeout",
    "iterbytes",
    "to_bytes",
]


@pytest.mark.parametrize("name", PYSERIAL_NAMES)
def test_pyserial_names_are_exported(name: str) -> None:
    """Each name is in both modules and in their `__all__`, as the same object."""
    assert name in serial.__all__
    assert name in serial.serialutil.__all__
    assert getattr(serial, name) is getattr(serial.serialutil, name)


def test_serial_is_a_serial_base() -> None:
    """A compat `Serial` passes pyserial-style `isinstance` checks."""
    assert isinstance(serial.serial_for_url("socket://127.0.0.1:1"), serial.SerialBase)


def test_port_not_open_error_is_a_serial_exception() -> None:
    """`except serial.SerialException` also catches `PortNotOpenError`."""
    assert issubclass(serial.PortNotOpenError, serial.SerialException)


def test_constants_match_pyserial() -> None:
    """The flow-control bytes and parity names have pyserial's values."""
    assert serial.XON == b"\x11"
    assert serial.XOFF == b"\x13"
    assert serial.PARITY_NAMES[serial.PARITY_MARK] == "Mark"
    assert serial.PARITY_NAMES["N"] == "None"  # type: ignore[index]
    assert serial.SerialBase is serialx.BaseSerial


def test_to_bytes_and_iterbytes() -> None:
    """`to_bytes` and `iterbytes` behave like pyserial's helpers."""
    assert serial.to_bytes([1, 2, 255]) == b"\x01\x02\xff"
    assert serial.to_bytes(bytearray(b"ab")) == b"ab"
    assert serial.to_bytes(memoryview(b"ab")) == b"ab"

    with pytest.raises(TypeError):
        serial.to_bytes("text")

    assert list(serial.iterbytes(b"ab")) == [b"a", b"b"]
    assert list(serial.iterbytes(memoryview(b"ab"))) == [b"a", b"b"]


def test_timeout() -> None:
    """`Timeout` tracks a deadline, `None` never expires and `0` always has."""
    infinite = serial.Timeout(None)
    assert infinite.is_infinite
    assert infinite.time_left() is None
    assert not infinite.expired()

    non_blocking = serial.Timeout(0)
    assert non_blocking.is_non_blocking
    assert non_blocking.time_left() == 0.0
    assert non_blocking.expired()

    with patch("serial.serialutil.time.monotonic", return_value=100.0):
        timeout = serial.Timeout(2.0)

    with patch("serial.serialutil.time.monotonic", return_value=101.5):
        assert timeout.time_left() == 0.5
        assert not timeout.expired()

    with patch("serial.serialutil.time.monotonic", return_value=103.0):
        assert timeout.time_left() == 0.0
        assert timeout.expired()

        timeout.restart(1.0)
        assert timeout.time_left() == 1.0
