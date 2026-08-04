from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from npa.cli.workbench.leisaac import _delete_resources, _install_agent_relay, app


IMAGE = "registry.example/npa-leisaac@sha256:" + "1" * 64
runner = CliRunner()


def test_delete_resources_addresses_each_kubernetes_kind_explicitly(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._kubectl",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    _delete_resources("cluster", "leisaac", "leisaac-live")

    assert calls[0][0][2] == [
        "delete",
        "deployment/leisaac-live",
        "service/leisaac-live-tcp",
        "service/leisaac-live-media",
        "service/leisaac-live-relay",
        "secret/leisaac-live-relay-client",
        "--ignore-not-found=true",
    ]


def test_install_relay_creates_required_agent_directories() -> None:
    class CaptureSSH:
        command = ""

        def run_or_raise(self, command, **_kwargs):
            self.command = command
            return 0, "b" * 64 + "\n", ""

    ssh = CaptureSSH()
    _install_agent_relay(
        ssh,
        run_id="live-relay",
        public_ip="8.8.8.8",
        session_nonce="a" * 64,
    )

    assert "sudo install -d -m 0755 /etc/npa /opt/npa-agent" in ssh.command
    assert "DynamicUser=yes" not in ssh.command  # unit is base64-encoded in transit
    assert "openssl req -x509" in ssh.command


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
        "--backhaul-source-range",
        "1.1.1.1/32",
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
    ingress_calls = []

    def ensure(**kwargs):
        ingress_calls.append(kwargs)
        return SimpleNamespace(changed=True)

    monkeypatch.setattr("npa.cli.workbench.leisaac.ensure_ingress", ensure)
    install_calls = []
    monkeypatch.setattr(
        "npa.cli.workbench.leisaac._install_agent_relay",
        lambda *args, **kwargs: (
            install_calls.append((args, kwargs)) or "f" * 64
        ),
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
    assert applied[0]["spec"]["type"] == "ClusterIP"
    assert applied[1]["kind"] == "Secret"
    assert applied[-1]["kind"] == "Deployment"
    assert ingress_calls == [
        {
            "vm_id": "vm-agent",
            "ports": (47998,),
            "source": "8.8.8.8/32",
            "tool": "leisaac-relay",
            "protocol": "UDP",
        },
        {
            "vm_id": "vm-agent",
            "ports": (48081,),
            "source": "1.1.1.1/32",
            "tool": "leisaac-backhaul",
            "protocol": "TCP",
        },
    ]
    assert install_calls[0][0] == (ssh,)
    assert install_calls[0][1]["public_ip"] == "8.8.4.4"
    assert install_calls[0][1]["session_nonce"] == "a" * 64
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
                "npa.nebius.com/backhaul-source-ranges": "1.1.1.1/32",
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
    assert ingress_removals[1][1]["source"] == "1.1.1.1/32"
    assert ingress_removals[1][1]["protocol"] == "TCP"
    assert k8s_removals == [("cluster", "leisaac", "leisaac-live-relay")]
