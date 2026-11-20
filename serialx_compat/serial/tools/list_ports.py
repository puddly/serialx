"""Compatibility shim for pyserial's ``serial.tools.list_ports`` module."""

from __future__ import annotations

import argparse
import sys

from serialx.tools.list_ports import comports, grep, hwid

__all__ = ["comports", "grep", "hwid", "main"]


def main(argv: list[str] | None = None) -> None:
    """List serial ports with the options and output of pyserial's tool."""
    parser = argparse.ArgumentParser(description="Serial port enumeration")
    parser.add_argument(
        "regexp", nargs="?", help="only show ports that match this regex"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show more messages"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress all messages"
    )
    parser.add_argument(
        "-1",
        "--only-one",
        action="store_true",
        help="require exactly one matching entry, otherwise error",
    )
    parser.add_argument("-n", type=int, help="only output the N-th entry")
    parser.add_argument(
        "-s",
        "--include-links",
        action="store_true",
        help="include entries that are symlinks to real devices",
    )
    args = parser.parse_args(argv)

    if args.regexp:
        if not args.quiet:
            sys.stderr.write(f"Filtered list with regexp: {args.regexp!r}\n")

        found = list(grep(args.regexp, include_links=args.include_links))
    else:
        found = comports(include_links=args.include_links)

    found.sort(key=lambda port: port.device)

    if args.only_one and len(found) != 1:
        names = ", ".join(port.device for port in found[:2])
        sys.stderr.write(
            f"Error: {len(found) or 'no'} serial ports"
            f"{f' match {args.regexp!r}' if args.regexp else ''}"
            f"{f': {names}' if found else ''}{', ...' if len(found) > 2 else ''}\n"
        )
        sys.exit(1)

    if args.n is not None:
        found = [found[args.n - 1]] if 1 <= args.n <= len(found) else []

    for port in found:
        sys.stdout.write(f"{port.device:20}\n")

        if args.verbose:
            sys.stdout.write(f"    desc: {port.description}\n")
            sys.stdout.write(f"    hwid: {hwid(port)}\n")

    if not args.quiet:
        sys.stderr.write(f"{len(found)} ports found\n" if found else "no ports found\n")


if __name__ == "__main__":
    main()
