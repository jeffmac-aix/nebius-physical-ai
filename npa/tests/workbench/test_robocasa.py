"""Tests for the RoboCasa shared implementation and service."""

from __future__ import annotations

import sys
import types

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
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_service_list_runs() -> None:
    app = create_app(auth_mode="none")
    client = TestClient(app)
    response = client.get("/runs")
    assert response.status_code == 200
    assert "runs" in response.json()
