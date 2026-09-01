from __future__ import annotations

from pathlib import Path

import yaml

from npa.orchestration.npa_workflow import build_plan, load_spec
from npa.orchestration.npa_workflow.skypilot_render import (
    SkypilotRenderOptions,
    render_skypilot_yaml,
    secret_env_hints_for_plan,
)
from npa.workbench.cosmos.super_benchmark import IMAGE, MODEL_REVISION, WORKLOAD


REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_PATH = (
    REPO_ROOT
    / "npa/workflows/workbench/npa-workflows/cosmos3-super-b200-benchmark.yaml"
)


def test_workflow_is_fixed_full_node_primary_sweep() -> None:
    raw = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    assert raw["resources"]["b200-node"]["accelerators"] == "B200:8"
    assert raw["resources"]["b200-node"]["image"] == IMAGE
    assert raw["config"]["topologies"] == "1x8,2x4,4x2,8x1"
    assert raw["config"]["attempts"] == "24"
    assert raw["states"]["benchmark"]["toolRef"] == "workbench.cosmos3.super_benchmark"
    assert raw["resources"]["b200-node"]["kubernetes"]["pod_config"]["spec"][
        "volumes"
    ][0]["emptyDir"]["sizeLimit"] == "32Gi"
    assert MODEL_REVISION in SPEC_PATH.parent.parent.parent.parent.joinpath(
        "src/npa/workbench/cosmos/super_benchmark.py"
    ).read_text(encoding="utf-8")
    assert WORKLOAD["guardrails"] is False


def test_workflow_renders_exact_vendor_digest_and_real_command(monkeypatch) -> None:
    monkeypatch.setenv("NPA_SRC_S3_URI", "s3://example-bucket/npa-src")
    spec = load_spec(SPEC_PATH)
    plan = build_plan(spec, run_id="cosmos3-super-test")
    rendered = render_skypilot_yaml(
        spec,
        plan,
        run_id="cosmos3-super-test",
        options=SkypilotRenderOptions(),
    )
    docs = [item for item in yaml.safe_load_all(rendered) if item]
    assert docs[1]["resources"]["image_id"] == f"docker:{IMAGE}"
    assert docs[1]["resources"]["accelerators"] == "B200:8"
    assert "npa workbench cosmos3 super-benchmark" in docs[1]["run"]
    assert "--attempts 24" in docs[1]["run"]
    hints = secret_env_hints_for_plan(plan.steps)
    assert "HF_TOKEN" in hints
    assert "NPA_COSMOS3_ACCEPT_NVIDIA_SOFTWARE_LICENSE" in hints
