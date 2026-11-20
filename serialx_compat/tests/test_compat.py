"""Tests for the pyserial compatibility shim package."""

from __future__ import annotations

import os
import socket
import sys

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


def test_serialx_serial_does_not_wrap_exceptions() -> None:
    """The underlying `serialx.Serial` leaves the original exception untouched."""
    port = serialx.Serial.from_url("/nonexistent/serialx-compat/path")

    with pytest.raises(FileNotFoundError):
        port.open()


@pytest.mark.skipif(sys.platform == "win32", reason="Uses a POSIX pty")
def test_compat_break_condition_and_send_break() -> None:
    """pyserial-style `break_condition` and `send_break` work unchanged."""
    import pty  # noqa: PLC0415

    controller, device = pty.openpty()
    try:
        with serial.serial_for_url(os.ttyname(device), baudrate=115200) as port:
            assert port.break_condition is False
            port.break_condition = True
            assert port.break_condition is True
            port.break_condition = False
            assert port.break_condition is False
            port.send_break(duration=0.01)
            assert port.break_condition is False
    finally:
        os.close(controller)
        os.close(device)


def test_compat_break_unsupported_raises_serial_exception() -> None:
    """Backends without break support raise `SerialException`, not silently pass."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        host, port_number = server.getsockname()[:2]

        with serial.serial_for_url(f"socket://{host}:{port_number}") as port:
            with pytest.raises(serial.SerialException):
                port.break_condition = True

            with pytest.raises(serial.SerialException):
                port.send_break(duration=0.01)

            assert port.break_condition is False
