"""Shared test utilities and fixtures."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
import contextlib
import dataclasses
import enum
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, Any

import psutil
import pytest
from typing_extensions import Self

import serialx

_PYODIDE_PAIR_COUNTER = 0

SOCAT_BINARY = shutil.which("socat")
SER2NET_BINARY = shutil.which("ser2net")
HUB4COM_BINARY = shutil.which(
    "hub4com.exe",
    path=Path(__file__).resolve().parent / "data" / "windows" / "hub4com-2.1.0.0-386",
) or shutil.which("hub4com")
ESPHOME_HOST_BINARY = shutil.which(
    "program",
    path=(
        Path(__file__).resolve().parent
        / "esphome"
        / ".esphome"
        / "build"
        / "serialx-host-daemon"
        / ".pioenvs"
        / "serialx-host-daemon"
    ),
)


class SerialBackend(str, enum.Enum):
    """Known serial-pair backend families used by the test suite."""

    SOCAT = "socat"
    SOCKET = "socket"
    ESPHOME = "esphome"
    ESPHOME_HOST = "esphome_host"
    ADAPTER = "adapter"
    RFC2217 = "rfc2217"
    SER2NET = "ser2net"
    HUB4COM = "hub4com"
    PYODIDE = "pyodide"


class SerialQuirk(str, enum.Enum):
    """Quirks carried by a serial transport."""

    NO_RTS_CTS = "no-rts-cts"
    NO_DTR_DSR = "no-dtr-dsr"
    NO_RTS_DTR_READBACK = "no-rts-dtr-readback"
    NO_NUM_UNREAD_BYTES = "no-num-unread-bytes"
    NO_RESET_WRITE_BUFFER = "no-reset-write-buffer"
    NO_RESET_READ_BUFFER = "no-reset-read-buffer"
    NO_WRITE_TIMEOUT = "no-write-timeout"
    NO_BUFFER_CONTROL = "no-buffer-control"
    NO_PAUSE_WRITING_CALLBACKS = "no-pause-writing-callbacks"
    NO_EXCLUSIVITY = "no-exclusivity"
    NO_GRACEFUL_PEER_CLOSE = "no-graceful-peer-close"
    NO_WRITE_BUFFERING = "no-write-buffering"


SERIAL_PAIR_DEFAULT_QUIRKS: dict[SerialBackend, frozenset[SerialQuirk]] = {
    SerialBackend.SOCAT: frozenset(
        {
            SerialQuirk.NO_RTS_CTS,
            SerialQuirk.NO_DTR_DSR,
            SerialQuirk.NO_RESET_WRITE_BUFFER,
            SerialQuirk.NO_EXCLUSIVITY,
            SerialQuirk.NO_BUFFER_CONTROL,
        }
    ),
    SerialBackend.SOCKET: frozenset(
        {
            SerialQuirk.NO_RTS_CTS,
            SerialQuirk.NO_DTR_DSR,
            SerialQuirk.NO_RESET_WRITE_BUFFER,
            SerialQuirk.NO_WRITE_TIMEOUT,
            SerialQuirk.NO_NUM_UNREAD_BYTES,
            SerialQuirk.NO_PAUSE_WRITING_CALLBACKS,
            SerialQuirk.NO_EXCLUSIVITY,
            SerialQuirk.NO_WRITE_BUFFERING,
        }
    ),
    SerialBackend.ESPHOME: frozenset(
        {
            SerialQuirk.NO_BUFFER_CONTROL,
            SerialQuirk.NO_RESET_WRITE_BUFFER,
            SerialQuirk.NO_WRITE_TIMEOUT,
            SerialQuirk.NO_EXCLUSIVITY,
            SerialQuirk.NO_WRITE_BUFFERING,
            # ESPHome has no orderly API close at runtime, so a dropped
            # connection is always abrupt
            SerialQuirk.NO_GRACEFUL_PEER_CLOSE,
        }
    ),
    SerialBackend.ESPHOME_HOST: frozenset(
        {
            SerialQuirk.NO_BUFFER_CONTROL,
            SerialQuirk.NO_RESET_WRITE_BUFFER,
            SerialQuirk.NO_WRITE_TIMEOUT,
            # Host binary does not support flow control
            SerialQuirk.NO_DTR_DSR,
            SerialQuirk.NO_RTS_CTS,
            SerialQuirk.NO_EXCLUSIVITY,
            SerialQuirk.NO_GRACEFUL_PEER_CLOSE,
            SerialQuirk.NO_WRITE_BUFFERING,
        }
    ),
    SerialBackend.RFC2217: frozenset(
        {
            SerialQuirk.NO_RTS_DTR_READBACK,
            SerialQuirk.NO_NUM_UNREAD_BYTES,
            SerialQuirk.NO_RESET_WRITE_BUFFER,
            SerialQuirk.NO_WRITE_TIMEOUT,
            SerialQuirk.NO_PAUSE_WRITING_CALLBACKS,
            SerialQuirk.NO_EXCLUSIVITY,
            SerialQuirk.NO_WRITE_BUFFERING,
        }
    ),
    SerialBackend.SER2NET: frozenset({}),
    SerialBackend.HUB4COM: frozenset({}),
    SerialBackend.ADAPTER: frozenset(),
    SerialBackend.PYODIDE: frozenset(
        {
            # Web Serial reports *input* signals only; output signals (RTS/DTR/BRK)
            # don't read back on the same port.
            SerialQuirk.NO_RTS_DTR_READBACK,
            SerialQuirk.NO_NUM_UNREAD_BYTES,
            SerialQuirk.NO_RESET_READ_BUFFER,
            SerialQuirk.NO_RESET_WRITE_BUFFER,
            SerialQuirk.NO_PAUSE_WRITING_CALLBACKS,
            SerialQuirk.NO_BUFFER_CONTROL,
            SerialQuirk.NO_EXCLUSIVITY,
        }
    ),
}


@dataclasses.dataclass(frozen=True, kw_only=True)
class UnresolvedSerialPair:
    """Description of a test serial pair before fixture creation."""

    # The URIs to connect to either side, always set for emitted specs
    left: str | None
    right: str | None

    original_left: str | None
    original_right: str | None

    # Backends to chain
    backends: tuple[SerialBackend, ...]

    # Accumulated quirks
    quirks: frozenset[SerialQuirk]

    uri_scheme: str | None = None
    modem_line_propagation_delay: float = 0.05

    # Largest payload a close()-time drain can carry through this pair, in bytes
    max_drain_payload: int = 4096

    def chain(self, *backends: SerialBackend) -> Self:
        """Chain another backend layer on top of this one, accumulating quirks."""
        result = self

        for backend in backends:
            result = dataclasses.replace(
                result,
                original_left=result.original_left,
                original_right=result.original_right,
                backends=(backend,) + result.backends,
                quirks=frozenset(result.quirks) | SERIAL_PAIR_DEFAULT_QUIRKS[backend],
            )

        return result


@dataclasses.dataclass(frozen=True)
class SerialPair(UnresolvedSerialPair):
    """Description of a test serial pair after fixture creation."""

    left: str
    right: str

    original_left: str
    original_right: str

    uri_scheme: str

    # Drop the left connection; None when the backend can't produce that flavor.
    unplug_left_graceful: Callable[[], None] | None = None
    unplug_left_abrupt: Callable[[], None] | None = None


def _snapshot_fds() -> set[int]:
    """Return the set of open fd numbers for this process."""
    if sys.platform == "linux":
        # `listdir` holds an fd that shows up in its own listing
        fd_dir = f"/proc/{os.getpid()}/fd"
        entries = os.listdir(fd_dir)

        return {int(e) for e in entries if os.path.lexists(f"{fd_dir}/{e}")}

    if sys.platform == "emscripten":
        return set()

    proc = psutil.Process()
    fds: set[int] = {f.fd for f in proc.open_files() if f.fd >= 0}
    fds |= {c.fd for c in proc.net_connections(kind="all") if c.fd >= 0}
    return fds


@contextlib.contextmanager
def check_fd_leaks() -> Iterator[None]:
    """Fail if any file descriptor is opened in this block without being closed."""
    before = _snapshot_fds()
    try:
        yield
    finally:
        leaked = _snapshot_fds() - before
        if leaked:
            pytest.fail(f"Leaked file descriptors: {sorted(leaked)}")


def _get_listening_ports(pid: int) -> list[int]:
    """Get the TCP ports a process is listening on, via psutil."""
    return sorted(
        c.laddr.port
        for c in psutil.Process(pid).net_connections(kind="tcp")
        if c.status == psutil.CONN_LISTEN
    )


def _wait_for_ready(
    process: subprocess.Popen[Any],
    stream: IO[bytes] | None,
    marker: str,
    name: str,
) -> None:
    """Wait for a process to print a ready marker to stdout or stderr."""
    assert stream is not None

    marker_bytes = marker.encode()
    output = bytearray()

    while True:
        line = stream.readline()

        if not line:
            raise RuntimeError(
                f"{name} exited before ready (code={process.returncode})"
                f"\n{stream}: {output.decode(errors='replace')}"
            )

        output.extend(line)

        if marker_bytes in line:
            return


@contextlib.contextmanager
def create_adapter_pair(left: str, right: str) -> Iterator[tuple[str, str]]:
    """Fixture to clean up/set up physical adapters, which may have quirks."""
    if left.startswith("CNC") or right.startswith("CNC"):
        # com0com has baudrate emulation and requires buffers to be purged
        assert sys.platform == "win32"

        from win32file import (  # noqa: PLC0415
            PURGE_RXABORT,
            PURGE_RXCLEAR,
            PURGE_TXABORT,
            PURGE_TXCLEAR,
            PurgeComm,
        )

        for adapter in (left, right):
            if not adapter.startswith("CNC"):
                continue

            with serialx.Serial.from_url(adapter, baudrate=10_000_000) as serial:
                flags = PURGE_TXABORT | PURGE_RXABORT | PURGE_TXCLEAR | PURGE_RXCLEAR

                PurgeComm(serial._handle, flags)  # type: ignore[attr-defined]
                time.sleep(0.05)

        yield (left, right)
    else:
        yield (left, right)


@contextlib.contextmanager
def create_esphome_pair(
    left_tty: str,
    right_tty: str,
    *,
    noise_psk: str = "",
) -> Iterator[tuple[str, str, Callable[[], None] | None, Callable[[], None] | None]]:
    """Create an esphome:// pair."""
    assert ESPHOME_HOST_BINARY is not None

    # The daemon's host UART never flushes the port it opens, so bytes an earlier test
    # left unread on a persistent device would be read back as the next test's data.
    # serialx flushes both queues on open.
    for tty in (left_tty, right_tty):
        with serialx.Serial.from_url(tty, baudrate=115200):
            pass

    env = os.environ.copy()
    env["SERIALX_UART_LEFT"] = left_tty
    env["SERIALX_UART_RIGHT"] = right_tty
    env["SERIALX_API_PORT"] = "0"
    env["SERIALX_NOISE_PSK"] = noise_psk

    process = subprocess.Popen(  # noqa: S603
        [ESPHOME_HOST_BINARY],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for_ready(
            process,
            stream=process.stderr,
            marker="Ready",
            name="ESPHome host daemon",
        )

        api_port = _get_listening_ports(process.pid)[0]

        def unplug_abrupt() -> None:
            """Kill the daemon so the API connection drops mid-session."""
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        yield (
            f"esphome://127.0.0.1:{api_port}?port_name=Serial+Proxy+Left",
            f"esphome://127.0.0.1:{api_port}?port_name=Serial+Proxy+Right",
            # No graceful flavor; see SerialQuirk.NO_GRACEFUL_PEER_CLOSE
            None,
            unplug_abrupt,
        )
    finally:
        if process.poll() is None:
            process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        # The unplug callables hold references to the Popen objects
        if process.stderr is not None:
            process.stderr.close()


@contextlib.contextmanager
def create_socat_pair() -> Iterator[
    tuple[str, str, Callable[[], None] | None, Callable[[], None] | None]
]:
    """Create a bridged pair of virtual PTYs using two socat processes.

    Each PTY is managed by its own socat process, linked via a UNIX socket.
    Killing one socat process closes its PTY and propagates EOF to the other.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        left_tty = os.path.join(tmpdir, "ttyLeft")
        right_tty = os.path.join(tmpdir, "ttyRight")
        bridge = os.path.join(tmpdir, "bridge.sock")

        # Start the right side first (UNIX-LISTEN), then the left (UNIX-CONNECT)
        right_proc = subprocess.Popen(
            [
                "socat",
                "-d",
                "-d",
                f"PTY,link={right_tty},raw,echo=0",
                f"UNIX-LISTEN:{bridge},rcvbuf=1024,sndbuf=1024",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        _wait_for_ready(
            right_proc,
            marker="listening on",
            stream=right_proc.stderr,
            name="socat(right)",
        )

        left_proc = subprocess.Popen(
            [
                "socat",
                "-d",
                "-d",
                f"PTY,link={left_tty},raw,echo=0",
                f"UNIX-CONNECT:{bridge},rcvbuf=1024,sndbuf=1024",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        _wait_for_ready(
            left_proc,
            marker="starting data transfer loop",
            stream=left_proc.stderr,
            name="socat(left)",
        )

        def _kill(proc: subprocess.Popen[Any]) -> None:
            proc.kill()
            proc.wait()

        try:
            # Killing socat tears down the PTY; the client read hangs up with
            # EIO, an inherently abrupt disconnect. There is no clean-FIN form.
            yield (
                left_tty,
                right_tty,
                None,
                lambda: _kill(left_proc),
            )
        finally:
            for proc in (left_proc, right_proc):
                if proc.returncode is None:
                    proc.terminate()
                    proc.wait()

                # The unplug callables hold references to the Popen objects
                if proc.stderr is not None:
                    proc.stderr.close()


@contextlib.contextmanager
def create_pyodide_pair() -> Iterator[tuple[str, str]]:
    """Create a fake Web Serial pair and register each side at a unique URL."""
    import js  # type: ignore[import-not-found]  # noqa: PLC0415

    from serialx.platforms.serial_pyodide import (  # noqa: PLC0415
        register_js_port,
        unregister_js_port,
    )

    global _PYODIDE_PAIR_COUNTER  # noqa: PLW0603
    _PYODIDE_PAIR_COUNTER += 1
    left_url = f"pyodide://pair{_PYODIDE_PAIR_COUNTER}-left"
    right_url = f"pyodide://pair{_PYODIDE_PAIR_COUNTER}-right"

    left_port, right_port = js.create_fake_serial_pair()
    register_js_port(left_url, left_port)
    register_js_port(right_url, right_port)
    try:
        yield (left_url, right_url)
    finally:
        unregister_js_port(left_url)
        unregister_js_port(right_url)


@contextlib.asynccontextmanager
async def async_create_socat_pair() -> AsyncIterator[tuple[str, str]]:
    """Create a pair of virtual PTYs using socat (asynchronous)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        in_tty = os.path.join(tmpdir, "ttyTestIn")
        out_tty = os.path.join(tmpdir, "ttyTestOut")

        proc = await asyncio.create_subprocess_exec(
            "socat",
            f"PTY,link={in_tty},raw,echo=0",
            f"PTY,link={out_tty},raw,echo=0",
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Give socat time to set up the PTYs
        await asyncio.sleep(0.5)

        assert proc.returncode is None

        yield (in_tty, out_tty)

        proc.terminate()
        await proc.wait()


@contextlib.contextmanager
def create_ser2net_pair(
    left_adapter: str, right_adapter: str
) -> Iterator[tuple[str, str, Callable[[], None] | None, Callable[[], None] | None]]:
    """Create a pair of independent RFC2217 sockets using ser2net."""

    # fmt: off
    proc = subprocess.Popen(
        [
            "ser2net",
            "-n",  # Don't detach from the controlling terminal
            "-r",  # Print "Ready" to stdout when listening
            "-u",  # Disable UUCP locking
            "-Y", json.dumps(
                {
                    "connections": {
                        "left_adapter": {
                            "accepter": "telnet(rfc2217),tcp,0",
                            "connector": f"serialdev(),{left_adapter},speed=115200n81",
                        },
                        "right_adapter": {
                            "accepter": "telnet(rfc2217),tcp,0",
                            "connector": f"serialdev(),{right_adapter},speed=115200n81",
                        },
                    }
                }
            )
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # fmt: on

    def _kill() -> None:
        proc.kill()
        proc.wait()

    try:
        _wait_for_ready(
            proc,
            stream=proc.stdout,
            marker="Ready",
            name="ser2net",
        )

        left, right = _get_listening_ports(proc.pid)

        # ser2net serves both adapters from one process. Killing it closes the
        # client socket with a graceful FIN; there is no abrupt-reset form.
        yield (
            f"rfc2217://127.0.0.1:{left}",
            f"rfc2217://127.0.0.1:{right}",
            _kill,
            None,
        )
    finally:
        if proc.returncode is None:
            proc.terminate()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


@contextlib.contextmanager
def create_hub4com_pair(
    left_adapter: str, right_adapter: str, *, comport: str = "server"
) -> Iterator[tuple[str, str]]:
    """Create a pair of independent RFC2217 sockets using hub4com on Windows."""
    assert HUB4COM_BINARY is not None

    hub4com_args = [
        f"--create-filter=telnet,tcp,telnet:--comport={comport} --suppress-echo=yes",
        "--create-filter=lsrmap,tcp,lsrmap",
        "--create-filter=pinmap,tcp,pinmap:--cts=cts --dsr=dsr --dcd=dcd --ring=ring",
        "--create-filter=linectl,tcp,lc:--br=local --lc=local",
        "--create-filter=pinmap,com,pinmap:--rts=cts --dtr=dsr --break=break",
        "--create-filter=linectl,com,lc:--br=remote --lc=remote",
        "--create-filter=purge,com,purge",
        "--add-filters=0:com",
        "--add-filters=1:tcp",
        "--octs=off",
        "--write-limit=65536",
    ]

    procs = []
    drain_threads: list[threading.Thread] = []

    try:
        for adapter in (left_adapter, right_adapter):
            proc = subprocess.Popen(
                [
                    HUB4COM_BINARY,
                    *hub4com_args,
                    f"\\\\.\\{adapter}",
                    "--use-driver=tcp",
                    "--interface=127.0.0.1",
                    "*0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append(proc)

        for proc in procs:
            _wait_for_ready(
                proc,
                stream=proc.stdout,
                marker="Started TCP(",
                name="hub4com",
            )

        # Drain hub4com's stdout/stderr. Otherwise the OS pipe buffers fill after a
        # handful of sessions and hub4com blocks.
        for proc in procs:
            for stream in (proc.stdout, proc.stderr):
                t = threading.Thread(
                    target=lambda s=stream: list(iter(s.readline, b"")),
                )
                t.start()
                drain_threads.append(t)

        left, right = [_get_listening_ports(proc.pid)[0] for proc in procs]

        yield (
            f"rfc2217://127.0.0.1:{left}",
            f"rfc2217://127.0.0.1:{right}",
        )
    finally:
        for proc in procs:
            if proc.returncode is None:
                proc.terminate()
                proc.wait()

        for t in drain_threads:
            t.join()


@contextlib.asynccontextmanager
async def async_create_serial_pair(
    left: str,
    right: str,
    **kwargs: Any,
) -> AsyncIterator[tuple[serialx.AsyncSerial, serialx.AsyncSerial]]:
    """Create AsyncSerial objects for both sides of a socat connection."""
    async with (
        serialx.async_serial_for_url(left, **kwargs) as ser_left,
        serialx.async_serial_for_url(right, **kwargs) as ser_right,
    ):
        yield ser_left, ser_right


@contextlib.contextmanager
def measure_time() -> Iterator[Callable[[], float]]:
    """Measure elapsed time in a context."""
    start = time.monotonic()
    end = None

    def get_result() -> float:
        if end is None:
            raise RuntimeError("Context has not exited yet")

        return end - start

    try:
        yield get_result
    finally:
        end = time.monotonic()
