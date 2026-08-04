"""LeIsaac runtime, Kubernetes, assets, EULA, and GPU guardrails."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from npa.agent_backend.leisaac import LEISAAC_CLIENT_JS_SHA256
from npa.workbench.leisaac import (
    GPU_PRODUCT,
    MEDIA_PORT,
    SIGNAL_PORT,
    TURN_PORT,
    TURN_RELAY_PORT,
    TRANSPORT_AGENT_RELAY,
    LeIsaacConfigError,
    deployment_manifest,
    relay_service_manifest,
    relay_client_secret_manifest,
    service_manifests,
    session_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "registry.example/npa-leisaac@sha256:" + "1" * 64
NONCE = "a" * 64


def _session_server_module():
    path = ROOT / "npa/docker/workbench/leisaac/session_server.py"
    spec = importlib.util.spec_from_file_location("npa_leisaac_session_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client_archive(path: Path, client_js: bytes) -> None:
    with tarfile.open(path, mode="w:gz") as bundle:
        for name, content in (
            ("package/dist/omniverse-webrtc-streaming-library.umd.cjs", client_js),
            ("package/LICENSE.txt", b"NVIDIA test license"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))


def test_service_manifests_source_restrict_tcp_and_udp_media() -> None:
    tcp, media = service_manifests(
        run_id="live-1", namespace="default", source_ranges=["8.8.8.8/32"]
    )
    assert tcp["spec"]["loadBalancerSourceRanges"] == ["8.8.8.8/32"]
    assert {port["port"] for port in tcp["spec"]["ports"]} == {8080, SIGNAL_PORT}
    assert all(port["protocol"] == "TCP" for port in tcp["spec"]["ports"])
    assert media["spec"]["loadBalancerSourceRanges"] == ["8.8.8.8/32"]
    assert media["spec"]["ports"] == [
        {
            "name": "media",
            "protocol": "UDP",
            "port": MEDIA_PORT,
            "targetPort": MEDIA_PORT,
        }
    ]


def test_deployment_is_real_rt_core_leisaac_and_operator_eula_runtime_config() -> None:
    deployment = deployment_manifest(
        run_id="live-1",
        namespace="default",
        image=IMAGE,
        media_host="1.1.1.1",
        session_nonce=NONCE,
    )
    pod = deployment["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"nvidia.com/gpu.product": GPU_PRODUCT}
    container = pod["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert container["securityContext"]["runAsNonRoot"] is True
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["OMNI_KIT_ACCEPT_EULA"] == "YES"
    assert env["ISAACSIM_ACCEPT_EULA"] == "YES"
    assert env["NPA_LEISAAC_MEDIA_HOST"] == "1.1.1.1"
    assert "/status" == container["readinessProbe"]["httpGet"]["path"]
    assert "hostPort" not in next(
        port for port in container["ports"] if port["name"] == "media"
    )


def test_agent_relay_service_is_private_clusterip_with_cleanup_metadata() -> None:
    service = relay_service_manifest(
        run_id="live-1",
        namespace="leisaac",
        agent_project="rtxpro",
        agent_name="agent",
        source_ranges=["8.8.8.8/32"],
        turn_peer_source="9.9.8.0/22",
    )

    assert service["spec"]["type"] == "ClusterIP"
    assert "loadBalancerSourceRanges" not in service["spec"]
    assert {item["name"] for item in service["spec"]["ports"]} == {
        "status",
        "signal",
        "media",
    }
    annotations = service["metadata"]["annotations"]
    assert annotations == {
        "npa.nebius.com/agent-project": "rtxpro",
        "npa.nebius.com/agent-name": "agent",
        "npa.nebius.com/source-ranges": "8.8.8.8/32",
        "npa.nebius.com/turn-peer-source": "9.9.8.0/22",
    }


def test_agent_relay_client_is_secret_mounted_as_non_gpu_sidecar() -> None:
    secret = relay_client_secret_manifest(
        run_id="live-relay",
        namespace="leisaac",
        agent_host="8.8.8.8",
        session_nonce=NONCE,
        certificate_sha256="b" * 64,
        auth_user="npa",
        auth_password="secret",
        client_source="print('client')\n",
    )
    assert secret["kind"] == "Secret"
    assert secret["stringData"]["config.json"]
    deployment = deployment_manifest(
        run_id="live-relay",
        namespace="leisaac",
        image=IMAGE,
        media_host="8.8.8.8",
        session_nonce=NONCE,
        relay_client_secret=secret["metadata"]["name"],
    )
    pod = deployment["spec"]["template"]["spec"]
    sidecar = pod["containers"][1]
    assert sidecar["name"] == "agent-relay-client"
    assert "nvidia.com/gpu" not in sidecar["resources"]["requests"]
    assert pod["volumes"][-1]["secret"]["secretName"] == secret["metadata"]["name"]
    media = next(port for port in pod["containers"][0]["ports"] if port["name"] == "media")
    assert media["containerPort"] == MEDIA_PORT
    assert "hostPort" not in media


def test_agent_relay_manifest_keeps_tcp_private_and_media_on_agent_public_ip() -> None:
    manifest = session_manifest(
        run_id="live-relay",
        image=IMAGE,
        signal_host="127.0.0.1",
        media_host="8.8.8.8",
        session_nonce=NONCE,
        transport=TRANSPORT_AGENT_RELAY,
    )

    assert manifest["transport"] == TRANSPORT_AGENT_RELAY
    assert manifest["signal_host"] == "127.0.0.1"
    assert manifest["service_url"] == "http://127.0.0.1:48080"
    assert manifest["media_host"] == "8.8.8.8"
    assert manifest["turn_port"] == TURN_PORT
    assert manifest["turn_relay_port"] == TURN_RELAY_PORT


def test_agent_relay_rejects_non_loopback_signal_or_missing_agent_identity() -> None:
    with pytest.raises(LeIsaacConfigError, match="127.0.0.1"):
        session_manifest(
            run_id="live-relay",
            image=IMAGE,
            signal_host="8.8.8.8",
            media_host="8.8.8.8",
            session_nonce=NONCE,
            transport=TRANSPORT_AGENT_RELAY,
        )
    with pytest.raises(LeIsaacConfigError, match="agent project and name"):
        relay_service_manifest(
            run_id="live-relay",
            namespace="default",
            source_ranges=["8.8.8.8/32"],
        )
    with pytest.raises(LeIsaacConfigError, match="public IPv4 CIDR"):
        relay_service_manifest(
            run_id="live-relay",
            namespace="default",
            agent_project="rtxpro",
            agent_name="agent",
            source_ranges=["8.8.8.8/32"],
            turn_peer_source="2001:4860:4860::8888/32",
        )


def test_manifest_records_exact_real_component_and_provenance() -> None:
    manifest = session_manifest(
        run_id="live-1",
        image=IMAGE,
        signal_host="8.8.8.8",
        media_host="1.1.1.1",
        session_nonce=NONCE,
        expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    )
    assert manifest["task"] == "LeIsaac-SO101-PickOrange-v0"
    assert manifest["teleop_device"] == "keyboard"
    assert manifest["source_commit"] == "1651c321e9b0c1bb54233211fc7b3cd70d8373d5"
    assert manifest["isaac_sim_version"] == "5.1.0.0"
    assert manifest["isaac_lab_version"] == "2.3.2.post1"
    assert manifest["image"] == IMAGE


def test_manifest_has_no_implicit_session_time_limit() -> None:
    manifest = session_manifest(
        run_id="live-unbounded",
        image=IMAGE,
        signal_host="8.8.8.8",
        media_host="1.1.1.1",
        session_nonce=NONCE,
    )

    assert "expires_at" not in manifest


@pytest.mark.parametrize("value", ["", "latest", "x:tag", "x@sha256:bad"])
def test_image_must_be_digest_pinned(value: str) -> None:
    with pytest.raises(LeIsaacConfigError, match="digest"):
        deployment_manifest(
            run_id="live-1",
            namespace="default",
            image=value,
            media_host="1.1.1.1",
            session_nonce=NONCE,
        )


def test_private_or_unrestricted_tcp_endpoints_are_rejected() -> None:
    with pytest.raises(LeIsaacConfigError, match="at least one"):
        service_manifests(run_id="live-1", namespace="default", source_ranges=[])
    with pytest.raises(LeIsaacConfigError, match="public"):
        service_manifests(
            run_id="live-1", namespace="default", source_ranges=["0.0.0.0/0"]
        )
    with pytest.raises(LeIsaacConfigError, match="public"):
        deployment_manifest(
            run_id="live-1",
            namespace="default",
            image=IMAGE,
            media_host="127.0.0.1",
            session_nonce=NONCE,
        )


def test_container_never_bakes_eula_client_or_assets() -> None:
    dockerfile = (ROOT / "npa/docker/workbench/leisaac/Dockerfile").read_text(
        encoding="utf-8"
    )
    server = (ROOT / "npa/docker/workbench/leisaac/session_server.py").read_text(
        encoding="utf-8"
    )
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    assert "ENV OMNI_KIT_ACCEPT_EULA" not in instructions
    assert "ENV ISAACSIM_ACCEPT_EULA" not in instructions
    copy_lines = [
        line
        for line in instructions.splitlines()
        if line.lstrip().startswith(("COPY ", "ADD "))
    ]
    assert not any(
        "so101_follower.usd" in line or "kitchen_with_orange" in line
        for line in copy_lines
    )
    assert "CLIENT_SHA512" in server and "CLIENT_JS_SHA256" in server
    assert "CLIENT_SOURCE_JS_SHA256" in server
    assert "5.6.0" in server
    assert LEISAAC_CLIENT_JS_SHA256 in server
    assert "CLIENT_WSS_PATCH_OLD" in server and "CLIENT_WSS_PATCH_NEW" in server
    assert "source.count(CLIENT_WSS_PATCH_OLD) != 1" in server
    assert "signalingPort=443" in server
    assert 'f"--/app/livestream/publicEndpointPort={MEDIA_PORT}"' in server
    assert 'f"--/app/livestream/fixedHostPort={MEDIA_PORT}"' in server
    assert 'f"--/app/livestream/minHostPort={MEDIA_PORT}"' in server
    assert 'f"--/app/livestream/maxHostPort={MEDIA_PORT}"' in server
    assert 'f"--/app/livestream/port={SIGNAL_PORT}"' in server
    assert "ROBOT_SHA256" in server and "KITCHEN_SHA256" in server
    assert "safe_extract_zip" in server and "safe_extract_client" in server
    assert '"--device=cpu"' in server
    assert 'f"--seed={TELEOP_SEED}"' in server
    assert 'environment["NPA_LEISAAC_BROWSER_TELEOP"] = "1"' in server
    assert "stdin=subprocess.DEVNULL" in server
    assert "start_new_session=True" in server
    assert "tcp_ready(SIGNAL_PORT) and READY_PATH.is_file()" in server
    assert "NPA_LEISAAC_INPUT_COUNTER" in server
    assert "feetech-servo-sdk" in dockerfile and "-m pip check" in dockerfile
    assert "git -C /opt/leisaac apply --check --unidiff-zero" in dockerfile
    assert os.access(ROOT / "npa/docker/workbench/leisaac/build.sh", os.X_OK)


def test_observability_patch_is_exact_and_records_real_upstream_input() -> None:
    patch = ROOT / "npa/docker/workbench/leisaac/upstream-observability.patch"
    server = _session_server_module()

    assert hashlib.sha256(patch.read_bytes()).hexdigest() == (
        server.UPSTREAM_OBSERVABILITY_PATCH_SHA256
    )
    source = patch.read_text(encoding="utf-8")
    assert "SO101Keyboard(Device)" in source
    assert "KeyboardEventType.KEY_PRESS" in source
    assert "self._delta_action +=" in source
    assert "NPA_LEISAAC_INPUT_COUNTER" in source
    assert "NPA_LEISAAC_READY_PATH" in source
    assert "NPA_LEISAAC_BROWSER_TELEOP" in source
    assert "env_cfg.observations.policy.wrist = None" in source
    assert "env_cfg.observations.policy.front = None" in source


def test_health_reads_upstream_keyboard_counter(tmp_path: Path) -> None:
    server = _session_server_module()
    counter = tmp_path / "input-events"
    counter.write_text("13\n", encoding="utf-8")
    server.INPUT_COUNTER_PATH = counter

    health = server.health_document()

    assert health["input_events"] == 13
    assert health["physics_device"] == "cpu"
    assert health["render_device"] == "cuda"
    assert health["seed"] == 42


def test_liveness_restarts_only_cold_reset_and_preserves_warm_retry() -> None:
    server = _session_server_module()

    server.STATE.update(state="starting", pid=0, warm_retry=False)
    assert server.liveness_status() == 200
    server.STATE.update(state="starting", pid=42, warm_retry=False)
    assert server.liveness_status() == 503
    server.STATE.update(state="starting", pid=42, warm_retry=True)
    assert server.liveness_status() == 200
    server.STATE.update(state="ready", pid=42)
    assert server.liveness_status() == 200
    server.STATE.update(state="failed", pid=0)
    assert server.liveness_status() == 503


def test_agent_bootstrap_installs_turn_without_baking_session_configuration() -> None:
    agent = (ROOT / "npa/src/npa/cli/agent.py").read_text(encoding="utf-8")
    ui = (ROOT / "npa/src/npa/cli/agent_ui.html").read_text(encoding="utf-8")

    assert "ca-certificates coturn" in agent
    assert "leisaac-turn.conf" not in agent
    assert 'iceTransportPolicy: "relay"' in ui
    assert "installLeIsaacPeerConnection(status)" in ui


def test_client_transport_patch_is_exact_hash_verified_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _session_server_module()
    source = b"prefix" + server.CLIENT_WSS_PATCH_OLD + b"suffix"
    archive = tmp_path / "client.tgz"
    destination = tmp_path / "client"
    _client_archive(archive, source)
    monkeypatch.setattr(
        server, "CLIENT_SOURCE_JS_SHA256", hashlib.sha256(source).hexdigest()
    )

    server.safe_extract_client(archive, destination)

    expected = source.replace(server.CLIENT_WSS_PATCH_OLD, server.CLIENT_WSS_PATCH_NEW)
    assert (destination / "index.js").read_bytes() == expected

    ambiguous = source + server.CLIENT_WSS_PATCH_OLD
    _client_archive(archive, ambiguous)
    monkeypatch.setattr(
        server, "CLIENT_SOURCE_JS_SHA256", hashlib.sha256(ambiguous).hexdigest()
    )
    with pytest.raises(RuntimeError, match="WSS patch anchor mismatch"):
        server.safe_extract_client(archive, destination)


def test_build_script_supports_repository_python_310() -> None:
    script = (ROOT / "npa/docker/workbench/leisaac/build.sh").read_text(
        encoding="utf-8"
    )
    assert "except ModuleNotFoundError:" in script
    assert "import tomli as tomllib" in script
