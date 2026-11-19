"""POSIX serial port tests."""

import sys

import pytest

if sys.platform in ("win32", "emscripten"):
    pytest.skip("POSIX-only tests", allow_module_level=True)

import array
import asyncio
import errno
import fcntl
import os
import pty
import termios
import time
from typing import Any
from unittest.mock import patch

from serialx import (
    ModemPins,
    PinState,
    SerialException,
    UnsupportedSetting,
    async_serial_for_url,
)
from serialx.platforms import serial_posix
from serialx.platforms.serial_posix import TIOCCBRK, TIOCSBRK, PosixSerial


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


@pytest.mark.parametrize(
    ("platform", "expected_set", "expected_clear"),
    [
        ("linux", 0x5427, 0x5428),
        ("darwin", 0x2000747B, 0x2000747A),
        ("freebsd", 0x2000747B, 0x2000747A),
    ],
)
def test_break_ioctl_constants(
    platform: str, expected_set: int, expected_clear: int
) -> None:
    """TIOCSBRK/TIOCCBRK resolve to the platform's ioctl numbers."""
    if not sys.platform.startswith(platform):
        pytest.skip(f"{platform}-only constants")

    if os.uname().machine.startswith("sparc"):
        pytest.skip("SPARC uses the BSD numbers")

    assert expected_set == serial_posix.TIOCSBRK
    assert expected_clear == serial_posix.TIOCCBRK


def test_break_condition_uses_tiocsbrk_and_tioccbrk() -> None:
    """Setting `break_condition` issues TIOCSBRK, clearing it TIOCCBRK."""
    seen_requests = []

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        seen_requests.append(request)
        return 0

    serial = PosixSerial(fileno=1)
    with (
        patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl),
        patch("serialx.common.time.sleep") as sleep,
    ):
        serial.break_condition = True
        assert serial.break_condition is True
        serial.break_condition = False
        assert serial.break_condition is False

        serial.send_break(0.3)
        sleep.assert_called_once_with(0.3)
        assert serial.break_condition is False

        serial.send_break()
        assert sleep.call_args.args == (0.25,)

    assert seen_requests == [
        TIOCSBRK,
        TIOCCBRK,
        TIOCSBRK,
        TIOCCBRK,
        TIOCSBRK,
        TIOCCBRK,
    ]


def test_send_break_clears_break_when_interrupted() -> None:
    """The break is released even if the sleep is interrupted."""
    seen_requests = []

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        seen_requests.append(request)
        return 0

    serial = PosixSerial(fileno=1)
    with (
        patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl),
        patch("serialx.common.time.sleep", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        serial.send_break()

    assert seen_requests == [TIOCSBRK, TIOCCBRK]
    assert serial.break_condition is False


def test_break_condition_failure_keeps_state() -> None:
    """A failing ioctl raises and leaves the tracked break condition unchanged."""

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        raise OSError(errno.EIO, "I/O error")

    serial = PosixSerial(fileno=1)
    with (
        patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl),
        pytest.raises(OSError),
    ):
        serial.break_condition = True

    assert serial.break_condition is False


def test_break_condition_requires_open_port() -> None:
    """Setting the break condition on a closed port raises `SerialException`."""
    serial = PosixSerial()

    with pytest.raises(SerialException, match="not open"):
        serial.break_condition = True

    with pytest.raises(SerialException, match="not open"):
        serial.send_break()


def test_break_condition_enotty_raises_unsupported_setting() -> None:
    """A tty driver without break support (`ENOTTY`) raises `UnsupportedSetting`."""

    def ioctl(fd: int, request: int, arg: Any = 0, mutate_flag: bool = True) -> int:
        raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

    serial = PosixSerial(fileno=1)
    with (
        patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl),
        pytest.raises(UnsupportedSetting, match="does not support break"),
    ):
        serial.send_break()

    assert serial.break_condition is False


async def test_async_break_condition_resets_on_close() -> None:
    """A closed transport no longer reports a held break condition."""
    controller, device = pty.openpty()
    try:
        async with async_serial_for_url(os.ttyname(device), baudrate=115200) as serial:
            transport = serial.transport
            await serial.set_break_condition(True)
            assert transport.break_condition is True

        assert transport.break_condition is False
    finally:
        os.close(controller)
        os.close(device)


async def test_async_send_break_clears_break_when_cancelled() -> None:
    """The break is released even if `send_break` is cancelled while asserting it."""
    seen_requests = []
    real_ioctl = fcntl.ioctl

    def ioctl(fd: int, request: int, *args: Any) -> Any:
        if request not in (TIOCSBRK, TIOCCBRK):
            return real_ioctl(fd, request, *args)

        seen_requests.append(request)

        # Keep the call in flight for long enough to be cancelled
        if request == TIOCSBRK:
            time.sleep(0.05)

        return 0

    controller, device = pty.openpty()
    try:
        async with async_serial_for_url(os.ttyname(device), baudrate=115200) as serial:
            with patch("serialx.platforms.serial_posix.fcntl.ioctl", side_effect=ioctl):
                task = asyncio.ensure_future(serial.send_break(1))

                # Cancel only once the worker thread is inside the TIOCSBRK call
                while not seen_requests:
                    await asyncio.sleep(0.001)

                task.cancel()

                with pytest.raises(asyncio.CancelledError):
                    await task

            assert seen_requests == [TIOCSBRK, TIOCCBRK]
            assert serial.break_condition is False
    finally:
        os.close(controller)
        os.close(device)
