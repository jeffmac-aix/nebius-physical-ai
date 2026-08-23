from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from npa.cli.main import app
from npa.workbench.antioch.dataset import AntiochDatasetError, validate_episode
from npa.workbench.antioch.manager import (
    AntiochManager,
    AntiochOperationError,
    _dataset_metadata,
    operation_key,
)
from npa.workbench.antioch.project import (
    AntiochProjectError,
    deterministic_project_id,
    package_project,
    stage_project,
)
from npa.workbench.antioch.redaction import REDACTED, redact_payload, redact_text
from npa.workbench.antioch.schemas import (
    EpisodeProvenance,
    OperationRecord,
    ProjectArchive,
    ProjectManifest,
    ResumeRequest,
    SubmitRequest,
)
from npa.workbench.antioch.storage import (
    StateStore,
    StoragePreconditionFailed,
    canonical_json,
    join_uri,
    sha256_bytes,
)
from npa.workbench.antioch.service import create_app
from npa.workbench.antioch.storage_config import (
    DEFAULT_NEBIUS_STORAGE_ENDPOINT,
    resolve_storage_client,
)
from npa.workbench.antioch.vendor_cli import AntiochCli, AntiochCliError


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.counter = 0

    def read_bytes_with_etag(self, uri: str):  # noqa: ANN201
        return self.objects.get(uri)

    def put_bytes_conditional(
        self,
        payload: bytes,
        uri: str,
        *,
        if_match: str = "",
        if_none_match: bool = False,
        content_type: str = "",
    ) -> str:
        del content_type
        current = self.objects.get(uri)
        if if_none_match and current is not None:
            raise StoragePreconditionFailed(uri)
        if if_match and (current is None or current[1] != if_match):
            raise StoragePreconditionFailed(uri)
        self.counter += 1
        etag = f'"{self.counter}"'
        self.objects[uri] = (bytes(payload), etag)
        return etag


@pytest.fixture(autouse=True)
def _accept_antioch_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NPA_ANTIOCH_ACCEPT_TERMS", "YES")


def _submit() -> SubmitRequest:
    return SubmitRequest(
        input_path="s3://safe/input",
        output_path="s3://safe/run",
        workflow_run="run-1",
        state_id="simulate",
        robot_type="dual-camera-cart",
        task="Move the cart to the requested target",
        suite="smoke",
    )


def test_submit_metadata_is_required_and_non_cartpole_values_are_preserved() -> None:
    with pytest.raises(ValidationError):
        SubmitRequest(
            input_path="s3://safe/input",
            output_path="s3://safe/run",
            workflow_run="run-1",
            state_id="simulate",
            suite="smoke",
        )
    request = _submit()
    assert request.robot_type == "dual-camera-cart"
    assert request.task == "Move the cart to the requested target"


def test_collection_fails_closed_for_legacy_state_without_dataset_metadata() -> None:
    request = _submit()
    record = OperationRecord(
        idempotency_key="a" * 64,
        request_sha256="b" * 64,
        workflow_run=request.workflow_run,
        state_id=request.state_id,
        input_path=request.input_path,
        output_path=request.output_path,
        derived_project_id="npa-legacy",
        remote_kind="suite",
        selection=request.suite,
    )
    with pytest.raises(AntiochOperationError) as raised:
        _dataset_metadata(record)
    assert raised.value.error_type == "dataset_metadata_missing"


def test_workload_identity_storage_resolver_needs_no_static_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_from_environment(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return object()

    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("NEBIUS_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("NPA_STORAGE_ENDPOINT", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setattr(
        "npa.workbench.antioch.storage_config.StorageClient.from_environment",
        fake_from_environment,
    )
    assert resolve_storage_client(host_resolver=lambda: None) is not None
    assert captured == {"endpoint_url": DEFAULT_NEBIUS_STORAGE_ENDPOINT}


def test_redaction_covers_nested_credentials_and_signed_urls() -> None:
    payload = redact_payload(
        {
            "token": "top-secret",
            "nested": {"Authorization": "Bearer abc.def.ghi"},
            "url": "https://x/a?X-Amz-Signature=secret&ok=1",
        }
    )
    rendered = json.dumps(payload)
    assert (
        "top-secret" not in rendered
        and "abc.def.ghi" not in rendered
        and "secret" not in rendered
    )
    assert REDACTED in rendered
    assert "eyJhbGciOiJIUzI1NiJ9.e30.signature" not in redact_text(
        "eyJhbGciOiJIUzI1NiJ9.e30.signature"
    )


@pytest.mark.parametrize(
    "status,retryable", [(401, False), (429, True), (500, True), (503, True)]
)
def test_structured_cli_error_classification(
    monkeypatch: pytest.MonkeyPatch, status: int, retryable: bool
) -> None:
    error = {
        "error": {
            "type": "http",
            "message": "Bearer sensitive",
            "http_status": status,
            "exit_code": 1,
        }
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", json.dumps(error)),
    )
    with pytest.raises(AntiochCliError) as raised:
        AntiochCli("antioch").show(Path("."), kind="scenario", remote_id="r")
    assert raised.value.retryable is retryable
    assert "sensitive" not in str(raised.value)


def test_cli_rejects_malformed_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "not-json", ""),
    )
    with pytest.raises(AntiochCliError, match="malformed"):
        AntiochCli("antioch").show(Path("."), kind="scenario", remote_id="r")


def _project_bundle(
    tmp_path: Path, member_name: str = "project/antioch.yaml"
) -> tuple[bytes, bytes]:
    archive = tmp_path / "project.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        content = b"id: original\nname: Synthetic\nservices: {}\n"
        info = tarfile.TarInfo(member_name)
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    raw = archive.read_bytes()
    manifest = ProjectManifest(
        archive=ProjectArchive(size_bytes=len(raw), sha256=sha256_bytes(raw)),
        source_name="synthetic-cartpole",
        source_revision="1",
        source_license="CC0-1.0",
        source_sha256="a" * 64,
    )
    return canonical_json(manifest.model_dump(mode="json")), raw


class ProjectStorage:
    def __init__(self, manifest: bytes, archive: bytes) -> None:
        self.manifest = manifest
        self.archive = archive

    def read_bytes_with_etag(self, uri: str):  # noqa: ANN201
        return self.manifest, '"1"'

    def download_file(self, uri: str, local: str) -> str:
        Path(local).write_bytes(self.archive)
        return local


def test_project_staging_is_immutable_and_deterministic(tmp_path: Path) -> None:
    manifest, archive = _project_bundle(tmp_path)
    root, _source, digest = stage_project(
        ProjectStorage(manifest, archive),
        "s3://safe/input",
        tmp_path / "stage",
        project_id="npa-safe-id",
    )
    assert digest == sha256_bytes(archive)
    assert (root / "antioch.yaml").read_text().startswith("id: npa-safe-id")
    assert deterministic_project_id("run", "state") == deterministic_project_id(
        "run", "state"
    )


def test_project_staging_rejects_traversal(tmp_path: Path) -> None:
    manifest, archive = _project_bundle(tmp_path, "../antioch.yaml")
    with pytest.raises(AntiochProjectError, match="unsafe"):
        stage_project(
            ProjectStorage(manifest, archive),
            "s3://safe/input",
            tmp_path / "stage",
            project_id="npa-safe",
        )


def test_project_packaging_is_reproducible_and_excludes_caches(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "antioch.yaml").write_text("id: example\nservices: {}\n")
    (source / "scenario.py").write_text("VALUE = 1\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "scenario.pyc").write_bytes(b"cache")
    kwargs = {
        "source_name": "synthetic",
        "source_revision": "1",
        "source_license": "CC0-1.0",
        "source_sha256": "a" * 64,
    }
    first = package_project(source, tmp_path / "first", **kwargs)
    second = package_project(source, tmp_path / "second", **kwargs)
    assert first.archive.sha256 == second.archive.sha256
    with tarfile.open(tmp_path / "first" / "project.tar.gz") as bundle:
        assert "scenario.py" in bundle.getnames()
        assert not any("__pycache__" in name for name in bundle.getnames())


def _episode(path: Path, **replacements: Any) -> None:
    length = 4
    provenance = EpisodeProvenance(
        scenario="cartpole",
        case="balance",
        seed=7,
        parameters={"mass": 1.0},
        engine_version="1",
        sdk_version="0.3.47",
        source_sha256="a" * 64,
        assets_sha256={"cart": "b" * 64},
        observation_schema=["position", "velocity"],
        action_schema=["force_positive", "force_negative"],
        fps=20,
    )
    arrays: dict[str, Any] = {
        "observation_state": np.zeros((length, 2), dtype=np.float32),
        "observation_image_workspace": np.zeros((length, 8, 8, 3), dtype=np.uint8),
        "observation_image_wrist": np.zeros((length, 8, 8, 3), dtype=np.uint8),
        "action": np.zeros((length, 2), dtype=np.float32),
        "reward": np.ones(length, dtype=np.float32),
        "terminated": np.array([False, False, False, True]),
        "truncated": np.zeros(length, dtype=bool),
        "timestamp": np.arange(length, dtype=np.float64) / 20,
        "provenance": np.array(provenance.model_dump_json()),
    }
    arrays.update(replacements)
    np.savez(path, **arrays)


def test_episode_contract_accepts_complete_data(tmp_path: Path) -> None:
    path = tmp_path / "episode.npz"
    _episode(path)
    arrays, provenance = validate_episode(path)
    assert arrays["action"].shape == (4, 2)
    assert provenance.seed == 7


@pytest.mark.parametrize(
    "replacement",
    [
        {"timestamp": np.array([0.0, 0.1, 0.1, 0.2])},
        {"terminated": np.zeros(4, dtype=bool)},
        {"action": np.zeros((3, 2))},
    ],
)
def test_episode_contract_fails_closed_on_incompatible_data(
    tmp_path: Path, replacement: dict[str, Any]
) -> None:
    path = tmp_path / "episode.npz"
    _episode(path, **replacement)
    with pytest.raises(AntiochDatasetError):
        validate_episode(path)


def test_episode_contract_rejects_partial_bundle(tmp_path: Path) -> None:
    path = tmp_path / "partial.npz"
    np.savez(path, action=np.zeros((2, 1)))
    with pytest.raises(AntiochDatasetError, match="missing required"):
        validate_episode(path)


def test_episode_contract_rejects_single_channel_act_data(tmp_path: Path) -> None:
    path = tmp_path / "single-action.npz"
    provenance = EpisodeProvenance(
        scenario="cartpole",
        case="balance",
        seed=7,
        parameters={},
        engine_version="1",
        sdk_version="0.3.47",
        source_sha256="a" * 64,
        assets_sha256={"cart": "b" * 64},
        observation_schema=["position", "velocity"],
        action_schema=["force"],
        fps=20,
    )
    _episode(
        path,
        action=np.zeros((4, 1), dtype=np.float32),
        provenance=np.array(provenance.model_dump_json()),
    )
    with pytest.raises(AntiochDatasetError, match="at least two action channels"):
        validate_episode(path)


class FakeCli:
    def __init__(self) -> None:
        self.submissions = 0
        self.cancellations = 0
        self.existing: list[dict[str, str]] = []

    def list_for_project(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self.existing

    def submit_suite(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.submissions += 1
        return {"suite_run_id": "suite-safe", "invocation_id": "invoke-safe"}

    def show(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return {"suite_run_id": "suite-safe", "phase": "running"}

    def cancel(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.cancellations += 1
        return {"suite_run_id": "suite-safe", "phase": "cancelled"}

    def rerun(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return {"suite_run_id": "suite-rerun", "invocation_id": "invoke-rerun"}


def test_idempotent_retry_restart_reconcile_and_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    memory = MemoryStorage()
    cli = FakeCli()
    monkeypatch.setattr(
        "npa.workbench.antioch.manager.stage_project",
        lambda *a, **k: (tmp_path, object(), "c" * 64),
    )
    first = AntiochManager.__new__(AntiochManager)
    first.storage = memory
    first.states = StateStore(memory)
    first._cli = lambda *a, **k: cli  # type: ignore[method-assign]
    record = first.submit(_submit())
    assert record.remote_id == "suite-safe" and cli.submissions == 1
    assert record.robot_type == "dual-camera-cart"
    assert record.task == "Move the cart to the requested target"
    assert record.terms_accepted is True
    assert first.submit(_submit()).remote_id == "suite-safe" and cli.submissions == 1

    restarted = AntiochManager.__new__(AntiochManager)
    restarted.storage = memory
    restarted.states = StateStore(memory)
    restarted._cli = lambda *a, **k: cli  # type: ignore[method-assign]
    resume = ResumeRequest(
        output_path="s3://safe/run", workflow_run="run-1", state_id="simulate"
    )
    assert restarted.reconcile(resume).status == "running"
    assert restarted.cancel(resume).status == "cancelled"
    assert restarted.cancel(resume).status == "cancelled"
    assert cli.cancellations == 1


@pytest.mark.parametrize("operation", ["cancel", "resume"])
@pytest.mark.parametrize("retryable", [False, True])
def test_cancel_and_rerun_persist_typed_cli_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    retryable: bool,
) -> None:
    memory = MemoryStorage()
    manager = AntiochManager.__new__(AntiochManager)
    manager.storage = memory
    manager.states = StateStore(memory)
    monkeypatch.setattr(
        "npa.workbench.antioch.manager.stage_project",
        lambda *a, **k: (tmp_path, object(), "c" * 64),
    )
    cli = FakeCli()
    manager._cli = lambda *a, **k: cli  # type: ignore[method-assign]
    record = manager.submit(_submit())
    if operation == "resume":
        record = manager.states.update(record, status="failed")
        cli.show = lambda *a, **k: {  # type: ignore[method-assign]
            "suite_run_id": "suite-safe",
            "phase": "failed",
        }

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AntiochCliError(
            "classified failure",
            error_type="capacity" if retryable else "authentication",
            retryable=retryable,
        )

    setattr(cli, "cancel" if operation == "cancel" else "rerun", fail)
    request = ResumeRequest(
        output_path=record.output_path,
        workflow_run=record.workflow_run,
        state_id=record.state_id,
        rerun_terminal=operation == "resume",
    )
    with pytest.raises(AntiochOperationError) as raised:
        getattr(manager, operation)(request)
    assert raised.value.retryable is retryable
    durable = manager._record_for(request)
    assert durable.retryable is retryable
    assert durable.error_type == ("capacity" if retryable else "authentication")
    assert durable.status == ("failed" if operation == "resume" else "running")


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_cancel_does_not_clobber_terminal_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, terminal: str
) -> None:
    memory = MemoryStorage()
    manager = AntiochManager.__new__(AntiochManager)
    manager.storage = memory
    manager.states = StateStore(memory)
    monkeypatch.setattr(
        "npa.workbench.antioch.manager.stage_project",
        lambda *a, **k: (tmp_path, object(), "c" * 64),
    )
    cli = FakeCli()
    manager._cli = lambda *a, **k: cli  # type: ignore[method-assign]
    record = manager.submit(_submit())
    immutable = {
        "artifact_manifest_uri": "s3://safe/run/manifests/v1.json",
        "dataset_uri": "s3://safe/run/dataset",
        "completion_uri": "s3://safe/run/_SUCCESS.json",
    }
    record = manager.states.update(record, status=terminal, **immutable)
    request = ResumeRequest(
        output_path=record.output_path,
        workflow_run=record.workflow_run,
        state_id=record.state_id,
    )
    result = manager.cancel(request)
    assert result.status == terminal
    assert {key: getattr(result, key) for key in immutable} == immutable
    assert cli.cancellations == 0


def test_cli_failure_envelope_exposes_retry_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AntiochOperationError(
            "try later", retryable=True, error_type="capacity"
        )

    monkeypatch.setattr("npa.sdk.workbench.antioch.cancel", fail)
    result = CliRunner().invoke(
        app,
        [
            "workbench",
            "antioch",
            "cancel",
            "--output-path",
            "s3://safe/run",
            "--workflow-run",
            "run-1",
            "--state-id",
            "simulate",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 1
    envelope = json.loads(result.stderr)
    assert envelope["error"] == {
        "type": "capacity",
        "message": "try later",
        "retryable": True,
        "terminal": False,
    }


def test_concurrent_submitter_is_fenced_and_expired_restart_reconciles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    memory = MemoryStorage()
    manager = AntiochManager.__new__(AntiochManager)
    manager.storage = memory
    manager.states = StateStore(memory)
    request = _submit()
    key = operation_key(request.workflow_run, request.state_id)
    claimed = manager.states.claim(
        OperationRecord(
            idempotency_key=key,
            request_sha256=sha256_bytes(
                canonical_json(request.model_dump(mode="json"))
            ),
            workflow_run=request.workflow_run,
            state_id=request.state_id,
            robot_type=request.robot_type,
            task=request.task,
            input_path=request.input_path,
            output_path=request.output_path,
            derived_project_id=deterministic_project_id(
                request.workflow_run, request.state_id
            ),
            remote_kind="suite",
            selection="smoke",
        )
    )
    leased, acquired = manager.states.acquire_submission(claimed, "first-owner")
    assert acquired
    cli = FakeCli()
    manager._cli = lambda *a, **k: cli  # type: ignore[method-assign]
    assert manager.submit(request).remote_id == ""
    assert cli.submissions == 0

    manager.states.update(
        leased,
        submission_lease_expires_at="2000-01-01T00:00:00Z",
    )
    cli.existing = [{"suite_run_id": "suite-recovered"}]
    monkeypatch.setattr(
        "npa.workbench.antioch.manager.stage_project",
        lambda *a, **k: (tmp_path, object(), "c" * 64),
    )
    recovered = manager.submit(request)
    assert recovered.remote_id == "suite-recovered"
    assert cli.submissions == 0


def test_atomic_immutable_completion_conflict() -> None:
    memory = MemoryStorage()
    states = StateStore(memory)
    uri = join_uri("s3://safe/run", "_SUCCESS.json")
    states.put_immutable_json(uri, {"schema": "v1", "ok": True})
    states.put_immutable_json(uri, {"schema": "v1", "ok": True})
    with pytest.raises(Exception, match="different content"):
        states.put_immutable_json(uri, {"schema": "v1", "ok": False})


def test_stale_cancel_update_cannot_overwrite_concurrent_completion() -> None:
    memory = MemoryStorage()
    states = StateStore(memory)
    request = _submit()
    stale = states.claim(
        OperationRecord(
            idempotency_key=operation_key(request.workflow_run, request.state_id),
            request_sha256=sha256_bytes(
                canonical_json(request.model_dump(mode="json"))
            ),
            workflow_run=request.workflow_run,
            state_id=request.state_id,
            robot_type=request.robot_type,
            task=request.task,
            input_path=request.input_path,
            output_path=request.output_path,
            derived_project_id="npa-race-test",
            remote_kind="suite",
            selection=request.suite,
            status="running",
        )
    )
    immutable = {
        "artifact_manifest_uri": "s3://safe/run/manifests/v1.json",
        "dataset_uri": "s3://safe/run/dataset",
        "completion_uri": "s3://safe/run/_SUCCESS.json",
    }
    states.update(stale, status="completed", **immutable)
    result = states.update(stale, status="cancelled", remote_phase="cancelled")
    assert result.status == "completed"
    assert {key: getattr(result, key) for key in immutable} == immutable


def test_run_poll_loop_does_not_read_state_before_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AntiochManager.__new__(AntiochManager)
    request = _submit()
    claimed_record = OperationRecord(
            idempotency_key="a" * 64,
            request_sha256="b" * 64,
            workflow_run=request.workflow_run,
            state_id=request.state_id,
            robot_type=request.robot_type,
            task=request.task,
            input_path=request.input_path,
            output_path=request.output_path,
            derived_project_id="npa-poll-test",
            remote_kind="suite",
            selection=request.suite,
            status="claimed",
        )
    completed_record = OperationRecord(
            idempotency_key="a" * 64,
            request_sha256="b" * 64,
            workflow_run=request.workflow_run,
            state_id=request.state_id,
            robot_type=request.robot_type,
            task=request.task,
            input_path=request.input_path,
            output_path=request.output_path,
            derived_project_id="npa-poll-test",
            remote_kind="suite",
            selection=request.suite,
            remote_id="suite-safe",
            status="completed",
        )
    records = [claimed_record, completed_record]
    manager.submit = lambda _request: records.pop(0)  # type: ignore[method-assign]
    manager.collect = lambda _request: completed_record  # type: ignore[method-assign]
    manager.states = type(
        "NoReadState",
        (),
        {"read": lambda *a, **k: pytest.fail("poll loop performed a wasted S3 read")},
    )()
    monkeypatch.setattr("npa.workbench.antioch.manager.time.sleep", lambda _delay: None)
    completed = manager.run(request, poll_seconds=0)
    assert completed.remote_id == "suite-safe"


def test_artifact_verification_accepts_s3_metadata_header_casing(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "result.bin"
    artifact.write_bytes(b"verified")

    class S3:
        def upload_file(
            self, path: str, bucket: str, key: str, ExtraArgs: dict[str, Any]
        ) -> None:  # noqa: N803
            self.path = Path(path)
            self.metadata = ExtraArgs["Metadata"]

        def head_object(self, **kwargs):  # noqa: ANN003, ANN201
            return {
                "ContentLength": self.path.stat().st_size,
                "Metadata": {
                    "Sha256": self.metadata["sha256"],
                    "Npa-Role": "antioch-artifact",
                },
            }

    storage = type("Storage", (), {"s3": S3()})()
    record = StateStore(storage).upload_artifact(
        artifact, "s3://safe/result.bin", name="result.bin"
    )
    assert record.sha256 == sha256_bytes(b"verified")


def test_service_auth_fails_closed_without_exposing_token() -> None:
    client = TestClient(
        create_app(manager=object(), auth_mode="token", token="service-secret")
    )
    denied = client.get("/system-info")
    assert denied.status_code == 401
    assert "service-secret" not in denied.text
    allowed = client.get(
        "/system-info", headers={"Authorization": "Bearer service-secret"}
    )
    assert allowed.status_code == 200
    assert allowed.json()["cpu_only"] is True


def test_service_preserves_submit_dataset_metadata() -> None:
    captured: dict[str, SubmitRequest] = {}

    class Manager:
        def run(self, body: SubmitRequest) -> OperationRecord:
            captured["body"] = body
            return OperationRecord(
                idempotency_key="a" * 64,
                request_sha256="b" * 64,
                workflow_run=body.workflow_run,
                state_id=body.state_id,
                robot_type=body.robot_type,
                task=body.task,
                input_path=body.input_path,
                output_path=body.output_path,
                derived_project_id="npa-service-test",
                remote_kind="suite",
                selection=body.suite,
                status="completed",
            )

    client = TestClient(create_app(manager=Manager(), auth_mode="none"))
    body = _submit().model_dump(mode="json")
    body["robot_type"] = "inspection-arm"
    body["task"] = "Inspect the valve seal"
    response = client.post("/run", json=body)
    assert response.status_code == 200
    assert captured["body"].robot_type == "inspection-arm"
    assert captured["body"].task == "Inspect the valve seal"
