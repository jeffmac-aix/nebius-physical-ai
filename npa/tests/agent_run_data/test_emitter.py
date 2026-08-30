"""Tests for the agent run data trajectory emitter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npa.agent_run_data.emitter import (
    CollectionStatus,
    DatasetConfig,
    emit_trajectory,
    redact,
    resolve_dataset_config,
    verify_destination,
)


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_writes = False
        self.fail_reads = False

    def head_bucket(self, **kwargs: object) -> None:
        return None

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        if self.fail_writes:
            raise RuntimeError("s3 write failed")
        self.objects[Key] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if self.fail_reads:
            raise RuntimeError("s3 read failed")
        return {"Body": _BytesReader(self.objects[Key])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorage:
    def __init__(self, s3: FakeS3) -> None:
        self.s3 = s3


@pytest.fixture
def dataset_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NPA_AGENT_DATASET_TENANT_ID", "tenant-test")
    monkeypatch.setenv("NPA_AGENT_DATASET_URI", "s3://test-bucket/agent-dataset")
    monkeypatch.setenv("NPA_AGENT_DATASET_OUTBOX", str(tmp_path / "outbox"))


def _trajectory() -> list[dict]:
    return [
        {"sequence": 0, "phase": "plan", "tool": "", "arguments": {}, "observation": {}, "status": "ok"},
        {"sequence": 1, "phase": "tool", "tool": "workbench.robocasa.random_rollout", "arguments": {"capability": "kitchen_random_rollout"}, "observation": {"ok": True}, "status": "ok"},
    ]


def _outcome(status: str = "succeeded") -> dict:
    return {"status": status, "verified": True, "verified_by": ["pytest"], "artifact_uris": [], "operator_interventions": [], "preference_pairs": []}


def test_resolve_dataset_config_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NPA_AGENT_DATASET_TENANT_ID", raising=False)
    monkeypatch.delenv("NPA_AGENT_DATASET_URI", raising=False)
    assert resolve_dataset_config() is None


def test_resolve_dataset_config_partial_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NPA_AGENT_DATASET_TENANT_ID", "tenant-test")
    monkeypatch.delenv("NPA_AGENT_DATASET_URI", raising=False)
    with pytest.raises(Exception):
        resolve_dataset_config()


def test_redact_secrets() -> None:
    payload = {
        "token": "Bearer abc123",
        "api_key": "synthetic-secret-value-12345",
        "nested": {"password": "hunter2", "ok": "fine"},
    }
    redacted = redact(payload)
    assert "abc123" not in json.dumps(redacted)
    assert "synthetic-secret-value-12345" not in json.dumps(redacted)
    assert "hunter2" not in json.dumps(redacted)
    assert redacted["nested"]["ok"] == "fine"


def test_emit_success_collected(dataset_env: None) -> None:
    s3 = FakeS3()
    storage = FakeStorage(s3)
    status, episode_id = emit_trajectory(
        episode_id="ep-1",
        session_id="sess-1",
        request_content="run robocasa rollout",
        intent="run",
        trajectory=_trajectory(),
        outcome=_outcome(),
        routing={"grounded": False, "tier": "gpu", "model": "test", "input_tokens": 0, "output_tokens": 0},
        versions={"agent": "test", "tools": {}},
        storage=storage,
    )
    assert status == CollectionStatus.COLLECTED
    assert episode_id == "ep-1"
    assert len(s3.objects) == 1
    key = next(iter(s3.objects))
    assert key.startswith("agent-dataset/episodes/")
    assert "ep-1-" in key
    payload = json.loads(s3.objects[key])
    assert payload["schema_version"] == "npa.agent.trajectory.v1"
    # The uploaded object is written before read-after-write verification, so it
    # must not claim `collected`; the returned status is what reflects verification.
    assert payload["collection"]["status"] == CollectionStatus.PENDING
    assert payload["collection"]["content_sha256"]


def test_emit_deterministic_key(dataset_env: None) -> None:
    s3 = FakeS3()
    storage = FakeStorage(s3)
    started = "2026-08-30T00:00:00+00:00"
    ended = "2026-08-30T00:01:00+00:00"
    emit_trajectory(
        episode_id="ep-2", session_id="sess-1", request_content="x", intent="run",
        trajectory=_trajectory(), outcome=_outcome(),
        routing={}, versions={}, storage=storage,
        started_at=started, ended_at=ended,
    )
    keys = list(s3.objects.keys())
    assert len(keys) == 1
    s3.objects.clear()
    emit_trajectory(
        episode_id="ep-2", session_id="sess-1", request_content="x", intent="run",
        trajectory=_trajectory(), outcome=_outcome(),
        routing={}, versions={}, storage=storage,
        started_at=started, ended_at=ended,
    )
    assert list(s3.objects.keys()) == keys


def test_emit_s3_failure_enters_outbox(dataset_env: None, tmp_path: Path) -> None:
    s3 = FakeS3()
    s3.fail_writes = True
    storage = FakeStorage(s3)
    status, episode_id = emit_trajectory(
        episode_id="ep-3", session_id="sess-1", request_content="x", intent="run",
        trajectory=_trajectory(), outcome=_outcome(),
        routing={}, versions={}, storage=storage,
    )
    assert status == CollectionStatus.PENDING
    outbox = tmp_path / "outbox"
    assert (outbox / "ep-3.json").exists()
    payload = json.loads((outbox / "ep-3.json").read_text())
    assert payload["collection"]["status"] == CollectionStatus.PENDING


def test_emit_disabled_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NPA_AGENT_DATASET_TENANT_ID", raising=False)
    monkeypatch.delenv("NPA_AGENT_DATASET_URI", raising=False)
    status, episode_id = emit_trajectory(
        episode_id="ep-4", session_id="sess-1", request_content="x", intent="run",
        trajectory=_trajectory(), outcome=_outcome(),
        routing={}, versions={},
    )
    assert status == CollectionStatus.DISABLED


def test_verify_destination_rejects_unwritable(dataset_env: None) -> None:
    s3 = FakeS3()
    s3.fail_writes = True
    storage = FakeStorage(s3)
    config = DatasetConfig(tenant_id="tenant-test", dataset_uri="s3://test-bucket/agent-dataset", bucket="test-bucket", prefix="agent-dataset")
    with pytest.raises(Exception):
        verify_destination(config, storage=storage)
