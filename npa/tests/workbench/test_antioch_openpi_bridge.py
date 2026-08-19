from __future__ import annotations

import json
import importlib.util
import re
import socket
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner
import yaml

from npa.cli.main import app
from npa.workbench.antioch.openpi_bridge import (
    ACTION_SHAPE,
    IMAGE_SHAPE,
    OpenPIBridgeError,
    OpenPIWebsocketClient,
    RetryPolicy,
    contract_smoke,
    pack_message,
    render_stack,
    safe_position_targets,
    unpack_message,
    validate_actions,
    validate_observation,
)
from npa.workbench.antioch.openpi_health import wait_for_health
from npa.workbench.antioch.openpi_isaac import _verify_vulkan_runtime


def test_health_module_import_does_not_load_offline_dataset_stack() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import npa.workbench.antioch.openpi_health; "
                "assert 'npa.workbench.antioch.manager' not in sys.modules; "
                "assert 'npa.workbench.antioch.dataset' not in sys.modules; "
                "assert 'npa.workbench.antioch.schemas' not in sys.modules; "
                "assert 'npa.workbench.antioch.openpi_bridge' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "antioch-openpi-franka"


def test_vulkan_preflight_rejects_missing_host_graphics_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VK_ICD_FILENAMES", str(tmp_path / "missing.json"))
    with pytest.raises(OpenPIBridgeError, match="NVIDIA Vulkan ICD is unavailable"):
        _verify_vulkan_runtime()


def test_vulkan_preflight_requires_an_nvidia_renderer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    icd = tmp_path / "nvidia.json"
    icd.write_text("{}")
    monkeypatch.setenv("VK_ICD_FILENAMES", str(icd))
    monkeypatch.setattr("shutil.which", lambda _command: "/usr/bin/vulkaninfo")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["vulkaninfo"], returncode=0, stdout="GPU: llvmpipe", stderr=""
        ),
    )
    with pytest.raises(OpenPIBridgeError, match="did not find an NVIDIA renderer"):
        _verify_vulkan_runtime()


def test_vulkan_preflight_accepts_nvidia_renderer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    icd = tmp_path / "nvidia.json"
    icd.write_text("{}")
    monkeypatch.setenv("VK_ICD_FILENAMES", str(icd))
    monkeypatch.setattr("shutil.which", lambda _command: "/usr/bin/vulkaninfo")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["vulkaninfo"], returncode=0, stdout="GPU: NVIDIA RTX", stderr=""
        ),
    )
    _verify_vulkan_runtime()


def test_hosted_example_pins_reviewed_npa_source_revision() -> None:
    manifest = yaml.safe_load((EXAMPLE_DIR / "antioch.yaml").read_text())
    source_ref = manifest["services"]["sim"]["build"]["args"]["NPA_SOURCE_REF"]

    assert re.fullmatch(r"[0-9a-f]{40}", source_ref)
    subprocess.run(
        ["git", "cat-file", "-e", f"{source_ref}^{{commit}}"],
        cwd=EXAMPLE_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    dockerfile = (EXAMPLE_DIR / "Dockerfile").read_text()
    assert "ARG NPA_SOURCE_REF" in dockerfile
    assert "@${NPA_SOURCE_REF}#subdirectory=npa" in dockerfile
    assert "COPY scenarios.py reverse_policy_relay.py /workspace/project/" in dockerfile
    dockerignore = (EXAMPLE_DIR / ".dockerignore").read_text().splitlines()
    assert ".antioch/" in dockerignore
    service = manifest["services"]["sim"]
    assert service["environment"] == {
        "OPENPI_POLICY_HOST": "127.0.0.1",
        "OPENPI_POLICY_PORT": "8000",
    }
    assert service["ports"] == ["18123:18123"]
    assert "secrets" not in service


def test_hosted_reverse_policy_relay_is_bidirectional() -> None:
    spec = importlib.util.spec_from_file_location(
        "npa_antioch_reverse_policy_relay",
        EXAMPLE_DIR / "reverse_policy_relay.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def unused_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    backend_port = unused_port()
    frontend_port = unused_port()
    with module.ReversePolicyRelay(
        backend_port=backend_port,
        frontend_port=frontend_port,
    ):
        backend = socket.create_connection(("127.0.0.1", backend_port), timeout=2)
        frontend = socket.create_connection(("127.0.0.1", frontend_port), timeout=2)
        frontend.sendall(b"request")
        assert backend.recv(7) == b"request"
        backend.sendall(b"response")
        assert frontend.recv(8) == b"response"
        frontend.close()
        backend.close()


def _observation() -> dict[str, object]:
    return {
        "observation/exterior_image_1_left": np.zeros(IMAGE_SHAPE, dtype=np.uint8),
        "observation/wrist_image_left": np.ones(IMAGE_SHAPE, dtype=np.uint8),
        "observation/joint_position": np.asarray(
            [0, -0.5, 0, -1.5, 0, 1.0, 0], dtype=np.float32
        ),
        "observation/gripper_position": np.asarray([0.5], dtype=np.float32),
        "prompt": "pick up the fork",
    }


def _actions() -> np.ndarray:
    return np.tile(np.asarray([0, -0.5, 0, -1.5, 0, 1.0, 0, 0.5]), (15, 1))


def test_messagepack_round_trip_matches_openpi_numpy_contract() -> None:
    value = validate_observation(_observation())
    decoded = unpack_message(pack_message(value))
    assert isinstance(decoded, dict)
    np.testing.assert_array_equal(
        decoded["observation/wrist_image_left"],
        value["observation/wrist_image_left"],
    )
    assert decoded["observation/joint_position"].dtype == np.float32


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.pop("prompt"), "keys"),
        (
            lambda value: value.__setitem__(
                "observation/exterior_image_1_left",
                np.zeros((10, 10, 3), dtype=np.uint8),
            ),
            "uint8",
        ),
        (lambda value: value.__setitem__("prompt", ""), "prompt"),
    ],
)
def test_observation_validation_fails_closed(mutate, message: str) -> None:
    value = _observation()
    mutate(value)
    with pytest.raises(OpenPIBridgeError, match=message):
        validate_observation(value)


def test_response_requires_exact_finite_in_range_chunk() -> None:
    assert validate_actions({"actions": _actions()}).shape == ACTION_SHAPE
    malformed = _actions()
    malformed[2, 3] = np.nan
    with pytest.raises(OpenPIBridgeError, match="non-finite"):
        validate_actions({"actions": malformed})
    wrong = _actions()[:14]
    with pytest.raises(OpenPIBridgeError, match="shape"):
        validate_actions({"actions": wrong})
    unsafe = _actions()
    unsafe[0, 0] = 4.0
    with pytest.raises(OpenPIBridgeError, match="position limits"):
        validate_actions({"actions": unsafe})


def test_safe_targets_rate_limit_without_clipping_invalid_policy_output() -> None:
    actions = _actions()
    actions[:, 0] = 1.0
    result = safe_position_targets(
        actions,
        np.asarray([0, -0.5, 0, -1.5, 0, 1.0, 0]),
        max_joint_delta_rad=0.1,
        execute_steps=5,
    )
    np.testing.assert_allclose(result[:, 0], [0.1, 0.2, 0.3, 0.4, 0.5])


class _FakeConnection:
    def __init__(self, frames: list[bytes | str]) -> None:
        self.frames = frames
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout = 0.0

    def recv(self) -> bytes | str:
        return self.frames.pop(0)

    def send_binary(self, payload: bytes) -> None:
        self.sent.append(payload)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True


def test_client_reconnects_then_returns_exact_chunk() -> None:
    connection = _FakeConnection(
        [pack_message({"model": "pi0.5"}), pack_message({"actions": _actions()})]
    )
    calls = 0

    def factory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("not ready")
        return connection

    sleeps: list[float] = []
    client = OpenPIWebsocketClient(
        "policy.default.svc",
        retry=RetryPolicy(attempts=2, initial_backoff_seconds=0.25),
        connection_factory=factory,
        sleep=sleeps.append,
    )
    result = client.infer(_observation())
    assert result.shape == ACTION_SHAPE
    assert calls == 2
    assert sleeps == [0.25]
    assert len(connection.sent) == 1


def test_client_exhaustion_is_no_action_and_hides_transport_detail() -> None:
    client = OpenPIWebsocketClient(
        "policy.default.svc",
        retry=RetryPolicy(attempts=2, initial_backoff_seconds=0),
        connection_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConnectionError("sensitive endpoint detail")
        ),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(OpenPIBridgeError, match="failed after 2 attempts") as caught:
        client.infer(_observation())
    assert "sensitive endpoint" not in str(caught.value)


def _stack(**overrides: str) -> dict[str, object]:
    values = {
        "run_id": "test-run",
        "namespace": "default",
        "policy_image": "registry.example.invalid/openpi@sha256:" + "a" * 64,
        "bridge_image": "registry.example.invalid/isaac@sha256:" + "b" * 64,
        "policy_terms_secret": "openpi-terms",
        "isaac_acceptance_secret": "isaac-acceptance",
        "policy_gpu_selector_key": "example.invalid/gpu",
        "policy_gpu_selector_value": "B200",
        "bridge_gpu_selector_key": "example.invalid/gpu",
        "bridge_gpu_selector_value": "RTX-PRO-6000",
        "image_pull_secret": "registry-pull",
        "antioch_config_secret": "antioch-config",
        "s3_credentials_secret": "s3-runtime",
        "output_uri": "s3://example-bucket/run/report.json",
    }
    values.update(overrides)
    return render_stack(**values)


def test_stack_uses_separate_gpu_placement_and_private_policy_service() -> None:
    items = _stack()["items"]
    policy, service, bridge, network = items
    assert policy["spec"]["template"]["spec"]["nodeSelector"] == {
        "example.invalid/gpu": "B200"
    }
    assert bridge["spec"]["template"]["spec"]["nodeSelector"] == {
        "example.invalid/gpu": "RTX-PRO-6000"
    }
    for workload in (policy, bridge):
        security = workload["spec"]["template"]["spec"]["securityContext"]
        assert security["runAsNonRoot"] is True
        assert security["runAsUser"] == 1000
        assert security["runAsGroup"] == 1000
        assert security["fsGroup"] == 1000
        assert security["seccompProfile"] == {"type": "RuntimeDefault"}
    init = bridge["spec"]["template"]["spec"]["initContainers"][0]
    assert init["name"] == "wait-for-policy"
    assert init["args"][-1] == "1800"
    assert any(env["name"] == "NO_PROXY" for env in init["env"])
    bridge_env = bridge["spec"]["template"]["spec"]["containers"][0]["env"]
    assert any(env["name"] == "NO_PROXY" for env in bridge_env)
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {"name": "policy", "port": 8000, "targetPort": 8000}
    ]
    assert network["kind"] == "NetworkPolicy"
    assert network["spec"]["ingress"][0]["ports"][0]["port"] == 8000


def test_stack_scopes_antioch_and_isaac_secrets_to_bridge_only() -> None:
    policy, _service, bridge, _network = _stack()["items"]
    serialized_policy = json.dumps(policy, sort_keys=True)
    serialized_bridge = json.dumps(bridge, sort_keys=True)
    assert "antioch-config" not in serialized_policy
    assert "isaac-acceptance" not in serialized_policy
    assert "s3-runtime" not in serialized_policy
    assert "openpi-terms" in serialized_policy
    assert "antioch-config" in serialized_bridge
    assert "isaac-acceptance" in serialized_bridge
    assert "s3-runtime" in serialized_bridge
    assert "NPA_OPENPI_ACCEPT_GEMMA_TERMS" not in serialized_bridge
    assert "token" not in serialized_bridge.lower()


def test_stack_rejects_mutable_images() -> None:
    with pytest.raises(OpenPIBridgeError, match="policy image must be digest-pinned"):
        _stack(policy_image="registry.example.invalid/openpi:latest")


def test_stack_rejects_invalid_readiness_timeout() -> None:
    with pytest.raises(OpenPIBridgeError, match="readiness timeout"):
        _stack(policy_ready_timeout_seconds=0)


def test_health_wait_rejects_non_private_style_host() -> None:
    with pytest.raises(ValueError, match="policy host"):
        wait_for_health("https://public.example.invalid")


def test_contract_smoke_is_real_serialization_and_rate_limit() -> None:
    assert contract_smoke() == {
        "schema": "npa.antioch.openpi-bridge.contract-smoke.v1",
        "status": "passed",
        "observation_keys": [
            "observation/exterior_image_1_left",
            "observation/gripper_position",
            "observation/joint_position",
            "observation/wrist_image_left",
            "prompt",
        ],
        "action_shape": [15, 8],
        "executed_target_shape": [5, 8],
        "fail_closed": True,
    }


def test_cli_renders_stack_without_secret_values() -> None:
    result = CliRunner().invoke(
        app,
        [
            "workbench",
            "antioch",
            "openpi-stack",
            "--run-id",
            "cli-test",
            "--policy-image",
            "registry.example.invalid/openpi@sha256:" + "a" * 64,
            "--bridge-image",
            "registry.example.invalid/isaac@sha256:" + "b" * 64,
            "--policy-terms-secret",
            "terms",
            "--isaac-acceptance-secret",
            "isaac",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "rendered"
    assert payload["manifest"]["kind"] == "List"


def test_cli_contract_smoke() -> None:
    result = CliRunner().invoke(
        app, ["workbench", "antioch", "openpi-contract-smoke", "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["action_shape"] == [15, 8]
