"""ESPHome serial proxy transport.

The `ESPHome serial proxy component <https://esphome.io/components/serial_proxy/>`_
exposes any ESPHome serial instance through the ESPHome API. This is used by serialx to
create a fully functional remote serial port, similar to RFC2217 but with many quality
of life, performance, and security improvements.

This platform requires the `aioesphomeapi <https://github.com/esphome/aioesphomeapi>`_
package to be installed, provided by the ``esphome`` dependency group:

.. code-block:: bash

    pip install 'serialx[esphome]'

.. note::
    The aioesphomeapi package does not provide a synchronous interface so the sync
    serial transport relies on spawning an asyncio event loop in a secondary thread in
    order to proxy the async methods. This interface is far from ideal. If you'd like to
    make use of this transport, please migrate your application to the async APIs for
    better compatibility.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import suppress
from enum import Enum, IntFlag
import errno
import functools
import logging
import threading
from typing import Any, ParamSpec, TypeVar, cast
import urllib.parse
import warnings

from aioesphomeapi.client import MIN_VERSION_PROXY_ACK, APIClient
from aioesphomeapi.core import (  # type: ignore[attr-defined]
    APIConnectionError,
    PingRequest,
    PingResponse,
    TimeoutAPIError,
)
from aioesphomeapi.model import (
    ConnectionClosedEvent,
    DisconnectReason,
    SerialProxyDataReceived,
    SerialProxyInfo,
    SerialProxyMode,
    SerialProxyParity,
    SerialProxyRequestResponse,
    SerialProxyStatus,
    SerialProxyUsbInfo,
    SerialProxyUsbInfoFlag,
)
from typing_extensions import Buffer, Unpack

from serialx import SerialException, UnsupportedSetting
from serialx.common import (
    BaseSerial,
    BaseSerialTransport,
    ConnectKwargs,
    ModemPins,
    Parity,
    PinState,
    SerialPortInfo,
    StopBits,
    register_uri_handler,
)

_T = TypeVar("_T")
_S = TypeVar("_S", bound=SerialProxyRequestResponse | None)
_P = ParamSpec("_P")

LOGGER = logging.getLogger(__name__)

ESPHOME_DEFAULT_PORT = 6053

PARITY_MAP = {
    Parity.NONE: SerialProxyParity.NONE,
    Parity.EVEN: SerialProxyParity.EVEN,
    Parity.ODD: SerialProxyParity.ODD,
}

STOP_BITS_MAP = {
    StopBits.ONE: 1,
    StopBits.TWO: 2,
}

STATUS_TO_ERROR_MAP: dict[
    SerialProxyStatus, None | Callable[[str], SerialException | OSError]
] = {
    SerialProxyStatus.OK: None,
    SerialProxyStatus.ASSUMED_SUCCESS: None,
    SerialProxyStatus.TIMEOUT: lambda msg: SerialException(
        f"Operation timed out: {msg}"
    ),
    SerialProxyStatus.NOT_SUPPORTED: lambda msg: SerialException(
        f"Operation is not supported: {msg}"
    ),
    SerialProxyStatus.PORT_IN_USE: lambda msg: OSError(
        errno.EBUSY, f"Serial proxy port is already in use: {msg}"
    ),
    SerialProxyStatus.INVALID_ARGUMENT: lambda msg: SerialException(
        f"Operation is invalid: {msg}"
    ),
}


class InvalidSettingsError(SerialException):
    """Raised when the provided settings are invalid."""


class SerialProxyModeName(str, Enum):
    """Mode requested from the ESPHome serial proxy."""

    RAW = "raw"
    PROTOCOL = "protocol"


# The device's own spelling of the modes, both ways
MODE_MAP = {
    SerialProxyModeName.RAW: SerialProxyMode.RAW,
    SerialProxyModeName.PROTOCOL: SerialProxyMode.PROTOCOL,
}


def parse_serial_proxy_mode(value: SerialProxyModeName | str) -> SerialProxyModeName:
    """Parse a serial proxy mode name, rejecting unknown values."""
    try:
        return SerialProxyModeName(value)
    except ValueError:
        valid = ", ".join(mode.value for mode in SerialProxyModeName)
        raise InvalidSettingsError(
            f"Invalid serial proxy mode {value!r}, expected one of: {valid}"
        ) from None


def translate_esphome_errors(
    func: Callable[_P, Coroutine[Any, Any, _T]],
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Translate aioesphomeapi errors into standard serialx exceptions."""

    @functools.wraps(func)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        try:
            return await func(*args, **kwargs)
        except TimeoutAPIError as exc:
            raise TimeoutError(str(exc)) from exc
        except APIConnectionError as exc:
            raise SerialException(str(exc)) from exc

    return cast(Callable[_P, Coroutine[Any, Any, _T]], wrapper)


def connection_closed_error(event: ConnectionClosedEvent) -> OSError:
    """Translate a closed ESPHome API connection into a broken-link error."""
    if event.error is not None:
        exc = OSError(errno.EIO, f"ESPHome API connection closed: {event.error}")
        exc.__cause__ = event.error
        return exc

    if event.reason is not None and event.reason is not DisconnectReason.UNSPECIFIED:
        return OSError(
            errno.EIO, f"ESPHome device closed the connection: {event.reason.name}"
        )

    return OSError(errno.EIO, "ESPHome API connection closed")


class LineStateFlag(IntFlag):
    """Bitmap of serial line states."""

    RTS = 1
    DTR = 2


class ESPHomeSerial(BaseSerial):
    """Synchronous serial interface over ESPHome serial proxy API.

    Warning:
        ESPHome does not have a native synchronous API, using this interface is
        heavily discouraged. Please use the async API.

    """

    def __init__(
        self,
        *args: Any,
        connect_timeout: float = 10.0,
        loop: asyncio.AbstractEventLoop | None = None,
        api: APIClient | None = None,
        port_name: str | None = None,
        port_instance: int | None = None,
        mode: SerialProxyModeName | str = SerialProxyModeName.RAW,
        usb_serial_number: str | None = None,
        key: str | None = None,
        password: str | None = None,
        noise_psk: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ESPHome serial port.

        Args:
            connect_timeout: Total timeout for connecting to the ESPHome device and
                subscribing to events.
            loop: Event loop to offload async operations to, optional. This is mostly
                intended for internal use from the async transport. If an event loop is
                not provided, the default for the synchronous API, a new one will be
                created at runtime.
            api: An instance of `aioesphomeapi.APIClient` to use. Authentication will
                be skipped and the API will not be disconnected once the serial object
                is closed.
            port_name: The `name` attribute of the ESPHome serial proxy to connect to.
            usb_serial_number: Serial number the USB device behind the port must
                report. A port is a socket, so without this the connection succeeds
                against whatever happens to be plugged in. Also read from a
                ``usb_serial`` query parameter.
            port_instance: The numerical instance ID of the ESPHome serial proxy
                instance to connect to.

                .. deprecated:: 1.2.0
            mode: The mode the serial proxy should use, either `raw` (the default)
                or `protocol` to engage the port's protocol-aware tap.
            key: The Noise PSK to use when creating an `aioesphomeapi.APIClient`
                instance.
            password: The API password to use when creating an `aioesphomeapi.APIClient`
                instance.
            noise_psk: An alias for `key`. Both cannot be passed at once.
            *args: Passed through to `BaseSerial`.
            **kwargs: Passed through to `BaseSerial`.

        """
        super().__init__(*args, **kwargs)

        if self._parity not in PARITY_MAP:
            raise UnsupportedSetting(f"Unsupported parity: {self._parity}")

        if self._stopbits not in STOP_BITS_MAP:
            raise UnsupportedSetting(f"Unsupported stop bits: {self._stopbits}")

        if key and noise_psk:
            raise ValueError("Both `key` and `noise_psk` cannot be provided")

        # This API is used by both the sync API and the async API. The sync API manages
        # a temporary event loop while the async one passes through its own.
        self._loop = loop
        self._loop_thread: threading.Thread | None = None
        self._connect_timeout = connect_timeout

        self._api: APIClient | None = api
        self._client_loop: asyncio.AbstractEventLoop | None = (
            api.loop if api is not None else None
        )
        self._port_name: str | None = port_name
        # When set, the port must have this exact USB device attached or the claim is refused
        # What the device says about the resolved port, known once it has been resolved
        self._port_info: SerialProxyInfo | None = None
        self._instance_id: int | None = port_instance
        self._mode: SerialProxyModeName = parse_serial_proxy_mode(mode)
        self._usb_serial_number: str | None = usb_serial_number
        self._active_mode: SerialProxyModeName | None = None
        self._usb_serial_checked: bool = False
        self._password: str | None = password
        self._noise_psk: str | None = key or noise_psk
        self._disconnect_api: bool = False

        self._read_buffer = bytearray()
        self._read_event = asyncio.Event()
        self._unsub: Callable[[], None] | None = None
        self._closed_unsub: Callable[[], None] | None = None
        self._usb_unsub: Callable[[], None] | None = None
        self._instance_subscribed = False

        self._last_line_states = LineStateFlag(0)

    def _in_event_loop(self) -> bool:
        """Check if we are currently running in the event loop."""
        assert self._loop is not None

        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _call_on_loop(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Dispatch a coroutine to the event loop thread and block."""
        assert self._loop is not None

        # XXX: Running a coroutine synchronously in its own event loop is an instant
        # deadlock of the loop! For projects like Home Assistant that register signal
        # handlers, this deadlock extends to nullifying CTRL-C!
        if self._in_event_loop():
            coro.close()
            raise RuntimeError(
                "Cannot call this sync ESPHomeSerial method from its own event "
                "loop thread; use the async API instead."
            )

        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _call_on_client_loop(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Await `coro` on the `APIClient`'s loop, bridging if needed."""
        client_loop = self._client_loop
        if client_loop is None or client_loop is self._loop:
            return await coro

        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coro, client_loop)
        )

    async def _call_on_client_loop_validated(self, coro: Coroutine[Any, Any, _S]) -> _S:
        """Await a serial proxy `coro` on the `APIClient`'s loop, bridging if needed."""
        result = await self._call_on_client_loop(coro)

        # Earlier ESPHome versions did not provide responses for many serial proxy
        # requests. To work around this, we enqueue a request with a response
        # immediately after and wait for _that_ to finish.
        assert self._api is not None
        version = self._api.api_version

        if version is None or version < MIN_VERSION_PROXY_ACK:
            await self._ping(timeout=self._connect_timeout)

        if result is not None and result.status is not None:
            error_factory = STATUS_TO_ERROR_MAP.get(result.status)
            if error_factory is not None:
                raise error_factory(result.error_message)

        return result

    def _schedule_on_client_loop(
        self,
        fn: Callable[_P, Any],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> None:
        """Invoke a synchronous `APIClient` method on the client's loop."""
        client_loop = self._client_loop
        if client_loop is None or client_loop is self._loop:
            fn(*args, **kwargs)
            return

        client_loop.call_soon_threadsafe(functools.partial(fn, *args, **kwargs))

    def _maybe_start_new_event_loop(self) -> None:
        """Start a new event loop in a background thread if one isn't set."""
        if self._loop is not None:
            return

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever)
        self._loop_thread.start()

    def _on_data(self, msg: SerialProxyDataReceived) -> None:
        if msg.instance != self._instance_id:
            return

        client_loop = self._client_loop
        if client_loop is None or client_loop is self._loop:
            self._handle_incoming_data(msg.data)
        else:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._handle_incoming_data, msg.data)

    def _handle_incoming_data(self, data: bytes) -> None:
        """Apply buffered data on `self._loop` after cross-loop marshalling."""
        self._read_buffer.extend(data)
        self._read_event.set()

    def _on_connection_closed(self, event: ConnectionClosedEvent) -> None:
        """Handle the API connection closing, called on the client's loop."""
        client_loop = self._client_loop
        if client_loop is None or client_loop is self._loop:
            self._handle_connection_closed(event)
        else:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._handle_connection_closed, event)

    def _handle_connection_closed(self, event: ConnectionClosedEvent) -> None:
        """Handle the connection being closed."""
        self._instance_subscribed = False
        self._active_mode = None
        self._mark_broken(connection_closed_error(event))

        # Wake a blocked reader so it raises instead of waiting out its timeout.
        self._read_event.set()

        self._unsubscribe_connection_closed()

    def _usb_device_lost_error(self, info: SerialProxyUsbInfo) -> OSError | None:
        """Return the error a USB identity message means for this port, if any."""
        if self._instance_id is None or info.instance != self._instance_id:
            return None

        if info.status is not SerialProxyStatus.OK:
            return None

        if not info.flags & SerialProxyUsbInfoFlag.CONNECTED:
            return OSError(
                errno.ENXIO, f"USB device removed from serial proxy {self._port_name!r}"
            )

        if (
            self._usb_serial_number is not None
            and info.serial_number != self._usb_serial_number
        ):
            return OSError(
                errno.ENXIO,
                f"Serial proxy {self._port_name!r} now has USB device"
                f" {info.serial_number!r} attached, expected {self._usb_serial_number!r}",
            )

        return None

    def _on_usb_info(self, info: SerialProxyUsbInfo) -> None:
        """Handle a USB identity message, called on the client's loop."""
        client_loop = self._client_loop
        if client_loop is None or client_loop is self._loop:
            self._handle_usb_info(info)
        else:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._handle_usb_info, info)

    def _handle_usb_info(self, info: SerialProxyUsbInfo) -> None:
        """Break the port when the USB device behind it went away."""
        exc = self._usb_device_lost_error(info)
        if exc is None:
            return

        # The port's subscription and the API connection are both still alive; only the
        # device is gone. Like a pulled cable, that is the end of this session, and a new
        # one has to be opened once something is plugged back in.
        self._mark_broken(exc)
        self._read_event.set()

    def _open(self) -> None:
        """Open the serial port."""
        self._maybe_start_new_event_loop()

        self._read_event = asyncio.Event()
        self._call_on_loop(self._async_open())
        self._call_on_loop(self._async_register_data_handler())

    @property
    def tap_mode(self) -> SerialProxyModeName | None:
        """The mode the device confirmed for this port, or `None` before subscribing.

        `raw` means the port has no tap, so a client must do the protocol's work itself.
        """
        return self._active_mode

    @property
    def is_open(self) -> bool:
        """Return whether the serial port is open."""
        return self._api is not None

    @translate_esphome_errors
    async def _async_open(self) -> None:
        # Only connect if the API was not passed in externally
        if self._api is None:
            if self._path is None:
                raise InvalidSettingsError(
                    "Cannot open a connection without a URI or a API instance"
                )

            parsed = urllib.parse.urlparse(str(self._path))
            params = urllib.parse.parse_qs(parsed.query)

            if "port_name" in params:
                port_value = params["port_name"][0]
            else:
                # Backwards compat: esphome://host:port/{instance_id}
                port_value = urllib.parse.unquote(parsed.path.strip("/"))

            if port_value.isdigit():
                self._instance_id = int(port_value)
            elif not self._port_name:
                self._port_name = port_value

            if "usb_serial" in params:
                self._usb_serial_number = params["usb_serial"][0]

            if "mode" in params:
                self._mode = parse_serial_proxy_mode(params["mode"][0])

            if "password" in params:
                self._password = params["password"][0]

            if "noise_psk" in params and "key" in params:
                raise ValueError("Both `key` and `noise_psk` cannot be provided")
            elif "noise_psk" in params:
                self._noise_psk = params["noise_psk"][0]
            elif "key" in params:
                self._noise_psk = params["key"][0]

            if parsed.hostname is None:
                raise InvalidSettingsError(f"URI {self._path!r} is missing a hostname")

            self._api = APIClient(
                address=parsed.hostname,
                port=parsed.port or ESPHOME_DEFAULT_PORT,
                password=self._password,
                noise_psk=self._noise_psk,
                # A serial proxy client is not the device's time source
                provide_time=False,
            )
            self._client_loop = self._api.loop

            self._disconnect_api = True
            await self._call_on_client_loop(self._api.connect(login=True))
        else:
            # Don't disconnect an externally-passed API
            self._disconnect_api = False

        if self._closed_unsub is None:
            self._closed_unsub = await self._call_on_client_loop(
                self._register_closed_handler()
            )

        # Before the port is resolved and its USB identity checked, so a device pulled in
        # between is not missed
        if self._usb_unsub is None:
            self._usb_unsub = await self._call_on_client_loop(
                self._register_usb_info_handler()
            )

    async def _register_closed_handler(self) -> Callable[[], None]:
        """Register `_on_connection_closed` on the client's loop and return the unsub."""
        assert self._api is not None
        return self._api.add_connection_closed_callback(self._on_connection_closed)

    async def _register_usb_info_handler(self) -> Callable[[], None]:
        """Register `_on_usb_info` on the client's loop and return the unsub."""
        assert self._api is not None
        return self._api.subscribe_serial_proxy_usb_info(self._on_usb_info)

    @translate_esphome_errors
    async def _async_list_serial_ports(self) -> list[SerialPortInfo]:
        assert self._api is not None

        device_info = await self._call_on_client_loop(self._api.device_info())
        ports = []

        for proxy in device_info.serial_proxies:
            url = urllib.parse.urlunparse(
                urllib.parse.ParseResult(
                    scheme="esphome",
                    netloc=f"{self._api.address}:{self._api.port}",
                    path="/",
                    params="",
                    query=urllib.parse.urlencode({"port_name": proxy.name}),
                    fragment="",
                )
            )

            ports.append(
                SerialPortInfo(
                    device=url,
                    resolved_device=url,
                    vid=None,
                    pid=None,
                    serial_number=device_info.mac_address,
                    manufacturer=device_info.manufacturer,
                    product=device_info.model,
                    bcd_device=None,
                    interface_description=proxy.name,
                    interface_num=None,
                )
            )

        return ports

    def _list_serial_ports(self) -> list[SerialPortInfo]:
        return self._call_on_loop(self._async_list_serial_ports())

    @translate_esphome_errors
    async def _async_register_data_handler(self) -> None:
        """Install the local data handler."""
        assert self._api is not None
        if self._unsub is not None:
            return
        self._unsub = await self._call_on_client_loop(self._register_data_handler())

    async def _register_data_handler(self) -> Callable[[], None]:
        """Register `_on_data` on the client's loop and return the unsub."""
        assert self._api is not None
        return self._api.subscribe_serial_proxy_data(self._on_data)

    @translate_esphome_errors
    async def _ping(self, *, timeout: float) -> None:
        """Ping the ESPHome API."""
        assert self._api is not None
        await self._call_on_client_loop(self._send_ping(timeout=timeout))

    async def _send_ping(self, *, timeout: float) -> None:
        """Issue a ping request on the client's loop."""
        assert self._api is not None
        conn = self._api._get_connection()

        await conn.send_messages_await_response_complex(
            messages=(PingRequest(),),
            do_append=None,
            do_stop=lambda msg: isinstance(msg, PingResponse),
            msg_types=(PingResponse,),
            timeout=timeout,
        )

    async def _resolve_instance_id(self) -> None:
        """Resolve `_instance_id` from `_port_name` against the device."""
        if self._api is None:
            return

        if self._instance_id is None:
            await self._resolve_instance_id_from_name()

        # Also reached when `port_instance` skipped the lookup above
        if self._usb_serial_number is not None and not self._usb_serial_checked:
            self._usb_serial_checked = True
            await self._check_usb_serial_number()

    async def _resolve_instance_id_from_name(self) -> None:
        """Look `_instance_id` up by `_port_name`."""
        assert self._api is not None
        assert self._port_name is not None
        info = await self._call_on_client_loop(self._api.device_info())

        name_to_info_mapping = {
            proxy_info.name: (index, proxy_info)
            for index, proxy_info in enumerate(info.serial_proxies)
        }

        if self._port_name not in name_to_info_mapping:
            raise ValueError(
                f"Serial proxy with name {self._port_name!r}"
                f" does not exist in {name_to_info_mapping!r}"
            )

        instance_id, proxy_info = name_to_info_mapping[self._port_name]
        self._instance_id = instance_id
        self._port_info = proxy_info

    async def _check_usb_serial_number(self) -> None:
        """Fail unless the device behind the port reports the expected serial number."""
        assert self._api is not None
        assert self._instance_id is not None

        usb_info = await self._call_on_client_loop(
            self._api.serial_proxy_get_usb_info(self._instance_id)
        )

        if usb_info.status is not None:
            error_factory = STATUS_TO_ERROR_MAP.get(usb_info.status)
            if error_factory is not None:
                raise error_factory("cannot read the port's USB identity")

        if not usb_info.flags & SerialProxyUsbInfoFlag.CONNECTED:
            raise SerialException(
                f"No USB device is attached to serial proxy {self._port_name!r}"
            )

        if usb_info.serial_number != self._usb_serial_number:
            raise SerialException(
                f"Serial proxy {self._port_name!r} has USB device"
                f" {usb_info.serial_number!r} attached, expected"
                f" {self._usb_serial_number!r}"
            )

    async def _subscribe_instance(self) -> None:
        """Subscribe serial proxy streaming for this instance if supported."""
        if self._api is None or self._instance_subscribed:
            return

        await self._resolve_instance_id()
        assert self._instance_id is not None

        # Awaited, not scheduled: the device answers a claim, and a refusal has to become an
        # exception here. Fire-and-forget would leave a rejected client waiting on a port it
        # never got, which looks exactly like a device with nothing to say.
        await self._call_on_client_loop_validated(
            self._api.serial_proxy_subscribe_await_response(
                self._instance_id,
                timeout=self._connect_timeout,
            )
        )
        self._instance_subscribed = True

        # Only the subscriber may set the mode, and `raw` is sent too so a client that
        # wants plain bytes can turn a tap off that an earlier session left on
        response = await self._call_on_client_loop(
            self._api.serial_proxy_set_mode_await_response(
                instance=self._instance_id,
                mode=MODE_MAP[self._mode],
                timeout=self._connect_timeout,
            )
        )

        # A port with no tap refuses PROTOCOL; that is a fact about the port, not a failure
        if response.status is SerialProxyStatus.NOT_SUPPORTED:
            self._active_mode = SerialProxyModeName.RAW
        elif (factory := STATUS_TO_ERROR_MAP.get(response.status)) is not None:
            raise factory(f"cannot set the mode of serial proxy {self._port_name!r}")
        else:
            self._active_mode = self._mode

    def _unsubscribe_instance(self) -> None:
        """Unsubscribe serial proxy streaming for this instance if supported."""
        if self._api is None or not self._instance_subscribed:
            return

        assert self._instance_id is not None
        with suppress(APIConnectionError):
            self._schedule_on_client_loop(
                self._api.serial_proxy_unsubscribe, self._instance_id
            )

        self._instance_subscribed = False
        self._active_mode = None

    def _configure_port(self) -> None:
        """Configure the serial port settings."""
        self._call_on_loop(self._async_configure_port())

    @translate_esphome_errors
    async def _async_configure_port(self) -> None:
        """Configure the serial port settings."""
        assert self._api is not None
        await self._resolve_instance_id()
        assert self._instance_id is not None

        # Since API 1.17 only the subscribed client may configure a port
        await self._subscribe_instance()

        await self._call_on_client_loop_validated(
            self._api.serial_proxy_configure_await_response(
                instance=self._instance_id,
                baudrate=self._baudrate,
                flow_control=self._rtscts,
                parity=PARITY_MAP[self._parity],
                stop_bits=STOP_BITS_MAP[self._stopbits],
                data_size=self._byte_size,
            )
        )

    def _compute_line_states(self, modem_pins: ModemPins) -> LineStateFlag:
        line_states = self._last_line_states

        if modem_pins.rts is PinState.HIGH:
            line_states |= LineStateFlag.RTS
        elif modem_pins.rts is PinState.LOW:
            line_states &= ~LineStateFlag.RTS

        if modem_pins.dtr is PinState.HIGH:
            line_states |= LineStateFlag.DTR
        elif modem_pins.dtr is PinState.LOW:
            line_states &= ~LineStateFlag.DTR

        self._last_line_states = line_states

        return line_states

    @translate_esphome_errors
    async def _async_set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Send a signal to set modem control bits, without waiting for a response."""
        assert self._api is not None
        assert self._instance_id is not None

        line_states = self._compute_line_states(modem_pins)
        await self._call_on_client_loop_validated(
            self._api.serial_proxy_set_modem_pins_await_response(
                instance=self._instance_id,
                line_states=line_states,
            )
        )

        await self._async_get_modem_pins()

    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set modem control bits."""
        assert self._loop is not None

        # XXX: This is the one sync API we allow to be used from an async context, by
        # enqueuing the operation instead of blocking.
        if self._in_event_loop():
            warnings.warn(
                "Use `await transport.set_modem_pins(...)` instead.",
                DeprecationWarning,
                stacklevel=2,
            )

            assert self._api is not None
            assert self._instance_id is not None

            line_states = self._compute_line_states(modem_pins)
            self._schedule_on_client_loop(
                self._api.serial_proxy_set_modem_pins,
                instance=self._instance_id,
                line_states=line_states,
            )
            return

        self._call_on_loop(self._async_set_modem_pins(modem_pins))

    def _get_modem_pins(self) -> ModemPins:
        return self._call_on_loop(self._async_get_modem_pins())

    @translate_esphome_errors
    async def _async_get_modem_pins(self) -> ModemPins:
        assert self._api is not None
        assert self._instance_id is not None
        rsp = await self._call_on_client_loop(
            self._api.serial_proxy_get_modem_pins(instance=self._instance_id)
        )
        self._last_line_states = LineStateFlag(rsp.line_states)

        return ModemPins(
            dtr=PinState.convert(bool(rsp.line_states & LineStateFlag.DTR)),
            rts=PinState.convert(bool(rsp.line_states & LineStateFlag.RTS)),
        )

    def num_unread_bytes(self) -> int:
        """Return the number of bytes waiting to be read."""
        return len(self._read_buffer)

    def num_unwritten_bytes(self) -> int:
        """Return the number of bytes waiting to be written."""
        return 0

    def _reset_read_buffer(self) -> None:
        """Reset the read buffer."""
        self._read_buffer.clear()

    def _reset_write_buffer(self) -> None:
        """Reset the write buffer."""

    @translate_esphome_errors
    async def _async_flush(self) -> None:
        """Flush write buffers."""
        assert self._api is not None
        assert self._instance_id is not None
        await self._call_on_client_loop_validated(
            self._api.serial_proxy_flush(instance=self._instance_id)
        )

    def _flush(self) -> None:
        """Flush write buffers."""
        self._call_on_loop(self._async_flush())

    def _write(self, b: Buffer, *, timeout: float | None) -> int:
        """Write bytes to serial port."""
        assert self._api is not None
        assert self._instance_id is not None
        data = bytes(b)
        self._schedule_on_client_loop(
            self._api.serial_proxy_write, instance=self._instance_id, data=data
        )
        return len(data)

    def _readinto(self, b: Buffer, *, timeout: float | None) -> int:
        """Read bytes from serial port into buffer."""
        try:
            return self._call_on_loop(self._async_readinto(b, timeout))
        except TimeoutError:
            return 0

    async def _async_readinto(self, b: Buffer, timeout: float | None) -> int:
        async with asyncio.timeout(timeout):  # type:ignore[unused-ignore,attr-defined]
            while not self._read_buffer:
                self._check_broken()
                self._read_event.clear()
                await self._read_event.wait()

        m = memoryview(b).cast("B")
        n = min(len(m), len(self._read_buffer))
        m[:n] = self._read_buffer[:n]
        del self._read_buffer[:n]
        return n

    def _unsubscribe_connection_closed(self) -> None:
        """Drop this serial's API close-event subscription, if it has one."""
        if self._closed_unsub is not None:
            self._schedule_on_client_loop(self._closed_unsub)
            self._closed_unsub = None

    def _unsubscribe_callbacks(self) -> None:
        """Drop every callback `_async_open` registered on the API client."""
        if self._unsub is not None:
            self._schedule_on_client_loop(self._unsub)
            self._unsub = None

        if self._usb_unsub is not None:
            self._schedule_on_client_loop(self._usb_unsub)
            self._usb_unsub = None

        self._unsubscribe_connection_closed()

    async def _async_close(self) -> None:
        """Close the serial port from a caller that already has a running loop."""
        self._unsubscribe_callbacks()

        if self._disconnect_api and self._api is not None:
            self._unsubscribe_instance()
            await self._call_on_client_loop(self._api.disconnect())
            self._api = None

    def _close(self) -> None:
        """Close the serial port."""
        self._unsubscribe_callbacks()

        if self._disconnect_api and self._api is not None:
            self._unsubscribe_instance()
            self._call_on_loop(self._call_on_client_loop(self._api.disconnect()))
            self._api = None

        if self._loop_thread is not None:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join()
            self._loop.close()
            self._loop = None
            self._loop_thread = None


class ESPHomeSerialTransport(BaseSerialTransport):
    """Serial transport over ESPHome serial proxy API."""

    transport_name = "esphome"
    _serial_cls: type[ESPHomeSerial] = ESPHomeSerial
    _serial: ESPHomeSerial | None

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        """Initialize the ESPHome serial transport."""
        super().__init__(loop, protocol)
        self._unsub: Callable[[], None] | None = None
        self._closed_unsub: Callable[[], None] | None = None
        self._usb_unsub: Callable[[], None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    @translate_esphome_errors
    async def _connect(
        self, *, path: str | None = None, **kwargs: Unpack[ConnectKwargs]
    ) -> None:
        self._serial = self._serial_cls(loop=self._loop, path=path, **kwargs)
        self._extra["serial"] = self._serial

        assert self._serial is not None
        await self._serial._async_open()

        assert self._serial._api is not None
        # Install the data handler *before* subscribing. The device starts streaming as
        # soon as the subscribe lands, so anything it sends in the gap -- a bootloader
        # banner, a chatty sensor's first reading -- is silently dropped.
        self._unsub = await self._serial._call_on_client_loop(
            self._register_transport_data_handler()
        )
        self._closed_unsub = await self._serial._call_on_client_loop(
            self._register_transport_closed_handler()
        )
        self._usb_unsub = await self._serial._call_on_client_loop(
            self._register_transport_usb_info_handler()
        )

        # Nothing is replayed for a connection that closed while we were setting
        # up, so the state has to be re-checked once the callback is installed.
        if not self._serial._api.is_connected:
            raise SerialException("ESPHome API connection closed while connecting")

        await self._serial._async_configure_port()

        self._call_protocol_connection_made()

    async def _register_transport_closed_handler(self) -> Callable[[], None]:
        """Register `_on_api_connection_closed` on the client's loop, return the unsub."""
        assert self._serial is not None
        assert self._serial._api is not None

        # This isn't a coroutine but needs to be run in the target loop
        unsub: Callable[[], None] = self._serial._api.add_connection_closed_callback(
            self._on_api_connection_closed
        )
        return unsub

    def _on_api_connection_closed(self, event: ConnectionClosedEvent) -> None:
        """Handle the API connection closing, called on the client's loop."""
        assert self._serial is not None

        client_loop = self._serial._client_loop
        if client_loop is None or client_loop is self._loop:
            self._api_connection_lost(event)
        else:
            self._loop.call_soon_threadsafe(self._api_connection_lost, event)

    def _api_connection_lost(self, event: ConnectionClosedEvent) -> None:
        """Tear down the transport after the API connection closed."""
        if self._connection_lost_called:
            return

        self._closing = True

        if not self._user_initiated_close:
            self._mark_broken(connection_closed_error(event))

        exc = None if event.expected_disconnect else connection_closed_error(event)
        self._call_protocol_connection_lost(exc)

        if self._closed_unsub is not None:
            self._schedule_unsub(self._closed_unsub)
            self._closed_unsub = None

    async def _register_transport_usb_info_handler(self) -> Callable[[], None]:
        """Register `_on_api_usb_info` on the client's loop, return the unsub."""
        assert self._serial is not None
        assert self._serial._api is not None

        # This isn't a coroutine but needs to be run in the target loop
        unsub: Callable[[], None] = self._serial._api.subscribe_serial_proxy_usb_info(
            self._on_api_usb_info
        )
        return unsub

    def _on_api_usb_info(self, info: SerialProxyUsbInfo) -> None:
        """Handle a USB identity message, called on the client's loop."""
        assert self._serial is not None

        exc = self._serial._usb_device_lost_error(info)
        if exc is None:
            return

        client_loop = self._serial._client_loop
        if client_loop is None or client_loop is self._loop:
            self._usb_device_lost(exc)
        else:
            self._loop.call_soon_threadsafe(self._usb_device_lost, exc)

    def _usb_device_lost(self, exc: OSError) -> None:
        """Tear down the transport after the USB device behind the port went away."""
        if self._connection_lost_called or self._closing:
            return

        self._mark_broken(exc)
        self._close_with(exc)

    def _schedule_unsub(self, unsub: Callable[[], None]) -> None:
        """Run an unsub on the client's loop, or inline if the serial is gone."""
        serial = self._serial
        if serial is not None:
            serial._schedule_on_client_loop(unsub)
        else:
            unsub()

    async def _register_transport_data_handler(self) -> Callable[[], None]:
        """Register `_on_data` on the client's loop and return the unsub."""
        assert self._serial is not None
        assert self._serial._api is not None

        # This isn't a coroutine but needs to be run in the target loop
        unsub: Callable[[], None] = self._serial._api.subscribe_serial_proxy_data(
            self._on_data
        )
        return unsub

    def _on_data(self, msg: SerialProxyDataReceived) -> None:
        assert self._serial is not None
        if msg.instance != self._serial._instance_id:
            return

        client_loop = self._serial._client_loop
        if client_loop is None or client_loop is self._loop:
            self._protocol.data_received(msg.data)
        else:
            self._loop.call_soon_threadsafe(self._protocol.data_received, msg.data)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Write data to the serial proxy."""
        # Before the _closing check: a broken link must raise rather than
        # silently drop the write, while a user-requested close stays quiet.
        self._check_broken()
        if self._closing:
            return
        assert self._serial is not None
        self._serial.write(data)

    def is_closing(self) -> bool:
        """Return whether the transport is closing."""
        return self._closing

    def close(self) -> None:
        """Close the transport."""
        if self._closing:
            return
        self._mark_user_closed()
        self._close_with(None)

    def _close_with(self, exc: OSError | None) -> None:
        """Release everything and tell the protocol why, or None for a clean close."""
        self._closing = True

        serial = self._serial
        if self._unsub is not None:
            self._schedule_unsub(self._unsub)
            self._unsub = None

        if self._usb_unsub is not None:
            self._schedule_unsub(self._usb_unsub)
            self._usb_unsub = None

        if self._closed_unsub is not None:
            self._schedule_unsub(self._closed_unsub)
            self._closed_unsub = None

        if serial is None:
            self._call_protocol_connection_lost(exc)
            return

        serial._unsubscribe_instance()
        serial._unsubscribe_callbacks()

        if not serial._disconnect_api:
            # Transport does not own the external API lifecycle.
            self._call_protocol_connection_lost(exc)
            return

        api = serial._api
        serial._api = None
        if api is None:
            self._call_protocol_connection_lost(exc)
            return

        # TODO: clean shutdown without `wait_closed()` needs a public sync
        # force-disconnect on APIClient (aioesphomeapi); today only the
        # private `api._connection.force_disconnect()` is sync.
        self._close_task = self._loop.create_task(self._async_close(api, exc))

    def abort(self) -> None:
        """Abort the transport immediately."""
        self.close()

    async def _async_close(self, api: APIClient, exc: OSError | None) -> None:
        """Close the API connection, then tell the protocol why the transport went."""
        assert self._serial is not None
        try:
            await self._serial._call_on_client_loop(api.disconnect())
        finally:
            self._call_protocol_connection_lost(exc)

    async def _flush(self) -> None:
        """Flush write buffers, waiting until all data is written, internal."""
        assert self._serial is not None
        await self._serial._async_flush()

    async def _get_modem_pins(self) -> ModemPins:
        """Get modem control bits, internal."""
        assert self._serial is not None
        return await self._serial._async_get_modem_pins()

    async def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Set modem control bits, internal."""
        assert self._serial is not None
        await self._serial._async_set_modem_pins(modem_pins)

    def get_write_buffer_size(self) -> int:
        """Get the number of bytes currently in the write buffer."""
        return 0


def esphome_list_serial_ports(*args: Any, **kwargs: Any) -> list[SerialPortInfo]:
    """List serial ports for ESPhome."""
    serial = ESPHomeSerial(*args, **kwargs)
    serial._maybe_start_new_event_loop()

    try:
        try:
            serial._call_on_loop(serial._async_open())
        except InvalidSettingsError:
            return []
        return serial._list_serial_ports()
    finally:
        serial._close()


async def async_esphome_list_serial_ports(
    *args: Any, **kwargs: Any
) -> list[SerialPortInfo]:
    """List serial ports for ESPhome."""
    serial = ESPHomeSerial(*args, **kwargs)

    try:
        await serial._async_open()
    except InvalidSettingsError:
        return []

    try:
        return await serial._async_list_serial_ports()
    finally:
        await serial._async_close()


register_uri_handler(
    scheme="esphome://",
    unique_scheme="esphome://",
    sync_cls=ESPHomeSerial,
    async_transport_cls=ESPHomeSerialTransport,
    list_serial_ports_func=esphome_list_serial_ports,
    async_list_serial_ports_func=async_esphome_list_serial_ports,
)
