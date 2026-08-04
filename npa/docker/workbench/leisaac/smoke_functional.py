#!/usr/bin/env python3
"""Golden eval: start real PickOrange keyboard teleoperation and WebRTC."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request


def main() -> int:
    environment = os.environ.copy()
    environment.setdefault("NPA_LEISAAC_RUN_ID", "leisaac-golden-eval")
    environment.setdefault("NPA_LEISAAC_SESSION_NONCE", "a" * 64)
    environment.setdefault("NPA_LEISAAC_MEDIA_HOST", "127.0.0.1")
    process = subprocess.Popen(
        [
            "/opt/npa/sim/venv/bin/python",
            "/opt/npa/leisaac/session_server.py",
        ],
        env=environment,
    )
    try:
        while process.poll() is None:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8080/status") as response:
                    status = json.loads(response.read().decode("utf-8"))
                if (
                    status.get("state") == "ready"
                    and status.get("webrtc_ready") is True
                ):
                    if status.get("task") != "LeIsaac-SO101-PickOrange-v0":
                        raise RuntimeError(f"wrong real task: {status}")
                    if status.get("teleop_device") != "keyboard":
                        raise RuntimeError(f"wrong teleoperation device: {status}")
                    if "RTX" not in str(status.get("gpu") or "") and "L40S" not in str(
                        status.get("gpu") or ""
                    ):
                        raise RuntimeError(f"not running on an RT-core GPU: {status}")
                    print(json.dumps(status, indent=2, sort_keys=True))
                    print("NPA_LEISAAC_PICK_ORANGE_KEYBOARD_WEBRTC_OK")
                    return 0
            except (OSError, urllib.error.HTTPError, ValueError):
                pass
            time.sleep(2)
        raise RuntimeError(f"LeIsaac service exited before ready: {process.returncode}")
    finally:
        process.terminate()
        process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
