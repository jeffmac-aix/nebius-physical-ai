from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from npa.workbench.leisaac.agent_relay import (
    DATA,
    HELLO,
    OPEN,
    Backhaul,
    _receive_frame,
    load_config as load_server_config,
)
from npa.workbench.leisaac.reverse_client import load_config as load_client_config


NONCE = "a" * 64


def test_relay_configs_pin_nonce_public_agent_and_certificate(tmp_path: Path) -> None:
    server_path = tmp_path / "server.json"
    server_path.write_text(json.dumps({"session_nonce": NONCE}), encoding="utf-8")
    assert load_server_config(server_path) == {"session_nonce": NONCE}

    client_path = tmp_path / "client.json"
    client_path.write_text(
        json.dumps(
            {
                "agent_host": "8.8.8.8",
                "session_nonce": NONCE,
                "certificate_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    assert load_client_config(client_path) == {
        "agent_host": "8.8.8.8",
        "session_nonce": NONCE,
        "certificate_sha256": "b" * 64,
    }

    for override in (
        {"agent_host": "127.0.0.1"},
        {"session_nonce": "bad"},
        {"certificate_sha256": "bad"},
    ):
        data = json.loads(client_path.read_text(encoding="utf-8"))
        data.update(override)
        client_path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError):
            load_client_config(client_path)


def test_backhaul_rejects_wrong_nonce_and_multiplexes_loopback_tcp() -> None:
    backhaul = Backhaul(NONCE)
    server_connection, pod_connection = socket.socketpair()

    def pod() -> None:
        pod_connection.sendall(
            __import__("struct").pack("!BII", HELLO, 0, len(NONCE))
            + NONCE.encode("ascii")
        )
        kind, stream_id, payload = _receive_frame(pod_connection)
        assert kind == OPEN
        assert payload == __import__("struct").pack("!H", 8080)
        pod_connection.sendall(
            __import__("struct").pack("!BII", DATA, stream_id, 5) + b"READY"
        )

    threading.Thread(target=pod, daemon=True).start()
    assert backhaul.attach(server_connection) is True
    local_server, local_client = socket.socketpair()
    threading.Thread(
        target=backhaul.open_stream,
        args=(local_server, 8080),
        daemon=True,
    ).start()
    kind, stream_id, payload = _receive_frame(server_connection)
    backhaul.handle(kind, stream_id, payload)
    assert local_client.recv(5) == b"READY"
    local_client.close()
    pod_connection.close()
    server_connection.close()


def test_backhaul_rejects_unauthenticated_hello() -> None:
    backhaul = Backhaul(NONCE)
    server_connection, peer = socket.socketpair()
    peer.sendall(
        __import__("struct").pack("!BII", HELLO, 0, 64) + ("b" * 64).encode("ascii")
    )
    assert backhaul.attach(server_connection) is False
    peer.close()
    server_connection.close()
