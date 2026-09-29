"""Sync transport tests."""

from asyncio import IncompleteReadError
import logging
import os
import sys
import time

import pytest

if sys.platform == "emscripten":
    pytest.skip(
        "Pyodide has no sync serial backend (PyodideSerial is stubs)",
        allow_module_level=True,
    )

from serialx import ModemPins, Parity, PinState, Serial, StopBits, serial_for_url
from tests.common import (
    SerialBackend,
    SerialPair,
    SerialQuirk,
    check_fd_leaks,
    measure_time,
)

LOGGER = logging.getLogger(__name__)


def test_sync_all_bytes(serial_pair: SerialPair) -> None:
    """Test that all bytes 0-255 can be transmitted."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        data = bytes(range(256))
        left.write(data)
        assert right.readexactly(len(data)) == data


def test_sync_serial_for_url_factory(serial_pair: SerialPair) -> None:
    """Test the top-level serial_for_url factory on all backends."""
    with (
        serial_for_url(serial_pair.left, baudrate=115200) as left,
        serial_for_url(serial_pair.right, baudrate=115200) as right,
    ):
        data = b"serial_for_url"
        left.write(data)
        assert right.readexactly(len(data)) == data


def test_sync_segmented_binary_data(serial_pair: SerialPair) -> None:
    """Test binary data sent in segments."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        segment_size = 16
        data = bytes(range(256))

        for i in range(0, 256, segment_size):
            segment = data[i : i + segment_size]
            left.write(segment)
            assert right.readexactly(len(segment)) == segment


@pytest.mark.parametrize("size", [1, 16, 64, 256, 512, 1024])
def test_sync_binary_payload_sizes(serial_pair: SerialPair, size: int) -> None:
    """Test various binary payload sizes."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        data = bytes([i % 256 for i in range(size)])
        left.write(data)
        assert right.readexactly(len(data)) == data


def test_sync_write_bytearray(serial_pair: SerialPair) -> None:
    """Test writing bytearray data."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        data = bytearray(b"hello bytearray")
        left.write(data)
        assert right.readexactly(len(data)) == b"hello bytearray"


def test_sync_write_empty(serial_pair: SerialPair) -> None:
    """Test writing empty data is a no-op."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        left.write(b"")
        left.write(b"after_empty")
        assert right.readexactly(len(b"after_empty")) == b"after_empty"


def test_sync_null_bytes(serial_pair: SerialPair) -> None:
    """Test that null bytes (0x00) can be transmitted."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        null_data = b"\x00" * 64
        left.write(null_data)
        assert right.readexactly(len(null_data)) == null_data


def test_sync_readline(serial_pair: SerialPair) -> None:
    """Test readline returns successive newline-terminated lines."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=1.0) as right,
    ):
        left.write(b"alpha\nbeta\ngamma\n")
        assert right.readline() == b"alpha\n"
        assert right.readline() == b"beta\n"
        assert right.readline() == b"gamma\n"


def test_sync_readline_returns_partial_on_timeout(serial_pair: SerialPair) -> None:
    """Test readline returns the partial line if no newline arrives before timeout."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=0.5) as right,
    ):
        left.write(b"no newline here")

        with measure_time() as elapsed:
            result = right.readline()

        assert result == b"no newline here"
        assert elapsed() == pytest.approx(0.5, abs=0.2)


def test_sync_writelines(serial_pair: SerialPair) -> None:
    """Test writelines writes an iterable of buffers in order."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        left.writelines([b"foo", b"bar", b"baz"])
        assert right.readexactly(9) == b"foobarbaz"


def test_sync_overlapping_read_write(serial_pair: SerialPair) -> None:
    """Test that read and write can overlap, data is buffered."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        data = bytes(range(256))
        read = b""

        left.write(data[:100])
        read += right.readexactly(10)
        left.write(data[100:150])
        read += right.readexactly(10)
        left.write(data[150:])
        read += right.readexactly(10)
        read += right.readexactly(256 - 30)

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
def test_sync_random_large(
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

    with (
        Serial.from_url(serial_pair.left, baudrate=baudrate) as left,
        Serial.from_url(serial_pair.right, baudrate=baudrate) as right,
    ):
        data = os.urandom(chunk_size)
        left.write(data)
        assert right.readexactly(chunk_size) == data


@pytest.mark.parametrize("iterations", [16, 32, 64])
def test_sync_repeated_write_read_cycles(
    serial_pair: SerialPair, iterations: int
) -> None:
    """Test repeated write/read cycles."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        data = bytes(range(256))

        for _ in range(iterations):
            left.write(data)
            assert right.readexactly(len(data)) == data


def test_sync_buffered_writes_then_read(serial_pair: SerialPair) -> None:
    """Test multiple writes followed by a single read."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        chunk = bytes(range(256))
        iterations = 4

        for _ in range(iterations):
            left.write(chunk)

        assert right.readexactly(len(chunk) * iterations) == chunk * iterations


@pytest.mark.parametrize("payload_size", [1024, 2048])
def test_sync_large_payload(serial_pair: SerialPair, payload_size: int) -> None:
    """Test large payload transmission."""
    if sys.platform == "darwin" and (
        serial_pair.uri_scheme in ("posix://", "extended-posix://")
        or SerialBackend.SER2NET in serial_pair.backends
    ):
        pytest.xfail("macOS termios lacks constants above B230400")

    with (
        Serial.from_url(serial_pair.left, baudrate=921600) as left,
        Serial.from_url(serial_pair.right, baudrate=921600) as right,
    ):
        data = bytes([i % 256 for i in range(payload_size)])
        left.write(data)
        assert right.readexactly(len(data)) == data


def test_sync_rapid_small_writes(serial_pair: SerialPair) -> None:
    """Test rapid succession of small writes."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        iterations = 256
        received = bytearray()

        for i in range(iterations):
            left.write(bytes([i % 256]))
            received.extend(right.readexactly(1))

        assert bytes(received) == bytes([i % 256 for i in range(iterations)])


@pytest.mark.parametrize("baudrate,iterations", [(9600, 4), (115200, 32), (921600, 32)])
def test_sync_sustained_throughput(
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

    with (
        Serial.from_url(serial_pair.left, baudrate=baudrate) as left,
        Serial.from_url(serial_pair.right, baudrate=baudrate) as right,
    ):
        chunk = os.urandom(1024)
        for _ in range(iterations):
            left.write(chunk)
            assert right.readexactly(len(chunk)) == chunk


# --- Configuration ---


@pytest.mark.parametrize(
    "baudrate", [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
)
def test_sync_valid_baudrates(serial_pair: SerialPair, baudrate: int) -> None:
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

    with Serial.from_url(serial_pair.left, baudrate=baudrate) as serial:
        assert serial.baudrate == baudrate
        serial.write(b"test")


def test_sync_nonstandard_baudrate(serial_pair: SerialPair) -> None:
    """Test that a non-standard baudrate (no termios constant) is accepted."""
    if serial_pair.uri_scheme in ("posix://", "extended-posix://"):
        pytest.skip("Base POSIX backends only support standard baudrates")

    if sys.platform == "darwin" and SerialBackend.SER2NET in serial_pair.backends:
        pytest.xfail("macOS termios lacks constants above B230400")

    with (
        Serial.from_url(serial_pair.left, baudrate=200000) as left,
        Serial.from_url(serial_pair.right, baudrate=200000) as right,
    ):
        assert left.baudrate == 200000
        left.write(b"test")
        assert right.readexactly(4) == b"test"


@pytest.mark.parametrize(
    "parity", [Parity.NONE, Parity.ODD, Parity.EVEN, Parity.MARK, Parity.SPACE]
)
def test_sync_valid_parity(serial_pair: SerialPair, parity: Parity) -> None:
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

    with Serial.from_url(serial_pair.left, baudrate=115200, parity=parity) as serial:
        assert serial.parity == parity
        serial.write(b"test")


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
def test_sync_valid_stopbits(
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

    with Serial.from_url(
        serial_pair.left, baudrate=115200, stopbits=stopbits
    ) as serial:
        assert serial.stopbits == expected
        serial.write(b"test")


@pytest.mark.parametrize("byte_size", [5, 6, 7, 8])
def test_sync_valid_byte_size(serial_pair: SerialPair, byte_size: int) -> None:
    """Test that valid byte sizes are accepted."""
    with Serial.from_url(
        serial_pair.left, baudrate=115200, byte_size=byte_size
    ) as serial:
        assert serial.byte_size == byte_size
        serial.write(b"test")


def test_sync_invalid_byte_size(serial_pair: SerialPair) -> None:
    """Test that an invalid byte size is rejected."""
    if SerialBackend.SOCKET in serial_pair.backends:
        pytest.skip("socket transport does not validate serial settings")

    with pytest.raises(Exception):
        with Serial.from_url(serial_pair.left, baudrate=115200, byte_size=123):
            pass


@pytest.mark.parametrize("xonxoff", [True, False])
def test_sync_xonxoff_setting(serial_pair: SerialPair, xonxoff: bool) -> None:
    """Test that xonxoff setting is accepted."""
    with Serial.from_url(serial_pair.left, baudrate=115200, xonxoff=xonxoff) as serial:
        serial.write(b"test")


@pytest.mark.parametrize("rtscts", [True, False])
def test_sync_rtscts_setting(serial_pair: SerialPair, rtscts: bool) -> None:
    """Test that rtscts setting is accepted."""
    if rtscts and serial_pair.uri_scheme == "posix://":
        pytest.xfail("Strict POSIX backend does not support RTS/CTS flow control")

    if rtscts and SerialBackend.ESPHOME_HOST in serial_pair.backends:
        pytest.xfail("ESPHome host does not support RTS/CTS flow control")

    # Open both sides: on com0com, opening right asserts DTR which raises CTS on left
    with Serial.from_url(serial_pair.right, baudrate=115200) as right:
        with Serial.from_url(serial_pair.left, baudrate=115200, rtscts=rtscts) as left:
            left.write(b"test")

            # ser2net closes both sides concurrently, so data still in flight when the
            # right side drops RTS stays held by CTS until `closing_wait` expires
            assert right.readexactly(4) == b"test"


def test_sync_reconfigure_flow_control(serial_pair: SerialPair) -> None:
    """Test that flow control can be switched on an open port."""
    if serial_pair.uri_scheme == "posix://":
        pytest.xfail("Strict POSIX backend does not support RTS/CTS flow control")

    if SerialBackend.ESPHOME_HOST in serial_pair.backends:
        pytest.xfail("ESPHome host does not support RTS/CTS flow control")

    # Open both sides: on com0com, opening right asserts DTR which raises CTS on left
    with Serial.from_url(serial_pair.right, baudrate=9600) as right:
        with Serial.from_url(serial_pair.left, baudrate=9600) as left:
            left.write(b"before")
            assert right.readexactly(6) == b"before"

            left.reconfigure_port(rtscts=True, xonxoff=False, dsrdtr=False)

            left.write(b"after")
            assert right.readexactly(5) == b"after"


@pytest.mark.skip(
    reason="held output stalls the kernel close and wedges the adapter; needs the bounded flush"
)
@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_WRITE_TIMEOUT)
def test_sync_reconfigure_rtscts_holds_writes(serial_pair: SerialPair) -> None:
    """Test that enabling RTS/CTS on an open port makes a held CTS line stall writes."""
    with Serial.from_url(serial_pair.right, baudrate=115200) as right:
        right.set_modem_pins(rts=False)
        time.sleep(serial_pair.modem_line_propagation_delay)

        with Serial.from_url(
            serial_pair.left, baudrate=115200, write_timeout=0.5
        ) as left:
            left.write(b"x" * 1024)

            left.reconfigure_port(rtscts=True)

            with measure_time() as elapsed:
                with pytest.raises(TimeoutError):
                    left.write(b"x" * 1024)

            assert 0.3 <= elapsed() <= 1.2

            # Let the held bytes drain, or closing the port waits on them
            right.set_modem_pins(rts=True)


@pytest.mark.skip_quirks(SerialQuirk.NO_EXCLUSIVITY)
def test_sync_exclusive(serial_pair: SerialPair) -> None:
    """Test that exclusive setting is respected."""
    if SerialBackend.ESPHOME_HOST in serial_pair.backends:
        pytest.skip("network-based transports do not enforce OS-level exclusive access")

    with Serial.from_url(serial_pair.left, baudrate=115200, exclusive=True) as serial:
        assert serial.exclusive is True

        with pytest.raises(OSError):
            with Serial.from_url(serial_pair.left, baudrate=115200, exclusive=True):
                pass


@pytest.mark.skip_quirks(SerialQuirk.NO_EXCLUSIVITY)
def test_sync_exclusive_open_failure_does_not_leak(serial_pair: SerialPair) -> None:
    """A failed exclusive open must not leak the fd it acquired before locking."""
    with Serial.from_url(serial_pair.left, baudrate=115200, exclusive=True):
        # We check explicitly to ensure the gc doesn't hide a leak
        with check_fd_leaks():
            blocked = Serial.from_url(serial_pair.left, baudrate=115200, exclusive=True)
            with pytest.raises(OSError):
                blocked.open()


def test_sync_exclusive_disabled(serial_pair: SerialPair) -> None:
    """Test that non-exclusive mode allows multiple opens."""
    if sys.platform == "win32":
        pytest.skip("Windows does not support shared access")

    if SerialBackend.SER2NET in serial_pair.backends:
        pytest.skip("ser2net only allows one connection per port")

    if SerialBackend.ESPHOME_HOST in serial_pair.backends:
        pytest.skip("ESPHome Host only allows one connection per port")

    with Serial.from_url(serial_pair.left, baudrate=115200, exclusive=False) as serial1:
        assert serial1.exclusive is False

        with Serial.from_url(
            serial_pair.left, baudrate=115200, exclusive=False
        ) as serial2:
            assert serial2.exclusive is False
            serial2.write(b"test")


# --- Lifecycle ---


def test_sync_close_is_idempotent(serial_pair: SerialPair) -> None:
    """Test closing multiple times is safe."""
    serial = Serial.from_url(serial_pair.left, baudrate=115200)
    serial.open()
    serial.close()

    # Second close should be no-op
    serial.close()


def test_sync_context_manager_multiple_times(serial_pair: SerialPair) -> None:
    """Test that context manager can be used multiple times."""
    left = Serial.from_url(serial_pair.left, baudrate=115200)
    right = Serial.from_url(serial_pair.right, baudrate=115200)

    with left, right:
        left.write(b"test1")
        assert right.readexactly(5) == b"test1"

    with left, right:
        left.write(b"test2")
        assert right.readexactly(5) == b"test2"


@pytest.mark.skip_quirks(SerialQuirk.NO_RESET_READ_BUFFER)
def test_sync_open_close_cycles(serial_pair: SerialPair) -> None:
    """Test multiple open/close cycles."""
    left = Serial.from_url(serial_pair.left, baudrate=115200)
    right = Serial.from_url(serial_pair.right, baudrate=115200)

    for i in range(1, 4):
        left.open()
        right.open()

        chunk = str(i).encode("ascii")
        left.write(chunk)
        assert right.readexactly(1) == chunk

        left.close()
        right.close()


def test_sync_flush_after_write(serial_pair: SerialPair) -> None:
    """Test flushing after write operation."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        left.flush()

        data = b"Test flush operation"
        left.write(data)
        left.flush()

        assert right.readexactly(len(data)) == data


def test_sync_multiple_flush_calls(serial_pair: SerialPair) -> None:
    """Test multiple consecutive flush calls."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        data = b""

        for i in range(5):
            chunk = f"Test data {i}".encode("ascii")
            data += chunk
            left.write(chunk)
            left.flush()

        assert right.readexactly(len(data)) == data


# --- Modem pins ---


def test_sync_get_modem_pins(serial_pair: SerialPair) -> None:
    """Test reading modem control bits."""
    with Serial.from_url(serial_pair.left, baudrate=115200) as serial:
        modem_pins = serial.get_modem_pins()
        assert isinstance(modem_pins, ModemPins)

        for field in ["le", "dtr", "rts", "st", "sr", "cts", "car", "rng", "dsr"]:
            value = getattr(modem_pins, field)
            assert value in (PinState.HIGH, PinState.LOW, PinState.UNDEFINED)


def test_sync_set_modem_pins_api(serial_pair: SerialPair) -> None:
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

    with Serial.from_url(serial_pair.left, baudrate=115200) as serial:
        serial.set_modem_pins(dtr=True, rts=True)
        pins_high = serial.get_modem_pins()

        serial.set_modem_pins(dtr=False, rts=False)
        pins_low = serial.get_modem_pins()

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
def test_sync_set_modem_pins(serial_pair: SerialPair) -> None:
    """Test setting modem control bits and verifying readback."""

    with Serial.from_url(serial_pair.left, baudrate=115200) as serial:
        serial.set_modem_pins(dtr=True, rts=True)
        modem_pins = serial.get_modem_pins()
        assert modem_pins.dtr is PinState.HIGH
        assert modem_pins.rts is PinState.HIGH

        serial.set_modem_pins(dtr=False)
        modem_pins = serial.get_modem_pins()
        assert modem_pins.dtr is PinState.LOW
        assert modem_pins.rts is PinState.HIGH

        serial.set_modem_pins(dtr=False, rts=False)
        modem_pins = serial.get_modem_pins()
        assert modem_pins.dtr is PinState.LOW
        assert modem_pins.rts is PinState.LOW


@pytest.mark.skip_quirks(
    SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_DTR_DSR, SerialQuirk.NO_RTS_DTR_READBACK
)
def test_sync_deprecated_dtr_property(serial_pair: SerialPair) -> None:
    """Test DTR property (deprecated alias)."""

    with Serial.from_url(serial_pair.left, baudrate=115200) as serial:
        serial.dtr = True
        assert serial.dtr is True

        serial.dtr = False
        assert serial.dtr is False


@pytest.mark.skip_quirks(
    SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_DTR_DSR, SerialQuirk.NO_RTS_DTR_READBACK
)
def test_sync_deprecated_rts_property(serial_pair: SerialPair) -> None:
    """Test RTS property (deprecated alias)."""

    with Serial.from_url(serial_pair.left, baudrate=115200) as serial:
        serial.rts = True
        assert serial.rts is True

        serial.rts = False
        assert serial.rts is False


# --- Timeouts ---


def test_sync_read_timeout(serial_pair: SerialPair) -> None:
    """Test that reading with a timeout returns 0 bytes after the timeout."""
    with Serial.from_url(serial_pair.left, baudrate=115200, read_timeout=0.1) as serial:
        assert serial.read_timeout == 0.1

        with measure_time() as elapsed:
            result = serial.read(10)

        assert len(result) == 0
        assert elapsed() >= 0.09


def test_sync_read_timeout_with_partial_data(serial_pair: SerialPair) -> None:
    """Test that reading with a timeout returns available data immediately."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200, read_timeout=1.0) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=1.0) as right,
    ):
        left.write(b"hello")

        with measure_time() as elapsed:
            result = right.read(5)

        assert result == b"hello"
        assert elapsed() < 0.2


def test_sync_short_timeout_preserves_buffered_bytes(serial_pair: SerialPair) -> None:
    """A read whose timeout expires before the inter byte timeout must still succeed."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            # On Win32 this pushes the COMMTIMEOUTS gap timer past the caller
            # deadline, forcing the cancel path. Ignored / harmless elsewhere.
            inter_byte_timeout=1.0,
        ) as right,
    ):
        left.write(b"hello")
        left.flush()

        buf = bytearray(4096)
        n = right.readinto(buf, timeout=0.2)

        assert n == 5
        assert bytes(buf[:n]) == b"hello"


def test_sync_zero_timeout_returns_buffered_data(serial_pair: SerialPair) -> None:
    """Reading with `timeout=0` should return buffered data immediately."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            inter_byte_timeout=1.0,
        ) as right,
    ):
        left.write(b"hello")
        left.flush()
        time.sleep(0.1)

        buf = bytearray(4096)
        with measure_time() as elapsed:
            n = right.readinto(buf, timeout=0)

        assert n == 5
        assert bytes(buf[:n]) == b"hello"
        assert elapsed() < 0.05


def test_sync_zero_timeout_no_data(serial_pair: SerialPair) -> None:
    """Reading with `timeout=0` and nothing buffered should return nothing immediately."""
    with Serial.from_url(serial_pair.right, baudrate=115200) as right:
        buf = bytearray(4096)
        with measure_time() as elapsed:
            n = right.readinto(buf, timeout=0)

        assert n == 0
        assert elapsed() < 0.05


def test_sync_buffered_bytes_recovered_after_short_timeout(
    serial_pair: SerialPair,
) -> None:
    """Bytes are deferred, not lost, after a short timeout."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            inter_byte_timeout=1.0,
        ) as right,
    ):
        left.write(b"hello")
        left.flush()
        time.sleep(0.1)

        buf = bytearray(4096)
        view = memoryview(buf)
        n1 = right.readinto(view, timeout=0.05)
        n2 = right.readinto(view[n1:], timeout=1.0)

        assert n1 + n2 == 5
        assert bytes(buf[: n1 + n2]) == b"hello"


def test_sync_inter_byte_timeout_bounds_trailing_latency(
    serial_pair: SerialPair,
) -> None:
    """`inter_byte_timeout` sets the trailing-chunk latency."""
    # Only native Win32 ports honor `inter_byte_timeout`, POSIX reads are non-blocking
    if sys.platform != "win32" or serial_pair.backends != (SerialBackend.ADAPTER,):
        pytest.skip("Only native Win32 ports support `inter_byte_timeout`")

    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            inter_byte_timeout=0.1,
        ) as right,
    ):
        left.write(b"hello")
        left.flush()

        buf = bytearray(4096)
        with measure_time() as elapsed:
            n = right.readinto(buf, timeout=2.0)

        assert n == 5
        assert bytes(buf[:n]) == b"hello"
        assert elapsed() == pytest.approx(0.1, abs=0.08)


@pytest.mark.skip_quirks(SerialQuirk.NO_WRITE_TIMEOUT)
def test_sync_write_short_timeout_partial_delivery(serial_pair: SerialPair) -> None:
    """A `write_timeout` firing should still deliver the bytes the kernel pushed."""
    with (
        Serial.from_url(
            serial_pair.left,
            baudrate=9600,
            write_timeout=0.1,
        ) as left,
        Serial.from_url(serial_pair.right, baudrate=9600) as right,
    ):
        data = b"x" * 1024

        with measure_time() as elapsed:
            with pytest.raises(TimeoutError):
                for _ in range(1000):
                    left.write(data)

        assert elapsed() == pytest.approx(0.1, abs=0.2)

        buf = bytearray(2 * 1024 * 1024)
        view = memoryview(buf)
        received = 0
        drain_deadline = time.monotonic() + 2.0
        while time.monotonic() < drain_deadline:
            n = right.readinto(view[received:], timeout=0.3)
            if n == 0:
                break
            received += n

        assert received > 0


def test_sync_readexactly_partial_timeout(serial_pair: SerialPair) -> None:
    """Test that readexactly(10) with only 5 bytes raises IncompleteReadError."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200, read_timeout=0.5) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=0.5) as right,
    ):
        left.write(b"hello")

        with measure_time() as elapsed:
            with pytest.raises(IncompleteReadError) as exc_info:
                right.readexactly(10)

        assert exc_info.value.partial == b"hello"
        assert elapsed() == pytest.approx(0.5, abs=0.1)


def test_sync_read_until(serial_pair: SerialPair) -> None:
    """Test that read_until returns data up to and including the delimiter."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=1.0) as right,
    ):
        left.write(b"hello\nworld\n")

        assert right.read_until(b"\n") == b"hello\n"
        assert right.read_until(b"\n") == b"world\n"


def test_sync_read_until_repeated_separator(serial_pair: SerialPair) -> None:
    """Test read_until with consecutive separators that don't align as framing."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=1.0) as right,
    ):
        left.write(b"foo|||||bar||tail||")
        assert right.read_until(b"||") == b"foo||"
        assert right.read_until(b"||") == b"||"
        assert right.read_until(b"||") == b"|bar||"
        assert right.read_until(b"||") == b"tail||"


def test_sync_readexactly_total_timeout(serial_pair: SerialPair) -> None:
    """Test that readexactly bounds total wall-clock time, not per-read time."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=0.5) as right,
    ):
        # Write partial data so readexactly loops: first readinto returns 5 bytes,
        # second readinto blocks until timeout expires
        left.write(b"hello")

        with measure_time() as elapsed:
            with pytest.raises(IncompleteReadError) as exc_info:
                right.readexactly(10)

        assert exc_info.value.partial == b"hello"
        assert elapsed() == pytest.approx(0.5, abs=0.15)


def test_sync_read_until_total_timeout(serial_pair: SerialPair) -> None:
    """Test that read_until bounds total wall-clock time across many 1-byte reads."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=0.5) as right,
    ):
        # Write data without a newline; read_until calls readexactly(1) per byte,
        # then blocks on the next one. Total time must still be ~0.5s.
        left.write(b"no newline here")

        with measure_time() as elapsed:
            with pytest.raises(IncompleteReadError):
                right.read_until(b"\n")

        assert elapsed() == pytest.approx(0.5, abs=0.15)


@pytest.mark.skip_quirks(SerialQuirk.NO_WRITE_TIMEOUT)
@pytest.mark.xfail(
    sys.platform.startswith("freebsd"),
    reason="FreeBSD ucom driver does not enforce write buffer limits",
)
def test_sync_write_timeout(serial_pair: SerialPair) -> None:
    """Test that write timeout works when buffer is full."""

    with (
        Serial.from_url(serial_pair.left, baudrate=9600, write_timeout=0.1) as left,
        Serial.from_url(serial_pair.right, baudrate=9600) as _right,
    ):
        data = b"x" * 1024

        with pytest.raises(TimeoutError):
            for _ in range(1000):
                left.write(data)


# --- Buffer inspection and reset ---


@pytest.mark.skip_quirks(SerialQuirk.NO_NUM_UNREAD_BYTES)
def test_sync_num_unread_bytes(serial_pair: SerialPair) -> None:
    """Test that num_unread_bytes reflects pending data."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        assert right.num_unread_bytes() == 0

        left.write(b"hello")
        left.flush()
        time.sleep(0.05)

        assert right.num_unread_bytes() == 5

        right.readexactly(5)
        assert right.num_unread_bytes() == 0


def test_sync_num_unwritten_bytes(serial_pair: SerialPair) -> None:
    """Test that num_unwritten_bytes returns an integer."""
    with Serial.from_url(serial_pair.left, baudrate=115200) as left:
        # After flush, unwritten bytes should be zero
        left.flush()
        assert left.num_unwritten_bytes() == 0


@pytest.mark.skip_quirks(SerialQuirk.NO_RESET_READ_BUFFER)
def test_sync_reset_read_buffer(serial_pair: SerialPair) -> None:
    """Test that reset_read_buffer discards pending input."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, read_timeout=0.2) as right,
    ):
        left.write(b"discard me")
        left.flush()
        time.sleep(0.05)

        assert right.num_unread_bytes() >= 0
        right.reset_read_buffer()
        assert right.num_unread_bytes() == 0

        # Confirm read returns nothing after flush
        assert right.read(1024) == b""


@pytest.mark.skip_quirks(SerialQuirk.NO_RESET_WRITE_BUFFER)
def test_sync_reset_write_buffer(serial_pair: SerialPair) -> None:
    """Test that reset_write_buffer discards pending output."""
    with Serial.from_url(serial_pair.left, baudrate=9600, write_timeout=0) as left:
        left.write(b"x" * 1024)

        assert left.num_unwritten_bytes() > 0
        left.reset_write_buffer()
        assert left.num_unwritten_bytes() == 0


def test_sync_buffer_methods(serial_pair: SerialPair) -> None:
    """Test that buffer inspection and reset methods."""
    with Serial.from_url(serial_pair.left, baudrate=115200) as left:
        assert left.num_unread_bytes() >= 0
        assert left.num_unwritten_bytes() >= 0

        left.reset_read_buffer()
        assert left.num_unread_bytes() == 0

        left.reset_write_buffer()
        assert left.num_unwritten_bytes() == 0


@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS)
def test_rts_cts(serial_pair: SerialPair) -> None:
    """Test that RTS on one side controls CTS on the other (null modem)."""

    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        left.set_modem_pins(rts=True)
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert right.get_modem_pins().cts is PinState.HIGH

        right.set_modem_pins(rts=True)
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.HIGH

        left.set_modem_pins(rts=False)
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert right.get_modem_pins().cts is PinState.LOW

        right.set_modem_pins(rts=False)
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.LOW


@pytest.mark.skip_quirks(SerialQuirk.NO_DTR_DSR)
def test_dtr_dsr(serial_pair: SerialPair) -> None:
    """Test that DTR on one side controls DSR and CD on the other (null modem)."""

    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        left.set_modem_pins(dtr=True)
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert right.get_modem_pins().dsr is PinState.HIGH

        right.set_modem_pins(dtr=True)
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().dsr is PinState.HIGH

        left.set_modem_pins(dtr=False)
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert right.get_modem_pins().dsr is PinState.LOW

        right.set_modem_pins(dtr=False)
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().dsr is PinState.LOW


@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS)
def test_deprecated_null_modem_pins(serial_pair: SerialPair) -> None:
    """Test null modem cross-port behavior via deprecated property aliases."""

    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        left.rts = True
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert right.get_modem_pins().cts is PinState.HIGH

        right.rts = True
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.HIGH

        left.rts = False
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert right.get_modem_pins().cts is PinState.LOW

        right.rts = False
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.LOW


def test_fast_open_close(serial_pair: SerialPair) -> None:
    """Test quickly opening and closing a port."""

    message = b"Fast write and close test"

    with Serial.from_url(serial_pair.left, baudrate=115200) as left:
        with Serial.from_url(serial_pair.right, baudrate=115200) as right:
            right.write(message)
            right.flush()

            # Some backends (notably complex chained RFC2217) lose data on close, making
            # this test flaky without a tiny delay
            time.sleep(0.01)

        assert left.readexactly(len(message)) == message


@pytest.mark.skipif(sys.platform == "win32", reason="CloseHandle resets modem signals")
@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS)
def test_deassert_on_open(serial_pair: SerialPair) -> None:
    """Test RTS/CTS deassertion on open."""
    if serial_pair.uri_scheme in (
        "linux://",
        "darwin://",
        "posix://",
        "extended-posix://",
    ):
        pytest.skip("POSIX backends do not support deasserting pins on open")

    with Serial.from_url(serial_pair.left, baudrate=115200) as left:
        with Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_open=PinState.HIGH,
            rts_on_close=PinState.HIGH,
        ) as right:
            right.set_modem_pins(rts=True)
            time.sleep(serial_pair.modem_line_propagation_delay)
            assert left.get_modem_pins().cts is PinState.HIGH

        # rts_on_close=HIGH keeps RTS asserted
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.HIGH

        with Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_open=PinState.LOW,
            rts_on_close=PinState.HIGH,
        ) as right:
            # rts_on_open=LOW deasserts RTS
            time.sleep(serial_pair.modem_line_propagation_delay)
            assert left.get_modem_pins().cts is PinState.LOW
            right.set_modem_pins(rts=True)

        # rts_on_close=HIGH keeps RTS asserted
        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.HIGH


@pytest.mark.skipif(sys.platform == "win32", reason="CloseHandle resets modem signals")
@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS)
def test_hang_up_on_close(serial_pair: SerialPair) -> None:
    """Test RTS/CTS hang up on close."""
    if serial_pair.uri_scheme in (
        "linux://",
        "darwin://",
        "posix://",
        "extended-posix://",
    ):
        pytest.skip("POSIX backends do not support deasserting pins on open")

    with Serial.from_url(serial_pair.left, baudrate=115200) as left:
        with Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_close=PinState.HIGH,
            rts_on_open=PinState.HIGH,
        ) as right:
            right.set_modem_pins(rts=True)
            time.sleep(serial_pair.modem_line_propagation_delay)
            assert left.get_modem_pins().cts is PinState.HIGH

        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.HIGH

        with Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_close=PinState.HIGH,
            rts_on_open=PinState.HIGH,
        ) as right:
            time.sleep(serial_pair.modem_line_propagation_delay)
            assert left.get_modem_pins().cts is PinState.HIGH

        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.HIGH

        with Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            rts_on_close=PinState.LOW,
            rts_on_open=PinState.HIGH,
        ) as right:
            time.sleep(serial_pair.modem_line_propagation_delay)
            assert left.get_modem_pins().cts is PinState.HIGH

        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.LOW


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
def test_deassert_on_open_with_rtscts(
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

    with Serial.from_url(serial_pair.left, baudrate=115200) as left:
        with Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            rtscts=False,
            rts_on_open=PinState.HIGH,
            rts_on_close=PinState.HIGH,
        ) as right:
            right.set_modem_pins(rts=True)
            time.sleep(serial_pair.modem_line_propagation_delay)
            assert left.get_modem_pins().cts is PinState.HIGH

        time.sleep(serial_pair.modem_line_propagation_delay)
        assert left.get_modem_pins().cts is PinState.HIGH

        with Serial.from_url(
            serial_pair.right,
            baudrate=115200,
            rtscts=rtscts,
            rts_on_open=rts_on_open,
        ):
            time.sleep(serial_pair.modem_line_propagation_delay)
            assert left.get_modem_pins().cts is expected_state


def test_sync_unplug_raises(serial_pair: SerialPair) -> None:
    """Each operation on an unplugged port raises rather than silently EOFing."""
    unplug_left = serial_pair.unplug_left_graceful or serial_pair.unplug_left_abrupt
    if unplug_left is None:
        pytest.skip("backend cannot simulate a disconnect")

    with (
        Serial.from_url(serial_pair.left, baudrate=115200, timeout=2.0) as left,
        Serial.from_url(serial_pair.right, baudrate=115200) as right,
    ):
        right.write(b"ping\n")
        right.flush()
        assert left.readline() == b"ping\n"

        unplug_left()

        with pytest.raises(OSError):
            left.read(1)

        with pytest.raises(OSError):
            left.write(b"x")

        with pytest.raises(OSError):
            left.flush()

        with pytest.raises(OSError):
            left.get_modem_pins()

        with pytest.raises(OSError):
            left.set_modem_pins(rts=True)


@pytest.mark.skip(
    reason="held output stalls the kernel close and wedges the adapter; needs the bounded flush"
)
@pytest.mark.skip_quirks(SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_WRITE_TIMEOUT)
def test_write_timeout_cts_held(serial_pair: SerialPair) -> None:
    """Test that write timeout fires when CTS is deasserted (flow control hold)."""

    with Serial.from_url(serial_pair.right, baudrate=9600) as right:
        right.set_modem_pins(rts=False)
        time.sleep(serial_pair.modem_line_propagation_delay)

        with Serial.from_url(
            serial_pair.left,
            baudrate=9600,
            rtscts=True,
            write_timeout=0.5,
        ) as left:
            with measure_time() as elapsed:
                with pytest.raises(TimeoutError):
                    left.write(b"x" * 1024)

            assert 0.3 <= elapsed() <= 1.2

            # Let the held bytes drain, or closing the port waits on them
            right.set_modem_pins(rts=True)
