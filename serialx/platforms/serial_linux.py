"""Linux serial port implementation."""

from __future__ import annotations

import array
import asyncio
from collections.abc import Iterator
from contextlib import suppress
import ctypes
import errno
import fcntl
import logging
from pathlib import Path
import sys
import termios
from typing import Any

from ..common import Parity, SerialPortInfo, UnsupportedSetting, register_uri_handler
from .serial_extended_posix import ExtendedPosixSerial, ExtendedPosixSerialTransport

LOGGER = logging.getLogger(__name__)

SYS_ROOT = Path("/sys")
DEV_ROOT = Path("/dev")

# From `include/uapi/linux/serial.h`
PORT_UNKNOWN = 0

ASYNC_LOW_LATENCY = 1 << 13
CMSPAR = 0o10000000000
TCGETS = 0x5401
TCGETS2 = 0x802C542A
TCSETS2 = 0x402C542B

TIOCGSERIAL = getattr(termios, "TIOCGSERIAL", None)
TIOCSSERIAL = getattr(termios, "TIOCSSERIAL", None)
CBAUD = getattr(termios, "CBAUD", 0o00010017)
CBAUDEX = getattr(termios, "CBAUDEX", 0o00010000)

# When we need to set a non-POSIX baudrate, we set the baudrates to a known default and
# then override
NON_POSIX_FALLBACK_BAUDRATE = 115200
NON_POSIX_FALLBACK_BAUDRATE_CONST = termios.B115200

# `NCCS` is 19 on every Linux architecture and the `struct termios2` layout has been
# stable since 2007
NCCS = 19


class Termios2Struct(ctypes.Structure):
    """The `termios2` struct."""

    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (
        ("c_iflag", ctypes.c_uint32),
        ("c_oflag", ctypes.c_uint32),
        ("c_cflag", ctypes.c_uint32),
        ("c_lflag", ctypes.c_uint32),
        ("c_line", ctypes.c_uint8),
        ("c_cc", ctypes.c_uint8 * NCCS),
        ("c_ispeed", ctypes.c_uint32),
        ("c_ospeed", ctypes.c_uint32),
    )


class LinuxSerial(ExtendedPosixSerial):
    """Linux serial port implementation."""

    def __init__(
        self,
        *args: Any,
        low_latency: bool = True,
        **kwargs: Any,
    ) -> None:
        """Initialize Linux serial port."""
        super().__init__(*args, **kwargs)
        self._low_latency = low_latency

    def _set_non_posix_baudrate(self, baudrate: int) -> None:
        """Set the baudrate of the serial port, must be called after `tcsetattr`."""
        assert self._fileno is not None

        buffer = bytearray(ctypes.sizeof(Termios2Struct))
        fcntl.ioctl(self._fileno, TCGETS2, buffer)

        termios2 = Termios2Struct.from_buffer(buffer)

        # Sanity check that our struct layout matches the kernel's
        if termios2.c_ispeed == 0 or termios2.c_ospeed == 0:
            raise RuntimeError(f"termios2 speed fields are zero: {buffer.hex()}")

        # The POSIX baudrates are stored in the lower bits of `c_cflag`. We clear them.
        termios2.c_cflag &= ~CBAUD
        termios2.c_cflag |= CBAUDEX

        termios2.c_ispeed = baudrate
        termios2.c_ospeed = baudrate

        # The ctypes structure mutates the buffer in place
        LOGGER.debug("Writing termios2 struct: %r", buffer.hex())
        fcntl.ioctl(self._fileno, TCSETS2, buffer)

    def _build_parity_flags(self) -> int:
        if self._parity == Parity.NONE:
            return 0
        elif self._parity == Parity.EVEN:
            return termios.PARENB
        elif self._parity == Parity.ODD:
            return termios.PARENB | termios.PARODD
        elif self._parity == Parity.MARK:
            return termios.PARENB | termios.PARODD | CMSPAR
        elif self._parity == Parity.SPACE:
            return termios.PARENB | CMSPAR
        else:
            raise UnsupportedSetting(f"Unsupported parity {self._parity}")

    @property
    def _has_non_posix_baudrate(self) -> bool:
        return not hasattr(termios, f"B{self._baudrate}")

    def _build_ispeed(self) -> int:
        return (
            NON_POSIX_FALLBACK_BAUDRATE_CONST
            if self._has_non_posix_baudrate
            else getattr(termios, f"B{self._baudrate}")
        )

    def _build_ospeed(self) -> int:
        return (
            NON_POSIX_FALLBACK_BAUDRATE_CONST
            if self._has_non_posix_baudrate
            else getattr(termios, f"B{self._baudrate}")
        )

    def _after_configure_port(self) -> None:
        if self._has_non_posix_baudrate:
            LOGGER.debug("Setting non-POSIX baudrate %d", self._baudrate)
            self._set_non_posix_baudrate(self._baudrate)

        if TIOCSSERIAL is not None:
            try:
                self._set_low_latency(self._low_latency)
            except OSError as exc:
                if exc.errno in (errno.ENOTTY, errno.EOPNOTSUPP):
                    LOGGER.debug("Device does not support setting low latency")
                else:
                    raise

    def _set_low_latency(self, value: bool) -> None:
        """Set low latency mode."""
        assert self._fileno is not None
        assert TIOCGSERIAL is not None
        assert TIOCSSERIAL is not None

        LOGGER.debug("Setting low latency mode: %r", value)

        buffer = array.array("i", [0x00000000] * 19 * 8)

        fcntl.ioctl(self._fileno, TIOCGSERIAL, buffer)

        if self._low_latency:
            buffer[4] |= ASYNC_LOW_LATENCY
        else:
            buffer[4] &= ~ASYNC_LOW_LATENCY

        fcntl.ioctl(self._fileno, TIOCSSERIAL, buffer)


class LinuxSerialTransport(ExtendedPosixSerialTransport):
    """Linux serial port transport using asyncio."""

    _serial_cls = LinuxSerial


def iterdir_safe(path: Path) -> Iterator[Path]:
    """Safely iterate over a dir, yielding nothing on error."""

    with suppress(OSError):
        yield from path.iterdir()


def _read_optional_sysfs(path: Path) -> str | None:
    """Read a sysfs string file, returning None if it does not exist."""
    try:
        # `read_text` would translate newlines within the descriptor
        return path.read_bytes().decode("utf-8")[:-1]
    except OSError:
        return None


def linux_list_serial_ports() -> list[SerialPortInfo]:
    """List serial ports on Linux."""
    by_id_symlinks = {}
    by_id_path = DEV_ROOT / "serial/by-id"

    # `/dev/serial/by-id/` can disappear if nothing is plugged in
    for symlink in iterdir_safe(by_id_path):
        by_id_symlinks[symlink.resolve()] = symlink

    results = []

    for path in iterdir_safe(SYS_ROOT / "class/tty"):
        if not path.name.startswith("tty"):
            continue

        tty_device = path / "device"
        if not (tty_device / "driver").exists():
            continue

        device = DEV_ROOT / path.name

        try:
            resolved = tty_device.resolve(strict=True)
            # Some devices have no subsystem (GitHub Actions runner VM)
            subsystem = (resolved / "subsystem").resolve(strict=True).name
        except OSError:
            continue

        unique_device = by_id_symlinks.get(device, device)

        if subsystem == "usb-serial":
            # USB-serial chips
            usb_interface = resolved.parent
            usb_device = usb_interface.parent

            try:
                vid = int((usb_device / "idVendor").read_text(), 16)
                pid = int((usb_device / "idProduct").read_text(), 16)
                bcd_device = int((usb_device / "bcdDevice").read_text(), 16)
                interface_num = int(
                    (usb_interface / "bInterfaceNumber").read_text(), 16
                )
            except OSError:
                LOGGER.debug(
                    "Serial device %r disappeared during iteration", usb_device
                )
                continue

            info = SerialPortInfo(
                device=str(unique_device),
                resolved_device=str(device),
                vid=vid,
                pid=pid,
                serial_number=_read_optional_sysfs(usb_device / "serial"),
                manufacturer=_read_optional_sysfs(usb_device / "manufacturer"),
                product=_read_optional_sysfs(usb_device / "product"),
                bcd_device=bcd_device,
                interface_description=_read_optional_sysfs(usb_interface / "interface"),
                interface_num=interface_num,
            )
        elif subsystem == "usb":
            # CDC ACM devices
            usb_interface = resolved
            usb_device = usb_interface.parent

            try:
                vid = int((usb_device / "idVendor").read_text(), 16)
                pid = int((usb_device / "idProduct").read_text(), 16)
                bcd_device = int((usb_device / "bcdDevice").read_text(), 16)
                interface_num = int(
                    (usb_interface / "bInterfaceNumber").read_text(), 16
                )
            except OSError:
                LOGGER.debug("USB device %r disappeared during iteration", usb_device)
                continue

            info = SerialPortInfo(
                device=str(unique_device),
                resolved_device=str(device),
                vid=vid,
                pid=pid,
                serial_number=_read_optional_sysfs(usb_device / "serial"),
                manufacturer=_read_optional_sysfs(usb_device / "manufacturer"),
                product=_read_optional_sysfs(usb_device / "product"),
                bcd_device=bcd_device,
                interface_description=_read_optional_sysfs(usb_interface / "interface"),
                interface_num=interface_num,
            )
        elif subsystem in ("serial-base", "platform", "pnp", "amba"):
            # `serial-base` is the per-port subsystem introduced in Linux 6.10.
            # Older kernels expose native ports through their bus directly:
            #   `platform` - 8250 placeholders, RP1 `pl011-axi`, most ARM SoCs
            #   `pnp`      - PnP-discovered 16550A on x86
            #   `amba`     - ARM PrimeCell UART (`uart-pl011`) on BCM2712 etc.
            # All four expose `/sys/class/tty/<tty>/type`.
            try:
                port_type = (path / "type").read_text()
            except OSError:
                LOGGER.debug("Port %r disappeared during iteration", device)
                continue

            if int(port_type) == PORT_UNKNOWN:
                continue

            # Native serial ports
            info = SerialPortInfo(
                device=str(unique_device),
                resolved_device=str(device),
                vid=None,
                pid=None,
                serial_number=None,
                manufacturer=None,
                product=None,
                bcd_device=None,
                interface_description=None,
                interface_num=None,
            )
        else:
            LOGGER.warning(
                "Unknown serial device subsystem %r for device %r",
                subsystem,
                device,
            )
            continue

        results.append(info)

    return results


async def async_linux_list_serial_ports() -> list[SerialPortInfo]:
    """List serial ports on Linux, async."""
    return await asyncio.to_thread(linux_list_serial_ports)


if sys.platform == "linux":
    register_uri_handler(
        scheme="device://",
        unique_scheme="linux://",
        sync_cls=LinuxSerial,
        async_transport_cls=LinuxSerialTransport,
        list_serial_ports_func=linux_list_serial_ports,
        async_list_serial_ports_func=async_linux_list_serial_ports,
        weight=3,
        strip_uri_scheme=True,
    )
