#!/usr/bin/env python3
"""Start the real LeIsaac teleoperator and expose its browser-streaming assets.

Isaac Sim, task assets, and NVIDIA's WebRTC browser client are fetched only at
runtime after the operator has explicitly accepted the two NVIDIA EULAs.  None
of those bytes are part of the distributable image.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import signal
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

try:
    from leisaac_registry import (
        REGISTRY_FINGERPRINT,
        RUNTIME_ASSETS,
        registry_payload,
        validate_environment_id,
        validate_environment_index,
        validate_num_envs,
        validate_seed,
        validate_task,
    )
except ImportError:  # Repository unit tests import the script directly.
    from npa.agent_backend.leisaac_registry import (
        REGISTRY_FINGERPRINT,
        RUNTIME_ASSETS,
        registry_payload,
        validate_environment_id,
        validate_environment_index,
        validate_num_envs,
        validate_seed,
        validate_task,
    )

SCHEMA = "npa.leisaac.health.v2"
TASK = os.environ.get("NPA_LEISAAC_TASK", "LeIsaac-SO101-PickOrange-v0")
ENVIRONMENT_ID = os.environ.get("NPA_LEISAAC_ENVIRONMENT_ID", "operator-0")
ENVIRONMENT_INDEX = int(os.environ.get("NPA_LEISAAC_ENVIRONMENT_INDEX", "0"))
TELEOP_DEVICE = "keyboard"
TELEOP_SEED = int(os.environ.get("NPA_LEISAAC_SEED", "42"))
NUM_ENVS = int(os.environ.get("NPA_LEISAAC_NUM_ENVS", "1"))
SOURCE_COMMIT = "1651c321e9b0c1bb54233211fc7b3cd70d8373d5"
SOURCE_VERSION = "0.4.0"
ISAAC_SIM_VERSION = "5.1.0.0"
ISAAC_LAB_VERSION = "2.3.2.post1"
SIGNAL_PORT = 49100
MEDIA_PORT = 47998
SERVICE_PORT = 8080

_ASSET_BY_ID = {item["id"]: item for item in RUNTIME_ASSETS}
ROBOT_URL = _ASSET_BY_ID["so101_follower"]["url"]
ROBOT_SHA256 = _ASSET_BY_ID["so101_follower"]["sha256"]
KITCHEN_URL = _ASSET_BY_ID["kitchen_with_orange"]["url"]
KITCHEN_SHA256 = _ASSET_BY_ID["kitchen_with_orange"]["sha256"]
TABLE_URL = _ASSET_BY_ID["table_with_cube"]["url"]
TABLE_SHA256 = _ASSET_BY_ID["table_with_cube"]["sha256"]
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
    "9f8d4c8a5a5054b47944bdbd44f542f3333413791ad5a53cb40bb363bb2d338a"
)
CLIENT_WSS_PATCH_OLD = b"M=Yc(B)?D.AppLevelProtocol.HTTP:D.AppLevelProtocol.HTTPS;"
CLIENT_WSS_PATCH_NEW = (
    b"M=de===443?D.AppLevelProtocol.HTTPS:Yc(B)?"
    b"D.AppLevelProtocol.HTTP:D.AppLevelProtocol.HTTPS;"
)

CACHE_ROOT = Path(os.environ.get("NPA_LEISAAC_CACHE_DIR", "/opt/leisaac-cache"))
ASSETS_ROOT = CACHE_ROOT / "assets" / "runtime"
CLIENT_ROOT = CACHE_ROOT / "client" / "5.6.0"
PROVENANCE_PATH = CACHE_ROOT / "provenance.json"
READY_PATH = Path("/tmp/npa-leisaac-ready")
INPUT_COUNTER_PATH = Path("/tmp/npa-leisaac-input-events")
APPLIED_COUNTER_PATH = Path("/tmp/npa-leisaac-applied-inputs")
INPUT_QUEUE_PATH = Path("/tmp/npa-leisaac-input-queue.jsonl")
FRAME_PATH = Path("/tmp/npa-leisaac-frame.jpg")
RECORDER_ROOT = Path("/tmp/npa-leisaac-recorder")
RECORDER_STATUS_PATH = RECORDER_ROOT / "status.json"
RECORDER_CONTROL_PATH = RECORDER_ROOT / "control.jsonl"
RECORDER_PENDING_PATH = RECORDER_ROOT / "pending-command.json"
STATE_LOCK = threading.Lock()
INPUT_LOCK = threading.Lock()
RECORDER_COMMAND_LOCK = threading.Lock()
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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def enqueue_recorder_command(
    command: str, request_id: str
) -> tuple[int, dict[str, Any]]:
    """Validate and reserve exactly one asynchronous recorder transition."""

    if command not in {"start", "mark-success", "mark-failure", "finalize"}:
        return 400, {"detail": "invalid recorder command"}
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
        return 400, {"detail": "invalid recorder request ID"}
    with RECORDER_COMMAND_LOCK:
        try:
            status = json.loads(RECORDER_STATUS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 503, {"detail": "recorder unavailable"}
        if not isinstance(status, dict):
            return 503, {"detail": "recorder unavailable"}
        if status.get("last_command_id") == request_id:
            if status.get("last_command") != command:
                return 409, {"detail": "recorder request ID was reused"}
            RECORDER_PENDING_PATH.unlink(missing_ok=True)
            return 202, {
                "accepted": True,
                "duplicate": True,
                "processed": True,
                "request_id": request_id,
            }
        try:
            pending = json.loads(RECORDER_PENDING_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = None
        if isinstance(pending, dict):
            if (
                pending.get("request_id") == request_id
                and pending.get("command") == command
            ):
                return 202, {
                    "accepted": True,
                    "duplicate": True,
                    "processed": False,
                    "request_id": request_id,
                }
            return 409, {
                "detail": "another recorder transition is already in progress",
                "pending_command": str(pending.get("command") or ""),
            }
        state = str(status.get("state") or "")
        valid = {
            "start": state == "idle",
            "mark-success": state in {"recording", "outcome-pending"},
            "mark-failure": state in {"recording", "outcome-pending"},
            "finalize": state in {"outcome-pending", "upload-failed"},
        }
        if not valid[command]:
            return 409, {
                "detail": "invalid recorder transition",
                "state": state,
            }
        pending = {
            "schema": "npa.leisaac.recorder-command.v1",
            "request_id": request_id,
            "command": command,
            "state_before": state,
            "accepted_at": utc_now(),
        }
        try:
            _write_json_atomic(RECORDER_PENDING_PATH, pending)
            record = (
                json.dumps(
                    {"command": command, "request_id": request_id},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            with RECORDER_CONTROL_PATH.open("a", encoding="utf-8") as queue:
                queue.write(record)
                queue.flush()
                os.fsync(queue.fileno())
        except OSError:
            RECORDER_PENDING_PATH.unlink(missing_ok=True)
            return 503, {"detail": "recorder command queue is unavailable"}
        return 202, {
            "accepted": True,
            "duplicate": False,
            "processed": False,
            "request_id": request_id,
        }


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


def validate_runtime_configuration() -> None:
    try:
        validate_task(TASK)
        validate_environment_id(ENVIRONMENT_ID)
        validate_environment_index(ENVIRONMENT_INDEX)
        validate_seed(TELEOP_SEED)
        validate_num_envs(NUM_ENVS)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if os.environ.get("NPA_LEISAAC_REGISTRY_FINGERPRINT") != REGISTRY_FINGERPRINT:
        raise RuntimeError("task registry fingerprint mismatch")
    output = os.environ.get("NPA_LEISAAC_OUTPUT_PATH", "")
    if not output.startswith("s3://"):
        raise RuntimeError(
            "NPA_LEISAAC_OUTPUT_PATH must be an operator-owned S3 prefix"
        )


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
    table = downloads / "table_with_cube.zip"
    client = downloads / "omniverse-webrtc-streaming-library-5.6.0.tgz"
    download_verified(ROBOT_URL, robot, ROBOT_SHA256)
    download_verified(KITCHEN_URL, kitchen, KITCHEN_SHA256)
    download_verified(TABLE_URL, table, TABLE_SHA256)
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
    table_scene = ASSETS_ROOT / "scenes" / "table_with_cube" / "scene.usd"
    if not table_scene.is_file():
        scenes = ASSETS_ROOT / "scenes"
        scenes.mkdir(parents=True, exist_ok=True)
        safe_extract_zip(table, scenes)
    if not table_scene.is_file():
        raise RuntimeError(f"asset archive did not produce {table_scene}")

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
            {
                "url": TABLE_URL,
                "sha256": TABLE_SHA256,
                "bytes": table.stat().st_size,
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
        update_state(detail=f"starting {TASK}")
        media_host = os.environ.get("NPA_LEISAAC_MEDIA_HOST", "").strip()
        if not media_host:
            raise RuntimeError("NPA_LEISAAC_MEDIA_HOST is required")
        command = [
            "/isaac-sim/python.sh",
            "/opt/leisaac/scripts/environments/teleoperation/teleop_se3_agent.py",
            f"--task={TASK}",
            f"--teleop_device={TELEOP_DEVICE}",
            f"--num_envs={NUM_ENVS}",
            f"--seed={TELEOP_SEED}",
            # Isaac Sim 5.1 does not ship sm_120 PhysX kernels. Keep physics on
            # CPU for this single interactive environment; RTX rendering and
            # WebRTC encoding still run on the selected RT-core GPU.
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
                    # The pod requests one RTX GPU.  Keeping Kit's renderer in
                    # single-GPU mode avoids a CUDA-interoperability path that
                    # can connect WebRTC while emitting no encoded frames.
                    "--/renderer/multiGpu/enabled=False",
                ]
            ),
        ]
        environment = os.environ.copy()
        module_root = "/opt/npa/leisaac"
        inherited_pythonpath = environment.get("PYTHONPATH", "").strip()
        environment["PYTHONPATH"] = (
            f"{module_root}:{inherited_pythonpath}"
            if inherited_pythonpath
            else module_root
        )
        environment["LEISAAC_ASSETS_ROOT"] = str(ASSETS_ROOT)
        environment["NPA_LEISAAC_BROWSER_TELEOP"] = "1"
        environment["NPA_LEISAAC_READY_PATH"] = str(READY_PATH)
        environment["NPA_LEISAAC_INPUT_COUNTER"] = str(INPUT_COUNTER_PATH)
        environment["NPA_LEISAAC_APPLIED_COUNTER"] = str(APPLIED_COUNTER_PATH)
        environment["NPA_LEISAAC_INPUT_QUEUE"] = str(INPUT_QUEUE_PATH)
        environment["NPA_LEISAAC_FRAME_PATH"] = str(FRAME_PATH)
        environment["NPA_LEISAAC_RECORDER_ROOT"] = str(RECORDER_ROOT)
        READY_PATH.unlink(missing_ok=True)
        INPUT_COUNTER_PATH.write_text("0\n", encoding="utf-8")
        APPLIED_COUNTER_PATH.write_text("0\n", encoding="utf-8")
        INPUT_QUEUE_PATH.write_text("", encoding="utf-8")
        FRAME_PATH.unlink(missing_ok=True)
        shutil.rmtree(RECORDER_ROOT, ignore_errors=True)
        RECORDER_ROOT.mkdir(parents=True, exist_ok=True)
        CHILD = subprocess.Popen(
            command,
            cwd="/opt/leisaac",
            env=environment,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
        update_state(pid=CHILD.pid, gpu=detect_gpu(), started_at=utc_now())
        while CHILD.poll() is None:
            if (
                READY_PATH.is_file()
                and FRAME_PATH.is_file()
                and FRAME_PATH.stat().st_size > 0
            ):
                update_state(
                    state="ready",
                    detail="live",
                    webrtc_ready=True,
                    stream_ready=True,
                    stream_transport="jpeg-poll",
                )
                break
            update_state(detail="warming RTX renderer")
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
    try:
        applied_inputs = max(
            0, int(APPLIED_COUNTER_PATH.read_text(encoding="utf-8").strip() or "0")
        )
    except (OSError, ValueError):
        applied_inputs = 0
    try:
        frame_bytes = FRAME_PATH.stat().st_size
        frame_updated_at = (
            datetime.fromtimestamp(FRAME_PATH.stat().st_mtime, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except OSError:
        frame_bytes = 0
        frame_updated_at = ""
    try:
        recorder = json.loads(RECORDER_STATUS_PATH.read_text(encoding="utf-8"))
        if not isinstance(recorder, dict):
            recorder = {}
    except (OSError, ValueError):
        recorder = {
            "state": "starting",
            "dataset_uri": os.environ.get("NPA_LEISAAC_OUTPUT_PATH", ""),
            "task": TASK,
            "environment_id": ENVIRONMENT_ID,
            "environment_index": ENVIRONMENT_INDEX,
            "seed": TELEOP_SEED,
        }
    nonce = os.environ.get("NPA_LEISAAC_SESSION_NONCE", "")
    attestation = (
        hashlib.sha256(f"npa-leisaac-session:{nonce}".encode()).hexdigest()
        if len(nonce) == 64
        else ""
    )
    return {
        "schema": SCHEMA,
        "run_id": os.environ.get("NPA_LEISAAC_RUN_ID", ""),
        "task": TASK,
        "task_registry_fingerprint": REGISTRY_FINGERPRINT,
        "task_registry": registry_payload(),
        "teleop_device": TELEOP_DEVICE,
        "seed": TELEOP_SEED,
        "environment_id": ENVIRONMENT_ID,
        "environment_index": ENVIRONMENT_INDEX,
        "num_envs": NUM_ENVS,
        "source_commit": SOURCE_COMMIT,
        "source_version": SOURCE_VERSION,
        "isaac_sim_version": ISAAC_SIM_VERSION,
        "isaac_lab_version": ISAAC_LAB_VERSION,
        "session_attestation": attestation,
        "signal_port": SIGNAL_PORT,
        "media_port": MEDIA_PORT,
        "input_events": input_events,
        "applied_inputs": applied_inputs,
        "stream_ready": bool(frame_bytes),
        "stream_transport": "jpeg-poll",
        "frame_bytes": frame_bytes,
        "frame_updated_at": frame_updated_at,
        "physics_device": "cpu",
        "render_device": "cuda",
        "recorder": recorder,
        **state,
    }


def liveness_status() -> int:
    """Keep a live simulator process alive while readiness is still pending."""

    with STATE_LOCK:
        state = str(STATE.get("state") or "")
        pid = int(STATE.get("pid") or 0)
    child = CHILD
    if state == "failed" or (pid > 0 and (child is None or child.poll() is not None)):
        return 503
    return 200


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

    def authorized(self) -> bool:
        expected = os.environ.get("NPA_LEISAAC_SESSION_NONCE", "")
        supplied = self.headers.get("X-NPA-LeIsaac-Nonce", "")
        return bool(expected) and hmac.compare_digest(expected, supplied)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            status = liveness_status()
            body = b'{"ok":true}\n' if status == 200 else b'{"ok":false}\n'
            self.send_bytes(status, "application/json", body)
            return
        if path == "/status":
            document = health_document()
            status = 200 if document["state"] == "ready" else 503
            self.send_bytes(
                status, "application/json", (json.dumps(document) + "\n").encode()
            )
            return
        if path == "/frame.jpg":
            if not self.authorized():
                self.send_bytes(403, "application/json", b'{"detail":"forbidden"}\n')
                return
            try:
                content = FRAME_PATH.read_bytes()
            except OSError:
                content = b""
            if not content or len(content) > 4 * 1024 * 1024:
                self.send_bytes(
                    503, "application/json", b'{"detail":"frame unavailable"}\n'
                )
                return
            self.send_bytes(200, "image/jpeg", content)
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

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path not in {"/input", "/recorder/control"}:
            self.send_bytes(404, "application/json", b'{"detail":"not found"}\n')
            return
        if not self.authorized():
            self.send_bytes(403, "application/json", b'{"detail":"forbidden"}\n')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 1024:
            self.send_bytes(400, "application/json", b'{"detail":"invalid body"}\n')
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, ValueError):
            payload = None
        if path == "/recorder/control":
            command = str(payload.get("command") if isinstance(payload, dict) else "")
            request_id = str(
                payload.get("request_id") if isinstance(payload, dict) else ""
            ) or secrets.token_hex(16)
            status_code, response = enqueue_recorder_command(command, request_id)
            self.send_bytes(
                status_code,
                "application/json",
                (json.dumps(response, sort_keys=True) + "\n").encode(),
            )
            return
        key = str(payload.get("key") if isinstance(payload, dict) else "").upper()
        event = str(payload.get("event") if isinstance(payload, dict) else "")
        if key not in {
            "W",
            "S",
            "A",
            "D",
            "Q",
            "E",
            "J",
            "L",
            "I",
            "K",
            "U",
            "O",
        } or event not in {"press", "release"}:
            self.send_bytes(400, "application/json", b'{"detail":"invalid input"}\n')
            return
        with STATE_LOCK:
            ready = STATE.get("state") == "ready"
        if not ready:
            self.send_bytes(
                503, "application/json", b'{"detail":"simulator not ready"}\n'
            )
            return
        record = json.dumps({"key": key, "event": event}, separators=(",", ":")) + "\n"
        with INPUT_LOCK:
            with INPUT_QUEUE_PATH.open("a", encoding="utf-8") as queue:
                queue.write(record)
            if event == "press":
                try:
                    count = (
                        int(
                            INPUT_COUNTER_PATH.read_text(encoding="utf-8").strip()
                            or "0"
                        )
                        + 1
                    )
                except (OSError, ValueError):
                    count = 1
                temporary = INPUT_COUNTER_PATH.with_suffix(".tmp")
                temporary.write_text(f"{count}\n", encoding="utf-8")
                temporary.replace(INPUT_COUNTER_PATH)
        self.send_bytes(202, "application/json", b'{"accepted":true}\n')

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("leisaac-http " + (format % args) + "\n")


def stop_child(*_args: Any) -> None:
    if CHILD is not None and CHILD.poll() is None:
        CHILD.terminate()


def main() -> int:
    require_operator_eula()
    validate_runtime_configuration()
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
