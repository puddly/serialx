"""Tests for udev naming."""

from __future__ import annotations

from typing import Any

import pytest

from serialx import SerialPortInfo, udev_serial_by_id_stem


def _usb_port(**overrides: Any) -> SerialPortInfo:
    fields: dict[str, Any] = {
        "device": "/dev/ttyACM0",
        "resolved_device": "/dev/ttyACM0",
        "vid": 0x303A,
        "pid": 0x4001,
        "serial_number": "10B41DE589E4",
        "manufacturer": "Nabu Casa",
        "product": "ZBT-2",
        "bcd_device": 0x0100,
        "interface_description": "Nabu Casa ZBT-2",
        "interface_num": 0,
    }
    fields.update(overrides)
    return SerialPortInfo(**fields)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "usb-Nabu_Casa_ZBT-2_10B41DE589E4-if00"),
        ({"interface_num": 10}, "usb-Nabu_Casa_ZBT-2_10B41DE589E4-if0a"),
        # Missing strings fall back to the IDs as sysfs spells them
        (
            {"manufacturer": None, "product": None, "serial_number": None},
            "usb-303a_4001-if00",
        ),
        # Whitespace is trimmed and each run collapses to one underscore
        (
            {"manufacturer": " \t Nabu   Casa\t\n"},
            "usb-Nabu_Casa_ZBT-2_10B41DE589E4-if00",
        ),
        # Leading `\v` and `\f` are not skipped, only collapsed
        ({"manufacturer": "\v Nabu"}, "usb-_Nabu_ZBT-2_10B41DE589E4-if00"),
        # Characters outside the allowed set become underscores
        ({"product": "ZBT-2 (R)/x"}, "usb-Nabu_Casa_ZBT-2__R__x_10B41DE589E4-if00"),
        # Hex escapes and valid UTF-8 are kept, a lone backslash is not
        ({"product": "a\\x2fb\\c"}, "usb-Nabu_Casa_a\\x2fb_c_10B41DE589E4-if00"),
        ({"manufacturer": "Café"}, "usb-Café_ZBT-2_10B41DE589E4-if00"),
        # Noncharacters are replaced byte by byte
        ({"manufacturer": "a\ufdd0b"}, "usb-a___b_ZBT-2_10B41DE589E4-if00"),
        ({"manufacturer": "a\uffffb"}, "usb-a___b_ZBT-2_10B41DE589E4-if00"),
        # Vendor and model are cut at 63 bytes, splitting a character
        ({"manufacturer": "é" * 40}, f"usb-{'é' * 31}__ZBT-2_10B41DE589E4-if00"),
        # Serial numbers Windows rejects are dropped
        ({"serial_number": "12,34"}, "usb-Nabu_Casa_ZBT-2-if00"),
        ({"serial_number": "12\t34"}, "usb-Nabu_Casa_ZBT-2-if00"),
        ({"serial_number": "Café"}, "usb-Nabu_Casa_ZBT-2-if00"),
        # DEL passes that check but is still replaced
        ({"serial_number": "12\x7f34"}, "usb-Nabu_Casa_ZBT-2_12_34-if00"),
        ({"serial_number": "   "}, "usb-Nabu_Casa_ZBT-2-if00"),
        # Trailing newlines are stripped when udev reads the attribute
        ({"serial_number": "1234\r\n"}, "usb-Nabu_Casa_ZBT-2_1234-if00"),
        # Values end at the first NUL
        ({"manufacturer": "AB\x00CD"}, "usb-AB_ZBT-2_10B41DE589E4-if00"),
        ({"serial_number": "12\x0034"}, "usb-Nabu_Casa_ZBT-2_12-if00"),
        # `ID_SERIAL` is cut at 255 bytes
        (
            {"manufacturer": "V", "product": "M", "serial_number": "A" * 300},
            f"usb-V_M_{'A' * 251}-if00",
        ),
        (
            {"manufacturer": "V", "product": "M", "serial_number": None},
            "usb-V_M-if00",
        ),
    ],
)
def test_udev_serial_by_id_stem(overrides: dict[str, Any], expected: str) -> None:
    """The stem follows the rules of udev's `usb_id` builtin and `60-serial.rules`."""
    assert udev_serial_by_id_stem(_usb_port(**overrides)) == expected


@pytest.mark.parametrize("missing", ["vid", "pid", "interface_num"])
def test_udev_serial_by_id_stem_not_usb(missing: str) -> None:
    """No by-id link exists without a USB bus or interface number."""
    assert udev_serial_by_id_stem(_usb_port(**{missing: None})) is None
