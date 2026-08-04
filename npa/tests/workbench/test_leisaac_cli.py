from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from npa.cli.workbench.leisaac import app


IMAGE = "registry.example/npa-leisaac@sha256:" + "1" * 64
runner = CliRunner()


def _args() -> list[str]:
    return [
        "launch",
        "--run-id",
        "live-relay",
        "--image",
        IMAGE,
        "--context",
        "cluster",
        "--namespace",
        "leisaac",
        "--source-range",
        "8.8.8.8/32",
        "--artifact-uri",
        "s3://bucket/checkpoints",
        "--transport",
        "agent-relay",
        "--agent-project",
        "rtxpro",
        "--agent-name",
        "agent",
    ]


def _patch_launch(monkeypatch):
    monkeypatch.setenv("OMNI_KIT_ACCEPT_EULA", "YES")
    monkeypatch.setenv("ISAACSIM_ACCEPT_EULA", "YES")
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._kubectl",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="secret/x", stderr=""),
    )
    applied = []
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._apply",
        lambda _context, _namespace, documents: applied.extend(documents),
    )
    ssh = object()
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._agent_relay_context",
        lambda *_args: ("vm-agent", "8.8.4.4", ssh),
    )
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._node_internal_ip", lambda *_args: "10.96.0.22"
    )
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._relay_nodeports",
        lambda *_args: {"status": 30001, "signal": 30002, "media": 30003},
    )
    ingress_calls = []

    def ensure(**kwargs):
        ingress_calls.append(kwargs)
        return SimpleNamespace(changed=True)

    monkeypatch.setattr("npa.cli.workbench.leisaac.ensure_ingress", ensure)
    install_calls = []
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._install_agent_relay",
        lambda *args, **kwargs: install_calls.append((args, kwargs)),
    )
    monkeypatch.setattr("npa.cli.workbench.leisaac._wait_ready", lambda *_args: None)
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._relay_status",
        lambda *_args: {
            "state": "ready",
            "task": "LeIsaac-SO101-PickOrange-v0",
            "source_commit": "1651c321e9b0c1bb54233211fc7b3cd70d8373d5",
            "session_nonce": "nonce-filled-later",
            "gpu": "NVIDIA-RTX-PRO-6000-Blackwell-Server-Edition",
        },
    )
    manifests = []

    def put(_uri, manifest):
        manifests.append(manifest)
        return "s3://bucket/checkpoints/live-relay/reports/leisaac-session.json"

    monkeypatch.setattr("npa.cli.workbench.leisaac._put_manifest", put)
    monkeypatch.setattr("npa.cli.workbench.leisaac.secrets.token_hex", lambda _n: "a" * 64)
    # Make the attestation nonce exactly match the deterministic nonce above.
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._relay_status",
        lambda *_args: {
            "state": "ready",
            "task": "LeIsaac-SO101-PickOrange-v0",
            "source_commit": "1651c321e9b0c1bb54233211fc7b3cd70d8373d5",
            "session_nonce": "a" * 64,
            "gpu": "NVIDIA-RTX-PRO-6000-Blackwell-Server-Edition",
        },
    )
    return applied, ingress_calls, install_calls, manifests, ssh


def test_launch_agent_relay_wires_private_cluster_public_agent_and_manifest(monkeypatch) -> None:
    applied, ingress_calls, install_calls, manifests, ssh = _patch_launch(monkeypatch)

    result = runner.invoke(app, _args())

    assert result.exit_code == 0, result.output
    assert "transport: agent-relay" in result.output
    assert "public_agent_url: https://8.8.4.4/" in result.output
    assert applied[0]["spec"]["type"] == "NodePort"
    assert applied[-1]["kind"] == "Deployment"
    assert ingress_calls == [
        {
            "vm_id": "vm-agent",
            "ports": (47998,),
            "source": "8.8.8.8/32",
            "tool": "leisaac-relay",
            "protocol": "UDP",
        }
    ]
    assert install_calls[0][0] == (ssh,)
    assert install_calls[0][1]["target_host"] == "10.96.0.22"
    assert manifests[0]["transport"] == "agent-relay"
    assert manifests[0]["signal_host"] == "127.0.0.1"
    assert manifests[0]["media_host"] == "8.8.4.4"


def test_failed_agent_relay_launch_removes_partial_relay_ingress_and_kubernetes(
    monkeypatch,
) -> None:
    _applied, _ingress, _install, _manifests, _ssh = _patch_launch(monkeypatch)
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._install_agent_relay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("install failed")),
    )
    removed_relay = []
    removed_ingress = []
    removed_kubernetes = []
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._remove_agent_relay",
        lambda *args, **kwargs: removed_relay.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac.remove_exact_npa_ingress_for_instance",
        lambda *args, **kwargs: removed_ingress.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._delete_resources",
        lambda *args: removed_kubernetes.append(args),
    )

    result = runner.invoke(app, _args())

    assert result.exit_code == 1
    assert "install failed" in result.output
    assert removed_relay
    assert removed_ingress
    assert removed_kubernetes == [("cluster", "leisaac", "leisaac-live-relay")]


def test_destroy_uses_service_metadata_to_remove_only_its_agent_relay(monkeypatch) -> None:
    relay_service = {
        "metadata": {
            "annotations": {
                "npa.nebius.com/agent-project": "rtxpro",
                "npa.nebius.com/agent-name": "agent",
                "npa.nebius.com/source-ranges": "8.8.8.8/32",
            }
        }
    }
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._kubectl",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(relay_service), stderr=""
        ),
    )
    ssh = object()
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._agent_relay_context",
        lambda *_args: ("vm-agent", "8.8.4.4", ssh),
    )
    relay_removals = []
    ingress_removals = []
    k8s_removals = []
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._remove_agent_relay",
        lambda *args, **kwargs: relay_removals.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac.remove_exact_npa_ingress_for_instance",
        lambda *args, **kwargs: ingress_removals.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._delete_resources",
        lambda *args: k8s_removals.append(args),
    )

    result = runner.invoke(
        app,
        [
            "destroy",
            "--run-id",
            "live-relay",
            "--context",
            "cluster",
            "--namespace",
            "leisaac",
        ],
    )

    assert result.exit_code == 0, result.output
    assert relay_removals == [((ssh,), {"run_id": "live-relay"})]
    assert ingress_removals[0][0] == ("vm-agent",)
    assert ingress_removals[0][1]["source"] == "8.8.8.8/32"
    assert ingress_removals[0][1]["protocol"] == "UDP"
    assert k8s_removals == [("cluster", "leisaac", "leisaac-live-relay")]
