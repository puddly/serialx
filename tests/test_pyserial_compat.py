"""Tests for pyserial API compatibility shims."""

import sys

import pytest

if sys.platform == "emscripten":
    pytest.skip(
        "pyserial compatibility uses the sync Serial class, which is stubs "
        "under Pyodide",
        allow_module_level=True,
    )

from serialx import (
    EIGHTBITS,
    FIVEBITS,
    PARITY_EVEN,
    PARITY_MARK,
    PARITY_NONE,
    PARITY_ODD,
    PARITY_SPACE,
    SEVENBITS,
    SIXBITS,
    STOPBITS_ONE,
    STOPBITS_ONE_POINT_FIVE,
    STOPBITS_TWO,
    Parity,
    PinState,
    Serial,
    SerialException,
    SerialPortInfo,
)
from serialx.tools.list_ports import comports, grep
from serialx.tools.list_ports_common import ListPortInfo
from tests.common import SerialPair


def test_compat_constructor_kwargs(serial_pair: SerialPair) -> None:
    """Test that pyserial-style constructor kwargs map to the correct properties."""
    with Serial.from_url(
        serial_pair.left,
        baudrate=9600,
        timeout=1.5,
        writeTimeout=2.0,
        bytesize=7,
    ) as s:
        # pyserial kwarg -> serialx property
        assert s.read_timeout == 1.5
        assert s.write_timeout == 2.0
        assert s.byte_size == 7

        # pyserial deprecated property aliases read back correctly
        assert s.port == str(s.path)
        assert s.portstr == str(s.path)
        assert s.timeout == 1.5
        assert s.writeTimeout == 2.0
        assert s.bytesize == 7
        assert s.baudrate == 9600


def test_compat_deprecated_aliases(serial_pair: SerialPair) -> None:
    """Test deprecated method and property aliases on an opened serial port."""
    with (
        Serial.from_url(serial_pair.left, baudrate=115200) as left,
        Serial.from_url(serial_pair.right, baudrate=115200, timeout=0.2) as right,
    ):
        # isOpen
        assert left.isOpen() is True
        assert left.is_open is True

        # in_waiting / inWaiting
        assert right.in_waiting == 0
        assert right.inWaiting == 0

        left.write(b"hello")
        left.flush()

        right.readexactly(5)

        # out_waiting
        assert left.out_waiting >= 0

        # reset_input_buffer / flushInput -> reset_read_buffer
        right.reset_input_buffer()
        right.flushInput()

        # reset_output_buffer / flushOutput -> reset_write_buffer
        left.reset_output_buffer()
        left.flushOutput()


def test_compat_timeout_setter(serial_pair: SerialPair) -> None:
    """Test that the deprecated .timeout setter mutates read_timeout."""
    with Serial.from_url(serial_pair.left, baudrate=115200) as s:
        s.timeout = 0.1
        assert s.read_timeout == 0.1
        assert s.timeout == 0.1

        s.timeout = 0.5
        assert s.read_timeout == 0.5


def test_compat_parity_none(serial_pair: SerialPair) -> None:
    """Test that `parity=None` is accepted and maps to `Parity.NONE`."""
    with Serial.from_url(serial_pair.left, baudrate=115200, parity=None) as s:
        assert s.parity is Parity.NONE


def test_compat_baudrate_setter(serial_pair: SerialPair) -> None:
    """Test that the deprecated .baudrate setter reconfigures the port."""
    with Serial.from_url(serial_pair.left, baudrate=9600) as s:
        assert s.baudrate == 9600
        with pytest.warns(DeprecationWarning, match="baudrate"):
            s.baudrate = 115200
        assert s.baudrate == 115200


def test_compat_data_bits(serial_pair: SerialPair) -> None:
    """Test that `data_bits` is a read/write alias for `byte_size`."""
    with Serial.from_url(serial_pair.left, baudrate=9600, bytesize=7) as s:
        assert s.data_bits == 7
        assert s.byte_size == 7

        with pytest.warns(DeprecationWarning, match="data_bits"):
            s.data_bits = 8
        assert s.data_bits == 8
        assert s.byte_size == 8
        assert s.bytesize == 8


def test_compat_stop_bits(serial_pair: SerialPair) -> None:
    """Test that `stop_bits` reads/writes as int/float and maps to `stopbits`."""
    with Serial.from_url(serial_pair.left, baudrate=9600) as s:
        assert s.stop_bits == 1
        assert s.stopbits.value == 1

        with pytest.warns(DeprecationWarning, match="stop_bits"):
            s.stop_bits = 2
        assert s.stop_bits == 2
        assert s.stopbits.value == 2

    # Not every backend supports 1.5 stop bits, so only store it
    s = Serial()
    with pytest.warns(DeprecationWarning, match="closed port"):
        s.stop_bits = 1.5
    assert s.stop_bits == 1.5
    assert s.stopbits.value == 1.5

    with pytest.raises(ValueError):
        s.stop_bits = 3


def test_reconfigure_port_closed_raises() -> None:
    """Settings for a closed port belong in the constructor."""
    s = Serial()
    with pytest.raises(SerialException, match="closed port"):
        s.reconfigure_port(baudrate=115200)


def test_compat_no_arg_construction() -> None:
    """Test that Serial can be constructed with no args (deferred open pattern)."""
    s = Serial()
    assert s.path is None
    assert s.baudrate == 9600
    assert s.port is None


def test_compat_set_dtr_rts_before_open() -> None:
    """Test the pyserial configure-then-open pattern for DTR/RTS."""
    s = Serial()

    # Defaults reflect the on-open state (dtr_on_open=rts_on_open=HIGH)
    assert s.dtr is True
    assert s.rts is True

    # Setting before open seeds the state applied on open instead of raising
    s.dtr = False
    s.rts = True

    assert s.dtr is False
    assert s.rts is True
    assert s.dtr_on_open is PinState.LOW
    assert s.rts_on_open is PinState.HIGH


def test_compat_legacy_rtsdtr_kwargs() -> None:
    """The legacy `rtsdtr_on_*` kwargs map to both pins and warn."""
    with pytest.warns(DeprecationWarning, match="rtsdtr_on_open"):
        s = Serial(rtsdtr_on_open=PinState.LOW, rtsdtr_on_close=PinState.HIGH)

    # A single legacy value drives both DTR and RTS
    assert s.dtr_on_open is PinState.LOW
    assert s.rts_on_open is PinState.LOW
    assert s.dtr_on_close is PinState.HIGH
    assert s.rts_on_close is PinState.HIGH


def test_compat_legacy_rtsdtr_one_sided() -> None:
    """Only the legacy kwarg that is passed overrides; the other keeps defaults."""
    with pytest.warns(DeprecationWarning, match="rtsdtr_on_open"):
        s = Serial(rtsdtr_on_open=PinState.LOW)

    assert s.dtr_on_open is PinState.LOW
    assert s.rts_on_open is PinState.LOW
    # close side untouched -> defaults
    assert s.dtr_on_close is PinState.LOW
    assert s.rts_on_close is PinState.LOW


def test_compat_no_legacy_kwargs_does_not_warn(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Constructing without the legacy kwargs emits no deprecation warning."""
    Serial(dtr_on_open=PinState.LOW, rts_on_open=PinState.HIGH)

    assert [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)] == []


def test_compat_do_not_open(serial_pair: SerialPair) -> None:
    """Test `do_not_open` backwards compatibility."""
    with pytest.raises(RuntimeError, match="do_not_open=False is not supported"):
        Serial(serial_pair.left, do_not_open=False)

    s = Serial(serial_pair.left, do_not_open=True)
    assert not s.is_open


def test_compat_constants() -> None:
    """Test that pyserial module-level constants are re-exported with correct values."""
    assert FIVEBITS == 5
    assert SIXBITS == 6
    assert SEVENBITS == 7
    assert EIGHTBITS == 8

    assert PARITY_NONE == "N"
    assert PARITY_EVEN == "E"
    assert PARITY_ODD == "O"
    assert PARITY_MARK == "M"
    assert PARITY_SPACE == "S"

    assert STOPBITS_ONE == 1  # type: ignore[comparison-overlap]
    assert STOPBITS_ONE_POINT_FIVE == 1.5
    assert STOPBITS_TWO == 2  # type: ignore[comparison-overlap]


def test_compat_tools_module() -> None:
    """Test that serialx.tools.list_ports provides pyserial-compatible API."""
    # comports is list_serial_ports
    ports = comports()
    assert isinstance(ports, list)

    for port in ports:
        assert isinstance(port, SerialPortInfo)

    # ListPortInfo is SerialPortInfo
    assert ListPortInfo is SerialPortInfo

    # grep returns an iterable (may be empty on CI with no ports)
    results = list(grep(".*"))
    for result in results:
        assert isinstance(result, SerialPortInfo)
