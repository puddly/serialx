# Async serial
Python's asyncio module introduces many similar async primitives. serialx provides APIs
for both high-level and low-level async code.

## Async-with
`async with` is the simplest pattern and is suited for scripts and self-contained code:

```python
import serialx

async with serialx.async_serial_for_url(
    "/dev/serial/by-id/port", baudrate=115200,
) as serial:
    await serial.write(b"ping")
    data = await serial.readexactly(4)
```

Unlike the sync API, you have all of the asyncio primitives at your disposal, including
granular task cancellation, timeouts, and concurrency:

```python
import asyncio

async with serialx.async_serial_for_url(
    "/dev/serial/by-id/port", baudrate=115200,
) as serial:
    async with asyncio.TaskGroup() as tg:
        async def ping() -> None:
            while True:
                await serial.write(b"ping")
                await asyncio.sleep(1)

        tg.create_task(ping())

        async with asyncio.timeout(30):
            data = await serial.readexactly(4)
```

### Manual open and close
The instance returned by `async_serial_for_url` is unopened. Open and close
explicitly when you need to keep the connection alive across function boundaries:

```python
serial = serialx.async_serial_for_url(
    "/dev/serial/by-id/port", baudrate=115200,
)

await serial.open()

try:
    ...
finally:
    await serial.close()
```

### Reading and writing
Reads and writes are coroutines. `write()` queues the data and waits until it
has been handed to the OS, so write errors surface at the call site:

```python
data = await serial.read(64)              # up to 64 bytes
chunk = await serial.readexactly(32)      # exactly 32 bytes
line = await serial.readline()            # through the next \n
header = await serial.readuntil(b"\r\n")  # through a custom delimiter

await serial.write(b"hello ")
await serial.write(b"world\n")
```

To batch several writes before yielding, use the `_nowait` variants and `drain()`:

```python
serial.write_nowait(b"hello ")
serial.write_nowait(b"world\n")
await serial.drain()
```

### Modem pins
Modem control pins are async, since some transports (ESPHome, RFC2217) round-trip
to the device:

```python
await serial.set_modem_pins(rts=True, dtr=True)
pins = await serial.get_modem_pins()
assert pins.rts is serialx.PinState.HIGH
```

### Reconfiguring
`reconfigure_port` changes settings on the open port. Only the settings passed are
changed:

```python
await serial.reconfigure_port(baudrate=9600, parity=serialx.Parity.EVEN)
```

## Async protocols and transports
While the high-level async API is useful for simple code, libraries and other
high-performance uses should use asyncio transports and protocols. These have the
benefit of allowing an `asyncio.Protocol` to immediately enqueue data in the same event
loop cycle as it is received.

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
