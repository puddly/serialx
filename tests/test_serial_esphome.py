"""ESPHome serial port tests."""

import pytest

try:
    from aioesphomeapi.client import APIClient
except ImportError:
    pytest.skip(
        "aioesphomeapi is required to run esphome transport tests",
        allow_module_level=True,
    )

import asyncio
from base64 import b64encode
from collections.abc import AsyncIterator, Callable, Iterator
import contextlib
import errno
import threading
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch
import urllib.parse
import warnings

from aioesphomeapi.model import (
    APIVersion,
    DeviceInfo,
    SerialProxyIdentity,
    SerialProxyIdentityFlag,
    SerialProxyIdentitySource,
    SerialProxyMode,
    SerialProxyParity,
    SerialProxyRequestResponse,
    SerialProxyStatus,
    UsbDeviceDescriptor,
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
    MIN_VERSION_SERIAL_PROXY_SET_MODE,
    ESPHomeSerial,
    ESPHomeSerialTransport,
    InvalidSettingsError,
    SerialProxyModeName,
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
        AsyncMock(return_value=SerialProxyRequestResponse(status=SerialProxyStatus.OK)),  # type:ignore[call-arg]
        "serial_proxy_set_mode_await_response",
    )
    # Recent enough to answer set-mode, and for the validated helper to skip the flushing ping
    api.api_version = MIN_VERSION_SERIAL_PROXY_SET_MODE
    api._get_connection.return_value.send_messages_await_response_complex = AsyncMock()

    return api


def proxy_calls(api: MagicMock) -> list[object]:
    """Return only the ordering-relevant serial proxy calls made on `api`."""
    return [c for c in api.mock_calls if c[0] in PROXY_CALL_NAMES]


@pytest.mark.skipif(not ESPHOME_HOST_BINARY, reason="esphome host binary not available")
async def test_host_daemon_api_version(esphome_api_version: tuple[int, int]) -> None:
    """The daemon under test speaks the API version CI says it should."""
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
                assert api.api_version == APIVersion(*esphome_api_version)
            finally:
                await api.disconnect()


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
            serial_number=None,
            manufacturer=None,
            product=None,
            bcd_device=None,
            interface_description="Serial Proxy Left",
            interface_num=None,
        ),
        SerialPortInfo(
            device=f"esphome://{netloc}/?port_name=Serial+Proxy+Right",
            resolved_device=f"esphome://{netloc}/?port_name=Serial+Proxy+Right",
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
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


@pytest.mark.parametrize("query", ["", "&mode=protocol"])
async def test_mode_skipped_on_old_device(query: str) -> None:
    """A device predating API 1.17 never answers set-mode, so it is not sent."""
    api = mock_api_client("Zigbee")
    api.api_version = APIVersion(1, 16)
    url = f"esphome://127.0.0.1:6053/?port_name=Zigbee{query}"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200) as serial:
            assert isinstance(serial.transport.serial, ESPHomeSerial)
            assert serial.transport.serial.tap_mode is SerialProxyModeName.RAW

    assert proxy_calls(api) == [
        call.subscribe_serial_proxy_data(ANY),
        call.serial_proxy_subscribe_await_response(0, timeout=ANY),
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


def _identity(**overrides: object) -> SerialProxyIdentity:
    fields = {
        "instance": 0,
        "source": SerialProxyIdentitySource.USB,
        "flags": SerialProxyIdentityFlag.CONNECTED,
        "manufacturer": "Nabu Casa",
        "product": "ZBT-2",
        "serial_number": "AABBCCDDEEFF",
        "usb": UsbDeviceDescriptor(  # type:ignore[call-arg]
            vendor_id=0x303A, product_id=0x4001, bcd_device=0x0101, interface_number=0
        ),
    }
    fields.update(overrides)
    return SerialProxyIdentity(**fields)


async def test_serial_number_match_allows_connection() -> None:
    """A port whose device reports the expected serial number is used."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200):
            pass

    assert api.serial_proxy_get_identity.mock_calls == [call(0)]

    # Identity changes are only sent to a subscriber, so the subscription has to be in
    # place before the check, or a device pulled in between goes unnoticed
    names = [c[0] for c in api.mock_calls]
    assert names.index("subscribe_serial_proxy_identity") < names.index(
        "serial_proxy_get_identity"
    )


async def test_serial_number_mismatch_is_rejected() -> None:
    """A different stick in the socket fails before the port is claimed."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(return_value=_identity(serial_number="112233445566")),
        "serial_proxy_get_identity",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="112233445566"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass

    assert proxy_calls(api) == [call.subscribe_serial_proxy_data(ANY)]


async def test_serial_number_with_nothing_attached_is_rejected() -> None:
    """An empty socket is a mismatch, not a silent success."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(return_value=_identity(flags=0, serial_number="")),
        "serial_proxy_get_identity",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="No device is attached"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_serial_number_on_port_without_identity_is_rejected() -> None:
    """A port that carries no identity cannot answer for its serial number."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(
            return_value=_identity(
                source=SerialProxyIdentitySource.NONE,
                flags=0,
                manufacturer="",
                product="",
                serial_number="",
                usb=UsbDeviceDescriptor(),
            )
        ),
        "serial_proxy_get_identity",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="no identity"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_serial_number_with_unreadable_identity_is_rejected() -> None:
    """A device whose descriptors could not be read is not taken on faith."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(
            return_value=_identity(
                flags=SerialProxyIdentityFlag.CONNECTED | SerialProxyIdentityFlag.ERROR,
                serial_number="",
            )
        ),
        "serial_proxy_get_identity",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="Cannot read the identity"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_configured_identity_satisfies_serial_number() -> None:
    """An identity stated in the device's configuration is as good as a USB one."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(
            return_value=_identity(
                source=SerialProxyIdentitySource.CONFIGURED,
                usb=UsbDeviceDescriptor(),
            )
        ),
        "serial_proxy_get_identity",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200):
            pass


async def test_all_port_matchers_allow_connection() -> None:
    """Every matcher is checked, with integers parsed from the URL as Python literals."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")
    query = urllib.parse.urlencode(
        {
            "port_name": "Zigbee",
            "port_manufacturer": "Nabu Casa",
            "port_product": "ZBT-2",
            "port_serial_number": "AABBCCDDEEFF",
            "port_usb_vid": "0x303A",
            "port_usb_pid": "0x4001",
            "port_usb_bcd_device": "0x0101",
            "port_usb_interface_num": "0",
            "port_udev_id": "usb-Nabu_Casa_ZBT-2_AABBCCDDEEFF-if00",
        }
    )

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(
            url=f"esphome://127.0.0.1:6053/?{query}", baudrate=115200
        ):
            pass

    assert api.serial_proxy_get_identity.mock_calls == [call(0)]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("port_manufacturer", "Espressif"),
        ("port_product", "ZBT-1"),
        ("port_serial_number", "112233445566"),
        ("port_usb_vid", 0x10C4),
        ("port_usb_pid", 0xEA60),
        ("port_usb_bcd_device", 0x0100),
        ("port_usb_interface_num", 1),
        ("port_udev_id", "usb-Nabu_Casa_ZBT-2_AABBCCDDEEFF-if00-port0"),
    ],
)
async def test_port_matcher_mismatch_is_rejected(name: str, value: str | int) -> None:
    """Any single matcher failing refuses the port, whatever the others say."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")

    with pytest.raises(SerialException, match=f"{name}="):
        async with async_serial_for_url(
            url=None,
            transport_cls=ESPHomeSerialTransport,
            api=api,
            port_name="Zigbee",
            baudrate=115200,
            **{"port_serial_number": "AABBCCDDEEFF", name: value},  # type: ignore[arg-type]
        ):
            pass

    assert proxy_calls(api) == [call.subscribe_serial_proxy_data(ANY)]


@pytest.mark.parametrize(
    "usb",
    [
        UsbDeviceDescriptor(),
        UsbDeviceDescriptor(vendor_id=0x303A),  # type:ignore[call-arg]
        UsbDeviceDescriptor(product_id=0x4001),  # type:ignore[call-arg]
    ],
)
async def test_port_usb_matcher_rejects_missing_usb_ids(
    usb: UsbDeviceDescriptor,
) -> None:
    """A zero ID means no USB metadata, which `port_usb_*` must not match."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(return_value=_identity(usb=usb)), "serial_proxy_get_identity"
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_usb_interface_num=0"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="no USB descriptor"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_port_usb_matcher_accepts_configured_identity() -> None:
    """A port that is not USB can still carry USB metadata to match against."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(return_value=_identity(source=SerialProxyIdentitySource.CONFIGURED)),
        "serial_proxy_get_identity",
    )
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_usb_vid=0x303A"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200):
            pass


async def test_list_serial_ports_reports_identity() -> None:
    """Each port lists what its identity says is attached, and nothing it cannot know."""
    api = mock_api_client(
        "USB", "Configured", "Empty", "Unreadable", "None", "Mimic", "Partial"
    )
    api.api_version = APIVersion(1, 18)
    identities = [
        _identity(instance=0),
        _identity(
            instance=1,
            source=SerialProxyIdentitySource.CONFIGURED,
            serial_number="",
            usb=UsbDeviceDescriptor(),
        ),
        _identity(instance=2, flags=0),
        _identity(
            instance=3,
            flags=SerialProxyIdentityFlag.CONNECTED | SerialProxyIdentityFlag.ERROR,
        ),
        _identity(instance=4, source=SerialProxyIdentitySource.NONE),
        _identity(instance=5, source=SerialProxyIdentitySource.CONFIGURED),
        _identity(
            instance=6,
            serial_number="",
            usb=UsbDeviceDescriptor(vendor_id=0x303A),  # type:ignore[call-arg]
        ),
    ]
    api.attach_mock(
        AsyncMock(side_effect=lambda instance, timeout: identities[instance]),
        "serial_proxy_get_identity",
    )

    ports = await async_list_serial_ports(Platform.ESPHOME, api=api)

    base = "esphome://127.0.0.1:6053/?port_name="
    # A device with a serial number is found by it, not by the port it is on
    zbt2_url = (
        "esphome://127.0.0.1:6053/?port_serial_number=AABBCCDDEEFF"
        "&port_udev_id=usb-Nabu_Casa_ZBT-2_AABBCCDDEEFF-if00"
    )
    empty = {
        "vid": None,
        "pid": None,
        "serial_number": None,
        "manufacturer": None,
        "product": None,
        "bcd_device": None,
        "interface_num": None,
    }
    assert ports == [
        SerialPortInfo(
            device=zbt2_url,
            resolved_device=zbt2_url,
            vid=0x303A,
            pid=0x4001,
            serial_number="AABBCCDDEEFF",
            manufacturer="Nabu Casa",
            product="ZBT-2",
            bcd_device=0x0101,
            interface_description="USB",
            interface_num=0,
        ),
        SerialPortInfo(
            device=f"{base}Configured",
            resolved_device=f"{base}Configured",
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer="Nabu Casa",
            product="ZBT-2",
            bcd_device=None,
            interface_description="Configured",
            interface_num=None,
        ),
        *(
            SerialPortInfo(
                device=f"{base}{name}",
                resolved_device=f"{base}{name}",
                interface_description=name,
                **empty,
            )
            for name in ("Empty", "Unreadable", "None")
        ),
        SerialPortInfo(
            device=zbt2_url,
            resolved_device=zbt2_url,
            vid=0x303A,
            pid=0x4001,
            serial_number="AABBCCDDEEFF",
            manufacturer="Nabu Casa",
            product="ZBT-2",
            bcd_device=0x0101,
            interface_description="Mimic",
            interface_num=0,
        ),
        SerialPortInfo(
            device=f"{base}Partial",
            resolved_device=f"{base}Partial",
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer="Nabu Casa",
            product="ZBT-2",
            bcd_device=None,
            interface_description="Partial",
            interface_num=None,
        ),
    ]
    assert api.serial_proxy_get_identity.mock_calls == [
        call(instance, timeout=10.0) for instance in range(7)
    ]


async def test_udev_id_match_allows_connection() -> None:
    """The by-id stem is computed from the identity and compared as a whole."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")
    url = (
        "esphome://127.0.0.1:6053/?port_name=Zigbee"
        "&port_udev_id=usb-Nabu_Casa_ZBT-2_AABBCCDDEEFF-if00"
    )

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200):
            pass

    assert api.serial_proxy_get_identity.mock_calls == [call(0)]


async def test_port_found_by_matchers() -> None:
    """Without a port name, the one port whose device matches every matcher is used."""
    api = mock_api_client("Empty", "Other", "Zigbee")
    api.api_version = APIVersion(1, 18)
    identities = [
        _identity(instance=0, flags=0),
        _identity(instance=1, serial_number="112233445566"),
        _identity(instance=2),
    ]
    api.attach_mock(
        AsyncMock(side_effect=lambda instance, timeout: identities[instance]),
        "serial_proxy_get_identity",
    )
    url = (
        "esphome://127.0.0.1:6053/?port_serial_number=AABBCCDDEEFF"
        "&port_udev_id=usb-Nabu_Casa_ZBT-2_AABBCCDDEEFF-if00"
    )

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200):
            pass

    assert api.serial_proxy_get_identity.mock_calls == [
        call(instance, timeout=10.0) for instance in range(3)
    ]
    assert call.serial_proxy_subscribe_await_response(2, timeout=ANY) in proxy_calls(
        api
    )


@pytest.mark.parametrize(
    ("serial_numbers", "match"),
    [
        (["112233445566", "112233445566"], "No serial proxy has a device attached"),
        (["AABBCCDDEEFF", "AABBCCDDEEFF"], r"Serial proxies \['A', 'B'\] all have"),
    ],
)
async def test_port_found_by_matchers_needs_one_match(
    serial_numbers: list[str], match: str
) -> None:
    """Finding a port by its device fails unless exactly one port matches."""
    api = mock_api_client("A", "B")
    api.api_version = APIVersion(1, 18)
    identities = [
        _identity(instance=instance, serial_number=serial_number)
        for instance, serial_number in enumerate(serial_numbers)
    ]
    api.attach_mock(
        AsyncMock(side_effect=lambda instance, timeout: identities[instance]),
        "serial_proxy_get_identity",
    )
    url = "esphome://127.0.0.1:6053/?port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match=match):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_port_found_by_matchers_needs_identity() -> None:
    """A device below API 1.18 cannot say what is behind its ports."""
    api = mock_api_client("Zigbee")
    api.api_version = APIVersion(1, 17)
    api.attach_mock(AsyncMock(), "serial_proxy_get_identity")
    url = "esphome://127.0.0.1:6053/?port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="API 1.18"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass

    assert len(api.serial_proxy_get_identity.mock_calls) == 0


async def test_port_name_or_matcher_required() -> None:
    """A URL naming neither a port nor a device cannot be opened."""
    api = mock_api_client("Zigbee")
    url = "esphome://127.0.0.1:6053/"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(InvalidSettingsError, match="port name or a port matcher"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_url_matcher_applies_with_external_api() -> None:
    """Matchers in the URL are checked even when the API client is passed in."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")

    with pytest.raises(SerialException, match="port_udev_id="):
        async with async_serial_for_url(
            url="esphome://127.0.0.1:6053/?port_udev_id=usb-Other-if00",
            transport_cls=ESPHomeSerialTransport,
            api=api,
            port_name="Zigbee",
            baudrate=115200,
        ):
            pass


async def test_udev_id_without_usb_is_rejected() -> None:
    """A device without USB IDs has no by-id link to match."""
    api = mock_api_client("Zigbee")
    api.attach_mock(
        AsyncMock(
            return_value=_identity(
                source=SerialProxyIdentitySource.CONFIGURED, usb=UsbDeviceDescriptor()
            )
        ),
        "serial_proxy_get_identity",
    )
    url = (
        "esphome://127.0.0.1:6053/?port_name=Zigbee"
        "&port_udev_id=usb-Nabu_Casa_ZBT-2_AABBCCDDEEFF-if00"
    )

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        with pytest.raises(SerialException, match="port_udev_id=None"):
            async with async_serial_for_url(url=url, baudrate=115200):
                pass


async def test_list_serial_ports_skips_identity_on_old_api() -> None:
    """A device below API 1.18 never answers, so it is not asked."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(), "serial_proxy_get_identity")

    ports = await async_list_serial_ports(Platform.ESPHOME, api=api)

    url = "esphome://127.0.0.1:6053/?port_name=Zigbee"
    assert ports == [
        SerialPortInfo(
            device=url,
            resolved_device=url,
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
            bcd_device=None,
            interface_description="Zigbee",
            interface_num=None,
        )
    ]
    assert len(api.serial_proxy_get_identity.mock_calls) == 0


def _identity_handlers(api: MagicMock) -> list[Callable[[SerialProxyIdentity], None]]:
    """Return the handlers subscribed to identity messages on the mock client."""
    return [c.args[0] for c in api.subscribe_serial_proxy_identity.mock_calls]


async def test_device_removed_breaks_transport() -> None:
    """Pulling the device ends the session like a vanished device node."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200) as serial:
            handlers = _identity_handlers(api)
            assert len(handlers) == 2  # the serial and the transport

            # Another port's device is not our business
            for handler in handlers:
                handler(_identity(instance=1, flags=0, serial_number=""))
            assert not serial.transport.is_closing()

            for handler in handlers:
                handler(_identity(flags=0, serial_number=""))

            with pytest.raises(OSError) as excinfo:
                await serial.read(1)
            assert excinfo.value.errno == errno.ENXIO

            with pytest.raises(OSError):
                serial.write_nowait(b"x")

            assert serial.transport.is_closing()

    # Torn down like a close: the port released, the handlers dropped, and the
    # connection this transport opened for itself closed
    await asyncio.sleep(0)
    assert len(api.subscribe_serial_proxy_identity.return_value.mock_calls) == 2
    assert len(api.serial_proxy_unsubscribe.mock_calls) == 1
    assert len(api.disconnect.mock_calls) == 1


async def test_device_removed_leaves_external_api_alone() -> None:
    """With a shared client, losing the device releases the port but not the client."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")

    async with async_serial_for_url(
        url=None,
        transport_cls=ESPHomeSerialTransport,
        api=api,
        port_name="Zigbee",
        port_serial_number="AABBCCDDEEFF",
        baudrate=115200,
    ) as serial:
        for handler in _identity_handlers(api):
            handler(_identity(flags=0, serial_number=""))

        with pytest.raises(OSError):
            await serial.read(1)
        assert serial.transport.is_closing()

    await asyncio.sleep(0)
    assert len(api.subscribe_serial_proxy_identity.return_value.mock_calls) == 2
    assert len(api.serial_proxy_unsubscribe.mock_calls) == 1
    assert len(api.disconnect.mock_calls) == 0


async def test_device_swapped_breaks_transport() -> None:
    """A different device appearing in the socket is not the one this session claimed."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200) as serial:
            for handler in _identity_handlers(api):
                handler(_identity(serial_number="112233445566"))

            with pytest.raises(OSError) as excinfo:
                await serial.read(1)
            assert excinfo.value.errno == errno.ENXIO
            assert "112233445566" in str(excinfo.value)


async def test_device_swapped_by_other_matcher_breaks_transport() -> None:
    """Matchers other than the serial number keep applying while the port is open."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_usb_vid=0x303A&port_usb_pid=0x4001"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200) as serial:
            for handler in _identity_handlers(api):
                handler(
                    _identity(
                        usb=UsbDeviceDescriptor(  # type:ignore[call-arg]
                            vendor_id=0x303A, product_id=0x1001, interface_number=0
                        )
                    )
                )

            with pytest.raises(OSError) as excinfo:
                await serial.read(1)
            assert excinfo.value.errno == errno.ENXIO
            assert "port_usb_pid=4097, expected 16385" in str(excinfo.value)


async def test_device_reattached_is_not_a_change() -> None:
    """The expected device reporting itself again leaves the session alone."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(return_value=_identity()), "serial_proxy_get_identity")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee&port_serial_number=AABBCCDDEEFF"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200) as serial:
            for handler in _identity_handlers(api):
                handler(_identity())
            assert not serial.transport.is_closing()


async def test_identity_that_says_nothing_about_presence_is_ignored() -> None:
    """A port without identity, or one with unreadable descriptors, breaks nothing."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(), "serial_proxy_get_identity")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200) as serial:
            for handler in _identity_handlers(api):
                handler(
                    _identity(
                        source=SerialProxyIdentitySource.NONE, flags=0, serial_number=""
                    )
                )
                handler(
                    _identity(
                        flags=SerialProxyIdentityFlag.CONNECTED
                        | SerialProxyIdentityFlag.ERROR,
                        serial_number="",
                    )
                )
            assert not serial.transport.is_closing()


async def test_device_removed_without_expected_serial() -> None:
    """Removal breaks the session even when no particular device was required."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(), "serial_proxy_get_identity")
    url = "esphome://127.0.0.1:6053/?port_name=Zigbee"

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(url=url, baudrate=115200) as serial:
            for handler in _identity_handlers(api):
                handler(_identity(flags=0, serial_number=""))

            with pytest.raises(OSError):
                await serial.read(1)
            assert serial.transport.is_closing()


async def test_no_serial_number_skips_the_check() -> None:
    """Without the setting nothing asks the device for the port's identity."""
    api = mock_api_client("Zigbee")
    api.attach_mock(AsyncMock(), "serial_proxy_get_identity")

    with patch("serialx.platforms.serial_esphome.APIClient", return_value=api):
        async with async_serial_for_url(
            url="esphome://127.0.0.1:6053/?port_name=Zigbee", baudrate=115200
        ):
            pass

    assert len(api.serial_proxy_get_identity.mock_calls) == 0
