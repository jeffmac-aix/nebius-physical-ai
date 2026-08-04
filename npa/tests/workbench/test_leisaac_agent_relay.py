from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from npa.workbench.leisaac.agent_relay import _TCPServer, load_config, relay_udp


def test_relay_config_requires_private_target_and_nodeports(tmp_path: Path) -> None:
    path = tmp_path / "relay.json"
    path.write_text(
        json.dumps(
            {
                "target_host": "10.96.0.22",
                "status_port": 30001,
                "signal_port": 30002,
                "media_port": 30003,
            }
        ),
        encoding="utf-8",
    )
    assert load_config(path) == {
        "target_host": "10.96.0.22",
        "status_port": 30001,
        "signal_port": 30002,
        "media_port": 30003,
    }

    for host, port in (("8.8.8.8", 30001), ("127.0.0.1", 30001), ("10.0.0.1", 8080)):
        path.write_text(
            json.dumps(
                {
                    "target_host": host,
                    "status_port": port,
                    "signal_port": 30002,
                    "media_port": 30003,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load_config(path)


def test_tcp_relay_forwards_bidirectionally() -> None:
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen()
    upstream_host = upstream.getsockname()

    def echo() -> None:
        connection, _address = upstream.accept()
        with connection:
            connection.sendall(connection.recv(64).upper())

    echo_thread = threading.Thread(target=echo, daemon=True)
    echo_thread.start()
    relay = _TCPServer(("127.0.0.1", 0), upstream_host)
    relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
    relay_thread.start()
    try:
        with socket.create_connection(relay.server_address) as client:
            client.sendall(b"leisaac")
            client.shutdown(socket.SHUT_WR)
            assert client.recv(64) == b"LEISAAC"
    finally:
        relay.shutdown()
        relay.server_close()
        upstream.close()


def test_udp_relay_forwards_return_media() -> None:
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream_address = upstream.getsockname()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    relay_address = probe.getsockname()
    probe.close()
    stop = threading.Event()

    def echo() -> None:
        payload, address = upstream.recvfrom(64)
        upstream.sendto(payload.upper(), address)

    threading.Thread(target=echo, daemon=True).start()
    thread = threading.Thread(
        target=relay_udp,
        args=(upstream_address,),
        kwargs={"stop": stop, "listen": relay_address},
        daemon=True,
    )
    thread.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2)
    try:
        client.sendto(b"media", relay_address)
        assert client.recv(64) == b"MEDIA"
    finally:
        stop.set()
        thread.join(timeout=2)
        client.close()
        upstream.close()
