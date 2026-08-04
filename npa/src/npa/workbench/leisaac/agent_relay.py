"""Minimal TCP/UDP relay for a private LeIsaac Kubernetes NodePort service.

This process runs on an NPA agent VM.  Status and WebSocket signaling bind only
to loopback; WebRTC media binds its fixed public UDP port.  The cloud security
group remains the source-of-truth allowlist for that UDP socket.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import selectors
import signal
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any

STATUS_LISTEN = ("127.0.0.1", 48080)
SIGNAL_LISTEN = ("127.0.0.1", 49100)
MEDIA_LISTEN = ("0.0.0.0", 47998)


def load_config(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("relay config must be an object")
    target = ipaddress.ip_address(str(data.get("target_host") or ""))
    if target.is_global or target.is_loopback or target.is_unspecified:
        raise ValueError("relay target must be a private VPC address")
    result: dict[str, Any] = {"target_host": target.compressed}
    for name in ("status_port", "signal_port", "media_port"):
        port = int(data.get(name) or 0)
        if port < 30000 or port > 32767:
            raise ValueError(f"{name} must be a Kubernetes NodePort")
        result[name] = port
    return result


def _copy(source: socket.socket, destination: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            destination.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _TCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = socket.create_connection(self.server.target, timeout=10)  # type: ignore[attr-defined]
        upstream.settimeout(None)
        self.request.settimeout(None)
        reverse = threading.Thread(
            target=_copy,
            args=(upstream, self.request),
            daemon=True,
        )
        reverse.start()
        _copy(self.request, upstream)
        reverse.join(timeout=2)
        upstream.close()


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen: tuple[str, int], target: tuple[str, int]):
        self.target = target
        super().__init__(listen, _TCPHandler)


def relay_udp(
    target: tuple[str, int],
    *,
    stop: threading.Event,
    listen: tuple[str, int] = MEDIA_LISTEN,
) -> None:
    public = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    public.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    public.bind(listen)
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.connect(target)
    selector = selectors.DefaultSelector()
    selector.register(public, selectors.EVENT_READ, "public")
    selector.register(upstream, selectors.EVENT_READ, "upstream")
    client: tuple[str, int] | None = None
    try:
        while not stop.is_set():
            for key, _events in selector.select(timeout=0.5):
                if key.data == "public":
                    payload, client = public.recvfrom(65536)
                    upstream.send(payload)
                elif client is not None:
                    payload = upstream.recv(65536)
                    public.sendto(payload, client)
    finally:
        selector.close()
        upstream.close()
        public.close()


def serve(config: dict[str, Any]) -> None:
    host = str(config["target_host"])
    status = _TCPServer(STATUS_LISTEN, (host, int(config["status_port"])))
    signal_server = _TCPServer(SIGNAL_LISTEN, (host, int(config["signal_port"])))
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    threads = [
        threading.Thread(target=status.serve_forever, daemon=True),
        threading.Thread(target=signal_server.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        relay_udp(
            (host, int(config["media_port"])),
            stop=stop,
        )
    finally:
        status.shutdown()
        signal_server.shutdown()
        status.server_close()
        signal_server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    serve(load_config(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
