"""ESPHome Zigbee proxy transport.

The `ESPHome zigbee_proxy component <https://esphome.io/components/zigbee_proxy/>`_
bridges an EmberZNet NCP through the ESPHome API. Unlike ``serial_proxy``, which
shuttles an opaque byte stream, ``zigbee_proxy`` shuttles complete ASH frames: each
``ZigbeeProxyFrame`` carries one framed, CRC'd, byte-stuffed ASH frame.

Exposing that as a byte stream is nonetheless safe in both directions. The ESPHome
side feeds whatever it receives into a byte-oriented ASH parser, so a write need not
align to a frame boundary, and ASH clients such as bellows already reassemble frames
from an arbitrarily chunked stream. Presenting a plain serial port therefore lets
existing ASH implementations work unmodified.

Settings that have no meaning for this transport -- baud rate, parity, stop bits,
modem control lines, and buffer flushing -- are accepted and ignored, or raise
:class:`UnsupportedSetting` where silently ignoring them would hide a real error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import urllib.parse

from aioesphomeapi.client import APIClient
from aioesphomeapi.model import ZigbeeProxyFrame, ZigbeeProxyRequestType
from typing_extensions import Buffer

from serialx import UnsupportedSetting
from serialx.common import ModemPins, SerialPortInfo, register_uri_handler
from serialx.platforms.serial_esphome import (
    ESPHomeSerial,
    ESPHomeSerialTransport,
    translate_esphome_errors,
)

LOGGER = logging.getLogger(__name__)

ZIGBEE_SCHEME = "esphome+zigbee://"

# There is at most one zigbee_proxy per ESPHome device (the component is single
# instance and adds USE_ZIGBEE_PROXY), so there is no instance ID to resolve and
# no port name to match against.
ZIGBEE_PORT_NAME = "Zigbee"


class ESPHomeZigbeeSerial(ESPHomeSerial):
    """Synchronous serial interface over the ESPHome Zigbee proxy API.

    Warning:
        ESPHome does not have a native synchronous API, using this interface is
        heavily discouraged. Please use the async API.

    """

    async def _register_data_handler(self) -> Callable[[], None]:
        """Register `_on_zigbee_frame` on the client's loop and return the unsub."""
        assert self._api is not None
        return self._api.subscribe_zigbee_proxy_frame(self._on_zigbee_frame)

    def _on_zigbee_frame(self, msg: ZigbeeProxyFrame) -> None:
        """Marshal an inbound ASH frame onto the owning loop as stream bytes."""
        client_loop = self._client_loop
        if client_loop is None or client_loop is self._loop:
            self._handle_incoming_data(msg.data)
        else:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._handle_incoming_data, msg.data)

    async def _resolve_instance_id(self) -> None:
        """No-op: zigbee_proxy is single instance and has no instance ID."""

    async def _subscribe_instance(self) -> None:
        """Subscribe to Zigbee proxy frame streaming."""
        if self._api is None or self._instance_subscribed:
            return

        self._schedule_on_client_loop(
            self._api.send_zigbee_proxy_request, ZigbeeProxyRequestType.SUBSCRIBE
        )

        # Ping to ensure the device has processed the subscribe
        await self._ping(timeout=self._connect_timeout)

        self._instance_subscribed = True

    def _unsubscribe_instance(self) -> None:
        """Unsubscribe from Zigbee proxy frame streaming."""
        if self._api is None or not self._instance_subscribed:
            return

        # Unlike serial_proxy, an unsubscribe cannot be scheduled after the API is
        # gone, and a failure here is not actionable: the device drops the
        # subscription when the connection closes regardless.
        self._schedule_on_client_loop(
            self._api.send_zigbee_proxy_request, ZigbeeProxyRequestType.UNSUBSCRIBE
        )
        self._instance_subscribed = False

    @translate_esphome_errors
    async def _async_configure_port(self) -> None:
        """Subscribe only; the ASH link has no configurable UART settings.

        The NCP-side baud rate, parity and stop bits are fixed by the ESPHome
        ``usb_uart`` / ``uart`` component the proxy is bound to, so there is
        nothing to negotiate from this end.
        """
        assert self._api is not None
        await self._subscribe_instance()

    @translate_esphome_errors
    async def _async_flush(self) -> None:
        """No-op: frames are handed to the API as whole messages."""

    def _send_set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Raise; the Zigbee proxy exposes no modem control lines."""
        raise UnsupportedSetting("Zigbee proxy does not support modem control lines")

    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """Raise; the Zigbee proxy exposes no modem control lines."""
        raise UnsupportedSetting("Zigbee proxy does not support modem control lines")

    def _get_modem_pins(self) -> ModemPins:
        """Raise; the Zigbee proxy exposes no modem control lines."""
        raise UnsupportedSetting("Zigbee proxy does not support modem control lines")

    def _write(self, b: Buffer, *, timeout: float | None) -> int:
        """Send bytes to the NCP as a Zigbee proxy frame."""
        assert self._api is not None
        data = bytes(b)
        self._schedule_on_client_loop(self._api.send_zigbee_proxy_frame, data)
        return len(data)

    @translate_esphome_errors
    async def _async_list_serial_ports(self) -> list[SerialPortInfo]:
        """Return a single port if the device advertises a Zigbee proxy."""
        assert self._api is not None

        device_info = await self._call_on_client_loop(self._api.device_info())

        if not device_info.zigbee_proxy_feature_flags:
            return []

        url = urllib.parse.urlunparse(
            urllib.parse.ParseResult(
                scheme="esphome+zigbee",
                netloc=f"{self._api.address}:{self._api.port}",
                path="/",
                params="",
                query="",
                fragment="",
            )
        )

        return [
            SerialPortInfo(
                device=url,
                resolved_device=url,
                vid=None,
                pid=None,
                serial_number=device_info.mac_address,
                manufacturer=device_info.manufacturer,
                product=device_info.model,
                bcd_device=None,
                interface_description=ZIGBEE_PORT_NAME,
                interface_num=None,
            )
        ]


class ESPHomeZigbeeSerialTransport(ESPHomeSerialTransport):
    """Serial transport over the ESPHome Zigbee proxy API."""

    transport_name = "esphome+zigbee"
    _serial_cls: type[ESPHomeZigbeeSerial] = ESPHomeZigbeeSerial
    _serial: ESPHomeZigbeeSerial | None

    async def _register_transport_data_handler(self) -> Callable[[], None]:
        """Register `_on_zigbee_frame` on the client's loop and return the unsub."""
        assert self._serial is not None
        assert self._serial._api is not None

        # This isn't a coroutine but needs to be run in the target loop
        unsub: Callable[[], None] = self._serial._api.subscribe_zigbee_proxy_frame(
            self._on_zigbee_frame
        )
        return unsub

    def _on_zigbee_frame(self, msg: ZigbeeProxyFrame) -> None:
        """Feed an inbound ASH frame to the protocol as stream bytes."""
        assert self._serial is not None

        client_loop = self._serial._client_loop
        if client_loop is None or client_loop is self._loop:
            self._protocol.data_received(msg.data)
        else:
            self._loop.call_soon_threadsafe(self._protocol.data_received, msg.data)


def _serial_for_listing(
    api: APIClient | None, path: str | None
) -> ESPHomeZigbeeSerial:
    return ESPHomeZigbeeSerial(path=path, api=api)


def esphome_zigbee_list_serial_ports(
    api: APIClient | None = None, path: str | None = None
) -> list[SerialPortInfo]:
    """List the Zigbee proxy exposed by an ESPHome device."""
    serial = _serial_for_listing(api, path)
    serial._maybe_start_new_event_loop()
    try:
        serial._call_on_loop(serial._async_open())
        return serial._list_serial_ports()
    finally:
        serial._close()


async def async_esphome_zigbee_list_serial_ports(
    api: APIClient | None = None, path: str | None = None
) -> list[SerialPortInfo]:
    """List the Zigbee proxy exposed by an ESPHome device, async."""
    serial = _serial_for_listing(api, path)
    serial._loop = asyncio.get_running_loop()
    await serial._async_open()
    try:
        return await serial._async_list_serial_ports()
    finally:
        if serial._disconnect_api and serial._api is not None:
            await serial._api.disconnect()
            serial._api = None


register_uri_handler(
    scheme=ZIGBEE_SCHEME,
    unique_scheme=ZIGBEE_SCHEME,
    sync_cls=ESPHomeZigbeeSerial,
    async_transport_cls=ESPHomeZigbeeSerialTransport,
    list_serial_ports_func=esphome_zigbee_list_serial_ports,
    async_list_serial_ports_func=async_esphome_zigbee_list_serial_ports,
)
