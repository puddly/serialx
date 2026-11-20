"""Tests for the Win32 serial port implementation."""

from __future__ import annotations

import errno
import sys
from unittest.mock import patch

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only tests", allow_module_level=True)

import pywintypes
from winerror import ERROR_INVALID_HANDLE

from serialx.platforms import _win32api
from serialx.platforms.serial_win32 import Win32Serial

# `INVALID_HANDLE_VALUE`. Every comm call rejects it; `CloseHandle` accepts it.
INVALID_HANDLE = -1


@pytest.mark.parametrize(
    "name", ["GetCommModemStatus", "ClearCommError", "SetCommBreak", "ClearCommBreak"]
)
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


def test_break_condition_uses_set_and_clear_comm_break() -> None:
    """Break maps to `SetCommBreak`/`ClearCommBreak` on the port handle."""
    port = Win32Serial()
    port._handle = 1234

    with (
        patch("serialx.platforms.serial_win32.SetCommBreak") as set_break,
        patch("serialx.platforms.serial_win32.ClearCommBreak") as clear_break,
        patch("serialx.common.time.sleep") as sleep,
    ):
        port.break_condition = True
        set_break.assert_called_once_with(1234)
        assert port.break_condition is True

        port.break_condition = False
        clear_break.assert_called_once_with(1234)
        assert port.break_condition is False

        port.send_break(0.1)
        sleep.assert_called_once_with(0.1)
        assert set_break.call_count == 2
        assert clear_break.call_count == 2

    port._handle = None
