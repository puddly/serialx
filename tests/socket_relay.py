"""Test sync APIs with socket:// endpoints."""

# Async imports
import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
import contextlib
import logging
import queue
import selectors
import socket
import struct
import threading

LOGGER = logging.getLogger(__name__)

_PEERS = {"left": "right", "right": "left"}


class _SocketPairRelay:
    """Relay bytes between the current client of each of two listening sockets."""

    def __init__(self) -> None:
        self._selector = selectors.DefaultSelector()
        self._servers = {"left": self._make_server(), "right": self._make_server()}
        self._connections: dict[str, socket.socket | None] = {
            "left": None,
            "right": None,
        }
        self._outgoing = {"left": bytearray(), "right": bytearray()}
        self._commands: queue.Queue[tuple[Callable[[], None], threading.Event]] = (
            queue.Queue()
        )
        self._wakeup_reader, self._wakeup_writer = socket.socketpair()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)

        for side, server in self._servers.items():
            self._selector.register(server, selectors.EVENT_READ, ("server", side))

        self._selector.register(
            self._wakeup_reader, selectors.EVENT_READ, ("wakeup", None)
        )

        self.left_url = f"socket://127.0.0.1:{self._servers['left'].getsockname()[1]}"
        self.right_url = f"socket://127.0.0.1:{self._servers['right'].getsockname()[1]}"

    @staticmethod
    def _make_server() -> socket.socket:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.setblocking(False)
        return server

    def _run(self) -> None:
        while not self._stopped:
            events = self._selector.select()
            self._accept_pending()

            for key, _mask in events:
                kind, side = key.data

                if kind == "wakeup":
                    self._wakeup_reader.recv(4096)
                elif kind == "conn" and key.fileobj is self._connections[side]:
                    self._flush(side)
                    # `_flush` may have dropped the connection
                    if key.fileobj is self._connections[side]:
                        self._read(side)

            while not self._commands.empty():
                command, done = self._commands.get()
                self._accept_pending()
                command()
                done.set()

    def _accept_pending(self) -> None:
        for side, server in self._servers.items():
            while True:
                try:
                    conn, _ = server.accept()
                except BlockingIOError:
                    break

                LOGGER.debug("accepted %s client connection", side)
                conn.setblocking(False)
                self._close_connection(side, abrupt=False)
                self._connections[side] = conn
                self._selector.register(conn, selectors.EVENT_READ, ("conn", side))
                self._flush(side)

    def _read(self, side: str) -> None:
        conn = self._connections[side]
        assert conn is not None

        try:
            data = conn.recv(65536)
        except BlockingIOError:
            return
        except ConnectionResetError:
            data = b""

        if not data:
            LOGGER.debug("%s client disconnected", side)
            self._close_connection(side, abrupt=False)
            return

        peer = _PEERS[side]
        LOGGER.debug("relaying %d bytes from %s to %s", len(data), side, peer)
        self._outgoing[peer] += data
        self._flush(peer)

    def _flush(self, side: str) -> None:
        conn = self._connections[side]
        outgoing = self._outgoing[side]

        if conn is None:
            return

        if outgoing:
            try:
                sent = conn.send(outgoing)
            except BlockingIOError:
                sent = 0
            except (BrokenPipeError, ConnectionResetError):
                self._close_connection(side, abrupt=False)
                return

            del outgoing[:sent]

        events = selectors.EVENT_READ
        if outgoing:
            events |= selectors.EVENT_WRITE

        self._selector.modify(conn, events, ("conn", side))

    def _close_connection(self, side: str, *, abrupt: bool) -> None:
        conn = self._connections[side]
        if conn is None:
            return

        self._connections[side] = None
        self._outgoing[side].clear()
        self._selector.unregister(conn)

        if abrupt:
            # A zero linger timeout makes `close()` send a RST, like a yanked cable
            with contextlib.suppress(OSError):
                conn.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
        else:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)

        conn.close()

    def _call(self, command: Callable[[], None]) -> None:
        """Run a command on the relay thread and wait for it to finish."""
        done = threading.Event()
        self._commands.put((command, done))
        self._wakeup_writer.send(b"\x00")
        done.wait()

    def _stop(self) -> None:
        self._stopped = True

    def start(self) -> None:
        LOGGER.debug(
            "started socket pair server left=%s right=%s",
            self.left_url,
            self.right_url,
        )
        self._thread.start()

    def disconnect_side(self, side: str, *, abrupt: bool) -> None:
        self._call(lambda: self._close_connection(side, abrupt=abrupt))

    def close(self) -> None:
        self._call(self._stop)
        self._thread.join()

        for side in _PEERS:
            self._close_connection(side, abrupt=False)

        for server in self._servers.values():
            server.close()

        self._selector.close()
        self._wakeup_reader.close()
        self._wakeup_writer.close()
        LOGGER.debug("stopped socket pair servers")


@contextlib.contextmanager
def create_silent_server() -> Iterator[str]:
    """Create a TCP server that accepts connections but never sends data."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.1)

    clients: list[socket.socket] = []
    stop = threading.Event()

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            clients.append(client)

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()

    try:
        yield f"127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        for client in clients:
            client.close()
        server.close()
        thread.join(timeout=1)


@contextlib.contextmanager
def create_accept_then_close_server() -> Iterator[str]:
    """Create a TCP server that accepts and immediately closes each connection."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.1)

    stop = threading.Event()

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            # Drain so close() produces FIN; with unread data macOS sends RST,
            # which surfaces as ECONNRESET rather than the 0-byte recv we want.
            client.settimeout(0.1)
            with contextlib.suppress(OSError):
                while client.recv(4096):
                    pass
            client.close()

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()

    try:
        yield f"127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        server.close()
        thread.join(timeout=1)


@contextlib.contextmanager
def create_socket_pair() -> Iterator[
    tuple[str, str, Callable[[], None], Callable[[], None]]
]:
    """Create two socket:// endpoints backed by a bidirectional relay.

    The relay can drop the left connection either gracefully (FIN) or abruptly
    (RST), so both unplug flavors are available.
    """
    relay = _SocketPairRelay()
    relay.start()
    try:
        yield (
            relay.left_url,
            relay.right_url,
            lambda: relay.disconnect_side("left", abrupt=False),
            lambda: relay.disconnect_side("left", abrupt=True),
        )
    finally:
        relay.close()


@contextlib.asynccontextmanager
async def async_create_socket_pair(
    relay_read_delay: float = 0.0,
) -> AsyncIterator[tuple[str, str]]:
    """Create two socket:// endpoints backed by a bidirectional relay."""
    left_to_right: asyncio.Queue[bytes | None] = asyncio.Queue()
    right_to_left: asyncio.Queue[bytes | None] = asyncio.Queue()
    handler_tasks: set[asyncio.Task[None]] = set()

    async def handle_client(
        side: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        handler_tasks.add(task)

        peer_side = "right" if side == "left" else "left"
        LOGGER.debug("accepted %s client connection", side)
        outbound_queue = left_to_right if side == "left" else right_to_left
        inbound_queue = right_to_left if side == "left" else left_to_right

        async def reader_to_queue() -> None:
            try:
                while data := await reader.read(4096):
                    await outbound_queue.put(data)
                    LOGGER.debug(
                        "queued %d bytes from %s to %s",
                        len(data),
                        side,
                        peer_side,
                    )
                    if relay_read_delay > 0:
                        await asyncio.sleep(relay_read_delay)
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.debug("%s client disconnected abruptly", side)
            finally:
                await outbound_queue.put(None)
                LOGGER.debug("%s client reached EOF", side)

        async def queue_to_writer() -> None:
            while True:
                data = await inbound_queue.get()
                if data is None:
                    return

                try:
                    writer.write(data)
                    await writer.drain()
                    LOGGER.debug(
                        "forwarded %d bytes from %s to %s",
                        len(data),
                        peer_side,
                        side,
                    )
                except (BrokenPipeError, ConnectionResetError, OSError):
                    LOGGER.debug(
                        "failed forwarding bytes from %s to %s",
                        peer_side,
                        side,
                        exc_info=True,
                    )
                    return

        read_task = asyncio.create_task(reader_to_queue())
        write_task = asyncio.create_task(queue_to_writer())

        try:
            done, pending = await asyncio.wait(
                {read_task, write_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for pending_task in pending:
                pending_task.cancel()

            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*done, return_exceptions=True)
        finally:
            for relay_task in (read_task, write_task):
                if not relay_task.done():
                    relay_task.cancel()
            await asyncio.gather(read_task, write_task, return_exceptions=True)
            writer.close()
            with contextlib.suppress(
                ConnectionResetError,
                BrokenPipeError,
                OSError,
                asyncio.CancelledError,
            ):
                await writer.wait_closed()
            handler_tasks.discard(task)
            LOGGER.debug("closed %s client connection", side)

    left_server = await asyncio.start_server(
        lambda reader, writer: handle_client("left", reader, writer),
        host="127.0.0.1",
        port=0,
    )
    right_server = await asyncio.start_server(
        lambda reader, writer: handle_client("right", reader, writer),
        host="127.0.0.1",
        port=0,
    )

    left_socket_info = left_server.sockets
    right_socket_info = right_server.sockets
    assert left_socket_info is not None and left_socket_info
    assert right_socket_info is not None and right_socket_info

    left_url = f"socket://127.0.0.1:{left_socket_info[0].getsockname()[1]}"
    right_url = f"socket://127.0.0.1:{right_socket_info[0].getsockname()[1]}"
    LOGGER.debug("started socket pair server left=%s right=%s", left_url, right_url)

    try:
        yield (left_url, right_url)
    finally:
        wait_closed_coros = []
        for server in (left_server, right_server):
            try:
                server.close()
            except OSError:  # noqa: PERF203
                continue
            wait_closed_coros.append(server.wait_closed())

        if wait_closed_coros:
            await asyncio.gather(*wait_closed_coros)

        if handler_tasks:
            for task in list(handler_tasks):
                task.cancel()
            await asyncio.gather(*handler_tasks, return_exceptions=True)

        LOGGER.debug("stopped socket pair servers")
