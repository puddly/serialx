"""Tests for the AsyncSerial API."""

import pytest

from serialx import Parity, SerialException, StopBits, async_serial_for_url
from tests.common import SerialPair, async_create_serial_pair


async def test_unopened_state() -> None:
    """A freshly-constructed AsyncSerial reports closed and has no transport."""
    serial = async_serial_for_url("socket://1.2.3.4:5678", baudrate=115200)
    assert serial.is_open is False
    await serial.close()
    assert serial.is_open is False


async def test_repr_unopened() -> None:
    """repr() works on an unopened instance and reports url + null transport."""
    serial = async_serial_for_url("socket://1.2.3.4:5678", baudrate=115200)
    text = repr(serial)
    assert "AsyncSerial" in text
    assert "url='socket://1.2.3.4:5678'" in text
    assert "transport=None" in text


async def test_async_with_opens_and_closes(serial_pair: SerialPair) -> None:
    """`async with` opens on enter and closes on exit."""
    serial = async_serial_for_url(serial_pair.left, baudrate=115200)
    assert serial.is_open is False

    async with serial:
        assert serial.is_open is True
        assert serial.baudrate == 115200

    assert serial.is_open is False


async def test_manual_open_close(serial_pair: SerialPair) -> None:
    """Users can open() and await close() manually."""
    serial = async_serial_for_url(serial_pair.left, baudrate=115200)
    await serial.open()
    assert serial.is_open is True
    assert serial.baudrate == 115200
    await serial.close()
    assert serial.is_open is False


async def test_double_open_raises(serial_pair: SerialPair) -> None:
    """Calling open() on an already-open instance raises SerialException."""
    async with async_serial_for_url(serial_pair.left, baudrate=115200) as serial:
        with pytest.raises(SerialException, match="already open"):
            await serial.open()


async def test_reopen_after_close(serial_pair: SerialPair) -> None:
    """The same instance can be re-opened after close, like sync Serial."""
    serial = async_serial_for_url(serial_pair.left, baudrate=115200)

    async with serial:
        assert serial.is_open is True
    assert serial.is_open is False

    async with serial:
        assert serial.is_open is True
    assert serial.is_open is False


async def test_schedule_close_then_wait(serial_pair: SerialPair) -> None:
    """schedule_close() returns immediately; wait_closed() finishes the close."""
    serial = async_serial_for_url(serial_pair.left, baudrate=115200)
    await serial.open()
    assert serial.is_open is True
    serial.schedule_close()
    await serial.wait_closed()
    assert serial.is_open is False


async def test_abort(serial_pair: SerialPair) -> None:
    """abort() drops pending writes and triggers close immediately."""
    serial = async_serial_for_url(serial_pair.left, baudrate=115200)
    await serial.open()
    serial.write_nowait(b"this may be dropped")
    serial.abort()
    await serial.wait_closed()
    assert serial.is_open is False


async def test_read_when_unopened_raises() -> None:
    """Reading or writing on a never-opened instance raises SerialException."""
    serial = async_serial_for_url("socket://1.2.3.4:5678", baudrate=115200)
    with pytest.raises(SerialException, match="not open"):
        await serial.read(1)

    with pytest.raises(SerialException, match="not open"):
        await serial.write(b"x")

    with pytest.raises(SerialException, match="not open"):
        serial.write_nowait(b"x")

    with pytest.raises(SerialException, match="not open"):
        await serial.writelines([b"x"])

    with pytest.raises(SerialException, match="not open"):
        serial.writelines_nowait([b"x"])


async def test_write_then_close_preserves_data(serial_pair: SerialPair) -> None:
    """`await write` drains before returning so close() doesn't lose bytes."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        await left.write(b"hello world")
        await left.close()
        assert await right.readexactly(11) == b"hello world"


async def test_writelines_then_close_preserves_data(serial_pair: SerialPair) -> None:
    """`await writelines` drains before returning so close() doesn't lose bytes."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        await left.writelines([b"foo", b"bar", b"baz"])
        await left.close()
        assert await right.readexactly(9) == b"foobarbaz"


async def test_reconfigure_port(serial_pair: SerialPair) -> None:
    """`reconfigure_port` changes only the settings passed and keeps data flowing."""
    async with async_create_serial_pair(
        serial_pair.left, serial_pair.right, baudrate=115200
    ) as (left, right):
        await left.reconfigure_port(baudrate=9600, parity=Parity.EVEN)
        await right.reconfigure_port(baudrate=9600, parity=Parity.EVEN)

        assert left.baudrate == 9600
        assert left.parity is Parity.EVEN
        assert left.stopbits is StopBits.ONE
        assert left.byte_size == 8

        await left.write(b"hello")
        assert await right.readexactly(5) == b"hello"
