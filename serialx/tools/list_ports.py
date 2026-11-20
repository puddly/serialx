"""Pyserial compatibility module."""

import argparse
from collections.abc import Iterable
import dataclasses
import glob
import os
import re
from urllib.parse import urlparse

from serialx import SerialPortInfo, list_serial_ports


def hwid(info: SerialPortInfo) -> str:
    """Hardware ID of a port in the format of pyserial's `ListPortInfo.hwid`."""
    if info.vid is None or info.pid is None:
        return "n/a"

    serial_number = ""
    if info.serial_number is not None:
        serial_number = f" SER={info.serial_number}"  # codespell:ignore ser

    return f"USB VID:PID={info.vid:04X}:{info.pid:04X}{serial_number}"


def _list_links(
    ports: list[SerialPortInfo], dev_dir: str = "/dev"
) -> list[SerialPortInfo]:
    """Entries for the symlinks in `dev_dir` that point at a listed port."""
    targets = {os.path.realpath(port.resolved_device): port for port in ports}
    listed = {port.device for port in ports}
    links = []

    for path in sorted(glob.glob(os.path.join(dev_dir, "*"))):
        port = targets.get(os.path.realpath(path))
        if os.path.islink(path) and port is not None and path not in listed:
            links.append(dataclasses.replace(port, device=path))

    return links


def comports(include_links: bool = False) -> list[SerialPortInfo]:
    """List serial ports, with `include_links` also the symlinks pointing at them."""
    ports = list_serial_ports()

    if include_links:
        ports += _list_links(ports)

    return ports


def grep(regexp: str, include_links: bool = False) -> Iterable[SerialPortInfo]:
    """Search for ports by device, description or hardware ID."""
    pattern = re.compile(regexp, flags=re.IGNORECASE)

    for info in comports(include_links):
        if (
            pattern.search(str(info.device))
            or pattern.search(info.description)
            or pattern.search(hwid(info))
        ):
            yield info


__all__ = ["comports", "grep", "hwid"]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List serial ports.")
    parser.add_argument(
        "uri",
        nargs="?",
        help="List the ports behind a URI, such as `esphome://`",
    )
    args = parser.parse_args()

    if args.uri is None:
        ports = comports()
    else:
        ports = list_serial_ports(urlparse(args.uri).scheme, path=args.uri)

    for port in ports:
        print(port)  # noqa: T201
