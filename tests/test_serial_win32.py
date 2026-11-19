"""Tests for the Win32 serial port implementation."""

from __future__ import annotations

import errno
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only tests", allow_module_level=True)

import pywintypes
from win32con import DTR_CONTROL_DISABLE, RTS_CONTROL_DISABLE
from winerror import ERROR_INVALID_HANDLE

from serialx.platforms import _win32api
from serialx.platforms.serial_win32 import CLRDTR, CLRRTS, Win32Serial

# `INVALID_HANDLE_VALUE`. Every comm call rejects it; `CloseHandle` accepts it.
INVALID_HANDLE = -1


@pytest.mark.parametrize("name", ["GetCommModemStatus", "ClearCommError"])
def test_win32_failure_raises_os_error(name: str) -> None:
    """A real pywin32 failure surfaces as `OSError` with the Win32 code in `winerror`."""
    func = getattr(_win32api, name)

    with pytest.raises(OSError) as raised:
        func(INVALID_HANDLE)

    assert not isinstance(raised.value, pywintypes.error)
    assert raised.value.winerror == ERROR_INVALID_HANDLE
    assert raised.value.errno == errno.EBADF


def test_close_tolerates_failing_win32_calls() -> None:
    """Closing a port whose device is gone does not raise."""

    port = Win32Serial()

    # An unplugged device makes `EscapeCommFunction` fail on close. An invalid handle
    # fails it the same way, with a real `pywintypes.error` from pywin32.
    port._handle = INVALID_HANDLE
    port.close()

    assert not port.is_open


def test_reconfigure_keeps_modem_pins() -> None:
    """`SetCommState` gets DTR/RTS as last set, not the on-open state."""
    port = Win32Serial(baudrate=9600)
    port._handle = 1234
    dcbs = []

    with (
        patch(
            "serialx.platforms.serial_win32.GetCommState",
            return_value=SimpleNamespace(),
        ),
        patch(
            "serialx.platforms.serial_win32.SetCommState",
            side_effect=lambda h, dcb: dcbs.append(dcb),
        ),
        patch("serialx.platforms.serial_win32.SetCommTimeouts"),
        patch("serialx.platforms.serial_win32.ClearCommError"),
        patch("serialx.platforms.serial_win32.EscapeCommFunction") as escape,
    ):
        port.set_modem_pins(dtr=False, rts=False)
        escape.reset_mock()

        port.reconfigure_port(baudrate=19200)

    assert dcbs[-1].BaudRate == 19200
    assert dcbs[-1].fRtsControl == RTS_CONTROL_DISABLE
    assert dcbs[-1].fDtrControl == DTR_CONTROL_DISABLE
    assert {c.args[1] for c in escape.call_args_list} == {CLRRTS, CLRDTR}

    port._handle = None
