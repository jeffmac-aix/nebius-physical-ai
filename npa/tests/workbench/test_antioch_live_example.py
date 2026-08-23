from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import numpy as np
import pytest
import yaml

from npa.workbench.antioch import live

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "npa/examples/antioch-openpi-live"


def test_live_example_uses_only_runtime_project_identity() -> None:
    manifest = yaml.safe_load((EXAMPLE / "antioch.yaml").read_text(encoding="utf-8"))
    assert manifest["id"] == "replace-at-runtime"
    sim = manifest["services"]["sim"]
    assert "image" not in sim
    assert sim["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    rendered = (EXAMPLE / "antioch.yaml").read_text(encoding="utf-8")
    assert not re.search(r"(?:project|tenant|cluster)-[a-z0-9]+", rendered)


def test_live_scenario_is_real_bounded_and_fail_closed() -> None:
    source = (EXAMPLE / "src/scenario.py").read_text(encoding="utf-8")
    compile(source, "antioch-openpi-live-scenario", "exec")
    for contract in (
        "ACTION_SHAPE = (15, 8)",
        "MAX_RESPONSE_AGE_SECONDS",
        "MAX_JOINT_STEP",
        "ssl.create_default_context",
        'additional_headers={"Authorization": f"Api-Key {token}"}',
        'logger.image("camera/exterior"',
        'logger.image("camera/wrist"',
        'logger.scalar("decision/observation_sequence"',
        'logger.scalar("decision/policy_requests"',
        'logger.scalar("decision/round_trips"',
        'logger.scalar("decision/inference_latency_ms"',
        'logger.scalar("decision/safe_hold"',
        'logger.scalar("decision/reconnects"',
        'logger.scalar("decision/safe_targets_applied"',
        "ArticulationAction",
    ):
        assert contract in source
    assert "WebsocketClientPolicy(" not in source
    assert "verify_mode = ssl.CERT_NONE" not in source
    assert "while True:" in source


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
    assert '"msgpack==1.1.1"' in dockerfile
    assert '"websockets==15.0.1"' in dockerfile
    assert "USER 1000:1000" in dockerfile
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
    script = tmp_path / "supervise.sh"
    live._write_supervisor(
        script,
        cli_path=Path("/opt/antioch/bin/antioch"),
        stop_file=tmp_path / ".stop",
        scenario_timeout_seconds=14_400,
    )
    source = script.read_text(encoding="utf-8")
    assert "while [ ! -f" in source
    assert "scenario run --scenario openpi_droid_live" in source
    assert "--timeout 14400 --stream --verbose" in source
    assert "NPA_ANTIOCH_RENEWAL" in source
    assert "timeout 14400s" not in source
    assert script.stat().st_mode & 0o777 == 0o700


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
    live._validate_bundle(bundle)
    (bundle / "api-key").chmod(0o644)
    try:
        live._validate_bundle(bundle)
    except live.AntiochLiveError as exc:
        assert "group/world" in str(exc)
    else:
        raise AssertionError("a public client credential was accepted")


def test_live_stop_cancels_scenario_before_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    states = iter((True, False, False))
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
    monkeypatch.setattr(live, "_session_running", lambda _session: next(states))
    monkeypatch.setattr(
        live,
        "_tmux",
        lambda *args, **_kwargs: calls.append("tmux:" + " ".join(args)),
    )

    class FakeCli:
        def __init__(self, _path: Path) -> None:
            pass

        def services_down(self, _runtime: Path) -> None:
            calls.append("services-down")

    monkeypatch.setattr(live, "AntiochCli", FakeCli)
    result = live.stop_live(project_id="assigned-project-for-test")

    assert calls == ["tmux:send-keys -t exact-session:0.0 C-c", "services-down"]
    assert result["service_stopped_after_scenario"] is True
    assert (runtime / ".stop").stat().st_mode & 0o777 == 0o600
