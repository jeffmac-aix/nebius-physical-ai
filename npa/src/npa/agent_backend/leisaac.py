"""Pure helpers for the agent's capability-gated LeIsaac teleoperation tab.

The live session manifest is an artifact emitted by ``npa workbench leisaac``.
It is intentionally treated as untrusted input here: TURN must be public,
direct TCP endpoints must be public, relay TCP endpoints must be exact
loopback addresses, ports are fixed to the Isaac Sim 5.1 WebRTC contract, and
the browser sees only the private media peer beside the GPU-local TURN
allocation plus same-origin, authenticated agent routes. Agent-relayed sessions
return one derived, ephemeral TURN
credential from the authenticated no-store status route; the relay nonce and
agent credentials are never returned.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

try:  # Shipped agent modules use the top-level package name.
    from agent_backend.leisaac_registry import (  # type: ignore[import-not-found]
        DEFAULT_ENVIRONMENT_ID,
        REGISTRY_FINGERPRINT,
        registry_payload,
        validate_environment_id,
        validate_environment_index,
        validate_seed,
        validate_task,
    )
except ImportError:  # Repository package imports use the npa namespace.
    from npa.agent_backend.leisaac_registry import (
        DEFAULT_ENVIRONMENT_ID,
        REGISTRY_FINGERPRINT,
        registry_payload,
        validate_environment_id,
        validate_environment_index,
        validate_seed,
        validate_task,
    )

LEISAAC_SESSION_SCHEMA = "npa.leisaac.session.v2"
LEISAAC_LEGACY_SESSION_SCHEMA = "npa.leisaac.session.v1"
LEISAAC_HEALTH_SCHEMA = "npa.leisaac.health.v2"
LEISAAC_LEGACY_HEALTH_SCHEMA = "npa.leisaac.health.v1"
LEISAAC_MANIFEST_NAME = "leisaac-session.json"
LEISAAC_SIGNAL_PORT = 49100
LEISAAC_MEDIA_PORT = 47998
LEISAAC_SERVICE_PORT = 8080
LEISAAC_RELAY_SERVICE_PORT = 48080
LEISAAC_TURN_PORT = 3478
LEISAAC_TURN_RELAY_PORT = 47999
LEISAAC_TURN_RELAY_MAX_PORT = 48015
LEISAAC_TRANSPORT_LOAD_BALANCER = "public-load-balancer"
LEISAAC_TRANSPORT_AGENT_RELAY = "agent-relay"
LEISAAC_TASK = "LeIsaac-SO101-PickOrange-v0"
LEISAAC_TELEOP_DEVICE = "keyboard"
LEISAAC_CLIENT_VERSION = "5.6.0"
LEISAAC_CLIENT_JS_SHA256 = (
    "e9ac6563db79d3aea8afe94c4f60e50571abc01e3470d9bafb4e2f8b54cbd2a5"
)
LEISAAC_CLIENT_MODULE_PATH = "/api/leisaac/client/index.js"
LEISAAC_SIGNAL_PATH = "/api/leisaac/signal"
LEISAAC_FRAME_PATH = "/api/leisaac/frame.jpg"
LEISAAC_INPUT_PATH = "/api/leisaac/input"
LEISAAC_RECORDER_PATH = "/api/leisaac/recorder"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_leisaac_manifest_key(key: str) -> bool:
    """Return whether an artifact key is the canonical session manifest."""

    value = str(key or "").strip().replace("\\", "/")
    return value.endswith(f"/reports/{LEISAAC_MANIFEST_NAME}")


def load_manifest_artifact(
    run_id: str,
    *,
    validate_run_id: Callable[[str], str],
    s3_client: Callable[[], tuple[Any, dict]],
    s3_buckets: Callable[[Any, dict], list[str]],
    find_artifacts: Callable[..., tuple[str, list[Any]]],
) -> dict | None:
    """Load one bounded canonical manifest for a validated run from S3."""

    normalized_run = validate_run_id(run_id)
    s3, settings = s3_client()
    bucket, artifacts = find_artifacts(
        s3_buckets(s3, settings),
        base_prefix=settings.get("prefix", ""),
        run_id=normalized_run,
        s3=s3,
    )
    matches = [
        item for item in artifacts if is_leisaac_manifest_key(str(item.key or ""))
    ]
    preferred = [
        item
        for item in matches
        if f"/{LEISAAC_MANIFEST_NAME}/" not in str(item.key or "").replace("\\", "/")
    ]
    selected = preferred if preferred else matches
    if not bucket or len(selected) != 1:
        return None
    response = s3.get_object(Bucket=bucket, Key=str(selected[0].key or ""))
    body = response["Body"].read(131073)
    if len(body) > 131072:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def selected_run_id(state: dict | None, requested: str = "") -> str:
    """Resolve an explicit run or the agent's registered live LeIsaac run."""

    explicit = str(requested or "").strip()
    if explicit:
        return explicit if _RUN_ID_RE.fullmatch(explicit) else ""
    data = state if isinstance(state, dict) else {}
    leisaac = data.get("leisaac") if isinstance(data.get("leisaac"), dict) else {}
    sim_viz = data.get("sim_viz") if isinstance(data.get("sim_viz"), dict) else {}
    candidate = str(
        leisaac.get("run_id")
        or sim_viz.get("active_run_id")
        or sim_viz.get("run_id")
        or data.get("active_run_id")
        or ""
    ).strip()
    return candidate if _RUN_ID_RE.fullmatch(candidate) else ""


def _public_ip(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return ""
    if not address.is_global:
        return ""
    return address.compressed


def _private_ip(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return ""
    if (
        address.version != 4
        or not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        return ""
    return address.compressed


def _service_url(value: Any, signal_host: str, transport: str) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        return ""
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return ""
    raw_host = str(parsed.hostname or "")
    try:
        port = parsed.port
    except ValueError:
        return ""
    if transport == LEISAAC_TRANSPORT_AGENT_RELAY:
        if raw_host != "127.0.0.1" or port != LEISAAC_RELAY_SERVICE_PORT:
            return ""
        return f"http://127.0.0.1:{LEISAAC_RELAY_SERVICE_PORT}"
    host = _public_ip(raw_host)
    if not host or host != signal_host or port != LEISAAC_SERVICE_PORT:
        return ""
    return f"http://{host}:{LEISAAC_SERVICE_PORT}"


def _parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_manifest(
    payload: dict | None,
    *,
    expected_run_id: str = "",
    now: datetime | None = None,
) -> tuple[dict | None, str]:
    """Validate a live-session artifact and return its internal normalized form."""

    data = payload if isinstance(payload, dict) else {}
    schema = str(data.get("schema") or "")
    if schema not in {LEISAAC_SESSION_SCHEMA, LEISAAC_LEGACY_SESSION_SCHEMA}:
        return None, "selected run has no LeIsaac session capability"
    run_id = str(data.get("run_id") or "").strip()
    if not _RUN_ID_RE.fullmatch(run_id):
        return None, "LeIsaac session has an invalid run id"
    if expected_run_id and run_id != expected_run_id:
        return None, "LeIsaac session does not belong to the selected run"
    if str(data.get("provider") or "") != "nebius-kubernetes":
        return None, "LeIsaac session provider is unsupported"
    task = str(data.get("task") or "")
    try:
        task = validate_task(task)
    except ValueError:
        return None, "LeIsaac session does not expose the supported task"
    if str(data.get("teleop_device") or "") != LEISAAC_TELEOP_DEVICE:
        return None, "LeIsaac session is not keyboard-teleoperation capable"

    transport = str(data.get("transport") or LEISAAC_TRANSPORT_LOAD_BALANCER)
    if transport not in (
        LEISAAC_TRANSPORT_LOAD_BALANCER,
        LEISAAC_TRANSPORT_AGENT_RELAY,
    ):
        return None, "LeIsaac session transport is unsupported"
    raw_signal_host = str(data.get("signal_host") or "").strip()
    signal_host = (
        "127.0.0.1"
        if transport == LEISAAC_TRANSPORT_AGENT_RELAY and raw_signal_host == "127.0.0.1"
        else _public_ip(raw_signal_host)
    )
    media_host = _public_ip(data.get("media_host"))
    raw_media_server = data.get("media_server")
    media_server = (
        _private_ip(raw_media_server)
        if transport == LEISAAC_TRANSPORT_AGENT_RELAY
        else _public_ip(raw_media_server or media_host)
    )
    if (
        not signal_host
        or not media_host
        or not media_server
        or (transport == LEISAAC_TRANSPORT_LOAD_BALANCER and media_server != media_host)
    ):
        return None, "LeIsaac session endpoints violate the fixed network contract"
    if _integer(data.get("signal_port")) != LEISAAC_SIGNAL_PORT:
        return None, "LeIsaac session has an unsupported signaling port"
    if _integer(data.get("media_port")) != LEISAAC_MEDIA_PORT:
        return None, "LeIsaac session has an unsupported media port"
    if transport == LEISAAC_TRANSPORT_AGENT_RELAY and (
        _integer(data.get("turn_port")) != LEISAAC_TURN_PORT
        or _integer(data.get("turn_relay_port")) != LEISAAC_TURN_RELAY_PORT
        or _integer(data.get("turn_relay_max_port")) != LEISAAC_TURN_RELAY_MAX_PORT
    ):
        return None, "LeIsaac session has an unsupported TURN contract"
    service_url = _service_url(data.get("service_url"), signal_host, transport)
    if not service_url:
        return None, "LeIsaac session service endpoint is invalid"

    nonce = str(data.get("session_nonce") or "").strip()
    if not re.fullmatch(r"[A-Fa-f0-9]{32,128}", nonce):
        return None, "LeIsaac session attestation is invalid"
    expected_attestation = hashlib.sha256(
        f"npa-leisaac-session:{nonce.lower()}".encode()
    ).hexdigest()
    if schema == LEISAAC_SESSION_SCHEMA:
        if str(data.get("session_attestation") or "") != expected_attestation:
            return None, "LeIsaac session public attestation is invalid"
        if str(data.get("task_registry_fingerprint") or "") != REGISTRY_FINGERPRINT:
            return None, "LeIsaac session task registry is stale"
        environment = data.get("environment")
        dataset = data.get("dataset")
        if not isinstance(environment, dict) or not isinstance(dataset, dict):
            return None, "LeIsaac session environment or dataset contract is missing"
        try:
            environment_id = validate_environment_id(environment.get("id"))
            environment_index = validate_environment_index(environment.get("index"))
            seed = validate_seed(environment.get("seed"))
        except (TypeError, ValueError):
            return None, "LeIsaac session environment is invalid"
        if _integer(environment.get("num_envs")) != 1:
            return None, "LeIsaac session parallel environment routing is unsupported"
        dataset_uri = str(dataset.get("output_path") or "").rstrip("/")
        parsed_dataset = urlparse(dataset_uri)
        if (
            parsed_dataset.scheme != "s3"
            or not parsed_dataset.netloc
            or not parsed_dataset.path.strip("/")
        ):
            return None, "LeIsaac session dataset destination is invalid"
    else:
        environment_id = DEFAULT_ENVIRONMENT_ID
        environment_index = 0
        seed = _integer(data.get("seed")) or 42
        dataset_uri = ""
    raw_expires_at = str(data.get("expires_at") or "").strip()
    expires_at = _parse_utc(raw_expires_at) if raw_expires_at else None
    if raw_expires_at and expires_at is None:
        return None, "LeIsaac session expiry is invalid"
    current = now or datetime.now(timezone.utc)
    if expires_at is not None and expires_at <= current.astimezone(timezone.utc):
        return None, "LeIsaac session has expired"

    source_commit = str(data.get("source_commit") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        return None, "LeIsaac source commit is invalid"
    image = str(data.get("image") or "").strip()
    if not image or "@sha256:" not in image:
        return None, "LeIsaac session image is not digest pinned"

    return {
        "schema": schema,
        "run_id": run_id,
        "provider": "nebius-kubernetes",
        "transport": transport,
        "task": task,
        "task_registry_fingerprint": (
            REGISTRY_FINGERPRINT if schema == LEISAAC_SESSION_SCHEMA else "legacy"
        ),
        "teleop_device": LEISAAC_TELEOP_DEVICE,
        "signal_host": signal_host,
        "signal_port": LEISAAC_SIGNAL_PORT,
        "media_host": media_host,
        "media_server": media_server,
        "media_port": LEISAAC_MEDIA_PORT,
        "turn_port": _integer(data.get("turn_port")) or 0,
        "turn_relay_port": _integer(data.get("turn_relay_port")) or 0,
        "turn_relay_max_port": _integer(data.get("turn_relay_max_port")) or 0,
        "service_url": service_url,
        "session_nonce": nonce.lower(),
        "session_attestation": expected_attestation,
        "environment_id": environment_id,
        "environment_index": environment_index,
        "seed": seed,
        "num_envs": 1,
        "dataset_uri": dataset_uri,
        "expires_at": (
            expires_at.isoformat().replace("+00:00", "Z") if expires_at else ""
        ),
        "source_commit": source_commit,
        "source_version": str(data.get("source_version") or "").strip(),
        "isaac_sim_version": str(data.get("isaac_sim_version") or "").strip(),
        "isaac_lab_version": str(data.get("isaac_lab_version") or "").strip(),
        "image": image,
        "gpu": str(data.get("gpu") or "").strip(),
        "created_at": str(data.get("created_at") or "").strip(),
    }, ""


def validate_health(manifest: dict, payload: dict | None) -> tuple[dict | None, str]:
    """Validate the service's live attestation against the S3 capability artifact."""

    data = payload if isinstance(payload, dict) else {}
    health_schema = str(data.get("schema") or "")
    if health_schema not in {LEISAAC_HEALTH_SCHEMA, LEISAAC_LEGACY_HEALTH_SCHEMA}:
        return None, "LeIsaac service returned an invalid health document"
    attestation_keys = ["run_id", "task", "source_commit"]
    if health_schema == LEISAAC_HEALTH_SCHEMA:
        attestation_keys.extend(
            [
                "session_attestation",
                "task_registry_fingerprint",
                "environment_id",
                "environment_index",
                "seed",
            ]
        )
    else:
        attestation_keys.append("session_nonce")
    for key in attestation_keys:
        if str(data.get(key) or "") != str(manifest.get(key) or ""):
            return None, f"LeIsaac service attestation mismatch: {key}"
    stream_ready = bool(data.get("stream_ready", data.get("webrtc_ready")))
    stream_transport = str(data.get("stream_transport") or "webrtc")
    if stream_transport not in {"webrtc", "jpeg-poll"}:
        return None, "LeIsaac service returned an unsupported stream transport"
    if str(data.get("state") or "") != "ready" or not stream_ready:
        detail = str(data.get("detail") or data.get("state") or "starting")
        return None, f"LeIsaac service is not ready: {detail}"
    if _integer(data.get("signal_port")) != LEISAAC_SIGNAL_PORT:
        return None, "LeIsaac service signaling port mismatch"
    recorder = data.get("recorder") if isinstance(data.get("recorder"), dict) else {}
    safe_recorder = {
        key: recorder.get(key)
        for key in (
            "state",
            "dataset_uri",
            "dataset_version_uri",
            "last_episode_commit_uri",
            "task",
            "environment_id",
            "environment_index",
            "seed",
            "active_episode",
            "last_episode_index",
            "frame_count",
            "completed_episode_count",
            "pending_outcome",
            "last_outcome",
            "last_upload_status",
            "last_error",
            "pending_command_id",
            "last_command_id",
            "last_command",
            "command_revision",
        )
    }
    if health_schema == LEISAAC_HEALTH_SCHEMA and (
        safe_recorder.get("task") != manifest.get("task")
        or safe_recorder.get("environment_id") != manifest.get("environment_id")
        or _integer(safe_recorder.get("environment_index"))
        != manifest.get("environment_index")
        or str(safe_recorder.get("dataset_uri") or "").rstrip("/")
        != manifest.get("dataset_uri")
    ):
        return None, "LeIsaac recorder identity does not match the selected manifest"
    return {
        "state": "ready",
        "webrtc_ready": True,
        "stream_ready": True,
        "stream_transport": stream_transport,
        "pid": _integer(data.get("pid")) or 0,
        "started_at": str(data.get("started_at") or ""),
        "gpu": str(data.get("gpu") or manifest.get("gpu") or ""),
        "input_events": _integer(data.get("input_events")) or 0,
        "applied_inputs": _integer(data.get("applied_inputs")) or 0,
        "frame_bytes": _integer(data.get("frame_bytes")) or 0,
        "frame_updated_at": str(data.get("frame_updated_at") or ""),
        "recorder": safe_recorder,
    }, ""


def status_payload(
    manifest: dict | None,
    health: dict | None = None,
    *,
    reason: str = "",
) -> dict:
    """Build the authenticated, no-store payload consumed by the agent UI."""

    if not manifest or not health:
        return {
            "available": False,
            "reason": reason or "No usable LeIsaac session is selected.",
        }
    run_id = str(manifest["run_id"])
    payload = {
        "available": True,
        "reason": "",
        "run_id": run_id,
        "transport": manifest["transport"],
        "task": manifest["task"],
        "task_registry": registry_payload(),
        "environment_id": manifest.get("environment_id", DEFAULT_ENVIRONMENT_ID),
        "environment_index": manifest.get("environment_index", 0),
        "seed": manifest.get("seed", 42),
        "num_envs": 1,
        "dataset_uri": manifest.get("dataset_uri", ""),
        "teleop_device": manifest["teleop_device"],
        "media_server": manifest["media_server"],
        "media_port": manifest["media_port"],
        "signaling_server": "same-origin",
        "signaling_port": 443,
        "signaling_path": LEISAAC_SIGNAL_PATH,
        "client_module_url": f"{LEISAAC_CLIENT_MODULE_PATH}?run_id={run_id}",
        "stream_transport": health.get("stream_transport", "webrtc"),
        "frame_url": f"{LEISAAC_FRAME_PATH}?run_id={run_id}",
        "input_url": f"{LEISAAC_INPUT_PATH}?run_id={run_id}",
        "recorder_url": f"{LEISAAC_RECORDER_PATH}?run_id={run_id}",
        "recorder": health.get("recorder", {}),
        "source_version": manifest.get("source_version", ""),
        "source_commit": manifest.get("source_commit", ""),
        "isaac_sim_version": manifest.get("isaac_sim_version", ""),
        "isaac_lab_version": manifest.get("isaac_lab_version", ""),
        "image": manifest.get("image", ""),
        "gpu": health.get("gpu") or manifest.get("gpu", ""),
        "started_at": health.get("started_at", ""),
        "input_events": health.get("input_events", 0),
        "applied_inputs": health.get("applied_inputs", 0),
        "frame_bytes": health.get("frame_bytes", 0),
        "frame_updated_at": health.get("frame_updated_at", ""),
        "controls": {
            "translate": "W/S forward/back · A/D left/right · Q/E up/down",
            "rotate": "J/L yaw · K/I pitch",
            "gripper": "U/O open/close",
            "episode": "Use explicit start, outcome, and finalize controls",
        },
    }
    if manifest.get("transport") == LEISAAC_TRANSPORT_AGENT_RELAY:
        nonce = str(manifest.get("session_nonce") or "")
        credential = hashlib.sha256(
            f"npa-leisaac-turn:{nonce}".encode("utf-8")
        ).hexdigest()
        payload["ice_servers"] = [
            {
                "urls": [
                    f"turn:{manifest['media_host']}:{LEISAAC_TURN_PORT}?transport=udp"
                ],
                "username": run_id,
                "credential": credential,
            }
        ]
        payload["ice_transport_policy"] = "relay"
    return payload
