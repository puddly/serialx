"""Windows serial port implementation using Win32 API."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from typing_extensions import Buffer, Unpack
from win32con import (
    DTR_CONTROL_DISABLE,
    DTR_CONTROL_ENABLE,
    DTR_CONTROL_HANDSHAKE,
    EVENPARITY,
    FILE_ATTRIBUTE_NORMAL,
    FILE_FLAG_OVERLAPPED,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    GENERIC_READ,
    GENERIC_WRITE,
    MARKPARITY,
    MAXDWORD,
    NOPARITY,
    ODDPARITY,
    ONE5STOPBITS,
    ONESTOPBIT,
    OPEN_EXISTING,
    RTS_CONTROL_DISABLE,
    RTS_CONTROL_ENABLE,
    RTS_CONTROL_HANDSHAKE,
    SPACEPARITY,
    TWOSTOPBITS,
)
from win32file import (
    OVERLAPPED,
    PURGE_RXABORT,
    PURGE_RXCLEAR,
    PURGE_TXABORT,
    PURGE_TXCLEAR,
)
from winerror import ERROR_IO_PENDING

from serialx.common import SerialPortInfo
from serialx.serialx_rust import list_serial_ports_impl

from ..common import (
    BaseSerial,
    BaseSerialTransport,
    ConnectKwargs,
    ModemPins,
    Parity,
    PinState,
    PortSettingsUpdate,
    StopBits,
    register_uri_handler,
)
from ._win32api import (
    CancelIo,
    ClearCommError,
    CloseHandle,
    CreateEvent,
    CreateFile,
    EscapeCommFunction,
    FlushFileBuffers,
    GetCommModemStatus,
    GetCommState,
    GetOverlappedResult,
    PurgeComm,
    ReadFile,
    ResetEvent,
    SetCommState,
    SetCommTimeouts,
    SetupComm,
    WriteFile,
)

if TYPE_CHECKING:
    from _win32typing import PyOVERLAPPED

# Constants missing from win32con
MS_CTS_ON = 0x0010
MS_DSR_ON = 0x0020
MS_RING_ON = 0x0040
MS_RLSD_ON = 0x0080
SETXOFF = 1
SETXON = 2
SETRTS = 3
CLRRTS = 4
SETDTR = 5
CLRDTR = 6

LOGGER = logging.getLogger(__name__)

WIN32_PARITY_MAP = {
    Parity.NONE: NOPARITY,
    Parity.ODD: ODDPARITY,
    Parity.EVEN: EVENPARITY,
    Parity.MARK: MARKPARITY,
    Parity.SPACE: SPACEPARITY,
}

WIN32_STOPBITS_MAP = {
    StopBits.ONE: ONESTOPBIT,
    StopBits.ONE_POINT_FIVE: ONE5STOPBITS,
    StopBits.TWO: TWOSTOPBITS,
}


def _normalize_windows_port_path(path: os.PathLike[str] | str) -> str:
    """Normalize a Windows serial device path for CreateFile."""
    normalized = str(path)

    # COM ports >= 10 require the \\.\  prefix for CreateFile
    if not normalized.startswith("\\\\.\\"):
        normalized = "\\\\.\\" + normalized

    return normalized


class CommTimeouts(NamedTuple):
    """COMMTIMEOUTS struct as a 5-tuple, accepted directly by SetCommTimeouts."""

    ReadIntervalTimeout: int
    ReadTotalTimeoutMultiplier: int
    ReadTotalTimeoutConstant: int
    WriteTotalTimeoutMultiplier: int
    WriteTotalTimeoutConstant: int


def _safe_close_handle(handle: int) -> None:
    """Close a Win32 handle, suppressing and logging errors."""
    try:
        CloseHandle(handle)
    except OSError:
        LOGGER.debug("Failed to close handle %r", handle, exc_info=True)


def CreateFile_detached(
    *,
    file_name: str,
    desired_access: int,
    share_mode: int,
    creation_disposition: int,
    flags_and_attributes: int,
) -> int:
    """`CreateFile` returning a raw `int` HANDLE that the caller owns."""
    handle = CreateFile(
        file_name,
        desired_access,
        share_mode,
        None,
        creation_disposition,
        flags_and_attributes,
        None,
    )

    # `Detach()` is stubbed `-> Self` but actually returns the underlying int.
    # Without `Detach()`, we would get a `PyHANDLE` object that closes the handle on
    # `__del__`, masking bugs.
    return cast(int, handle.Detach())


class Win32Serial(BaseSerial):
    """Windows serial port implementation using Win32 API."""

    def __init__(
        self,
        *args: Any,
        handle: int | None = None,
        inter_byte_timeout: float = 0.01,
        read_buffer_size: int = 4096,
        write_buffer_size: int = 4096,
        **kwargs: Any,
    ) -> None:
        """Initialize the Windows serial port."""
        super().__init__(*args, **kwargs)
        self._handle = handle
        self._inter_byte_timeout = inter_byte_timeout
        self._read_buffer_size = read_buffer_size
        self._write_buffer_size = write_buffer_size
        self._overlapped_read: PyOVERLAPPED | None = None
        self._overlapped_write: PyOVERLAPPED | None = None
        self._commtimeouts: CommTimeouts | None = None

    def _apply_commtimeouts(
        self,
        *,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
    ) -> None:
        """Encode and push timeouts to the kernel; skip the syscall if unchanged."""
        assert self._handle is not None
        interval = (
            max(int(self._inter_byte_timeout * 1000), 1)
            if self._inter_byte_timeout
            else 0
        )

        if read_timeout == 0:
            # Documented sentinel: return immediately with whatever's buffered
            read_interval_timeout = MAXDWORD
            read_total_timeout_constant = 0
        elif read_timeout is None:
            read_interval_timeout = interval
            read_total_timeout_constant = 0
        else:
            read_interval_timeout = interval
            read_total_timeout_constant = max(int(read_timeout * 1000), 1)

        if write_timeout is None or write_timeout == 0:
            write_total_timeout_constant = 0
        else:
            write_total_timeout_constant = max(int(write_timeout * 1000), 1)

        timeouts = CommTimeouts(
            ReadIntervalTimeout=read_interval_timeout,
            ReadTotalTimeoutMultiplier=0,
            ReadTotalTimeoutConstant=read_total_timeout_constant,
            WriteTotalTimeoutMultiplier=0,
            WriteTotalTimeoutConstant=write_total_timeout_constant,
        )

        if self._commtimeouts == timeouts:
            return

        SetCommTimeouts(self._handle, timeouts)
        self._commtimeouts = timeouts

    def _open(self) -> None:
        """Open the serial port."""
        LOGGER.debug("Opening serial port %r", self._path)

        if self._handle is not None:
            raise ValueError("Serial port is already open")

        assert self._path is not None
        path = _normalize_windows_port_path(self._path)

        share_mode = 0 if self._exclusive else FILE_SHARE_READ | FILE_SHARE_WRITE

        try:
            self._handle = CreateFile_detached(
                file_name=path,
                desired_access=GENERIC_READ | GENERIC_WRITE,
                share_mode=share_mode,
                creation_disposition=OPEN_EXISTING,
                flags_and_attributes=FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
            )
        except OSError as e:
            e.filename = path
            raise

        self._overlapped_read = OVERLAPPED()
        self._overlapped_read.hEvent = CreateEvent(None, 1, 0, None)
        self._overlapped_write = OVERLAPPED()
        self._overlapped_write.hEvent = CreateEvent(None, 1, 0, None)

        self._auto_close = True
        self._setup_comm()

    @property
    def is_open(self) -> bool:
        """Check if the serial port is open."""
        return self._handle is not None

    def _setup_comm(self) -> None:
        """Set up and clear the driver buffers, once per open."""
        assert self._handle is not None
        SetupComm(self._handle, self._read_buffer_size, self._write_buffer_size)
        PurgeComm(
            self._handle,
            PURGE_TXABORT | PURGE_RXABORT | PURGE_TXCLEAR | PURGE_RXCLEAR,
        )

    def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        """Configure the serial port settings."""
        assert self._handle is not None

        try:
            self._apply_commtimeouts()

            # Configure DCB (Device Control Block)
            dcb = cast(Any, GetCommState(self._handle))  # TODO: fix in typeshed

            if update.baudrate is not None:
                dcb.BaudRate = update.baudrate

            if update.byte_size is not None:
                dcb.ByteSize = update.byte_size

            if update.stopbits is not None:
                dcb.StopBits = WIN32_STOPBITS_MAP[update.stopbits]

            if update.parity is not None:
                dcb.Parity = WIN32_PARITY_MAP[update.parity]

            dcb.fBinary = 1  # Always True on Windows

            # Flow Control
            if update.rtscts is not None:
                if update.rtscts:
                    dcb.fRtsControl = RTS_CONTROL_HANDSHAKE
                    dcb.fOutxCtsFlow = 1
                elif self._rts_on_open is PinState.LOW:
                    dcb.fRtsControl = RTS_CONTROL_DISABLE
                    dcb.fOutxCtsFlow = 0
                else:
                    dcb.fRtsControl = RTS_CONTROL_ENABLE
                    dcb.fOutxCtsFlow = 0

            if update.xonxoff is not None:
                if update.xonxoff:
                    dcb.fOutX = 1
                    dcb.fInX = 1
                else:
                    dcb.fOutX = 0
                    dcb.fInX = 0

            if update.dsrdtr is not None:
                if update.dsrdtr:
                    dcb.fDtrControl = DTR_CONTROL_HANDSHAKE
                    dcb.fOutxDsrFlow = 1
                elif self._dtr_on_open is PinState.LOW:
                    dcb.fDtrControl = DTR_CONTROL_DISABLE
                    dcb.fOutxDsrFlow = 0
                else:
                    dcb.fDtrControl = DTR_CONTROL_ENABLE
                    dcb.fOutxDsrFlow = 0

            dcb.fDsrSensitivity = 0
            dcb.fErrorChar = 0
            dcb.fNull = 0
            dcb.fAbortOnError = 0

            SetCommState(self._handle, dcb)

            # Driver-owned lines (RTS under rtscts, DTR under dsrdtr) are skipped
            self.set_modem_pins(self._modem_pins_on_open())

            # Clear any errors
            ClearCommError(self._handle)
        except OSError:
            LOGGER.debug("Failed to configure port", exc_info=True)
            raise

    def fileno(self) -> int:
        """Return the file descriptor."""
        assert self._handle is not None
        return int(self._handle)

    def _close(self) -> None:
        """Close the serial port and release all handles."""
        if self._handle is not None:
            # Windows has no way to automatically do this on close, we do it manually. A
            # driver-owned line (RTS under rtscts, DTR under dsrdtr) cannot be adjusted
            # via EscapeCommFunction, so those are skipped.
            try:
                self.set_modem_pins(self._modem_pins_on_close())
            except OSError:
                LOGGER.debug("Failed to set modem pins on close", exc_info=True)

            try:
                CancelIo(self._handle)
            except OSError:
                LOGGER.debug("Failed to cancel IO on close", exc_info=True)

            _safe_close_handle(self._handle)
            self._handle = None
            self._commtimeouts = None

        if self._overlapped_read is not None and self._overlapped_read.hEvent:
            _safe_close_handle(self._overlapped_read.hEvent)
        self._overlapped_read = None

        if self._overlapped_write is not None and self._overlapped_write.hEvent:
            _safe_close_handle(self._overlapped_write.hEvent)
        self._overlapped_write = None

    def _get_modem_pins(self) -> ModemPins:
        """Get the current modem control bits."""
        assert self._handle is not None

        stat = GetCommModemStatus(self._handle)
        return ModemPins(
            cts=PinState.HIGH if stat & MS_CTS_ON else PinState.LOW,
            dsr=PinState.HIGH if stat & MS_DSR_ON else PinState.LOW,
            rng=PinState.HIGH if stat & MS_RING_ON else PinState.LOW,
            car=PinState.HIGH if stat & MS_RLSD_ON else PinState.LOW,
        )

    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set the modem control bits."""
        assert self._handle is not None

        if modem_pins.rts is not PinState.UNDEFINED:
            EscapeCommFunction(  # type:ignore[call-arg]
                self._handle, (SETRTS if modem_pins.rts is PinState.HIGH else CLRRTS)
            )

        if modem_pins.dtr is not PinState.UNDEFINED:
            EscapeCommFunction(  # type:ignore[call-arg]
                self._handle, (SETDTR if modem_pins.dtr is PinState.HIGH else CLRDTR)
            )

    def num_unread_bytes(self) -> int:
        """Return the number of bytes waiting to be read."""
        assert self._handle is not None
        _flags, comstat = ClearCommError(self._handle)
        return comstat.cbInQue

    def num_unwritten_bytes(self) -> int:
        """Return the number of bytes waiting to be written."""
        assert self._handle is not None
        _flags, comstat = ClearCommError(self._handle)
        return comstat.cbOutQue

    def _reset_read_buffer(self) -> None:
        """Reset the read buffer."""
        assert self._handle is not None
        PurgeComm(self._handle, PURGE_RXABORT | PURGE_RXCLEAR)

    def _reset_write_buffer(self) -> None:
        """Reset the write buffer."""
        assert self._handle is not None
        PurgeComm(self._handle, PURGE_TXABORT | PURGE_TXCLEAR)

    def _flush(self) -> None:
        """Flush write buffers."""
        assert self._handle is not None
        FlushFileBuffers(self._handle)

    def _readinto(self, b: Buffer, *, timeout: float | None) -> int:
        """Read data into the provided bytearray."""
        assert self._overlapped_read is not None
        assert self._handle is not None

        self._apply_commtimeouts(read_timeout=timeout)
        ResetEvent(self._overlapped_read.hEvent)

        ReadFile(self._handle, b, self._overlapped_read)

        return GetOverlappedResult(self._handle, self._overlapped_read, True)

    def _write(self, data: Buffer, *, timeout: float | None) -> int:
        """Write data to the serial port synchronously."""
        assert self._overlapped_write is not None
        assert self._handle is not None

        self._apply_commtimeouts(write_timeout=timeout)
        ResetEvent(self._overlapped_write.hEvent)

        err, _ = WriteFile(self._handle, data, self._overlapped_write)  # type:ignore[arg-type]

        if err == ERROR_IO_PENDING and timeout == 0:
            # Fire-and-forget: the kernel accepted the whole write.
            return memoryview(data).nbytes

        n = GetOverlappedResult(self._handle, self._overlapped_write, True)

        expected_bytes = memoryview(data).nbytes
        if timeout is not None and timeout > 0 and n != expected_bytes:
            raise TimeoutError(
                f"Write timeout: wrote {n} of {expected_bytes} in {timeout:0.2f}"
            )

        return n


class _MethodProxy:
    """Proxy object that forwards attribute access to a mapping."""

    def __init__(self, name: str, mapping: dict[str, Any]):
        """Initialize the method proxy."""
        self._name = name
        self._mapping = mapping

    def eof_received(self) -> bool:
        """Handle EOF by signalling the transport to close."""
        return False

    def __getattr__(self, name: str) -> Any:
        """Forward attribute access to the mapping."""
        try:
            return self._mapping[name]
        except KeyError:
            raise AttributeError(f"{self._name!r} has no attribute {name!r}") from None


class Win32SerialTransport(BaseSerialTransport):
    """Windows serial transport using ProactorEventLoop."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        """Initialize the Windows serial transport."""
        if not hasattr(loop, "_make_duplex_pipe_transport"):
            raise RuntimeError(
                f"Win32SerialTransport requires ProactorEventLoop, got {loop}"
            )

        super().__init__(loop, protocol)

        self._handle: int | None = None
        self._open_fut: asyncio.Future[int] | None = None
        self._internal_transport: asyncio.Transport | None = None
        self._close_future: asyncio.Future[None] | None = None
        self._closing: bool = False
        self._connect_in_progress: bool = False
        self._connection_made_waiter: asyncio.Future[None] | None = None
        self._pending_connection_lost_exc: Exception | None = None

    def serial_close(self) -> None:
        """Release the handle off the event loop, then report connection lost."""
        if self._close_future is not None:
            return

        serial = self._serial
        self._serial = None
        self._handle = None

        if serial is None:
            self._call_protocol_connection_lost(self._pending_connection_lost_exc)
            return

        self._close_future = self._loop.run_in_executor(None, serial.close)
        self._close_future.add_done_callback(self._on_serial_closed)

    def _on_serial_closed(self, fut: asyncio.Future[None]) -> None:
        # Consume the future's exception so it does not surface later as a noisy warning
        if (exc := fut.exception()) is not None:
            self._loop.call_exception_handler(
                {
                    "message": "Unhandled exception while closing the serial port",
                    "exception": exc,
                    "transport": self,
                    "protocol": self._protocol,
                }
            )

        self._call_protocol_connection_lost(self._pending_connection_lost_exc)

    def serial_shutdown(self, how: int) -> None:
        """Shutdown the serial connection."""
        # Intentionally ignored

    def serial_fileno(self) -> int:
        """Return the file descriptor."""
        assert self._serial is not None
        return self._serial.fileno()

    def protocol_data_received(self, data: bytes) -> None:
        """Forward data_received to the protocol."""
        self._protocol.data_received(data)

    def protocol_connection_made(self, transport: asyncio.Transport) -> None:
        """Forward connection_made to the protocol."""

        # Ignore `transport` and pass self instead
        self._call_protocol_connection_made()

        if (
            self._connection_made_waiter is not None
            and not self._connection_made_waiter.done()
        ):
            self._connection_made_waiter.set_result(None)

    def protocol_connection_lost(self, exc: Exception | None) -> None:
        """Stash the connection-lost reason, `serial_close` dispatches it."""
        self._pending_connection_lost_exc = exc

    def protocol_pause_writing(self) -> None:
        """Forward pause_writing to the protocol."""
        self._protocol.pause_writing()

    def protocol_resume_writing(self) -> None:
        """Forward resume_writing to the protocol."""
        self._protocol.resume_writing()

    def _maybe_resolve_closed_waiter(self) -> None:
        """Potentially resolve the closed future when the transport is closing."""
        if not self._closing:
            return
        if self._connect_in_progress:
            return
        if self._open_fut is not None:
            return
        if self._internal_transport is not None:
            return
        if self._handle is not None:
            return

        self._resolve_closed_waiter()

    def _on_cancelled_open_done(self, open_fut: asyncio.Future[int]) -> None:
        if self._open_fut is not open_fut:
            return

        self._open_fut = None

        try:
            handle = open_fut.result()
        except BaseException:
            pass
        else:
            _safe_close_handle(handle)

        self._maybe_resolve_closed_waiter()

    async def _open(
        self, path: str | os.PathLike[str], *, exclusive: bool = True
    ) -> None:
        """Open the serial port."""
        if self._open_fut is not None:
            raise RuntimeError("Open is already in progress")

        normalized_path = _normalize_windows_port_path(path)
        share_mode = 0 if exclusive else FILE_SHARE_READ | FILE_SHARE_WRITE

        # Use `CreateFile_detached` so the future's result is a plain `int`,
        # matching the shape of `os.open` on POSIX. The PyHANDLE wrapper
        # never escapes the executor thread, so its `tp_dealloc` can't paper
        # over a missing explicit close on cancel.
        self._open_fut = self._loop.run_in_executor(
            None,
            functools.partial(
                CreateFile_detached,
                file_name=normalized_path,
                desired_access=GENERIC_READ | GENERIC_WRITE,
                share_mode=share_mode,
                creation_disposition=OPEN_EXISTING,
                flags_and_attributes=FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
            ),
        )

        try:
            # Shield so a cancellation of the awaiting task doesn't cancel the
            # executor future. Otherwise, when `CreateFile` completes after the
            # cancel, `wrap_future` drops the result silently and the HANDLE
            # is leaked.
            handle = await asyncio.shield(self._open_fut)
        except asyncio.CancelledError:
            self._open_fut.add_done_callback(self._on_cancelled_open_done)
            raise
        except OSError as e:
            self._open_fut = None
            e.filename = normalized_path
            raise

        self._open_fut = None
        self._handle = handle

    async def _connect(
        self, *, path: str | None = None, **kwargs: Unpack[ConnectKwargs]
    ) -> None:
        """Connect to the serial port."""
        if self._closing:
            self._resolve_closed_waiter()
            return

        self._connect_in_progress = True

        if path is None:
            raise ValueError("A serial path is required")

        try:
            exclusive = kwargs.get("exclusive", True)
            await self._open(path, exclusive=exclusive)

            # Ensure inter_byte_timeout is set to a small value to enable
            # "Wait for first byte, then return on gap" behavior for ReadFile.
            # If 0 (default), ReadFile with default timeouts might wait for full buffer.
            original_inter_byte_timeout = kwargs.get("inter_byte_timeout", 0)
            if original_inter_byte_timeout == 0:
                kwargs["inter_byte_timeout"] = 0.01

            self._serial = Win32Serial(
                **kwargs,
                path=path,
                handle=self._handle,
            )
            self._extra["serial"] = self._serial

            await self._loop.run_in_executor(None, self._serial._setup_comm)
            await self._loop.run_in_executor(
                None, self._serial._reconfigure_port, self._serial._all_settings()
            )

            if self._closing:
                await self._loop.run_in_executor(None, self._serial.close)  # type: ignore[unreachable]
                self._serial = None
                self._handle = None
                self._resolve_closed_waiter()
                return

            # Use the internal _make_duplex_pipe_transport to create a true overlapping
            # bidirectional transport on the single handle.
            assert hasattr(self._loop, "_make_duplex_pipe_transport")
            self._connection_made_waiter = self._loop.create_future()
            self._internal_transport = self._loop._make_duplex_pipe_transport(
                # Proxy access to serial and protocol attributes through this instance
                sock=_MethodProxy(
                    "sock",
                    {
                        "fileno": self.serial_fileno,
                        "close": self.serial_close,
                        "shutdown": self.serial_shutdown,
                    },
                ),
                protocol=_MethodProxy(
                    "protocol",
                    {
                        "connection_made": self.protocol_connection_made,
                        "data_received": self.protocol_data_received,
                        "connection_lost": self.protocol_connection_lost,
                        "pause_writing": self.protocol_pause_writing,
                        "resume_writing": self.protocol_resume_writing,
                    },
                ),
                extra=self._extra,
            )
            if self._closing:
                self._internal_transport.close()  # type: ignore[unreachable]
                return

            # The internal duplex pipe transport schedules connection_made via
            # call_soon. Wait for it so callers can assume MADE upon return.
            await self._connection_made_waiter
        except BaseException:
            if self._handle is not None:
                await self._loop.run_in_executor(None, _safe_close_handle, self._handle)

            self._serial = None
            self._handle = None
            raise
        finally:
            self._connect_in_progress = False
            self._maybe_resolve_closed_waiter()

    def get_write_buffer_size(self) -> int:
        """Return the current size of the write buffer."""
        if self._internal_transport is None:
            return 0

        return self._internal_transport.get_write_buffer_size()

    def get_write_buffer_limits(self) -> tuple[int, int]:
        """Return the write buffer limits."""
        if self._internal_transport is None:
            return (0, 0)

        return self._internal_transport.get_write_buffer_limits()

    def set_write_buffer_limits(
        self, high: int | None = None, low: int | None = None
    ) -> None:
        """Set the write buffer limits."""
        if self._internal_transport is None:
            raise RuntimeError("Transport not connected")

        self._internal_transport.set_write_buffer_limits(high=high, low=low)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Write data to the transport."""
        if self._internal_transport is None:
            raise RuntimeError("Transport not connected")

        self._internal_transport.write(data)

    def close(self) -> None:
        """Close the transport."""
        self._closing = True
        self._arm_close_timeout()

        if self._internal_transport is not None:
            # Internal transport closes self._serial via sock.close()
            self._internal_transport.close()
        else:
            self._maybe_resolve_closed_waiter()

    def abort(self) -> None:
        """Abort the transport immediately."""
        self._closing = True
        if self._internal_transport is not None:
            self._internal_transport.abort()
        else:
            self._maybe_resolve_closed_waiter()

    def pause_reading(self) -> None:
        """Pause reading from the transport."""
        if self._internal_transport is not None:
            self._internal_transport.pause_reading()

    def resume_reading(self) -> None:
        """Resume reading from the transport."""
        if self._internal_transport is not None:
            self._internal_transport.resume_reading()

    def set_protocol(self, protocol: asyncio.Protocol) -> None:  # type: ignore[override]
        """Set the protocol."""
        self._protocol = protocol
        if self._internal_transport is not None:
            self._internal_transport.set_protocol(protocol)

    async def _flush(self) -> None:
        """Flush write buffers, waiting until all data is written, internal."""
        assert self._serial is not None
        assert self._internal_transport is not None
        try:
            # Wait for asyncio buffer to drain
            await self._internal_transport._make_empty_waiter()  # type:ignore[attr-defined]

            # Wait for hardware buffer to flush
            await self._loop.run_in_executor(None, self._serial.flush)
        finally:
            self._internal_transport._reset_empty_waiter()  # type:ignore[attr-defined]

    async def _get_modem_pins(self) -> ModemPins:
        """Get modem control bits, internal."""
        assert self._serial is not None
        return await self._loop.run_in_executor(None, self._serial.get_modem_pins)

    async def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set modem control bits, internal."""
        assert self._serial is not None
        await self._loop.run_in_executor(None, self._serial.set_modem_pins, modem_pins)

    async def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        assert self._serial is not None
        await self._loop.run_in_executor(None, self._serial._reconfigure_port, update)


def win32_list_serial_ports() -> list[SerialPortInfo]:
    """List available serial ports on Windows."""
    return [
        SerialPortInfo(
            device=port.device,
            resolved_device=port.device,
            vid=port.vid,
            pid=port.pid,
            serial_number=port.serial_number,
            manufacturer=port.manufacturer,
            product=port.product,
            bcd_device=port.bcd_device,
            interface_description=port.interface_description,
            interface_num=port.interface_num,
        )
        for port in list_serial_ports_impl()
    ]


async def async_win32_list_serial_ports() -> list[SerialPortInfo]:
    """List available serial ports on Windows, async."""
    return await asyncio.to_thread(win32_list_serial_ports)


register_uri_handler(
    scheme="device://",
    unique_scheme="windows://",
    sync_cls=Win32Serial,
    async_transport_cls=Win32SerialTransport,
    list_serial_ports_func=win32_list_serial_ports,
    async_list_serial_ports_func=async_win32_list_serial_ports,
    strip_uri_scheme=True,
)
