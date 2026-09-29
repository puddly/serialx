"""Pytest configuration for serialx tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
import contextlib
import dataclasses
import sys
import urllib.parse

import pytest

try:
    from aioesphomeapi.timezone import get_local_timezone
except ImportError:
    get_local_timezone = None  # type: ignore[assignment]

from serialx.common import get_uri_handler
from tests.common import (
    ESPHOME_HOST_BINARY,
    HUB4COM_BINARY,
    SER2NET_BINARY,
    SERIAL_PAIR_DEFAULT_QUIRKS,
    SOCAT_BINARY,
    SerialBackend,
    SerialPair,
    SerialQuirk,
    UnresolvedSerialPair,
    check_fd_leaks,
    create_adapter_pair,
    create_esphome_pair,
    create_hub4com_pair,
    create_pyodide_pair,
    create_ser2net_pair,
    create_socat_pair,
)
from tests.socket_relay import create_socket_pair


def _get_forced_posix_uri_schemes() -> list[str]:
    """Get extra POSIX URI schemes to test on this platform.

    On Linux (or any platform with a deeper class hierarchy), we also test the
    generic POSIX and extended POSIX backends by addressing the underlying
    adapter with an explicit URI scheme so the registry dispatches to those
    classes instead of the platform-native handler.
    """
    try:
        import termios  # noqa: F401, PLC0415
    except ImportError:
        return []

    from serialx.platforms.serial_extended_posix import (  # noqa: PLC0415
        is_extended_posix,
    )

    result = ["posix://"]

    if is_extended_posix():
        result.append("extended-posix://")

    return result


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "xdist_group(name): group tests for pytest-xdist parallel execution control",
    )
    config.addinivalue_line(
        "markers",
        "skip_quirks(*quirks): skip test when serial_pair exposes any listed quirk",
    )
    config.addinivalue_line(
        "markers",
        "esphome_api(major, minor): skip when --esphome-api-version is older than this",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command line options for serial adapter configuration."""
    parser.addoption(
        "--adapter-pair",
        action="append",
        default=[],
        help=(
            "Pair of serial endpoints in format LEFT,RIGHT[,FLAG...] "
            "(e.g. /dev/ttyUSB0,/dev/ttyUSB1,no-rts-cts "
            "or rfc2217://127.0.0.1:5001,"
            "rfc2217://127.0.0.1:5002,no-write-timeout)"
        ),
    )
    parser.addoption(
        "--esphome-api-version",
        default=None,
        help="API version the ESPHome host daemon is expected to report (e.g. 1.16)",
    )


def parse_esphome_api_version(config: pytest.Config) -> tuple[int, int] | None:
    """Return the expected ESPHome API version from `--esphome-api-version`, if any."""
    value = config.getoption("--esphome-api-version")
    if value is None:
        return None

    major, minor = value.split(".")
    return int(major), int(minor)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip tests marked `esphome_api` above the expected daemon API version."""
    expected = parse_esphome_api_version(config)
    if expected is None:
        return

    for item in items:
        marker = item.get_closest_marker("esphome_api")
        if marker is not None and tuple(marker.args) > expected:
            item.add_marker(
                pytest.mark.skip(
                    reason=f"needs ESPHome API {marker.args}, have {expected}"
                )
            )


@pytest.fixture
def esphome_api_version(request: pytest.FixtureRequest) -> tuple[int, int]:
    """Return the API version the ESPHome host daemon is expected to report."""
    expected = parse_esphome_api_version(request.config)
    if expected is None:
        pytest.skip("--esphome-api-version not given")

    return expected


def _get_endpoint_backend(path: str) -> SerialBackend:
    """Classify a single endpoint into a backend family."""
    lower_path = path.lower()
    if lower_path.startswith("rfc2217://"):
        return SerialBackend.RFC2217
    if lower_path.startswith("socket://"):
        return SerialBackend.SOCKET
    if lower_path.startswith("esphome://"):
        return SerialBackend.ESPHOME
    return SerialBackend.ADAPTER


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize tests based on available backends."""

    if sys.platform == "emscripten":
        # Under Pyodide the only available backend is PYODIDE; skip the rest
        # so tests don't try to spawn socat/ser2net/tcp servers that can't run.
        if "serial_pair" in metafunc.fixturenames:
            spec = UnresolvedSerialPair(
                backends=(SerialBackend.PYODIDE,),
                left=None,
                right=None,
                original_left="gen",
                original_right="gen",
                quirks=SERIAL_PAIR_DEFAULT_QUIRKS[SerialBackend.PYODIDE],
            )
            metafunc.parametrize(
                "serial_pair",
                [pytest.param(spec, id="PYODIDE")],
                indirect=True,
            )
        return

    adapters = []

    # Physical adapters passed in with a CLI flag
    for pair in metafunc.config.getoption("--adapter-pair"):
        parts = [part.strip() for part in pair.split(",")]
        expected_format = "LEFT,RIGHT[,FLAG...]"

        if len(parts) < 2:
            raise ValueError(
                f"Invalid adapter pair format: {pair}. Expected {expected_format}"
            )

        left, right, *raw_flags = parts

        if not left or not right:
            raise ValueError(
                f"Invalid adapter pair format: {pair}. Expected {expected_format}"
            )

        left_backend = _get_endpoint_backend(left)
        right_backend = _get_endpoint_backend(right)

        adapters.append(
            UnresolvedSerialPair(
                backends=(SerialBackend.ADAPTER,),
                left=left,
                right=right,
                original_left=left,
                original_right=right,
                quirks=(
                    SERIAL_PAIR_DEFAULT_QUIRKS[left_backend]
                    | SERIAL_PAIR_DEFAULT_QUIRKS[right_backend]
                    | frozenset({SerialQuirk(raw_flag) for raw_flag in raw_flags})
                ),
            )
        )

    if "serial_pair" in metafunc.fixturenames:
        # `socat` can always be used to create virtual serial port pairs
        if SOCAT_BINARY:
            adapters.append(
                UnresolvedSerialPair(
                    backends=(SerialBackend.SOCAT,),
                    left=None,
                    right=None,
                    original_left="gen",
                    original_right="gen",
                    quirks=SERIAL_PAIR_DEFAULT_QUIRKS[SerialBackend.SOCAT],
                )
            )

        # Transport chains build on top of adapters
        specs = []

        # We can always create a TCP server
        specs.append(
            UnresolvedSerialPair(
                backends=(SerialBackend.SOCKET,),
                left=None,
                right=None,
                original_left="gen",
                original_right="gen",
                quirks=SERIAL_PAIR_DEFAULT_QUIRKS[SerialBackend.SOCKET],
            )
        )

        for adapter_spec in adapters:
            specs.append(adapter_spec)

            if SER2NET_BINARY is not None:
                ser2net_spec = adapter_spec.chain(
                    SerialBackend.SER2NET, SerialBackend.RFC2217
                )

                specs.append(
                    dataclasses.replace(
                        ser2net_spec,
                        # ser2net only polls modem line states every second...
                        modem_line_propagation_delay=1.1,
                    )
                )

            if HUB4COM_BINARY is not None:
                specs.append(
                    adapter_spec.chain(SerialBackend.HUB4COM, SerialBackend.RFC2217)
                )

            if sys.version_info >= (3, 11) and ESPHOME_HOST_BINARY is not None:
                specs.append(
                    dataclasses.replace(
                        adapter_spec.chain(SerialBackend.ESPHOME_HOST),
                        # The host build pushes a write to the PTY with a single
                        # blocking write(2); anything larger than the PTY plus the
                        # socat bridge buffers (see create_socat_pair) blocks the
                        # single-threaded daemon before it can drain them.
                        max_drain_payload=2048,
                    )
                )

        # For POSIX, we should test base classes on platforms that extend them
        for uri_scheme in _get_forced_posix_uri_schemes():
            for adapter in adapters:
                quirks = adapter.quirks

                # The generic POSIX backend rejects hardware flow control outright
                if uri_scheme == "posix://":
                    quirks |= {SerialQuirk.NO_RTS_CTS, SerialQuirk.NO_DTR_DSR}

                specs.append(
                    dataclasses.replace(adapter, uri_scheme=uri_scheme, quirks=quirks)
                )

        # Build the pytest parameter groups to limit concurrency to underlying resources
        params = []

        for spec in specs:
            marks = []

            if spec.left is not None:
                marks.append(pytest.mark.xdist_group(name=spec.left))

            if spec.right is not None:
                marks.append(pytest.mark.xdist_group(name=spec.right))

            backends = [b.name for b in spec.backends]

            if spec.original_left != "gen" and spec.original_right != "gen":
                backends.append(f"{spec.original_left}-{spec.original_right}")

            param_id = "+".join(backends)

            if spec.uri_scheme:
                param_id += f"({spec.uri_scheme})"

            params.append(pytest.param(spec, marks=marks, id=param_id))

        # Finally, emit tests
        metafunc.parametrize("serial_pair", params, indirect=True)


@pytest.fixture
def serial_pair(request: pytest.FixtureRequest) -> Generator[SerialPair]:
    """Fixture for a connected serial port pair with the provided backend."""
    spec: UnresolvedSerialPair = request.param

    for marker in request.node.iter_markers("skip_quirks"):
        if set(marker.args) & set(spec.quirks):
            pytest.skip(f"Skipping, blocked quirks {marker.args} exist in spec {spec}")

    # Now we create the chained backends
    stack = contextlib.ExitStack()
    left = spec.left
    right = spec.right
    unplug_left_graceful: Callable[[], None] | None = None
    unplug_left_abrupt: Callable[[], None] | None = None

    for backend in spec.backends[::-1]:
        match backend:
            # Synthetic backends don't have an underlying serial port
            case SerialBackend.SOCAT:
                assert left is None and right is None
                left, right, unplug_left_graceful, unplug_left_abrupt = (
                    stack.enter_context(create_socat_pair())
                )

            case SerialBackend.SOCKET:
                assert left is None and right is None
                left, right, unplug_left_graceful, unplug_left_abrupt = (
                    stack.enter_context(create_socket_pair())
                )

            case SerialBackend.PYODIDE:
                assert left is None and right is None
                left, right = stack.enter_context(create_pyodide_pair())

            # Wrapped backends require one
            case SerialBackend.ESPHOME_HOST:
                assert left is not None and right is not None
                # The esphome daemon sits between the client and the inner PTY,
                # so an inner unplug doesn't surface as a client disconnect;
                # killing the daemon closes the API connection instead.
                left, right, unplug_left_graceful, unplug_left_abrupt = (
                    stack.enter_context(create_esphome_pair(left, right))
                )

            case SerialBackend.SER2NET:
                assert left is not None and right is not None
                left, right, unplug_left_graceful, unplug_left_abrupt = (
                    stack.enter_context(create_ser2net_pair(left, right))
                )

            case SerialBackend.HUB4COM:
                assert left is not None and right is not None
                left, right = stack.enter_context(create_hub4com_pair(left, right))

            case SerialBackend.RFC2217:
                # This backend doesn't require creating anything but introduces its own
                # quirks
                pass

            case SerialBackend.ADAPTER:
                # This doesn't actually create physical adapters, it just has fixes
                # for OS-specific adapter quirks
                assert left is not None and right is not None
                left, right = stack.enter_context(create_adapter_pair(left, right))

            case _:
                raise ValueError(f"Unsupported backend: {backend!r}")

    # At this point, both left and right should exist
    assert left is not None
    assert right is not None
    assert spec.original_left is not None
    assert spec.original_right is not None

    # If a URI scheme override is requested (e.g. "posix://"), rewrite the raw
    # device paths to route through that handler. Paths that are already URIs
    # (e.g. rfc2217://) are left alone.
    if spec.uri_scheme:
        if not urllib.parse.urlparse(left).scheme:
            left = spec.uri_scheme + left
        if not urllib.parse.urlparse(right).scheme:
            right = spec.uri_scheme + right
        effective_scheme = spec.uri_scheme
    else:
        effective_scheme = get_uri_handler("device://").unique_scheme

    # Finally, emit the spec
    try:
        yield SerialPair(
            left=left,
            right=right,
            original_left=spec.original_left,
            original_right=spec.original_right,
            backends=spec.backends,
            quirks=spec.quirks,
            uri_scheme=effective_scheme,
            modem_line_propagation_delay=spec.modem_line_propagation_delay,
            max_drain_payload=spec.max_drain_payload,
            unplug_left_graceful=unplug_left_graceful,
            unplug_left_abrupt=unplug_left_abrupt,
        )
    finally:
        stack.close()


@pytest.fixture(scope="session", autouse=True)
def _warm_lazy_timezone_import() -> None:
    """Resolve the ESPHome timezone once, before any fd snapshotting."""
    if get_local_timezone is not None:
        asyncio.run(get_local_timezone())


@pytest.fixture(autouse=True)
async def _check_fd_leaks_autouse() -> AsyncGenerator[None]:
    """Run every test inside `check_fd_leaks` to catch unintended fd leaks."""
    with check_fd_leaks():
        yield
