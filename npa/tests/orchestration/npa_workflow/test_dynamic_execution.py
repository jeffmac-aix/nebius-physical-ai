from __future__ import annotations

import json
from pathlib import Path

from npa.orchestration.npa_workflow import load_spec, run_workflow
from npa.orchestration.npa_workflow.decisions import (
    DECISION_LOOP_BACK,
    DECISION_PROMOTE,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
SPECS = REPO_ROOT / "npa" / "workflows" / "workbench" / "npa-workflows"
SIM2REAL_DEMO = (
    REPO_ROOT
    / "npa"
    / "tests"
    / "fixtures"
    / "npa-workflows"
    / "sim2real-vlm-rl-demo.yaml"
)
PAIDF = SPECS / "physical-ai-data-factory.yaml"


def test_dynamic_execute_reads_decision_for_promote(monkeypatch) -> None:
    spec = load_spec(SIM2REAL_DEMO)

    def fake_reader(_bucket: str, _key: str) -> str:
        return json.dumps({"decision": DECISION_PROMOTE})

    monkeypatch.setattr(
        "npa.orchestration.npa_workflow.interpreter._execute_step",
        lambda step, execute=True: {"state": step.state, "status": "ok"},
    )

    report = run_workflow(
        spec,
        run_id="dyn-promote",
        execute=True,
        decision_reader=fake_reader,
    )
    states = [step["state"] for step in report["steps"]]
    assert states.count("rollouts") == spec.config["inner_iterations"]
    assert states[-1] == "finalize"


def test_dynamic_execute_reads_decision_for_loop_back(monkeypatch) -> None:
    spec = load_spec(SIM2REAL_DEMO)
    decisions = iter([DECISION_LOOP_BACK, DECISION_PROMOTE])

    def fake_reader(_bucket: str, _key: str) -> str:
        return json.dumps({"decision": next(decisions, DECISION_PROMOTE)})

    monkeypatch.setattr(
        "npa.orchestration.npa_workflow.interpreter._execute_step",
        lambda step, execute=True: {"state": step.state, "status": "ok"},
    )

    report = run_workflow(
        spec,
        run_id="dyn-loop",
        execute=True,
        decision_reader=fake_reader,
    )
    states = [step["state"] for step in report["steps"]]
    assert (
        states.count("rollouts")
        == spec.config["inner_iterations"] * spec.config["outer_iterations"]
    )


def test_paidf_promoted_runtime_takes_full_visualization_path(monkeypatch) -> None:
    spec = load_spec(PAIDF)
    monkeypatch.setattr(
        "npa.orchestration.npa_workflow.interpreter._execute_step",
        lambda step, execute=True: {"state": step.state, "status": "ok"},
    )

    report = run_workflow(
        spec,
        run_id="paidf-promoted",
        execute=True,
        decision_reader=lambda _bucket, _key: json.dumps(
            {"decision": DECISION_PROMOTE}
        ),
    )

    states = [step["state"] for step in report["steps"]]
    assert states[-2:] == ["visualize", "finalize"]
    assert "visualize-rejected" not in states
    assert "reject-quality" not in states


def test_paidf_rejected_runtime_writes_rrd_before_rejecting(monkeypatch) -> None:
    spec = load_spec(PAIDF)
    monkeypatch.setattr(
        "npa.orchestration.npa_workflow.interpreter._execute_step",
        lambda step, execute=True: {"state": step.state, "status": "ok"},
    )

    report = run_workflow(
        spec,
        run_id="paidf-rejected",
        execute=True,
        decision_reader=lambda _bucket, _key: json.dumps(
            {"decision": DECISION_LOOP_BACK}
        ),
    )

    states = [step["state"] for step in report["steps"]]
    assert states[-2:] == ["visualize-rejected", "reject-quality"]
    assert "annotate-augmented" not in states
    assert states.index("quality-disposition") < states.index("visualize-rejected")
