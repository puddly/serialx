"""Darwin serial port implementation."""

from __future__ import annotations

import array
import asyncio
import errno
import fcntl
import logging
import sys
import termios

from serialx.common import SerialPortInfo, register_uri_handler
from serialx.serialx_rust import list_serial_ports_impl

from .serial_extended_posix import ExtendedPosixSerial, ExtendedPosixSerialTransport
from .serial_posix import TermiosSettings

LOGGER = logging.getLogger(__name__)

IOSSIOSPEED = 0x80045402

# When we need to set a non-POSIX baudrate, we set the baudrates to a known default and
# then override
NON_POSIX_FALLBACK_BAUDRATE_CONST = termios.B115200


class DarwinSerial(ExtendedPosixSerial):
    """Darwin serial port implementation."""

    def _set_non_posix_baudrate(self, baudrate: int) -> None:
        """Set the baudrate of the serial port."""
        assert self._fileno is not None

        buffer = array.array("i", [baudrate])

        try:
            fcntl.ioctl(self._fileno, IOSSIOSPEED, buffer)
        except OSError as exc:
            if exc.errno == errno.ENOTTY:
                LOGGER.debug("Device is not a serial port, cannot set baudrate")

    def _write_termios_settings(self, settings: TermiosSettings) -> None:
        if hasattr(termios, f"B{settings.baudrate}"):
            super()._write_termios_settings(settings)
            return

        # `IOSSIOSPEED` only sets the speed, so the other settings are applied first
        self._tcsetattr(settings, NON_POSIX_FALLBACK_BAUDRATE_CONST)

        LOGGER.debug("Setting non-POSIX baudrate %d", settings.baudrate)
        self._set_non_posix_baudrate(settings.baudrate)


class DarwinSerialTransport(ExtendedPosixSerialTransport):
    """Darwin asyncio serial port transport."""

    _serial_cls = DarwinSerial


def darwin_list_serial_ports() -> list[SerialPortInfo]:
    """List available serial ports on macOS."""
    return [
        SerialPortInfo(
            device=port.device,
            resolved_device=port.device,
            vid=port.vid,
            pid=port.pid,
            serial_number=port.serial_number,
            manufacturer=port.manufacturer,
            product=port.product,
            bcd_device=port.bcd_device,
            interface_description=port.interface_description,
            interface_num=port.interface_num,
        )
        for port in list_serial_ports_impl()
    ]


async def async_darwin_list_serial_ports() -> list[SerialPortInfo]:
    """List available serial ports on macOS, async."""
    return await asyncio.to_thread(darwin_list_serial_ports)


if sys.platform == "darwin":
    register_uri_handler(
        scheme="device://",
        unique_scheme="darwin://",
        sync_cls=DarwinSerial,
        async_transport_cls=DarwinSerialTransport,
        list_serial_ports_func=darwin_list_serial_ports,
        async_list_serial_ports_func=async_darwin_list_serial_ports,
        weight=3,
        strip_uri_scheme=True,
    )
