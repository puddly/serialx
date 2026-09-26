# Usage
serialx supports both synchronous and asynchronous APIs on all platforms.

## Sync
```python
import serialx

with serialx.serial_for_url("/dev/serial/by-id/port", baudrate=115200) as serial:
    data = serial.readexactly(5)
    serial.write(b"test")
```

## Async
There are quite a few approaches in async Python. serialx supports all of the popular
asyncio primitives in addition to a simple async API. It's recommended to use the async
API over the sync API in general, as the async API allows for granular timeouts, 
task cancellation, and concurrency.

### Async (simple API)
For a simple translation of sync code into async code, you can use `serialx.async_serial_for_url`:
```python
import serialx

async with serialx.async_serial_for_url("/dev/serial/by-id/port", baudrate=115200) as serial:
    data = await serial.readexactly(5)
    await serial.write(b"test")
```

All functions, including `open` and `close`, are async and work exactly as they do with
the sync API.

## Async (`StreamReader` and `StreamWriter`)
A `(StreamReader, StreamWriter)` pair is available for code already wired up to
the asyncio streams API:

```python
import serialx

reader, writer = await serialx.open_serial_connection(
    "/dev/serial/by-id/port",
    baudrate=115200,
)

try:
    data = await reader.readexactly(5)
    writer.write(b"test")
    await writer.drain()
finally:
    writer.close()
    await writer.wait_closed()
```

## Async (transport)
For protocol-style consumers that want raw `asyncio.Protocol` callbacks:

```python
import asyncio
import serialx

loop = asyncio.get_running_loop()
transport, protocol = await serialx.create_serial_connection(
    loop=loop,
    protocol_factory=your_protocol_factory,
    url="/dev/serial/by-id/port",
    baudrate=115200,
)
```

## Reconfiguring a port
`reconfigure_port` changes settings on an open port. Only the settings passed are changed:

```python
serial.reconfigure_port(baudrate=9600, parity=serialx.Parity.EVEN)
```

The async APIs expose the same method as a coroutine:

```python
await serial.reconfigure_port(baudrate=9600)
await transport.reconfigure_port(baudrate=9600)
```
