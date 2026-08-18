"""Connect Antioch's authenticated local tunnel to a local policy port-forward."""

from __future__ import annotations

import argparse
import socket
import time

from reverse_policy_relay import ReversePolicyRelay


def _connect(host: str, port: int) -> socket.socket:
    while True:
        try:
            return socket.create_connection((host, port), timeout=5)
        except OSError:
            time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay-host", default="127.0.0.1")
    parser.add_argument("--relay-port", type=int, default=18123)
    parser.add_argument("--policy-host", default="127.0.0.1")
    parser.add_argument("--policy-port", type=int, default=18000)
    parser.add_argument("--sessions", type=int, default=4)
    args = parser.parse_args()
    if args.sessions < 1:
        parser.error("--sessions must be positive")
    for _ in range(args.sessions):
        relay = _connect(args.relay_host, args.relay_port)
        policy = _connect(args.policy_host, args.policy_port)
        ReversePolicyRelay._pipe_pair(relay, policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
