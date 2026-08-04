"""Capability, attestation, and security tests for the agent LeIsaac tab."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from npa.agent_backend.leisaac import (
    LEISAAC_MEDIA_PORT,
    LEISAAC_SIGNAL_PORT,
    LEISAAC_TASK,
    normalize_manifest,
    selected_run_id,
    status_payload,
    validate_health,
)
from npa.agent_backend.leisaac_routes import LeIsaacDeps, register_leisaac_routes


def _manifest(**overrides):
    now = datetime.now(timezone.utc)
    data = {
        "schema": "npa.leisaac.session.v1",
        "run_id": "leisaac-live-1",
        "provider": "nebius-kubernetes",
        "task": LEISAAC_TASK,
        "teleop_device": "keyboard",
        "signal_host": "8.8.8.8",
        "signal_port": LEISAAC_SIGNAL_PORT,
        "media_host": "1.1.1.1",
        "media_port": LEISAAC_MEDIA_PORT,
        "service_url": "http://8.8.8.8:8080",
        "session_nonce": "a" * 64,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "source_version": "0.4.0",
        "source_commit": "1" * 40,
        "isaac_sim_version": "5.1.0.0",
        "isaac_lab_version": "2.3.2.post1",
        "image": "registry.example/npa-leisaac@sha256:" + "2" * 64,
        "gpu": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
    }
    data.update(overrides)
    return data


def _normalized(**overrides):
    manifest, reason = normalize_manifest(
        _manifest(**overrides), expected_run_id="leisaac-live-1"
    )
    assert reason == ""
    assert manifest is not None
    return manifest


def test_selected_run_requires_safe_exact_identifier() -> None:
    assert (
        selected_run_id({"sim_viz": {"active_run_id": "leisaac-live-1"}})
        == "leisaac-live-1"
    )
    assert selected_run_id({}, "../../etc/passwd") == ""
    assert selected_run_id({"sim_viz": {"run_id": "other"}}, "explicit") == "explicit"


@pytest.mark.parametrize(
    "override,reason_fragment",
    [
        ({"task": "Isaac-Cartpole-v0"}, "supported task"),
        ({"teleop_device": "so101leader"}, "keyboard"),
        ({"signal_host": "127.0.0.1"}, "public IP"),
        ({"service_url": "http://1.1.1.1:8080"}, "service endpoint"),
        ({"signal_port": "not-an-int"}, "signaling port"),
        ({"media_port": 80}, "media port"),
        ({"image": "registry.example/npa-leisaac:latest"}, "digest"),
        ({"source_commit": "main"}, "source commit"),
    ],
)
def test_manifest_failures_suppress_capability(override, reason_fragment: str) -> None:
    manifest, reason = normalize_manifest(
        _manifest(**override), expected_run_id="leisaac-live-1"
    )
    assert manifest is None
    assert reason_fragment in reason


def test_expired_or_cross_run_manifest_is_rejected() -> None:
    now = datetime.now(timezone.utc)
    assert normalize_manifest(_manifest(), expected_run_id="different")[0] is None
    manifest, reason = normalize_manifest(
        _manifest(expires_at=(now - timedelta(seconds=1)).isoformat()),
        expected_run_id="leisaac-live-1",
        now=now,
    )
    assert manifest is None
    assert "expired" in reason


def test_manifest_without_expiry_remains_lifecycle_gated() -> None:
    now = datetime.now(timezone.utc)
    normalized, reason = normalize_manifest(
        _manifest(expires_at=None),
        expected_run_id="leisaac-live-1",
        now=now,
    )

    assert reason == ""
    assert normalized is not None
    assert normalized["expires_at"] == ""


def test_agent_relay_manifest_accepts_only_fixed_loopback_tcp_contract() -> None:
    normalized, reason = normalize_manifest(
        _manifest(
            transport="agent-relay",
            signal_host="127.0.0.1",
            service_url="http://127.0.0.1:48080",
        ),
        expected_run_id="leisaac-live-1",
    )

    assert reason == ""
    assert normalized is not None
    assert normalized["transport"] == "agent-relay"
    assert normalized["signal_host"] == "127.0.0.1"

    for override in (
        {"signal_host": "127.0.0.2"},
        {"service_url": "http://127.0.0.1:8080"},
        {"service_url": "http://127.0.0.2:48080"},
        {"service_url": "http://169.254.169.254:48080"},
    ):
        relay_values = {
            "transport": "agent-relay",
            "signal_host": "127.0.0.1",
            "service_url": "http://127.0.0.1:48080",
        }
        relay_values.update(override)
        rejected, rejected_reason = normalize_manifest(
            _manifest(**relay_values),
            expected_run_id="leisaac-live-1",
        )
        assert rejected is None
        assert rejected_reason


def test_live_health_attestation_gates_secret_free_status() -> None:
    manifest = _normalized()
    health, reason = validate_health(
        manifest,
        {
            "schema": "npa.leisaac.health.v1",
            "state": "ready",
            "webrtc_ready": True,
            "run_id": manifest["run_id"],
            "task": manifest["task"],
            "source_commit": manifest["source_commit"],
            "session_nonce": manifest["session_nonce"],
            "signal_port": LEISAAC_SIGNAL_PORT,
            "pid": 42,
            "gpu": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        },
    )
    assert reason == ""
    payload = status_payload(manifest, health)
    assert payload["available"] is True
    assert payload["signaling_server"] == "same-origin"
    assert payload["signaling_path"] == "/api/leisaac/signal"
    assert payload["media_server"] == "1.1.1.1"
    serialized = repr(payload)
    assert manifest["session_nonce"] not in serialized
    assert manifest["service_url"] not in serialized


def test_health_nonce_or_readiness_mismatch_suppresses_tab() -> None:
    manifest = _normalized()
    health, reason = validate_health(
        manifest,
        {
            "schema": "npa.leisaac.health.v1",
            "state": "ready",
            "webrtc_ready": True,
            "run_id": manifest["run_id"],
            "task": manifest["task"],
            "source_commit": manifest["source_commit"],
            "session_nonce": "b" * 64,
            "signal_port": LEISAAC_SIGNAL_PORT,
        },
    )
    assert health is None
    assert "session_nonce" in reason
    assert status_payload(manifest, health, reason=reason)["available"] is False


def test_authenticated_backend_routes_gate_status_and_proxy_client(monkeypatch) -> None:
    raw_manifest = _manifest()
    client_content = b"window.OVWebStreamingLibrary={};"
    monkeypatch.setattr(
        "npa.agent_backend.leisaac_routes.LEISAAC_CLIENT_JS_SHA256",
        hashlib.sha256(client_content).hexdigest(),
    )

    class FakeResponse:
        def __init__(self, payload=None, *, content=b"", status_code=200):
            self._payload = payload
            self.content = content
            self.status_code = status_code

        def json(self):
            return self._payload

    def http_get(url: str, **_kwargs):
        if url.endswith("/status"):
            return FakeResponse(
                {
                    "schema": "npa.leisaac.health.v1",
                    "state": "ready",
                    "webrtc_ready": True,
                    "run_id": raw_manifest["run_id"],
                    "task": raw_manifest["task"],
                    "source_commit": raw_manifest["source_commit"],
                    "session_nonce": raw_manifest["session_nonce"],
                    "signal_port": LEISAAC_SIGNAL_PORT,
                    "pid": 42,
                }
            )
        return FakeResponse(content=client_content)

    api = FastAPI()
    register_leisaac_routes(
        api,
        LeIsaacDeps(
            load_state=lambda: {"sim_viz": {"active_run_id": raw_manifest["run_id"]}},
            resolve_manifest=lambda run_id: (
                raw_manifest if run_id == raw_manifest["run_id"] else None
            ),
            http_get=http_get,
            response=Response,
            websocket_connect=lambda *_args, **_kwargs: None,
        ),
    )
    client = TestClient(api)
    missing = client.get("/leisaac/status", params={"run_id": "other"})
    assert missing.status_code == 200
    assert missing.json()["available"] is False
    insecure = client.get(
        "/leisaac/status", params={"run_id": raw_manifest["run_id"]}
    )
    assert insecure.json()["available"] is False
    assert "HTTPS" in insecure.json()["reason"]
    status = client.get(
        "/leisaac/status",
        params={"run_id": raw_manifest["run_id"]},
        headers={"x-forwarded-proto": "https"},
    )
    assert status.status_code == 200
    assert status.json()["available"] is True
    module = client.get(
        "/leisaac/client/index.js", params={"run_id": raw_manifest["run_id"]}
    )
    assert module.status_code == 200
    assert module.headers["cache-control"] == "private, no-store"
    assert module.content == client_content
    monkeypatch.setattr(
        "npa.agent_backend.leisaac_routes.LEISAAC_CLIENT_JS_SHA256", "0" * 64
    )
    rejected = client.get(
        "/leisaac/client/index.js", params={"run_id": raw_manifest["run_id"]}
    )
    assert rejected.status_code == 502
    assert "integrity" in rejected.json()["detail"]
