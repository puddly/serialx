# Introduction
Serialx is a no-compromise serial communication library for Python targeting common
platforms such as Linux (POSIX), macOS, and Windows. It provides both synchronous and
native asynchronous APIs for all platforms.

**For more information, visit serialx's documentation: https://puddly.github.io/serialx/**

# Installation
```console
pip install serialx
```

For drop-in import compatibility (`serial`, `serial_asyncio`, `serial_asyncio_fast`) in
environments where existing code cannot be migrated:
```console
pip install serialx-compat
```

# Usage
Serialx features a familiar synchronous API:

```Python
import serialx

with serialx.serial_for_url("/dev/serial/by-id/port", baudrate=115200) as serial:
    data = serial.readexactly(5)
    serial.write(b"test")

    serial.set_modem_pins(rts=True, dtr=True)
    pins = serial.get_modem_pins()
    assert pins.rts is serialx.PinState.HIGH
    assert pins.dtr is serialx.PinState.HIGH
```

An async equivalent of the synchronous API:

```Python
import asyncio
import serialx

async def main():
    async with serialx.async_serial_for_url(
        "/dev/serial/by-id/port", baudrate=115200,
    ) as serial:
        data = await serial.readexactly(5)
        await serial.write(b"test")

        await serial.set_modem_pins(rts=True, dtr=True)
        pins = await serial.get_modem_pins()
        assert pins.rts is serialx.PinState.HIGH
```

A `(StreamReader, StreamWriter)` pair is also available for code already wired up to
the asyncio streams API:

```Python
import asyncio
import serialx

async def main():
    reader, writer = await serialx.open_serial_connection(
        "/dev/serial/by-id/port", baudrate=115200,
    )

    try:
        data = await reader.readexactly(5)
        writer.write(b"test")
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()
```

And a low-level asynchronous serial transport for protocol-style consumers:

```Python
import asyncio
import serialx

async def main():
    loop = asyncio.get_running_loop()
    protocol = YourProtocol()

    transport, protocol = await serialx.create_serial_connection(
        loop,
        lambda: protocol,
        url="/dev/serial/by-id/port",
        baudrate=115200,
    )

    await transport.set_modem_pins(rts=True, dtr=True)
```

## ESPHome serial proxy
Serialx can communicate with serial devices exposed by [ESPHome](https://esphome.io/).

It can either create the API instance directly, for simplicity:

```python
from serialx import open_serial_connection

reader, writer = await open_serial_connection(
    url="esphome://192.168.1.42:6053/?port_name=Zigbee&key=...",
    baudrate=115200,
)
```

Or reuse an existing API instance, for efficiency:
```python
from aioesphomeapi import APIClient
from serialx import open_serial_connection
from serialx.platforms.serial_esphome import ESPHomeSerialTransport

# An external API instance
api = APIClient(address="192.168.1.42", port=6053, key="...", password=None)
await api.connect(login=True)

reader, writer = await open_serial_connection(
    url=None,
    transport_cls=ESPHomeSerialTransport,
    api=api,
    port_name="Zigbee",
    baudrate=115200,
)
```

# Development
All development dependencies are listed in `pyproject.toml`. To install them, use:
```bash
uv pip install '.[dev]'
```

On macOS and Windows, a Rust toolchain is required to build the native serial port
enumeration extension. Install Rust via [rustup](https://rustup.rs/).

Set up pre-commit hooks with `pre-commit install`. Your code will then be type checked
and auto-formatted when you run `git commit`. You can do this on-demand with
`pre-commit run`.

Serialx relies on automated testing. CI runs tests using both `socat` virtual PTYs
(Linux/macOS) and socket-based serial pairs. To also test with physical adapter pairs,
pass CLI flags to `pytest`:

```bash
pytest --adapter-pair=/dev/serial/by-id/left1,/dev/serial/by-id/right1 \
       --adapter-pair=/dev/serial/by-id/left2,/dev/serial/by-id/right2
```

Each test runs once, on any one of the pairs. More pairs let more `pytest-xdist`
workers run adapter tests at once.

By default, tests run in parallel. You can disable this by passing `-n 0` to `pytest`.
