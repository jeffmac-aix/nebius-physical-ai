"""Guardrail: a spec may not hand a stage a path inside the repo checkout.

``vlm-eval-benchmark.yaml`` passed
``--dataset npa/src/npa/workbench/vlm_eval/fixtures/sample_benchmark/benchmark.json``.
That resolves on a developer's laptop and *never* in the pod the stage actually runs in,
where the repo is not checked out at that path — so the stage could only fail with
"benchmark dataset not found", and the spec looked plausible in review.

The check is deliberately narrow so it cannot produce false positives: it fires only when
an argv value is a *relative* path that also *exists in this repo*. A value like that is
always a mistake, because a stage's filesystem is a container image plus staged artifacts,
never the checkout. Real inputs belong in object storage (``s3://``), which the live
harness seeds, or at an absolute path inside the image.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from npa.orchestration.npa_workflow.blueprints import iter_npa_workflow_specs
from npa.orchestration.npa_workflow.interpreter import build_plan
from npa.orchestration.npa_workflow.spec import load_spec

REPO_ROOT = Path(__file__).resolve().parents[3]


def _argv_values() -> list[tuple[str, str, str]]:
    """Return (spec name, state, value) for every argv value worth checking."""

    out: list[tuple[str, str, str]] = []
    for path in iter_npa_workflow_specs():
        spec = load_spec(path)
        assume = (
            "promote_checkpoint"
            if any(state.transitions for state in spec.states.values())
            else None
        )
        plan = build_plan(spec, run_id="repo-relative-check", assume_decision=assume)
        for step in plan.steps:
            for value in step.argv:
                # "/" filters out single words ("json", "default") that could collide
                # with a top-level repo entry by accident.
                if value.startswith("-") or "/" not in value:
                    continue
                out.append((path.name, step.state, value))
    return out


CASES = _argv_values()


def test_there_are_argv_values_to_check() -> None:
    assert len(CASES) >= 50, f"expected many argv values across the catalog, got {len(CASES)}"


@pytest.mark.parametrize(
    ("spec_name", "state", "value"),
    CASES,
    ids=[f"{name}:{state}:{index}" for index, (name, state, _) in enumerate(CASES)],
)
def test_argv_value_is_not_a_repo_relative_path(spec_name: str, state: str, value: str) -> None:
    candidate = Path(value)
    if candidate.is_absolute() or "://" in value:
        return

    assert not (REPO_ROOT / candidate).exists(), (
        f"{spec_name}:{state} passes {value!r}, which is a path inside this repo. The "
        "stage runs in a pod where the checkout does not exist, so it can only fail. "
        "Use an s3:// URI (the live harness seeds inputs) or an absolute path in the image."
    )


def test_the_guardrail_would_have_caught_the_original_bug() -> None:
    """The exact value that shipped in vlm-eval-benchmark.yaml must be rejected."""

    original = "npa/src/npa/workbench/vlm_eval/fixtures/sample_benchmark/benchmark.json"

    assert (REPO_ROOT / original).exists(), "fixture moved; update this regression anchor"
    with pytest.raises(AssertionError, match="inside this repo"):
        test_argv_value_is_not_a_repo_relative_path("spec.yaml", "state", original)
