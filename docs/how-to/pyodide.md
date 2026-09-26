# Connecting to a Web Serial port under Pyodide
The Pyodide backend exposes the browser's [Web Serial API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Serial_API) through the same `serialx` API you'd use on any other platform. The backend is registered automatically when running under Pyodide, with URIs of the form `pyodide://<name>`. There is no sync `Serial` class, the backend is async-only.

## Registering a port
Unlike the other platforms, security constraints require user interaction before a reference to a serial port is given to the application. Use `navigator.serial.requestPort()` in response to a user interaction to get a [`SerialPort`](https://developer.mozilla.org/en-US/docs/Web/API/SerialPort) JS object. Once you have a reference to the `SerialPort` object, register the object with pyodide and attach it to a URI:

```javascript
const pyodide = await loadPyodide();
const registerJsPort = pyodide.pyimport(
    "serialx.platforms.serial_pyodide"
).register_js_port;

document.getElementById("connect").addEventListener("click", async () => {
    const port = await navigator.serial.requestPort();
    registerJsPort("pyodide://my-device", port);
});
```

The Python half of your application can now connect to `pyodide://my-device` using the regular serialx async APIs (see the [quickstart](../quickstart.md)):

```python
import serialx

reader, writer = await serialx.open_serial_connection(
    "pyodide://my-device",
    baudrate=115200,
)

writer.write(b"ping")
data = await reader.readexactly(4)
```

## Limitations
- Web Serial only accepts port settings on open. `reconfigure_port()` closes and reopens the browser port, which drops DTR and RTS and discards unread bytes. A warning is logged.
- Software flow control (`xonxoff=True`) is accepted but has no effect.
