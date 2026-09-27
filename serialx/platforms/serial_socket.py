"""Socket serial transport."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
import errno
import logging
import socket
import sys
from typing import Any
import urllib.parse

if sys.version_info >= (3, 11):
    from asyncio import timeout as asyncio_timeout
else:
    from async_timeout import timeout as asyncio_timeout

from typing_extensions import Buffer

from serialx.common import (
    BaseSerial,
    BaseSerialTransport,
    ModemPins,
    Parity,
    PortSettingsUpdate,
    StopBits,
    register_uri_handler,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_CONNECT_TIMEOUT = 10.0


class SocketSerial(BaseSerial):
    """Synchronous serial interface over a TCP socket."""

    def __init__(
        self,
        *args: Any,
        # Socket-specific kwargs
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        """Initialize socket serial port."""
        super().__init__(*args, **kwargs)

        parsed = urllib.parse.urlparse(str(self._path))
        if parsed.hostname is None or parsed.port is None:
            raise ValueError(
                f"Invalid socket URI, expected both host and port: {self._path}"
            )

        self._host = parsed.hostname
        self._port = parsed.port
        self._connect_timeout = connect_timeout

        self._socket: socket.socket | None = None

    def _open(self) -> None:
        """Open the socket connection."""
        assert self._host is not None
        assert self._port is not None

        self._socket = socket.create_connection(
            (self._host, self._port), timeout=self._connect_timeout
        )

    @property
    def is_open(self) -> bool:
        """Check if the serial port is open."""
        return self._socket is not None

    @property
    def connect_timeout(self) -> float | None:
        """Get the connection timeout in seconds."""
        return self._connect_timeout

    @contextmanager
    def _socket_timeout(
        self, timeout: float | None
    ) -> Generator[float | None, None, None]:
        """Context manager to set socket timeout temporarily."""
        assert self._socket is not None

        original_timeout = self._socket.gettimeout()
        self._socket.settimeout(timeout)

        try:
            yield original_timeout
        finally:
            self._socket.settimeout(original_timeout)

    def _get_effective_socket_timeout(self) -> float | None:
        """Calculate effective socket timeout as min of read and write timeouts."""
        if self._read_timeout is None:
            return self._write_timeout

        if self._write_timeout is None:
            return self._read_timeout

        effective = min(self._read_timeout, self._write_timeout)

        if self._read_timeout != self._write_timeout:
            LOGGER.debug(
                "Serial over TCP accepts only a single timeout, taking the min of read=%s and write=%s",
                self._read_timeout,
                self._write_timeout,
            )

        return effective

    def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        """Configure the serial port settings."""
        if self._socket is not None:
            self._socket.settimeout(self._get_effective_socket_timeout())

    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        pass

    def _get_modem_pins(self) -> ModemPins:
        return ModemPins()

    def num_unread_bytes(self) -> int:
        """Return the number of bytes waiting to be read."""
        return 0

    def num_unwritten_bytes(self) -> int:
        """Return the number of bytes waiting to be written."""
        return 0

    def _reset_read_buffer(self) -> None:
        """Reset the read buffer."""

        # Drain all of the pending data in nonblocking mode
        with self._socket_timeout(0):
            while True:
                if self._socket is None:
                    break

                try:
                    data = self._socket.recv(1024)
                except BlockingIOError:
                    break

                if not data:
                    break

    def _reset_write_buffer(self) -> None:
        """Reset the write buffer."""

    def _flush(self) -> None:
        """Flush write buffers (no-op for sockets)."""

    def _write(self, b: Buffer, *, timeout: float | None) -> int:
        """Write bytes to socket."""
        assert self._socket is not None

        data = bytes(b)

        with self._socket_timeout(timeout):
            self._socket.sendall(data)

        return len(data)

    def _readinto(self, b: Buffer, *, timeout: float | None) -> int:
        """Read bytes from socket into buffer."""
        assert self._socket is not None

        m = memoryview(b).cast("B")
        try:
            with self._socket_timeout(timeout):
                n = self._socket.recv_into(m)
        except (TimeoutError, BlockingIOError):
            return 0

        if n == 0:
            self._mark_broken(OSError(errno.EIO, "socket closed by peer"))
            self._check_broken()

        return n

    def _close(self) -> None:
        """Close the socket."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class _SocketProxyProtocol(asyncio.Protocol):
    """Bridge protocol between asyncio TCP transport and SocketSerialTransport."""

    def __init__(self, serial_transport: SocketSerialTransport) -> None:
        self._serial_transport = serial_transport

    def data_received(self, data: bytes) -> None:
        self._serial_transport._data_received(data)

    def pause_writing(self) -> None:
        self._serial_transport._pause_writing()

    def resume_writing(self) -> None:
        self._serial_transport._resume_writing()

    def connection_lost(self, exc: Exception | None) -> None:
        self._serial_transport._tcp_connection_lost()
        self._serial_transport._connection_lost(exc)


class SocketSerialTransport(BaseSerialTransport):
    """Serial transport over a TCP socket."""

    transport_name = "socket"

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        """Initialize the socket serial transport."""
        super().__init__(loop, protocol)
        self._tcp_transport: asyncio.Transport | None = None
        self._tcp_connection_lost_waiter: asyncio.Future[None] | None = None

    async def _connect(  # type: ignore[override]
        self,
        *,
        path: str,
        baudrate: int,
        parity: Parity = Parity.NONE,
        stopbits: StopBits | int | float = StopBits.ONE,
        xonxoff: bool = False,
        rtscts: bool = False,
        byte_size: int = 8,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        self._serial = SocketSerial(
            path=path,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            xonxoff=xonxoff,
            rtscts=rtscts,
            byte_size=byte_size,
            connect_timeout=connect_timeout,
        )
        self._extra["serial"] = self._serial

        self._tcp_connection_lost_waiter = self._loop.create_future()

        async with asyncio_timeout(self._serial._connect_timeout):
            tcp_transport, _ = await self._loop.create_connection(
                lambda: _SocketProxyProtocol(self),
                host=self._serial._host,
                port=self._serial._port,
            )

        self._tcp_transport = tcp_transport

        if self._connection_lost_called:
            tcp_transport.close()
            if self._tcp_connection_lost_waiter is not None:
                await self._tcp_connection_lost_waiter

            self._tcp_transport = None
            return

        self._call_protocol_connection_made()

    def _data_received(self, data: bytes) -> None:
        """Handle data received from the TCP transport."""
        self._protocol.data_received(data)

    def _pause_writing(self) -> None:
        """Propagate backpressure from TCP transport to serial protocol."""
        self._protocol.pause_writing()

    def _resume_writing(self) -> None:
        """Propagate resume signal from TCP transport to serial protocol."""
        self._protocol.resume_writing()

    def _connection_lost(self, exc: Exception | None) -> None:
        """Handle connection lost from the TCP transport."""
        if self._connection_lost_called:
            return
        self._closing = True
        self._tcp_transport = None
        if not self._user_initiated_close:
            self._mark_broken(
                exc if exc is not None else OSError(errno.EIO, "socket closed by peer")
            )
        self._call_protocol_connection_lost(exc)

    def _tcp_connection_lost(self) -> None:
        """Track the underlying TCP transport's connection_lost callback."""
        if (
            self._tcp_connection_lost_waiter is not None
            and not self._tcp_connection_lost_waiter.done()
        ):
            self._tcp_connection_lost_waiter.set_result(None)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Write data to the socket."""
        self._check_broken()
        assert self._tcp_transport is not None
        self._tcp_transport.write(data)

    def pause_reading(self) -> None:
        """Pause reading from the socket transport."""
        assert self._tcp_transport is not None
        self._tcp_transport.pause_reading()

    def resume_reading(self) -> None:
        """Resume reading from the socket transport."""
        assert self._tcp_transport is not None
        self._tcp_transport.resume_reading()

    def is_closing(self) -> bool:
        """Return whether the transport is closing."""
        return self._closing

    def abort(self) -> None:
        """Abort the transport immediately."""
        if self._connection_lost_called:
            return
        self._closing = True
        self._mark_user_closed()

        if self._tcp_transport is not None:
            self._tcp_transport.abort()
        else:
            self._connection_lost(None)

    def close(self) -> None:
        """Close the transport."""
        if self._connection_lost_called:
            return
        self._closing = True
        self._mark_user_closed()
        self._arm_close_timeout()

        if self._tcp_transport is not None:
            self._tcp_transport.close()
        else:
            self._connection_lost(None)

    async def _flush(self) -> None:
        """Flush write buffers, waiting until all data is written, internal."""

    async def _get_modem_pins(self) -> ModemPins:
        """Get modem control bits, internal."""
        return ModemPins()

    async def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set modem control bits, internal."""

    def get_write_buffer_size(self) -> int:
        """Get the number of bytes currently in the write buffer."""
        if self._tcp_transport is not None:
            return self._tcp_transport.get_write_buffer_size()
        return 0

    def get_write_buffer_limits(self) -> tuple[int, int]:
        """Get the write buffer low and high water marks."""
        if self._tcp_transport is None:
            return (0, 0)

        return self._tcp_transport.get_write_buffer_limits()

    def set_write_buffer_limits(
        self, high: int | None = None, low: int | None = None
    ) -> None:
        """Set the write buffer low and high water marks."""
        if self._tcp_transport is None:
            raise RuntimeError("Transport not connected")

        self._tcp_transport.set_write_buffer_limits(high=high, low=low)

    def can_write_eof(self) -> bool:
        """Return whether the underlying TCP transport supports EOF."""
        return (
            self._tcp_transport.can_write_eof()
            if self._tcp_transport is not None
            else False
        )

    async def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        pass


register_uri_handler(
    scheme="socket://",
    unique_scheme="socket://",
    sync_cls=SocketSerial,
    async_transport_cls=SocketSerialTransport,
)
register_uri_handler(
    scheme="tcp://",
    unique_scheme="tcp://",
    sync_cls=SocketSerial,
    async_transport_cls=SocketSerialTransport,
)
