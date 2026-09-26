"""Asynchronous serial port support."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import logging
from typing import Any, Generic, TypeVar, cast

from typing_extensions import Self, Unpack

from .common import (
    AllConnectKwargs,
    BaseSerialTransport,
    ModemPins,
    Parity,
    PinState,
    SerialException,
    StopBits,
    get_uri_handler,
    route_backend_kwargs,
)

LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T", bound=asyncio.WriteTransport)


class SerialStreamWriter(asyncio.StreamWriter, Generic[_T]):
    """StreamWriter with properly typed transport."""

    @property
    def transport(self) -> _T:
        """Return the underlying transport."""
        return cast(_T, super().transport)


class AsyncSerial:
    """Async serial port with a sync-style API."""

    def __init__(
        self,
        url: str | None,
        *,
        transport_cls: type[BaseSerialTransport] | None = None,
        **kwargs: Unpack[AllConnectKwargs],
    ) -> None:
        """Initialize an unopened serial port.

        Directly creating this class is not recommended; use `async_serial_for_url`
        instead.
        """

        self._url = url
        self._connect_kwargs: dict[str, Any] = dict(kwargs)
        self._transport_cls = transport_cls

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transport: BaseSerialTransport | None = None

    # ---- Lifecycle ----

    async def open(self) -> None:
        """Open the serial port connection."""
        if self._transport is not None:
            raise SerialException("AsyncSerial is already open")

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        transport, _ = await create_serial_connection(
            loop,
            lambda: protocol,
            self._url,
            transport_cls=self._transport_cls,
            **self._connect_kwargs,
        )
        self._reader = reader
        self._writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        self._transport = transport

    async def close(self) -> None:
        """Close the connection and wait until the port is fully closed."""
        if self._transport is None:
            return
        self._transport.close()
        await self._transport.wait_closed()
        self._reset()

    def schedule_close(self) -> None:
        """Signal a graceful close without waiting for it to finish."""
        if self._transport is None:
            return
        self._transport.close()

    def abort(self) -> None:
        """Drop pending writes and close immediately, without waiting."""
        if self._transport is None:
            return
        self._transport.abort()

    async def wait_closed(self) -> None:
        """Wait until a previously-scheduled close or abort has finished."""
        if self._transport is None:
            return
        await self._transport.wait_closed()
        self._reset()

    def _reset(self) -> None:
        self._reader = None
        self._writer = None
        self._transport = None

    @property
    def is_open(self) -> bool:
        """Whether the connection is currently open."""
        return self._transport is not None and not self._transport.is_closing()

    async def __aenter__(self) -> Self:
        """Open the connection and return self."""
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the connection and wait until it's fully closed."""
        await self.close()

    def __repr__(self) -> str:
        """Return a debug representation."""
        return f"<AsyncSerial url={self._url!r} transport={self._transport!r}>"

    # ---- Reads ----

    async def read(self, n: int = -1) -> bytes:
        """Read up to `n` bytes (or until EOF if `n` is -1)."""
        return await self._require_reader().read(n)

    async def readexactly(self, n: int) -> bytes:
        """Read exactly `n` bytes."""
        return await self._require_reader().readexactly(n)

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        """Read up to and including `separator`."""
        return await self._require_reader().readuntil(separator)

    async def readline(self) -> bytes:
        """Read until the next newline."""
        return await self._require_reader().readline()

    def _require_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            raise SerialException("AsyncSerial is not open")
        return self._reader

    # ---- Writes ----

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        """Queue data for writing and wait for the application buffer to drain."""
        writer = self._require_writer()
        writer.write(data)
        await writer.drain()

    async def writelines(self, data: Iterable[bytes | bytearray | memoryview]) -> None:
        """Queue buffers for writing and wait for the application buffer to drain."""
        writer = self._require_writer()
        writer.writelines(data)
        await writer.drain()

    def write_nowait(self, data: bytes | bytearray | memoryview) -> None:
        """Queue data for writing without waiting; pair with `drain()` to batch."""
        self._require_writer().write(data)

    def writelines_nowait(self, data: Iterable[bytes | bytearray | memoryview]) -> None:
        """Queue buffers for writing without waiting; pair with `drain()` to batch."""
        self._require_writer().writelines(data)

    async def drain(self) -> None:
        """Wait until the application-level write buffer can accept more data."""
        await self._require_writer().drain()

    async def flush(self) -> None:
        """Drain app-level buffer, then wait for the OS-level buffer to flush."""
        await self.drain()
        await self.transport.flush()

    def _require_writer(self) -> asyncio.StreamWriter:
        if self._writer is None:
            raise SerialException("AsyncSerial is not open")
        return self._writer

    # ---- Transport access ----

    @property
    def transport(self) -> BaseSerialTransport:
        """Return the underlying serial transport."""
        if self._transport is None:
            raise SerialException("AsyncSerial is not open")
        return self._transport

    # ---- Modem pins (proxy to transport) ----

    async def get_modem_pins(self) -> ModemPins:
        """Get modem control pins."""
        return await self.transport.get_modem_pins()

    async def set_modem_pins(
        self,
        modem_pins: ModemPins | None = None,
        *,
        le: PinState | bool | None = PinState.UNDEFINED,
        dtr: PinState | bool | None = PinState.UNDEFINED,
        rts: PinState | bool | None = PinState.UNDEFINED,
        st: PinState | bool | None = PinState.UNDEFINED,
        sr: PinState | bool | None = PinState.UNDEFINED,
        cts: PinState | bool | None = PinState.UNDEFINED,
        car: PinState | bool | None = PinState.UNDEFINED,
        rng: PinState | bool | None = PinState.UNDEFINED,
        dsr: PinState | bool | None = PinState.UNDEFINED,
    ) -> None:
        """Set modem control pins."""
        await self.transport.set_modem_pins(
            modem_pins,
            le=le,
            dtr=dtr,
            rts=rts,
            st=st,
            sr=sr,
            cts=cts,
            car=car,
            rng=rng,
            dsr=dsr,
        )

    async def reconfigure_port(
        self,
        *,
        baudrate: int | None = None,
        parity: Parity | str | None = None,
        stopbits: StopBits | int | float | None = None,
        byte_size: int | None = None,
        xonxoff: bool | None = None,
        rtscts: bool | None = None,
        dsrdtr: bool | None = None,
    ) -> None:
        """Change serial port settings. Only the settings passed are changed."""
        await self.transport.reconfigure_port(
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            byte_size=byte_size,
            xonxoff=xonxoff,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
        )

    # ---- Settings (proxy to transport) ----

    @property
    def baudrate(self) -> int:
        """Get the baud rate."""
        return self.transport.baudrate

    @property
    def parity(self) -> Parity:
        """Get the parity."""
        return self.transport.parity

    @property
    def stopbits(self) -> StopBits:
        """Get the number of stop bits."""
        return self.transport.stopbits

    @property
    def byte_size(self) -> int:
        """Get the byte size."""
        return self.transport.byte_size

    @property
    def exclusive(self) -> bool:
        """Get the exclusive setting."""
        return self.transport.exclusive


async def create_serial_connection(  # noqa: PLR0917
    loop: asyncio.AbstractEventLoop,
    protocol_factory: Callable[[], asyncio.Protocol],
    url: str | None,
    baudrate: int,
    parity: Parity = Parity.NONE,
    stopbits: StopBits = StopBits.ONE,
    xonxoff: bool = False,
    rtscts: bool = False,
    exclusive: bool = True,
    *,
    transport_cls: type[BaseSerialTransport] | None = None,
    **kwargs: Any,
) -> tuple[BaseSerialTransport, asyncio.Protocol]:
    """Create a serial port connection with asyncio."""
    if transport_cls is not None:
        resolved_cls = transport_cls
    elif url is None:
        raise ValueError("One of `url` or `transport_cls` must be provided.")
    else:
        handler = await asyncio.get_running_loop().run_in_executor(
            None, get_uri_handler, url
        )
        resolved_cls = handler.async_transport_cls
        kwargs = route_backend_kwargs(handler, kwargs)  # pylint: disable=serialx-reassigned-parameter

    protocol = protocol_factory()
    transport = resolved_cls(loop=loop, protocol=protocol)

    await transport.connect(
        path=url,
        baudrate=baudrate,
        parity=parity,
        stopbits=stopbits,
        xonxoff=xonxoff,
        rtscts=rtscts,
        exclusive=exclusive,
        **kwargs,
    )

    return transport, protocol


async def open_serial_connection(
    *args: Any, **kwargs: Any
) -> tuple[asyncio.StreamReader, SerialStreamWriter[BaseSerialTransport]]:
    """Open a serial port connection using StreamReader and StreamWriter."""
    loop = asyncio.get_running_loop()

    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await create_serial_connection(
        loop, lambda: protocol, *args, **kwargs
    )
    writer: SerialStreamWriter[BaseSerialTransport] = SerialStreamWriter(
        transport, protocol, reader, loop
    )

    return reader, writer


def async_serial_for_url(
    url: str | None,
    *,
    transport_cls: type[BaseSerialTransport] | None = None,
    **kwargs: Unpack[AllConnectKwargs],
) -> AsyncSerial:
    """Build an unopened AsyncSerial. Use `async with` or `await serial.open()`."""
    return AsyncSerial(url, transport_cls=transport_cls, **kwargs)
