"""RFC 2217 (Telnet Com Port Control) implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import suppress
import dataclasses
from enum import IntEnum
import errno
import logging
import sys

if sys.version_info >= (3, 11):
    from asyncio import timeout as asyncio_timeout
else:
    from async_timeout import timeout as asyncio_timeout

from typing import Any, overload

from typing_extensions import Buffer, Unpack

from ...common import (
    BaseSerialTransport,
    ConnectKwargs,
    ModemPins,
    Parity,
    PinState,
    PortSettingsUpdate,
    SerialException,
    StopBits,
    UnsupportedSetting,
    measure_time,
    register_uri_handler,
)
from ..serial_socket import SocketSerial
from .types import (
    CMD_ID_TO_CLASS,
    ControlCmdId,
    DoCmd,
    DontCmd,
    LineStateFlag,
    ModemStateFlag,
    NotifyLinestateCmd,
    NotifyModemstateCmd,
    PurgeDataCmd,
    PurgeDataValue,
    Rfc2217CmdId,
    Rfc2217Command,
    Rfc2217Parity,
    Rfc2217StopSize,
    SetBaudrateCmd,
    SetControlCmd,
    SetDatasizeCmd,
    SetLinestateMaskCmd,
    SetModemstateMaskCmd,
    SetParityCmd,
    SetStopsizeCmd,
    TelnetCmdId,
    TelnetCommand,
    TelnetOption,
    WillCmd,
    WontCmd,
    encode_command,
    iac_escape,
)

LOGGER = logging.getLogger(__name__)

RFC2217_PARITY_MAP = {
    Parity.NONE: Rfc2217Parity.NONE,
    Parity.ODD: Rfc2217Parity.ODD,
    Parity.EVEN: Rfc2217Parity.EVEN,
    Parity.MARK: Rfc2217Parity.MARK,
    Parity.SPACE: Rfc2217Parity.SPACE,
}

RFC2217_STOPBITS_MAP = {
    StopBits.ONE: Rfc2217StopSize.ONE,
    StopBits.TWO: Rfc2217StopSize.TWO,
    StopBits.ONE_POINT_FIVE: Rfc2217StopSize.ONE_POINT_FIVE,
}


OPTION_CMD_TO_TYPE: dict[int, type[WillCmd | WontCmd | DoCmd | DontCmd]] = {
    TelnetCmdId.WILL: WillCmd,
    TelnetCmdId.WONT: WontCmd,  # codespell:ignore wont
    TelnetCmdId.DO: DoCmd,
    TelnetCmdId.DONT: DontCmd,
}


class TelnetParserState(IntEnum):
    """States for the telnet protocol parser state machine."""

    NORMAL = 0
    IAC_SEEN = 1
    OPTION_CMD = (
        2  # waiting for option byte after WILL/WONT/DO/DONT  # codespell:ignore wont
    )
    SUBNEG_OPTION = 3  # waiting for option byte after SB
    SUBNEG_DATA = 4  # accumulating subneg payload
    SUBNEG_IAC = 5  # IAC seen inside subnegotiation


class TelnetParser:
    """State machine that separates a telnet byte stream into data and commands."""

    def __init__(self) -> None:
        """Initialize the telnet parser."""
        self._state = TelnetParserState.NORMAL
        self._pending_cmd: int = 0
        self._subneg_option: int = 0
        self._subneg_buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes | TelnetCommand | Rfc2217Command]:
        """Process raw bytes and return data chunks and commands."""
        LOGGER.debug("Parser feed: %d bytes: %s", len(data), data.hex(" "))
        result: list[bytes | TelnetCommand | Rfc2217Command] = []
        data_buf = bytearray()

        for byte in data:
            match self._state:
                case TelnetParserState.NORMAL:
                    if byte == TelnetCmdId.IAC:
                        self._state = TelnetParserState.IAC_SEEN
                    else:
                        data_buf.append(byte)

                case TelnetParserState.IAC_SEEN:
                    if byte == TelnetCmdId.IAC:
                        # Escaped IAC -> literal 0xFF data byte
                        data_buf.append(0xFF)
                        self._state = TelnetParserState.NORMAL
                    elif byte == TelnetCmdId.SB:
                        # Flush data before command
                        if data_buf:
                            result.append(bytes(data_buf))
                            data_buf.clear()
                        self._state = TelnetParserState.SUBNEG_OPTION
                    elif byte in OPTION_CMD_TO_TYPE:
                        if data_buf:
                            result.append(bytes(data_buf))
                            data_buf.clear()
                        self._pending_cmd = byte
                        self._state = TelnetParserState.OPTION_CMD
                    else:
                        # Ignore NOP, BRK, DM, etc.
                        self._state = TelnetParserState.NORMAL

                case TelnetParserState.OPTION_CMD:
                    cmd_cls = OPTION_CMD_TO_TYPE[self._pending_cmd]
                    option_cmd = cmd_cls(option=TelnetOption(byte))
                    LOGGER.debug("Parsed telnet command: %r", option_cmd)
                    result.append(option_cmd)
                    self._state = TelnetParserState.NORMAL

                case TelnetParserState.SUBNEG_OPTION:
                    self._subneg_option = byte
                    self._subneg_buffer.clear()
                    self._state = TelnetParserState.SUBNEG_DATA

                case TelnetParserState.SUBNEG_DATA:
                    if byte == TelnetCmdId.IAC:
                        self._state = TelnetParserState.SUBNEG_IAC
                    else:
                        self._subneg_buffer.append(byte)

                case TelnetParserState.SUBNEG_IAC:
                    if byte == TelnetCmdId.IAC:
                        # Escaped IAC inside subneg
                        self._subneg_buffer.append(0xFF)
                        self._state = TelnetParserState.SUBNEG_DATA
                    elif byte == TelnetCmdId.SE:
                        cmd = self._finish_subneg()
                        if cmd is not None:
                            result.append(cmd)
                        self._state = TelnetParserState.NORMAL
                    else:
                        # Malformed, discard subneg and treat as normal IAC cmd
                        LOGGER.debug(
                            "Malformed subneg: IAC %d inside subneg for option %d",
                            byte,
                            self._subneg_option,
                        )
                        self._state = TelnetParserState.NORMAL

        if data_buf:
            result.append(bytes(data_buf))

        return result

    def _finish_subneg(self) -> Rfc2217Command | None:
        """Parse a completed subnegotiation."""
        option = self._subneg_option
        payload = bytes(self._subneg_buffer)

        LOGGER.debug("Subneg complete: option=%d payload=%r", option, payload.hex(" "))

        if option != TelnetOption.COM_PORT_OPTION:
            LOGGER.debug("Ignoring subneg for unknown option %d", option)
            return None

        if not payload:
            LOGGER.debug("Empty COM-PORT-OPTION subneg")
            return None

        cmd_code = payload[0]
        cmd_payload = payload[1:]

        # Server responses use codes offset by +100
        if cmd_code >= 100:
            cmd_code -= 100

        try:
            cmd_id = Rfc2217CmdId(cmd_code)
        except ValueError:
            LOGGER.debug("Unknown RFC 2217 command code: %d", cmd_code)
            return None

        cmd_cls = CMD_ID_TO_CLASS.get(cmd_id)
        if cmd_cls is None:
            LOGGER.debug("Unknown RFC 2217 command code: %d", cmd_id)
            return None

        cmd = cmd_cls.from_bytes(cmd_payload)
        LOGGER.debug("Parsed RFC2217 %s", cmd)
        return cmd


class Rfc2217:
    """IO-free RFC2217 protocol engine shared by sync and async wrappers."""

    _ACCEPTED_OPTIONS = frozenset(
        {
            TelnetOption.BINARY,
            TelnetOption.SUPPRESS_GO_AHEAD,
        }
    )

    def __init__(self) -> None:
        """Initialize parser state and protocol bookkeeping."""
        self._parser = TelnetParser()
        self._pending_telnet: list[TelnetCommand] = []
        self._pending_rfc2217: dict[Rfc2217CmdId, Rfc2217Command] = {}
        self._modemstate = ModemStateFlag(0)
        self._negotiated = False

    @property
    def negotiated(self) -> bool:
        """Return whether COM-PORT-OPTION negotiation has completed."""
        return self._negotiated

    def mark_negotiated(self) -> None:
        """Mark COM-PORT-OPTION negotiation as complete."""
        self._negotiated = True

    def reset(self) -> None:
        """Reset parser state and all protocol bookkeeping."""
        self._parser = TelnetParser()
        self._pending_telnet.clear()
        self._pending_rfc2217.clear()
        self._modemstate = ModemStateFlag(0)
        self._negotiated = False

    def feed(self, data: bytes) -> tuple[bytes, list[TelnetCommand]]:
        """Parse raw bytes, update protocol state, and return data/responses."""
        return self._dispatch_parser_items(self._parser.feed(data))

    def pop_matching_telnet(
        self, expected: list[TelnetCommand]
    ) -> TelnetCommand | None:
        """Pop a pending telnet command matching one of the expected responses."""
        for i, pending in enumerate(self._pending_telnet):
            if pending in expected:
                del self._pending_telnet[i]
                return pending

        return None

    def pop_pending_rfc2217(self, cmd_id: Rfc2217CmdId) -> Rfc2217Command | None:
        """Pop a pending RFC2217 command of the given type, if present."""
        return self._pending_rfc2217.pop(cmd_id, None)

    def build_port_config_commands(
        self, update: PortSettingsUpdate
    ) -> Generator[Rfc2217Command]:
        """Build the RFC2217 commands for the settings present in the update."""
        if update.rtscts and update.xonxoff:
            raise UnsupportedSetting(
                "Cannot enable both RTS/CTS and XON/XOFF flow control"
            )

        if update.baudrate is not None:
            yield SetBaudrateCmd(baudrate=update.baudrate)

        if update.byte_size is not None:
            yield SetDatasizeCmd(size=update.byte_size)

        if update.parity is not None:
            yield SetParityCmd(parity=RFC2217_PARITY_MAP[update.parity])

        if update.stopbits is not None:
            yield SetStopsizeCmd(size=RFC2217_STOPBITS_MAP[update.stopbits])

        if update.rtscts:
            yield SetControlCmd(control=ControlCmdId.USE_HARDWARE)
        elif update.xonxoff:
            yield SetControlCmd(control=ControlCmdId.USE_XON_XOFF)
        elif update.rtscts is not None or update.xonxoff is not None:
            yield SetControlCmd(control=ControlCmdId.USE_NO_FLOW_CONTROL)

    def build_state_mask_commands(self) -> tuple[Rfc2217Command, ...]:
        """Build the commands that subscribe to modem and line state changes."""
        return (
            SetModemstateMaskCmd(mask=ModemStateFlag(255)),
            SetLinestateMaskCmd(mask=LineStateFlag(0)),
        )

    def build_modem_pin_commands(
        self, modem_pins: ModemPins
    ) -> Generator[SetControlCmd]:
        """Build SET_CONTROL commands needed to apply modem pin changes."""
        if modem_pins.dtr is PinState.HIGH:
            yield SetControlCmd(control=ControlCmdId.SET_DTR_ON)
        elif modem_pins.dtr is PinState.LOW:
            yield SetControlCmd(control=ControlCmdId.SET_DTR_OFF)

        if modem_pins.rts is PinState.HIGH:
            yield SetControlCmd(control=ControlCmdId.SET_RTS_ON)
        elif modem_pins.rts is PinState.LOW:
            yield SetControlCmd(control=ControlCmdId.SET_RTS_OFF)

    def get_modem_pins(self) -> ModemPins:
        """Return modem pin state from the last NOTIFY-MODEMSTATE."""
        state = self._modemstate

        return ModemPins(
            cts=(PinState.HIGH if state & ModemStateFlag.CTS else PinState.LOW),
            dsr=(PinState.HIGH if state & ModemStateFlag.DSR else PinState.LOW),
            rng=(PinState.HIGH if state & ModemStateFlag.RI else PinState.LOW),
            car=(PinState.HIGH if state & ModemStateFlag.RLSD else PinState.LOW),
        )

    def _build_do_response(self, cmd: DoCmd) -> WillCmd | WontCmd | None:
        """Build a response to a telnet DO option negotiation command."""
        self._pending_telnet.append(cmd)

        if cmd.option in self._ACCEPTED_OPTIONS:
            return WillCmd(option=cmd.option)
        elif cmd.option == TelnetOption.COM_PORT_OPTION:
            return None
        else:
            return WontCmd(option=cmd.option)

    def _build_will_response(self, cmd: WillCmd) -> DoCmd | DontCmd | None:
        """Build a response to a telnet WILL option negotiation command."""
        self._pending_telnet.append(cmd)

        if cmd.option in self._ACCEPTED_OPTIONS:
            return DoCmd(option=cmd.option)
        elif cmd.option == TelnetOption.COM_PORT_OPTION:
            return None
        else:
            return DontCmd(option=cmd.option)

    def _dispatch_parser_items(
        self, items: list[bytes | TelnetCommand | Rfc2217Command]
    ) -> tuple[bytes, list[TelnetCommand]]:
        """Update protocol state from parsed items and produce responses."""
        responses: list[TelnetCommand] = []
        serial_data = bytearray()

        for item in items:
            if isinstance(item, bytes):
                LOGGER.debug("RX data: %d bytes  [%s]", len(item), item.hex(" "))
                serial_data.extend(item)
            elif isinstance(item, NotifyModemstateCmd):
                LOGGER.debug("RX modemstate notification: %r", item)
                self._modemstate = item.modemstate
            elif isinstance(item, NotifyLinestateCmd):
                LOGGER.debug("RX linestate notification: %r", item)
            elif isinstance(item, WillCmd):
                will_rsp = self._build_will_response(item)
                LOGGER.debug("RX %r -> %r", item, will_rsp)
                if will_rsp is not None:
                    responses.append(will_rsp)
            elif isinstance(item, DoCmd):
                do_rsp = self._build_do_response(item)
                LOGGER.debug("RX %r -> %r", item, do_rsp)
                if do_rsp is not None:
                    responses.append(do_rsp)
            elif isinstance(item, Rfc2217Command):
                LOGGER.debug("RX rfc2217 cmd: %r", item)
                self._pending_rfc2217[item.CMD_ID] = item
            else:
                LOGGER.debug("RX telnet cmd: %r", item)
                self._pending_telnet.append(item)

        return bytes(serial_data), responses


class RFC2217Serial(SocketSerial):
    """RFC 2217 serial port implementation (Synchronous)."""

    def __init__(
        self,
        *args: Any,
        receive_buffer_size: int = 4096,
        connect_timeout: float = 5.0,
        **kwargs: Any,
    ) -> None:
        """Initialize the RFC 2217 serial port."""
        super().__init__(*args, **kwargs)
        self._receive_buffer_size = receive_buffer_size
        self._connect_timeout = connect_timeout

        self._engine = Rfc2217()
        self._data_buffer = bytearray()

    def _open(self) -> None:
        """Open the TCP connection and negotiate COM-PORT-OPTION."""
        super()._open()
        assert self._socket is not None
        LOGGER.debug("TCP connected to %s:%s", self._host, self._port)
        self._negotiate()

    def _negotiate(self) -> None:
        """Perform the initial WILL/DO handshake for COM-PORT-OPTION."""
        assert self._socket is not None
        LOGGER.debug("Starting RFC 2217 negotiation")

        rsp = self._send_command(
            WillCmd(option=TelnetOption.BINARY),
            responses=[
                DoCmd(option=TelnetOption.BINARY),
                DontCmd(option=TelnetOption.BINARY),
            ],
            timeout=self._connect_timeout,
        )
        if isinstance(rsp, DontCmd):
            raise SerialException("Server refused BINARY transmission")

        rsp = self._send_command(
            DoCmd(option=TelnetOption.BINARY),
            responses=[
                WillCmd(option=TelnetOption.BINARY),
                WontCmd(option=TelnetOption.BINARY),
            ],
            timeout=self._connect_timeout,
        )
        if isinstance(rsp, WontCmd):
            raise SerialException("Server refused BINARY transmission")

        rsp = self._send_command(
            WillCmd(option=TelnetOption.COM_PORT_OPTION),
            responses=[
                DoCmd(option=TelnetOption.COM_PORT_OPTION),
                DontCmd(option=TelnetOption.COM_PORT_OPTION),
            ],
            timeout=self._connect_timeout,
        )
        if isinstance(rsp, DontCmd):
            raise SerialException("Server refused COM-PORT-OPTION")

        self._engine.mark_negotiated()
        LOGGER.debug("Negotiation complete: server accepted COM-PORT-OPTION")

        for cmd in self._engine.build_state_mask_commands():
            with self._socket_timeout(self._connect_timeout):
                self._send_and_wait(cmd)

    def _build_port_config_commands(
        self, update: PortSettingsUpdate
    ) -> Generator[Rfc2217Command]:
        # Flow control is a single RFC 2217 setting, so send the merged state
        if update.rtscts is not None or update.xonxoff is not None:
            settings = dataclasses.replace(
                update, rtscts=self._rtscts, xonxoff=self._xonxoff
            )
        else:
            settings = update

        return self._engine.build_port_config_commands(settings)

    def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        """Send serial port configuration to the access server."""
        # Let parent set socket timeout
        super()._reconfigure_port(update)

        if not self._engine.negotiated:
            return

        LOGGER.debug("Configuring port: %s", update)

        for cmd in self._build_port_config_commands(update):
            with self._socket_timeout(self._connect_timeout):
                self._send_and_wait(cmd)

        LOGGER.debug("Port configuration complete")

    def _close(self) -> None:
        """Close the connection and reset state."""
        self._engine.reset()
        self._data_buffer.clear()
        LOGGER.debug("Closing RFC 2217 connection")
        super()._close()

    # -- low-level send/receive helpers -------------------------------------

    def _send_command(
        self,
        cmd: TelnetCommand | Rfc2217Command,
        responses: list[TelnetCommand] | None = None,
        timeout: float | None = None,
    ) -> TelnetCommand | None:
        """Encode and send a command over the socket.

        If ``responses`` is provided, block until one of the expected responses
        arrives and return it.  ``timeout`` bounds the wait in seconds.
        """
        assert self._socket is not None
        data = encode_command(cmd)
        LOGGER.debug("TX cmd: %r  [%s]", cmd, data.hex(" "))
        self._socket.sendall(data)

        if responses is None:
            return None

        with self._socket_timeout(timeout):
            while True:
                match = self._engine.pop_matching_telnet(responses)
                if match is not None:
                    LOGGER.debug("RX response: %r", match)
                    return match

                self._recv_and_process()

    def _recv_and_process(self) -> None:
        """Read raw bytes from socket, feed through parser, dispatch results."""
        assert self._socket is not None
        buf = bytearray(self._receive_buffer_size)
        n = self._socket.recv_into(buf)

        if n == 0:
            self._mark_broken(
                OSError(errno.EIO, "RFC 2217 connection closed by server")
            )
            self._check_broken()

        raw = bytes(buf[:n])
        LOGGER.debug("RX raw: %d bytes  [%s]", n, raw.hex(" "))
        serial_data, responses = self._engine.feed(raw)
        self._data_buffer.extend(serial_data)

        for response in responses:
            self._send_command(response)

    def _drain_data_buffer(self, target: memoryview) -> int:
        """Copy buffered serial data into ``target`` when available."""
        if not self._data_buffer:
            return 0

        n = min(len(target), len(self._data_buffer))
        target[:n] = self._data_buffer[:n]
        del self._data_buffer[:n]
        LOGGER.debug("Read %d bytes from data buffer", n)
        return n

    def _send_and_wait(self, cmd: Rfc2217Command) -> Rfc2217Command:
        """Send a command and block until the matching server ack arrives."""
        cmd_id = cmd.CMD_ID
        self._send_command(cmd)
        LOGGER.debug("Waiting for %s ack...", type(cmd).__name__)

        while True:
            pending = self._engine.pop_pending_rfc2217(cmd_id)

            if pending is not None:
                LOGGER.debug("RX ack: %r", pending)
                return pending

            self._recv_and_process()

    def num_unread_bytes(self) -> int:
        """Return the number of buffered serial data bytes waiting to be read."""
        return len(self._data_buffer)

    def _reset_write_buffer(self) -> None:
        """Reset the write buffer."""
        self._send_and_wait(PurgeDataCmd(what=PurgeDataValue.RECEIVE))

    def _reset_read_buffer(self) -> None:
        """Reset the read buffer."""
        self._send_and_wait(PurgeDataCmd(what=PurgeDataValue.TRANSMIT))
        self._data_buffer.clear()

    def _write(self, b: Buffer, *, timeout: float | None) -> int:
        """Write data bytes to socket, escaping any 0xFF."""
        assert self._socket is not None
        data = bytes(b)
        escaped = iac_escape(data)
        LOGGER.debug(
            "TX data: %d bytes (%d on wire)  [%s]",
            len(data),
            len(escaped),
            escaped.hex(" "),
        )

        with self._socket_timeout(timeout):
            self._socket.sendall(escaped)

        return len(data)

    def _readinto(self, b: Buffer, *, timeout: float | None) -> int:
        """Read data bytes into buffer, transparently handling telnet framing."""
        m = memoryview(b).cast("B")

        # Serve from already-buffered data first
        n = self._drain_data_buffer(m)
        if n:
            return n

        # Read from socket and feed through parser
        assert self._socket is not None
        buf = bytearray(max(len(m), self._receive_buffer_size))

        # Individual RFC2217 socket reads may not always translate into real serial data
        # we need to loop until it actually produces some, or we hit the timeout limit
        remaining_timeout = timeout
        while True:
            with measure_time() as get_elapsed:
                try:
                    with self._socket_timeout(remaining_timeout):
                        n = self._socket.recv_into(buf)
                except (TimeoutError, BlockingIOError):
                    return 0

            if remaining_timeout is not None:
                remaining_timeout -= get_elapsed()

            if n == 0:
                self._mark_broken(
                    OSError(errno.EIO, "RFC 2217 connection closed by server")
                )
                self._check_broken()

            raw = bytes(buf[:n])
            LOGGER.debug("RX raw (readinto): %d bytes  [%s]", n, raw.hex(" "))
            serial_data, responses = self._engine.feed(raw)
            self._data_buffer.extend(serial_data)

            for response in responses:
                self._send_command(response)

            # Serve whatever data the parser produced
            n = self._drain_data_buffer(m)
            if n:
                return n

            if remaining_timeout is not None and remaining_timeout <= 0:
                return 0

    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set DTR/RTS via SET_CONTROL commands."""
        LOGGER.debug("Setting modem pins: %r", modem_pins)

        for cmd in self._engine.build_modem_pin_commands(modem_pins):
            self._send_and_wait(cmd)

    def _get_modem_pins(self) -> ModemPins:
        """Return modem pin state from the last NOTIFY-MODEMSTATE."""

        # Process any pending data to ensure we have the latest modem state
        with self._socket_timeout(0):  # noqa: SIM117
            with suppress(BlockingIOError):
                self._recv_and_process()

        return self._engine.get_modem_pins()

    def _flush(self) -> None:
        """Wait for the server to acknowledge all preceding writes."""
        if not self._engine.negotiated:
            return

        # RFC2217 has no flush. Instead, we "flush" the pipe with a req/rsp sequence.
        self._send_and_wait(SetBaudrateCmd(baudrate=self._baudrate))


class _RFC2217ProxyProtocol(asyncio.Protocol):
    """Bridge protocol between asyncio TCP transport and RFC2217SerialTransport."""

    def __init__(self, serial_transport: RFC2217SerialTransport) -> None:
        """Initialize the proxy protocol."""
        self._serial_transport = serial_transport

    def data_received(self, data: bytes) -> None:
        """Forward received data to the serial transport for parsing."""
        self._serial_transport._data_received(data)

    def pause_writing(self) -> None:
        """Propagate backpressure from TCP transport to serial protocol."""
        self._serial_transport._protocol.pause_writing()

    def resume_writing(self) -> None:
        """Propagate resume signal from TCP transport to serial protocol."""
        self._serial_transport._protocol.resume_writing()

    def connection_lost(self, exc: Exception | None) -> None:
        """Handle TCP connection loss."""
        self._serial_transport._tcp_connection_lost(exc)


class RFC2217SerialTransport(BaseSerialTransport):
    """Async RFC 2217 serial transport over TCP."""

    transport_name = "rfc2217"
    _serial: RFC2217Serial | None

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        """Initialize the RFC 2217 serial transport."""
        super().__init__(loop, protocol)
        self._telnet_waiters: list[
            tuple[list[TelnetCommand], asyncio.Future[TelnetCommand]]
        ] = []
        self._rfc2217_waiters: dict[Rfc2217CmdId, asyncio.Future[Rfc2217Command]] = {}
        self._tcp_transport: asyncio.Transport | None = None
        self._tcp_connection_lost_waiter: asyncio.Future[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._needs_flush = False

    # -- connection lifecycle -----------------------------------------------

    async def _connect(
        self,
        *,
        path: str | None = None,
        **kwargs: Unpack[ConnectKwargs],
    ) -> None:
        """Connect to the RFC 2217 server and negotiate COM-PORT-OPTION."""
        self._serial = RFC2217Serial(path=path, **kwargs)
        assert self._serial is not None

        self._extra["serial"] = self._serial

        self._tcp_connection_lost_waiter = self._loop.create_future()

        async with asyncio_timeout(self._serial._connect_timeout):
            tcp_transport, _ = await self._loop.create_connection(
                lambda: _RFC2217ProxyProtocol(self),
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

            await self._negotiate()
            await self._reconfigure_port(self._serial._all_settings())

        self._call_protocol_connection_made()

    async def _negotiate(self) -> None:
        """Perform the initial WILL/DO handshake for COM-PORT-OPTION."""
        LOGGER.debug("Starting RFC 2217 negotiation")

        rsp = await self._send_command(
            WillCmd(option=TelnetOption.BINARY),
            responses=[
                DoCmd(option=TelnetOption.BINARY),
                DontCmd(option=TelnetOption.BINARY),
            ],
        )
        if isinstance(rsp, DontCmd):
            raise SerialException("Server refused BINARY transmission")

        rsp = await self._send_command(
            DoCmd(option=TelnetOption.BINARY),
            responses=[
                WillCmd(option=TelnetOption.BINARY),
                WontCmd(option=TelnetOption.BINARY),
            ],
        )
        if isinstance(rsp, WontCmd):
            raise SerialException("Server refused BINARY transmission")

        rsp = await self._send_command(
            WillCmd(option=TelnetOption.COM_PORT_OPTION),
            responses=[
                DoCmd(option=TelnetOption.COM_PORT_OPTION),
                DontCmd(option=TelnetOption.COM_PORT_OPTION),
            ],
        )
        if isinstance(rsp, DontCmd):
            raise SerialException("Server refused COM-PORT-OPTION")

        assert self._serial is not None
        self._serial._engine.mark_negotiated()
        LOGGER.debug("Negotiation complete: server accepted COM-PORT-OPTION")

        for cmd in self._serial._engine.build_state_mask_commands():
            await self._send_and_wait(cmd)

    async def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        """Send serial port configuration to the access server."""
        assert self._serial is not None
        LOGGER.debug("Configuring port: %s", update)

        for cmd in self._serial._build_port_config_commands(update):
            await self._send_and_wait(cmd)

        LOGGER.debug("Port configuration complete")

    # -- low-level send/receive helpers -------------------------------------

    @overload
    def _send_command(
        self,
        cmd: TelnetCommand | Rfc2217Command,
    ) -> None: ...

    @overload
    def _send_command(
        self,
        cmd: TelnetCommand | Rfc2217Command,
        responses: list[TelnetCommand],
    ) -> asyncio.Future[TelnetCommand]: ...

    def _send_command(
        self,
        cmd: TelnetCommand | Rfc2217Command,
        responses: list[TelnetCommand] | None = None,
    ) -> asyncio.Future[TelnetCommand] | None:
        """Encode and send a command over the TCP transport.

        If ``responses`` is provided, return a future that resolves when one of
        the expected responses arrives.
        """
        assert self._tcp_transport is not None
        data = encode_command(cmd)
        LOGGER.debug("TX cmd: %r  [%s]", cmd, data.hex(" "))
        self._tcp_transport.write(data)

        if responses is None:
            return None

        # Check if already pending
        assert self._serial is not None
        match = self._serial._engine.pop_matching_telnet(responses)
        if match is not None:
            fut: asyncio.Future[TelnetCommand] = self._loop.create_future()
            fut.set_result(match)
            return fut

        waiter: asyncio.Future[TelnetCommand] = self._loop.create_future()
        self._telnet_waiters.append((responses, waiter))
        return waiter

    def _data_received(self, data: bytes) -> None:
        """Handle raw data from the TCP transport."""
        LOGGER.debug("RX raw: %d bytes  [%s]", len(data), data.hex(" "))
        assert self._serial is not None
        serial_data, responses = self._serial._engine.feed(data)
        self._resolve_pending_waiters()

        for response in responses:
            self._send_command(response)

        if serial_data and self._connection_made_called:
            self._protocol.data_received(serial_data)

    async def _send_and_wait(self, cmd: Rfc2217Command) -> Rfc2217Command:
        """Send a command and wait for the matching server ack."""
        waiter: asyncio.Future[Rfc2217Command] = self._loop.create_future()
        assert self._serial is not None
        pending = self._serial._engine.pop_pending_rfc2217(cmd.CMD_ID)
        if pending is not None:
            self._send_command(cmd)
            LOGGER.debug("RX ack (already pending): %r", pending)
            return pending

        self._rfc2217_waiters[cmd.CMD_ID] = waiter
        try:
            self._send_command(cmd)
            LOGGER.debug("Waiting for %s ack...", type(cmd).__name__)
            result = await waiter
        finally:
            if self._rfc2217_waiters.get(cmd.CMD_ID) is waiter:
                self._rfc2217_waiters.pop(cmd.CMD_ID, None)

        LOGGER.debug("RX ack: %r", result)
        return result

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Write data to the serial port, escaping IAC bytes."""
        self._check_broken()
        if self._closing or self._connection_lost_called:
            return
        assert self._tcp_transport is not None
        escaped = iac_escape(bytes(data))
        LOGGER.debug("TX data: %d bytes (%d on wire)", len(data), len(escaped))
        self._tcp_transport.write(escaped)
        self._needs_flush = True

    async def _get_modem_pins(self) -> ModemPins:
        """Return modem pin state from the last NOTIFY-MODEMSTATE."""
        assert self._serial is not None
        return self._serial._engine.get_modem_pins()

    async def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set DTR/RTS via SET_CONTROL commands."""
        LOGGER.debug("Setting modem pins: %r", modem_pins)
        assert self._serial is not None

        for cmd in self._serial._engine.build_modem_pin_commands(modem_pins):
            await self._send_and_wait(cmd)

    def _resolve_pending_waiters(self) -> None:
        """Resolve any async waiters whose commands have now been parsed."""
        assert self._serial is not None

        remaining: list[tuple[list[TelnetCommand], asyncio.Future[TelnetCommand]]] = []

        for expected, waiter in self._telnet_waiters:
            if waiter.done():
                continue

            match = self._serial._engine.pop_matching_telnet(expected)
            if match is not None:
                waiter.set_result(match)
            else:
                remaining.append((expected, waiter))

        self._telnet_waiters = remaining

        for rfc_cmd_id, rfc_waiter in list(self._rfc2217_waiters.items()):
            if rfc_waiter.done():
                self._rfc2217_waiters.pop(rfc_cmd_id, None)
                continue

            rfc_pending = self._serial._engine.pop_pending_rfc2217(rfc_cmd_id)
            if rfc_pending is not None:
                self._rfc2217_waiters.pop(rfc_cmd_id, None)
                rfc_waiter.set_result(rfc_pending)

    # -- transport lifecycle ------------------------------------------------

    def _tcp_connection_lost(self, exc: Exception | None) -> None:
        """Handle TCP connection loss."""
        if (
            self._tcp_connection_lost_waiter is not None
            and not self._tcp_connection_lost_waiter.done()
        ):
            self._tcp_connection_lost_waiter.set_result(None)

        if self._connection_lost_called:
            return
        self._closing = True
        self._tcp_transport = None

        if not self._user_initiated_close:
            self._mark_broken(
                exc
                if exc is not None
                else OSError(errno.EIO, "RFC 2217 connection closed by server")
            )

        # Pending in-protocol waiters can't resolve cleanly mid-handshake, so
        # always fail them with *some* exception even on a user-initiated close.
        waiter_exc = (
            exc if exc is not None else OSError(errno.EIO, "RFC 2217 transport closed")
        )
        for _expected, telnet_waiter in self._telnet_waiters:
            if not telnet_waiter.done():
                telnet_waiter.set_exception(waiter_exc)
        self._telnet_waiters.clear()

        for rfc2217_waiter in self._rfc2217_waiters.values():
            if not rfc2217_waiter.done():
                rfc2217_waiter.set_exception(waiter_exc)
        self._rfc2217_waiters.clear()

        if self._serial is not None:
            self._serial._engine.reset()

        self._call_protocol_connection_lost(exc)

    def pause_reading(self) -> None:
        """Pause reading from the TCP transport."""
        assert self._tcp_transport is not None
        self._tcp_transport.pause_reading()

    def resume_reading(self) -> None:
        """Resume reading from the TCP transport."""
        assert self._tcp_transport is not None
        self._tcp_transport.resume_reading()

    def close(self) -> None:
        """Close the transport, draining buffered writes to the server first."""
        if self._connection_lost_called or self._close_task is not None:
            return
        self._closing = True
        self._mark_user_closed()
        self._arm_close_timeout()

        if self._tcp_transport is None:
            self._tcp_connection_lost(None)
            return

        if not self._needs_flush:
            self._tcp_transport.close()
            return

        # Closing the TCP socket only flushes our own send buffer; the server may
        # still drop bytes it has buffered toward the device on disconnect. Drain
        # them first with a req/rsp round-trip, then tear down the connection.
        self._close_task = self._loop.create_task(self._flush_and_close())

    async def _flush_and_close(self) -> None:
        # If the connection drops (or the flush fails) mid-drain, close anyway
        with suppress(Exception):
            await self._flush()

        if self._tcp_transport is not None:
            self._tcp_transport.close()

    def abort(self) -> None:
        """Abort the transport immediately."""
        if self._connection_lost_called:
            return
        self._closing = True
        self._mark_user_closed()

        if self._close_task is not None:
            self._close_task.cancel()
            self._close_task = None

        if self._tcp_transport is not None:
            self._tcp_transport.abort()
        else:
            self._tcp_connection_lost(None)

    async def _flush(self) -> None:
        """Flush write buffers, waiting until all data is written, internal."""
        assert self._serial is not None
        if not self._serial._engine.negotiated:
            return

        # RFC2217 has no flush. Instead, we "flush" the pipe with a req/rsp sequence.
        await self._send_and_wait(SetBaudrateCmd(baudrate=self._serial._baudrate))

        # The round-trip confirms the server drained everything we sent, so a
        # subsequent close() needs no further flush unless we write again.
        self._needs_flush = False

    def get_write_buffer_size(self) -> int:
        """Get the number of bytes currently in the write buffer."""
        if self._tcp_transport is None:
            return 0

        return self._tcp_transport.get_write_buffer_size()

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
        if self._tcp_transport is None:
            return False

        return self._tcp_transport.can_write_eof()


register_uri_handler(
    scheme="rfc2217://",
    unique_scheme="rfc2217://",
    sync_cls=RFC2217Serial,
    async_transport_cls=RFC2217SerialTransport,
)
