"""Tests for the pyserial compatibility ``serial.tools.list_ports`` module."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest
import serial.tools.list_ports

import serialx


def _port(device: str, **kwargs: object) -> serialx.SerialPortInfo:
    fields: dict[str, object] = {
        "resolved_device": device,
        "vid": None,
        "pid": None,
        "serial_number": None,
        "manufacturer": None,
        "product": None,
        "bcd_device": None,
        "interface_description": None,
        "interface_num": None,
    }
    fields.update(kwargs)
    return serialx.SerialPortInfo(device=device, **fields)  # type: ignore[arg-type]


def test_list_ports_main_matches_pyserial_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `list_ports` tool takes pyserial's options and prints its output."""
    ports = [
        _port("/dev/ttyUSB0", vid=0x0403, pid=0x6001, product="FT232R USB UART"),
        _port("/dev/ttyS0"),
    ]

    with patch("serialx.tools.list_ports.list_serial_ports", return_value=ports):
        serial.tools.list_ports.main([])
        out, err = capsys.readouterr()
        assert out == f"{'/dev/ttyS0':20}\n{'/dev/ttyUSB0':20}\n"
        assert err == "2 ports found\n"

        serial.tools.list_ports.main(["-v", "-q", "0403:6001"])
        out, err = capsys.readouterr()
        assert out == (
            f"{'/dev/ttyUSB0':20}\n"
            "    desc: FT232R USB UART\n"
            "    hwid: USB VID:PID=0403:6001\n"
        )
        assert err == ""

        with pytest.raises(SystemExit) as raised:
            serial.tools.list_ports.main(["-1"])

        assert raised.value.code == 1
        assert capsys.readouterr().err.startswith("Error: 2 serial ports")


def test_list_ports_runs_as_module() -> None:
    """`python -m serial.tools.list_ports` prints pyserial's summary line."""
    result = subprocess.run(
        [sys.executable, "-m", "serial.tools.list_ports"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    assert result.stderr.endswith(("ports found\n", "no ports found\n"))
