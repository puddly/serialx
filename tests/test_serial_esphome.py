"""ESPHome serial port tests."""

import pytest

try:
    from aioesphomeapi.client import MIN_VERSION_PROXY_ACK, APIClient
except ImportError:
    pytest.skip(
        "aioesphomeapi is required to run esphome transport tests",
        allow_module_level=True,
    )

import asyncio
from base64 import b64encode
from collections.abc import AsyncIterator, Iterator
import contextlib
import threading
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch
import urllib.parse
import warnings

from aioesphomeapi.model import (
    DeviceInfo,
    SerialProxyMode,
    SerialProxyParity,
    SerialProxyRequestResponse,
    SerialProxyStatus,
    SerialProxyUsbInfo,
)

from serialx import (
    AsyncSerial,
    Platform,
    SerialException,
    SerialPortInfo,
    async_list_serial_ports,
    async_serial_for_url,
    list_serial_ports,
)
from serialx.platforms.serial_esphome import (
    ESPHOME_DEFAULT_PORT,
    ESPHomeSerial,
    ESPHomeSerialTransport,
    InvalidSettingsError,
)

from .common import ESPHOME_HOST_BINARY, create_esphome_pair, create_socat_pair


@contextlib.contextmanager
def api_client_on_thread_loop(
    url: str,
) -> Iterator[tuple[APIClient, asyncio.AbstractEventLoop]]:
    """Yield an APIClient connected on a dedicated background thread's loop."""
    parsed = urllib.parse.urlparse(url)
    assert parsed.hostname is not None
    hostname = parsed.hostname
    port = parsed.port or ESPHOME_DEFAULT_PORT

    thread_loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run_loop() -> None:
        asyncio.set_event_loop(thread_loop)
        ready.set()
        thread_loop.run_forever()

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
    ready.wait()

    async def _connect() -> APIClient:
        api = APIClient(
            address=hostname,
            port=port,
            password=None,
        )
        await api.connect(login=True)
        return api

    api = asyncio.run_coroutine_threadsafe(_connect(), thread_loop).result()

    try:
        yield api, thread_loop
    finally:
        asyncio.run_coroutine_threadsafe(api.disconnect(), thread_loop).result()
        thread_loop.call_soon_threadsafe(thread_loop.stop)
        thread.join(timeout=5)
        thread_loop.close()


@contextlib.asynccontextmanager
async def cross_loop_async_serial(url: str) -> AsyncIterator[AsyncSerial]:
    """Yield an AsyncSerial whose `APIClient` lives on its own thread loop."""
    port_name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["port_name"][0]

    with api_client_on_thread_loop(url) as (api, _thread_loop):
        async with async_serial_for_url(
            url=None,
            transport_cls=ESPHomeSerialTransport,
            api=api,
            port_name=port_name,
            baudrate=115200,
        ) as serial:
            yield serial


def base64(key: bytes) -> str:
    """Base64 encode a Noise key."""
    assert len(key) == 32

    return b64encode(key).decode("ascii")


# Serial proxy calls whose ordering the transport is expected to guarantee
PROXY_CALL_NAMES = (
    "subscribe_serial_proxy_data",
    "serial_proxy_configure_await_response",
    "serial_proxy_set_mode_await_response",
    "serial_proxy_subscribe_await_response",
)


def mock_api_client(*port_names: str) -> MagicMock:
    """Create a mock `APIClient` recording the serial proxy calls made on it."""
    device_info = DeviceInfo.from_dict(
        {
            "serial_proxies": [{"name": name} for name in port_names],
            "mac_address": "98:35:69:AB:F6:79",
            "manufacturer": "Host",
            "model": "host",
        }
    )

    api = MagicMock()
    api.loop = asyncio.get_running_loop()
    api.address = "127.0.0.1"
    api.port = ESPHOME_DEFAULT_PORT
    api.attach_mock(AsyncMock(), "connect")
    api.attach_mock(AsyncMock(), "disconnect")
    api.attach_mock(AsyncMock(return_value=device_info), "device_info")
    api.attach_mock(
        AsyncMock(return_value=None), "serial_proxy_configure_await_response"
    )
    api.attach_mock(
        AsyncMock(return_value=None), "serial_proxy_subscribe_await_response"
    )
    api.attach_mock(
        AsyncMock(return_value=SerialProxyRequestResponse(status=SerialProxyStatus.OK)),
        "serial_proxy_set_mode_await_response",
    )
    # Recent enough that the validated helper does not fall back to a flushing ping
    api.api_version = MIN_VERSION_PROXY_ACK
    api._get_connection.return_value.send_messages_await_response_complex = AsyncMock()

    return api


def proxy_calls(api: MagicMock) -> list[object]:
    """Return only the ordering-relevant serial proxy calls made on `api`."""
    return [c for c in api.mock_calls if c[0] in PROXY_CALL_NAMES]


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_externally_passed_api() -> None:
    """Test passing an ESPHome API instance externally."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            # Connect to the ESPHome API externally
            parsed = urllib.parse.urlparse(left)
            assert parsed.hostname is not None

            api = APIClient(
                address=parsed.hostname,
                port=parsed.port or ESPHOME_DEFAULT_PORT,
                password=None,
            )
            await api.connect(login=True)

            try:
                for _attempt in range(10):
                    async with async_serial_for_url(
                        url=None,
                        transport_cls=ESPHomeSerialTransport,
                        api=api,
                        port_name="Serial Proxy Left",
                        baudrate=115200,
                    ) as serial:
                        serial.write_nowait(b"test")
                        await serial.drain()

                # The API is still connected
                await api.device_info()
            finally:
                await api.disconnect()


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_externally_passed_api_close_after_disconnect() -> None:
    """Test closing the transport after the API has been disconnected."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)
            assert parsed.hostname is not None

            api = APIClient(
                address=parsed.hostname,
                port=parsed.port or ESPHOME_DEFAULT_PORT,
                password=None,
            )
            await api.connect(login=True)

            serial = async_serial_for_url(
                url=None,
                transport_cls=ESPHomeSerialTransport,
                api=api,
                port_name="Serial Proxy Left",
                baudrate=115200,
            )
            await serial.open()

            # Disconnect the API before closing the transport
            await api.disconnect()

            await serial.close()


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_connect_by_instance_id() -> None:
    """Test connecting to an ESPHome serial proxy by instance ID."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)

            # Connect by instance ID instead of name, with a password
            url = f"esphome://{parsed.hostname}:{parsed.port}/0?password=unused"

            async with async_serial_for_url(url=url, baudrate=115200) as serial:
                serial.write_nowait(b"test")
                await serial.drain()


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_connect_by_invalid_name() -> None:
    """Test that connecting with an invalid port name raises ValueError."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)
            url = f"esphome://{parsed.hostname}:{parsed.port}?port_name=Nonexistent"

            with pytest.raises(ValueError, match="does not exist"):
                async with async_serial_for_url(url=url, baudrate=115200):
                    pass


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_connect_plaintext_to_encrypted_server() -> None:
    """Test that connecting without encryption to an encrypted server raises."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(
            socat_left,
            socat_right,
            noise_psk=base64(b"A noise PSK we do not provide..."),
        ) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)
            url = (
                f"esphome://{parsed.hostname}:{parsed.port}?port_name=Serial+Proxy+Left"
            )

            with pytest.raises(SerialException, match="Connection requires encryption"):
                async with async_serial_for_url(url=url, baudrate=115200):
                    pass


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_connect_with_invalid_key() -> None:
    """Test that connecting with the wrong encryption key raises."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(
            socat_left,
            socat_right,
            noise_psk=base64(b"The real noise PSK of the device"),
        ) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)
            wrong_key = base64(b"A different, incorrect noise PSK")

            url = (
                f"esphome://{parsed.hostname}:{parsed.port}"
                f"?port_name=Serial+Proxy+Left"
                f"&key={wrong_key}"
            )

            with pytest.raises(SerialException, match="Invalid encryption key"):
                async with async_serial_for_url(url=url, baudrate=115200):
                    pass


async def test_connect_timeout_raises_timeout_error() -> None:
    """Test that a TCP connect timeout is translated to TimeoutError."""

    with patch("aioesphomeapi.connection.TCP_CONNECT_TIMEOUT", 1.0):
        with pytest.raises(TimeoutError, match="Timeout while connecting"):
            # 192.0.2.1 is TEST-NET-1 (RFC 5737), packets are silently dropped
            async with async_serial_for_url(
                url="esphome://192.0.2.1:6053?port_name=test", baudrate=115200
            ):
                pass


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_noise_psk_key_alias() -> None:
    """Test that connecting without encryption to an encrypted server raises."""
    key = base64(b"A noise PSK 32 bytes in length..")

    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(
            socat_left,
            socat_right,
            noise_psk=key,
        ) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)
            with pytest.raises(
                ValueError, match="Both `key` and `noise_psk` cannot be provided"
            ):
                async with async_serial_for_url(
                    url=f"esphome://{parsed.hostname}:{parsed.port}",
                    port_name="Serial Proxy Left",
                    noise_psk=key,
                    key=key,
                    baudrate=115200,
                ):
                    pass

            with pytest.raises(
                ValueError, match="Both `key` and `noise_psk` cannot be provided"
            ):
                async with async_serial_for_url(
                    url=f"esphome://{parsed.hostname}:{parsed.port}?key={key}&noise_psk={key}",
                    port_name="Serial Proxy Left",
                    baudrate=115200,
                ):
                    pass

            async with async_serial_for_url(
                url=f"esphome://{parsed.hostname}:{parsed.port}",
                port_name="Serial Proxy Left",
                noise_psk=key,  # alias
                baudrate=115200,
            ):
                pass


def _expected_esphome_ports(netloc: str) -> list[SerialPortInfo]:
    return [
        SerialPortInfo(
            device=f"esphome://{netloc}/?port_name=Serial+Proxy+Left",
            resolved_device=f"esphome://{netloc}/?port_name=Serial+Proxy+Left",
            vid=None,
            pid=None,
            serial_number="98:35:69:AB:F6:79",
            manufacturer="Host",
            product="host",
            bcd_device=None,
            interface_description="Serial Proxy Left",
            interface_num=None,
        ),
        SerialPortInfo(
            device=f"esphome://{netloc}/?port_name=Serial+Proxy+Right",
            resolved_device=f"esphome://{netloc}/?port_name=Serial+Proxy+Right",
            vid=None,
            pid=None,
            serial_number="98:35:69:AB:F6:79",
            manufacturer="Host",
            product="host",
            bcd_device=None,
            interface_description="Serial Proxy Right",
            interface_num=None,
        ),
    ]


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_esphome_list_serial_ports() -> None:
    """Test listing ESPHome serial ports asynchronously via an externally-passed API."""
    assert list_serial_ports(Platform.ESPHOME) == []
    assert await async_list_serial_ports(Platform.ESPHOME) == []

    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)
            assert parsed.hostname is not None

            api = APIClient(
                address=parsed.hostname,
                port=parsed.port or ESPHOME_DEFAULT_PORT,
                password=None,
            )
            await api.connect(login=True)

            serial_ports = await async_list_serial_ports(Platform.ESPHOME, api=api)
            assert serial_ports == _expected_esphome_ports(parsed.netloc)


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_async_esphome_list_serial_ports_via_uri() -> None:
    """Test listing ESPHome serial ports asynchronously via a URI."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)

            serial_ports = await async_list_serial_ports(Platform.ESPHOME, path=left)
            assert serial_ports == _expected_esphome_ports(parsed.netloc)


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
def test_sync_esphome_list_serial_ports_via_uri() -> None:
    """Test listing ESPHome serial ports synchronously via a URI."""
    assert list_serial_ports(Platform.ESPHOME) == []

    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)

            serial_ports = list_serial_ports(Platform.ESPHOME, path=left)
            assert serial_ports == _expected_esphome_ports(parsed.netloc)


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_sync_esphome_list_serial_ports_external_api() -> None:
    """Test sync listing of ESPHome serial ports with an externally-passed API."""
    test_loop = asyncio.get_running_loop()

    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)
            assert parsed.hostname is not None

            api = APIClient(
                address=parsed.hostname,
                port=parsed.port or ESPHOME_DEFAULT_PORT,
                password=None,
            )
            await api.connect(login=True)

            serial_ports = await asyncio.to_thread(
                list_serial_ports, Platform.ESPHOME, api=api, loop=test_loop
            )
            assert serial_ports == _expected_esphome_ports(parsed.netloc)

            # The externally-passed API must remain connected.
            await api.device_info()
            await api.disconnect()


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_cross_loop_async_api() -> None:
    """Async API works with the `APIClient` on a separate loop."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, right, _, _):
            async with async_serial_for_url(url=right, baudrate=115200) as ser_right:
                async with cross_loop_async_serial(left) as ser_left:
                    serial = ser_left.transport.get_extra_info("serial")
                    assert isinstance(serial, ESPHomeSerial)
                    assert serial._client_loop is not asyncio.get_running_loop()
                    assert serial._loop is asyncio.get_running_loop()

                    ser_left.write_nowait(b"left to right")
                    data = await ser_right.readexactly(len(b"left to right"))
                    assert data == b"left to right"

                    ser_right.write_nowait(b"right to left")
                    data = await ser_left.readexactly(len(b"right to left"))
                    assert data == b"right to left"

                    await ser_left.set_modem_pins(dtr=True, rts=False)
                    await ser_left.get_modem_pins()
                    await ser_left.transport.flush()


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_cross_loop_sync_modem_pins_on_loop_thread() -> None:
    """Sync modem-pin access from `self._loop`'s thread must not deadlock."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            async with cross_loop_async_serial(left) as serial:
                esphome_serial = serial.transport.serial

                with warnings.catch_warnings():
                    warnings.simplefilter("always", DeprecationWarning)
                    with pytest.warns(
                        DeprecationWarning, match="transport.set_modem_pins"
                    ):
                        esphome_serial.dtr = False

                with pytest.raises(RuntimeError, match="sync ESPHomeSerial method"):
                    _ = esphome_serial.dtr


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_sync_api_with_external_api_on_different_loop() -> None:
    """Sync API works when the `APIClient` lives on a different loop."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, right, _, _):
            async with async_serial_for_url(url=right, baudrate=115200) as peer:
                with api_client_on_thread_loop(left) as (api, api_loop):

                    def _sync_open() -> ESPHomeSerial:
                        serial = ESPHomeSerial(
                            api=api,
                            port_name="Serial Proxy Left",
                            baudrate=115200,
                        )
                        serial.open()
                        return serial

                    serial = await asyncio.to_thread(_sync_open)

                    try:
                        # API on thread 1, sync-dispatch loop on thread 2
                        assert serial._client_loop is api_loop
                        assert serial._loop is not None
                        assert serial._loop is not api_loop

                        peer.write_nowait(b"peer-data")
                        await peer.drain()

                        data = await asyncio.to_thread(serial.read, len(b"peer-data"))
                        assert data == b"peer-data"
                    finally:
                        await asyncio.to_thread(serial.close)


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_single_api_multiple_async_ports() -> None:
    """Two async transports sharing one APIClient operate independently."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            parsed = urllib.parse.urlparse(left)
            assert parsed.hostname is not None

            api = APIClient(
                address=parsed.hostname,
                port=parsed.port or ESPHOME_DEFAULT_PORT,
                password=None,
            )
            await api.connect(login=True)

            try:
                async with (
                    async_serial_for_url(
                        url=None,
                        transport_cls=ESPHomeSerialTransport,
                        api=api,
                        port_name="Serial Proxy Left",
                        baudrate=115200,
                    ) as ser_left,
                    async_serial_for_url(
                        url=None,
                        transport_cls=ESPHomeSerialTransport,
                        api=api,
                        port_name="Serial Proxy Right",
                        baudrate=115200,
                    ) as ser_right,
                ):
                    ser_left.write_nowait(b"left to right")
                    await ser_left.drain()
                    data = await asyncio.wait_for(
                        ser_right.readexactly(len(b"left to right")), timeout=5
                    )
                    assert data == b"left to right"

                    ser_right.write_nowait(b"right to left")
                    await ser_right.drain()
                    data = await asyncio.wait_for(
                        ser_left.readexactly(len(b"right to left")), timeout=5
                    )
                    assert data == b"right to left"

                # The shared externally-owned API is still connected
                await api.device_info()
            finally:
                await api.disconnect()


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_single_api_multiple_sync_ports() -> None:
    """Two sync ESPHomeSerials sharing one APIClient operate independently."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, _):
            with api_client_on_thread_loop(left) as (api, _api_loop):
                with (
                    ESPHomeSerial(
                        api=api, port_name="Serial Proxy Left", baudrate=115200
                    ) as serial_left,
                    ESPHomeSerial(
                        api=api, port_name="Serial Proxy Right", baudrate=115200
                    ) as serial_right,
                ):
                    serial_left.write(b"left to right")
                    assert serial_right.read(len(b"left to right")) == b"left to right"

                    serial_right.write(b"right to left")
                    assert serial_left.read(len(b"right to left")) == b"right to left"


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_externally_passed_api_disconnect_breaks_transport() -> None:
    """Losing an externally-owned API connection breaks the transport."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, unplug):
            assert unplug is not None
            parsed = urllib.parse.urlparse(left)
            assert parsed.hostname is not None

            api = APIClient(
                address=parsed.hostname,
                port=parsed.port or ESPHOME_DEFAULT_PORT,
                password=None,
            )
            await api.connect(login=True)

            async with async_serial_for_url(
                url=None,
                transport_cls=ESPHomeSerialTransport,
                api=api,
                port_name="Serial Proxy Left",
                baudrate=115200,
            ) as serial:
                serial.write_nowait(b"test")
                await serial.drain()

                unplug()

                with pytest.raises(OSError):
                    await serial.read(1)

                with pytest.raises(OSError):
                    serial.write_nowait(b"x")

                assert serial.transport.is_closing()
                assert not api.is_connected


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_cross_loop_api_disconnect_breaks_transport() -> None:
    """A connection lost on another loop is marshalled to the transport's loop."""
    with create_socat_pair() as (socat_left, socat_right, _, _):
        with create_esphome_pair(socat_left, socat_right) as (left, _right, _, unplug):
            assert unplug is not None

            with api_client_on_thread_loop(left) as (api, api_loop):
                async with async_serial_for_url(
                    url=None,
                    transport_cls=ESPHomeSerialTransport,
                    api=api,
                    port_name="Serial Proxy Left",
                    baudrate=115200,
                ) as serial:
                    assert api_loop is not asyncio.get_running_loop()
                    serial.write_nowait(b"test")
                    await serial.drain()

                    unplug()

                    with pytest.raises(OSError):
                        await serial.read(1)

                    assert serial.transport.is_closing()


async def test_mode_protocol_set_before_subscribe() -> None:
    """`mode=protocol` sets the proxy mode after resolving, before subscribing."""
    api = mock_api_client("Serial Proxy Left", "Zigbee")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&mode=protocol"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200):
            pass

    assert proxy_calls(api) == [
        # The data handler is installed before anything can stream
        call.subscribe_serial_proxy_data(ANY),
        call.serial_proxy_subscribe_await_response(1, timeout=ANY),
        call.serial_proxy_set_mode_await_response(
            instance=1, mode=SerialProxyMode.PROTOCOL, timeout=ANY
        ),
        call.serial_proxy_configure_await_response(
            instance=1,
            baudrate=115200,
            flow_control=False,
            parity=SerialProxyParity.NONE,
            stop_bits=1,
            data_size=8,
        ),
    ]


async def test_mode_protocol_kwarg_with_external_api() -> None:
    """The `mode` kwarg is honored when the API client is passed in externally."""
    api = mock_api_client("Zigbee")

    async with async_serial_for_url(
        url=None,
        transport_cls=ESPHomeSerialTransport,
        api=api,
        port_name="Zigbee",
        mode="protocol",
        baudrate=115200,
    ):
        pass

    assert proxy_calls(api) == [
        call.subscribe_serial_proxy_data(ANY),
        call.serial_proxy_subscribe_await_response(0, timeout=ANY),
        call.serial_proxy_set_mode_await_response(
            instance=0, mode=SerialProxyMode.PROTOCOL, timeout=ANY
        ),
        call.serial_proxy_configure_await_response(
            instance=0,
            baudrate=115200,
            flow_control=False,
            parity=SerialProxyParity.NONE,
            stop_bits=1,
            data_size=8,
        ),
    ]


@pytest.mark.parametrize("query", ["", "&mode=raw"])
async def test_mode_raw_is_sent_explicitly(query: str) -> None:
    """`raw` is sent too, so a client can turn a tap off that was left on."""
    api = mock_api_client("Zigbee")
    url = f"esphome://127.0.0.1:6053/?port_name=Zigbee{query}"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200):
            pass

    assert proxy_calls(api) == [
        call.subscribe_serial_proxy_data(ANY),
        call.serial_proxy_subscribe_await_response(0, timeout=ANY),
        call.serial_proxy_set_mode_await_response(
            instance=0, mode=SerialProxyMode.RAW, timeout=ANY
        ),
        call.serial_proxy_configure_await_response(
            instance=0,
            baudrate=115200,
            flow_control=False,
            parity=SerialProxyParity.NONE,
            stop_bits=1,
            data_size=8,
        ),
    ]


async def test_invalid_mode_in_url() -> None:
    """An unknown `mode` in the URL is rejected before any connection is made."""
    api = mock_api_client("Zigbee")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&mode=ezsp"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(InvalidSettingsError, match="Invalid serial proxy mode"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass

    assert len(api.mock_calls) == 0


def test_invalid_mode_kwarg() -> None:
    """An unknown `mode` kwarg is rejected immediately."""
    with pytest.raises(InvalidSettingsError, match="Invalid serial proxy mode"):
        ESPHomeSerial(port_name="Zigbee", mode="zigbee", baudrate=115200)


def _usb_info(**overrides: object) -> SerialProxyUsbInfo:
    fields = {
        "instance": 0,
        "status": SerialProxyStatus.OK,
        "connected": True,
        "vendor_id": 0x303A,
        "product_id": 0x4001,
        "bcd_device": 0x0101,
        "interface_number": 0,
        "manufacturer": "Nabu Casa",
        "product": "ZBT-2",
        "serial_number": "AABBCCDDEEFF",
    }
    fields.update(overrides)
    return SerialProxyUsbInfo(**fields)


async def test_usb_serial_number_match_allows_connection() -> None:
    """A port whose USB device reports the expected serial number is used."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_usb_info()), "serial_proxy_get_usb_info")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&usb_serial=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200):
            pass

    assert api.serial_proxy_get_usb_info.mock_calls == [call(0)]


async def test_usb_serial_number_mismatch_is_rejected() -> None:
    """A different stick in the socket fails before the port is claimed."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(return_value=_usb_info(serial_number="112233445566")),
        "serial_proxy_get_usb_info",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&usb_serial=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="112233445566"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass

    assert proxy_calls(api) == [call.subscribe_serial_proxy_data(ANY)]


async def test_usb_serial_number_with_nothing_attached_is_rejected() -> None:
    """An empty socket is a mismatch, not a silent success."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(return_value=_usb_info(connected=False, serial_number="")),
        "serial_proxy_get_usb_info",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&usb_serial=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="No USB device is attached"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_usb_serial_number_on_non_usb_port_is_rejected() -> None:
    """NOT_SUPPORTED means the port cannot answer for its identity."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(return_value=_usb_info(status=SerialProxyStatus.NOT_SUPPORTED)),
        "serial_proxy_get_usb_info",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&usb_serial=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="not supported"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_no_usb_serial_number_skips_the_check() -> None:
    """Without the setting nothing asks the device for its USB identity."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(), "serial_proxy_get_usb_info")

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(
            url="esphome://127.0.0.1:6053/?port_name=Zigbee", baudrate=115200
        ):
            pass

    assert len(api.serial_proxy_get_usb_info.mock_calls) == 0
