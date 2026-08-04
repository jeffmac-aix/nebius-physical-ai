"""Kubernetes contract for a browser-teleoperated LeIsaac session."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

SESSION_SCHEMA = "npa.leisaac.session.v1"
TASK = "LeIsaac-SO101-PickOrange-v0"
TELEOP_DEVICE = "keyboard"
SOURCE_VERSION = "0.4.0"
SOURCE_COMMIT = "1651c321e9b0c1bb54233211fc7b3cd70d8373d5"
ISAAC_SIM_VERSION = "5.1.0.0"
ISAAC_LAB_VERSION = "2.3.2.post1"
SIGNAL_PORT = 49100
MEDIA_PORT = 47998
SERVICE_PORT = 8080
RELAY_SERVICE_PORT = 48080
GPU_PRODUCT = "NVIDIA-RTX-PRO-6000-Blackwell-Server-Edition"
TRANSPORT_LOAD_BALANCER = "public-load-balancer"
TRANSPORT_AGENT_RELAY = "agent-relay"

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LeIsaacConfigError(ValueError):
    """Raised when a teleoperation session would be unsafe or unusable."""


def resource_name(run_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    if not normalized:
        raise LeIsaacConfigError("run id does not produce a Kubernetes resource name")
    return f"leisaac-{normalized[:45]}"


def validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not _RUN_ID.fullmatch(value):
        raise LeIsaacConfigError(
            "run id must contain only letters, numbers, '.', '_' and '-'"
        )
    return value


def validate_image(image: str) -> str:
    value = str(image or "").strip()
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", value):
        raise LeIsaacConfigError("LeIsaac image must be pinned by sha256 digest")
    return value


def validate_public_ip(value: str, label: str) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError as exc:
        raise LeIsaacConfigError(f"{label} must be an IP address") from exc
    if not address.is_global:
        raise LeIsaacConfigError(f"{label} must be a public IP address")
    return address.compressed


def validate_source_ranges(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for raw in values:
        try:
            network = ipaddress.ip_network(str(raw or "").strip(), strict=False)
        except ValueError as exc:
            raise LeIsaacConfigError(
                f"invalid LeIsaac source range: {raw}"
            ) from exc
        if not network.is_global:
            raise LeIsaacConfigError(f"LeIsaac source range must be public: {network}")
        result.append(network.with_prefixlen)
    if not result:
        raise LeIsaacConfigError(
            "at least one agent/operator source range is required"
        )
    return sorted(set(result))


def validate_expiry(value: str, *, now: datetime | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LeIsaacConfigError("expires-at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise LeIsaacConfigError("expires-at must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= (now or datetime.now(timezone.utc)):
        raise LeIsaacConfigError("expires-at must be in the future")
    return parsed.isoformat().replace("+00:00", "Z")


def split_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise LeIsaacConfigError("artifact-uri must be s3://BUCKET/PREFIX")
    return parsed.netloc, parsed.path.strip("/")


def service_manifests(
    *,
    run_id: str,
    namespace: str,
    source_ranges: list[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Build the two LBs before the GPU pod so its public media IP is known."""

    run_id = validate_run_id(run_id)
    ranges = validate_source_ranges(source_ranges)
    name = resource_name(run_id)
    labels = {
        "app": name,
        "app.kubernetes.io/name": "leisaac",
        "app.kubernetes.io/instance": name,
        "app.kubernetes.io/managed-by": "npa",
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"{name}-tcp",
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "type": "LoadBalancer",
                "loadBalancerSourceRanges": ranges,
                "selector": {"app": name},
                "ports": [
                    {
                        "name": "status",
                        "protocol": "TCP",
                        "port": SERVICE_PORT,
                        "targetPort": SERVICE_PORT,
                    },
                    {
                        "name": "signal",
                        "protocol": "TCP",
                        "port": SIGNAL_PORT,
                        "targetPort": SIGNAL_PORT,
                    },
                ],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"{name}-media",
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "type": "LoadBalancer",
                "loadBalancerSourceRanges": ranges,
                "selector": {"app": name},
                "ports": [
                    {
                        "name": "media",
                        "protocol": "UDP",
                        "port": MEDIA_PORT,
                        "targetPort": MEDIA_PORT,
                    }
                ],
            },
        },
    ]


def relay_service_manifest(
    *,
    run_id: str,
    namespace: str,
    agent_project: str = "",
    agent_name: str = "",
    source_ranges: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build one private ClusterIP service for an agent-relayed session.

    The service has no cloud load balancer or public address.  A separately
    authenticated NPA agent VM reaches these NodePorts over the private VPC and
    maintains only the fixed loopback TCP and source-restricted backhaul/media
    contracts. The sidecar uses pod-local sockets rather than this Service.
    """

    run_id = validate_run_id(run_id)
    name = resource_name(run_id)
    labels = {
        "app": name,
        "app.kubernetes.io/name": "leisaac",
        "app.kubernetes.io/instance": name,
        "app.kubernetes.io/managed-by": "npa",
    }
    annotations = {
        "npa.nebius.com/agent-project": str(agent_project),
        "npa.nebius.com/agent-name": str(agent_name),
        "npa.nebius.com/source-ranges": ",".join(
            validate_source_ranges(source_ranges)
        ),
    }
    if not agent_project or not agent_name:
        raise LeIsaacConfigError("agent relay requires an agent project and name")
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{name}-relay",
            "namespace": namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": name},
            "ports": [
                {
                    "name": "status",
                    "protocol": "TCP",
                    "port": SERVICE_PORT,
                    "targetPort": SERVICE_PORT,
                },
                {
                    "name": "signal",
                    "protocol": "TCP",
                    "port": SIGNAL_PORT,
                    "targetPort": SIGNAL_PORT,
                },
                {
                    "name": "media",
                    "protocol": "UDP",
                    "port": MEDIA_PORT,
                    "targetPort": MEDIA_PORT,
                },
            ],
        },
    }


def relay_client_secret_manifest(
    *,
    run_id: str,
    namespace: str,
    agent_host: str,
    session_nonce: str,
    certificate_sha256: str,
    auth_user: str,
    auth_password: str,
    client_source: str,
) -> dict[str, Any]:
    """Mount the authenticated TLS backhaul client into the GPU pod."""

    name = resource_name(validate_run_id(run_id))
    agent_host = validate_public_ip(agent_host, "agent host")
    if not re.fullmatch(r"[a-f0-9]{64}", session_nonce):
        raise LeIsaacConfigError("session nonce is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", certificate_sha256):
        raise LeIsaacConfigError("relay certificate fingerprint is invalid")
    if not auth_user or not auth_password or "\n" in auth_user + auth_password:
        raise LeIsaacConfigError("agent basic-auth credential is invalid")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"{name}-relay-client",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "leisaac",
                "app.kubernetes.io/instance": name,
                "app.kubernetes.io/managed-by": "npa",
            },
        },
        "type": "Opaque",
        "stringData": {
            "reverse_client.py": client_source,
            "config.json": json.dumps(
                {
                    "agent_host": agent_host,
                    "session_nonce": session_nonce,
                    "certificate_sha256": certificate_sha256,
                    "auth_user": auth_user,
                    "auth_password": auth_password,
                },
                sort_keys=True,
            ),
        },
    }


def deployment_manifest(
    *,
    run_id: str,
    namespace: str,
    image: str,
    media_host: str,
    session_nonce: str,
    image_pull_secret: str = "npa-registry",
    relay_client_secret: str = "",
) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    image = validate_image(image)
    media_host = validate_public_ip(media_host, "media host")
    if not re.fullmatch(r"[a-f0-9]{64}", session_nonce):
        raise LeIsaacConfigError(
            "session nonce must be 64 lowercase hexadecimal characters"
        )
    name = resource_name(run_id)
    labels = {
        "app": name,
        "app.kubernetes.io/name": "leisaac",
        "app.kubernetes.io/instance": name,
        "app.kubernetes.io/managed-by": "npa",
    }
    environment = {
        "OMNI_KIT_ACCEPT_EULA": "YES",
        "ISAACSIM_ACCEPT_EULA": "YES",
        "NPA_LEISAAC_RUN_ID": run_id,
        "NPA_LEISAAC_SESSION_NONCE": session_nonce,
        "NPA_LEISAAC_MEDIA_HOST": media_host,
        "NVIDIA_DRIVER_CAPABILITIES": "all",
    }
    pod_spec: dict[str, Any] = {
        "nodeSelector": {"nvidia.com/gpu.product": GPU_PRODUCT},
        "containers": [
            {
                "name": "leisaac",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "ports": [
                    {
                        "name": "status",
                        "containerPort": SERVICE_PORT,
                        "protocol": "TCP",
                    },
                    {"name": "signal", "containerPort": SIGNAL_PORT, "protocol": "TCP"},
                    {"name": "media", "containerPort": MEDIA_PORT, "protocol": "UDP"},
                ],
                "env": [
                    {"name": key, "value": value}
                    for key, value in sorted(environment.items())
                ],
                "resources": {
                    "requests": {
                        "cpu": "4",
                        "memory": "24Gi",
                        "ephemeral-storage": "70Gi",
                        "nvidia.com/gpu": "1",
                    },
                    "limits": {
                        "cpu": "8",
                        "memory": "48Gi",
                        "ephemeral-storage": "90Gi",
                        "nvidia.com/gpu": "1",
                    },
                },
                "readinessProbe": {
                    "httpGet": {"path": "/status", "port": "status"},
                    "periodSeconds": 5,
                    "failureThreshold": 720,
                },
                "livenessProbe": {
                    "httpGet": {"path": "/healthz", "port": "status"},
                    "initialDelaySeconds": 30,
                    "periodSeconds": 30,
                    "failureThreshold": 6,
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "runAsNonRoot": True,
                    "runAsUser": 1000,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "volumeMounts": [
                    {"name": "isaac-cache", "mountPath": "/opt/isaac-cache"},
                    {"name": "leisaac-cache", "mountPath": "/opt/leisaac-cache"},
                    {"name": "tmp", "mountPath": "/tmp"},
                    {"name": "shm", "mountPath": "/dev/shm"},
                ],
            }
        ],
        "volumes": [
            {"name": "isaac-cache", "emptyDir": {"sizeLimit": "30Gi"}},
            {"name": "leisaac-cache", "emptyDir": {"sizeLimit": "2Gi"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "20Gi"}},
            {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}},
        ],
        "restartPolicy": "Always",
    }
    if relay_client_secret:
        pod_spec["containers"].append(
            {
                "name": "agent-relay-client",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "/opt/npa/sim/venv/bin/python",
                    "/opt/npa-relay/reverse_client.py",
                    "--config",
                    "/opt/npa-relay/config.json",
                ],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "runAsNonRoot": True,
                    "runAsUser": 1000,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "volumeMounts": [
                    {"name": "relay-client", "mountPath": "/opt/npa-relay", "readOnly": True}
                ],
            }
        )
        pod_spec["volumes"].append(
            {
                "name": "relay-client",
                "secret": {
                    "secretName": relay_client_secret,
                    "defaultMode": 0o555,
                },
            }
        )
    if image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": image_pull_secret}]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


def session_manifest(
    *,
    run_id: str,
    image: str,
    signal_host: str,
    media_host: str,
    session_nonce: str,
    expires_at: str = "",
    gpu: str = GPU_PRODUCT,
    created_at: str | None = None,
    transport: str = TRANSPORT_LOAD_BALANCER,
) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    image = validate_image(image)
    if transport not in (TRANSPORT_LOAD_BALANCER, TRANSPORT_AGENT_RELAY):
        raise LeIsaacConfigError(f"unsupported LeIsaac transport: {transport}")
    if transport == TRANSPORT_AGENT_RELAY:
        if signal_host != "127.0.0.1":
            raise LeIsaacConfigError("agent-relay signaling must use 127.0.0.1")
    else:
        signal_host = validate_public_ip(signal_host, "signal host")
    media_host = validate_public_ip(media_host, "media host")
    expires_at = validate_expiry(expires_at)
    if not re.fullmatch(r"[a-f0-9]{64}", session_nonce):
        raise LeIsaacConfigError(
            "session nonce must be 64 lowercase hexadecimal characters"
        )
    manifest = {
        "schema": SESSION_SCHEMA,
        "run_id": run_id,
        "provider": "nebius-kubernetes",
        "transport": transport,
        "task": TASK,
        "teleop_device": TELEOP_DEVICE,
        "signal_host": signal_host,
        "signal_port": SIGNAL_PORT,
        "media_host": media_host,
        "media_port": MEDIA_PORT,
        "service_url": (
            f"http://127.0.0.1:{RELAY_SERVICE_PORT}"
            if transport == TRANSPORT_AGENT_RELAY
            else f"http://{signal_host}:{SERVICE_PORT}"
        ),
        "session_nonce": session_nonce,
        "created_at": created_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_version": SOURCE_VERSION,
        "source_commit": SOURCE_COMMIT,
        "isaac_sim_version": ISAAC_SIM_VERSION,
        "isaac_lab_version": ISAAC_LAB_VERSION,
        "image": image,
        "gpu": gpu,
    }
    if expires_at:
        manifest["expires_at"] = expires_at
    return manifest
