"""RFC2217 serial port tests."""

import asyncio
from collections.abc import Iterator
import contextlib
import socket
import sys
import threading

import pytest

if sys.platform == "emscripten":
    pytest.skip(
        "RFC2217 transport isn't available under Pyodide",
        allow_module_level=True,
    )

from serialx import Serial, SerialException, create_serial_connection
from serialx.common import PortSettingsUpdate, UnsupportedSetting, measure_time
from serialx.platforms.serial_rfc2217 import Rfc2217, TelnetParser
from serialx.platforms.serial_rfc2217.types import (
    DoCmd,
    FlowcontrolResumeCmd,
    FlowcontrolSuspendCmd,
    LineStateFlag,
    ModemStateFlag,
    NotifyLinestateCmd,
    NotifyModemstateCmd,
    Rfc2217Command,
    SetBaudrateCmd,
    SignatureCmd,
    TelnetOption,
    WillCmd,
    encode_command,
)

from .common import HUB4COM_BINARY, SerialBackend, SerialPair, create_hub4com_pair
from .socket_relay import create_accept_then_close_server, create_silent_server


def test_unknown_telnet_option() -> None:
    """Test that unknown telnet option codes are accepted."""
    opt = TelnetOption(99)
    assert opt.value == 99
    assert "UNKNOWN" in opt.name


def test_will_cmd_roundtrip() -> None:
    """Test WillCmd to_bytes/from_bytes round-trip."""
    cmd = WillCmd(option=TelnetOption.BINARY)
    assert WillCmd.from_bytes(cmd.to_bytes()) == cmd


def test_signature_roundtrip() -> None:
    """Test SignatureCmd to_bytes/from_bytes round-trip."""
    cmd = SignatureCmd(signature=b"serialx")
    assert cmd.to_bytes() == b"serialx"
    assert SignatureCmd.from_bytes(b"serialx") == cmd


def test_notify_linestate_roundtrip() -> None:
    """Test NotifyLinestateCmd to_bytes/from_bytes round-trip."""
    cmd = NotifyLinestateCmd(linestate=LineStateFlag(0x1E))
    assert NotifyLinestateCmd.from_bytes(cmd.to_bytes()) == cmd


def test_notify_modemstate_roundtrip() -> None:
    """Test NotifyModemstateCmd to_bytes/from_bytes round-trip."""
    cmd = NotifyModemstateCmd(modemstate=ModemStateFlag(0xB0))
    assert NotifyModemstateCmd.from_bytes(cmd.to_bytes()) == cmd


def test_flowcontrol_suspend_roundtrip() -> None:
    """Test FlowcontrolSuspendCmd to_bytes/from_bytes round-trip."""
    cmd = FlowcontrolSuspendCmd()
    assert cmd.to_bytes() == b""
    assert FlowcontrolSuspendCmd.from_bytes(b"") == cmd


def test_flowcontrol_resume_roundtrip() -> None:
    """Test FlowcontrolResumeCmd to_bytes/from_bytes round-trip."""
    cmd = FlowcontrolResumeCmd()
    assert cmd.to_bytes() == b""
    assert FlowcontrolResumeCmd.from_bytes(b"") == cmd


def test_invalid_byte_size_rejected_before_sending() -> None:
    """An invalid byte size fails locally instead of waiting for a server ack."""
    engine = Rfc2217()
    with pytest.raises(UnsupportedSetting):
        list(engine.build_port_config_commands(PortSettingsUpdate(byte_size=123)))


def test_sync_negotiate_timeout_silent_server() -> None:
    """Sync RFC2217 negotiation times out when the server never responds."""
    with create_silent_server() as addr:
        with measure_time() as elapsed:
            with pytest.raises(TimeoutError):
                with Serial.from_url(
                    f"rfc2217://{addr}", baudrate=115200, connect_timeout=0.1
                ):
                    pass

        assert 0.09 <= elapsed() < 1.1


async def test_async_negotiate_timeout_silent_server() -> None:
    """Async RFC2217 negotiation times out when the server never responds."""
    with create_silent_server() as addr:
        with measure_time() as elapsed:
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await create_serial_connection(
                    asyncio.get_running_loop(),
                    asyncio.Protocol,
                    url=f"rfc2217://{addr}",
                    baudrate=115200,
                    connect_timeout=0.1,
                )

        assert 0.1 <= elapsed() < 1.0


def test_sync_peer_close_during_negotiation_raises() -> None:
    """Peer-side TCP close surfaces as OSError, not a silent 0-byte read."""
    with create_accept_then_close_server() as addr:
        with pytest.raises(OSError, match="RFC 2217 connection closed by server"):
            with Serial.from_url(
                f"rfc2217://{addr}", baudrate=115200, connect_timeout=2.0
            ):
                pass


@contextlib.contextmanager
def create_rfc2217_ack_server() -> Iterator[tuple[str, list[Rfc2217Command]]]:
    """Accept one client, agree to every option, and ack every COM-PORT command."""
    server = socket.create_server(("127.0.0.1", 0))
    received: list[Rfc2217Command] = []

    def serve() -> None:
        client, _ = server.accept()
        parser = TelnetParser()
        # The client may close while an answer is still being sent
        with client, contextlib.suppress(OSError):
            while data := client.recv(4096):
                for item in parser.feed(data):
                    if isinstance(item, WillCmd):
                        client.sendall(encode_command(DoCmd(option=item.option)))
                    elif isinstance(item, DoCmd):
                        client.sendall(encode_command(WillCmd(option=item.option)))
                    elif isinstance(item, Rfc2217Command):
                        received.append(item)
                        # The client also accepts the unshifted command code
                        client.sendall(encode_command(item))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        yield f"127.0.0.1:{server.getsockname()[1]}", received
    finally:
        server.close()
        thread.join(timeout=1)


def test_sync_rejected_setting_sends_nothing() -> None:
    """A rejected byte size fails before any setting reaches the server."""
    with create_rfc2217_ack_server() as (addr, received):
        with Serial.from_url(f"rfc2217://{addr}", baudrate=9600) as serial:
            received.clear()

            with pytest.raises(UnsupportedSetting):
                serial.reconfigure_port(baudrate=19200, byte_size=9)

            # Round trip a valid setting so anything sent earlier has arrived
            serial.reconfigure_port(baudrate=9600)
            assert received == [SetBaudrateCmd(baudrate=9600)]


async def test_async_rejected_setting_sends_nothing() -> None:
    """A rejected byte size fails before any setting reaches the server."""
    with create_rfc2217_ack_server() as (addr, received):
        transport, _ = await create_serial_connection(
            asyncio.get_running_loop(),
            asyncio.Protocol,
            url=f"rfc2217://{addr}",
            baudrate=9600,
        )
        try:
            received.clear()

            with pytest.raises(UnsupportedSetting):
                await transport.reconfigure_port(baudrate=19200, byte_size=9)

            # Round trip a valid setting so anything sent earlier has arrived
            await transport.reconfigure_port(baudrate=9600)
            assert received == [SetBaudrateCmd(baudrate=9600)]
        finally:
            transport.close()


@pytest.mark.skipif(not HUB4COM_BINARY, reason="hub4com not available")
def test_sync_negotiate_comport_rejected_by_hub4com(serial_pair: SerialPair) -> None:
    """Sync RFC2217 negotiation fails when the server responds with `Wont`."""  # codespell:ignore wont
    if serial_pair.backends != (SerialBackend.ADAPTER,):
        pytest.skip("Requires a bare adapter pair")

    with create_hub4com_pair(serial_pair.left, serial_pair.right, comport="no") as (
        rfc_left,
        _rfc_right,
    ):
        with pytest.raises(SerialException, match="COM-PORT-OPTION"):
            with Serial.from_url(rfc_left, baudrate=115200, connect_timeout=1.0):
                pass


@pytest.mark.skipif(not HUB4COM_BINARY, reason="hub4com not available")
async def test_async_negotiate_comport_rejected_by_hub4com(
    serial_pair: SerialPair,
) -> None:
    """Async RFC2217 negotiation fails when the server responds with `Wont`."""  # codespell:ignore wont
    if serial_pair.backends != (SerialBackend.ADAPTER,):
        pytest.skip("Requires a bare adapter pair")

    with create_hub4com_pair(serial_pair.left, serial_pair.right, comport="no") as (
        rfc_left,
        _rfc_right,
    ):
        with pytest.raises(SerialException, match="COM-PORT-OPTION"):
            await create_serial_connection(
                asyncio.get_running_loop(),
                asyncio.Protocol,
                url=rfc_left,
                baudrate=115200,
                connect_timeout=1.0,
            )
