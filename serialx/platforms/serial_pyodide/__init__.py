"""Pyodide serial port implementation using the Web Serial API.

Under `Pyodide <https://pyodide.org/>`_ this module registers a transport for the
``pyodide://`` URI scheme (and for scheme-less ``device://`` fallbacks), backed by
the browser's `Web Serial API
<https://developer.mozilla.org/en-US/docs/Web/API/Web_Serial_API>`_. Only the async
transport :class:`PyodideSerialTransport` is implemented; there is no synchronous
:class:`~serialx.common.BaseSerial` equivalent because Web Serial is promise-based.

Because ``navigator.serial.requestPort()`` must be called in response to a user
gesture from JavaScript, the JS ``SerialPort`` object cannot be obtained from Python.
Instead, the host page obtains a ``SerialPort`` and hands it to Python via
:func:`register_js_port`, associating it with a URI path. Subsequent calls to
:func:`serialx.open_serial_connection` (or the lower-level transport) look the path
up in the registry. A ``SerialPort`` may also be passed directly via the ``js_port``
keyword argument, bypassing the registry.

See :doc:`/how-to/pyodide` for a more complete example.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, final

import js  # type: ignore[import-not-found]
from typing_extensions import Buffer

from ...common import (
    BaseSerial,
    BaseSerialTransport,
    ModemPins,
    Parity,
    PinState,
    PortSettingsUpdate,
    SerialException,
    StopBits,
    UnsupportedSetting,
    register_uri_handler,
)
from .types import (
    DataBits,
    FlowControlType,
    JsSerialPort,
    JsStreamReader,
    JsStreamWriter,
    ParityType,
    SerialOutputSignals,
    StopBits as JsStopBits,
)

_LOGGER = logging.getLogger(__name__)

_REGISTERED_JS_PORTS: dict[str, JsSerialPort] = {}
_SERIAL_PORT_CLOSING_TASKS: set[asyncio.Task[None]] = set()


def register_js_port(path: str, js_port: JsSerialPort) -> None:
    """Associate a URL with a JS `SerialPort` for later connection."""
    _REGISTERED_JS_PORTS[path] = js_port


def unregister_js_port(path: str) -> None:
    """Remove the entry for `path` from the JS port registry, if any."""
    _REGISTERED_JS_PORTS.pop(path, None)


_PARITY_MAP: dict[Parity, ParityType] = {
    Parity.NONE: "none",
    Parity.ODD: "odd",
    Parity.EVEN: "even",
}

_STOPBITS_MAP: dict[StopBits, JsStopBits] = {
    StopBits.ONE: 1,
    StopBits.TWO: 2,
}


@final
class ExitSentinel:
    """A sentinel object to signal writer loop exit."""


class PyodideSerial(BaseSerial):
    """Synchronous serial port implementation for Pyodide."""

    def _open(self) -> None:
        raise NotImplementedError()

    def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        raise NotImplementedError()

    def _close(self) -> None:
        raise NotImplementedError()

    def _get_modem_pins(self) -> ModemPins:
        raise NotImplementedError()

    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        raise NotImplementedError()

    def _readinto(self, b: Buffer, *, timeout: float | None) -> int:
        raise NotImplementedError()

    def _write(self, data: Buffer, *, timeout: float | None) -> int:
        raise NotImplementedError()

    def _flush(self) -> None:
        """Flush write buffers."""
        raise NotImplementedError()

    def num_unread_bytes(self) -> int:
        """Return the number of bytes in the read buffer."""
        raise NotImplementedError()

    def num_unwritten_bytes(self) -> int:
        """Return the number of bytes in the write buffer."""
        raise NotImplementedError()

    def _reset_read_buffer(self) -> None:
        """Clear the read buffer."""
        raise NotImplementedError()

    def _reset_write_buffer(self) -> None:
        """Clear the write buffer."""
        raise NotImplementedError()

    @property
    def is_open(self) -> bool:
        """Return whether the port is open."""
        raise NotImplementedError()


class PyodideSerialTransport(BaseSerialTransport):
    """Async serial transport for Pyodide using the Web Serial API."""

    transport_name = "pyodide"
    _serial: PyodideSerial

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        """Initialize the Pyodide serial transport.

        .. warning::
            The Web Serial API does not support software flow control (XON/XOFF).
            Passing ``xonxoff=True`` to :meth:`connect` is accepted for compatibility
            but is silently ignored; a warning is logged. Only hardware flow control
            (RTS/CTS) is honored.

        .. warning::
            The Web Serial API only accepts port settings on open, so
            :meth:`reconfigure_port` closes and reopens the browser port. This drops
            DTR and RTS, discards unread bytes in the browser's receive buffer, and
            logs a warning. See :doc:`/how-to/pyodide`.
        """
        super().__init__(loop, protocol)

        self._write_queue: asyncio.Queue[bytes | type[ExitSentinel]] = asyncio.Queue()
        self._write_buffer_size = 0
        self._closing = False
        self._close_port_task: asyncio.Task[None] | None = None

        self._js_port: JsSerialPort | None = None
        self._js_reader: JsStreamReader | None = None
        self._js_writer: JsStreamWriter | None = None

        self._reader_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None

        # Last-written DTR/RTS output state; Web Serial `getSignals` only reports
        # input lines, so output readback comes from this cache.
        self._dtr_state = PinState.UNDEFINED
        self._rts_state = PinState.UNDEFINED

    async def _connect(  # type: ignore[override]
        self,
        *,
        path: str,
        baudrate: int,
        parity: Parity = Parity.NONE,
        stopbits: StopBits = StopBits.ONE,
        xonxoff: bool = False,
        rtscts: bool = False,
        byte_size: int = 8,
        # A `SerialPort` object must be passed in externally, or pulled from the global
        js_port: JsSerialPort | None = None,
        **kwargs: Any,
    ) -> None:
        self._serial = PyodideSerial(
            path=path,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            xonxoff=xonxoff,
            rtscts=rtscts,
            byte_size=byte_size,
            **kwargs,
        )

        port = js_port if js_port is not None else _REGISTERED_JS_PORTS.get(path)

        if port is None:
            raise SerialException(
                f"No JS serial port registered for {path!r}; call "
                f"`register_js_port(path, js_port)` or pass `js_port=` to `connect`"
            )

        self._js_port = port
        await self._open_js_port()
        self._call_protocol_connection_made()

    async def _open_js_port(self) -> None:
        """Open the JS port with the current settings and start the stream loops."""
        assert self._js_port is not None

        # It would be more correct to raise an exception here but software flow control
        # is used by too many applications
        if self._serial._xonxoff:
            _LOGGER.warning("WebSerial does not support software flow control")

        flow_control: FlowControlType = "hardware" if self._serial._rtscts else "none"

        if self._serial.stopbits not in _STOPBITS_MAP:
            raise UnsupportedSetting(
                f"Unsupported stopbits setting: {self._serial.stopbits!r}"
            )

        if self._serial.parity not in _PARITY_MAP:
            raise UnsupportedSetting(
                f"Unsupported parity setting: {self._serial.parity!r}"
            )

        data_bits: DataBits
        if self._serial.byte_size == 7:
            data_bits = 7
        elif self._serial.byte_size == 8:
            data_bits = 8
        else:
            raise UnsupportedSetting(
                f"Unsupported byte_size: {self._serial.byte_size!r}"
            )

        await self._js_port.open(
            baudRate=self._serial.baudrate,
            dataBits=data_bits,
            flowControl=flow_control,
            parity=_PARITY_MAP[self._serial.parity],
            stopBits=_STOPBITS_MAP[self._serial.stopbits],
        )

        await self.set_modem_pins(
            rts=self._serial.rts_on_open,
            dtr=self._serial.dtr_on_open,
        )

        readable = self._js_port.readable
        assert readable is not None
        self._js_reader = readable.getReader()

        writable = self._js_port.writable
        assert writable is not None
        self._js_writer = writable.getWriter()

        self._reader_task = self._loop.create_task(self._reader_loop())
        self._writer_task = self._loop.create_task(self._writer_loop())

    async def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        """Reopen the JS port, since Web Serial only accepts settings on open."""
        assert self._js_port is not None
        _LOGGER.warning(
            "WebSerial cannot reconfigure an open port, closing and reopening it: %s",
            update,
        )

        await self._drain_writer()

        if self._reader_task is not None:
            self._reader_task.cancel()

        if self._js_reader is not None:
            self._js_reader.releaseLock()
            self._js_reader = None

        if self._reader_task is not None:
            with contextlib.suppress(BaseException):
                await self._reader_task

        await self._js_port.close()
        await self._open_js_port()

    async def _drain_writer(self) -> None:
        """Let queued writes finish, then release the JS writer."""
        if self._writer_task is not None and not self._writer_task.done():
            try:
                async with asyncio.timeout(self._serial.close_timeout):  # type: ignore[attr-defined,unused-ignore]
                    _LOGGER.debug("Waiting for pending writes to finish")
                    self._write_queue.put_nowait(ExitSentinel)
                    await self._writer_task
            except (asyncio.TimeoutError, asyncio.CancelledError):
                _LOGGER.debug("Write task did not drain cleanly; cancelling")
                if not self._writer_task.done():
                    self._writer_task.cancel()
                with contextlib.suppress(BaseException):
                    await self._writer_task

        if self._js_writer is not None:
            self._js_writer.releaseLock()
            self._js_writer = None

    async def _writer_loop(self) -> None:
        while True:
            chunk = await self._write_queue.get()

            if chunk is ExitSentinel or self._js_writer is None:
                _LOGGER.debug("Received exit sentinel, exiting")
                self._write_queue.task_done()
                return

            try:
                await self._js_writer.write(js.Uint8Array.new(chunk))
            except Exception as e:
                _LOGGER.exception("Error writing to serial port")
                self._cleanup(e)
                break
            finally:
                self._write_buffer_size -= len(chunk)
                self._write_queue.task_done()

    async def _reader_loop(self) -> None:
        while self._js_reader is not None:
            result = await self._js_reader.read()
            if result.done:
                self._cleanup(RuntimeError("Other side has closed"))
                return

            assert self._protocol is not None
            self._protocol.data_received(bytes(result.value))

    async def _get_modem_pins(self) -> ModemPins:
        """Get modem control bits, internal."""
        assert self._js_port is not None
        result = await self._js_port.getSignals()

        # `getSignals` only reports input lines; DTR/RTS come from the cache
        return ModemPins(
            dtr=self._dtr_state,
            rts=self._rts_state,
            cts=PinState.convert(result.clearToSend),
            car=PinState.convert(result.dataCarrierDetect),
            rng=PinState.convert(result.ringIndicator),
            dsr=PinState.convert(result.dataSetReady),
        )

    async def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set modem control bits, internal."""
        signals = SerialOutputSignals()
        if modem_pins.rts is not PinState.UNDEFINED:
            signals["requestToSend"] = modem_pins.rts is PinState.HIGH
        if modem_pins.dtr is not PinState.UNDEFINED:
            signals["dataTerminalReady"] = modem_pins.dtr is PinState.HIGH

        if signals:
            assert self._js_port is not None
            await self._js_port.setSignals(**signals)

        if modem_pins.dtr is not PinState.UNDEFINED:
            self._dtr_state = modem_pins.dtr
        if modem_pins.rts is not PinState.UNDEFINED:
            self._rts_state = modem_pins.rts

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Write data to the transport."""
        if self._closing:
            return
        self._write_buffer_size += len(data)
        self._write_queue.put_nowait(bytes(data))

    def get_write_buffer_size(self) -> int:
        """Return the number of bytes currently queued for writing."""
        return self._write_buffer_size

    async def _flush(self) -> None:
        """Flush write buffers, waiting until all data is written, internal."""
        await self._write_queue.join()

    def abort(self) -> None:
        """Close the transport immediately, discarding pending writes."""
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()

        while not self._write_queue.empty():
            self._write_queue.get_nowait()
            self._write_queue.task_done()
        self._write_buffer_size = 0

        self._cleanup(None)

    def __del__(self) -> None:
        """Clean up the transport if it was not properly closed."""
        self._cleanup(RuntimeError("Transport was not closed!"))

    async def _close_port(self, exception: Exception | None) -> None:
        # Drain pending writes, unless abort() already cancelled the writer.
        await self._drain_writer()

        if self._js_port is not None:
            with contextlib.suppress(Exception):
                await self.set_modem_pins(
                    rts=self._serial.rts_on_close,
                    dtr=self._serial.dtr_on_close,
                )
            await self._js_port.close()
            self._js_port = None

        assert self._close_port_task is not None

        # If the task cannot be removed, we should still call `connection_lost`
        with contextlib.suppress(ValueError):
            _SERIAL_PORT_CLOSING_TASKS.add(self._close_port_task)

        # Only now do we call `connection_lost`
        self._call_protocol_connection_lost(exception)

    def _cleanup(self, exception: Exception | None) -> None:
        self._closing = True

        # The reader task should be cancelled. We do not cancel the writer task, we wait
        # for it to cleanly exit.
        if self._reader_task is not None:
            self._reader_task.cancel()

        if self._js_reader is not None:
            self._js_reader.releaseLock()
            self._js_reader = None

        if self._js_port is not None and self._close_port_task is None:
            self._close_port_task = asyncio.create_task(self._close_port(exception))
            _SERIAL_PORT_CLOSING_TASKS.discard(self._close_port_task)
        elif not self._closed_waiter.done():
            self._call_protocol_connection_lost(exception)

    def close(self) -> None:
        """Close the transport."""
        self._arm_close_timeout()
        self._cleanup(None)


register_uri_handler(
    scheme="device://",
    unique_scheme="pyodide://",
    sync_cls=PyodideSerial,
    async_transport_cls=PyodideSerialTransport,
    weight=-1,
)
