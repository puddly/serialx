"""Linux udev naming, reproduced for ports that udev never sees."""

from __future__ import annotations

import re
import string
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .common import SerialPortInfo

# Ported from systemd's `rules.d/60-serial.rules` and `src/udev/udev-builtin-usb_id.c`.
# `udev_replace_whitespace` skips leading `WHITESPACE` but collapses `isspace()` runs.
UDEV_LEADING_WHITESPACE = b" \t\n\r"
UDEV_WHITESPACE = b" \t\n\v\f\r"
UDEV_WHITESPACE_RUN = re.compile(rb"[ \t\n\v\f\r]+")
UDEV_ALLOWED_CHARS = frozenset(
    (string.ascii_letters + string.digits + "#+-.:=@_").encode("ascii")
)


def _udev_sysattr(value: str) -> bytes:
    """Encode a descriptor string the way `sd_device_get_sysattr_value` reads it."""
    # udev handles the value as a C string, so it ends at the first NUL
    return value.encode("utf-8").partition(b"\0")[0].rstrip(b"\r\n")


def _udev_replace_whitespace(value: bytes, size: int) -> bytes:
    """Port of `udev_replace_whitespace`, which reads at most `size` bytes."""
    trimmed = value[:size].lstrip(UDEV_LEADING_WHITESPACE).rstrip(UDEV_WHITESPACE)
    return UDEV_WHITESPACE_RUN.sub(b"_", trimmed)


def _udev_utf8_char_len(value: bytes | bytearray, index: int) -> int:
    """Length of the valid multibyte UTF-8 character at `index`, or 0."""
    lead = value[index]

    # 5 and 6 byte sequences always decode outside of the Unicode range
    if lead & 0xE0 == 0xC0:
        length = 2
    elif lead & 0xF0 == 0xE0:
        length = 3
    elif lead & 0xF8 == 0xF0:
        length = 4
    else:
        return 0

    # Python rejects overlong forms, surrogates, and code points past U+10FFFF
    try:
        codepoint = ord(value[index : index + length].decode("utf-8"))
    except UnicodeDecodeError:
        return 0

    # Python accepts noncharacters, `unichar_is_valid` does not
    if 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFE == 0xFFFE:
        return 0

    return length


def _udev_replace_chars(value: bytes) -> bytes:
    """Port of `udev_replace_chars` with no extra allowed characters."""
    result = bytearray(value)
    i = 0

    while i < len(result):
        if result[i] in UDEV_ALLOWED_CHARS:
            i += 1
        elif result[i : i + 2] == b"\\x":
            i += 2
        elif (length := _udev_utf8_char_len(result, i)) > 1:
            i += length
        else:
            result[i] = ord("_")
            i += 1

    return bytes(result)


def udev_serial_by_id_stem(port: SerialPortInfo) -> str | None:
    """Compute the `/dev/serial/by-id/` link name udev gives a USB port, without `-portN`.

    udev appends `-port{N}` only when a `usb-serial` driver binds the port, not for CDC
    ACM. Which one binds cannot be known from the port, so it is left out.
    """
    if port.vid is None or port.pid is None or port.interface_num is None:
        return None

    vendor = _udev_sysattr(
        port.manufacturer if port.manufacturer is not None else f"{port.vid:04x}"
    )
    model = _udev_sysattr(
        port.product if port.product is not None else f"{port.pid:04x}"
    )
    id_serial = (
        _udev_replace_chars(_udev_replace_whitespace(vendor, 63))
        + b"_"
        + _udev_replace_chars(_udev_replace_whitespace(model, 63))
    )

    if port.serial_number is not None:
        serial = _udev_sysattr(port.serial_number)

        # Serial numbers with characters Windows rejects are dropped entirely
        if all(0x20 <= c <= 0x7F and c != ord(",") for c in serial):
            serial = _udev_replace_chars(_udev_replace_whitespace(serial, 511))

            if serial:
                id_serial += b"_" + serial

    # `ID_SERIAL` is built in a 256 byte buffer
    return f"usb-{id_serial[:255].decode('utf-8')}-if{port.interface_num:02x}"
