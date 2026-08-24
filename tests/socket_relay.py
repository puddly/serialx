"""Test sync APIs with socket:// endpoints."""

# Async imports
import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
import contextlib
import logging
import queue
import socket
import struct
import threading
import time

LOGGER = logging.getLogger(__name__)
_RelayQueueItem = tuple[bytes | None, socket.socket | None]


class _SocketPairRelay:
    def __init__(self) -> None:
        self.left_to_right: queue.Queue[_RelayQueueItem] = queue.Queue()
        self.right_to_left: queue.Queue[_RelayQueueItem] = queue.Queue()
        self.stop_event = threading.Event()
        self.active_connections: dict[str, socket.socket | None] = {
            "left": None,
            "right": None,
        }
        self.active_lock = threading.Lock()
        self.relay_threads: list[threading.Thread] = []
        self.left_server = self._make_server()
        self.right_server = self._make_server()
        self.left_url = f"socket://127.0.0.1:{self.left_server.getsockname()[1]}"
        self.right_url = f"socket://127.0.0.1:{self.right_server.getsockname()[1]}"

    @staticmethod
    def _close_socket(sock: socket.socket | None) -> None:
        if sock is None:
            return
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            sock.close()

    @staticmethod
    def _reset_socket(sock: socket.socket | None) -> None:
        """Abruptly drop a connection with a RST, simulating a yanked link."""
        if sock is None:
            return

        with contextlib.suppress(OSError):
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("ii", 1, 0),
            )

        with contextlib.suppress(OSError):
            sock.close()

    @staticmethod
    def _make_server() -> socket.socket:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(0.1)
        return server

    def _set_active_connection(self, side: str, conn: socket.socket) -> None:
        with self.active_lock:
            previous = self.active_connections[side]
            self.active_connections[side] = conn
        if previous is not conn:
            self._close_socket(previous)

    def _clear_active_connection(self, side: str, conn: socket.socket) -> bool:
        with self.active_lock:
            if self.active_connections[side] is conn:
                self.active_connections[side] = None
                return True
            return False

    def _get_active_connection(self, side: str) -> socket.socket | None:
        with self.active_lock:
            return self.active_connections[side]

    def _reader_loop(
        self,
        side: str,
        conn: socket.socket,
        outbound_queue: queue.Queue[_RelayQueueItem],
        peer_side: str,
    ) -> None:
        try:
            # Bounded recv so we don't deadlock or pin the FD
            conn.settimeout(0.5)

            while not self.stop_event.is_set():
                try:
                    data = conn.recv(4096)
                except TimeoutError:
                    continue  # Ignore timeouts

                if not data:
                    LOGGER.debug("%s client reached EOF", side)
                    return
                outbound_queue.put((data, self._get_active_connection(peer_side)))
                LOGGER.debug(
                    "queued %d bytes from %s to %s",
                    len(data),
                    side,
                    peer_side,
                )
        except OSError:
            LOGGER.debug("%s client disconnected abruptly", side, exc_info=True)
        finally:
            was_active = self._clear_active_connection(side, conn)
            if was_active:
                outbound_queue.put((None, self._get_active_connection(peer_side)))
            self._close_socket(conn)
            LOGGER.debug("closed %s client connection", side)

    def _accept_loop(
        self,
        side: str,
        server_sock: socket.socket,
        outbound_queue: queue.Queue[_RelayQueueItem],
        peer_side: str,
    ) -> None:
        while not self.stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self.stop_event.is_set():
                    LOGGER.debug("%s server accept failed", side, exc_info=True)
                return

            LOGGER.debug("accepted %s client connection", side)
            self._set_active_connection(side, conn)

            reader_thread = threading.Thread(
                target=self._reader_loop,
                args=(side, conn, outbound_queue, peer_side),
                daemon=True,
            )
            self.relay_threads.append(reader_thread)
            reader_thread.start()

    def _writer_loop(
        self,
        side: str,
        inbound_queue: queue.Queue[_RelayQueueItem],
        peer_side: str,
    ) -> None:
        while not self.stop_event.is_set():
            try:
                data, expected_conn = inbound_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if data is None:
                if (
                    expected_conn is not None
                    and self._get_active_connection(side) is expected_conn
                ):
                    self.disconnect_side(side, abrupt=False)
                continue

            conn = self._get_active_connection(side)
            while conn is None and not self.stop_event.is_set():
                time.sleep(0.001)
                conn = self._get_active_connection(side)
            if conn is None:
                continue
            if expected_conn is not None and conn is not expected_conn:
                continue

            try:
                conn.sendall(data)
                LOGGER.debug(
                    "forwarded %d bytes from %s to %s",
                    len(data),
                    peer_side,
                    side,
                )
            except OSError:
                self._clear_active_connection(side, conn)
                self._close_socket(conn)
                LOGGER.debug(
                    "failed forwarding bytes from %s to %s",
                    peer_side,
                    side,
                    exc_info=True,
                )

    def start(self) -> None:
        LOGGER.debug(
            "started socket pair server left=%s right=%s",
            self.left_url,
            self.right_url,
        )
        self.relay_threads.extend(
            [
                threading.Thread(
                    target=self._accept_loop,
                    args=("left", self.left_server, self.left_to_right, "right"),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._accept_loop,
                    args=("right", self.right_server, self.right_to_left, "left"),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._writer_loop,
                    args=("left", self.right_to_left, "right"),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._writer_loop,
                    args=("right", self.left_to_right, "left"),
                    daemon=True,
                ),
            ]
        )
        for relay_thread in self.relay_threads:
            relay_thread.start()

    def disconnect_side(self, side: str, *, abrupt: bool) -> None:
        # The client's connect() returns once the kernel completes the
        # handshake, which can be before the accept loop has handed us the
        # socket. Wait for it rather than no-op: it is guaranteed to arrive.
        deadline = time.monotonic() + 5.0
        conn = self._get_active_connection(side)
        while conn is None and time.monotonic() < deadline:
            time.sleep(0.005)
            conn = self._get_active_connection(side)

        if conn is None:
            return

        self._clear_active_connection(side, conn)

        if abrupt:
            self._reset_socket(conn)
        else:
            self._close_socket(conn)

    def close(self) -> None:
        self.stop_event.set()
        self._close_socket(self.left_server)
        self._close_socket(self.right_server)
        with self.active_lock:
            connections = list(self.active_connections.values())
            self.active_connections["left"] = None
            self.active_connections["right"] = None
        for conn in connections:
            self._close_socket(conn)
        for relay_thread in self.relay_threads:
            relay_thread.join()
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
