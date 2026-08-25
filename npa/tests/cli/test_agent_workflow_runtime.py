from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from typer.testing import CliRunner

from npa.agent_backend import workflow_runtime
from npa.agent_backend.workflow_runtime import WorkflowRuntimeResult
from npa.cli.main import app


SCOPE = "a" * 24


def test_prepare_workflow_runtime_returns_backend_neutral_typed_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir = tmp_path / "runtime"
    target_access = tmp_path / "target-access"
    monkeypatch.setattr(
        workflow_runtime,
        "_runtime_paths",
        lambda _scope, _cluster: (state_dir, target_access, 48123),
    )
    monkeypatch.setattr(
        "npa.cluster.state.load_cluster_state",
        lambda _cluster: SimpleNamespace(
            provider_name="provider-target", project_id="project-id"
        ),
    )
    monkeypatch.setattr(
        "npa.clients.config.resolve_environment",
        lambda _project: SimpleNamespace(project_id="project-id"),
    )
    monkeypatch.setattr(
        "npa.cli.skypilot.bootstrap_skypilot",
        lambda: SimpleNamespace(sky_bin=tmp_path / "runtime-bin", reused=True),
    )
    monkeypatch.setattr(
        "npa.orchestration.skypilot.api_server.ensure_isolated_api_server",
        lambda **_kwargs: SimpleNamespace(reused=True),
    )
    calls: list[tuple[str, ...]] = []

    def run_npa(args, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(tuple(args))
        if args[:2] == ("cluster", "kubeconfig"):
            target_access.write_text("access")
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(workflow_runtime, "_run_npa", run_npa)

    result = workflow_runtime.prepare_workflow_runtime(
        project="project-alias", cluster="target-context", scope=SCOPE
    )

    assert result == WorkflowRuntimeResult(
        status="ready",
        runtime_ready=True,
        target_ready=True,
        context_bound=True,
        reused=True,
    )
    assert calls[0][:2] == ("cluster", "kubeconfig")
    assert calls[1][:2] == ("skypilot", "verify")
    public = json.dumps(result.to_dict()).lower()
    assert not any(term in public for term in ("skypilot", "kubectl", "tmux", "port"))


def test_workflow_runtime_cli_emits_one_typed_json_document(monkeypatch) -> None:
    monkeypatch.setattr(
        "npa.cli.agent_workflow_runtime.prepare_workflow_runtime",
        lambda **_kwargs: WorkflowRuntimeResult(
            status="ready",
            runtime_ready=True,
            target_ready=True,
            context_bound=True,
            reused=False,
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "workflow-runtime",
            "prepare",
            "--project",
            "project-alias",
            "--cluster",
            "target-context",
            "--scope",
            SCOPE,
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ready"


def test_workflow_runtime_rejects_non_digest_scope() -> None:
    result = CliRunner().invoke(
        app,
        [
            "agent",
            "workflow-runtime",
            "status",
            "--cluster",
            "target-context",
            "--scope",
            "../../unsafe",
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code != 0
    assert "unsafe" not in result.stdout


def test_stop_workflow_runtime_requires_matching_owner_record(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    (state_dir / "runtime.json").write_text(
        json.dumps(
            {
                "schema": "npa.agent.workflow-runtime.v1",
                "project": "project-alias",
                "cluster": "other-target",
                "scope": SCOPE,
                "target_verified": True,
            }
        )
    )
    monkeypatch.setattr(
        workflow_runtime,
        "_runtime_state_dir",
        lambda _scope: state_dir,
    )

    try:
        workflow_runtime.stop_workflow_runtime(
            cluster="target-context", scope=SCOPE
        )
    except workflow_runtime.WorkflowRuntimeError as exc:
        assert exc.code == "runtime_owner_mismatch"
    else:
        raise AssertionError("mismatched runtime owner must fail closed")
