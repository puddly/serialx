# Connecting to an ESPHome serial port
[ESPHome](https://esphome.io/) can expose UARTs over its native API using the [`serial_proxy`](https://esphome.io/components/serial_proxy/) component. serialx can connect to ESPHome serial proxies with the `esphome://host:port?port_name=Name` URL scheme and expose a complete serial port implementation, with flow control, modem pin control, and buffer flushing.

Install the ESPHome extras first, since `aioesphomeapi` is not installed by default:
```bash
pip install 'serialx[esphome]'
```

## Example script

Open a connection straight from the URL. serialx will create and tear down an ESPHome API client at the end of a session:

```python
import asyncio
import serialx

async def main() -> None:
    reader, writer = await serialx.open_serial_connection(
        url="esphome://192.168.1.42:6053/?port_name=Zigbee",
        key="base64-psk-here",
        baudrate=115200,
    )

    try:
        writer.write(b"ping\n")
        await writer.drain()

        data = await reader.readexactly(5)
        print(data)
    finally:
        writer.close()
        await writer.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
```

Use `password=` instead of `noise_psk=` in the query string if the device uses legacy API password authentication. If the device does not use any authentication, you can omit this keyword argument.

## Proxy modes
A serial proxy streams an opaque byte stream by default. A port configured with a protocol-aware tap can instead be switched into `mode=protocol`, letting the device handle that protocol's timing-sensitive work on your behalf. It is still presented locally as a plain serial port:

```python
reader, writer = await serialx.open_serial_connection(
    url="esphome://192.168.1.42:6053/?port_name=Zigbee&mode=protocol",
    baudrate=115200,
)
```

The mode is applied before the proxy starts streaming. Only `raw` (the default) and `protocol` are accepted; any other value is rejected. Which protocol `protocol` engages depends on the tap the device's configuration gives that port.

## Reusing an existing API client
If your application already holds an `aioesphomeapi.APIClient`, pass it to the transport directly to avoid opening a second connection:

```python
import asyncio
import serialx

from aioesphomeapi import APIClient
from serialx.platforms.serial_esphome import ESPHomeSerialTransport


async def main() -> None:
    api = APIClient(
        address="192.168.1.42",
        port=6053,
        noise_psk="base64-psk-here",
        password=None,
    )
    await api.connect(login=True)

    reader, writer = await serialx.open_serial_connection(
        url=None,
        transport_cls=ESPHomeSerialTransport,
        api=api,
        port_name="Zigbee",
        baudrate=115200,
    )

    try:
        data = await reader.readexactly(5)
        print(data)
    finally:
        writer.close()
        await writer.wait_closed()
        await api.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
```

When the API client is passed in externally, serialx does not disconnect it on close, it only unsubscribes from the specific serial port.
