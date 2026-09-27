"""Tests for the URI handler registration API."""

from __future__ import annotations

from collections.abc import Generator
import errno
import logging
import traceback
from typing import Any
from unittest.mock import Mock

import pytest

from serialx import (
    Platform,
    async_list_serial_ports,
    get_serial_classes,
    list_serial_ports,
    register_uri_handler,
)
from serialx.common import (
    _REGISTERED_URI_HANDLERS,
    BACKEND_CONNECT_KWARGS,
    AllConnectKwargs,
    BaseSerial,
    BaseSerialTransport,
    PortSettingsUpdate,
    SerialPortInfo,
    UnknownUriScheme,
    get_uri_handler,
    route_backend_kwargs,
)


async def _async_list_serial_ports() -> list[SerialPortInfo]:
    return []


@pytest.fixture(autouse=True)
def ensure_registry_untouched() -> Generator[None]:
    """Snapshot the URI handler registry and restore it after each test."""
    snapshot = {
        scheme: list(items)
        for scheme, items in _REGISTERED_URI_HANDLERS.items()
        if items
    }

    try:
        yield
    finally:
        after = {
            scheme: list(items)
            for scheme, items in _REGISTERED_URI_HANDLERS.items()
            if items
        }
        if after != snapshot:
            pytest.fail(
                f"URI handlers were leaked by the test! Before: {snapshot}, after: {after}"
            )


def test_register_uri_handler_validation() -> None:
    """`register_uri_handler` rejects bad schemes and duplicate registrations."""
    with pytest.raises(ValueError, match="must end with"):
        register_uri_handler(
            scheme="bad",
            unique_scheme="test-unique-1://",
            sync_cls=BaseSerial,  # type:ignore[type-abstract]
            async_transport_cls=BaseSerialTransport,  # type:ignore[type-abstract]
            list_serial_ports_func=list,
            async_list_serial_ports_func=_async_list_serial_ports,
        )

    with pytest.raises(ValueError, match="must end with"):
        register_uri_handler(
            scheme="test-shared-1://",
            unique_scheme="bad",
            sync_cls=BaseSerial,  # type:ignore[type-abstract]
            async_transport_cls=BaseSerialTransport,  # type:ignore[type-abstract]
            list_serial_ports_func=list,
            async_list_serial_ports_func=_async_list_serial_ports,
        )

    unregister = register_uri_handler(
        scheme="test-shared-1://",
        unique_scheme="test-unique-1://",
        sync_cls=BaseSerial,  # type:ignore[type-abstract]
        async_transport_cls=BaseSerialTransport,  # type:ignore[type-abstract]
        list_serial_ports_func=list,
        async_list_serial_ports_func=_async_list_serial_ports,
    )

    try:
        with pytest.raises(ValueError, match="not unique"):
            register_uri_handler(
                scheme="test-shared-1://",
                unique_scheme="test-unique-1://",
                sync_cls=BaseSerial,  # type:ignore[type-abstract]
                async_transport_cls=BaseSerialTransport,  # type:ignore[type-abstract]
                list_serial_ports_func=list,
                async_list_serial_ports_func=_async_list_serial_ports,
            )
    finally:
        unregister()


def test_register_uri_handler_dispatch_and_unregister() -> None:
    """Registered handlers are discoverable via the public sync/async APIs."""
    mock_sync_cls = Mock(spec=type[BaseSerial])
    mock_async_transport_cls = Mock(spec=type[BaseSerialTransport])

    unregister = register_uri_handler(
        scheme="test-shared-2://",
        unique_scheme="test-unique-2://",
        sync_cls=mock_sync_cls,
        async_transport_cls=mock_async_transport_cls,
        list_serial_ports_func=list,
        async_list_serial_ports_func=_async_list_serial_ports,
    )

    for url in ("test-unique-2://", "test-shared-2://host/path"):
        handler = get_uri_handler(url)
        assert handler.sync_cls is mock_sync_cls
        assert handler.async_transport_cls is mock_async_transport_cls

        sync_cls, async_transport_cls = get_serial_classes(url)
        assert sync_cls is mock_sync_cls
        assert async_transport_cls is mock_async_transport_cls

    unregister()

    with pytest.raises(UnknownUriScheme):
        get_uri_handler("test-unique-2://")

    with pytest.raises(UnknownUriScheme):
        get_uri_handler("test-shared-2://")


def test_backend_connect_kwargs_match_typed_dict() -> None:
    """Every kwarg in the runtime table is also declared on `AllConnectKwargs`."""
    declared: set[str] = set()

    for extras in BACKEND_CONNECT_KWARGS.values():
        declared |= extras

    assert declared <= set(AllConnectKwargs.__optional_keys__)


class _StubSerial(BaseSerial):
    """A concrete `BaseSerial` whose abstract methods are inert stubs."""

    def _open(self) -> None: ...
    def _close(self) -> None: ...
    def _reconfigure_port(self, update: PortSettingsUpdate) -> None: ...
    def _flush(self) -> None: ...
    def _readinto(self, buf: Any) -> int: ...  # type:ignore[empty-body, override]
    def _write(self, data: Any) -> int: ...  # type:ignore[empty-body, override]
    def _reset_read_buffer(self) -> None: ...
    def _reset_write_buffer(self) -> None: ...
    def _get_modem_pins(self) -> Any: ...
    def _set_modem_pins(self, modem_pins: Any) -> None: ...

    @property
    def is_open(self) -> bool: ...  # type:ignore[empty-body]

    @property
    def num_unread_bytes(self) -> int: ...  # type:ignore[empty-body, override]

    @property
    def num_unwritten_bytes(self) -> int: ...  # type:ignore[empty-body, override]


def test_check_broken_raises_fresh_instance() -> None:
    """The stored exception is not re-raised, so its traceback cannot grow."""
    serial = _StubSerial("/dev/null", do_not_open=True)
    original = OSError(errno.EIO, "device disconnected", "/dev/ttyUSB0")
    original.marked_during = "read"  # type: ignore[attr-defined]
    serial._mark_broken(original)

    raised: list[OSError] = []

    for _ in range(3):
        with pytest.raises(OSError, match="device disconnected") as excinfo:
            serial._check_broken()

        raised.append(excinfo.value)

    assert len({id(exc) for exc in raised}) == len(raised)

    for exc in raised:
        assert exc is not original
        assert exc.__cause__ is original
        assert exc.errno == errno.EIO
        assert exc.strerror == "device disconnected"
        assert exc.filename == "/dev/ttyUSB0"
        assert exc.marked_during == "read"  # type: ignore[attr-defined]

    assert original.__traceback__ is None
    assert len({len(traceback.extract_tb(exc.__traceback__)) for exc in raised}) == 1


def test_check_broken_unreconstructible_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exceptions that `copy.copy` cannot rebuild still get a bounded traceback."""

    class CustomError(Exception):
        def __init__(self, code: int, *, detail: str) -> None:
            super().__init__(code, detail)
            self.code = code
            self.detail = detail

    serial = _StubSerial("/dev/null", do_not_open=True)
    original = CustomError(1, detail="boom")
    serial._mark_broken(original)

    lengths: list[int] = []

    for _ in range(3):
        with (
            caplog.at_level(logging.DEBUG, logger="serialx.common"),
            pytest.raises(CustomError) as excinfo,
        ):
            serial._check_broken()

        assert excinfo.value is original
        assert excinfo.value.__context__ is None
        lengths.append(len(traceback.extract_tb(excinfo.value.__traceback__)))

    assert len(set(lengths)) == 1

    records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(records) == 3
    assert "Failed to clone exception" in records[0].getMessage()


def test_serial_kwarg_forwarding(caplog: pytest.LogCaptureFixture) -> None:
    """."""

    class TestSerialBackend(_StubSerial):
        def __init__(self, *args: Any, test_opt: int = 1, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)

    unregister = register_uri_handler(
        scheme="test-backend://",
        unique_scheme="test-backend://",
        sync_cls=TestSerialBackend,
        async_transport_cls=BaseSerialTransport,  # type: ignore[type-abstract]
        connect_kwargs=frozenset({"test_opt"}),
    )

    try:
        handler = get_uri_handler("test-backend://")

        # The chosen backend's own kwarg and common kwargs are forwarded
        assert route_backend_kwargs(handler, {"test_opt": 5, "baudrate": 115200}) == {
            "test_opt": 5,
            "baudrate": 115200,
        }

        # A built-in backend-specific kwarg is dropped (not raised) for other backends
        with caplog.at_level(logging.DEBUG, logger="serialx.common"):
            assert route_backend_kwargs(handler, {"low_latency": False}) == {}

        assert "low_latency" in caplog.text

        # A typo isn't backend-specific, so it's forwarded for the backend to reject
        assert route_backend_kwargs(handler, {"test_opt": 1}) == {"test_opt": 1}

        # `from_url` drops other-backend kwargs; a real typo raises from the backend
        instance = BaseSerial.from_url(
            "test-backend://dev", low_latency=False, baudrate=4800
        )
        assert isinstance(instance, TestSerialBackend)
        assert instance.baudrate == 4800

        with pytest.raises(TypeError, match="nonsense"):
            BaseSerial.from_url("test-backend://dev", nonsense=1)  # type: ignore[call-arg]
    finally:
        unregister()


@pytest.mark.parametrize(
    ("port", "expected"),
    [
        # CP2102: product and interface_description are identical
        (
            SerialPortInfo(
                device="/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_ec4903cb-if00-port0",
                resolved_device="/dev/ttyUSB0",
                vid=0x10C4,
                pid=0xEA60,
                serial_number="ec4903cb",
                manufacturer="Silicon Labs",
                product="CP2102 USB to UART Bridge Controller",
                bcd_device=0x0100,
                interface_description="CP2102 USB to UART Bridge Controller",
                interface_num=0,
            ),
            "CP2102 USB to UART Bridge Controller - CP2102 USB to UART Bridge Controller",
        ),
        # Prolific: interface_description is None, falls back to product
        (
            SerialPortInfo(
                device="/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_DSDCb147613-if00-port0",
                resolved_device="/dev/ttyUSB3",
                vid=0x067B,
                pid=0x23A3,
                serial_number="DSDCb147613",
                manufacturer="Prolific Technology Inc.",
                product="USB-Serial Controller",
                bcd_device=0x0605,
                interface_description=None,
                interface_num=0,
            ),
            "USB-Serial Controller",
        ),
        # ZBT-2: distinct product and interface_description (regression case)
        (
            SerialPortInfo(
                device="/dev/serial/by-id/usb-Nabu_Casa_ZBT-2_80B54EEFAE18-if00",
                resolved_device="/dev/ttyACM0",
                vid=0x303A,
                pid=0x4005,
                serial_number="80B54EEFAE18",
                manufacturer="Nabu Casa",
                product="ZBT-2",
                bcd_device=0x0100,
                interface_description="Nabu Casa ZBT-2",
                interface_num=0,
            ),
            "ZBT-2 - Nabu Casa ZBT-2",
        ),
        # Native UART: no USB info, falls back to basename of resolved_device
        (
            SerialPortInfo(
                device="/dev/ttyAMA0",
                resolved_device="/dev/ttyAMA0",
                vid=None,
                pid=None,
                serial_number=None,
                manufacturer=None,
                product=None,
                bcd_device=None,
                interface_description=None,
                interface_num=None,
            ),
            "ttyAMA0",
        ),
        # Bug-for-bug: interface_description without product
        (
            SerialPortInfo(
                device="/dev/ttyUSB9",
                resolved_device="/dev/ttyUSB9",
                vid=0x1234,
                pid=0x5678,
                serial_number=None,
                manufacturer=None,
                product=None,
                bcd_device=None,
                interface_description="Some Interface",
                interface_num=0,
            ),
            "None - Some Interface",
        ),
    ],
)
def test_serial_port_info_description(port: SerialPortInfo, expected: str) -> None:
    """`description` mirrors pyserial's `usb_description()` output."""
    assert port.description == expected


@pytest.mark.parametrize("platform", list(Platform))
def test_list_serial_ports_all_platforms(platform: Platform) -> None:
    """Sync listing returns a list of `SerialPortInfo` for every platform."""
    try:
        ports = list_serial_ports(platform)
    except UnknownUriScheme:
        pytest.skip(f"{platform} is not registered in this environment")
        return

    assert isinstance(ports, list)
    for port in ports:
        assert isinstance(port, SerialPortInfo)


@pytest.mark.parametrize("platform", list(Platform))
async def test_async_list_serial_ports_all_platforms(platform: Platform) -> None:
    """Async listing returns a list of `SerialPortInfo` for every platform."""
    try:
        ports = await async_list_serial_ports(platform)
    except UnknownUriScheme:
        pytest.skip(f"{platform} is not registered in this environment")
        return

    assert isinstance(ports, list)
    for port in ports:
        assert isinstance(port, SerialPortInfo)
