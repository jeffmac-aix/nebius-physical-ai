"""Idempotent MK8s deployment for the cluster-native Antioch live path."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from npa.workflows.byof.openpi_live import LIVE_MANAGED_BY, _certificate

from .live import _relay_certificate

CONFIG_SCHEMA = "npa.antioch.mk8s-live-config.v1"
MANAGED_BY = "npa-antioch-mk8s-live"
SCENARIO = "openpi_franka_mk8s_live"
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_SECRET_KEY = re.compile(r"^[A-Za-z0-9._-]+$")
_METRIC_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_ANTIOCH_TERMS_ENV = "NPA_ANTIOCH_ACCEPT_TERMS"


class ClusterLiveError(RuntimeError):
    """The cluster-native desired state could not be reconciled safely."""


class ClusterLiveConfig(BaseModel):
    """Private, owner-readable runtime coordinates; never emitted by the CLI."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal[CONFIG_SCHEMA] = CONFIG_SCHEMA
    workflow_run: str = Field(min_length=1, max_length=128)
    state_id: str = Field(min_length=1, max_length=128)
    kubeconfig: str
    context: str = ""
    namespace: str = "workbench"
    adapter_image: str
    policy_selector: dict[str, str]
    policy_gateway_port: int = Field(default=8443, ge=1, le=65535)
    policy_probe_ports: list[int] = Field(default_factory=lambda: [8001, 8002])
    policy_network_policy_name: str
    policy_auth_secret_name: str
    policy_tls_secret_name: str
    policy_cache_pvc_name: str
    public_rollback_service_name: str = ""
    image_pull_secret: str = ""
    antioch_config_dir: str
    antioch_project_id_file: str
    adapter_replicas: int = Field(default=1, ge=0, le=1)
    scenario_timeout_seconds: int = Field(default=14_400, ge=60)
    kubelet_source_cidrs: list[str] = Field(min_length=1)

    @field_validator(
        "namespace",
        "policy_network_policy_name",
        "policy_auth_secret_name",
        "policy_tls_secret_name",
        "policy_cache_pvc_name",
        "public_rollback_service_name",
        "image_pull_secret",
    )
    @classmethod
    def _dns_label(cls, value: str) -> str:
        resolved = value.strip()
        if resolved and (len(resolved) > 63 or not _DNS_LABEL.fullmatch(resolved)):
            raise ValueError("Kubernetes names must be DNS labels")
        return resolved

    @field_validator("adapter_image")
    @classmethod
    def _immutable_image(cls, value: str) -> str:
        resolved = value.strip()
        if "@sha256:" not in resolved:
            raise ValueError("adapter_image must be pinned by sha256 digest")
        return resolved

    @model_validator(mode="after")
    def _identity_and_paths(self) -> "ClusterLiveConfig":
        if not self.policy_selector:
            raise ValueError("policy_selector must not be empty")
        for key, value in self.policy_selector.items():
            if not key.strip() or not value.strip():
                raise ValueError("policy_selector entries must not be empty")
        if len(set(self.policy_probe_ports)) != len(self.policy_probe_ports):
            raise ValueError("policy_probe_ports must be unique")
        return self

    @property
    def identity(self) -> str:
        return hashlib.sha256(
            f"{self.workflow_run}\n{self.state_id}".encode()
        ).hexdigest()[:12]

    @property
    def adapter_name(self) -> str:
        return f"npa-antioch-live-{self.identity}"

    @property
    def policy_service_name(self) -> str:
        return f"npa-openpi-internal-{self.identity}"

    @property
    def live_bundle_secret_name(self) -> str:
        return f"{self.adapter_name}-bundle"

    @property
    def config_secret_name(self) -> str:
        return f"{self.adapter_name}-config"

    @property
    def terms_secret_name(self) -> str:
        return f"{self.adapter_name}-terms"

    @property
    def project_secret_name(self) -> str:
        return f"{self.adapter_name}-project"


def load_private_config(path: Path) -> ClusterLiveConfig:
    if not path.is_file() or path.is_symlink():
        raise ClusterLiveError("private runtime config is not a regular file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ClusterLiveError("private runtime config must be mode 0600")
    try:
        return ClusterLiveConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClusterLiveError("private runtime config is malformed") from exc


def _labels(config: ClusterLiveConfig) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "npa-antioch-live",
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "app.kubernetes.io/part-of": "antioch-openpi-mk8s-live",
        "npa.nebius.ai/live-identity": config.identity,
    }


def _container_security() -> dict[str, Any]:
    return {
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "runAsNonRoot": True,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def build_public_manifests(config: ClusterLiveConfig) -> dict[str, dict[str, Any]]:
    """Build secret-free desired state suitable for review and apply."""

    labels = _labels(config)
    private_root = "/run/npa-antioch-private"
    state_root = "/var/run/npa-antioch"
    copy_private = (
        "install -d -m 0700 /private/antioch-config /private/live-bundle; "
        "cp -a /sources/config/. /private/antioch-config/; "
        "cp -a /sources/bundle/. /private/live-bundle/; "
        "install -m 0600 /sources/terms/accepted /private/antioch-terms; "
        "install -m 0600 /sources/project/project-id /private/project-id; "
        "find /private -type d -exec chmod 0700 {} +; "
        "find /private -type f -exec chmod 0600 {} +; "
        "chown -R 10001:10001 /private"
    )
    volumes: list[dict[str, Any]] = [
        {"name": "private", "emptyDir": {"medium": "Memory"}},
        {"name": "state", "emptyDir": {}},
        {"name": "runtime", "emptyDir": {}},
        {"name": "runtime-cache", "emptyDir": {}},
        {"name": "tmp", "emptyDir": {}},
        {
            "name": "source-config",
            "secret": {"secretName": config.config_secret_name, "defaultMode": 256},
        },
        {
            "name": "source-bundle",
            "secret": {
                "secretName": config.live_bundle_secret_name,
                "defaultMode": 256,
            },
        },
        {
            "name": "source-terms",
            "secret": {"secretName": config.terms_secret_name, "defaultMode": 256},
        },
        {
            "name": "source-project",
            "secret": {
                "secretName": config.project_secret_name,
                "defaultMode": 256,
            },
        },
    ]
    private_mount = {"name": "private", "mountPath": private_root, "readOnly": True}
    controller = {
        "name": "antioch-controller",
        "image": config.adapter_image,
        "imagePullPolicy": "IfNotPresent",
        "command": [
            "python",
            "-m",
            "npa.workbench.antioch.cluster_runtime",
            "run",
            "--scenario-timeout-seconds",
            str(config.scenario_timeout_seconds),
        ],
        "env": [
            {"name": "ANTIOCH_CONFIG_DIR", "value": f"{private_root}/antioch-config"},
            {
                "name": "NPA_ANTIOCH_RUNTIME_CACHE",
                "value": "/workspace/.cache/npa/antioch",
            },
        ],
        "securityContext": _container_security(),
        "resources": {
            "requests": {"cpu": "500m", "memory": "768Mi"},
            "limits": {"cpu": "2", "memory": "2Gi"},
        },
        "volumeMounts": [
            private_mount,
            {"name": "state", "mountPath": state_root},
            {"name": "runtime", "mountPath": "/var/lib/npa-antioch-live"},
            {"name": "runtime-cache", "mountPath": "/workspace/.cache/npa/antioch"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "readinessProbe": {
            "exec": {
                "command": [
                    "python",
                    "-m",
                    "npa.workbench.antioch.cluster_runtime",
                    "probe",
                    "--component",
                    "controller",
                    "--state-path",
                    f"{state_root}/controller.json",
                ]
            },
            "periodSeconds": 5,
            "failureThreshold": 60,
        },
    }
    relay = {
        "name": "policy-relay",
        "image": config.adapter_image,
        "imagePullPolicy": "IfNotPresent",
        "command": [
            "python",
            "-m",
            "npa.workbench.antioch.relay",
            "--bundle",
            f"{private_root}/live-bundle",
            "--local-port",
            "18444",
            "--stop-file",
            f"{state_root}/stop",
            "--state-path",
            f"{state_root}/relay.json",
        ],
        "securityContext": _container_security(),
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        },
        "volumeMounts": [
            private_mount,
            {"name": "state", "mountPath": state_root},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "readinessProbe": {
            "exec": {
                "command": [
                    "python",
                    "-m",
                    "npa.workbench.antioch.cluster_runtime",
                    "probe",
                    "--component",
                    "relay",
                    "--state-path",
                    f"{state_root}/relay.json",
                ]
            },
            "periodSeconds": 5,
            "failureThreshold": 120,
        },
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": config.adapter_name,
            "namespace": config.namespace,
            "labels": labels,
        },
        "spec": {
            "replicas": config.adapter_replicas,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 300,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": [
                        {
                            "name": "stage-private-runtime",
                            "image": config.adapter_image,
                            "command": ["/bin/sh", "-ceu", copy_private],
                            "securityContext": {
                                "runAsUser": 0,
                                "runAsNonRoot": False,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "25m", "memory": "32Mi"},
                                "limits": {"cpu": "250m", "memory": "128Mi"},
                            },
                            "volumeMounts": [
                                {"name": "private", "mountPath": "/private"},
                                {
                                    "name": "source-config",
                                    "mountPath": "/sources/config",
                                    "readOnly": True,
                                },
                                {
                                    "name": "source-bundle",
                                    "mountPath": "/sources/bundle",
                                    "readOnly": True,
                                },
                                {
                                    "name": "source-terms",
                                    "mountPath": "/sources/terms",
                                    "readOnly": True,
                                },
                                {
                                    "name": "source-project",
                                    "mountPath": "/sources/project",
                                    "readOnly": True,
                                },
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "containers": [controller, relay],
                    "volumes": volumes,
                    **(
                        {"imagePullSecrets": [{"name": config.image_pull_secret}]}
                        if config.image_pull_secret
                        else {}
                    ),
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": config.policy_service_name,
            "namespace": config.namespace,
            "labels": labels,
        },
        "spec": {
            "type": "ClusterIP",
            "selector": config.policy_selector,
            "ports": [
                {
                    "name": "wss",
                    "protocol": "TCP",
                    "port": 443,
                    "targetPort": config.policy_gateway_port,
                }
            ],
        },
    }
    ingress: list[dict[str, Any]] = [
        {
            "from": [{"podSelector": {"matchLabels": labels}}],
            "ports": [{"protocol": "TCP", "port": config.policy_gateway_port}],
        },
        {
            "from": [
                {"ipBlock": {"cidr": cidr}}
                for cidr in sorted(set(config.kubelet_source_cidrs))
            ],
            "ports": [
                {"protocol": "TCP", "port": port}
                for port in sorted(set(config.policy_probe_ports))
            ],
        },
    ]
    policy_network_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": config.policy_network_policy_name,
            "namespace": config.namespace,
            "labels": labels,
        },
        "spec": {
            "podSelector": {"matchLabels": config.policy_selector},
            "policyTypes": ["Ingress"],
            "ingress": ingress,
        },
    }
    adapter_network_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": config.adapter_name,
            "namespace": config.namespace,
            "labels": labels,
        },
        "spec": {
            "podSelector": {"matchLabels": labels},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                {
                    "to": [{"podSelector": {"matchLabels": config.policy_selector}}],
                    "ports": [{"protocol": "TCP", "port": config.policy_gateway_port}],
                },
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            }
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                    "ports": [{"protocol": "TCP", "port": 443}],
                },
            ],
        },
    }
    return {
        "policy_service": service,
        "adapter_deployment": deployment,
        "policy_network_policy": policy_network_policy,
        "adapter_network_policy": adapter_network_policy,
    }


def _private_file(path: Path, *, label: str) -> bytes:
    if (
        not path.is_file()
        or path.is_symlink()
        or stat.S_IMODE(path.stat().st_mode) & 0o077
    ):
        raise ClusterLiveError(f"private {label} must be a mode-0600 regular file")
    value = path.read_bytes()
    if not value:
        raise ClusterLiveError(f"private {label} must not be empty")
    return value


def _terms_acceptance() -> bytes:
    """Return an explicit process-scoped attestation; never read it from disk."""
    accepted = os.environ.get(_ANTIOCH_TERMS_ENV, "").encode()
    if accepted != b"YES":
        raise ClusterLiveError(
            "Antioch terms acceptance is not the exact required value"
        )
    return accepted


def _config_files(directory: Path) -> dict[str, bytes]:
    if (
        not directory.is_dir()
        or directory.is_symlink()
        or stat.S_IMODE(directory.stat().st_mode) & 0o077
    ):
        raise ClusterLiveError("private Antioch config directory must be mode 0700")
    result: dict[str, bytes] = {}
    for path in sorted(directory.iterdir()):
        if (
            not path.is_file()
            or path.is_symlink()
            or not _SECRET_KEY.fullmatch(path.name)
        ):
            raise ClusterLiveError(
                "Antioch config must contain only safe regular files"
            )
        result[path.name] = _private_file(path, label="Antioch config file")
    if not result:
        raise ClusterLiveError("private Antioch config directory is empty")
    return result


def _secret(
    name: str, namespace: str, labels: dict[str, str], data: dict[str, bytes]
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "type": "Opaque",
        "data": {key: base64.b64encode(value).decode() for key, value in data.items()},
    }


def _api_status(exc: Exception) -> int:
    try:
        return int(getattr(exc, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _parse_live_metrics(logs: str) -> dict[str, int | float]:
    latest: dict[str, int | float] = {}
    for line in logs.splitlines():
        marker = "NPA_OPENPI_METRICS "
        if marker not in line:
            continue
        candidate: dict[str, int | float] = {}
        try:
            for item in line.split(marker, 1)[1].split():
                key, separator, value = item.partition("=")
                if separator and _METRIC_KEY.fullmatch(key):
                    candidate[key] = float(value) if "." in value else int(value)
        except ValueError:
            continue
        latest = candidate
    return latest


def _owned(metadata: Any, identity: str, *, allow_openpi: bool = False) -> bool:
    labels = getattr(metadata, "labels", None) or {}
    if labels.get("npa.nebius.ai/live-identity") == identity:
        return labels.get("app.kubernetes.io/managed-by") == MANAGED_BY
    return (
        allow_openpi and labels.get("app.kubernetes.io/managed-by") == LIVE_MANAGED_BY
    )


def _apply_owned(
    read: Any,
    create: Any,
    patch: Any,
    *,
    name: str,
    namespace: str,
    body: dict[str, Any],
    identity: str,
    allow_openpi: bool = False,
) -> str:
    try:
        current = read(name=name, namespace=namespace)
    except Exception as exc:
        if _api_status(exc) != 404:
            raise
        create(namespace=namespace, body=body)
        return "created"
    if not _owned(current.metadata, identity, allow_openpi=allow_openpi):
        raise ClusterLiveError("refusing to replace an unowned Kubernetes object")
    patch(name=name, namespace=namespace, body=body)
    return "reconciled"


def apply_cluster(config: ClusterLiveConfig) -> dict[str, Any]:
    """Stage owner-scoped Secrets, rotate cluster-DNS TLS, and reconcile workloads."""

    from kubernetes import client, config as kube_config

    kube_config.load_kube_config(
        config_file=config.kubeconfig, context=config.context or None
    )
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    networking = client.NetworkingV1Api()
    core.read_namespace(name=config.namespace)
    selector = ",".join(
        f"{key}={value}" for key, value in sorted(config.policy_selector.items())
    )
    deployments = apps.list_namespaced_deployment(
        namespace=config.namespace, label_selector=selector
    ).items
    if len(deployments) != 1:
        raise ClusterLiveError("policy selector must resolve exactly one Deployment")
    policy_deployment = deployments[0]
    if not _owned(policy_deployment.metadata, config.identity, allow_openpi=True):
        raise ClusterLiveError("policy Deployment ownership is not proven")
    pvc = core.read_namespaced_persistent_volume_claim(
        name=config.policy_cache_pvc_name, namespace=config.namespace
    )
    if str(getattr(getattr(pvc, "status", None), "phase", "")) != "Bound":
        raise ClusterLiveError("policy checkpoint PVC is not Bound")
    auth = core.read_namespaced_secret(
        name=config.policy_auth_secret_name, namespace=config.namespace
    )
    if not _owned(auth.metadata, config.identity, allow_openpi=True):
        raise ClusterLiveError("policy authentication Secret ownership is not proven")
    encoded_api_key = (auth.data or {}).get("api-key", "")
    try:
        api_key = base64.b64decode(encoded_api_key, validate=True)
    except ValueError as exc:
        raise ClusterLiveError("policy authentication Secret is malformed") from exc
    if len(api_key.strip()) < 32:
        raise ClusterLiveError("policy authentication Secret is malformed")

    labels = _labels(config)
    host = f"{config.policy_service_name}.{config.namespace}.svc"
    bundle_keys = {
        "ca.crt",
        "api-key",
        "endpoint.json",
        "relay-ca.crt",
        "relay-server.crt",
        "relay-server.key",
        "relay-api-key",
    }
    try:
        existing_bundle = core.read_namespaced_secret(
            name=config.live_bundle_secret_name, namespace=config.namespace
        )
    except Exception as exc:
        if _api_status(exc) != 404:
            raise
        existing_bundle = None
    if existing_bundle is not None:
        if not _owned(existing_bundle.metadata, config.identity):
            raise ClusterLiveError("refusing to reuse an unowned live bundle Secret")
        encoded_bundle = existing_bundle.data or {}
        if set(encoded_bundle) != bundle_keys:
            raise ClusterLiveError("existing live bundle Secret has an invalid schema")
        try:
            bundle = {
                key: base64.b64decode(encoded_bundle[key], validate=True)
                for key in bundle_keys
            }
            endpoint = json.loads(bundle["endpoint.json"])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ClusterLiveError("existing live bundle Secret is malformed") from exc
        if endpoint != {"host": host, "port": 443, "scheme": "wss"}:
            raise ClusterLiveError(
                "existing live bundle targets a different policy Service"
            )
        if bundle["api-key"].strip() != api_key.strip():
            raise ClusterLiveError(
                "existing live bundle does not match policy authentication"
            )
        existing_tls = core.read_namespaced_secret(
            name=config.policy_tls_secret_name, namespace=config.namespace
        )
        if not _owned(existing_tls.metadata, config.identity, allow_openpi=True):
            raise ClusterLiveError("policy TLS Secret ownership is not proven")
        encoded_tls = existing_tls.data or {}
        try:
            certificate = base64.b64decode(encoded_tls["tls.crt"], validate=True)
            private_key = base64.b64decode(encoded_tls["tls.key"], validate=True)
        except (KeyError, ValueError) as exc:
            raise ClusterLiveError("policy TLS Secret is malformed") from exc
        ca = bundle["ca.crt"]
    else:
        ca, certificate, private_key = _certificate(host)
        relay_ca, relay_certificate, relay_key = _relay_certificate()
        relay_token = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=")
        bundle = {
            "ca.crt": ca,
            "api-key": api_key.strip() + b"\n",
            "endpoint.json": json.dumps(
                {"scheme": "wss", "host": host, "port": 443}, sort_keys=True
            ).encode()
            + b"\n",
            "relay-ca.crt": relay_ca,
            "relay-server.crt": relay_certificate,
            "relay-server.key": relay_key,
            "relay-api-key": relay_token + b"\n",
        }
    terms = _terms_acceptance()
    project_id = _private_file(
        Path(config.antioch_project_id_file), label="Antioch project identity"
    )
    secrets = [
        _secret(
            config.config_secret_name,
            config.namespace,
            labels,
            _config_files(Path(config.antioch_config_dir)),
        ),
        _secret(
            config.terms_secret_name,
            config.namespace,
            labels,
            {"accepted": terms.strip() + b"\n"},
        ),
        _secret(
            config.project_secret_name,
            config.namespace,
            labels,
            {"project-id": project_id.strip() + b"\n"},
        ),
        _secret(config.live_bundle_secret_name, config.namespace, labels, bundle),
    ]
    actions: dict[str, str] = {}
    for body in secrets:
        name = body["metadata"]["name"]
        actions[f"secret:{name.rsplit('-', 1)[-1]}"] = _apply_owned(
            core.read_namespaced_secret,
            core.create_namespaced_secret,
            core.patch_namespaced_secret,
            name=name,
            namespace=config.namespace,
            body=body,
            identity=config.identity,
        )

    tls_body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": config.policy_tls_secret_name,
            "namespace": config.namespace,
            "labels": dict(getattr(auth.metadata, "labels", None) or {}),
        },
        "type": "kubernetes.io/tls",
        "data": {
            "tls.crt": base64.b64encode(certificate).decode(),
            "tls.key": base64.b64encode(private_key).decode(),
        },
    }
    actions["policy_tls"] = _apply_owned(
        core.read_namespaced_secret,
        core.create_namespaced_secret,
        core.patch_namespaced_secret,
        name=config.policy_tls_secret_name,
        namespace=config.namespace,
        body=tls_body,
        identity=config.identity,
        allow_openpi=True,
    )
    manifests = build_public_manifests(config)
    service = manifests["policy_service"]
    actions["policy_service"] = _apply_owned(
        core.read_namespaced_service,
        core.create_namespaced_service,
        core.patch_namespaced_service,
        name=service["metadata"]["name"],
        namespace=config.namespace,
        body=service,
        identity=config.identity,
    )
    policy_np = manifests["policy_network_policy"]
    actions["policy_network_policy"] = _apply_owned(
        networking.read_namespaced_network_policy,
        networking.create_namespaced_network_policy,
        networking.patch_namespaced_network_policy,
        name=policy_np["metadata"]["name"],
        namespace=config.namespace,
        body=policy_np,
        identity=config.identity,
        allow_openpi=True,
    )
    annotations = {
        "npa.nebius.ai/cluster-live-tls-sha256": hashlib.sha256(
            ca + b"\0" + certificate + b"\0" + private_key
        ).hexdigest()
    }
    apps.patch_namespaced_deployment(
        name=policy_deployment.metadata.name,
        namespace=config.namespace,
        body={"spec": {"template": {"metadata": {"annotations": annotations}}}},
    )
    adapter = manifests["adapter_deployment"]
    actions["adapter_deployment"] = _apply_owned(
        apps.read_namespaced_deployment,
        apps.create_namespaced_deployment,
        apps.patch_namespaced_deployment,
        name=adapter["metadata"]["name"],
        namespace=config.namespace,
        body=adapter,
        identity=config.identity,
    )
    adapter_np = manifests["adapter_network_policy"]
    actions["adapter_network_policy"] = _apply_owned(
        networking.read_namespaced_network_policy,
        networking.create_namespaced_network_policy,
        networking.patch_namespaced_network_policy,
        name=adapter_np["metadata"]["name"],
        namespace=config.namespace,
        body=adapter_np,
        identity=config.identity,
    )
    return {
        "status": "reconciled",
        "identity": config.identity,
        "actions": actions,
        "policy_service_type": "ClusterIP",
        "transport": "same-pod-antioch-tunnel-to-cluster-local-policy",
        "dev_vm_in_data_path": False,
        "credentials_emitted": False,
    }


def cluster_status(config: ClusterLiveConfig) -> dict[str, Any]:
    from kubernetes import client, config as kube_config
    from kubernetes.client.exceptions import ApiException
    from kubernetes.stream import stream

    kube_config.load_kube_config(
        config_file=config.kubeconfig, context=config.context or None
    )
    apps = client.AppsV1Api()
    core = client.CoreV1Api()
    deployment = apps.read_namespaced_deployment(config.adapter_name, config.namespace)
    if not _owned(deployment.metadata, config.identity):
        raise ClusterLiveError("adapter Deployment ownership is not proven")
    pods = core.list_namespaced_pod(
        config.namespace,
        label_selector=f"npa.nebius.ai/live-identity={config.identity}",
    ).items
    restart_count = sum(
        int(status.restart_count or 0)
        for pod in pods
        for status in (getattr(pod.status, "container_statuses", None) or [])
    )
    ready = (
        int(getattr(deployment.status, "ready_replicas", 0) or 0) == 1
        and len(pods) == 1
        and restart_count == 0
    )
    policy = apps.list_namespaced_deployment(
        config.namespace,
        label_selector=",".join(
            f"{key}={value}" for key, value in sorted(config.policy_selector.items())
        ),
    ).items
    if len(policy) != 1 or not _owned(
        policy[0].metadata, config.identity, allow_openpi=True
    ):
        raise ClusterLiveError("policy Deployment ownership is not proven")
    policy_ready = int(policy[0].status.ready_replicas or 0) == 1
    relay_state: dict[str, Any] = {}
    live_metrics: dict[str, int | float] = {}
    cluster_local_policy_resolved = False
    if len(pods) == 1:
        pod_name = pods[0].metadata.name
        try:
            raw_state = stream(
                core.connect_get_namespaced_pod_exec,
                pod_name,
                config.namespace,
                container="policy-relay",
                command=["/bin/cat", "/var/run/npa-antioch/relay.json"],
                stderr=False,
                stdin=False,
                stdout=True,
                tty=False,
            )
            parsed = json.loads(raw_state)
            allowed = {
                "status",
                "connections",
                "reconnects",
                "forwarded_requests",
                "failures",
                "last_round_trip_ms",
                "last_error_type",
                "last_failed_phase",
            }
            relay_state = {key: parsed.get(key) for key in sorted(allowed)}
        except Exception:
            relay_state = {"status": "unavailable"}
        try:
            logs = core.read_namespaced_pod_log(
                pod_name,
                config.namespace,
                container="antioch-controller",
                tail_lines=2_000,
                timestamps=False,
            )
            live_metrics = _parse_live_metrics(str(logs))
        except ApiException:
            live_metrics = {}
        try:
            resolution = stream(
                core.connect_get_namespaced_pod_exec,
                pod_name,
                config.namespace,
                container="policy-relay",
                command=[
                    "python",
                    "-c",
                    (
                        "import json,socket;"
                        "p=json.load(open('/run/npa-antioch-private/live-bundle/endpoint.json'));"
                        "h=str(p['host']);"
                        "assert h.endswith('.svc') and socket.getaddrinfo(h,443);"
                        "print('ok')"
                    ),
                ],
                stderr=False,
                stdin=False,
                stdout=True,
                tty=False,
            )
            cluster_local_policy_resolved = str(resolution).strip() == "ok"
        except Exception:
            cluster_local_policy_resolved = False
    return {
        "status": "ready" if ready and policy_ready else "not_ready",
        "identity": config.identity,
        "adapter_ready": ready,
        "policy_ready": policy_ready,
        "adapter_pods": len(pods),
        "adapter_restarts": restart_count,
        "policy_service_type": "ClusterIP",
        "cluster_local_policy_resolved": cluster_local_policy_resolved,
        "relay": relay_state,
        "live_metrics": live_metrics,
        "dev_vm_in_data_path": False,
    }


def stop_cluster(
    config: ClusterLiveConfig, *, timeout_seconds: float = 360.0
) -> dict[str, Any]:
    from kubernetes import client, config as kube_config

    kube_config.load_kube_config(
        config_file=config.kubeconfig, context=config.context or None
    )
    apps = client.AppsV1Api()
    deployment = apps.read_namespaced_deployment(config.adapter_name, config.namespace)
    if not _owned(deployment.metadata, config.identity):
        raise ClusterLiveError("refusing to stop an unowned adapter Deployment")
    apps.patch_namespaced_deployment_scale(
        config.adapter_name, config.namespace, {"spec": {"replicas": 0}}
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = apps.read_namespaced_deployment(config.adapter_name, config.namespace)
        if int(current.status.replicas or 0) == 0:
            return {
                "status": "stopped",
                "identity": config.identity,
                "cleanup_order": "scenario_then_service",
            }
        time.sleep(2)
    raise ClusterLiveError("adapter did not finish supported scenario/service cleanup")


def disable_public_rollback_service(config: ClusterLiveConfig) -> dict[str, Any]:
    from kubernetes import client, config as kube_config

    if not config.public_rollback_service_name:
        return {"status": "not_configured"}
    kube_config.load_kube_config(
        config_file=config.kubeconfig, context=config.context or None
    )
    core = client.CoreV1Api()
    service = core.read_namespaced_service(
        config.public_rollback_service_name, config.namespace
    )
    if not _owned(service.metadata, config.identity, allow_openpi=True):
        raise ClusterLiveError("refusing to alter an unowned rollback Service")
    if str(service.spec.type) != "LoadBalancer":
        return {"status": "already_private"}
    core.delete_namespaced_service(
        config.public_rollback_service_name, config.namespace
    )
    return {"status": "disabled", "former_type": "LoadBalancer"}
