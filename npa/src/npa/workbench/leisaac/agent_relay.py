"""TLS backhaul endpoint for a private LeIsaac Kubernetes session.

The GPU pod initiates one authenticated TLS connection to this process on the
public agent VM.  Browser media arrives on the fixed, source-restricted UDP
port; status and signaling remain loopback-only for nginx/the agent backend.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import socket
import socketserver
import ssl
import struct
import threading
from pathlib import Path
from typing import Any

STATUS_LISTEN = ("127.0.0.1", 48080)
SIGNAL_LISTEN = ("127.0.0.1", 49100)
MEDIA_LISTEN = ("0.0.0.0", 47998)
BACKHAUL_LISTEN = ("0.0.0.0", 48081)
HELLO, OPEN, DATA, CLOSE, UDP = 1, 2, 3, 4, 5
HEADER = struct.Struct("!BII")
MAX_FRAME = 4 * 1024 * 1024


def load_config(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("relay config must be an object")
    nonce = str(data.get("session_nonce") or "")
    if len(nonce) != 64 or any(character not in "0123456789abcdef" for character in nonce):
        raise ValueError("relay session nonce must be 64 lowercase hexadecimal characters")
    return {"session_nonce": nonce}


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError("backhaul closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _receive_frame(connection: socket.socket) -> tuple[int, int, bytes]:
    kind, stream_id, size = HEADER.unpack(_receive_exact(connection, HEADER.size))
    if size > MAX_FRAME:
        raise ValueError("backhaul frame is too large")
    return kind, stream_id, _receive_exact(connection, size)


class Backhaul:
    def __init__(self, nonce: str):
        self.nonce = nonce.encode("ascii")
        self.condition = threading.Condition()
        self.connection: ssl.SSLSocket | None = None
        self.send_lock = threading.Lock()
        self.stream_lock = threading.Lock()
        self.streams: dict[int, socket.socket] = {}
        self.next_stream = 1
        self.public_udp: socket.socket | None = None
        self.browser_address: tuple[str, int] | None = None

    def attach(self, connection: ssl.SSLSocket) -> bool:
        kind, stream_id, payload = _receive_frame(connection)
        if kind != HELLO or stream_id != 0 or not hmac.compare_digest(payload, self.nonce):
            return False
        with self.condition:
            if self.connection is not None:
                return False
            self.connection = connection
            self.condition.notify_all()
        return True

    def detach(self, connection: ssl.SSLSocket) -> None:
        with self.condition:
            if self.connection is connection:
                self.connection = None
        with self.stream_lock:
            streams = list(self.streams.values())
            self.streams.clear()
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass

    def send(self, kind: int, stream_id: int, payload: bytes = b"") -> None:
        with self.condition:
            if self.connection is None:
                self.condition.wait_for(lambda: self.connection is not None, timeout=10)
            connection = self.connection
        if connection is None:
            raise ConnectionError("LeIsaac pod backhaul is unavailable")
        frame = HEADER.pack(kind, stream_id, len(payload)) + payload
        with self.send_lock:
            connection.sendall(frame)

    def open_stream(self, client: socket.socket, port: int) -> None:
        with self.stream_lock:
            stream_id = self.next_stream
            self.next_stream += 1
            self.streams[stream_id] = client
        try:
            self.send(OPEN, stream_id, struct.pack("!H", port))
            while True:
                payload = client.recv(65536)
                if not payload:
                    break
                self.send(DATA, stream_id, payload)
        finally:
            with self.stream_lock:
                self.streams.pop(stream_id, None)
            try:
                self.send(CLOSE, stream_id)
            except (ConnectionError, OSError):
                pass

    def handle(self, kind: int, stream_id: int, payload: bytes) -> None:
        if kind == DATA:
            with self.stream_lock:
                stream = self.streams.get(stream_id)
            if stream is not None:
                stream.sendall(payload)
        elif kind == CLOSE:
            with self.stream_lock:
                stream = self.streams.pop(stream_id, None)
            if stream is not None:
                stream.close()
        elif kind == UDP and self.public_udp is not None and self.browser_address is not None:
            self.public_udp.sendto(payload, self.browser_address)


class _TCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.server.backhaul.open_stream(self.request, self.server.target_port)  # type: ignore[attr-defined]


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen: tuple[str, int], backhaul: Backhaul, target_port: int):
        self.backhaul = backhaul
        self.target_port = target_port
        super().__init__(listen, _TCPHandler)


def serve_backhaul(
    backhaul: Backhaul,
    *,
    stop: threading.Event,
    certificate: str,
    private_key: str,
) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, private_key)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(BACKHAUL_LISTEN)
    listener.listen(2)
    listener.settimeout(0.5)
    try:
        while not stop.is_set():
            try:
                raw, _address = listener.accept()
            except socket.timeout:
                continue
            try:
                connection = context.wrap_socket(raw, server_side=True)
                if not backhaul.attach(connection):
                    connection.close()
                    continue
                try:
                    while not stop.is_set():
                        backhaul.handle(*_receive_frame(connection))
                finally:
                    backhaul.detach(connection)
                    connection.close()
            except (EOFError, OSError, ssl.SSLError, ValueError):
                raw.close()
    finally:
        listener.close()


def relay_udp(backhaul: Backhaul, *, stop: threading.Event) -> None:
    public = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    public.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    public.bind(MEDIA_LISTEN)
    public.settimeout(0.5)
    backhaul.public_udp = public
    try:
        while not stop.is_set():
            try:
                payload, address = public.recvfrom(65536)
            except socket.timeout:
                continue
            backhaul.browser_address = address
            try:
                backhaul.send(UDP, 0, payload)
            except (ConnectionError, OSError):
                continue
    finally:
        backhaul.public_udp = None
        public.close()


def serve(config: dict[str, Any], *, certificate: str, private_key: str) -> None:
    backhaul = Backhaul(str(config["session_nonce"]))
    status = _TCPServer(STATUS_LISTEN, backhaul, 8080)
    signaling = _TCPServer(SIGNAL_LISTEN, backhaul, 49100)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    threads = [
        threading.Thread(target=status.serve_forever, daemon=True),
        threading.Thread(target=signaling.serve_forever, daemon=True),
        threading.Thread(
            target=serve_backhaul,
            kwargs={
                "backhaul": backhaul,
                "stop": stop,
                "certificate": certificate,
                "private_key": private_key,
            },
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        relay_udp(backhaul, stop=stop)
    finally:
        status.shutdown()
        signaling.shutdown()
        status.server_close()
        signaling.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    credentials = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not credentials:
        raise RuntimeError("systemd credential directory is required")
    serve(
        load_config(args.config),
        certificate=str(Path(credentials) / "backhaul.crt"),
        private_key=str(Path(credentials) / "backhaul.key"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
