"""Linux serial port implementation."""

from __future__ import annotations

import array
import asyncio
from collections.abc import Iterator
from contextlib import suppress
import ctypes
import dataclasses
import errno
import fcntl
import logging
import os
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

TIOCGSERIAL = getattr(termios, "TIOCGSERIAL", None)
TIOCSSERIAL = getattr(termios, "TIOCSSERIAL", None)

# When we need to set a non-POSIX baudrate, we set the baudrates to a known default and
# then override
NON_POSIX_FALLBACK_BAUDRATE = 115200
NON_POSIX_FALLBACK_BAUDRATE_CONST = termios.B115200


class Termios2Struct(ctypes.Structure):
    """The generic `struct termios2`."""

    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (
        ("c_iflag", ctypes.c_uint32),
        ("c_oflag", ctypes.c_uint32),
        ("c_cflag", ctypes.c_uint32),
        ("c_lflag", ctypes.c_uint32),
        ("c_line", ctypes.c_uint8),
        ("c_cc", ctypes.c_uint8 * 19),
        ("c_ispeed", ctypes.c_uint32),
        ("c_ospeed", ctypes.c_uint32),
    )


class AlphaTermios2Struct(ctypes.Structure):
    """Alpha and PowerPC `struct termios2`."""

    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (
        ("c_iflag", ctypes.c_uint32),
        ("c_oflag", ctypes.c_uint32),
        ("c_cflag", ctypes.c_uint32),
        ("c_lflag", ctypes.c_uint32),
        # `c_cc` is before `c_line`
        ("c_cc", ctypes.c_uint8 * 19),
        ("c_line", ctypes.c_uint8),
        ("c_ispeed", ctypes.c_uint32),
        ("c_ospeed", ctypes.c_uint32),
    )


class MipsTermios2Struct(ctypes.Structure):
    """MIPS `struct termios2`."""

    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (
        ("c_iflag", ctypes.c_uint32),
        ("c_oflag", ctypes.c_uint32),
        ("c_cflag", ctypes.c_uint32),
        ("c_lflag", ctypes.c_uint32),
        ("c_line", ctypes.c_uint8),
        # `NCCS = 23`
        ("c_cc", ctypes.c_uint8 * 23),
        ("c_ispeed", ctypes.c_uint32),
        ("c_ospeed", ctypes.c_uint32),
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class IoctlEncoding:
    """The `_IOC` request encoding from `asm/ioctl.h`."""

    size_bits: int
    read: int
    write: int

    def encode(self, direction: int, request: tuple[str, int], size: int) -> int:
        """Encode an `_IOC(direction, type, nr, size)` request number."""
        request_type, number = request
        return (
            (direction << (16 + self.size_bits))
            | (size << 16)
            | (ord(request_type) << 8)
            | number
        )


@dataclasses.dataclass(frozen=True)
class Termios2Abi:
    """The `termios2` ABI, from `asm/{ioctl,ioctls,termbits}.h`."""

    ioctl: IoctlEncoding
    struct: type[ctypes.Structure]
    get_request: tuple[str, int]
    set_request: tuple[str, int]
    cbaud: int
    bother: int

    @property
    def tcgets2(self) -> int:
        """The `TCGETS2` request number."""
        return self.ioctl.encode(
            self.ioctl.read,
            self.get_request,
            ctypes.sizeof(self.struct),
        )

    @property
    def tcsets2(self) -> int:
        """The `TCSETS2` request number."""
        return self.ioctl.encode(
            self.ioctl.write,
            self.set_request,
            ctypes.sizeof(self.struct),
        )

    def validate(self, buffer: bytearray) -> None:
        """Ensure a `TCGETS2` readback matches the struct layout we expect."""
        termios2 = self.struct.from_buffer(buffer)
        if termios2.c_ispeed == 0 or termios2.c_ospeed == 0:
            raise RuntimeError(f"termios2 speed fields are zero: {buffer.hex()}")


@dataclasses.dataclass(frozen=True)
class PowerPcTermios2Abi(Termios2Abi):
    """PowerPC has no `termios2`: its `struct termios` already has the speed fields."""

    def validate(self, buffer: bytearray) -> None:
        """Ensure a `TCGETS` readback is not all zeroes."""
        # The speed fields can read back as zero until `BOTHER` is written
        if not any(buffer):
            raise RuntimeError(f"termios2 speed fields are zero: {buffer.hex()}")


# Keyed by `uname -m` prefix
TERMIOS2_ABIS: dict[str, Termios2Abi] = {
    "alpha": Termios2Abi(
        ioctl=IoctlEncoding(size_bits=13, read=2, write=4),
        struct=AlphaTermios2Struct,
        get_request=("T", 0x2A),
        set_request=("T", 0x2B),
        cbaud=0x0000001F,
        bother=0x0000001F,
    ),
    "mips": Termios2Abi(
        ioctl=IoctlEncoding(size_bits=13, read=2, write=4),
        struct=MipsTermios2Struct,
        get_request=("T", 0x2A),
        set_request=("T", 0x2B),
        cbaud=0x0000100F,
        bother=0x00001000,
    ),
    "parisc": Termios2Abi(
        ioctl=IoctlEncoding(size_bits=14, read=1, write=2),
        struct=Termios2Struct,
        get_request=("T", 0x2A),
        set_request=("T", 0x2B),
        cbaud=0x0000100F,
        bother=0x00001000,
    ),
    "ppc": PowerPcTermios2Abi(
        ioctl=IoctlEncoding(size_bits=13, read=2, write=4),
        struct=AlphaTermios2Struct,
        # `TCGETS` and `TCSETS`
        get_request=("t", 19),
        set_request=("t", 20),
        cbaud=0x000000FF,
        bother=0x0000001F,
    ),
    "sparc": Termios2Abi(
        ioctl=IoctlEncoding(size_bits=13, read=2, write=4),
        struct=Termios2Struct,
        get_request=("T", 12),
        set_request=("T", 13),
        cbaud=0x0000100F,
        bother=0x00001000,
    ),
}


def get_termios2_abi(machine: str) -> Termios2Abi:
    """Get the `termios2` ABI for a `uname -m` machine name."""
    for prefix, abi in TERMIOS2_ABIS.items():
        if machine.startswith(prefix):
            return abi

    # Generic fallback
    return Termios2Abi(
        ioctl=IoctlEncoding(size_bits=14, read=2, write=1),
        struct=Termios2Struct,
        get_request=("T", 0x2A),
        set_request=("T", 0x2B),
        cbaud=0x0000100F,
        bother=0x00001000,
    )


TERMIOS2_ABI = get_termios2_abi(os.uname().machine)


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

        abi = TERMIOS2_ABI
        buffer = bytearray(ctypes.sizeof(abi.struct))
        fcntl.ioctl(self._fileno, abi.tcgets2, buffer)
        abi.validate(buffer)

        termios2 = abi.struct.from_buffer(buffer)

        # The POSIX baudrates are stored in the lower bits of `c_cflag`. We clear them.
        termios2.c_cflag &= ~abi.cbaud
        termios2.c_cflag |= abi.bother

        termios2.c_ispeed = baudrate
        termios2.c_ospeed = baudrate

        # The ctypes structure mutates the buffer in place
        LOGGER.debug("Writing termios2 struct: %r", buffer.hex())
        fcntl.ioctl(self._fileno, abi.tcsets2, buffer)

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
