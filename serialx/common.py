"""Serial port communication utilities."""

from __future__ import annotations

from abc import abstractmethod
import asyncio
from asyncio import IncompleteReadError
import bisect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
import copy
import dataclasses
from enum import Enum
import functools
import io
import logging
import os.path
from pathlib import Path
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any, Concatenate, NamedTuple, ParamSpec, TypeVar, cast
import urllib.parse
import warnings

from typing_extensions import Buffer, Self, TypedDict, Unpack

if TYPE_CHECKING:
    from aioesphomeapi.client import APIClient

LOGGER = logging.getLogger(__name__)


class Platform(str, Enum):
    """Built-in platform name."""

    DEVICE = "device"

    POSIX = "posix"
    EXTENDED_POSIX = "extended_posix"
    FREEBSD = "freebsd"
    LINUX = "linux"
    DARWIN = "darwin"
    WIN32 = "win32"
    SOCKET = "socket"
    RFC2217 = "rfc2217"
    ESPHOME = "esphome"


@dataclasses.dataclass(frozen=True)
class RegisteredUriHandler:
    """A URI handler registration entry."""

    scheme: str
    unique_scheme: str
    weight: int
    sync_cls: type[BaseSerial]
    async_transport_cls: type[BaseSerialTransport]
    list_serial_ports_func: Callable[..., list[SerialPortInfo]]
    async_list_serial_ports_func: Callable[..., Awaitable[list[SerialPortInfo]]]
    strip_uri_scheme: bool
    connect_kwargs: frozenset[str] = frozenset()


class _RegistryEntry(NamedTuple):
    """Entry in `_REGISTERED_URI_HANDLERS`, ordered by (weight, insertion_time)."""

    weight: int
    insertion_time: float  # To avoid comparing `RegisteredUriHandler` objects
    handler: RegisteredUriHandler


_REGISTERED_URI_HANDLERS: defaultdict[str, list[_RegistryEntry]] = defaultdict(list)


def empty_port_list(*args: Any, **kwargs: Any) -> list[SerialPortInfo]:
    """Return an empty list of serial ports."""
    return []


async def async_empty_port_list(*args: Any, **kwargs: Any) -> list[SerialPortInfo]:
    """Return an empty list of serial ports, async."""
    return []


def register_uri_handler(
    *,
    scheme: str,
    unique_scheme: str,
    sync_cls: type[BaseSerial],
    async_transport_cls: type[BaseSerialTransport],
    list_serial_ports_func: Callable[..., list[SerialPortInfo]] = empty_port_list,
    async_list_serial_ports_func: Callable[
        ..., Awaitable[list[SerialPortInfo]]
    ] = async_empty_port_list,
    weight: int = 1,
    strip_uri_scheme: bool = False,
    connect_kwargs: frozenset[str] = frozenset(),
) -> Callable[[], None]:
    """Register a URI handler.

    Expose a new backend to ``serial_for_url`` / ``create_serial_connection`` /
    ``open_serial_connection``.

    Args:
        scheme: Shared dispatch scheme. URLs with this scheme resolve to the
            highest-weight handler registered under it.
        unique_scheme: A scheme that uniquely identifies this handler. Must end
            with ``://`` and must not collide with an existing registration.
            Use this to address the handler directly.
        sync_cls: Synchronous serial class, typically a subclass of
            :class:`BaseSerial`.
        async_transport_cls: Async transport class, typically a subclass of
            :class:`BaseSerialTransport`.
        list_serial_ports_func: Callable returning a list of :class:`SerialPortInfo`.
        async_list_serial_ports_func: Async callable returning a list of
            :class:`SerialPortInfo`.
        weight: Dispatch priority under ``scheme``. Higher wins.
        strip_uri_scheme: If ``True``, the leading ``scheme`` / ``unique_scheme``
            is removed before the URL is passed to the sync class. Set this when
            the underlying class expects a bare device path rather than a URL.
        connect_kwargs: Names of backend-specific connect kwargs this handler accepts.

    Returns:
        A callable that unregisters the handler.

    Raises:
        ValueError: if either scheme doesn't end with ``://`` or ``unique_scheme``
            is already registered.

    """
    if not scheme.endswith("://") or not unique_scheme.endswith("://"):
        raise ValueError(f"Schemes {scheme!r} and {unique_scheme!r} must end with ://")

    if _REGISTERED_URI_HANDLERS[unique_scheme]:
        raise ValueError(
            f"URI scheme {unique_scheme!r} is not unique,"
            f" already registered to {_REGISTERED_URI_HANDLERS[unique_scheme]}"
        )

    item = _RegistryEntry(
        weight=weight,
        insertion_time=time.monotonic(),
        handler=RegisteredUriHandler(
            scheme=scheme,
            unique_scheme=unique_scheme,
            weight=weight,
            sync_cls=sync_cls,
            async_transport_cls=async_transport_cls,
            list_serial_ports_func=list_serial_ports_func,
            async_list_serial_ports_func=async_list_serial_ports_func,
            strip_uri_scheme=strip_uri_scheme,
            connect_kwargs=connect_kwargs,
        ),
    )
    bisect.insort_right(_REGISTERED_URI_HANDLERS[scheme], item)

    if unique_scheme != scheme:
        _REGISTERED_URI_HANDLERS[unique_scheme].append(item)

    def remove_callback() -> None:
        _REGISTERED_URI_HANDLERS[scheme].remove(item)
        if unique_scheme != scheme:
            _REGISTERED_URI_HANDLERS[unique_scheme].remove(item)

    return remove_callback


def get_uri_handler(uri: str) -> RegisteredUriHandler:
    """Look up the registered handler for the given URI."""
    parsed_uri = urllib.parse.urlparse(uri)
    scheme = (parsed_uri.scheme or "device") + "://"
    handlers = _REGISTERED_URI_HANDLERS.get(scheme)
    if not handlers:
        raise UnknownUriScheme(f"No handler registered for URI scheme {scheme!r}")
    return handlers[-1].handler


def route_backend_kwargs(
    handler: RegisteredUriHandler, kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Drop kwargs that belong to a different backend before dispatch."""
    all_backend_specific_kwargs: set[str] = set()

    for extras in BACKEND_CONNECT_KWARGS.values():
        all_backend_specific_kwargs |= extras

    for entries in _REGISTERED_URI_HANDLERS.values():
        for entry in entries:
            all_backend_specific_kwargs |= entry.handler.connect_kwargs

    backend_specific_kwargs = (
        BACKEND_CONNECT_KWARGS.get(handler.unique_scheme, set())
        | handler.connect_kwargs
    )

    other_kwargs = all_backend_specific_kwargs - backend_specific_kwargs
    dropped = other_kwargs & kwargs.keys()

    if not dropped:
        return dict(kwargs)

    LOGGER.debug(
        "Ignoring kwarg not accepted by %r backend: %s", handler.unique_scheme, dropped
    )
    return {key: value for key, value in kwargs.items() if key not in dropped}


class SerialException(Exception):
    """Base serial exception."""


class UnsupportedSetting(SerialException):
    """Raised when an unsupported serial port setting is used."""


class UnknownUriScheme(SerialException):
    """Raised when a URI scheme has no registered handler."""


class StopBits(float, Enum):
    """Stop bits configuration."""

    ONE = 1
    ONE_POINT_FIVE = 1.5
    TWO = 2


class Parity(str, Enum):
    """Parity configuration."""

    NONE = "N"
    ODD = "O"
    EVEN = "E"
    MARK = "M"
    SPACE = "S"


class _CommonConnectKwargs(TypedDict, total=False):
    """Connect kwargs accepted by every backend (see `BaseSerial.__init__`)."""

    baudrate: int
    parity: Parity | str | None
    stopbits: StopBits | int | float
    xonxoff: bool
    rtscts: bool
    dsrdtr: bool
    byte_size: int
    read_timeout: float | None
    write_timeout: float | None
    dtr_on_open: PinState
    rts_on_open: PinState
    dtr_on_close: PinState
    rts_on_close: PinState
    exclusive: bool

    # backwards compatibility kwargs
    rtsdtr_on_open: PinState
    rtsdtr_on_close: PinState

    # pyserial compatibility kwargs
    port: str | None
    timeout: float | None
    bytesize: int | None
    writeTimeout: float | None
    do_not_open: bool | None


class ConnectKwargs(  # type: ignore[call-arg]  # PEP 728 not in mypy yet
    _CommonConnectKwargs, total=False, extra_items=Any
):
    """Connect kwargs plumbed internally to `BaseSerialTransport.connect`."""


class AllConnectKwargs(_CommonConnectKwargs, total=False):
    """Every connect kwarg any built-in backend accepts, for typing."""

    # linux://
    low_latency: bool

    # socket:// + tcp:// + rfc2217:// + esphome://
    connect_timeout: float | None

    # rfc2217://
    receive_buffer_size: int

    # windows://
    read_buffer_size: int
    write_buffer_size: int

    # esphome://
    api: APIClient | None
    port_name: str | None
    port_instance: int | None
    mode: str
    key: str | None
    password: str | None
    noise_psk: str | None


# Backend-specific connect kwargs per unique URI scheme
BACKEND_CONNECT_KWARGS: dict[str, frozenset[str]] = {
    "linux://": frozenset({"low_latency"}),
    "windows://": frozenset({"read_buffer_size", "write_buffer_size"}),
    "socket://": frozenset({"connect_timeout"}),
    "tcp://": frozenset({"connect_timeout"}),
    "rfc2217://": frozenset({"connect_timeout", "receive_buffer_size"}),
    "esphome://": frozenset(
        {
            "api",
            "connect_timeout",
            "port_name",
            "port_instance",
            "mode",
            "key",
            "password",
            "noise_psk",
        }
    ),
}


class PinState(Enum):
    """Pin state."""

    UNDEFINED = None
    LOW = 0
    HIGH = 1

    @classmethod
    def convert(cls, value: PinState | bool | None) -> PinState:
        """Create PinState from boolean."""
        if isinstance(value, cls):
            return value

        if value is None:
            return cls.UNDEFINED

        return cls.HIGH if value else cls.LOW

    def to_bool(self) -> bool | None:
        """Convert PinState to boolean."""
        if self is PinState.UNDEFINED:
            return None

        return self is PinState.HIGH


@dataclasses.dataclass(frozen=True)
class ModemPins:
    """Modem control bits."""

    le: PinState = PinState.UNDEFINED
    dtr: PinState = PinState.UNDEFINED
    rts: PinState = PinState.UNDEFINED
    st: PinState = PinState.UNDEFINED
    sr: PinState = PinState.UNDEFINED
    cts: PinState = PinState.UNDEFINED
    car: PinState = PinState.UNDEFINED
    rng: PinState = PinState.UNDEFINED
    dsr: PinState = PinState.UNDEFINED

    def __repr__(self) -> str:
        """Return string representation of modem pins."""

        bits = []

        for bit in ("le", "dtr", "rts", "st", "sr", "cts", "car", "rng", "dsr"):
            value = getattr(self, bit)

            if value is PinState.UNDEFINED:
                continue
            elif value is PinState.HIGH:
                bits.append(bit)
            else:
                bits.append(f"!{bit}")

        return f"{self.__class__.__name__}[{' '.join(bits)}]"


@contextmanager
def measure_time() -> Iterator[Callable[[], float]]:
    """Measure elapsed time in a context."""
    start: float = time.monotonic()
    end: float | None = None

    def get_result() -> float:
        if end is None:
            raise RuntimeError("Context has not exited yet")

        return end - start

    try:
        yield get_result
    finally:
        end = time.monotonic()


_P = ParamSpec("_P")
_R = TypeVar("_R")


def maybe_wrap_exceptions(
    func: Callable[Concatenate[BaseSerial, _P], _R],
) -> Callable[Concatenate[BaseSerial, _P], _R]:
    """Re-raise all exceptions as `SerialException` when the flag is set."""

    @functools.wraps(func)
    def replacement(self: BaseSerial, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return func(self, *args, **kwargs)
        except Exception as exc:
            if self._wrap_exceptions:
                raise SerialException(str(exc)) from exc

            raise

    return replacement


class BaseSerial(io.RawIOBase):
    """Base class for serial port communication.

    .. deprecated:: 1.9.0
        The ``rtsdtr_on_open`` and ``rtsdtr_on_close`` constructor kwargs are
        deprecated; use the per-pin ``dtr_on_open``, ``rts_on_open``,
        ``dtr_on_close``, and ``rts_on_close`` instead.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        baudrate: int = 9600,
        *,
        parity: Parity | str | None = Parity.NONE,
        stopbits: StopBits | int | float = StopBits.ONE,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        byte_size: int = 8,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        dtr_on_open: PinState = PinState.HIGH,
        rts_on_open: PinState = PinState.HIGH,
        dtr_on_close: PinState = PinState.LOW,
        rts_on_close: PinState = PinState.LOW,
        exclusive: bool = True,
        # pyserial compatibility kwargs
        port: str | None = None,
        timeout: float | None = None,
        bytesize: int | None = None,
        do_not_open: bool | None = None,
        writeTimeout: float | None = None,
        inter_byte_timeout: int | None = None,
        # Legacy kwargs
        rtsdtr_on_open: PinState = PinState.UNDEFINED,
        rtsdtr_on_close: PinState = PinState.UNDEFINED,
        # Internal pyserial compatibility signal
        _wrap_exceptions: bool = False,
    ) -> None:
        """Initialize serial port configuration."""
        super().__init__()

        normalized_stopbits = (
            stopbits if isinstance(stopbits, StopBits) else StopBits(stopbits)
        )

        if parity is None:
            normalized_parity = Parity.NONE
        elif isinstance(parity, Parity):
            normalized_parity = parity
        else:
            normalized_parity = Parity(parity)

        self._path = path
        self._baudrate = baudrate
        self._stopbits = normalized_stopbits
        self._xonxoff = xonxoff
        self._rtscts = rtscts
        self._dsrdtr = dsrdtr
        self._parity = normalized_parity
        self._byte_size = byte_size
        self._exclusive = exclusive
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

        if (
            rtsdtr_on_open is not PinState.UNDEFINED
            or rtsdtr_on_close is not PinState.UNDEFINED
        ):
            warnings.warn(
                "`rtsdtr_on_open`/`rtsdtr_on_close` are deprecated; use the per-pin"
                " `dtr_on_open`/`rts_on_open`/`dtr_on_close`/`rts_on_close` instead",
                DeprecationWarning,
                stacklevel=2,
            )

        self._rts_on_open: PinState = (
            rts_on_open if rtsdtr_on_open is PinState.UNDEFINED else rtsdtr_on_open
        )
        self._rts_on_close: PinState = (
            rts_on_close if rtsdtr_on_close is PinState.UNDEFINED else rtsdtr_on_close
        )
        self._dtr_on_open: PinState = (
            dtr_on_open if rtsdtr_on_open is PinState.UNDEFINED else rtsdtr_on_open
        )
        self._dtr_on_close: PinState = (
            dtr_on_close if rtsdtr_on_close is PinState.UNDEFINED else rtsdtr_on_close
        )

        # Last-written DTR/RTS output state, for readback on backends that can't
        # report output lines from hardware
        self._dtr_state = PinState.UNDEFINED
        self._rts_state = PinState.UNDEFINED

        self._auto_close = False

        # Compatibility kwargs
        if port is not None:
            self._path = port

        if timeout is not None:
            self._read_timeout = timeout

        if bytesize is not None:
            self._byte_size = bytesize

        if writeTimeout is not None:
            self._write_timeout = writeTimeout

        if do_not_open is False:
            raise RuntimeError("do_not_open=False is not supported")

        self._wrap_exceptions = _wrap_exceptions

        # Enter a "broken" state so that an error condition can persist
        self._broken: Exception | None = None

    def _mark_broken(self, exc: Exception) -> None:
        if self._broken is None:
            self._broken = exc

    def _check_broken(self) -> None:
        if self._broken is None:
            return

        exc = self._broken

        # Re-raising the same instance grows its `__traceback__` on every raise
        try:
            fresh = copy.copy(exc)
        except Exception:  # noqa: BLE001
            LOGGER.debug("Failed to clone exception %r", exc)
        else:
            raise fresh from exc

        # Raised outside of the `except` so the copy failure isn't attached to the
        # shared instance as its `__context__`
        raise exc.with_traceback(None)

    @classmethod
    def from_url(
        cls, url: str, *args: Any, **kwargs: Unpack[AllConnectKwargs]
    ) -> BaseSerial:
        """Create the appropriate serial port subclass for the given URL."""
        handler = get_uri_handler(url)
        target = url
        if handler.strip_uri_scheme:
            target = url.removeprefix(handler.scheme).removeprefix(
                handler.unique_scheme
            )
        routed = route_backend_kwargs(handler, dict(kwargs))
        return handler.sync_cls(target, *args, **routed)

    @maybe_wrap_exceptions
    def open(self) -> None:
        """Open the serial port."""
        self._broken = None
        try:
            self._open()
            self._configure_port()
        except BaseException:
            self.close()
            raise

    @maybe_wrap_exceptions
    def configure_port(self) -> None:
        """Configure the serial port settings."""
        self._configure_port()

    @abstractmethod
    def _open(self) -> None:
        """Open the serial port (platform-specific)."""
        raise NotImplementedError

    @abstractmethod
    def _configure_port(self) -> None:
        """Configure the serial port settings (platform-specific)."""
        raise NotImplementedError

    @maybe_wrap_exceptions
    def close(self) -> None:
        """Close the serial port."""
        self._close()

    @abstractmethod
    def _close(self) -> None:
        """Close the serial port, internal."""
        raise NotImplementedError

    @property
    def read_timeout(self) -> float | None:
        """Get the read timeout in seconds."""
        return self._read_timeout

    @property
    def write_timeout(self) -> float | None:
        """Get the write timeout in seconds."""
        return self._write_timeout

    @maybe_wrap_exceptions
    def get_modem_pins(self) -> ModemPins:
        """Get modem control bits."""
        self._check_broken()
        pins = self._get_modem_pins()
        return dataclasses.replace(
            pins,
            dtr=pins.dtr if pins.dtr is not PinState.UNDEFINED else self._dtr_state,
            rts=pins.rts if pins.rts is not PinState.UNDEFINED else self._rts_state,
        )

    def _modem_pins_on_open(self) -> ModemPins:
        """DTR/RTS to apply on open."""
        return ModemPins(
            dtr=self._dtr_on_open if not self._dsrdtr else PinState.UNDEFINED,
            rts=self._rts_on_open if not self._rtscts else PinState.UNDEFINED,
        )

    def _modem_pins_on_close(self) -> ModemPins:
        """DTR/RTS to apply on close."""
        return ModemPins(
            dtr=self._dtr_on_close if not self._dsrdtr else PinState.UNDEFINED,
            rts=self._rts_on_close if not self._rtscts else PinState.UNDEFINED,
        )

    @maybe_wrap_exceptions
    def set_modem_pins(
        self,
        modem_pins: ModemPins | None = None,
        *,
        le: PinState | bool | None = PinState.UNDEFINED,
        dtr: PinState | bool | None = PinState.UNDEFINED,
        rts: PinState | bool | None = PinState.UNDEFINED,
        st: PinState | bool | None = PinState.UNDEFINED,
        sr: PinState | bool | None = PinState.UNDEFINED,
        cts: PinState | bool | None = PinState.UNDEFINED,
        car: PinState | bool | None = PinState.UNDEFINED,
        rng: PinState | bool | None = PinState.UNDEFINED,
        dsr: PinState | bool | None = PinState.UNDEFINED,
    ) -> None:
        """Set modem control bits."""
        self._check_broken()

        pins = modem_pins
        if pins is None:
            pins = ModemPins(
                le=PinState.convert(le),
                dtr=PinState.convert(dtr),
                rts=PinState.convert(rts),
                st=PinState.convert(st),
                sr=PinState.convert(sr),
                cts=PinState.convert(cts),
                car=PinState.convert(car),
                rng=PinState.convert(rng),
                dsr=PinState.convert(dsr),
            )

        self._set_modem_pins(pins)

        if pins.dtr is not PinState.UNDEFINED:
            self._dtr_state = pins.dtr

        if pins.rts is not PinState.UNDEFINED:
            self._rts_state = pins.rts

    @abstractmethod
    def _get_modem_pins(self) -> ModemPins:
        """Get modem control bits, internal."""
        raise NotImplementedError

    @abstractmethod
    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set modem control bits, internal."""
        raise NotImplementedError

    @maybe_wrap_exceptions
    def readinto(self, b: Buffer, *, timeout: float | None = None) -> int:
        """Read bytes from serial port into buffer."""
        self._check_broken()
        effective_timeout = self._read_timeout if timeout is None else timeout
        return self._readinto(b, timeout=effective_timeout)

    @abstractmethod
    def _readinto(self, b: Buffer, *, timeout: float | None) -> int:
        """Read bytes from serial port into buffer, internal."""
        raise NotImplementedError

    @maybe_wrap_exceptions
    def write(self, data: Buffer, *, timeout: float | None = None) -> int:
        """Write bytes to serial port."""
        self._check_broken()
        effective_timeout = self._write_timeout if timeout is None else timeout
        return self._write(data, timeout=effective_timeout)

    @abstractmethod
    def _write(self, data: Buffer, *, timeout: float | None) -> int:
        """Write bytes to serial port, internal."""
        raise NotImplementedError

    def flush(self) -> None:
        """Flush write buffers."""
        self._check_broken()
        self._flush()

    @abstractmethod
    def _flush(self) -> None:
        """Flush write buffers, internal."""
        raise NotImplementedError

    @property
    def path(self) -> str | Path | None:
        """Get the serial port path."""
        return self._path

    @property
    def baudrate(self) -> int:
        """Get the baud rate."""
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value: int) -> None:
        """Set baud rate (deprecated)."""
        self._baudrate = value
        self._configure_port()

    @property
    def parity(self) -> Parity:
        """Get the parity."""
        return self._parity

    @property
    def byte_size(self) -> int:
        """Get the byte size."""
        return self._byte_size

    @property
    def stopbits(self) -> StopBits:
        """Get the number of stop bits."""
        return self._stopbits

    @property
    def dtr_on_open(self) -> PinState:
        """Get the DTR pin state (on open) setting."""
        return self._dtr_on_open

    @property
    def rts_on_open(self) -> PinState:
        """Get the RTS pin state (on open) setting."""
        return self._rts_on_open

    @property
    def dtr_on_close(self) -> PinState:
        """Get the DTR pin state (on close) setting."""
        return self._dtr_on_close

    @property
    def rts_on_close(self) -> PinState:
        """Get the RTS pin state (on close) setting."""
        return self._rts_on_close

    @property
    def exclusive(self) -> bool:
        """Get the exclusive setting."""
        return self._exclusive

    def readexactly(self, n: int, *, timeout: float | None = None) -> bytes:
        """Read exactly n bytes."""
        buffer = bytearray(n)
        view = memoryview(buffer)
        remaining = n
        remaining_timeout = self.read_timeout if timeout is None else timeout

        while remaining > 0:
            with measure_time() as get_elapsed:
                read = self.readinto(view, timeout=remaining_timeout)

            if remaining_timeout is not None:
                remaining_timeout -= get_elapsed()

            view = view[read:]
            remaining -= read

            if read == 0:
                # `IncompleteReadError` is a subclass of `EOFError`
                raise IncompleteReadError(
                    expected=n, partial=bytes(buffer[: n - remaining])
                )

        return bytes(buffer)

    def read_until(
        self,
        expected: bytes = b"\n",
        size: int | None = None,
        *,
        timeout: float | None = None,
    ) -> bytes:
        """Read until the expected sequence is found."""
        buffer = bytearray()
        expected_len = len(expected)
        remaining_timeout = self.read_timeout if timeout is None else timeout

        while True:
            with measure_time() as get_elapsed:
                byte = self.readexactly(1, timeout=remaining_timeout)

            if remaining_timeout is not None:
                remaining_timeout -= get_elapsed()

            if not byte:
                break

            buffer += byte

            if buffer[-expected_len:] == expected:
                break

            if size is not None and len(buffer) >= size:
                break

        return bytes(buffer)

    def __enter__(self) -> Self:
        """Enter context manager."""
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context manager."""
        self.close()

    def __del__(self) -> None:
        """Cleanup on deletion."""
        if getattr(self, "_auto_close", False):
            self.close()

    @abstractmethod
    def num_unread_bytes(self) -> int:
        """Return the number of bytes waiting to be read."""
        raise NotImplementedError

    @abstractmethod
    def num_unwritten_bytes(self) -> int:
        """Return the number of bytes waiting to be written."""
        raise NotImplementedError

    @maybe_wrap_exceptions
    def reset_read_buffer(self) -> None:
        """Reset the read buffer."""
        self._reset_read_buffer()

    @abstractmethod
    def _reset_read_buffer(self) -> None:
        """Reset the read buffer, internal."""
        raise NotImplementedError

    @maybe_wrap_exceptions
    def reset_write_buffer(self) -> None:
        """Reset the write buffer."""
        self._reset_write_buffer()

    @abstractmethod
    def _reset_write_buffer(self) -> None:
        """Reset the write buffer, internal."""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """Return whether the serial port is open."""
        raise NotImplementedError

    # Deprecated aliases
    @property
    def port(self) -> str | None:
        """Deprecated alias for `path`.

        Warning:
            Deprecated, use `path` instead.

        """
        return str(self.path) if self.path is not None else None

    @property
    def portstr(self) -> str | None:
        """Deprecated alias for `path`.

        Warning:
            Deprecated, use `path` instead.

        """
        return str(self.path) if self.path is not None else None

    @property
    def timeout(self) -> float | None:
        """Deprecated alias for `read_timeout`.

        Warning:
            Deprecated, use `read_timeout` instead.

        """
        return self.read_timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._read_timeout = value

    @property
    def bytesize(self) -> int:
        """Deprecated alias for `byte_size`.

        Warning:
            Deprecated, use `byte_size` instead.

        """
        return self.byte_size

    @property
    def data_bits(self) -> int:
        """Deprecated alias for `byte_size`.

        Warning:
            Deprecated, use `byte_size` instead.

        """
        return self.byte_size

    @data_bits.setter
    def data_bits(self, value: int) -> None:
        """Set the byte size (deprecated)."""
        self._byte_size = value

    @property
    def stop_bits(self) -> int | float:
        """Deprecated alias for `stopbits`.

        Warning:
            Deprecated, use `stopbits` instead.

        """
        return cast(int | float, self._stopbits.value)

    @stop_bits.setter
    def stop_bits(self, value: int | float) -> None:
        """Set the number of stop bits (deprecated)."""
        self._stopbits = StopBits(value)

    @property
    def writeTimeout(self) -> float | None:
        """Deprecated alias for `write_timeout`.

        Warning:
            Deprecated, use `write_timeout` instead.

        """
        return self.write_timeout

    def reset_input_buffer(self) -> None:
        """Reset the read buffer.

        Warning:
            Deprecated, use `reset_read_buffer` instead.

        """
        self.reset_read_buffer()

    def reset_output_buffer(self) -> None:
        """Reset the write buffer.

        Warning:
            Deprecated, use `reset_write_buffer` instead.

        """
        self.reset_write_buffer()

    def flushInput(self) -> None:
        """Reset the read buffer.

        Warning:
            Deprecated, use `reset_read_buffer` instead.

        """
        self.reset_read_buffer()

    def flushOutput(self) -> None:
        """Reset the write buffer.

        Warning:
            Deprecated, use `reset_write_buffer` instead.

        """
        self.reset_write_buffer()

    @property
    def in_waiting(self) -> int:
        """Deprecated alias for `num_unread_bytes`.

        Warning:
            Deprecated, use `num_unread_bytes` instead.

        """
        return self.num_unread_bytes()

    @property
    def out_waiting(self) -> int:
        """Deprecated alias for `num_unwritten_bytes`.

        Warning:
            Deprecated, use `num_unwritten_bytes` instead.

        """
        return self.num_unwritten_bytes()

    @property
    def inWaiting(self) -> int:
        """Deprecated alias for `num_unread_bytes`.

        Warning:
            Deprecated, use `num_unread_bytes` instead.

        """
        return self.in_waiting

    def isOpen(self) -> bool:
        """Return whether the serial port is open.

        Warning:
            Deprecated, use `is_open` instead.

        """
        return self.is_open

    @property
    def dtr(self) -> bool | None:
        """Get DTR modem bit."""
        if not self.is_open:
            return self._dtr_on_open.to_bool()
        return self.get_modem_pins().dtr.to_bool()

    @dtr.setter
    def dtr(self, value: bool) -> None:
        """Set DTR modem bit."""
        if not self.is_open:
            self._dtr_on_open = PinState.convert(value)
            return
        self.set_modem_pins(dtr=bool(value))

    @property
    def rts(self) -> bool | None:
        """Get RTS modem bit."""
        if not self.is_open:
            return self._rts_on_open.to_bool()
        return self.get_modem_pins().rts.to_bool()

    @rts.setter
    def rts(self, value: bool) -> None:
        """Set RTS modem bit."""
        if not self.is_open:
            self._rts_on_open = PinState.convert(value)
            return
        self.set_modem_pins(rts=bool(value))

    @property
    def cts(self) -> bool | None:
        """Get CTS modem bit."""
        return self.get_modem_pins().cts.to_bool()

    @cts.setter
    def cts(self, value: bool) -> None:
        """Set CTS modem bit."""
        self.set_modem_pins(cts=bool(value))


class BaseSerialTransport(asyncio.Transport):
    """Base class for serial port asyncio transport."""

    transport_name = "serial"

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        """Initialize serial transport."""
        super().__init__()
        self._loop = loop
        self._protocol = protocol
        self._extra: dict[str, Any] = {}

        self._serial: BaseSerial | None = None
        self._closing: bool = False
        self._closed_waiter: asyncio.Future[None] = loop.create_future()
        self._connection_made_called: bool = False
        self._connection_lost_called: bool = False
        self._user_initiated_close: bool = False

    def _mark_user_closed(self) -> None:
        """Record that the application requested close/abort."""
        self._user_initiated_close = True

    def _mark_broken(self, exc: Exception) -> None:
        if self._serial is not None:
            self._serial._mark_broken(exc)

    def _check_broken(self) -> None:
        if self._serial is not None:
            self._serial._check_broken()

    def is_closing(self) -> bool:
        """Return whether the transport is closing."""
        return self._closing

    def _resolve_closed_waiter(self) -> None:
        if not self._closed_waiter.done():
            self._closed_waiter.set_result(None)

    def _call_protocol_connection_made(self) -> None:
        """Mark connection_made and dispatch to the protocol exactly once."""
        assert not self._connection_made_called

        self._connection_made_called = True
        self._protocol.connection_made(self)

    def _call_protocol_connection_lost(self, exc: Exception | None) -> None:
        """Idempotent dispatch of connection_lost."""
        if self._connection_lost_called:
            return

        self._connection_lost_called = True

        try:
            if self._connection_made_called:
                self._protocol.connection_lost(exc)
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as protocol_exc:
            self._loop.call_exception_handler(
                {
                    "message": "protocol.connection_lost() failed",
                    "exception": protocol_exc,
                    "transport": self,
                    "protocol": self._protocol,
                }
            )
        finally:
            self._resolve_closed_waiter()

    def get_protocol(self) -> asyncio.Protocol:
        """Get the protocol used by this transport."""
        return self._protocol

    def set_protocol(self, protocol: asyncio.Protocol) -> None:  # type: ignore[override]
        """Set the protocol to use with this transport."""
        self._protocol = protocol

    @property
    def serial(self) -> BaseSerial:
        """Get the serial port instance."""
        assert self._serial is not None
        return self._serial

    @property
    def baudrate(self) -> int:
        """Get the baud rate."""
        assert self._serial is not None
        return self._serial.baudrate

    @property
    def parity(self) -> Parity:
        """Get the parity."""
        assert self._serial is not None
        return self._serial.parity

    @property
    def stopbits(self) -> StopBits:
        """Get the number of stop bits."""
        assert self._serial is not None
        return self._serial.stopbits

    @property
    def byte_size(self) -> int:
        """Get the byte size."""
        assert self._serial is not None
        return self._serial.byte_size

    @property
    def exclusive(self) -> bool:
        """Get the exclusive setting."""
        assert self._serial is not None
        return self._serial.exclusive

    @abstractmethod
    async def _connect(
        self, *, path: str | None, **kwargs: Unpack[ConnectKwargs]
    ) -> None:
        """Connect to serial port."""
        raise NotImplementedError

    async def connect(
        self,
        *,
        path: str | None,
        **kwargs: Unpack[ConnectKwargs],
    ) -> None:
        """Connect to serial port."""
        target = path
        if target is not None:
            handler = get_uri_handler(target)
            if handler.strip_uri_scheme:
                target = target.removeprefix(handler.scheme).removeprefix(
                    handler.unique_scheme
                )

        try:
            await self._connect(path=target, **kwargs)
        except BaseException:
            # Intentionally catch cancellation too: callers should only observe
            # connect failure/cancel after transport resources are released.
            self.close()
            await self.wait_closed()

            raise

    async def get_modem_pins(self) -> ModemPins:
        """Get modem control bits."""
        self._check_broken()
        return await self._get_modem_pins()

    @abstractmethod
    async def _get_modem_pins(self) -> ModemPins:
        """Get modem control bits, internal."""
        raise NotImplementedError

    @abstractmethod
    async def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set modem control bits, internal."""
        raise NotImplementedError

    async def set_modem_pins(
        self,
        modem_pins: ModemPins | None = None,
        *,
        le: PinState | bool | None = PinState.UNDEFINED,
        dtr: PinState | bool | None = PinState.UNDEFINED,
        rts: PinState | bool | None = PinState.UNDEFINED,
        st: PinState | bool | None = PinState.UNDEFINED,
        sr: PinState | bool | None = PinState.UNDEFINED,
        cts: PinState | bool | None = PinState.UNDEFINED,
        car: PinState | bool | None = PinState.UNDEFINED,
        rng: PinState | bool | None = PinState.UNDEFINED,
        dsr: PinState | bool | None = PinState.UNDEFINED,
    ) -> None:
        """Set modem control bits."""
        self._check_broken()

        pins = modem_pins
        if pins is None:
            pins = ModemPins(
                le=PinState.convert(le),
                dtr=PinState.convert(dtr),
                rts=PinState.convert(rts),
                st=PinState.convert(st),
                sr=PinState.convert(sr),
                cts=PinState.convert(cts),
                car=PinState.convert(car),
                rng=PinState.convert(rng),
                dsr=PinState.convert(dsr),
            )

        return await self._set_modem_pins(pins)

    async def flush(self) -> None:
        """Flush write buffers, waiting until all data is written."""
        self._check_broken()
        await self._flush()

    @abstractmethod
    async def _flush(self) -> None:
        """Flush write buffers, waiting until all data is written, internal."""
        raise NotImplementedError

    async def wait_closed(self) -> None:
        """Wait until transport is fully closed."""
        await self._closed_waiter


def get_serial_classes(
    url: str,
) -> tuple[type[BaseSerial], type[BaseSerialTransport]]:
    """Get the appropriate serial and transport classes based on the URL scheme."""
    handler = get_uri_handler(url)
    return handler.sync_cls, handler.async_transport_cls


@dataclasses.dataclass
class SerialPortInfo:
    """A serial port."""

    device: str
    resolved_device: str

    vid: int | None
    pid: int | None
    serial_number: str | None
    manufacturer: str | None
    product: str | None
    bcd_device: int | None
    interface_description: str | None
    interface_num: int | None

    def __getitem__(self, key: int | slice) -> str | None:
        """Compatibility shim for `serial.tools.list_ports_common.ListPortInfo`."""
        warnings.warn(
            "Slicing `SerialPortInfo` is deprecated, use attributes instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return (str(self.device), self.description, "")[key]

    @property
    def description(self) -> str:
        """Description of the device.

        Warning:
            Deprecated, this description is unstable and will change in the future.

        """

        if self.interface_description is not None:
            product = self.product if self.product is not None else "None"
            return f"{product} - {self.interface_description}"

        if self.product is not None:
            return self.product

        return os.path.basename(self.resolved_device)


def list_serial_ports(
    platform: Platform | str = Platform.DEVICE, **kwargs: Any
) -> list[SerialPortInfo]:
    """List serial ports, defaulting to the system platform."""
    handler = get_uri_handler(platform + "://")
    return handler.list_serial_ports_func(**kwargs)


async def async_list_serial_ports(
    platform: Platform | str = Platform.DEVICE, **kwargs: Any
) -> list[SerialPortInfo]:
    """List serial ports (async), defaulting to the system platform."""
    handler = get_uri_handler(platform + "://")
    return await handler.async_list_serial_ports_func(**kwargs)


def serial_for_url(
    url: str, *args: Any, **kwargs: Unpack[AllConnectKwargs]
) -> BaseSerial:
    """Create the appropriate serial port subclass for the given URL."""
    return BaseSerial.from_url(url, *args, **kwargs)
