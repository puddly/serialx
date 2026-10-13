"""Pyserial compatibility module."""

import argparse
from collections.abc import Iterable
import re
from urllib.parse import urlparse

from serialx import SerialPortInfo, list_serial_ports


def grep(regexp: str, include_links: bool = False) -> Iterable[SerialPortInfo]:
    """Search for ports using a regular expression."""
    pattern = re.compile(regexp, flags=re.IGNORECASE)

    for info in list_serial_ports():
        if pattern.search(str(info.device)) or pattern.search(info.product or ""):
            yield info


comports = list_serial_ports
__all__ = ["comports", "grep"]

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
