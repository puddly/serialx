"""Async transport tests."""

import asyncio
import contextlib
import logging
import os
import sys
from unittest.mock import Mock

if sys.version_info >= (3, 11):
    from asyncio import timeout as asyncio_timeout
else:
    from async_timeout import timeout as asyncio_timeout

import pytest

import serialx
from serialx import (
    BaseSerialTransport,
    ModemPins,
    Parity,
    PinState,
    StopBits,
    create_serial_connection,
    get_serial_classes,
    open_serial_connection,
)
from tests.common import (
    SerialBackend,
    SerialPair,
    SerialQuirk,
    async_create_serial_pair,
)

LOGGER = logging.getLogger(__name__)


# --- Data transmission ---


async def test_async_all_bytes(serial_pair: SerialPair) -> None:
    """Test that all bytes 0-255 can be transmitted."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        data = bytes(range(256))
        left.write_nowait(data)
        result = await right.readexactly(len(data))
        assert result == data


async def test_async_segmented_binary_data(serial_pair: SerialPair) -> None:
    """Test binary data sent in segments."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        segment_size = 16
        data = bytes(range(256))

        for i in range(0, 256, segment_size):
            segment = data[i : i + segment_size]
            left.write_nowait(segment)
            result = await right.readexactly(len(segment))
            assert result == segment


@pytest.mark.parametrize("size", [1, 16, 64, 256, 512, 1024])
async def test_async_binary_payload_sizes(serial_pair: SerialPair, size: int) -> None:
    """Test various binary payload sizes."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        data = bytes([i % 256 for i in range(size)])
        left.write_nowait(data)
        result = await right.readexactly(len(data))
        assert result == data


async def test_async_null_bytes(serial_pair: SerialPair) -> None:
    """Test that null bytes (0x00) can be transmitted."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        null_data = b"\x00" * 64
        left.write_nowait(null_data)
        result = await right.readexactly(len(null_data))
        assert result == null_data


async def test_async_readuntil(serial_pair: SerialPair) -> None:
    """Test readuntil reads up to and including the default newline separator."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.write_nowait(b"hello\nworld\n")
        assert await right.readuntil() == b"hello\n"
        assert await right.readuntil(b"\n") == b"world\n"


async def test_async_readuntil_custom_separator(serial_pair: SerialPair) -> None:
    """Test readuntil with a multi-byte custom separator."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.write_nowait(b"first||second||tail")
        assert await right.readuntil(b"||") == b"first||"
        assert await right.readuntil(b"||") == b"second||"


async def test_async_readuntil_repeated_separator(serial_pair: SerialPair) -> None:
    """Test readuntil with consecutive separators that don't align as framing."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.write_nowait(b"foo|||||bar||tail||")
        assert await right.readuntil(b"||") == b"foo||"
        assert await right.readuntil(b"||") == b"||"
        assert await right.readuntil(b"||") == b"|bar||"
        assert await right.readuntil(b"||") == b"tail||"


async def test_async_readline(serial_pair: SerialPair) -> None:
    """Test readline returns successive newline-terminated lines."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.write_nowait(b"alpha\nbeta\ngamma\n")
        assert await right.readline() == b"alpha\n"
        assert await right.readline() == b"beta\n"
        assert await right.readline() == b"gamma\n"


async def test_async_writelines(serial_pair: SerialPair) -> None:
    """Test writelines writes an iterable of buffers in order."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.writelines_nowait([b"foo", b"bar", b"baz"])
        assert await right.readexactly(9) == b"foobarbaz"


async def test_async_overlapping_read_write(serial_pair: SerialPair) -> None:
    """Test that read and write can overlap, data is buffered."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        data = bytes(range(256))
        read = b""

        left.write_nowait(data[:100])
        read += await right.readexactly(10)
        left.write_nowait(data[100:150])
        read += await right.readexactly(10)
        left.write_nowait(data[150:])
        read += await right.readexactly(10)
        read += await right.readexactly(256 - 30)

        assert read == data


@pytest.mark.parametrize(
    "baudrate,chunk_size",
    [
        (9600, 1),
        (9600, 16),
        (115200, 1),
        (115200, 16),
        (115200, 64),
        (921600, 1),
        (921600, 16),
        (921600, 256),
        (921600, 1024),
    ],
)
async def test_async_random_large(
    serial_pair: SerialPair, baudrate: int, chunk_size: int
) -> None:
    """Test random read/write at various speeds."""
    if (
        baudrate > 230400
        and sys.platform == "darwin"
        and (
            serial_pair.uri_scheme in ("posix://", "extended-posix://")
            or SerialBackend.SER2NET in serial_pair.backends
        )
    ):
        pytest.xfail("macOS termios lacks constants above B230400")

    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=baudrate
    ) as (left, right):
        data = os.urandom(chunk_size)
        left.write_nowait(data)
        read_data = await right.readexactly(chunk_size)
        assert read_data == data


@pytest.mark.parametrize("iterations", [16, 32, 64])
async def test_async_repeated_write_read_cycles(
    serial_pair: SerialPair, iterations: int
) -> None:
    """Test repeated write/read cycles."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        data = bytes(range(256))

        for _ in range(iterations):
            left.write_nowait(data)
            result = await right.readexactly(len(data))
            assert result == data


async def test_async_buffered_writes_then_read(serial_pair: SerialPair) -> None:
    """Test multiple writes followed by a single read."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        chunk = bytes(range(256))
        iterations = 4

        for _ in range(iterations):
            left.write_nowait(chunk)

        total_size = len(chunk) * iterations
        result = await right.readexactly(total_size)
        expected = chunk * iterations
        assert result == expected


@pytest.mark.parametrize("payload_size", [1024, 2048])
async def test_async_large_payload(serial_pair: SerialPair, payload_size: int) -> None:
    """Test large payload transmission."""
    if sys.platform == "darwin" and (
        serial_pair.uri_scheme in ("posix://", "extended-posix://")
        or SerialBackend.SER2NET in serial_pair.backends
    ):
        pytest.xfail("macOS termios lacks constants above B230400")

    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=921600
    ) as (left, right):
        data = bytes([i % 256 for i in range(payload_size)])
        left.write_nowait(data)
        result = await right.readexactly(len(data))
        assert result == data


async def test_async_rapid_small_writes(serial_pair: SerialPair) -> None:
    """Test rapid succession of small writes."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        iterations = 256
        received = bytearray()

        for i in range(iterations):
            data = bytes([i % 256])
            left.write_nowait(data)
            result = await right.readexactly(1)
            received.extend(result)

        expected = bytes([i % 256 for i in range(iterations)])
        assert bytes(received) == expected


@pytest.mark.parametrize("baudrate,iterations", [(9600, 4), (115200, 32), (921600, 32)])
async def test_async_sustained_throughput(
    serial_pair: SerialPair, baudrate: int, iterations: int
) -> None:
    """Test sustained data throughput at various baudrates."""
    if (
        baudrate > 230400
        and sys.platform == "darwin"
        and (
            serial_pair.uri_scheme in ("posix://", "extended-posix://")
            or SerialBackend.SER2NET in serial_pair.backends
        )
    ):
        pytest.xfail("macOS termios lacks constants above B230400")

    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=baudrate
    ) as (left, right):
        chunk = os.urandom(1024)
        for _ in range(iterations):
            left.write_nowait(chunk)
            result = await right.readexactly(len(chunk))
            assert result == chunk


# --- Configuration ---


@pytest.mark.parametrize(
    "baudrate", [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
)
async def test_async_valid_baudrates(serial_pair: SerialPair, baudrate: int) -> None:
    """Test that valid baudrates are accepted."""
    if (
        baudrate > 230400
        and sys.platform == "darwin"
        and (
            serial_pair.uri_scheme in ("posix://", "extended-posix://")
            or SerialBackend.SER2NET in serial_pair.backends
        )
    ):
        pytest.xfail("macOS termios lacks constants above B230400")

    async with serialx.async_serial_for_url(
        serial_pair.left, baudrate=baudrate
    ) as left:
        assert left.baudrate == baudrate
        left.write_nowait(b"test")


async def test_async_nonstandard_baudrate(serial_pair: SerialPair) -> None:
    """Test that a non-standard baudrate (no termios constant) is accepted."""
    if serial_pair.uri_scheme in ("posix://", "extended-posix://"):
        pytest.skip("Base POSIX backends only support standard baudrates")

    if sys.platform == "darwin" and SerialBackend.SER2NET in serial_pair.backends:
        pytest.xfail("macOS termios lacks constants above B230400")

    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=200000
    ) as (left, right):
        assert left.baudrate == 200000
        left.write_nowait(b"test")
        assert await right.readexactly(4) == b"test"


@pytest.mark.parametrize(
    "parity", [Parity.NONE, Parity.ODD, Parity.EVEN, Parity.MARK, Parity.SPACE]
)
async def test_async_valid_parity(serial_pair: SerialPair, parity: Parity) -> None:
    """Test that valid parity settings are accepted."""
    if (
        SerialBackend.ESPHOME_HOST in serial_pair.backends
        or SerialBackend.ESPHOME in serial_pair.backends
    ) and parity in (
        Parity.MARK,
        Parity.SPACE,
    ):
        pytest.xfail("ESPHome backend does not support MARK/SPACE parity")

    if serial_pair.uri_scheme not in ("linux://", "windows://") and parity in (
        Parity.MARK,
        Parity.SPACE,
    ):
        pytest.skip("MARK/SPACE parity requires CMSPAR (Linux) or Win32")

    async with serialx.async_serial_for_url(
        serial_pair.left, baudrate=115200, parity=parity
    ) as left:
        assert left.parity == parity
        left.write_nowait(b"test")


@pytest.mark.parametrize(
    "stopbits,expected",
    [
        (StopBits.ONE, StopBits.ONE),
        (StopBits.ONE_POINT_FIVE, StopBits.ONE_POINT_FIVE),
        (StopBits.TWO, StopBits.TWO),
        (1, StopBits.ONE),
        (1.5, StopBits.ONE_POINT_FIVE),
        (2, StopBits.TWO),
    ],
)
async def test_async_valid_stopbits(
    serial_pair: SerialPair,
    stopbits: StopBits | int | float,
    expected: StopBits,
) -> None:
    """Test that valid stopbits settings are accepted."""
    if (
        SerialBackend.ESPHOME_HOST in serial_pair.backends
        or SerialBackend.ESPHOME in serial_pair.backends
    ) and expected is StopBits.ONE_POINT_FIVE:
        pytest.xfail("ESPHome backend does not support 1.5 stop bits")

    if (
        serial_pair.uri_scheme not in ("windows://",)
        and expected is StopBits.ONE_POINT_FIVE
    ):
        pytest.skip("1.5 stop bits only supported on Win32")

    async with serialx.async_serial_for_url(
        serial_pair.left, baudrate=115200, stopbits=stopbits
    ) as left:
        assert left.stopbits == expected
        left.write_nowait(b"test")


@pytest.mark.parametrize("byte_size", [5, 6, 7, 8])
async def test_async_valid_byte_size(serial_pair: SerialPair, byte_size: int) -> None:
    """Test that valid byte sizes are accepted."""
    if sys.platform == "emscripten" and byte_size in (5, 6):
        pytest.skip("Web Serial spec only defines dataBits 7 or 8")

    async with serialx.async_serial_for_url(
        serial_pair.left, baudrate=115200, byte_size=byte_size
    ) as left:
        assert left.byte_size == byte_size
        left.write_nowait(b"test")


async def test_async_invalid_byte_size(serial_pair: SerialPair) -> None:
    """Test that an invalid byte size is rejected."""
    if SerialBackend.SOCKET in serial_pair.backends:
        pytest.skip("socket transport does not validate serial settings")

    with pytest.raises(Exception):
        async with serialx.async_serial_for_url(
            serial_pair.left, baudrate=115200, byte_size=123
        ):
            pass


@pytest.mark.parametrize("xonxoff", [True, False])
async def test_async_xonxoff_setting(serial_pair: SerialPair, xonxoff: bool) -> None:
    """Test that xonxoff setting is accepted."""
    async with serialx.async_serial_for_url(
        serial_pair.left, baudrate=115200, xonxoff=xonxoff
    ) as left:
        left.write_nowait(b"test")


@pytest.mark.parametrize("rtscts", [True, False])
async def test_async_rtscts_setting(serial_pair: SerialPair, rtscts: bool) -> None:
    """Test that rtscts setting is accepted."""
    if rtscts and serial_pair.uri_scheme == "posix://":
        pytest.xfail("Strict POSIX backend does not support RTS/CTS flow control")

    if rtscts and SerialBackend.ESPHOME_HOST in serial_pair.backends:
        pytest.xfail("ESPHome host does not support RTS/CTS flow control")

    async with serialx.async_serial_for_url(
        serial_pair.right, baudrate=115200
    ) as right:
        async with serialx.async_serial_for_url(
            serial_pair.left, baudrate=115200, rtscts=rtscts
        ) as left:
            left.write_nowait(b"test")

            # ser2net closes both sides concurrently, so data still in flight when the
            # right side drops RTS stays held by CTS until `closing_wait` expires
            assert await right.readexactly(4) == b"test"


async def test_async_reconfigure_flow_control(serial_pair: SerialPair) -> None:
    """Test that flow control can be switched on an open port."""
    if serial_pair.uri_scheme == "posix://":
        pytest.xfail("Strict POSIX backend does not support RTS/CTS flow control")

    if SerialBackend.ESPHOME_HOST in serial_pair.backends:
        pytest.xfail("ESPHome host does not support RTS/CTS flow control")

    async with serialx.async_serial_for_url(serial_pair.right, baudrate=9600) as right:
        async with serialx.async_serial_for_url(
            serial_pair.left, baudrate=9600
        ) as left:
            await left.write(b"before")
            assert await right.readexactly(6) == b"before"

            await left.reconfigure_port(rtscts=True, xonxoff=False, dsrdtr=False)

            await left.write(b"after")
            assert await right.readexactly(5) == b"after"


@pytest.mark.skip(
    reason="held output stalls the kernel close and wedges the adapter; needs the bounded flush"
)
@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_PAUSE_WRITING_CALLBACKS)
async def test_async_reconfigure_rtscts_holds_writes(serial_pair: SerialPair) -> None:
    """Test that enabling RTS/CTS on an open port makes a held CTS line stall writes."""
    async with serialx.async_serial_for_url(
        serial_pair.right, baudrate=115200
    ) as right:
        await right.set_modem_pins(rts=False)
        await asyncio.sleep(serial_pair.modem_line_propagation_delay)

        # `close()` drains, so bound it or it waits on CTS forever
        async with serialx.async_serial_for_url(
            serial_pair.left, baudrate=115200, close_timeout=0.5
        ) as left:
            async with asyncio_timeout(1):
                await left.write(b"x" * 1024)
                await left.flush()

            await left.reconfigure_port(rtscts=True)

            with pytest.raises(TimeoutError):
                async with asyncio_timeout(0.5):
                    await left.write(b"x" * 1024)
                    await left.flush()

            # Let the held bytes drain, or closing the port waits on them
            await right.set_modem_pins(rts=True)


# --- Lifecycle ---


async def test_async_concurrent_writes(serial_pair: SerialPair) -> None:
    """Test concurrent writes from multiple tasks."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):

        async def write_data(data: bytes) -> None:
            left.write_nowait(data)

        data1 = b"A" * 100
        data2 = b"B" * 100
        data3 = b"C" * 100

        await asyncio.gather(
            write_data(data1),
            write_data(data2),
            write_data(data3),
        )

        total_data = await right.readexactly(300)
        assert total_data == b"A" * 100 + b"B" * 100 + b"C" * 100


async def test_async_read_with_timeout(serial_pair: SerialPair) -> None:
    """Test reading with timeout."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.write_nowait(b"test")

        result = await asyncio.wait_for(right.readexactly(4), timeout=1.0)
        assert result == b"test"

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(right.readexactly(1), timeout=0.1)


async def test_async_close_is_idempotent(serial_pair: SerialPair) -> None:
    """Test closing the serial port multiple times is safe."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        assert left.transport.get_write_buffer_size() == 0
        await left.close()
        # Second close should be a no-op
        await left.close()


@pytest.mark.skip_quirks(SerialQuirk.NO_BUFFER_CONTROL)
async def test_async_pause_resume(serial_pair: SerialPair) -> None:
    """Test transport pause and resume."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.transport.pause_reading()

        right.write_nowait(b"A long message")
        await right.drain()

        # Nothing can be read
        with pytest.raises(asyncio.TimeoutError):
            async with asyncio_timeout(1):
                await left.read(1)

        left.transport.resume_reading()
        assert (await left.read(14)) == b"A long message"


async def test_async_abort(serial_pair: SerialPair) -> None:
    """Test aborting a transport discards buffered data and closes."""
    connection_lost_event = asyncio.Event()

    class AbortProtocol(asyncio.Protocol):
        def connection_lost(self, exc: Exception | None) -> None:
            connection_lost_event.set()

    transport, _ = await create_serial_connection(
        asyncio.get_running_loop(),
        AbortProtocol,
        serial_pair.left,
        baudrate=115200,
    )

    transport.write(b"data that will be discarded on abort")
    transport.abort()

    await asyncio.wait_for(connection_lost_event.wait(), timeout=2.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=2.0)
    assert transport.is_closing()


async def test_async_close_before_connect(serial_pair: SerialPair) -> None:
    """Test close during connect does not crash and stays idempotent."""

    loop = asyncio.get_running_loop()
    _serial_cls, transport_cls = get_serial_classes(serial_pair.left)
    protocol = Mock(spec=asyncio.Protocol)
    transport = transport_cls(loop=loop, protocol=protocol)

    connect_task = asyncio.create_task(
        transport.connect(path=serial_pair.left, baudrate=115200)
    )
    await asyncio.sleep(0)
    transport.close()
    transport.close()

    with contextlib.suppress(Exception):
        await asyncio.wait_for(connect_task, timeout=2.0)

    await asyncio.wait_for(transport.wait_closed(), timeout=2.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=2.0)

    assert transport.is_closing()
    assert len(protocol.connection_made.mock_calls) <= 1
    assert len(protocol.connection_lost.mock_calls) <= 1


async def test_async_abort_before_connect(serial_pair: SerialPair) -> None:
    """Test abort during connect does not crash and stays idempotent."""

    loop = asyncio.get_running_loop()
    _serial_cls, transport_cls = get_serial_classes(serial_pair.left)
    protocol = Mock(spec=asyncio.Protocol)
    transport = transport_cls(loop=loop, protocol=protocol)

    connect_task = asyncio.create_task(
        transport.connect(path=serial_pair.left, baudrate=115200)
    )
    await asyncio.sleep(0)
    transport.abort()
    transport.abort()

    with contextlib.suppress(Exception):
        await asyncio.wait_for(connect_task, timeout=2.0)

    await asyncio.wait_for(transport.wait_closed(), timeout=2.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=2.0)

    assert transport.is_closing()
    assert len(protocol.connection_made.mock_calls) <= 1
    assert len(protocol.connection_lost.mock_calls) <= 1


async def test_async_write_bytearray(serial_pair: SerialPair) -> None:
    """Test writing bytearray data."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        data = bytearray(b"hello bytearray")
        left.write_nowait(data)
        result = await right.readexactly(len(data))
        assert result == b"hello bytearray"


async def test_async_write_empty(serial_pair: SerialPair) -> None:
    """Test writing empty data is a no-op."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.write_nowait(b"")
        left.write_nowait(b"after_empty")
        result = await right.readexactly(len(b"after_empty"))
        assert result == b"after_empty"


async def test_async_transport_api(serial_pair: SerialPair) -> None:
    """Test transport public API methods."""
    async with serialx.async_serial_for_url(serial_pair.left, baudrate=115200) as left:
        transport = left.transport

        # get/set protocol
        protocol = transport.get_protocol()
        assert protocol is not None
        transport.set_protocol(protocol)
        assert transport.get_protocol() is protocol

        # write buffer size starts at 0
        assert transport.get_write_buffer_size() == 0


@pytest.mark.skip_quirks(SerialQuirk.NO_BUFFER_CONTROL)
async def test_async_transport_write_buffer_limits(serial_pair: SerialPair) -> None:
    """Test get/set write buffer limits and can_write_eof."""

    async with serialx.async_serial_for_url(serial_pair.left, baudrate=115200) as left:
        transport = left.transport

        low, high = transport.get_write_buffer_limits()
        assert 0 <= low <= high

        transport.set_write_buffer_limits(low=32 * 1024, high=128 * 1024)
        assert transport.get_write_buffer_limits() == (32 * 1024, 128 * 1024)

        assert transport.can_write_eof() in (True, False)


async def test_async_flush(serial_pair: SerialPair) -> None:
    """Test flushing async transport write buffers."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        left.write_nowait(b"flush test data")
        await left.flush()

        result = await right.readexactly(len(b"flush test data"))
        assert result == b"flush test data"


@pytest.mark.skip_quirks(SerialQuirk.NO_BUFFER_CONTROL)
async def test_async_resume_reading_when_not_paused(serial_pair: SerialPair) -> None:
    """Test that resume_reading when not paused is a no-op."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        # resume without prior pause should be a no-op
        left.transport.resume_reading()


async def test_async_invalid_uri() -> None:
    """Test invalid URIs are rejected by public async API."""
    loop = asyncio.get_running_loop()

    with pytest.raises(ValueError, match="expected both host and port"):
        await create_serial_connection(
            loop,
            asyncio.Protocol,
            "socket://127.0.0.1",
            baudrate=115200,
        )


async def test_create_serial_connection_no_url_no_transport() -> None:
    """Test that create_serial_connection requires url or transport_cls."""
    loop = asyncio.get_running_loop()

    with pytest.raises(ValueError, match="url.*transport_cls"):
        await create_serial_connection(
            loop,
            asyncio.Protocol,
            url=None,
            baudrate=115200,
        )


async def test_async_get_modem_pins(serial_pair: SerialPair) -> None:
    """Test reading modem control bits."""
    async with serialx.async_serial_for_url(serial_pair.left, baudrate=115200) as left:
        modem_pins = await left.get_modem_pins()
        assert isinstance(modem_pins, ModemPins)
        for field in ["le", "dtr", "rts", "st", "sr", "cts", "car", "rng", "dsr"]:
            value = getattr(modem_pins, field)
            assert value in (PinState.HIGH, PinState.LOW, PinState.UNDEFINED)


async def test_async_set_modem_pins_api(serial_pair: SerialPair) -> None:
    """Test modem pin writes are accepted on all backends."""
    if SerialBackend.SER2NET in serial_pair.backends and (
        SerialQuirk.NO_DTR_DSR in serial_pair.quirks
        or SerialQuirk.NO_RTS_CTS in serial_pair.quirks
    ):
        pytest.skip("ser2net hangs setting modem pins on backends without support")

    if SerialBackend.SOCAT in serial_pair.backends and sys.platform.startswith(
        "freebsd"
    ):
        pytest.xfail("FreeBSD socat sets all pins to LOW")

    async with serialx.async_serial_for_url(serial_pair.left, baudrate=115200) as left:
        await left.set_modem_pins(dtr=True, rts=True)
        pins_high = await left.get_modem_pins()

        await left.set_modem_pins(dtr=False, rts=False)
        pins_low = await left.get_modem_pins()

        for pins in (pins_high, pins_low):
            assert isinstance(pins, ModemPins)
            for field in ["le", "dtr", "rts", "st", "sr", "cts", "car", "rng", "dsr"]:
                value = getattr(pins, field)
                assert value in (PinState.HIGH, PinState.LOW, PinState.UNDEFINED)

        if (
            pins_high.dtr is not PinState.UNDEFINED
            and pins_low.dtr is not PinState.UNDEFINED
        ):
            assert pins_high.dtr is PinState.HIGH
            assert pins_low.dtr is PinState.LOW

        if (
            pins_high.rts is not PinState.UNDEFINED
            and pins_low.rts is not PinState.UNDEFINED
        ):
            assert pins_high.rts is PinState.HIGH
            assert pins_low.rts is PinState.LOW


@pytest.mark.skip_quirks(
    SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_DTR_DSR, SerialQuirk.NO_RTS_DTR_READBACK
)
async def test_async_set_modem_pins(serial_pair: SerialPair) -> None:
    """Test setting modem control bits and verifying readback."""

    async with serialx.async_serial_for_url(serial_pair.left, baudrate=115200) as left:
        await left.set_modem_pins(dtr=True, rts=True)
        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        modem_pins = await left.get_modem_pins()
        assert modem_pins.dtr is PinState.HIGH
        assert modem_pins.rts is PinState.HIGH

        await left.set_modem_pins(dtr=False)
        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        modem_pins = await left.get_modem_pins()
        assert modem_pins.dtr is PinState.LOW
        assert modem_pins.rts is PinState.HIGH

        await left.set_modem_pins(dtr=False, rts=False)
        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        modem_pins = await left.get_modem_pins()
        assert modem_pins.dtr is PinState.LOW
        assert modem_pins.rts is PinState.LOW


@pytest.mark.skip_quirks(
    SerialQuirk.NO_BUFFER_CONTROL, SerialQuirk.NO_PAUSE_WRITING_CALLBACKS
)
async def test_async_backpressure_callbacks(serial_pair: SerialPair) -> None:
    """Test backpressure pause/resume callbacks through public async APIs."""

    output_pause_count = 0
    output_resume_count = 0

    loop = asyncio.get_running_loop()
    input_lost = loop.create_future()
    output_lost = loop.create_future()

    class Input(asyncio.Protocol):
        def data_received(self, data: bytes) -> None:
            return

        def connection_lost(self, exc: Exception | None) -> None:
            if not input_lost.done():
                input_lost.set_result(None)

    class Output(asyncio.Protocol):
        _transport: BaseSerialTransport | None = None

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, BaseSerialTransport)
            self._transport = transport

        def pause_writing(self) -> None:
            nonlocal output_pause_count
            output_pause_count += 1

        def resume_writing(self) -> None:
            nonlocal output_resume_count
            output_resume_count += 1

        def connection_lost(self, exc: Exception | None) -> None:
            if not output_lost.done():
                output_lost.set_result(None)

    in_transport, _ = await create_serial_connection(
        loop, Input, serial_pair.left, baudrate=115200
    )
    out_transport, _ = await create_serial_connection(
        loop, Output, serial_pair.right, baudrate=115200
    )
    await asyncio.sleep(0.1)

    out_transport.set_write_buffer_limits(high=1024, low=256)

    # Write enough to overflow the kernel buffer and any intermediate buffers
    # (PTY ~4KB, UNIX socket ~200KB on Linux) so that the userspace buffer
    # exceeds `high` and triggers pause_writing.
    payload = b"X" * 8192
    for _ in range(4):
        if out_transport.is_closing():
            break
        out_transport.write(payload)
        await asyncio.sleep(0)

    assert output_pause_count > 0

    assert out_transport.get_write_buffer_size() > 0
    await out_transport.flush()
    assert out_transport.get_write_buffer_size() == 0

    assert output_resume_count > 0

    out_transport.close()
    in_transport.close()
    await asyncio.gather(input_lost, output_lost)


@pytest.mark.skip_quirks(
    SerialQuirk.NO_BUFFER_CONTROL, SerialQuirk.NO_PAUSE_WRITING_CALLBACKS
)
async def test_async_backpressure_writer_removal(serial_pair: SerialPair) -> None:
    """Test that large writes with backpressure are handled correctly.

    This test catches three issue categories:
    1. AssertionError from writer not being removed when buffer empties
    2. Deadlock (via timeout) from direct writes blocking indefinitely
    3. Timing failures from writer not being added when buffering data
    Source: https://github.com/home-assistant-libs/pyserial-asyncio-fast/pull/36
    """

    TEXT = b"Hello, World!"
    COUNT = 8 * 1024
    output_resume_event = asyncio.Event()
    data_received_count = 0

    class Input(asyncio.Protocol):
        _transport: BaseSerialTransport

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, BaseSerialTransport)
            self._transport = transport

        def data_received(self, data: bytes) -> None:
            nonlocal data_received_count
            data_received_count += len(data)
            self._transport.write(data)

    class Output(asyncio.Protocol):
        _transport: BaseSerialTransport

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, BaseSerialTransport)
            self._transport = transport
            output_resume_event.set()

        def pause_writing(self) -> None:
            output_resume_event.clear()

        def resume_writing(self) -> None:
            output_resume_event.set()

    loop = asyncio.get_running_loop()

    in_transport, _ = await create_serial_connection(
        loop, Input, serial_pair.left, baudrate=115200
    )
    out_transport, _ = await create_serial_connection(
        loop, Output, serial_pair.right, baudrate=115200
    )

    await asyncio.sleep(0.1)

    try:
        for _ in range(COUNT):
            try:
                async with asyncio_timeout(10):
                    await output_resume_event.wait()
            except asyncio.TimeoutError:
                if out_transport.is_closing():
                    break
                raise

            out_transport.write(TEXT)

        async with asyncio_timeout(10):
            while out_transport.get_write_buffer_size() > 0:
                await asyncio.sleep(0.1)

        for _ in range(50):
            if data_received_count > 0:
                break
            await asyncio.sleep(0.1)

        assert data_received_count > 0
    finally:
        in_transport.abort()
        out_transport.abort()
        await in_transport.wait_closed()
        await out_transport.wait_closed()


# --- Adapter-specific tests ---
# These tests require physical adapter pairs (com0com, real hardware)
# and verify cross-port behavior that virtual backends can't emulate.


async def test_async_fast_open_close(serial_pair: SerialPair) -> None:
    """Test quickly opening and closing a port doesn't crash."""
    connection_lost_event = asyncio.Event()

    class FastCloseProtocol(asyncio.Protocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, BaseSerialTransport)
            transport.write(b"data that will be discarded on abort")
            transport.abort()

        def connection_lost(self, exc: Exception | None) -> None:
            connection_lost_event.set()

    transport, _ = await create_serial_connection(
        asyncio.get_running_loop(),
        FastCloseProtocol,
        serial_pair.left,
        baudrate=115200,
    )

    await asyncio.wait_for(connection_lost_event.wait(), timeout=5.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    assert transport.is_closing()


async def test_async_fast_open_close_with_close(serial_pair: SerialPair) -> None:
    """Test quickly opening and closing a port via close() doesn't crash."""
    connection_lost_event = asyncio.Event()

    class FastCloseProtocol(asyncio.Protocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, BaseSerialTransport)
            transport.close()

        def connection_lost(self, exc: Exception | None) -> None:
            connection_lost_event.set()

    transport, _ = await create_serial_connection(
        asyncio.get_running_loop(),
        FastCloseProtocol,
        serial_pair.left,
        baudrate=115200,
    )

    await asyncio.wait_for(connection_lost_event.wait(), timeout=5.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    assert transport.is_closing()


@pytest.mark.skipif(sys.platform == "win32", reason="CloseHandle resets modem signals")
@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS)
async def test_async_deassert_on_open(serial_pair: SerialPair) -> None:
    """Test RTS/CTS deassertion on open."""

    if serial_pair.uri_scheme in (
        "linux://",
        "darwin://",
        "posix://",
        "extended-posix://",
    ):
        pytest.skip("POSIX backends do not support deasserting pins on open")

    async with serialx.async_serial_for_url(serial_pair.left, baudrate=115200) as left:
        async with serialx.async_serial_for_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_open=PinState.HIGH,
            rts_on_close=PinState.HIGH,
        ) as right:
            await right.set_modem_pins(rts=True)
            await asyncio.sleep(serial_pair.modem_line_propagation_delay)
            assert (await left.get_modem_pins()).cts is PinState.HIGH

        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        assert (await left.get_modem_pins()).cts is PinState.HIGH

        async with serialx.async_serial_for_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_open=PinState.LOW,
            rts_on_close=PinState.HIGH,
        ) as right:
            await asyncio.sleep(serial_pair.modem_line_propagation_delay)
            assert (await left.get_modem_pins()).cts is PinState.LOW
            await right.set_modem_pins(rts=True)

        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        assert (await left.get_modem_pins()).cts is PinState.HIGH


@pytest.mark.skipif(sys.platform == "win32", reason="CloseHandle resets modem signals")
@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_DTR_DSR)
async def test_async_hang_up_on_close(serial_pair: SerialPair) -> None:
    """Test RTS/CTS hang up on close."""
    if serial_pair.uri_scheme in (
        "linux://",
        "darwin://",
        "posix://",
        "extended-posix://",
    ):
        pytest.skip("POSIX backends do not support deasserting pins on open")

    async with serialx.async_serial_for_url(serial_pair.left, baudrate=115200) as left:
        async with serialx.async_serial_for_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_close=PinState.HIGH,
            rts_on_open=PinState.HIGH,
        ) as right:
            await right.set_modem_pins(rts=True)
            await asyncio.sleep(serial_pair.modem_line_propagation_delay)
            assert (await left.get_modem_pins()).cts is PinState.HIGH

        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        assert (await left.get_modem_pins()).cts is PinState.HIGH

        async with serialx.async_serial_for_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_close=PinState.HIGH,
            rts_on_open=PinState.HIGH,
        ) as right:
            await asyncio.sleep(serial_pair.modem_line_propagation_delay)
            assert (await left.get_modem_pins()).cts is PinState.HIGH

        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        assert (await left.get_modem_pins()).cts is PinState.HIGH

        async with serialx.async_serial_for_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_close=PinState.LOW,
            rts_on_open=PinState.HIGH,
        ) as right:
            await asyncio.sleep(serial_pair.modem_line_propagation_delay)
            assert (await left.get_modem_pins()).cts is PinState.HIGH

        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        assert (await left.get_modem_pins()).cts is PinState.LOW


@pytest.mark.skipif(sys.platform == "win32", reason="CloseHandle resets modem signals")
@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_DTR_DSR)
@pytest.mark.parametrize(
    ("rtscts", "rts_on_open", "expected_state"),
    [
        (False, PinState.HIGH, PinState.HIGH),
        (False, PinState.LOW, PinState.LOW),
        (True, PinState.HIGH, PinState.HIGH),
        (True, PinState.LOW, PinState.LOW),
    ],
)
async def test_async_deassert_on_open_with_rtscts(
    serial_pair: SerialPair,
    rtscts: bool,
    rts_on_open: PinState,
    expected_state: PinState,
) -> None:
    """Test interaction of rts_on_open with rtscts."""

    if serial_pair.uri_scheme in (
        "linux://",
        "darwin://",
        "posix://",
        "extended-posix://",
    ):
        pytest.skip("POSIX backends do not support deasserting pins on open")

    async with serialx.async_serial_for_url(serial_pair.left, baudrate=115200) as left:
        async with serialx.async_serial_for_url(
            serial_pair.right,
            baudrate=115200,
            rtscts=False,
            rts_on_open=PinState.HIGH,
            rts_on_close=PinState.HIGH,
        ) as right:
            await right.set_modem_pins(rts=True)
            await asyncio.sleep(serial_pair.modem_line_propagation_delay)
            assert (await left.get_modem_pins()).cts is PinState.HIGH

        await asyncio.sleep(serial_pair.modem_line_propagation_delay)
        assert (await left.get_modem_pins()).cts is PinState.HIGH

        async with serialx.async_serial_for_url(
            serial_pair.right,
            baudrate=115200,
            rtscts=rtscts,
            rts_on_open=rts_on_open,
        ) as right:
            await asyncio.sleep(serial_pair.modem_line_propagation_delay)
            assert (await left.get_modem_pins()).cts is expected_state


@pytest.mark.skip_quirks(SerialQuirk.NO_EXCLUSIVITY)
async def test_async_exclusive(serial_pair: SerialPair) -> None:
    """Test that exclusive setting is respected for async connections."""
    async with serialx.async_serial_for_url(
        serial_pair.left, baudrate=115200, exclusive=True
    ) as left:
        assert left.exclusive is True

        with pytest.raises(OSError):
            async with serialx.async_serial_for_url(
                serial_pair.left, baudrate=115200, exclusive=True
            ):
                pass


@pytest.mark.skip_quirks(SerialQuirk.NO_EXCLUSIVITY)
async def test_async_exclusive_disabled(serial_pair: SerialPair) -> None:
    """Test that non-exclusive mode allows multiple opens."""
    if sys.platform == "win32":
        pytest.skip("Windows does not support shared access")

    if SerialBackend.SER2NET in serial_pair.backends:
        pytest.skip("ser2net only allows one connection per port")

    async with serialx.async_serial_for_url(
        serial_pair.left, baudrate=115200, exclusive=False
    ) as left1:
        assert left1.exclusive is False

        async with serialx.async_serial_for_url(
            serial_pair.left, baudrate=115200, exclusive=False
        ) as left2:
            assert left2.exclusive is False

            left1.write_nowait(b"hello")
            left2.write_nowait(b"world")


async def test_async_connect_nonexistent_port() -> None:
    """Test that a failed connect still leaves a closed transport."""
    if sys.platform == "emscripten":
        pytest.skip("No POSIX/Windows-style device paths under Pyodide")

    loop = asyncio.get_running_loop()
    path = "COM25" if sys.platform == "win32" else "/dev/this_port_does_not_exist"
    _, transport_cls = await loop.run_in_executor(None, get_serial_classes, path)
    transport = transport_cls(loop=loop, protocol=asyncio.Protocol())

    with pytest.raises(OSError):
        await asyncio.wait_for(
            transport.connect(path=path, baudrate=115200),
            timeout=5.0,
        )

    # Closing and `wait_closed` are both idempotent
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    transport.close()
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    assert transport.is_closing()


async def test_async_connect_cancel(serial_pair: SerialPair) -> None:
    """Test that cancelling connect still leaves a closed transport."""
    loop = asyncio.get_running_loop()
    _, transport_cls = await loop.run_in_executor(
        None, get_serial_classes, serial_pair.left
    )

    protocol = asyncio.Protocol()
    transport = transport_cls(loop=loop, protocol=protocol)

    connect_task = asyncio.create_task(
        transport.connect(path=serial_pair.left, baudrate=115200)
    )
    await asyncio.sleep(0)
    connect_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(connect_task, timeout=5.0)

    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    transport.close()
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    assert transport.is_closing()


async def test_async_close_before_connect_wait_closed(serial_pair: SerialPair) -> None:
    """Test close-before-connect still resolves wait_closed."""
    loop = asyncio.get_running_loop()
    _serial_cls, transport_cls = get_serial_classes(serial_pair.left)

    transport = transport_cls(loop=loop, protocol=asyncio.Protocol())
    transport.close()

    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    transport.close()
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    assert transport.is_closing()


async def test_async_abort_before_connect_wait_closed(serial_pair: SerialPair) -> None:
    """Test abort-before-connect still resolves wait_closed."""
    loop = asyncio.get_running_loop()
    _serial_cls, transport_cls = get_serial_classes(serial_pair.left)

    transport = transport_cls(loop=loop, protocol=asyncio.Protocol())
    transport.abort()

    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    transport.abort()
    await asyncio.wait_for(transport.wait_closed(), timeout=5.0)
    assert transport.is_closing()


async def test_async_wait_closed_multiple_waiters_close(
    serial_pair: SerialPair,
) -> None:
    """Test multiple wait_closed waiters are released together on close."""
    loop = asyncio.get_running_loop()
    connection_lost_event = asyncio.Event()

    class WaitersProtocol(asyncio.Protocol):
        def connection_lost(self, exc: Exception | None) -> None:
            connection_lost_event.set()

    transport, _ = await create_serial_connection(
        loop,
        WaitersProtocol,
        serial_pair.left,
        baudrate=115200,
    )

    wait_closed_1 = asyncio.create_task(transport.wait_closed())
    wait_closed_2 = asyncio.create_task(transport.wait_closed())

    await asyncio.sleep(0)
    transport.close()

    await asyncio.wait_for(connection_lost_event.wait(), timeout=5.0)
    await asyncio.wait_for(asyncio.gather(wait_closed_1, wait_closed_2), timeout=5.0)
    assert transport.is_closing()


async def test_async_wait_closed_multiple_waiters_abort(
    serial_pair: SerialPair,
) -> None:
    """Test multiple wait_closed waiters are released together on abort."""
    loop = asyncio.get_running_loop()
    connection_lost_event = asyncio.Event()

    class WaitersProtocol(asyncio.Protocol):
        def connection_lost(self, exc: Exception | None) -> None:
            connection_lost_event.set()

    transport, _ = await create_serial_connection(
        loop,
        WaitersProtocol,
        serial_pair.left,
        baudrate=115200,
    )

    wait_closed_1 = asyncio.create_task(transport.wait_closed())
    wait_closed_2 = asyncio.create_task(transport.wait_closed())

    await asyncio.sleep(0)
    transport.abort()

    await asyncio.wait_for(connection_lost_event.wait(), timeout=5.0)
    await asyncio.wait_for(asyncio.gather(wait_closed_1, wait_closed_2), timeout=5.0)
    assert transport.is_closing()


async def test_async_unplug_raises(serial_pair: SerialPair) -> None:
    """Each operation on an abruptly-unplugged port raises rather than EOFing."""
    if serial_pair.unplug_left_abrupt is None:
        pytest.skip("backend cannot simulate an abrupt disconnect")

    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        right.write_nowait(b"ping\n")
        await right.drain()
        assert await left.readline() == b"ping\n"

        serial_pair.unplug_left_abrupt()

        with pytest.raises(OSError):
            await left.read(1)

        with pytest.raises(OSError):
            left.write_nowait(b"x")

        with pytest.raises(OSError):
            await left.drain()

        with pytest.raises(OSError):
            await left.get_modem_pins()

        with pytest.raises(OSError):
            await left.set_modem_pins(rts=True)


async def test_async_unplug_raises_on_streamreader_readline(
    serial_pair: SerialPair,
) -> None:
    """`reader.readline()` after an abrupt unplug must raise, not return b''."""
    if serial_pair.unplug_left_abrupt is None:
        pytest.skip("backend cannot simulate an abrupt disconnect")

    reader, writer = await open_serial_connection(serial_pair.left, baudrate=115200)
    try:
        async with serialx.async_serial_for_url(
            serial_pair.right, baudrate=115200
        ) as right:
            right.write_nowait(b"ping\n")
            await right.drain()
            assert await reader.readline() == b"ping\n"

            serial_pair.unplug_left_abrupt()

            with pytest.raises(OSError):
                await reader.readline()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


@pytest.mark.skip_quirks(SerialQuirk.NO_GRACEFUL_PEER_CLOSE)
async def test_async_graceful_peer_close_does_not_raise(
    serial_pair: SerialPair,
) -> None:
    """A clean peer FIN reads as EOF and wait_closed() must not raise."""
    if serial_pair.unplug_left_graceful is None:
        pytest.skip("backend cannot simulate a graceful peer close")

    reader, writer = await open_serial_connection(serial_pair.left, baudrate=115200)

    try:
        serial_pair.unplug_left_graceful()
        assert await reader.read() == b""

        with pytest.raises(OSError):
            writer.write(b"foo")
    finally:
        writer.close()
        await writer.wait_closed()
