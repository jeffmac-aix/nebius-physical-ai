"""Workflow tests for the RoboCasa native workbench."""

from __future__ import annotations

from pathlib import Path

from npa.orchestration.npa_workflow import build_plan, load_spec, validate_spec
from npa.orchestration.npa_workflow.catalog import TOOL_CATALOG, argv_for_tool

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / "npa" / "workflows" / "workbench" / "npa-workflows" / "robocasa-smoke.yaml"


def test_workflow_validates() -> None:
    spec = load_spec(WORKFLOW)
    validate_spec(spec)
    assert spec.name == "robocasa-smoke"
    assert spec.initial == "task-registration"


def test_workflow_expands_all_states() -> None:
    spec = load_spec(WORKFLOW)
    plan = build_plan(spec, run_id="test")
    states = [step.state for step in plan.steps]
    assert states == [
        "task-registration",
        "asset-availability",
        "egl-env-reset",
        "random-rollout",
    ]


def test_workflow_dependency_order_is_topological() -> None:
    spec = load_spec(WORKFLOW)
    assert spec.states["task-registration"].needs == []
    assert spec.states["asset-availability"].needs == ["task-registration"]
    assert spec.states["egl-env-reset"].needs == ["asset-availability"]
    assert spec.states["random-rollout"].needs == ["egl-env-reset"]


def test_robocasa_toolrefs_render() -> None:
    for tool_ref in (
        "workbench.robocasa.task_registration",
        "workbench.robocasa.asset_availability",
        "workbench.robocasa.egl_env_reset",
        "workbench.robocasa.random_rollout",
    ):
        assert tool_ref in TOOL_CATALOG
        argv = argv_for_tool(tool_ref)
        assert argv, tool_ref
        assert argv[:4] == ["npa", "workbench", "robocasa", "run"]
        assert "--capability" in argv
        assert "--output-uri" in argv
        assert "--service" in argv
        assert "--endpoint" in argv


def test_random_rollout_toolref_includes_iterations() -> None:
    argv = argv_for_tool("workbench.robocasa.random_rollout")
    assert "--iterations" in argv
    assert "--num-envs" in argv
