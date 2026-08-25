from __future__ import annotations

import base64
import io
import json
import os
import tarfile
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


def test_config_archive_preserves_owner_only_nested_assignment_state(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    root = Path(config.antioch_config_dir)
    ssh = root / "ssh"
    ssh.mkdir(mode=0o700)
    private_key = ssh / "assigned-machine"
    private_key.write_bytes(b"private-runtime-state")
    os.chmod(private_key, 0o600)
    lock = root / "session.lock"
    lock.touch(mode=0o600)
    os.chmod(lock, 0o600)

    first = cluster_deploy._config_archive(root)["config.tar"]
    second = cluster_deploy._config_archive(root)["config.tar"]
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r") as archive:
        members = {member.name: member for member in archive.getmembers()}
        assert set(members) == {
            "config.json",
            "session.lock",
            "ssh",
            "ssh/assigned-machine",
        }
        assert members["ssh"].isdir()
        assert members["ssh"].mode == 0o700
        assert members["ssh/assigned-machine"].isfile()
        assert members["ssh/assigned-machine"].mode == 0o600
        extracted = archive.extractfile(members["ssh/assigned-machine"])
        assert extracted is not None
        assert extracted.read() == b"private-runtime-state"


def test_config_archive_rejects_non_owner_only_nested_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    nested = Path(config.antioch_config_dir) / "ssh"
    nested.mkdir(mode=0o755)
    os.chmod(nested, 0o755)
    with pytest.raises(cluster_deploy.ClusterLiveError, match="owner-only"):
        cluster_deploy._config_archive(Path(config.antioch_config_dir))


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
    assert pod["terminationGracePeriodSeconds"] >= 1_100
    init = pod["initContainers"][0]
    init_command = init["command"][-1]
    volume_names = [volume["name"] for volume in pod["volumes"]]
    assert len(volume_names) == len(set(volume_names))
    init_mount_names = [mount["name"] for mount in init["volumeMounts"]]
    assert len(init_mount_names) == len(set(init_mount_names))
    assert "tar --extract --file /sources/config/config.tar" in init_command
    assert "cp -L /sources/bundle/*" in init_command
    assert init["securityContext"]["capabilities"] == {
        "drop": ["ALL"],
        "add": ["CHOWN"],
    }
    init_mounts = {mount["name"]: mount["mountPath"] for mount in init["volumeMounts"]}
    assert init_mounts["state"] == "/state"
    assert init_mounts["runtime"] == "/runtime"
    assert init_mounts["runtime-cache"] == "/runtime-cache"
    assert (
        "chown -R 10001:10001 /private /state /runtime /runtime-cache" in init_command
    )
    assert "cp -a" not in init_command
    controller, relay = pod["containers"]
    controller_mounts = {mount["name"]: mount for mount in controller["volumeMounts"]}
    relay_mounts = {mount["name"]: mount for mount in relay["volumeMounts"]}
    assert controller_mounts["private"]["readOnly"] is False
    assert relay_mounts["private"]["readOnly"] is True
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
    assert adapter_policy["egress"][-1] == {
        "to": [
            {
                "ipBlock": {
                    "cidr": cluster_deploy.UNRESTRICTED_VENDOR_EGRESS_CIDR
                }
            }
        ],
        "ports": [
            {"protocol": "TCP", "port": 22},
            {"protocol": "TCP", "port": 443},
            {"protocol": "TCP", "port": 8443},
        ],
    }


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


@pytest.mark.parametrize(
    "case,match",
    [
        ("unowned_policy", "policy Deployment ownership"),
        ("unbound_pvc", "PVC is not Bound"),
        ("unowned_auth", "authentication Secret ownership"),
    ],
)
def test_apply_cluster_guards_ownership_and_dependencies_beyond_selector(
    case: str,
    match: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    from kubernetes import client

    policy_labels = (
        {}
        if case == "unowned_policy"
        else {"app.kubernetes.io/managed-by": cluster_deploy.LIVE_MANAGED_BY}
    )
    deployment = SimpleNamespace(
        metadata=SimpleNamespace(labels=policy_labels),
        spec=SimpleNamespace(
            selector=SimpleNamespace(match_labels=config.policy_selector)
        ),
    )
    pvc = SimpleNamespace(
        status=SimpleNamespace(phase="Pending" if case == "unbound_pvc" else "Bound")
    )
    auth = SimpleNamespace(
        metadata=SimpleNamespace(
            labels=(
                {}
                if case == "unowned_auth"
                else {"app.kubernetes.io/managed-by": cluster_deploy.LIVE_MANAGED_BY}
            )
        ),
        data={"api-key": base64.b64encode(b"a" * 48).decode()},
    )
    core = SimpleNamespace(
        read_namespace=lambda **_kwargs: object(),
        read_namespaced_persistent_volume_claim=lambda **_kwargs: pvc,
        read_namespaced_secret=lambda **_kwargs: auth,
    )
    apps = SimpleNamespace(
        list_namespaced_deployment=lambda **_kwargs: SimpleNamespace(
            items=[deployment]
        )
    )
    monkeypatch.setattr("kubernetes.config.load_kube_config", lambda **_kwargs: None)
    monkeypatch.setattr(client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(client, "AppsV1Api", lambda: apps)
    monkeypatch.setattr(client, "NetworkingV1Api", lambda: object())
    with pytest.raises(cluster_deploy.ClusterLiveError, match=match):
        cluster_deploy.apply_cluster(config)


def test_cluster_status_reports_sanitized_probe_exception_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    from kubernetes import client

    owned = SimpleNamespace(labels=cluster_deploy._labels(config))
    deployment = SimpleNamespace(
        metadata=owned, status=SimpleNamespace(ready_replicas=1)
    )
    policy = SimpleNamespace(
        metadata=SimpleNamespace(
            labels={"app.kubernetes.io/managed-by": cluster_deploy.LIVE_MANAGED_BY}
        ),
        status=SimpleNamespace(ready_replicas=1),
        spec=SimpleNamespace(
            selector=SimpleNamespace(match_labels=config.policy_selector)
        ),
    )
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="adapter-pod"),
        status=SimpleNamespace(container_statuses=[]),
    )
    apps = SimpleNamespace(
        read_namespaced_deployment=lambda *_args, **_kwargs: deployment,
        list_namespaced_deployment=lambda *_args, **_kwargs: SimpleNamespace(
            items=[policy]
        ),
    )
    core = SimpleNamespace(
        list_namespaced_pod=lambda *_args, **_kwargs: SimpleNamespace(items=[pod]),
        connect_get_namespaced_pod_exec=object(),
        read_namespaced_pod_log=lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr("kubernetes.config.load_kube_config", lambda **_kwargs: None)
    monkeypatch.setattr(client, "AppsV1Api", lambda: apps)
    monkeypatch.setattr(client, "CoreV1Api", lambda: core)
    calls = 0

    def failing_stream(*_args, **_kwargs):  # noqa: ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("private relay endpoint must not leak")
        raise RuntimeError("private DNS target must not leak")

    monkeypatch.setattr("kubernetes.stream.stream", failing_stream)
    result = cluster_deploy.cluster_status(config)
    assert result["probe_diagnostics"] == {
        "relay_state": {"status": "failed", "exception_class": "ValueError"},
        "policy_dns": {"status": "failed", "exception_class": "RuntimeError"},
    }
    rendered = json.dumps(result)
    assert "private relay" not in rendered
    assert "private DNS" not in rendered


@pytest.mark.parametrize("cleanup_status,stops", [("stopped", True), ("cleanup_failed", False)])
def test_stop_cluster_requires_supported_remote_cleanup_evidence(
    cleanup_status: str,
    stops: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    from kubernetes import client

    deployment = SimpleNamespace(
        metadata=SimpleNamespace(labels=cluster_deploy._labels(config))
    )
    scales: list[dict[str, object]] = []
    apps = SimpleNamespace(
        read_namespaced_deployment=lambda *_args, **_kwargs: deployment,
        patch_namespaced_deployment_scale=lambda *_args, **kwargs: scales.append(kwargs),
    )
    pod = SimpleNamespace(metadata=SimpleNamespace(name="adapter-pod"))
    core = SimpleNamespace(
        list_namespaced_pod=lambda *_args, **_kwargs: SimpleNamespace(items=[pod]),
        connect_get_namespaced_pod_exec=object(),
    )
    monkeypatch.setattr("kubernetes.config.load_kube_config", lambda **_kwargs: None)
    monkeypatch.setattr(client, "AppsV1Api", lambda: apps)
    monkeypatch.setattr(client, "CoreV1Api", lambda: core)
    replies = iter(("", json.dumps({"status": cleanup_status})))
    monkeypatch.setattr("kubernetes.stream.stream", lambda *_args, **_kwargs: next(replies))
    if stops:
        result = cluster_deploy.stop_cluster(config, timeout_seconds=1)
        assert result["remote_terminal_evidence"] == "supported-controller-cleanup"
        assert len(scales) == 1
    else:
        with pytest.raises(cluster_deploy.ClusterLiveError, match="cleanup failed"):
            cluster_deploy.stop_cluster(config, timeout_seconds=1)
        assert scales == []


def test_policy_lookup_uses_pod_selector_not_deployment_metadata() -> None:
    expected = {"app": "policy", "npa.nebius.ai/cleanup-owner": "owned-run"}
    deployment = SimpleNamespace(
        metadata=SimpleNamespace(labels={"app": "different-metadata"}),
        spec=SimpleNamespace(selector=SimpleNamespace(match_labels=dict(expected))),
    )
    assert cluster_deploy._matching_policy_deployments([deployment], expected) == [
        deployment
    ]


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
