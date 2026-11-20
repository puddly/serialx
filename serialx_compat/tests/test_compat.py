"""Tests for the pyserial compatibility shim package."""

from __future__ import annotations

import pytest
import serial
import serial.serialutil
import serial.tools
import serial.tools.list_ports
import serial.tools.list_ports_common
import serial_asyncio
import serial_asyncio_fast

import serialx


def test_serial_exports_versions_and_core_symbols() -> None:
    """Serial module exposes expected symbols and version attributes."""
    assert serial.__version__ == serial.VERSION
    assert isinstance(serial.VERSION, str)
    assert hasattr(serial, "Serial")
    assert hasattr(serial, "serial_for_url")


def test_serial_submodules_are_importable() -> None:
    """Serial package submodules expected by pyserial users import correctly."""
    assert hasattr(serial.tools.list_ports, "comports")


def test_serial_asyncio_exports_versions_and_core_symbols() -> None:
    """serial_asyncio module exposes expected symbols and version attributes."""
    assert serial_asyncio.__version__ == serial_asyncio.VERSION
    assert isinstance(serial_asyncio.VERSION, str)
    assert hasattr(serial_asyncio, "create_serial_connection")
    assert hasattr(serial_asyncio, "open_serial_connection")
    assert hasattr(serial_asyncio, "SerialTransport")


def test_serial_asyncio_fast_exports_versions_and_core_symbols() -> None:
    """serial_asyncio_fast module exposes expected symbols and version attributes."""
    assert serial_asyncio_fast.__version__ == serial_asyncio_fast.VERSION
    assert isinstance(serial_asyncio_fast.VERSION, str)
    assert hasattr(serial_asyncio_fast, "create_serial_connection")
    assert hasattr(serial_asyncio_fast, "open_serial_connection")
    assert hasattr(serial_asyncio_fast, "SerialTransport")


def test_compat_serial_from_url_wraps_exceptions() -> None:
    """`serial.Serial.from_url` wraps underlying exceptions as `SerialException`."""
    port = serial.Serial.from_url("/nonexistent/serialx-compat/path")

    with pytest.raises(serial.SerialException) as exc_info:
        port.open()

    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


def test_compat_serial_for_url_wraps_exceptions() -> None:
    """`serial.serial_for_url` wraps underlying exceptions as `SerialException`."""
    port = serial.serial_for_url("/nonexistent/serialx-compat/path")

    with pytest.raises(serial.SerialException) as exc_info:
        port.open()

    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


def test_compat_serial_not_open_raises() -> None:
    """Reading a port that is not open raises pyserial's `SerialException`."""
    port = serial.Serial.from_url("/nonexistent/serialx-compat/path")

    with pytest.raises(
        serial.SerialException, match="^Attempting to use a port that is not open$"
    ):
        port.read(1)


def test_serialx_serial_does_not_wrap_exceptions() -> None:
    """The underlying `serialx.Serial` leaves the original exception untouched."""
    port = serialx.Serial.from_url("/nonexistent/serialx-compat/path")

    with pytest.raises(FileNotFoundError):
        port.open()
