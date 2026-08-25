from __future__ import annotations

import importlib.util
import hashlib
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
import yaml

from npa.workbench.antioch import live
from npa.workbench.antioch import relay as live_relay
from npa.workbench.antioch import live_reconcile
from npa.workbench.antioch.vendor_cli import AntiochCliError
from npa.workflows.byof.openpi_live import _certificate

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "npa/examples/antioch-openpi-live"


def test_live_example_uses_only_runtime_project_identity() -> None:
    manifest = yaml.safe_load((EXAMPLE / "antioch.yaml").read_text(encoding="utf-8"))
    assert manifest["id"] == "replace-at-runtime"
    sim = manifest["services"]["sim"]
    assert "image" not in sim
    assert sim["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert sim["ports"] == [
        {"name": "policy-relay", "target": 8444, "published": 18444}
    ]
    assert sim["watch"] == [
        {"action": "rebuild", "path": "Dockerfile"},
        {"action": "rebuild", "path": "src/relay_bridge.py"},
    ]
    rendered = (EXAMPLE / "antioch.yaml").read_text(encoding="utf-8")
    assert not re.search(r"(?:project|tenant|cluster)-[a-z0-9]+", rendered)


def test_live_scenario_is_real_bounded_and_fail_closed() -> None:
    source = (EXAMPLE / "src/scenario.py").read_text(encoding="utf-8")
    compile(source, "antioch-openpi-live-scenario", "exec")
    for contract in (
        "ACTION_SHAPE = (15, 8)",
        "MAX_RESPONSE_AGE_SECONDS",
        "MAX_JOINT_STEP",
        "GRIPPER_JOINT_MAX = 0.04",
        'raise ActionValidationError("gripper_range")',
        "isinstance(exc, ActionValidationError)",
        "ssl.create_default_context",
        'CLIENT_ROOT = Path("/tmp/npa-live-client-current")',
        'return "wss://127.0.0.1:8444", token, context',
        '"X-NPA-Relay-Role": "simulation"',
        'logger.image("camera/exterior"',
        'logger.image("camera/wrist"',
        'logger.scalar("decision/observation_sequence"',
        'logger.scalar("decision/policy_requests"',
        'logger.scalar("decision/policy_in_flight"',
        'logger.scalar("decision/round_trips"',
        'logger.scalar("decision/inference_latency_ms"',
        'logger.scalar("decision/safe_hold"',
        'logger.scalar("decision/reconnects"',
        'logger.scalar("decision/safe_targets_applied"',
        "ArticulationAction",
        'enable_extension("isaacsim.robot.manipulators.examples")',
        "NPA_OPENPI_ROUND_TRIP",
        "NPA_OPENPI_SAFE_HOLD",
        "NPA_OPENPI_LOOP_READY",
        "NPA_OPENPI_FIRST_FRAME",
        "NPA_OPENPI_REQUEST",
        "NPA_OPENPI_APPLIED",
        "waiting for camera frames",
        'ThreadPoolExecutor(max_workers=1, thread_name_prefix="openpi-policy")',
    ):
        assert contract in source
    assert "WebsocketClientPolicy(" not in source
    assert "verify_mode = ssl.CERT_NONE" not in source
    assert "while True:" in source

    relay = (ROOT / "npa/src/npa/workbench/antioch/relay.py").read_text(
        encoding="utf-8"
    )
    assert 'additional_headers={"Authorization": f"Api-Key {policy_token}"}' in relay
    assert '"X-NPA-Relay-Role": "operator"' in relay
    assert "proxy=None" in relay
    assert "port != 443" in relay
    assert "ssl.create_default_context" in relay
    assert "CERT_NONE" not in relay

    bridge = (EXAMPLE / "src/relay_bridge.py").read_text(encoding="utf-8")
    compile(bridge, "antioch-openpi-live-relay-bridge", "exec")
    assert "ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)" in bridge
    assert '"0.0.0.0",\n        8444,' in bridge
    assert "hmac.compare_digest" in bridge
    assert 'ROLES = frozenset({"operator", "simulation"})' in bridge


def test_live_protocol_codec_round_trips_arrays_and_rejects_objects() -> None:
    pytest.importorskip("msgpack")
    path = EXAMPLE / "src/openpi_protocol.py"
    spec = importlib.util.spec_from_file_location("openpi_protocol_test", path)
    assert spec is not None and spec.loader is not None
    codec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codec)

    value = np.arange(24, dtype=np.float32).reshape(3, 8)
    decoded = codec.unpackb(codec.Packer().pack({"actions": value}))
    np.testing.assert_array_equal(decoded["actions"], value)
    with pytest.raises(ValueError, match="unsupported array dtype"):
        codec.Packer().pack(np.asarray([object()], dtype=object))


def test_live_sim_image_contains_only_protocol_dependencies() -> None:
    dockerfile = (EXAMPLE / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("FROM antioch-engine/isaac-sim-6.0.1:0.3.63\n")
    assert 'npa.antioch.live-transport="declared-port-double-wss-v1"' in dockerfile
    assert '"msgpack==1.1.1"' in dockerfile
    assert '"websockets==15.0.1"' in dockerfile
    assert "/workspace/project" in dockerfile
    assert "/tmp/npa-home/.cache \\" in dockerfile
    assert "/tmp/npa-home/.cache/ov" in dockerfile
    assert (
        "/usr/local/lib/python3.12/dist-packages/isaacsim/kit/cache/DerivedDataCache"
        in dockerfile
    )
    assert (
        "/usr/local/lib/python3.12/dist-packages/isaacsim/kit/data/documents/Kit/"
        "apps/Isaac-Sim Python/scripts"
        in dockerfile
    )
    assert (
        "/usr/local/lib/python3.12/dist-packages/isaacsim/kit/data/documents/Kit/"
        "shared"
        in dockerfile
    )
    assert "ENV HOME=/tmp/npa-home" in dockerfile
    assert "COPY --chown=1000:1000 src/ /workspace/project/src/" in dockerfile
    assert "USER 1000:1000" in dockerfile
    assert (
        'ENTRYPOINT ["/usr/local/bin/python", "/workspace/project/src/relay_bridge.py"'
        in dockerfile
    )
    assert '"--wait-for-bundle"]' in dockerfile
    assert 'args.service_command not in ([], ["sleep", "infinity"])' in (
        EXAMPLE / "src/relay_bridge.py"
    ).read_text(encoding="utf-8")
    assert "git clone" not in dockerfile
    assert "checkpoint" not in dockerfile.lower()


def test_live_example_documents_supported_renewal_boundary() -> None:
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    assert "antioch services cp" in readme
    assert "finite supported timeout" in readme
    assert "resets the simulated episode" in readme
    assert "not one infinitely lived simulator process" in readme


def test_runtime_staging_keeps_private_project_id_out_of_source(tmp_path: Path) -> None:
    destination = tmp_path / "runtime"
    live._stage_project(EXAMPLE, destination, "assigned-project-for-test")
    staged = yaml.safe_load((destination / "antioch.yaml").read_text(encoding="utf-8"))
    source = yaml.safe_load((EXAMPLE / "antioch.yaml").read_text(encoding="utf-8"))
    assert staged["id"] == "assigned-project-for-test"
    assert source["id"] == "replace-at-runtime"
    assert (destination / "antioch.yaml").stat().st_mode & 0o777 == 0o600


def test_supervisor_has_finite_run_boundary_but_no_total_limit(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    for name in ("scenario.py", "openpi_protocol.py", "relay_bridge.py"):
        (source_dir / name).write_text("# reviewed source\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in live.REQUIRED_BUNDLE_FILES:
        (bundle / name).write_text(f"private-{name}\n", encoding="utf-8")
    script = tmp_path / "supervise.sh"
    live._write_supervisor(
        script,
        cli_path=Path("/opt/antioch/bin/antioch"),
        python_path=Path("/opt/npa/bin/python"),
        client_bundle=bundle,
        stop_file=tmp_path / ".stop",
        active_state_path=tmp_path / "active-run.json",
        scenario_timeout_seconds=14_400,
    )
    source = script.read_text(encoding="utf-8")
    assert "while [ ! -f" in source
    assert "scenario run --scenario openpi_droid_live" in source
    assert "--timeout 14400 --stream --verbose" in source
    assert "NPA_ANTIOCH_RENEWAL" in source
    assert "npa.workbench.antioch.live_reconcile" in source
    assert "NPA_ANTIOCH_RECONCILED_TERMINAL" in source
    assert "services cp" in source
    assert "services exec sim /bin/sh -lc" in source
    assert "npa-live-supervisor-source-" in source
    assert "sha256sum /workspace/project/src/scenario.py" in source
    assert "sha256sum /workspace/project/src/openpi_protocol.py" in source
    assert "install -m 0644" in source
    assert "services up --json" in source
    assert "services build --service sim --json" in source
    assert "services exec sim /bin/true" in source
    assert "NPA_ANTIOCH_SERVICE_NOT_READY" in source
    assert "npa-live-client-generation-" in source
    assert "mv -Tf" in source
    assert "sleep 15" in source
    assert "timeout 14400s" not in source
    assert script.stat().st_mode & 0o777 == 0o700
    subprocess.run(["sh", "-n", str(script)], check=True)


def test_relay_supervisor_has_no_credential_values_in_arguments(tmp_path: Path) -> None:
    script = tmp_path / "relay-supervise.sh"
    live._write_relay_supervisor(
        script,
        python_path=Path("/opt/npa/bin/python"),
        client_bundle=tmp_path / "private-bundle",
        stop_file=tmp_path / ".stop",
        state_path=tmp_path / "relay-state.json",
    )
    source = script.read_text(encoding="utf-8")
    assert "npa.workbench.antioch.relay" in source
    assert "--local-port 18444" in source
    assert "api-key" not in source
    assert "Authorization" not in source
    assert "while [ ! -f" in source
    subprocess.run(["sh", "-n", str(script)], check=True)


def test_bridge_supervisor_uses_short_health_exec_calls(tmp_path: Path) -> None:
    script = tmp_path / "bridge-supervise.sh"
    live._write_bridge_supervisor(
        script,
        cli_path=Path("/opt/antioch/bin/antioch"),
        stop_file=tmp_path / ".stop",
    )
    source = script.read_text(encoding="utf-8")
    assert "services exec sim /usr/local/bin/python -c" in source
    assert "socket.create_connection" in source
    assert "NPA_ANTIOCH_BRIDGE_HEALTHY" in source
    assert "NPA_ANTIOCH_BRIDGE_NOT_READY" in source
    assert "relay_bridge.py" not in source
    assert "nohup" not in source
    assert "api-key" not in source
    assert "while [ ! -f" in source
    subprocess.run(["sh", "-n", str(script)], check=True)


def test_relay_certificate_covers_verified_localhost_endpoint() -> None:
    from cryptography import x509

    _ca, certificate, _key = live._relay_certificate()
    names = (
        x509.load_pem_x509_certificate(certificate)
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )

    assert names.get_values_for_type(x509.IPAddress)[0].compressed == "127.0.0.1"
    assert names.get_values_for_type(x509.DNSName) == []


def test_client_bundle_requires_private_files(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name, content in {
        "ca.crt": "certificate",
        "api-key": "x" * 48,
        "endpoint.json": '{"scheme":"wss","host":"example.invalid","port":443}',
    }.items():
        path = bundle / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
    live._validate_upstream_bundle(bundle)
    (bundle / "api-key").chmod(0o644)
    try:
        live._validate_upstream_bundle(bundle)
    except live.AntiochLiveError as exc:
        assert "group/world" in str(exc)
    else:
        raise AssertionError("a public client credential was accepted")


def test_runtime_bundle_adds_private_relay_identity(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    for name, content in {
        "ca.crt": "certificate",
        "api-key": "x" * 48,
        "endpoint.json": '{"scheme":"wss","host":"example.invalid","port":443}',
    }.items():
        path = upstream / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    destination = tmp_path / "runtime-bundle"
    live._prepare_runtime_bundle(upstream, destination)

    assert set(path.name for path in destination.iterdir()) == set(
        live.REQUIRED_BUNDLE_FILES
    )
    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(
        (destination / name).stat().st_mode & 0o777 == 0o600
        for name in live.REQUIRED_BUNDLE_FILES
    )
    assert "BEGIN PRIVATE KEY" in (destination / "relay-server.key").read_text(
        encoding="utf-8"
    )
    assert "example.invalid" not in (destination / "relay-server.crt").read_text(
        encoding="utf-8"
    )


def test_initial_bundle_staging_recovers_from_service_recreation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in live.REQUIRED_BUNDLE_FILES:
        (bundle / name).write_text("private", encoding="utf-8")
    calls: list[str] = []

    class Cli:
        installs = 0

        def services_exec(self, _runtime, _service, command):  # noqa: ANN001, ANN202
            calls.append("exec:" + str(command[0]))
            if command[0] == "install":
                self.installs += 1

        def services_copy(self, _runtime, source, _destination):  # noqa: ANN001, ANN202
            calls.append("copy:" + source.name)
            if self.installs == 1:
                raise AntiochCliError("container recreated")

    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    cli = Cli()
    live._stage_private_bundle(
        cli,  # type: ignore[arg-type]
        runtime=tmp_path / "runtime",
        client_bundle=bundle,
        attempts=2,
    )

    assert cli.installs == 2
    assert calls.count("copy:ca.crt") == 2
    assert calls[-1] == "exec:/bin/sh"


def test_runtime_source_is_staged_through_supported_service_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    for name in ("scenario.py", "openpi_protocol.py", "relay_bridge.py"):
        (source / name).write_text("# reviewed public source\n", encoding="utf-8")
    calls: list[tuple[str, object]] = []

    class Cli:
        def services_exec(self, _runtime, _service, command):  # noqa: ANN001, ANN202
            calls.append(("exec", command))
            if command[0] == "sha256sum":
                return hashlib.sha256(b"# reviewed public source\n").hexdigest()
            return ""

        def services_copy(self, _runtime, path, destination):  # noqa: ANN001, ANN202
            calls.append(("copy", (path.name, destination)))

    live._stage_runtime_source(Cli(), runtime=tmp_path)  # type: ignore[arg-type]

    assert calls[0][0] == "exec"
    copies = [call for call in calls if call[0] == "copy"]
    assert copies[0][1][0] == "scenario.py"
    assert copies[0][1][1].startswith("sim:/tmp/npa-live-source-")
    assert copies[0][1][1].endswith("/scenario.py")
    assert copies[1][1][0] == "openpi_protocol.py"
    assert copies[1][1][1].startswith("sim:/tmp/npa-live-source-")
    assert copies[1][1][1].endswith("/openpi_protocol.py")
    assert copies[2][1][0] == "relay_bridge.py"
    assert copies[2][1][1].endswith("/relay_bridge.py")
    assert calls[-1][0] == "exec"


def test_runtime_source_staging_recovers_from_service_recreation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    for name in ("scenario.py", "openpi_protocol.py", "relay_bridge.py"):
        (source / name).write_text("# reviewed public source\n", encoding="utf-8")
    copies: list[str] = []

    class Cli:
        attempts = 0

        def services_exec(self, _runtime, _service, command):  # noqa: ANN001, ANN202
            if command[:2] == ["install", "-d"]:
                self.attempts += 1
            if command[0] == "sha256sum":
                return hashlib.sha256(b"# reviewed public source\n").hexdigest()
            return ""

        def services_copy(self, _runtime, path, _destination):  # noqa: ANN001, ANN202
            copies.append(path.name)
            if self.attempts == 1 and path.name == "openpi_protocol.py":
                raise AntiochCliError("container recreated")

    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    cli = Cli()
    live._stage_runtime_source(cli, runtime=tmp_path, attempts=2)  # type: ignore[arg-type]

    assert cli.attempts == 2
    assert copies.count("scenario.py") == 2
    assert copies.count("openpi_protocol.py") == 2
    assert copies.count("relay_bridge.py") == 1


def test_live_cleanup_cancels_only_exact_active_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pages = iter(
        (
            [
                {
                    "scenario": "openpi_droid_live",
                    "phase": "running",
                    "scenario_run_id": "exact-live-run",
                },
                {
                    "scenario": "other_scenario",
                    "phase": "running",
                    "scenario_run_id": "unrelated-run",
                },
                {
                    "scenario": "openpi_droid_live",
                    "phase": "completed",
                    "scenario_run_id": "terminal-run",
                },
            ],
            [],
            [],
            [],
        )
    )
    cancelled: list[str] = []

    class Cli:
        def list_for_project(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return next(pages)

        def machine_status(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return {"stream": {}}

        def show(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return {
                "scenario": "openpi_droid_live",
                "project_id": "assigned-project-for-test",
                "phase": "running",
            }

        def cancel(self, _runtime, *, kind, remote_id):  # noqa: ANN001, ANN202
            assert kind == "scenario"
            cancelled.append(remote_id)
            return {}

    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    count = live._cancel_remote_live_runs(
        Cli(),  # type: ignore[arg-type]
        runtime=tmp_path,
        project_id="assigned-project-for-test",
    )

    assert count == 1
    assert cancelled == ["exact-live-run"]


def test_live_cleanup_accepts_exact_list_cancel_terminalization_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pages = iter(
        (
            [
                {
                    "scenario": "openpi_droid_live",
                    "phase": "booting",
                    "scenario_run_id": "just-terminalized",
                }
            ],
            [],
            [],
            [],
        )
    )

    class Cli:
        def list_for_project(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return next(pages)

        def machine_status(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return {"stream": {"state": "idle"}}

        def cancel(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AntiochCliError("scenario run 'just-terminalized' was not found")

    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    count = live._cancel_remote_live_runs(
        Cli(),  # type: ignore[arg-type]
        runtime=tmp_path,
        project_id="assigned-project-for-test",
        attempts=4,
    )

    assert count == 0


def test_live_cleanup_accepts_terminal_failed_stream_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Cli:
        def list_for_project(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return []

        def machine_status(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return {"stream": {"state": "failed", "scenario_run_id": "terminal"}}

        def show(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return {
                "scenario": "openpi_droid_live",
                "project_id": "assigned-project-for-test",
                "phase": "completed",
                "outcome": "cancelled",
            }

    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    assert (
        live._cancel_remote_live_runs(
            Cli(),  # type: ignore[arg-type]
            runtime=tmp_path,
            project_id="assigned-project-for-test",
        )
        == 0
    )


def test_live_cleanup_tolerates_typed_missing_run_during_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pages = iter(
        (
            [
                {
                    "scenario": "openpi_droid_live",
                    "phase": "running",
                    "scenario_run_id": "exact-live-run",
                }
            ],
            [],
            [],
            [],
        )
    )

    class Cli:
        def list_for_project(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return next(pages)

        def machine_status(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return {"stream": {}}

        def cancel(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AntiochCliError(
                "remote run is gone",
                error_type="scenario_not_found",
                http_status=404,
            )

    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    assert (
        live._cancel_remote_live_runs(
            Cli(),  # type: ignore[arg-type]
            runtime=tmp_path,
            project_id="assigned-project-for-test",
        )
        == 0
    )


def test_live_reconcile_adopts_only_machine_stream_owner(tmp_path: Path) -> None:
    class Cli:
        def list_for_project(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return [
                {
                    "scenario": "openpi_droid_live",
                    "phase": "running",
                    "scenario_run_id": "exact-active",
                }
            ]

        def machine_status(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return {
                "stream": {
                    "state": "ready",
                    "scenario_run_id": "exact-active",
                }
            }

    active = live_reconcile._active_run(
        Cli(),  # type: ignore[arg-type]
        runtime=tmp_path,
        project_id="assigned-project-for-test",
    )
    assert active is not None
    assert active["scenario_run_id"] == "exact-active"


def test_live_reconcile_rejects_unlisted_stream_owner(tmp_path: Path) -> None:
    class Cli:
        def list_for_project(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return []

        def machine_status(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return {"stream": {"state": "ready", "scenario_run_id": "other"}}

    with pytest.raises(
        live_reconcile.AntiochLiveReconcileError,
        match="absent from the exact project",
    ):
        live_reconcile._active_run(
            Cli(),  # type: ignore[arg-type]
            runtime=tmp_path,
            project_id="assigned-project-for-test",
        )


def test_double_wss_relay_forwards_bounded_request_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    ca, _certificate_bytes, _key = _certificate("127.0.0.1")
    for name, content in {
        "ca.crt": ca,
        "api-key": b"p" * 48,
        "endpoint.json": b'{"scheme":"wss","host":"127.0.0.1","port":443}',
    }.items():
        path = upstream / name
        path.write_bytes(content)
        path.chmod(0o600)
    bundle = tmp_path / "bundle"
    live._prepare_runtime_bundle(upstream, bundle)
    stop_file = tmp_path / ".stop"

    class Connection:
        def __init__(self, kind: str) -> None:
            self.kind = kind
            self.received = 0
            self.sent: list[bytes] = []

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_args):  # noqa: ANN002, ANN204
            return None

        def recv(self, **_kwargs):  # noqa: ANN202
            self.received += 1
            if self.kind == "policy":
                return b"greeting" if self.received == 1 else b"response"
            if self.received == 1:
                return b"request"
            raise RuntimeError("test stream complete")

        def send(self, payload: bytes) -> None:
            self.sent.append(payload)
            if self.kind == "simulation" and payload == b"response":
                stop_file.touch()

    policy = Connection("policy")
    simulation = Connection("simulation")
    connection_order: list[str] = []

    def connect(uri: str, **kwargs):  # noqa: ANN003, ANN202
        assert kwargs["proxy"] is None
        assert kwargs["additional_headers"]["Authorization"].startswith("Api-Key ")
        kind = "policy" if uri.endswith(":443") else "simulation"
        connection_order.append(kind)
        return policy if kind == "policy" else simulation

    monkeypatch.setattr(live_relay, "connect", connect)
    state = live_relay.run_relay(
        bundle=bundle,
        local_port=18_444,
        stop_file=stop_file,
        state_path=tmp_path / "relay-state.json",
    )

    assert policy.sent == [b"request"]
    assert simulation.sent == [b"greeting", b"response"]
    assert connection_order == ["simulation", "policy"]
    assert state["forwarded_requests"] == 1
    assert state["status"] == "stopped"


def test_live_stop_cancels_scenario_before_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    windows = {
        "scenario": iter((True, False, False)),
        "relay": iter((False,)),
        "bridge": iter((False,)),
    }
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(
        live,
        "_read_state",
        lambda _project_id: {
            "project_id": "assigned-project-for-test",
            "session": "exact-session",
            "runtime": str(runtime),
            "cli": "/opt/antioch/bin/antioch",
        },
    )
    monkeypatch.setattr(live, "_session_running", lambda _session: False)
    monkeypatch.setattr(
        live,
        "_window_running",
        lambda _session, window: next(windows[window]),
    )
    monkeypatch.setattr(
        live,
        "_tmux",
        lambda *args, **_kwargs: calls.append("tmux:" + " ".join(args)),
    )

    class FakeCli:
        def __init__(self, _path: Path) -> None:
            pass

        def list_for_project(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            calls.append("list-live-runs")
            return []

        def machine_status(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            calls.append("machine-status")
            return {"stream": {}}

        def services_down(self, _runtime: Path) -> None:
            calls.append("services-down")

    monkeypatch.setattr(live, "AntiochCli", FakeCli)
    result = live.stop_live(project_id="assigned-project-for-test")

    assert calls == [
        "tmux:send-keys -t exact-session:scenario.0 C-c",
        "list-live-runs",
        "machine-status",
        "list-live-runs",
        "machine-status",
        "list-live-runs",
        "machine-status",
        "services-down",
    ]
    assert result["service_stopped_after_scenario"] is True
    assert result["cancelled_remote_runs"] == 0
    assert (runtime / ".stop").stat().st_mode & 0o777 == 0o600
