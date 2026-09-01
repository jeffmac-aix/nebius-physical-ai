"""Tests for the RoboCasa shared implementation and service."""

from __future__ import annotations

import sys
import types

import numpy as np

import pytest
from fastapi.testclient import TestClient

from npa.workbench.robocasa.capabilities import (
    RoboCasaError,
    compute_manifest_sha256,
    kitchen_asset_availability,
    kitchen_task_registration,
    make_run_id,
    system_info,
)
from npa.workbench.robocasa.schemas import RoboCasaRunRequest
from npa.workbench.robocasa.service import create_app


def _install_fake_robocasa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake robocasa + gymnasium module tree so capability tests run
    without the real simulation stack."""

    class FakeSpec:
        entry_point = "robocasa.envs:KitchenEnv"

    class FakeRegistry(dict):
        def __init__(self) -> None:
            super().__init__()
            self["robocasa/PickPlaceCounterToCabinet"] = FakeSpec()
            self["robocasa/StackHouseholdItems"] = FakeSpec()

    class FakeGym:
        envs = types.SimpleNamespace(registry=FakeRegistry())

    fake_robocasa = types.ModuleType("robocasa")
    fake_robocasa.__file__ = "/opt/robocasa/robocasa/__init__.py"
    monkeypatch.setitem(sys.modules, "robocasa", fake_robocasa)
    monkeypatch.setitem(sys.modules, "gymnasium", FakeGym())


def test_compute_manifest_sha256_is_deterministic() -> None:
    payload = {"env_id": "robocasa/PickPlaceCounterToCabinet", "capability": "kitchen_random_rollout"}
    a = compute_manifest_sha256("run", payload)
    b = compute_manifest_sha256("run", dict(payload))
    assert a == b
    assert len(a) == 64


def test_make_run_id_is_deterministic() -> None:
    a = make_run_id("kitchen_random_rollout", "abc")
    b = make_run_id("kitchen_random_rollout", "abc")
    assert a == b
    assert a.startswith("robocasa-kitchen_random_rollout-")


def test_system_info_returns_payload() -> None:
    info = system_info()
    assert info.status == "ok"
    assert info.python


def test_kitchen_task_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_robocasa(monkeypatch)
    result = kitchen_task_registration()
    assert result["env_id"] == "robocasa/PickPlaceCounterToCabinet"
    assert result["registered_env_count"] == 2


def test_kitchen_task_registration_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_robocasa(monkeypatch)
    with pytest.raises(RoboCasaError):
        kitchen_task_registration(env_id="robocasa/DoesNotExist")


def test_kitchen_asset_availability_missing_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_robocasa(monkeypatch)
    with pytest.raises(RoboCasaError):
        kitchen_asset_availability()


def test_run_capability_unsupported() -> None:
    # The schema rejects an unsupported capability before dispatch.
    with pytest.raises(ValueError):
        RoboCasaRunRequest(capability="bogus", output_uri="s3://bucket/out")


def test_run_request_validates_capability() -> None:
    with pytest.raises(ValueError):
        RoboCasaRunRequest(capability="bogus", output_uri="s3://bucket/out")


def test_service_health() -> None:
    app = create_app(auth_mode="none")
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_service_system_info() -> None:
    app = create_app(auth_mode="none")
    client = TestClient(app)
    response = client.get("/system-info")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_service_run_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_robocasa(monkeypatch)
    app = create_app(auth_mode="none")
    client = TestClient(app)
    response = client.post(
        "/run",
        json={
            "capability": "kitchen_task_registration",
            "env_id": "robocasa/PickPlaceCounterToCabinet",
            "output_uri": "s3://bucket/out",
        },
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    status_response = client.get("/status", params={"run_id": run_id})
    assert status_response.status_code == 200
    assert status_response.json()["status"] in {"running", "completed"}


def test_service_status_unknown_run() -> None:
    app = create_app(auth_mode="none")
    client = TestClient(app)
    response = client.get("/status", params={"run_id": "nope"})
    assert response.status_code == 404


def test_service_run_invalid_capability() -> None:
    app = create_app(auth_mode="none")
    client = TestClient(app)
    response = client.post(
        "/run",
        json={"capability": "bogus", "output_uri": "s3://bucket/out"},
    )
    assert response.status_code == 422


def test_service_auth_token() -> None:
    app = create_app(auth_mode="token", token="secret")
    client = TestClient(app)
    # /health is intentionally unauthenticated so Kubernetes liveness/readiness
    # probes can reach it without a token; the protected surface is /system-info.
    assert client.get("/health").status_code == 200
    assert client.get("/system-info").status_code == 401
    assert (
        client.get("/system-info", headers={"Authorization": "Bearer secret"}).status_code
        == 200
    )
    assert (
        client.get("/system-info", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )


def test_service_list_runs() -> None:
    app = create_app(auth_mode="none")
    client = TestClient(app)
    response = client.get("/runs")
    assert response.status_code == 200
    assert "runs" in response.json()


class _FakeActionSpace:
    def sample(self) -> np.ndarray:
        return np.zeros(7, dtype=np.float32)


class _FakeEnv:
    action_space = _FakeActionSpace()

    def __init__(self) -> None:
        self._closed = False

    def reset(self, seed=None):
        return self._obs(), {}

    def step(self, action):
        return self._obs(), 0.0, False, False, {}

    def render(self):
        return np.zeros((64, 64, 3), dtype=np.uint8)

    def close(self) -> None:
        self._closed = True

    @staticmethod
    def _obs() -> dict:
        return {
            "agentview_image": np.zeros((64, 64, 3), dtype=np.uint8),
            "eye_in_hand_image": np.zeros((64, 64, 3), dtype=np.uint8),
            "robot0_joint_pos": np.zeros(7, dtype=np.float32),
            "robot0_eef_pos": np.zeros(3, dtype=np.float32),
            "robot0_gripper_qpos": np.zeros(1, dtype=np.float32),
        }


def _install_fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake gymnasium whose make() returns a scripted RoboCasa env."""
    _install_fake_robocasa(monkeypatch)

    class FakeGym:
        envs = types.SimpleNamespace(registry={})
        @staticmethod
        def make(env_id):
            return _FakeEnv()

    monkeypatch.setitem(sys.modules, "gymnasium", FakeGym())


def test_kitchen_trajectory_export(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _install_fake_env(monkeypatch)
    from npa.workbench.robocasa.capabilities import kitchen_trajectory_export

    result = kitchen_trajectory_export(
        env_id="robocasa/PickPlaceCounterToCabinet",
        iterations=3,
        num_envs=2,
        seed=1,
        output_dir=tmp_path,
    )
    assert result["trajectory_export_ok"] is True
    assert result["num_episodes"] == 2
    for ep in range(2):
        ep_dir = tmp_path / f"episode_{ep:04d}"
        assert (ep_dir / "obs_workspace.npy").exists()
        assert (ep_dir / "obs_wrist.npy").exists()
        assert (ep_dir / "state.npy").exists()
        assert (ep_dir / "actions.npy").exists()
        ws = np.load(ep_dir / "obs_workspace.npy")
        assert ws.shape == (3, 64, 64, 3)
        assert ws.dtype == np.uint8
        st = np.load(ep_dir / "state.npy")
        assert st.shape == (3, 11)
    assert (tmp_path / "metadata.json").exists()
    assert (tmp_path / "metrics.json").exists()


def test_kitchen_trajectory_export_missing_image_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _install_fake_robocasa(monkeypatch)
    from npa.workbench.robocasa.capabilities import RoboCasaError, kitchen_trajectory_export

    with pytest.raises(RoboCasaError):
        kitchen_trajectory_export(
            env_id="robocasa/PickPlaceCounterToCabinet",
            iterations=1,
            num_envs=1,
            output_dir=tmp_path,
        )
