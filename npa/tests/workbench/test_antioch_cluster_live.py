from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from npa.workbench.antioch import cluster_deploy, cluster_runtime


def _config(tmp_path: Path, **updates: object) -> cluster_deploy.ClusterLiveConfig:
    config_dir = tmp_path / "antioch-config"
    config_dir.mkdir(mode=0o700)
    (config_dir / "config.json").write_text("{}", encoding="utf-8")
    os.chmod(config_dir / "config.json", 0o600)
    project = tmp_path / "project-id"
    project.write_text("private-project-id\n", encoding="utf-8")
    os.chmod(project, 0o600)
    values: dict[str, object] = {
        "workflow_run": "workflow-a",
        "state_id": "antioch-live-a",
        "kubeconfig": str(tmp_path / "kubeconfig"),
        "namespace": "workbench",
        "adapter_image": "registry.invalid/npa-antioch@sha256:" + "a" * 64,
        "policy_selector": {"app": "openpi-policy"},
        "policy_network_policy_name": "openpi-policy",
        "policy_auth_secret_name": "openpi-auth",
        "policy_tls_secret_name": "openpi-tls",
        "policy_cache_pvc_name": "openpi-cache",
        "antioch_config_dir": str(config_dir),
        "antioch_project_id_file": str(project),
        "kubelet_source_cidrs": ["192.0.2.10/32"],
    }
    values.update(updates)
    return cluster_deploy.ClusterLiveConfig.model_validate(values)


def test_private_config_requires_mode_0600_and_per_state_identity(
    tmp_path: Path,
) -> None:
    first = _config(tmp_path)
    second = first.model_copy(update={"state_id": "antioch-live-b"})
    assert first.identity != second.identity
    path = tmp_path / "runtime.json"
    path.write_text(first.model_dump_json(), encoding="utf-8")
    os.chmod(path, 0o644)
    with pytest.raises(cluster_deploy.ClusterLiveError, match="0600"):
        cluster_deploy.load_private_config(path)
    os.chmod(path, 0o600)
    assert cluster_deploy.load_private_config(path) == first


def test_cluster_live_requires_digest_pinned_adapter(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="sha256"):
        _config(tmp_path, adapter_image="registry.invalid/npa-antioch:latest")


def test_cluster_live_terms_acceptance_is_process_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.delenv("NPA_ANTIOCH_ACCEPT_TERMS", raising=False)
    with pytest.raises(cluster_deploy.ClusterLiveError, match="exact required value"):
        cluster_deploy._terms_acceptance()
    monkeypatch.setenv("NPA_ANTIOCH_ACCEPT_TERMS", "yes")
    with pytest.raises(cluster_deploy.ClusterLiveError, match="exact required value"):
        cluster_deploy._terms_acceptance()
    monkeypatch.setenv("NPA_ANTIOCH_ACCEPT_TERMS", "YES")
    assert cluster_deploy._terms_acceptance() == b"YES"
    assert "antioch_terms_file" not in type(config).model_fields


def test_public_manifests_keep_vm_out_and_policy_cluster_local(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifests = cluster_deploy.build_public_manifests(config)
    service = manifests["policy_service"]
    assert service["spec"]["type"] == "ClusterIP"
    assert "externalIPs" not in service["spec"]
    pod = manifests["adapter_deployment"]["spec"]["template"]["spec"]
    assert {item["name"] for item in pod["containers"]} == {
        "antioch-controller",
        "policy-relay",
    }
    assert pod["automountServiceAccountToken"] is False
    assert pod["terminationGracePeriodSeconds"] >= 300
    init_command = pod["initContainers"][0]["command"][-1]
    assert "cp -L /sources/config/*" in init_command
    assert "cp -L /sources/bundle/*" in init_command
    assert "cp -a" not in init_command
    controller, relay = pod["containers"]
    assert "cluster_runtime" in " ".join(controller["command"])
    assert "14400" in controller["command"]
    assert "antioch.relay" in " ".join(relay["command"])
    assert "18444" in relay["command"]
    rendered = json.dumps(manifests, sort_keys=True)
    assert "LoadBalancer" not in rendered
    assert "hostNetwork" not in rendered
    assert "private-project-id" not in rendered
    assert "api-key" not in rendered
    assert "CERT_NONE" not in rendered

    ingress = manifests["policy_network_policy"]["spec"]["ingress"]
    assert ingress[0]["from"] == [
        {"podSelector": {"matchLabels": cluster_deploy._labels(config)}}
    ]
    assert ingress[0]["ports"] == [{"protocol": "TCP", "port": 8443}]
    adapter_policy = manifests["adapter_network_policy"]["spec"]
    assert adapter_policy["ingress"] == []
    assert adapter_policy["policyTypes"] == ["Ingress", "Egress"]


def test_cluster_runtime_probe_is_fail_closed(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    assert cluster_runtime.probe(state, component="controller") == 1
    state.write_text(json.dumps({"status": "starting"}), encoding="utf-8")
    assert cluster_runtime.probe(state, component="controller") == 1
    state.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    assert cluster_runtime.probe(state, component="controller") == 0
    state.write_text(json.dumps({"status": "reconnecting"}), encoding="utf-8")
    assert cluster_runtime.probe(state, component="relay") == 1
    state.write_text(json.dumps({"status": "connected"}), encoding="utf-8")
    assert cluster_runtime.probe(state, component="relay") == 0


def test_live_metrics_parser_uses_latest_complete_numeric_line() -> None:
    logs = """\
NPA_OPENPI_METRICS frames=10 round_trips=9 latency_p95_ms=123.5
NPA_OPENPI_METRICS frames=broken round_trips=10
NPA_OPENPI_METRICS frames=12 round_trips=11 latency_p95_ms=125.25
"""
    assert cluster_deploy._parse_live_metrics(logs) == {
        "frames": 12,
        "round_trips": 11,
        "latency_p95_ms": 125.25,
    }


def test_retained_openpi_cleanup_owner_is_a_narrow_adoption_proof() -> None:
    metadata = SimpleNamespace(
        labels={"npa.nebius.ai/cleanup-owner": "owned-retained-run"}
    )
    assert cluster_deploy._owned(
        metadata,
        "new-live-identity",
        allow_openpi=True,
        openpi_cleanup_owner="owned-retained-run",
    )
    assert not cluster_deploy._owned(
        metadata,
        "new-live-identity",
        allow_openpi=True,
        openpi_cleanup_owner="different-run",
    )


def test_apply_refuses_ambiguous_policy_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    from kubernetes import client

    monkeypatch.setattr("kubernetes.config.load_kube_config", lambda **_kwargs: None)
    core = SimpleNamespace(read_namespace=lambda **_kwargs: object())
    apps = SimpleNamespace(
        list_namespaced_deployment=lambda **_kwargs: SimpleNamespace(items=[])
    )
    monkeypatch.setattr(client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(client, "AppsV1Api", lambda: apps)
    monkeypatch.setattr(client, "NetworkingV1Api", lambda: object())
    with pytest.raises(cluster_deploy.ClusterLiveError, match="exactly one"):
        cluster_deploy.apply_cluster(config)


def test_source_uses_only_supported_antioch_live_commands() -> None:
    live = Path(cluster_runtime.__file__).read_text(encoding="utf-8")
    helper = (
        Path(cluster_runtime.__file__).with_name("live.py").read_text(encoding="utf-8")
    )
    assert "Rome" not in live
    assert "requests." not in live
    for command in (
        "services_build",
        "services_up",
        "services_exec",
        "services_copy",
        "services_down",
    ):
        assert command in helper
    assert '"scenario",\n            "run"' in helper


def test_adapter_build_is_base_pinned_and_records_exact_revision() -> None:
    root = Path(cluster_runtime.__file__).resolve().parents[4]
    dockerfile = (root / "docker/workbench/antioch/Dockerfile").read_text()
    build = (root / "docker/workbench/antioch/build.sh").read_text()
    assert "FROM python:3.12-slim-bookworm@sha256:" in dockerfile
    assert 'org.opencontainers.image.revision="${NPA_REVISION}"' in dockerfile
    assert '--build-arg "NPA_REVISION=${REVISION}"' in build
