"""POSIX serial port tests."""

import sys

import pytest

if sys.platform in ("win32", "emscripten"):
    pytest.skip("POSIX-only tests", allow_module_level=True)

import array
from collections.abc import Iterator
import errno
import os
import pty
import select
import termios
import time
import tty
from typing import Any
from unittest.mock import patch

from serialx import ModemPins, PinState, Serial, async_serial_for_url
from serialx.platforms.serial_posix import PosixSerial


@pytest.fixture
def pty_pair() -> Iterator[tuple[int, str]]:
    """Raw pty master file descriptor and the path of its slave."""
    master, slave = pty.openpty()
    tty.setraw(master)
    path = os.ttyname(slave)

    try:
        yield master, path
    finally:
        os.close(slave)
        os.close(master)


def _read_master(master: int, size: int, timeout: float = 2.0) -> bytes:
    data = b""
    deadline = time.monotonic() + timeout

    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        readable, _, _ = select.select([master], [], [], remaining)
        if not readable:
            break

        data += os.read(master, size - len(data))

    return data


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


def test_reconfigure_keeps_written_data(pty_pair: tuple[int, str]) -> None:
    """Changing the baudrate does not discard data already accepted by `write`."""
    master, path = pty_pair
    data = bytes(range(64))

    with Serial(path, baudrate=9600) as serial:
        assert serial.write(data) == len(data)
        serial.reconfigure_port(baudrate=19200)

        assert _read_master(master, len(data)) == data


def test_reconfigure_keeps_received_data(pty_pair: tuple[int, str]) -> None:
    """Data received before a reconfigure can still be read after it."""
    master, path = pty_pair
    data = b"received before reconfigure"

    with Serial(path, baudrate=9600, read_timeout=2.0) as serial:
        os.write(master, data)

        deadline = time.monotonic() + 2.0
        while serial.num_unread_bytes() < len(data) and time.monotonic() < deadline:
            time.sleep(0.01)

        serial.reconfigure_port(baudrate=19200)

        assert serial.readexactly(len(data)) == data


def test_open_discards_stale_input(pty_pair: tuple[int, str]) -> None:
    """Opening the port discards data that arrived before it was opened."""
    master, path = pty_pair
    os.write(master, b"stale")
    time.sleep(0.05)

    with Serial(path, baudrate=9600, read_timeout=2.0) as serial:
        os.write(master, b"fresh")

        assert serial.readexactly(5) == b"fresh"
        assert serial.num_unread_bytes() == 0


async def test_async_open_discards_stale_input_and_reconfigure_keeps_data(
    pty_pair: tuple[int, str],
) -> None:
    """The async transport flushes once at open and not on a reconfigure."""
    master, path = pty_pair
    os.write(master, b"stale")
    time.sleep(0.05)
    data = bytes(range(64))

    async with async_serial_for_url(path, baudrate=9600) as serial:
        os.write(master, b"fresh")
        assert await serial.readexactly(5) == b"fresh"

        await serial.write(data)
        await serial.reconfigure_port(baudrate=19200)

        assert _read_master(master, len(data)) == data
