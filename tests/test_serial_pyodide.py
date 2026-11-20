"""Pyodide serial port tests."""

import asyncio
import sys

import pytest

if sys.platform != "emscripten":
    pytest.skip(
        "PyodideSerialTransport is only available under Pyodide",
        allow_module_level=True,
    )

from serialx import Parity, StopBits, create_serial_connection
from serialx.common import SerialException, UnsupportedSetting
from serialx.platforms.serial_pyodide import PyodideSerial
from tests.common import create_pyodide_pair


async def test_pyodide_one_point_five_stopbits_unsupported() -> None:
    """1.5 stop bits is not supported."""
    with create_pyodide_pair() as (left, _):
        with pytest.raises(UnsupportedSetting, match="stopbits"):
            await create_serial_connection(
                asyncio.get_running_loop(),
                asyncio.Protocol,
                url=left,
                baudrate=115200,
                stopbits=StopBits.ONE_POINT_FIVE,
            )


async def test_pyodide_mark_parity_unsupported() -> None:
    """Mark parity is not supported."""
    with create_pyodide_pair() as (left, _):
        with pytest.raises(UnsupportedSetting, match="parity"):
            await create_serial_connection(
                asyncio.get_running_loop(),
                asyncio.Protocol,
                url=left,
                baudrate=115200,
                parity=Parity.MARK,
            )


async def test_pyodide_invalid_byte_size_unsupported() -> None:
    """Byte sizes other than 7 or 8 are not supported."""
    with create_pyodide_pair() as (left, _):
        with pytest.raises(UnsupportedSetting, match="byte_size"):
            await create_serial_connection(
                asyncio.get_running_loop(),
                asyncio.Protocol,
                url=left,
                baudrate=115200,
                byte_size=5,
            )


def test_pyodide_serial_repr() -> None:
    """repr() works on the settings-only sync class."""
    serial = PyodideSerial("pyodide://port0", baudrate=115200)
    assert repr(serial) == (
        "<PyodideSerial path='pyodide://port0' baudrate=115200 open=False>"
    )


async def test_pyodide_unregistered_port_raises() -> None:
    """Connecting to a URL with no registered JS port raises."""
    with pytest.raises(SerialException, match="No JS serial port registered"):
        await create_serial_connection(
            asyncio.get_running_loop(),
            asyncio.Protocol,
            url="pyodide://not-registered",
            baudrate=115200,
        )
