"""POSIX serial port implementation."""

from __future__ import annotations

import array
import asyncio
import errno
import fcntl
import logging
import os
import select
import sys
import termios
import time
from typing import Any, NamedTuple, cast

if sys.version_info >= (3, 11):
    from asyncio import timeout as asyncio_timeout
else:
    from async_timeout import timeout as asyncio_timeout

from typing_extensions import Buffer

from ..common import (
    BaseSerial,
    ModemPins,
    Parity,
    PinState,
    PortSettingsUpdate,
    StopBits,
    UnsupportedSetting,
    measure_time,
    register_uri_handler,
)
from ..descriptor_transport import DescriptorTransport
from ._termios_api import tcdrain, tcflush, tcgetattr, tcsetattr

LOGGER = logging.getLogger(__name__)

FLUSH_TIMEOUT = 10.0

# Reportedly, some drivers benefit from delaying between the `open` syscall and using
# `TCIOFLUSH`. Otherwise, the flush operation does not work reliably and stale data may
# be read.
AFTER_OPEN_DELAY = 0.01

MODEM_BIT_MAPPING = {
    "le": termios.TIOCM_LE,
    "dtr": termios.TIOCM_DTR,
    "rts": termios.TIOCM_RTS,
    "st": termios.TIOCM_ST,
    "sr": termios.TIOCM_SR,
    "cts": termios.TIOCM_CTS,
    "car": termios.TIOCM_CAR,
    "rng": termios.TIOCM_RNG,
    "dsr": termios.TIOCM_DSR,
}
assert MODEM_BIT_MAPPING.keys() == ModemPins.__annotations__.keys()


class TcsetattrFlags(NamedTuple):
    """Flags for `termios.tcsetattr`."""

    iflag: int
    oflag: int
    cflag: int
    lflag: int
    ispeed: int
    ospeed: int
    cc_vmin: int
    cc_vtime: int


def modem_pins_mask_of_value(modem_pins: ModemPins, mask: PinState) -> int:
    """Get modem bit mask for bits matching the specified value."""
    result = 0x00000000

    for name, bit in MODEM_BIT_MAPPING.items():
        value = getattr(modem_pins, name)

        if value is mask:
            result |= bit

    return result


def modem_pins_as_int(modem_pins: ModemPins) -> int:
    """Convert modem pins to integer."""
    result = 0x00000000

    for name, bit in MODEM_BIT_MAPPING.items():
        result |= bit if getattr(modem_pins, name) else 0x00000000

    return result


class PosixSerial(BaseSerial):
    """POSIX serial port implementation."""

    def __init__(
        self,
        *args: Any,
        fileno: int | None = None,
        inter_byte_timeout: float = 0.01,
        min_read_size: int = 1,
        **kwargs: Any,
    ) -> None:
        """Initialize POSIX serial port."""
        super().__init__(*args, **kwargs)
        self._fileno: int | None = fileno
        self._inter_byte_timeout = inter_byte_timeout
        self._min_read_size = min_read_size

    def _open(self) -> None:
        """Open the serial port."""
        LOGGER.debug("Opening serial port %r", self._path)

        if self._fileno is not None:
            raise ValueError("Serial port is already open")

        assert self._path is not None
        self._fileno = os.open(self._path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._auto_close = True

        if self._exclusive:
            self._lock()

        time.sleep(AFTER_OPEN_DELAY)

    @property
    def is_open(self) -> bool:
        """Check if the serial port is open."""
        return self._fileno is not None

    def _lock(self) -> None:
        """Lock the serial port for exclusive access."""
        LOGGER.debug("Locking serial port %r", self._path)

        assert self._fileno is not None

        try:
            fcntl.flock(self._fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OSError(
                errno.EBUSY,
                f"Serial port {self._path!r} is already locked by another process",
            ) from exc

    def _build_parity_flags(self) -> int:
        if self._parity == Parity.NONE:
            return 0
        elif self._parity == Parity.EVEN:
            return termios.PARENB
        elif self._parity == Parity.ODD:
            return termios.PARENB | termios.PARODD
        else:
            raise UnsupportedSetting(f"Unsupported parity {self._parity}")

    def _build_character_size_flags(self) -> int:
        if self._byte_size == 5:
            return termios.CS5
        elif self._byte_size == 6:
            return termios.CS6
        elif self._byte_size == 7:
            return termios.CS7
        elif self._byte_size == 8:
            return termios.CS8
        else:
            raise UnsupportedSetting(
                f"Unsupported byte size {self._byte_size}, must be 5, 6, 7, or 8"
            )

    def _build_stopbits_flags(self) -> int:
        if self._stopbits == StopBits.ONE:
            return 0
        elif self._stopbits == StopBits.TWO:
            return termios.CSTOPB
        elif self._stopbits == StopBits.ONE_POINT_FIVE:
            raise UnsupportedSetting("1.5 stop bits not supported on POSIX")
        else:
            raise UnsupportedSetting(f"Unsupported stop bits {self._stopbits}")

    def _build_flow_control_flags(self) -> tuple[int, int]:
        iflag = 0x00000000
        cflag = 0x00000000

        if self._xonxoff:
            iflag |= termios.IXON | termios.IXOFF | termios.IXANY

        if self._rtscts:
            raise UnsupportedSetting(
                "RTS/CTS hardware flow control is not supported on this POSIX platform"
            )

        if self._dsrdtr:
            raise UnsupportedSetting(
                "DSR/DTR hardware flow control is not supported on this POSIX platform"
            )

        return (iflag, cflag)

    def _build_ispeed(self) -> int:
        try:
            return cast(int, getattr(termios, f"B{self._baudrate}"))
        except AttributeError as exc:
            raise UnsupportedSetting(f"Unsupported baudrate {self._baudrate}") from exc

    def _build_ospeed(self) -> int:
        try:
            return cast(int, getattr(termios, f"B{self._baudrate}"))
        except AttributeError as exc:
            raise UnsupportedSetting(f"Unsupported baudrate {self._baudrate}") from exc

    def _build_tcsetattr_flags(self) -> TcsetattrFlags:
        iflag = 0x00000000
        oflag = 0x00000000
        cflag = 0x00000000
        lflag = 0x00000000

        # Enable receiver
        cflag |= termios.CREAD

        # Ignore modem control lines
        cflag |= termios.CLOCAL

        # Lower modem control lines after last process closes the device (hang up).
        # HUPCL is all-or-nothing: it lowers both DTR and RTS together, so POSIX can
        # only honor uniform close states.
        if (
            self._dtr_on_close is PinState.UNDEFINED
            and self._rts_on_close is PinState.UNDEFINED
        ):
            pass
        elif self._dtr_on_close is PinState.LOW and self._rts_on_close is PinState.LOW:
            cflag |= termios.HUPCL
        else:
            raise UnsupportedSetting(
                "POSIX only supports lowering both DTR and RTS together on close"
                " (dtr_on_close=rts_on_close=LOW) or leaving both untouched"
            )

        cflag |= self._build_character_size_flags()
        cflag |= self._build_parity_flags()
        cflag |= self._build_stopbits_flags()

        fc_iflag, fc_cflag = self._build_flow_control_flags()
        cflag |= fc_cflag
        iflag |= fc_iflag

        # Only emit reads if VMIN characters have been read, after no more data comes in
        # for VTIME seconds
        vmin = self._min_read_size
        vtime = int(self._inter_byte_timeout * 10)

        if not 0 <= vmin <= 255:
            raise ValueError(
                f"VMIN must be in range 0-255 (min_read_size={self._min_read_size})"
            )

        if not 0 <= vtime <= 255:
            raise ValueError(
                f"VTIME must be in range 0-255 (inter_byte_timeout={self._inter_byte_timeout})"
            )

        ispeed = self._build_ispeed()
        ospeed = self._build_ospeed()

        return TcsetattrFlags(
            iflag=iflag,
            oflag=oflag,
            cflag=cflag,
            lflag=lflag,
            ispeed=ispeed,
            ospeed=ospeed,
            cc_vmin=vmin,
            cc_vtime=vtime,
        )

    def _after_configure_port(self) -> None:
        pass

    def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        """Configure the serial port settings."""
        # termios applies the whole struct at once, so rebuild it from `self._*`
        LOGGER.debug("Configuring serial port %r", self._path)

        if self._fileno is None:
            raise ValueError("Cannot configure, serial port is not open")

        tcsetattr_flags = self._build_tcsetattr_flags()

        # We need to overwrite VMIN and VTIME in the CC array
        (
            _iflag,
            _oflag,
            _cflag,
            _lflag,
            _ispeed,
            _ospeed,
            cc,
        ) = tcgetattr(self._fileno)

        cc[termios.VMIN] = tcsetattr_flags.cc_vmin
        cc[termios.VTIME] = tcsetattr_flags.cc_vtime

        LOGGER.debug("Configuring serial port: %r + cc=%r", tcsetattr_flags, cc)

        # Finally, set up the serial port
        tcsetattr(
            self._fileno,
            termios.TCSANOW,  # TODO: should we use TCSADRAIN or TCSAFLUSH instead?
            [
                tcsetattr_flags.iflag,
                tcsetattr_flags.oflag,
                tcsetattr_flags.cflag,
                tcsetattr_flags.lflag,
                tcsetattr_flags.ispeed,
                tcsetattr_flags.ospeed,
                cc,
            ],
        )

        self._after_configure_port()

        self.set_modem_pins(self._modem_pins_on_open())

        # Flush input and output buffers to discard stale data
        tcflush(self._fileno, termios.TCIOFLUSH)

    def _get_modem_pins(self) -> ModemPins:
        """Get current modem control bits."""
        assert self._fileno is not None

        buffer = array.array("i", [0x00000000])

        try:
            fcntl.ioctl(self._fileno, termios.TIOCMGET, buffer)
        except OSError as exc:
            if exc.errno == errno.ENOTTY:
                LOGGER.debug("Device is not a serial port, cannot get modem pins")
                return ModemPins()

        n = buffer[0]
        return ModemPins(
            **{
                name: PinState.HIGH if n & bit else PinState.LOW
                for name, bit in MODEM_BIT_MAPPING.items()
            }
        )

    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set modem control bits."""
        assert self._fileno is not None

        LOGGER.debug("Setting modem pins: %r", modem_pins)

        all_pins_set = all(
            getattr(modem_pins, name) is not PinState.UNDEFINED
            for name in MODEM_BIT_MAPPING
        )

        try:
            if all_pins_set:
                value = modem_pins_as_int(modem_pins)
                LOGGER.debug("Setting all with TIOCMSET: 0x%08X", value)
                fcntl.ioctl(self._fileno, termios.TIOCMSET, array.array("i", [value]))
            else:
                to_set = modem_pins_mask_of_value(modem_pins, PinState.HIGH)
                to_clear = modem_pins_mask_of_value(modem_pins, PinState.LOW)

                if to_set:
                    LOGGER.debug("Setting TIOCMBIS: 0x%08X", to_set)
                    fcntl.ioctl(
                        self._fileno, termios.TIOCMBIS, array.array("i", [to_set])
                    )

                if to_clear:
                    LOGGER.debug("TIOCMBIC: 0x%08X", to_clear)
                    fcntl.ioctl(
                        self._fileno, termios.TIOCMBIC, array.array("i", [to_clear])
                    )
        except OSError as exc:
            if exc.errno == errno.ENOTTY:
                LOGGER.debug("Device is not a serial port, cannot set modem pins")

    def _flush(self) -> None:
        """Flush write buffers, waiting until all data is written."""
        assert self._fileno is not None
        LOGGER.debug("Flushing file descriptor %r", self._fileno)
        tcdrain(self._fileno)

    def _close(self) -> None:
        """Close the serial port."""
        if self._fileno is not None:
            os.close(self._fileno)
            self._fileno = None

    def fileno(self) -> int:
        """Get the file descriptor number."""
        assert self._fileno is not None
        return self._fileno

    # `io.IOBase` implements `read`, `readline`, using `readinto`
    if sys.version_info >= (3, 14):

        def _readinto(self, b: Buffer, *, timeout: float | None) -> int:
            """Read bytes from serial port into buffer."""
            assert self._fileno is not None

            ready, _, _ = select.select([self._fileno], [], [], timeout)
            if not ready:
                return 0

            try:
                n = os.readinto(self._fileno, b)
            except BlockingIOError:
                return 0

            LOGGER.debug("Read %d bytes", n)

            if n == 0:
                self._mark_broken(
                    OSError(
                        errno.EIO, "device disconnected or in use by another process"
                    )
                )
                self._check_broken()

            return n

    else:

        def _readinto(self, b: Buffer, *, timeout: float | None) -> int:
            """Read bytes from serial port into buffer."""
            assert self._fileno is not None

            ready, _, _ = select.select([self._fileno], [], [], timeout)
            if not ready:
                return 0

            m = memoryview(b).cast("B")
            size = len(m)
            LOGGER.debug("Reading up to %d bytes", size)

            try:
                chunk = os.read(self._fileno, size)
            except BlockingIOError:
                return 0

            n = len(chunk)
            m[:n] = chunk
            LOGGER.debug("Read %d bytes: %r", n, chunk)

            if n == 0:
                self._mark_broken(
                    OSError(
                        errno.EIO, "device disconnected or in use by another process"
                    )
                )
                self._check_broken()

            return n

    def _write(self, data: Buffer, *, timeout: float | None) -> int:
        """Write bytes to serial port."""
        LOGGER.debug("Writing %d bytes: %r", len(data), data)  # type: ignore[arg-type]
        assert self._fileno is not None

        view = memoryview(data).cast("B")
        remaining_timeout = timeout
        written = 0

        while written < len(view):
            with measure_time() as get_elapsed:
                _, ready, _ = select.select([], [self._fileno], [], remaining_timeout)

            if not ready:
                raise TimeoutError("Write timeout")

            if remaining_timeout is not None:
                remaining_timeout = max(remaining_timeout - get_elapsed(), 0)

            try:
                n = os.write(self._fileno, view[written:])
            except BlockingIOError:
                continue

            written += n

        return written

    def num_unread_bytes(self) -> int:
        """Return the number of bytes waiting to be read."""
        assert self._fileno is not None
        buffer = array.array("i", [0x00000000])

        fcntl.ioctl(self._fileno, termios.FIONREAD, buffer)

        return buffer[0]

    def num_unwritten_bytes(self) -> int:
        """Return the number of bytes waiting to be written."""
        assert self._fileno is not None
        buffer = array.array("i", [0x00000000])

        fcntl.ioctl(self._fileno, termios.TIOCOUTQ, buffer)

        return buffer[0]

    def _reset_read_buffer(self) -> None:
        """Reset the read buffer."""
        assert self._fileno is not None
        tcflush(self._fileno, termios.TCIFLUSH)

    def _reset_write_buffer(self) -> None:
        """Reset the write buffer."""
        assert self._fileno is not None
        tcflush(self._fileno, termios.TCOFLUSH)


class PosixSerialTransport(DescriptorTransport):
    """POSIX serial port transport using asyncio."""

    _serial_cls: type[PosixSerial] = PosixSerial

    async def _connect(  # type: ignore[override]
        self, *, path: str | os.PathLike[str], **kwargs: Any
    ) -> None:
        """Connect to serial port."""
        normalized_path = str(path)
        await super()._open(normalized_path)

        self._serial = self._serial_cls(
            **kwargs,
            path=normalized_path,
            # `DescriptorTransport` opened the port
            fileno=self._fileno,
            # Nonblocking mode
            min_read_size=0,
            inter_byte_timeout=0,
        )
        self._extra["serial"] = self._serial

        if self._serial._exclusive:
            await self._loop.run_in_executor(None, self._serial._lock)

        await asyncio.sleep(AFTER_OPEN_DELAY)

        await self._loop.run_in_executor(
            None, self._serial._reconfigure_port, self._serial._all_settings()
        )

        if self.is_closing():
            # If we are closing, we should not call `connection_made`
            return

        await super()._connect()

        self._call_protocol_connection_made()

    async def _flush(self) -> None:
        """Flush write buffers, waiting until all data is written, internal."""
        assert self._serial is not None

        try:
            # Wait for internal buffer to drain
            await self._make_empty_waiter()

            # Wait for hardware buffer to flush (with timeout)
            async with asyncio_timeout(FLUSH_TIMEOUT):
                await self._loop.run_in_executor(None, self._serial.flush)
        finally:
            self._reset_empty_waiter()

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


register_uri_handler(
    scheme="device://",
    unique_scheme="posix://",
    sync_cls=PosixSerial,
    async_transport_cls=PosixSerialTransport,
    strip_uri_scheme=True,
)
