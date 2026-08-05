"""Individual platform implementations."""

from contextlib import suppress
from typing import TYPE_CHECKING

from ..common import get_uri_handler
from . import (
    serial_rfc2217,  # noqa: F401
    serial_socket,  # noqa: F401
)

with suppress(ImportError):
    from . import serial_win32  # noqa: F401

with suppress(ImportError):
    from . import serial_posix  # noqa: F401

with suppress(ImportError):
    from . import serial_extended_posix  # noqa: F401

with suppress(ImportError):
    from . import serial_linux  # noqa: F401

with suppress(ImportError):
    from . import serial_darwin  # noqa: F401

with suppress(ImportError):
    from . import serial_freebsd  # noqa: F401

with suppress(ImportError):
    from . import serial_esphome  # noqa: F401

with suppress(ImportError):
    from . import serial_pyodide  # noqa: F401

if TYPE_CHECKING:
    from ..common import BaseSerial as Serial, BaseSerialTransport as SerialTransport
else:
    _handler = get_uri_handler("device://")
    Serial = _handler.sync_cls
    SerialTransport = _handler.async_transport_cls

__all__ = [
    "Serial",
    "SerialTransport",
]
