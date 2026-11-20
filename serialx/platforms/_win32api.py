"""pywin32 entry points re-raising `pywintypes.error` as `OSError`."""

from __future__ import annotations

from collections.abc import Callable
import functools
from typing import ParamSpec, TypeVar

import pywintypes
import win32event
import win32file

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _translated(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    # The default `assigned` includes `__type_params__`, which is not a tuple on the
    # Sphinx autodoc mocks that stand in for pywin32 when building docs off-Windows.
    @functools.wraps(fn, assigned=("__module__", "__name__", "__qualname__", "__doc__"))
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return fn(*args, **kwargs)
        except pywintypes.error as e:
            # Passing the Win32 code as `winerror` lets CPython derive `errno`
            raise OSError(None, e.strerror, None, e.winerror) from e

    return wrapper


CreateEvent = _translated(win32event.CreateEvent)
ResetEvent = _translated(win32event.ResetEvent)
WaitForSingleObject = _translated(win32event.WaitForSingleObject)

CancelIo = _translated(win32file.CancelIo)
ClearCommBreak = _translated(win32file.ClearCommBreak)
ClearCommError = _translated(win32file.ClearCommError)
CloseHandle = _translated(win32file.CloseHandle)
CreateFile = _translated(win32file.CreateFile)
EscapeCommFunction = _translated(win32file.EscapeCommFunction)
FlushFileBuffers = _translated(win32file.FlushFileBuffers)
GetCommModemStatus = _translated(win32file.GetCommModemStatus)
GetCommState = _translated(win32file.GetCommState)
GetOverlappedResult = _translated(win32file.GetOverlappedResult)
PurgeComm = _translated(win32file.PurgeComm)
ReadFile = _translated(win32file.ReadFile)
SetCommBreak = _translated(win32file.SetCommBreak)
SetCommState = _translated(win32file.SetCommState)
SetCommTimeouts = _translated(win32file.SetCommTimeouts)
SetupComm = _translated(win32file.SetupComm)
WriteFile = _translated(win32file.WriteFile)
