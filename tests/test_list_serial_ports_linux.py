"""Tests for Linux serial port listing."""

from __future__ import annotations

import logging
import sys

import pytest

if sys.platform not in ("linux", "darwin"):
    pytest.skip("Linux-only tests", allow_module_level=True)

from pathlib import Path
import re
import shutil
from unittest.mock import patch

from serialx.common import SerialPortInfo
from serialx.platforms import serial_linux
from serialx.platforms.serial_linux import linux_list_serial_ports
from serialx.udev import udev_serial_by_id_stem
from tests.umockdev_loader import load_umockdev

DATA_DIR = Path(__file__).parent / "data" / "linux"


def _list_ports(tmp_path: Path, dump_name: str) -> tuple[Path, list[SerialPortInfo]]:
    """Replay a dump and return (dev_root, sorted ports)."""
    sys_root, dev_root = load_umockdev(tmp_path, DATA_DIR / dump_name)
    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()
    return dev_root, sorted(ports, key=lambda p: Path(p.resolved_device).name)


def test_replay_debian_12_kernel_6_1(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-6.10 kernel: PnP 16550A on `pnp` plus serial8250 placeholders."""
    with caplog.at_level(logging.WARNING):
        dev_root, ports = _list_ports(tmp_path, "debian-12-6.1.umockdev")
    assert "Unknown serial device subsystem" not in caplog.text

    assert ports == [
        SerialPortInfo(
            device=str(dev_root / "ttyS0"),
            resolved_device=str(dev_root / "ttyS0"),
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
            bcd_device=None,
            interface_description=None,
            interface_num=None,
        ),
    ]


def test_replay_debian_13_kernel_6_12(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Kernel 6.12 with ttyUSB, CDC ACM, PnP ttyS0, and `serial-base` placeholders."""
    with caplog.at_level(logging.WARNING):
        dev_root, ports = _list_ports(tmp_path, "debian-13-6.12.umockdev")
    assert "Unknown serial device subsystem" not in caplog.text

    by_id = dev_root / "serial/by-id"
    assert ports == [
        SerialPortInfo(
            device=str(by_id / "usb-Nabu_Casa_ZBT-2_10B41DE589E4-if00"),
            resolved_device=str(dev_root / "ttyACM0"),
            vid=0x303A,
            pid=0x4001,
            serial_number="10B41DE589E4",
            manufacturer="Nabu Casa",
            product="ZBT-2",
            bcd_device=0x0100,
            interface_description="Nabu Casa ZBT-2",
            interface_num=0,
        ),
        SerialPortInfo(
            device=str(dev_root / "ttyS0"),
            resolved_device=str(dev_root / "ttyS0"),
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
            bcd_device=None,
            interface_description=None,
            interface_num=None,
        ),
        SerialPortInfo(
            device=str(
                by_id
                / "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_41b06ea8-if00-port0"
            ),
            resolved_device=str(dev_root / "ttyUSB0"),
            vid=0x10C4,
            pid=0xEA60,
            serial_number="41b06ea8",
            manufacturer="Silicon Labs",
            product="CP2102 USB to UART Bridge Controller",
            bcd_device=0x0100,
            interface_description="CP2102 USB to UART Bridge Controller",
            interface_num=0,
        ),
        SerialPortInfo(
            device=str(
                by_id
                / "usb-Nabu_Casa_Home_Assistant_Connect_ZBT-1_a28a310e2bedec118f3d4540ad51a8b2-if00-port0"
            ),
            resolved_device=str(dev_root / "ttyUSB1"),
            vid=0x10C4,
            pid=0xEA60,
            serial_number="a28a310e2bedec118f3d4540ad51a8b2",
            manufacturer="Nabu Casa",
            product="Home Assistant Connect ZBT-1",
            bcd_device=0x0100,
            interface_description=None,
            interface_num=0,
        ),
        SerialPortInfo(
            device=str(by_id / "usb-FTDI_FT232R_USB_UART_A5069RR4-if00-port0"),
            resolved_device=str(dev_root / "ttyUSB2"),
            vid=0x0403,
            pid=0x6001,
            serial_number="A5069RR4",
            manufacturer="FTDI",
            product="FT232R USB UART",
            bcd_device=0x0600,
            interface_description="FT232R USB UART",
            interface_num=0,
        ),
        SerialPortInfo(
            device=str(dev_root / "ttyUSB3"),
            resolved_device=str(dev_root / "ttyUSB3"),
            vid=0x0403,
            pid=0x6001,
            serial_number="A5069RR4",
            manufacturer="FTDI",
            product="FT232R USB UART",
            bcd_device=0x0600,
            interface_description="FT232R USB UART",
            interface_num=0,
        ),
        SerialPortInfo(
            device=str(by_id / "usb-FTDI_FT232R_USB_UART_rutabaga-if00-port0"),
            resolved_device=str(dev_root / "ttyUSB4"),
            vid=0x0403,
            pid=0x6001,
            serial_number="rutabaga",
            manufacturer="FTDI",
            product="FT232R USB UART",
            bcd_device=0x0600,
            interface_description="FT232R USB UART",
            interface_num=0,
        ),
        SerialPortInfo(
            device=str(
                by_id
                / "usb-Prolific_Technology_Inc._USB-Serial_Controller_DSDCb147613-if00-port0"
            ),
            resolved_device=str(dev_root / "ttyUSB5"),
            vid=0x067B,
            pid=0x23A3,
            serial_number="DSDCb147613",
            manufacturer="Prolific Technology Inc. ",
            product="USB-Serial Controller ",
            bcd_device=0x0605,
            interface_description=None,
            interface_num=0,
        ),
    ]


def test_replay_haos_x86_kernel_6_12(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """HAOS x86 with two Nabu Casa ZBT-2 CDC ACM dongles."""
    with caplog.at_level(logging.WARNING):
        dev_root, ports = _list_ports(tmp_path, "haos-x86-6.12.umockdev")
    assert "Unknown serial device subsystem" not in caplog.text

    by_id = dev_root / "serial/by-id"
    assert ports == [
        SerialPortInfo(
            device=str(by_id / "usb-Nabu_Casa_ZBT-2_10B41DE589FC-if00"),
            resolved_device=str(dev_root / "ttyACM0"),
            vid=0x303A,
            pid=0x4001,
            serial_number="10B41DE589FC",
            manufacturer="Nabu Casa",
            product="ZBT-2",
            bcd_device=0x0101,
            interface_description="Nabu Casa ZBT-2",
            interface_num=0,
        ),
        SerialPortInfo(
            device=str(by_id / "usb-Nabu_Casa_ZBT-2_10B41DE58A2C-if00"),
            resolved_device=str(dev_root / "ttyACM1"),
            vid=0x303A,
            pid=0x4001,
            serial_number="10B41DE58A2C",
            manufacturer="Nabu Casa",
            product="ZBT-2",
            bcd_device=0x0100,
            interface_description="Nabu Casa ZBT-2",
            interface_num=0,
        ),
    ]


def test_replay_haos_yellow_kernel_6_6(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """HAOS Yellow kernel 6.6: PL011 UARTs on both `platform` and `amba`."""
    with caplog.at_level(logging.WARNING):
        dev_root, ports = _list_ports(tmp_path, "haos-yellow-6.6.umockdev")
    assert "Unknown serial device subsystem" not in caplog.text

    assert ports == [
        SerialPortInfo(
            device=str(dev_root / name),
            resolved_device=str(dev_root / name),
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
            bcd_device=None,
            interface_description=None,
            interface_num=None,
        )
        for name in ("ttyAMA0", "ttyAMA1", "ttyAMA10", "ttyAMA2")
    ]


def test_list_serial_ports_no_sysfs(tmp_path: Path) -> None:
    """Test listing serial ports when /sys/class/tty doesn't exist."""
    sys_root = tmp_path / "sys"
    dev_root = tmp_path / "dev"
    sys_root.mkdir()
    dev_root.mkdir()

    # Don't create /sys/class/tty at all
    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()

    assert ports == []


def test_list_serial_ports_no_by_id_dir(tmp_path: Path) -> None:
    """Test listing serial ports when /dev/serial/by-id doesn't exist."""
    sys_root, dev_root = load_umockdev(tmp_path, DATA_DIR / "debian-13-6.12.umockdev")

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        baseline = linux_list_serial_ports()

    # Sanity-check the dump actually exercises by-id resolution.
    assert any(p.device != p.resolved_device for p in baseline)

    # Wipe the by-id tree; ports should still be enumerated, just without aliases.
    shutil.rmtree(dev_root / "serial/by-id")

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()

    assert len(ports) == len(baseline)
    for p in ports:
        assert p.device == p.resolved_device


def test_list_serial_ports_device_disappears_during_scan(tmp_path: Path) -> None:
    """Test that a USB device disappearing mid-scan is handled gracefully."""
    sys_root, dev_root = load_umockdev(tmp_path, DATA_DIR / "debian-13-6.12.umockdev")

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        baseline = linux_list_serial_ports()
    baseline_names = {Path(p.resolved_device).name for p in baseline}
    assert "ttyUSB0" in baseline_names

    # Simulate ttyUSB0's USB device (the CP2102 at usb1/1-3) disappearing by
    # removing its idVendor attribute mid-scan.
    usb_device = sys_root / "devices/pci0000:00/0000:00:1e.0/0000:01:1b.0/usb1/1-3"
    (usb_device / "idVendor").unlink()

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()

    names = {Path(p.resolved_device).name for p in ports}
    assert names == baseline_names - {"ttyUSB0"}


def test_list_serial_ports_cdc_acm_device_disappears(tmp_path: Path) -> None:
    """Test that a CDC ACM device disappearing mid-scan is handled gracefully."""
    sys_root, dev_root = load_umockdev(tmp_path, DATA_DIR / "debian-13-6.12.umockdev")

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        baseline = linux_list_serial_ports()
    baseline_names = {Path(p.resolved_device).name for p in baseline}
    assert "ttyACM0" in baseline_names

    # Simulate ttyACM0's underlying USB device (the ZBT-2 at usb1/1-6)
    # disappearing mid-scan.
    usb_device = sys_root / "devices/pci0000:00/0000:00:1e.0/0000:01:1b.0/usb1/1-6"
    (usb_device / "idVendor").unlink()

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()

    names = {Path(p.resolved_device).name for p in ports}
    assert names == baseline_names - {"ttyACM0"}


def test_list_serial_ports_native_device_disappears(tmp_path: Path) -> None:
    """Test that a native serial device disappearing mid-scan is handled gracefully."""
    sys_root, dev_root = load_umockdev(tmp_path, DATA_DIR / "haos-yellow-6.6.umockdev")

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        baseline = linux_list_serial_ports()
    baseline_names = {Path(p.resolved_device).name for p in baseline}
    assert "ttyAMA0" in baseline_names

    # Simulate ttyAMA0 disappearing by deleting its `type` file mid-scan.
    tty_dir = (
        sys_root / "devices/platform/axi/1000120000.pcie/1f00030000.serial/tty/ttyAMA0"
    )
    (tty_dir / "type").unlink()

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()

    names = {Path(p.resolved_device).name for p in ports}
    assert names == baseline_names - {"ttyAMA0"}


def test_list_serial_ports_unknown_subsystem(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that devices with unknown subsystems are skipped with a warning."""
    sys_root, dev_root = load_umockdev(tmp_path, DATA_DIR / "haos-yellow-6.6.umockdev")

    # Repoint ttyAMA0's parent at a fabricated bus name that the code doesn't
    # recognize.
    parent = sys_root / "devices/platform/axi/1000120000.pcie/1f00030000.serial"
    (parent / "subsystem").unlink()
    (sys_root / "bus/some-unknown-bus").mkdir(parents=True)
    (parent / "subsystem").symlink_to(sys_root / "bus/some-unknown-bus")

    with (
        caplog.at_level(logging.WARNING),
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()

    names = {Path(p.resolved_device).name for p in ports}
    assert "ttyAMA0" not in names
    assert "Unknown serial device subsystem 'some-unknown-bus'" in caplog.text


def test_list_serial_ports_missing_subsystem(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Devices whose parent has no `subsystem` symlink are silently skipped."""
    sys_root, dev_root = load_umockdev(tmp_path, DATA_DIR / "haos-yellow-6.6.umockdev")

    # Drop ttyAMA0's parent subsystem symlink so resolve(strict=True) raises.
    parent = sys_root / "devices/platform/axi/1000120000.pcie/1f00030000.serial"
    (parent / "subsystem").unlink()

    with (
        caplog.at_level(logging.WARNING),
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()

    names = {Path(p.resolved_device).name for p in ports}
    assert "ttyAMA0" not in names
    assert not caplog.text


def test_replay_github_actions_ubuntu_24_04_kernel_6_17(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """GitHub Actions Azure runner: 2 PnP 16550A + 30 serial8250 placeholders."""
    with caplog.at_level(logging.WARNING):
        dev_root, ports = _list_ports(
            tmp_path, "github-actions-ubuntu-24.04-6.17.umockdev"
        )
    assert "Unknown serial device subsystem" not in caplog.text

    assert ports == [
        SerialPortInfo(
            device=str(dev_root / "ttyS0"),
            resolved_device=str(dev_root / "ttyS0"),
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
            bcd_device=None,
            interface_description=None,
            interface_num=None,
        ),
        SerialPortInfo(
            device=str(dev_root / "ttyS1"),
            resolved_device=str(dev_root / "ttyS1"),
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
            bcd_device=None,
            interface_description=None,
            interface_num=None,
        ),
    ]


def test_list_serial_ports_empty(tmp_path: Path) -> None:
    """Test that listing serial ports still works when there are no ports."""
    sys_root = tmp_path / "sys"
    dev_root = tmp_path / "dev"
    sys_root.mkdir()
    dev_root.mkdir()

    with (
        patch.object(serial_linux, "SYS_ROOT", sys_root),
        patch.object(serial_linux, "DEV_ROOT", dev_root),
    ):
        ports = linux_list_serial_ports()

    assert ports == []


def _recorded_by_id_links(dump: Path) -> dict[str, set[str]]:
    """Map each device node in a dump to the by-id links udev recorded for it."""
    links: dict[str, set[str]] = {}

    for paragraph in dump.read_text().split("\n\n"):
        lines = paragraph.splitlines()
        nodes = [line[3:] for line in lines if line.startswith("N: ")]
        if not nodes:
            continue

        links[nodes[0]] = {
            line.removeprefix("S: serial/by-id/")
            for line in lines
            if line.startswith("S: serial/by-id/")
        }

    return links


@pytest.mark.parametrize(
    "dump", sorted(DATA_DIR.glob("*.umockdev")), ids=lambda dump: dump.stem
)
def test_udev_serial_by_id_stem_matches_recorded_links(
    tmp_path: Path, dump: Path
) -> None:
    """The stem matches the link udev created for every recorded port."""
    _, ports = _list_ports(tmp_path, dump.name)
    links = _recorded_by_id_links(dump)

    for port in ports:
        expected = {
            re.sub(r"-port\d+$", "", link)
            for link in links[Path(port.resolved_device).name]
        }
        stem = udev_serial_by_id_stem(port)

        assert expected == (set() if stem is None else {stem})


def test_read_optional_sysfs_keeps_newlines(tmp_path: Path) -> None:
    """Descriptor bytes are read as-is, with only the kernel's newline removed."""
    attr = tmp_path / "manufacturer"
    attr.write_bytes(b"Nabu\r\nCasa\r\n")

    assert serial_linux._read_optional_sysfs(attr) == "Nabu\r\nCasa\r"
