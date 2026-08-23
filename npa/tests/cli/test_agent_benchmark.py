from __future__ import annotations

import json
import concurrent.futures
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from npa.cluster.state import ClusterState, save_cluster_state

from npa.cli.agent_benchmark import (
    BenchmarkToolbox,
    StreamingPlanner,
    _execution_evidence,
    _sanitize,
    representative_context,
)


def test_streaming_planner_collects_usage_and_never_disables_tls() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        body = "\n".join(
            [
                "data: "
                + json.dumps(
                    {"id": "call-1", "choices": [{"delta": {"content": '{"final":"'}}]}
                ),
                "data: "
                + json.dumps(
                    {
                        "id": "call-1",
                        "choices": [
                            {"delta": {"content": 'ok"}'}, "finish_reason": "stop"}
                        ],
                    }
                ),
                "data: "
                + json.dumps(
                    {
                        "id": "call-1",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 3,
                            "total_tokens": 14,
                        },
                    }
                ),
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    planner = StreamingPlanner(
        endpoint="https://provider.example/v1",
        model="test-model",
        api_key="not-recorded",
        transport=httpx.MockTransport(handler),
    )
    response = planner([{"role": "user", "content": "test"}], phase="unit")

    assert response["choices"][0]["message"]["content"] == '{"final":"ok"}'
    assert planner.records[0]["usage"]["total_tokens"] == 14
    assert planner.records[0]["ttft_s"] is not None
    assert planner.records[0]["finish_reason"] == "stop"
    assert seen["url"] == "https://provider.example/v1/chat/completions"
    assert seen["authorization"] == "Bearer not-recorded"
    assert "not-recorded" not in json.dumps(planner.records)


def test_streaming_planner_rejects_non_tls_endpoint() -> None:
    with pytest.raises(ValueError, match="https"):
        StreamingPlanner(endpoint="http://provider.example/v1", model="m", api_key="k")


def test_streaming_planner_serializes_concurrent_record_callbacks() -> None:
    body = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
            "data: [DONE]",
            "",
        ]
    )
    planner = StreamingPlanner(
        endpoint="https://provider.example/v1",
        model="test-model",
        api_key="not-recorded",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=body)),
    )
    snapshots: list[list[int]] = []
    planner.on_record = lambda: snapshots.append(
        [int(record["call_index"]) for record in planner.records]
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda index: planner([{"role": "user", "content": str(index)}]),
                range(4),
            )
        )

    assert [record["call_index"] for record in planner.records] == [1, 2, 3, 4]
    assert snapshots == [[1], [1, 2], [1, 2, 3], [1, 2, 3, 4]]


def test_benchmark_toolbox_fails_closed_on_prerequisites_and_operation_digest(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "paidf-cosmos3.yaml"
    spec.write_text("apiVersion: npa.workflow/v0.0.1\n")
    state = {
        "run_id": "run-fixed",
        "operation_digest": "op-fixed",
        "completed_tools": [],
        "tool_calls": [],
    }
    toolbox = BenchmarkToolbox(
        repo=tmp_path,
        state=state,
        save=lambda: None,
        project="project-alias",
        cluster="cluster-context",
        bucket="bucket-name",
        accelerator="RTXPRO6000:1",
        registry="ghcr.io/nebius/nebius-physical-ai",
        rerun_image="",
        spec=spec,
    )

    mismatch = toolbox.execute("workflow_submit", {"operation_digest": "wrong"})
    assert mismatch["ok"] is False
    assert "operation_digest" in mismatch["error"]

    missing = toolbox.execute("workflow_submit", {"operation_digest": "op-fixed"})
    assert missing["ok"] is False
    assert "health_access" in missing["error"]
    assert state["tool_calls"] == [], "rejected proposals are not executed tool calls"


def test_cluster_state_reconcile_uses_fixed_selected_identity(
    tmp_path: Path, monkeypatch, mocker
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("NPA_CONFIG_DIR", str(config_dir))
    # agent_benchmark imports cluster.state lazily so its path constants observe
    # the benchmark's isolated config root in a fresh test process in production.
    import npa.cluster.state as cluster_state_module

    monkeypatch.setattr(cluster_state_module, "CLUSTERS_DIR", config_dir / "clusters")
    save_cluster_state(
        ClusterState(
            name="selected-context",
            provider_name="provider-cluster",
            cluster_id="cluster-id",
            project_id="project-id",
            region="us-central1",
            node_count=2,
            node_platform="gpu-rtx6000",
            node_preset="1gpu-24vcpu-218gb",
            k8s_version="1.32",
            subnet_id="subnet-id",
            created_at="2026-01-01T00:00:00Z",
            kubeconfig_path="/stale/global/kubeconfig",
        )
    )
    spec = tmp_path / "paidf-cosmos3.yaml"
    spec.write_text("apiVersion: npa.workflow/v0.0.1\n")
    state = {
        "run_id": "run-fixed",
        "operation_digest": "op-fixed",
        "completed_tools": ["infra_plan"],
        "tool_calls": [],
    }
    toolbox = BenchmarkToolbox(
        repo=tmp_path,
        state=state,
        save=lambda: None,
        project="project-alias",
        cluster="selected-context",
        bucket="bucket-name",
        accelerator="RTXPRO6000:1",
        registry="ghcr.io/nebius/nebius-physical-ai",
        rerun_image="",
        spec=spec,
    )
    command = mocker.patch.object(toolbox, "_command", return_value={"ok": True})

    result = toolbox.execute(
        "cluster_state_reconcile", {"operation_digest": "op-fixed"}
    )

    assert result["ok"] is True
    argv = command.call_args.args[0]
    assert argv == [
        str(tmp_path / "npa/.venv/bin/npa"),
        "cluster",
        "kubeconfig",
        "--cluster-name",
        "provider-cluster",
        "--project",
        "project-alias",
        "--context",
        "selected-context",
        "--kubeconfig",
        str(config_dir / "clusters" / "selected-context" / "kubeconfig"),
    ]


def test_toolbox_normalizes_configured_s3_bucket_for_workflow_argv(
    tmp_path: Path, mocker
) -> None:
    spec = tmp_path / "paidf-cosmos3.yaml"
    spec.write_text("apiVersion: npa.workflow/v0.0.1\n")
    state = {
        "run_id": "run-fixed",
        "operation_digest": "op-fixed",
        "completed_tools": [
            "health_access",
            "workflow_plan",
            "workflow_preflight_images",
            "rerun_image_verify",
            "skypilot_api_server",
        ],
        "tool_calls": [
            {
                "tool": "skypilot_api_server",
                "ok": True,
                "observation": {"result": {"context_bound": True}},
            }
        ],
        "rerun_image": "registry.example/npa-rerun-viewer@sha256:" + "1" * 64,
    }
    toolbox = BenchmarkToolbox(
        repo=tmp_path,
        state=state,
        save=lambda: None,
        project="project-alias",
        cluster="cluster-context",
        bucket="s3://bucket-name",
        accelerator="RTXPRO6000:1",
        registry="ghcr.io/nebius/nebius-physical-ai",
        rerun_image="",
        spec=spec,
    )
    command = mocker.patch.object(toolbox, "_command", return_value={"ok": True})

    result = toolbox.execute("workflow_submit", {"operation_digest": "op-fixed"})

    assert result["ok"] is True
    argv = command.call_args.args[0]
    assert "bucket=bucket-name" in argv
    assert not any("s3://bucket-name" in item for item in argv)
    assert toolbox.command_env["NPA_WORKFLOW_GPU_ACCELERATOR"] == "RTXPRO6000:1"
    assert toolbox.command_env["NPA_SKYPILOT_BIN"].endswith("/skypilot-venv/bin/sky")
    assert toolbox.command_env["SKYPILOT_API_SERVER_ENDPOINT"].startswith(
        "http://127.0.0.1:"
    )


def test_toolbox_resumes_failed_submission_and_allows_status_observation(
    tmp_path: Path, mocker
) -> None:
    spec = tmp_path / "paidf-cosmos3.yaml"
    spec.write_text("apiVersion: npa.workflow/v0.0.1\n")
    state = {
        "run_id": "run-fixed",
        "operation_digest": "op-fixed",
        "completed_tools": [
            "health_access",
            "workflow_plan",
            "workflow_preflight_images",
            "rerun_image_verify",
            "skypilot_api_server",
        ],
        "tool_calls": [
            {
                "tool": "skypilot_api_server",
                "ok": True,
                "observation": {"result": {"context_bound": True}},
            }
        ],
    }
    toolbox = BenchmarkToolbox(
        repo=tmp_path,
        state=state,
        save=lambda: None,
        project="project-alias",
        cluster="cluster-context",
        bucket="bucket-name",
        accelerator="RTXPRO6000:1",
        registry="ghcr.io/nebius/nebius-physical-ai",
        rerun_image="registry.example/npa-rerun-viewer@sha256:" + "1" * 64,
        spec=spec,
    )
    command = mocker.patch.object(
        toolbox,
        "_command",
        side_effect=[
            {"ok": False, "error": "failed workload"},
            {"ok": True, "status": "failed"},
            {"ok": True, "status": "succeeded"},
        ],
    )

    first = toolbox.execute("workflow_submit", {"operation_digest": "op-fixed"})
    status = toolbox.execute("workflow_status", {"run_id": "run-fixed"})
    second = toolbox.execute("workflow_submit", {"operation_digest": "op-fixed"})

    assert first["ok"] is False
    assert status["ok"] is True, status
    assert second["ok"] is True
    first_argv = command.call_args_list[0].args[0]
    second_argv = command.call_args_list[2].args[0]
    assert first_argv[first_argv.index("--run-id") + 1] == "run-fixed"
    assert "--resume-run" not in first_argv
    assert second_argv[second_argv.index("--resume-run") + 1] == "run-fixed"
    assert "--run-id" not in second_argv
    assert "--resume" in second_argv
    assert second_argv[second_argv.index("--retries") + 1] == "1"


def test_toolbox_rejects_bucket_prefix(tmp_path: Path) -> None:
    spec = tmp_path / "paidf-cosmos3.yaml"
    spec.write_text("apiVersion: npa.workflow/v0.0.1\n")

    with pytest.raises(ValueError, match="without a prefix"):
        BenchmarkToolbox(
            repo=tmp_path,
            state={"run_id": "run", "operation_digest": "op", "tool_calls": []},
            save=lambda: None,
            project="project-alias",
            cluster="cluster-context",
            bucket="s3://bucket-name/prefix",
            accelerator="RTXPRO6000:1",
            registry="ghcr.io/nebius/nebius-physical-ai",
            rerun_image="",
            spec=spec,
        )


def test_remediation_requires_observed_preflight_failure_and_digest_binding(
    tmp_path: Path, mocker
) -> None:
    spec = tmp_path / "paidf-cosmos3.yaml"
    spec.write_text("apiVersion: npa.workflow/v0.0.1\n")
    state = {
        "run_id": "run-fixed",
        "operation_digest": "op-fixed",
        "completed_tools": ["skypilot_verify"],
        "tool_calls": [],
    }
    toolbox = BenchmarkToolbox(
        repo=tmp_path,
        state=state,
        save=lambda: None,
        project="project-alias",
        cluster="cluster-context",
        bucket="bucket-name",
        accelerator="RTXPRO6000:1",
        registry="ghcr.io/nebius/nebius-physical-ai",
        rerun_image="",
        spec=spec,
    )

    premature = toolbox.execute("registry_plan", {})
    assert premature["ok"] is False
    assert "preflight failure" in premature["error"]

    state["tool_calls"].append(
        {"tool": "workflow_preflight_images", "ok": False, "observation": {}}
    )
    command = mocker.patch.object(
        toolbox,
        "_command",
        return_value={"ok": True, "result": {"outcome": "planned_create"}},
    )
    planned = toolbox.execute("registry_plan", {})
    assert planned["ok"] is True
    argv = command.call_args.args[0]
    assert argv[:3] == [str(tmp_path / "npa/.venv/bin/npa"), "registry", "ensure"]
    assert "--yes" not in argv

    state["completed_tools"].extend(
        ["registry_provision", "rerun_image_build", "rerun_image_inspect"]
    )
    state["tool_calls"].append(
        {
            "tool": "rerun_image_inspect",
            "ok": True,
            "observation": {
                "result": {
                    "image_id": "sha256:" + "1" * 64,
                    "inspection_digest": "2" * 64,
                }
            },
        }
    )
    rejected = toolbox.execute(
        "rerun_image_push",
        {
            "operation_digest": "op-fixed",
            "image_id": "sha256:" + "9" * 64,
            "inspection_digest": "2" * 64,
        },
    )
    assert rejected["ok"] is False
    assert "prior exact inspection" in rejected["error"]


def test_sanitizer_removes_credentials_and_live_identifiers() -> None:
    raw = {
        "project": "project-live123456",
        "authorization": "Bearer abcdef",
        "nested": "api_key=supersecret",
    }
    sanitized = _sanitize(raw, {"project-live123456": "<project-alias>"})
    encoded = json.dumps(sanitized)
    assert "project-live123456" not in encoded
    assert "abcdef" not in encoded
    assert "supersecret" not in encoded
    assert "<redacted>" in encoded


def test_sanitizer_removes_bare_nebius_account_ids() -> None:
    identifier = "u00" + "a" * 16
    sanitized = _sanitize(f"cr.us-central1.nebius.cloud/{identifier}/image:tag", {})
    assert identifier not in sanitized
    assert sanitized == "<task-registry>"


def test_representative_context_uses_real_files_and_is_bounded() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    context, manifest = representative_context(repo_root, max_chars=5_000)
    assert context
    assert (
        len(context) <= 5_500
    )  # section headers are intentionally outside the content cap
    assert manifest[0]["path"] == "AGENTS.md"
    assert all(item["sha256"] for item in manifest)
    assert not any("padding" in item["path"] for item in manifest)


def test_execution_evidence_extracts_stage_and_resource_seconds() -> None:
    evidence = _execution_evidence(
        {
            "tool_calls": [
                {
                    "tool": "workflow_status",
                    "observation": {
                        "stages": [
                            {
                                "stage": "generate",
                                "duration_s": 12.5,
                                "gpu_seconds": 25.0,
                            }
                        ]
                    },
                },
                {"tool": "health_access", "observation": {"elapsed_s": 99}},
            ]
        }
    )
    assert evidence["availability"] == "measured"
    assert evidence["stage_timings"] == [
        {"stage": "generate", "metric": "duration_s", "seconds": 12.5}
    ]
    assert evidence["resource_measurements"] == [
        {"stage": "generate", "metric": "gpu_seconds", "value": 25.0}
    ]


def test_committed_report_schema_accepts_sanitized_minimum() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    schema = json.loads(
        (repo_root / "docs/workbench/agent-benchmark-report.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(
        {
            "schema": "npa.agent.benchmark.v1",
            "status": "complete",
            "provider": {
                "kind": "openai_compatible",
                "model": "model",
                "tls_verification": True,
                "endpoint_disclosed": False,
            },
            "operation": {
                "digest": "a" * 24,
                "workflow": "paidf-cosmos3",
                "seed_fixture": True,
                "variant_count": 1,
                "run_id_digest": "b" * 16,
            },
            "model_calls": [],
            "tool_calls": [],
            "summary": {
                "wall_s": 0,
                "model_calls": 0,
                "tool_calls": 0,
                "usage": {},
                "execution_evidence": {
                    "stage_timings": [],
                    "resource_measurements": [],
                    "source_tool_calls": 0,
                    "availability": "not_reported",
                },
                "cost": {
                    "monetary": None,
                    "status": "unavailable",
                    "measured_token_usage": {},
                },
            },
        }
    )
