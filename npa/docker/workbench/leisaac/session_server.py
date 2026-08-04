#!/usr/bin/env python3
"""Start the real LeIsaac teleoperator and expose its browser-streaming assets.

Isaac Sim, task assets, and NVIDIA's WebRTC browser client are fetched only at
runtime after the operator has explicitly accepted the two NVIDIA EULAs.  None
of those bytes are part of the distributable image.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "npa.leisaac.health.v1"
TASK = "LeIsaac-SO101-PickOrange-v0"
TELEOP_DEVICE = "keyboard"
SOURCE_COMMIT = "1651c321e9b0c1bb54233211fc7b3cd70d8373d5"
SOURCE_VERSION = "0.4.0"
ISAAC_SIM_VERSION = "5.1.0.0"
ISAAC_LAB_VERSION = "2.3.2.post1"
SIGNAL_PORT = 49100
MEDIA_PORT = 47998
SERVICE_PORT = 8080

ASSET_RELEASE = "v0.1.0"
ROBOT_URL = "https://github.com/LightwheelAI/leisaac/releases/download/v0.1.0/so101_follower.usd"
ROBOT_SHA256 = "64a877c3b82cdc4a48ab8a1f321a2dd3ef7c55d4b10bce222b58c530d978ae58"
KITCHEN_URL = "https://github.com/LightwheelAI/leisaac/releases/download/v0.1.0/kitchen_with_orange.zip"
KITCHEN_SHA256 = "d314c54b63a17e91402bfaddf26e21ff614adf2430fa092b78897f15b8adea34"
CLIENT_URL = (
    "https://edge.urm.nvidia.com/artifactory/api/npm/omniverse-client-npm/"
    "@nvidia/omniverse-webrtc-streaming-library/-/"
    "@nvidia/omniverse-webrtc-streaming-library-5.6.0.tgz"
)
CLIENT_SHA512 = "37bd827a8194bfec2ccfbc656d10e42e83deebd682ac134095b2a8126901faa0966773752dd017353a1a5f7d1bc0b53be668d474ad5a14fd016c01df649f85dd"
CLIENT_SOURCE_JS_SHA256 = (
    "93cf2b328bcaaf9cf5a864c5b51f62e1bafcc533da9432ccc85633892f79ed86"
)
CLIENT_JS_SHA256 = "e9ac6563db79d3aea8afe94c4f60e50571abc01e3470d9bafb4e2f8b54cbd2a5"
UPSTREAM_OBSERVABILITY_PATCH_SHA256 = (
    "14dbbdd616d33bcc63d8e6476cb37e760dd0ed6db1dae4c4f87613b6847c2d9f"
)
CLIENT_WSS_PATCH_OLD = (
    b"M=Yc(B)?D.AppLevelProtocol.HTTP:D.AppLevelProtocol.HTTPS;"
)
CLIENT_WSS_PATCH_NEW = (
    b"M=de===443?D.AppLevelProtocol.HTTPS:Yc(B)?"
    b"D.AppLevelProtocol.HTTP:D.AppLevelProtocol.HTTPS;"
)

CACHE_ROOT = Path(os.environ.get("NPA_LEISAAC_CACHE_DIR", "/opt/leisaac-cache"))
ASSETS_ROOT = CACHE_ROOT / "assets" / ASSET_RELEASE
CLIENT_ROOT = CACHE_ROOT / "client" / "5.6.0"
PROVENANCE_PATH = CACHE_ROOT / "provenance.json"
READY_PATH = Path("/tmp/npa-leisaac-ready")
INPUT_COUNTER_PATH = Path("/tmp/npa-leisaac-input-events")
STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "state": "starting",
    "detail": "staging runtime",
    "webrtc_ready": False,
    "pid": 0,
    "gpu": "",
    "started_at": "",
}
CHILD: subprocess.Popen[str] | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require_operator_eula() -> None:
    missing = [
        name
        for name in ("OMNI_KIT_ACCEPT_EULA", "ISAACSIM_ACCEPT_EULA")
        if os.environ.get(name) != "YES"
    ]
    if missing:
        print(
            "LeIsaac refuses to start until the operator sets "
            + " and ".join(f"{name}=YES" for name in missing),
            file=sys.stderr,
        )
        raise SystemExit(78)


def hash_file(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(
    url: str, destination: Path, expected: str, algorithm: str = "sha256"
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and hash_file(destination, algorithm) == expected:
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "npa-leisaac/0.4.0"})
    with urllib.request.urlopen(request) as response, temporary.open("wb") as output:  # noqa: S310 - fixed URLs
        shutil.copyfileobj(response, output)
    observed = hash_file(temporary, algorithm)
    if observed != expected:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"hash mismatch for {url}: expected {expected}, got {observed}"
        )
    temporary.replace(destination)


def safe_extract_zip(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"unsafe asset archive member: {member.filename}")
        bundle.extractall(destination)


def safe_extract_client(archive: Path, destination: Path) -> None:
    wanted = {
        "package/dist/omniverse-webrtc-streaming-library.umd.cjs": "index.js",
        "package/LICENSE.txt": "LICENSE.txt",
    }
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = {member.name: member for member in bundle.getmembers()}
        for source, target in wanted.items():
            member = members.get(source)
            if member is None or not member.isfile():
                raise RuntimeError(f"NVIDIA client archive is missing {source}")
            handle = bundle.extractfile(member)
            if handle is None:
                raise RuntimeError(f"could not read {source}")
            with (destination / target).open("wb") as output:
                shutil.copyfileobj(handle, output)

    client_js = destination / "index.js"
    if hash_file(client_js) != CLIENT_SOURCE_JS_SHA256:
        raise RuntimeError("NVIDIA streaming client source hash mismatch")
    source = client_js.read_bytes()
    if source.count(CLIENT_WSS_PATCH_OLD) != 1:
        raise RuntimeError("NVIDIA streaming client WSS patch anchor mismatch")
    client_js.write_bytes(source.replace(CLIENT_WSS_PATCH_OLD, CLIENT_WSS_PATCH_NEW))


def stage_runtime() -> None:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    downloads = CACHE_ROOT / "downloads"
    robot = downloads / "so101_follower.usd"
    kitchen = downloads / "kitchen_with_orange.zip"
    client = downloads / "omniverse-webrtc-streaming-library-5.6.0.tgz"
    download_verified(ROBOT_URL, robot, ROBOT_SHA256)
    download_verified(KITCHEN_URL, kitchen, KITCHEN_SHA256)
    download_verified(CLIENT_URL, client, CLIENT_SHA512, "sha512")

    robot_target = ASSETS_ROOT / "robots" / "so101_follower.usd"
    if not robot_target.is_file() or hash_file(robot_target) != ROBOT_SHA256:
        robot_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(robot, robot_target)
    scene = ASSETS_ROOT / "scenes" / "kitchen_with_orange" / "scene.usd"
    if not scene.is_file():
        scenes = ASSETS_ROOT / "scenes"
        scenes.mkdir(parents=True, exist_ok=True)
        safe_extract_zip(kitchen, scenes)
    if not scene.is_file():
        raise RuntimeError(f"asset archive did not produce {scene}")

    client_js = CLIENT_ROOT / "index.js"
    if not client_js.is_file() or hash_file(client_js) != CLIENT_JS_SHA256:
        shutil.rmtree(CLIENT_ROOT, ignore_errors=True)
        safe_extract_client(client, CLIENT_ROOT)
    if hash_file(client_js) != CLIENT_JS_SHA256:
        raise RuntimeError(
            "NVIDIA streaming client JavaScript hash mismatch after extraction"
        )

    provenance = {
        "schema": "npa.leisaac.provenance.v1",
        "staged_at": utc_now(),
        "leisaac": {
            "repository": "https://github.com/LightwheelAI/leisaac",
            "version": SOURCE_VERSION,
            "commit": SOURCE_COMMIT,
            "license": "Apache-2.0",
            "npa_observability_patch": {
                "path": "upstream-observability.patch",
                "sha256": UPSTREAM_OBSERVABILITY_PATCH_SHA256,
            },
        },
        "assets": [
            {"url": ROBOT_URL, "sha256": ROBOT_SHA256, "bytes": robot.stat().st_size},
            {
                "url": KITCHEN_URL,
                "sha256": KITCHEN_SHA256,
                "bytes": kitchen.stat().st_size,
            },
        ],
        "browser_client": {
            "url": CLIENT_URL,
            "version": "5.6.0",
            "sha512": CLIENT_SHA512,
            "source_index_js_sha256": CLIENT_SOURCE_JS_SHA256,
            "index_js_sha256": CLIENT_JS_SHA256,
            "transport_patch": (
                "force HTTPS signaling for numeric hosts when signalingPort=443"
            ),
            "license": "NVIDIA proprietary; operator-fetched at runtime",
        },
    }
    PROVENANCE_PATH.write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )


def tcp_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def detect_gpu() -> str:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=False,
        )
        return (result.stdout.splitlines() or [""])[0].strip()
    except OSError:
        return ""


def update_state(**values: Any) -> None:
    with STATE_LOCK:
        STATE.update(values)


def run_simulation() -> None:
    global CHILD
    try:
        update_state(detail="fetching operator-licensed Isaac runtime")
        subprocess.run(["/opt/npa/bin/isaac-bootstrap", "ensure"], check=True)
        update_state(detail="starting LeIsaac PickOrange")
        media_host = os.environ.get("NPA_LEISAAC_MEDIA_HOST", "").strip()
        if not media_host:
            raise RuntimeError("NPA_LEISAAC_MEDIA_HOST is required")
        command = [
            "/isaac-sim/python.sh",
            "/opt/leisaac/scripts/environments/teleoperation/teleop_se3_agent.py",
            f"--task={TASK}",
            f"--teleop_device={TELEOP_DEVICE}",
            "--num_envs=1",
            # One interactive environment does not benefit from GPU PhysX. CPU
            # physics avoids the Isaac camera/DirectGpu interoperability fault
            # on sm_120 while RTX rendering and WebRTC remain on the RT GPU.
            "--device=cpu",
            "--enable_cameras",
            "--kit_args="
            + " ".join(
                [
                    "--no-window",
                    "--enable omni.kit.livestream.webrtc",
                    f"--/app/livestream/publicEndpointAddress={media_host}",
                    f"--/app/livestream/publicEndpointPort={MEDIA_PORT}",
                    f"--/app/livestream/fixedHostPort={MEDIA_PORT}",
                    f"--/app/livestream/minHostPort={MEDIA_PORT}",
                    f"--/app/livestream/maxHostPort={MEDIA_PORT}",
                    f"--/app/livestream/port={SIGNAL_PORT}",
                ]
            ),
        ]
        environment = os.environ.copy()
        environment["LEISAAC_ASSETS_ROOT"] = str(ASSETS_ROOT)
        environment["NPA_LEISAAC_READY_PATH"] = str(READY_PATH)
        environment["NPA_LEISAAC_INPUT_COUNTER"] = str(INPUT_COUNTER_PATH)
        READY_PATH.unlink(missing_ok=True)
        INPUT_COUNTER_PATH.write_text("0\n", encoding="utf-8")
        CHILD = subprocess.Popen(
            command, cwd="/opt/leisaac", env=environment, text=True
        )
        update_state(pid=CHILD.pid, gpu=detect_gpu(), started_at=utc_now())
        while CHILD.poll() is None:
            if tcp_ready(SIGNAL_PORT) and READY_PATH.is_file():
                update_state(state="ready", detail="live", webrtc_ready=True)
                break
            time.sleep(1)
        while CHILD.poll() is None:
            time.sleep(2)
        raise RuntimeError(f"LeIsaac exited with status {CHILD.returncode}")
    except Exception as exc:
        update_state(state="failed", detail=str(exc), webrtc_ready=False)


def health_document() -> dict[str, Any]:
    with STATE_LOCK:
        state = dict(STATE)
    try:
        input_events = max(
            0, int(INPUT_COUNTER_PATH.read_text(encoding="utf-8").strip() or "0")
        )
    except (OSError, ValueError):
        input_events = 0
    return {
        "schema": SCHEMA,
        "run_id": os.environ.get("NPA_LEISAAC_RUN_ID", ""),
        "task": TASK,
        "teleop_device": TELEOP_DEVICE,
        "source_commit": SOURCE_COMMIT,
        "source_version": SOURCE_VERSION,
        "isaac_sim_version": ISAAC_SIM_VERSION,
        "isaac_lab_version": ISAAC_LAB_VERSION,
        "session_nonce": os.environ.get("NPA_LEISAAC_SESSION_NONCE", ""),
        "signal_port": SIGNAL_PORT,
        "media_port": MEDIA_PORT,
        "input_events": input_events,
        "physics_device": "cpu",
        "render_device": "cuda",
        **state,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "npa-leisaac/0.4.0"

    def send_bytes(self, status: int, content_type: str, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.send_bytes(200, "application/json", b'{"ok":true}\n')
            return
        if path == "/status":
            document = health_document()
            status = 200 if document["state"] == "ready" else 503
            self.send_bytes(
                status, "application/json", (json.dumps(document) + "\n").encode()
            )
            return
        if path == "/client/index.js":
            self.send_bytes(
                200, "text/javascript", (CLIENT_ROOT / "index.js").read_bytes()
            )
            return
        if path == "/client/LICENSE.txt":
            self.send_bytes(
                200, "text/plain", (CLIENT_ROOT / "LICENSE.txt").read_bytes()
            )
            return
        if path == "/provenance":
            self.send_bytes(200, "application/json", PROVENANCE_PATH.read_bytes())
            return
        self.send_bytes(404, "application/json", b'{"detail":"not found"}\n')

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("leisaac-http " + (format % args) + "\n")


def stop_child(*_args: Any) -> None:
    if CHILD is not None and CHILD.poll() is None:
        CHILD.terminate()


def main() -> int:
    require_operator_eula()
    stage_runtime()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop_child)
    worker = threading.Thread(
        target=run_simulation, name="leisaac-simulation", daemon=True
    )
    worker.start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", SERVICE_PORT), Handler)
    try:
        server.serve_forever()
    finally:
        stop_child()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
