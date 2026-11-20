"""POSIX serial port tests."""

import sys

import pytest

if sys.platform in ("win32", "emscripten"):
    pytest.skip("POSIX-only tests", allow_module_level=True)

import array
import asyncio
from collections.abc import Iterator
import errno
import os
import termios
import threading
from typing import Any
from unittest.mock import patch

from serialx import ModemPins, PinState, UnsupportedSetting, async_serial_for_url
from serialx.platforms.serial_posix import PosixSerial


def test_num_unread_bytes_uses_native_int_ioctl_buffer() -> None:
    """FIONREAD writes a native C int, not a little-endian byte string."""

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        assert request == termios.FIONREAD
        assert isinstance(arg, array.array)
        assert arg.typecode == "i"
        arg[0] = 123
        return 0

    serial = PosixSerial(fileno=1)
    with patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl):
        assert serial.num_unread_bytes() == 123


def test_num_unwritten_bytes_uses_native_int_ioctl_buffer() -> None:
    """TIOCOUTQ writes a native C int, not a little-endian byte string."""

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        assert request == termios.TIOCOUTQ
        assert isinstance(arg, array.array)
        assert arg.typecode == "i"
        arg[0] = 456
        return 0

    serial = PosixSerial(fileno=1)
    with patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl):
        assert serial.num_unwritten_bytes() == 456


def test_get_modem_pins_uses_native_int_ioctl_buffer() -> None:
    """TIOCMGET writes a native C int, not a little-endian byte string."""

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        assert request == termios.TIOCMGET
        assert isinstance(arg, array.array)
        assert arg.typecode == "i"
        arg[0] = termios.TIOCM_DTR
        return 0

    serial = PosixSerial(fileno=1)
    with patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl):
        pins = serial.get_modem_pins()

    assert pins.dtr is PinState.HIGH
    assert pins.rts is PinState.LOW


def test_set_modem_pins_uses_native_int_ioctl_buffer() -> None:
    """TIOCMSET reads a native C int, not a little-endian byte string."""

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        assert request == termios.TIOCMSET
        assert isinstance(arg, array.array)
        assert arg.typecode == "i"
        assert arg[0] & termios.TIOCM_DTR
        assert arg[0] & termios.TIOCM_RTS
        return 0

    serial = PosixSerial(fileno=1)
    with patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl):
        serial.set_modem_pins(
            ModemPins(
                le=PinState.LOW,
                dtr=PinState.HIGH,
                rts=PinState.HIGH,
                st=PinState.LOW,
                sr=PinState.LOW,
                cts=PinState.LOW,
                car=PinState.LOW,
                rng=PinState.LOW,
                dsr=PinState.LOW,
            )
        )


def test_partial_set_modem_pins_uses_native_int_ioctl_buffer() -> None:
    """TIOCMBIS/TIOCMBIC read native C ints, not little-endian byte strings."""

    seen_requests = []

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        assert isinstance(arg, array.array)
        assert arg.typecode == "i"
        seen_requests.append(request)
        if request == termios.TIOCMBIS:
            assert arg[0] == termios.TIOCM_DTR
        elif request == termios.TIOCMBIC:
            assert arg[0] == termios.TIOCM_RTS
        else:
            raise OSError(errno.ENOTTY, "unexpected ioctl")
        return 0

    serial = PosixSerial(fileno=1)
    with patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl):
        serial.set_modem_pins(dtr=True, rts=False)

    assert seen_requests == [termios.TIOCMBIS, termios.TIOCMBIC]


def test_termios_error_raised_as_os_error_bad_descriptor() -> None:
    """`termios.error` are re-raised as `OSError` with the errno from the C call."""

    # Pass bad file descriptor directly, _open() would raise ValueError
    serial = PosixSerial(fileno=1)
    with pytest.raises(OSError) as raised:
        serial.reconfigure_port()

    assert raised.value.errno is not None
    assert raised.value.strerror is not None


@pytest.fixture
def pty_path() -> Iterator[str]:
    """Yield the path of a fresh pty, closing it afterwards."""
    controller, peripheral = os.openpty()

    try:
        yield os.ttyname(peripheral)
    finally:
        os.close(peripheral)
        os.close(controller)


def test_reconfigure_rejected_rtscts_keeps_port_usable(pty_path: str) -> None:
    """A rejected RTS/CTS update does not break later reconfigures."""
    with PosixSerial(pty_path, baudrate=9600) as serial:
        with pytest.raises(UnsupportedSetting, match="RTS/CTS"):
            serial.reconfigure_port(rtscts=True)

        assert serial._rtscts is False

        serial.reconfigure_port(baudrate=19200)
        assert serial.baudrate == 19200
        assert termios.tcgetattr(serial.fileno())[5] == termios.B19200


async def test_async_reconfigure_rejected_rtscts_keeps_port_usable(
    pty_path: str,
) -> None:
    """The async transport also restores the settings of a rejected update."""
    async with async_serial_for_url(f"posix://{pty_path}", baudrate=9600) as serial:
        with pytest.raises(UnsupportedSetting, match="RTS/CTS"):
            await serial.reconfigure_port(rtscts=True)

        await serial.reconfigure_port(baudrate=19200)
        assert serial.baudrate == 19200


async def test_async_reconfigure_cancelled_mid_apply_keeps_new_settings(
    pty_path: str,
) -> None:
    """A cancelled reconfigure keeps the values the executor thread still applies."""
    started = threading.Event()
    release = threading.Event()
    applied = threading.Event()
    apply_attributes = PosixSerial._apply_attributes

    def slow_apply(self: PosixSerial, attributes: list[Any]) -> None:
        started.set()
        release.wait(timeout=5)
        apply_attributes(self, attributes)
        applied.set()

    async with async_serial_for_url(f"posix://{pty_path}", baudrate=9600) as serial:
        with patch.object(PosixSerial, "_apply_attributes", slow_apply):
            task = asyncio.create_task(serial.reconfigure_port(baudrate=19200))
            assert await asyncio.to_thread(started.wait, 5)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            release.set()
            assert await asyncio.to_thread(applied.wait, 5)

        assert serial.baudrate == 19200

        fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY)
        try:
            assert termios.tcgetattr(fd)[5] == termios.B19200
        finally:
            os.close(fd)
