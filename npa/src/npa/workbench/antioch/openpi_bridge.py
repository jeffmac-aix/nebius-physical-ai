"""Fail-closed OpenPI pi0.5-DROID bridge and two-GPU deployment contract.

The transport matches upstream OpenPI's ``WebsocketClientPolicy`` protocol:
MessagePack with its safe NumPy extension over a private WebSocket endpoint.
Isaac imports live in :mod:`npa.workbench.antioch.openpi_isaac`; this module is
therefore importable by the CLI, SDK, tests, and a CPU deployment renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import time
from typing import Callable, Mapping, Protocol

import msgpack
import numpy as np

ACTION_SHAPE = (15, 8)
IMAGE_SHAPE = (224, 224, 3)
JOINT_LOWER = np.asarray(
    [-2.8973, -1.7628, -2.8973, -3.0718, -2.8973, -0.0175, -2.8973],
    dtype=np.float64,
)
JOINT_UPPER = np.asarray(
    [2.8973, 1.7628, 2.8973, -0.0698, 2.8973, 3.7525, 2.8973],
    dtype=np.float64,
)
RUNTIME_IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


class OpenPIBridgeError(RuntimeError):
    """Raised when the policy boundary cannot be proven safe."""


class _Connection(Protocol):
    def recv(self) -> bytes | str: ...

    def send_binary(self, payload: bytes) -> None: ...

    def settimeout(self, timeout: float) -> None: ...

    def close(self) -> None: ...


def _pack_array(value: object) -> object:
    if isinstance(value, (np.ndarray, np.generic)) and value.dtype.kind in {
        "V",
        "O",
        "c",
    }:
        raise OpenPIBridgeError(f"unsupported NumPy dtype {value.dtype}")
    if isinstance(value, np.ndarray):
        return {
            b"__ndarray__": True,
            b"data": value.tobytes(),
            b"dtype": value.dtype.str,
            b"shape": value.shape,
        }
    if isinstance(value, np.generic):
        return {
            b"__npgeneric__": True,
            b"data": value.item(),
            b"dtype": value.dtype.str,
        }
    return value


def _unpack_array(value: dict[bytes, object]) -> object:
    if b"__ndarray__" in value:
        return np.ndarray(
            buffer=value[b"data"],
            dtype=np.dtype(value[b"dtype"]),
            shape=tuple(value[b"shape"]),
        )
    if b"__npgeneric__" in value:
        return np.dtype(value[b"dtype"]).type(value[b"data"])
    return value


def pack_message(value: object) -> bytes:
    """Encode without pickle or object-array fallback."""

    return msgpack.packb(value, default=_pack_array)


def unpack_message(value: bytes) -> object:
    return msgpack.unpackb(value, object_hook=_unpack_array)


def validate_observation(observation: Mapping[str, object]) -> dict[str, object]:
    """Validate the exact Polaris DROID observation before serialization."""

    expected = {
        "observation/exterior_image_1_left",
        "observation/wrist_image_left",
        "observation/joint_position",
        "observation/gripper_position",
        "prompt",
    }
    if set(observation) != expected:
        raise OpenPIBridgeError(
            "OpenPI observation keys do not match the DROID contract"
        )
    result = dict(observation)
    for key in ("observation/exterior_image_1_left", "observation/wrist_image_left"):
        image = np.asarray(result[key])
        if image.shape != IMAGE_SHAPE or image.dtype != np.uint8:
            raise OpenPIBridgeError(f"{key} must be uint8{IMAGE_SHAPE}")
        result[key] = np.ascontiguousarray(image)
    joints = np.asarray(result["observation/joint_position"], dtype=np.float32)
    gripper = np.asarray(result["observation/gripper_position"], dtype=np.float32)
    if joints.shape != (7,) or gripper.shape != (1,):
        raise OpenPIBridgeError(
            "Franka state must contain seven joints and one gripper value"
        )
    if not np.isfinite(joints).all() or not np.isfinite(gripper).all():
        raise OpenPIBridgeError("Franka state contains non-finite values")
    prompt = result["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1024:
        raise OpenPIBridgeError(
            "prompt must be a non-empty string of at most 1024 characters"
        )
    result["observation/joint_position"] = joints
    result["observation/gripper_position"] = gripper
    return result


def validate_actions(response: object) -> np.ndarray:
    """Require finite, in-range absolute targets with exact ``[15, 8]`` shape."""

    if not isinstance(response, Mapping) or "actions" not in response:
        raise OpenPIBridgeError("OpenPI response does not contain actions")
    actions = np.asarray(response["actions"], dtype=np.float64)
    if actions.shape != ACTION_SHAPE:
        raise OpenPIBridgeError(f"OpenPI actions must have shape {ACTION_SHAPE}")
    if not np.isfinite(actions).all():
        raise OpenPIBridgeError("OpenPI actions contain non-finite values")
    if np.any(actions[:, :7] < JOINT_LOWER) or np.any(actions[:, :7] > JOINT_UPPER):
        raise OpenPIBridgeError("OpenPI joint targets exceed Franka position limits")
    if np.any(actions[:, 7] < 0.0) or np.any(actions[:, 7] > 1.0):
        raise OpenPIBridgeError("OpenPI gripper targets must be normalized to [0, 1]")
    return actions


def safe_position_targets(
    actions: np.ndarray,
    current_joints: np.ndarray,
    *,
    max_joint_delta_rad: float,
    execute_steps: int,
) -> np.ndarray:
    """Rate-limit already validated absolute targets; invalid input never clips through."""

    checked = validate_actions({"actions": actions})
    current = np.asarray(current_joints, dtype=np.float64)
    if current.shape != (7,) or not np.isfinite(current).all():
        raise OpenPIBridgeError("current Franka joint state is invalid")
    if not math.isfinite(max_joint_delta_rad) or max_joint_delta_rad <= 0:
        raise OpenPIBridgeError("max joint delta must be positive and finite")
    if not 1 <= execute_steps <= ACTION_SHAPE[0]:
        raise OpenPIBridgeError("execute steps must be between 1 and 15")
    result: list[np.ndarray] = []
    previous = current.copy()
    for raw in checked[:execute_steps]:
        limited = np.clip(
            raw[:7], previous - max_joint_delta_rad, previous + max_joint_delta_rad
        )
        if np.any(limited < JOINT_LOWER) or np.any(limited > JOINT_UPPER):
            raise OpenPIBridgeError(
                "rate-limited joint target is outside Franka limits"
            )
        result.append(np.concatenate([limited, raw[7:8]]))
        previous = limited
    return np.stack(result)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    initial_backoff_seconds: float = 0.5
    maximum_backoff_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("retry attempts must be positive")
        if self.initial_backoff_seconds < 0 or self.maximum_backoff_seconds < 0:
            raise ValueError("retry backoff must not be negative")


class OpenPIWebsocketClient:
    """Bounded, reconnecting client for upstream OpenPI on port 8000."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 8000,
        connect_timeout_seconds: float = 10.0,
        inference_timeout_seconds: float = 30.0,
        retry: RetryPolicy = RetryPolicy(),
        connection_factory: Callable[..., _Connection] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not host or "://" in host or "/" in host:
            raise ValueError(
                "policy host must be a DNS name or IP address without a scheme"
            )
        if not 1 <= port <= 65535:
            raise ValueError("policy port is invalid")
        if connect_timeout_seconds <= 0 or inference_timeout_seconds <= 0:
            raise ValueError("policy timeouts must be positive")
        self._url = f"ws://{host}:{port}"
        self._connect_timeout = connect_timeout_seconds
        self._inference_timeout = inference_timeout_seconds
        self._retry = retry
        self._factory = connection_factory
        self._sleep = sleep
        self._connection: _Connection | None = None
        self.server_metadata: Mapping[str, object] = {}

    def _connect(self) -> _Connection:
        factory = self._factory
        if factory is None:
            import websocket

            factory = websocket.create_connection
        connection = factory(
            self._url,
            timeout=self._connect_timeout,
            enable_multithread=False,
            skip_utf8_validation=False,
        )
        connection.settimeout(self._inference_timeout)
        metadata_raw = connection.recv()
        if not isinstance(metadata_raw, bytes):
            connection.close()
            raise OpenPIBridgeError("OpenPI metadata frame must be binary")
        metadata = unpack_message(metadata_raw)
        if not isinstance(metadata, Mapping):
            connection.close()
            raise OpenPIBridgeError("OpenPI metadata frame is malformed")
        self.server_metadata = metadata
        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def infer(self, observation: Mapping[str, object]) -> np.ndarray:
        payload = pack_message(validate_observation(observation))
        last_error: Exception | None = None
        for attempt in range(self._retry.attempts):
            try:
                connection = self._connection or self._connect()
                connection.send_binary(payload)
                raw = connection.recv()
                if not isinstance(raw, bytes):
                    raise OpenPIBridgeError("OpenPI inference frame must be binary")
                return validate_actions(unpack_message(raw))
            except Exception as exc:
                last_error = exc
                self.close()
                if attempt + 1 < self._retry.attempts:
                    delay = min(
                        self._retry.maximum_backoff_seconds,
                        self._retry.initial_backoff_seconds * 2**attempt,
                    )
                    self._sleep(delay)
        raise OpenPIBridgeError(
            f"OpenPI inference failed after {self._retry.attempts} attempts"
        ) from last_error


def _safe_name(run_id: str) -> str:
    return "npa-antioch-openpi-" + hashlib.sha256(run_id.encode()).hexdigest()[:10]


def render_stack(
    *,
    run_id: str,
    namespace: str,
    policy_image: str,
    bridge_image: str,
    policy_terms_secret: str,
    isaac_acceptance_secret: str,
    policy_gpu_selector_key: str,
    policy_gpu_selector_value: str,
    bridge_gpu_selector_key: str,
    bridge_gpu_selector_value: str,
    image_pull_secret: str = "",
    antioch_config_secret: str = "",
    output_uri: str = "",
    s3_credentials_secret: str = "",
    prompt: str = "pick up the fork",
    policy_ready_timeout_seconds: int = 1800,
) -> dict[str, object]:
    """Render a private two-workload Kubernetes stack with disjoint secrets/GPUs."""

    for label, image in (("policy", policy_image), ("bridge", bridge_image)):
        if not RUNTIME_IMAGE_RE.fullmatch(image):
            raise OpenPIBridgeError(f"{label} image must be digest-pinned")
    required = {
        "run id": run_id,
        "namespace": namespace,
        "policy terms secret": policy_terms_secret,
        "Isaac acceptance secret": isaac_acceptance_secret,
        "policy GPU selector key": policy_gpu_selector_key,
        "policy GPU selector value": policy_gpu_selector_value,
        "bridge GPU selector key": bridge_gpu_selector_key,
        "bridge GPU selector value": bridge_gpu_selector_value,
    }
    if any(not value.strip() for value in required.values()):
        missing = ", ".join(
            name for name, value in required.items() if not value.strip()
        )
        raise OpenPIBridgeError(f"required deployment values are empty: {missing}")
    if policy_ready_timeout_seconds <= 0:
        raise OpenPIBridgeError("policy readiness timeout must be positive")
    name = _safe_name(run_id)
    policy_labels = {"app": f"{name}-policy", "npa.nebius.ai/run": name}
    bridge_labels = {"app": f"{name}-bridge", "npa.nebius.ai/run": name}
    pull_secrets = [{"name": image_pull_secret}] if image_pull_secret else []
    policy_container = {
        "name": "openpi-policy",
        "image": policy_image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/bash", "-lc"],
        "args": [
            "set -euo pipefail; "
            'test "$NPA_OPENPI_ACCEPT_GEMMA_TERMS" = YES || exit 64; '
            "checkpoint_dir=$(/opt/venv/bin/python -c 'from openpi.shared import download; "
            'print(download.maybe_download("gs://openpi-assets/checkpoints/polaris/'
            'pi05_droid_jointpos_polaris", token="anon"))\'); '
            "exec /opt/venv/bin/python /opt/byof/scripts/serve_policy.py --port=8000 "
            "policy:checkpoint --policy.config=pi05_droid_jointpos_polaris "
            '--policy.dir="$checkpoint_dir"'
        ],
        "ports": [{"name": "policy", "containerPort": 8000}],
        "env": [
            {
                "name": "NPA_OPENPI_ACCEPT_GEMMA_TERMS",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": policy_terms_secret,
                        "key": "NPA_OPENPI_ACCEPT_GEMMA_TERMS",
                    }
                },
            },
            {"name": "OPENPI_DATA_HOME", "value": "/workspace/openpi-cache"},
        ],
        "resources": {
            "requests": {"cpu": "16", "memory": "96Gi", "nvidia.com/gpu": "1"},
            "limits": {"nvidia.com/gpu": "1"},
        },
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": 8000},
            "periodSeconds": 5,
            "failureThreshold": 240,
        },
        "livenessProbe": {
            "httpGet": {"path": "/healthz", "port": 8000},
            "initialDelaySeconds": 600,
            "periodSeconds": 30,
            "failureThreshold": 6,
        },
        "volumeMounts": [
            {"name": "policy-cache", "mountPath": "/workspace/openpi-cache"}
        ],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    bridge_env: list[dict[str, object]] = [
        {"name": "OPENPI_POLICY_HOST", "value": f"{name}-policy"},
        {"name": "OPENPI_POLICY_PORT", "value": "8000"},
        {
            "name": "NO_PROXY",
            "value": f"{name}-policy,{name}-policy.{namespace}.svc,.svc,.cluster.local",
        },
        {
            "name": "no_proxy",
            "value": f"{name}-policy,{name}-policy.{namespace}.svc,.svc,.cluster.local",
        },
        {"name": "OPENPI_PROMPT", "value": prompt},
        {"name": "NPA_OPENPI_BRIDGE_OUTPUT_URI", "value": output_uri},
        {
            "name": "ACCEPT_EULA",
            "valueFrom": {
                "secretKeyRef": {"name": isaac_acceptance_secret, "key": "ACCEPT_EULA"}
            },
        },
    ]
    bridge_mounts: list[dict[str, object]] = [
        {"name": "isaac-cache", "mountPath": "/opt/isaac-cache"},
    ]
    bridge_volumes: list[dict[str, object]] = [
        {"name": "isaac-cache", "emptyDir": {"sizeLimit": "16Gi"}},
    ]
    if antioch_config_secret:
        bridge_env.append({"name": "ANTIOCH_CONFIG_DIR", "value": "/etc/antioch"})
        bridge_mounts.append(
            {"name": "antioch-config", "mountPath": "/etc/antioch", "readOnly": True}
        )
        bridge_volumes.append(
            {"name": "antioch-config", "secret": {"secretName": antioch_config_secret}}
        )
    bridge_container: dict[str, object] = {
        "name": "isaac-franka-bridge",
        "image": bridge_image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["/isaac-sim/python.sh"],
        "args": ["-m", "npa.workbench.antioch.openpi_isaac"],
        "env": bridge_env,
        "resources": {
            "requests": {"cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "1"},
            "limits": {"nvidia.com/gpu": "1"},
        },
        "volumeMounts": bridge_mounts,
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    if s3_credentials_secret:
        bridge_container["envFrom"] = [{"secretRef": {"name": s3_credentials_secret}}]
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": f"{name}-policy", "namespace": namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": policy_labels},
                    "template": {
                        "metadata": {"labels": policy_labels},
                        "spec": {
                            "restartPolicy": "Always",
                            "imagePullSecrets": pull_secrets,
                            "nodeSelector": {
                                policy_gpu_selector_key: policy_gpu_selector_value
                            },
                            "securityContext": {"runAsNonRoot": True},
                            "containers": [policy_container],
                            "volumes": [
                                {
                                    "name": "policy-cache",
                                    "emptyDir": {"sizeLimit": "40Gi"},
                                }
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": f"{name}-policy", "namespace": namespace},
                "spec": {
                    "type": "ClusterIP",
                    "selector": policy_labels,
                    "ports": [{"name": "policy", "port": 8000, "targetPort": 8000}],
                },
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": f"{name}-bridge", "namespace": namespace},
                "spec": {
                    "backoffLimit": 0,
                    "template": {
                        "metadata": {"labels": bridge_labels},
                        "spec": {
                            "restartPolicy": "Never",
                            "imagePullSecrets": pull_secrets,
                            "nodeSelector": {
                                bridge_gpu_selector_key: bridge_gpu_selector_value
                            },
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "runAsGroup": 1000,
                                "fsGroup": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "initContainers": [
                                {
                                    "name": "wait-for-policy",
                                    "image": bridge_image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["/opt/npa/sim/venv/bin/python"],
                                    "args": [
                                        "-m",
                                        "npa.workbench.antioch.openpi_health",
                                        "--host",
                                        f"{name}-policy",
                                        "--port",
                                        "8000",
                                        "--timeout-seconds",
                                        str(policy_ready_timeout_seconds),
                                    ],
                                    "env": [
                                        {
                                            "name": "NO_PROXY",
                                            "value": (
                                                f"{name}-policy,"
                                                f"{name}-policy.{namespace}.svc,.svc,.cluster.local"
                                            ),
                                        },
                                        {
                                            "name": "no_proxy",
                                            "value": (
                                                f"{name}-policy,"
                                                f"{name}-policy.{namespace}.svc,.svc,.cluster.local"
                                            ),
                                        },
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "128Mi"},
                                        "limits": {"cpu": "1", "memory": "512Mi"},
                                    },
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                }
                            ],
                            "containers": [bridge_container],
                            "volumes": bridge_volumes,
                        },
                    },
                },
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": f"{name}-policy-ingress", "namespace": namespace},
                "spec": {
                    "podSelector": {"matchLabels": policy_labels},
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "from": [{"podSelector": {"matchLabels": bridge_labels}}],
                            "ports": [{"protocol": "TCP", "port": 8000}],
                        }
                    ],
                },
            },
        ],
    }


def contract_smoke() -> dict[str, object]:
    """Exercise serialization, exact shapes, and safe target limiting without a GPU."""

    observation = {
        "observation/exterior_image_1_left": np.zeros(IMAGE_SHAPE, dtype=np.uint8),
        "observation/wrist_image_left": np.zeros(IMAGE_SHAPE, dtype=np.uint8),
        "observation/joint_position": np.asarray(
            [0, -0.5, 0, -1.5, 0, 1.0, 0], dtype=np.float32
        ),
        "observation/gripper_position": np.asarray([0.5], dtype=np.float32),
        "prompt": "pick up the fork",
    }
    decoded = unpack_message(pack_message(validate_observation(observation)))
    actions = np.tile(np.asarray([0, -0.5, 0, -1.5, 0, 1.0, 0, 0.5]), (15, 1))
    limited = safe_position_targets(
        actions,
        np.asarray(observation["observation/joint_position"]),
        max_joint_delta_rad=0.1,
        execute_steps=5,
    )
    return {
        "schema": "npa.antioch.openpi-bridge.contract-smoke.v1",
        "status": "passed",
        "observation_keys": sorted(decoded),
        "action_shape": list(actions.shape),
        "executed_target_shape": list(limited.shape),
        "fail_closed": True,
    }
