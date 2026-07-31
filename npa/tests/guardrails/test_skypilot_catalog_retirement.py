"""Guardrail: the raw SkyPilot task catalog only ever shrinks.

`npa.workflow/v0.0.1` specs are becoming the only workflow authoring surface. The
raw SkyPilot task templates under ``npa/src/npa/workflows/skypilot/`` are being
retired one verified port at a time, so this guardrail pins the exact remaining
set. Two properties matter to a reviewer:

* **No re-additions.** A new raw SkyPilot task YAML cannot appear without editing
  this list, which forces the question "why is this not an npa.workflow spec?".
* **A machine-checked tally.** Each retirement PR shows the count going down in a
  single readable diff, instead of a prose claim in a PR body.

Deleting an entry from ``REMAINING`` is the *last* step of a retirement: the twin
spec must already have a live run recorded in ``EVIDENCE.md``.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKYPILOT_DIR = REPO_ROOT / "npa" / "src" / "npa" / "workflows" / "skypilot"

#: Raw SkyPilot task templates still shipped, with the reason each one survives.
#: Retirement tally: started at 36.
REMAINING: dict[str, str] = {
    # --- loaded and launched by a shipped runner script ---
    "bdd100k-pipeline.yaml": "npa/scripts/run_bdd100k_pipeline.py DEFAULT_YAML",
    "byof-container-smoke-rtxpro.yaml": "npa/scripts/run_byof_container_verify.py",
    "byof-datagen-rtxpro-smoke.yaml": "npa/scripts/run_byof_datagen.py",
    "isaac-lab-rl-train.yaml": "npa/scripts/run_isaac_lab_rl.py DEFAULT_YAML",
    "isaac-lab-rl-train-rtxpro.yaml": "byof/live.py resource profile",
    "isaac-lab-rl-train-rtxpro-smoke.yaml": "byof/live.py resource profile",
    "sim-to-real-pipeline.yaml": "npa/scripts/run_sim_to_real_pipeline.py DEFAULT_YAML",
    # --- referenced by a CLI/SDK path pointer or shipped data ---
    "retargeting.yaml": "cli/workbench/retargeting.py WORKFLOW_PATH",
    "sim-to-real-loop.yaml": "solutions.toml sim-to-real cli_command",
    "sim-to-real-trigger.yaml": "three-tier contract (legacy YAML tier)",
    "sonic-train-standalone.yaml": "three-tier contract + standalone-policy guardrail",
    "vlm-eval.yaml": "cli/workbench/vlm_eval.py WORKFLOW_PATH",
    "vlm-eval-benchmark.yaml": "cli/workbench/vlm_eval.py BENCHMARK_WORKFLOW_PATH",
    # --- no npa.workflow twin authored yet ---
    "cosmos2-transfer.yaml": "no twin; cosmos2.transfer is used via other specs",
    "cosmos3-ea-fetch.yaml": "no twin; access check, overlaps `npa workbench cosmos check`",
    "cosmos3-text-to-image-inference.yaml": "no twin; raw-sky e2e test targets it",
    "dataset-ingest-curate.yaml": "twin exists but has no live-matrix coverage yet",
    "isaac-franka-capture-reason.yaml": "no twin",
    "isaac-lab-cosmos-sdg-burst-smoke.yaml": "no twin; single-task burst reference",
    "scenario-gen-adversarial.yaml": "no twin with live coverage",
    "sim2real-actions.yaml": "no twin",
    "sim2real-envgen-split.yaml": "no twin",
    "tokenfactory-rollout-judge.yaml": "twin live-verified; retire with the rest",
    "tokenfactory-scene-to-rollout-judge.yaml": "no twin",
    "tokenfactory-train-triage.yaml": "no twin",
    # --- twin exists but is NOT live-verified yet ---
    "sonic-locomotion-finetuning.yaml": (
        "twin needs a real SOMA/G1 motion dataset (NPA_E2E_SONIC_MOTION_SRC) that the "
        "repo does not vendor; not verified live yet, so not retired"
    ),
    # --- RETIRED here: twin live-verified, see EVIDENCE.md -----------------------
    # cosmos3-reason.yaml     job 182            npa-wf-gpu-cosmos3-reason-af7ded35
    # isaac-lab-rl-sweep.yaml jobs 185/186/187   npa-wf-multi-isaac-lab-rl-sweep-c4b86dc5
    # sonic-export.yaml       job 192            npa-wf-gpu-sonic-export-cb60c5ab
    # sonic-eval.yaml         job 198            npa-wf-gpu-sonic-eval-bb3b9c72
    # sonic-export-eval.yaml  job 197            npa-wf-multi-sonic-export-eval-2f5e979e
    #
    # Phase 2a (pointer-only CLI callers repointed first):
    # token-factory-caption.yaml       job 199  npa-wf-cpu-token-factory-caption-1dbebbb4
    # vlm-eval-token-factory.yaml      job 200  npa-wf-cpu-vlm-eval-token-factory-736df0b1
    # token-factory-cosmos-reason.yaml job 201  npa-wf-cpu-token-factory-cosmos-reason-d9669c7f
    # token-factory-generate.yaml      job 202  npa-wf-cpu-token-factory-generate-94815797
    # mjlab-eval.yaml                  no template-specific live run needed: the CLI
    #   constant is a printed path (now the spec) and the twin is a gpu-tier matrix
    #   case; see EVIDENCE §R10 for why it is retired without its own GPU run.
}


def test_remaining_skypilot_templates_match_the_pinned_tally() -> None:
    on_disk = {path.name for path in SKYPILOT_DIR.glob("*.yaml")}
    pinned = set(REMAINING)

    added = sorted(on_disk - pinned)
    assert not added, (
        "new raw SkyPilot task YAML(s) appeared in the retiring catalog: "
        f"{added}. Author an npa.workflow/v0.0.1 spec under "
        "npa/workflows/workbench/npa-workflows/ instead; if a raw template is "
        "genuinely required, add it to REMAINING with a reason."
    )
    removed = sorted(pinned - on_disk)
    assert not removed, (
        f"REMAINING lists templates that are already deleted: {removed}. "
        "Drop them from the list in the same change that deletes the files."
    )


def test_every_remaining_template_states_why_it_survives() -> None:
    unexplained = sorted(name for name, reason in REMAINING.items() if not reason.strip())
    assert not unexplained, f"REMAINING entries need a reason: {unexplained}"


#: Workbench CLI modules that advertise a workflow file through a module constant.
#: These are printed by `<tool> workflow` / `<tool> status`, so a retired template
#: silently turns the advertised path into a 404 for the operator who copies it.
CLI_WORKFLOW_PATH_MODULES = (
    "npa.cli.workbench.mjlab",
    "npa.cli.workbench.retargeting",
    "npa.cli.workbench.token_factory",
    "npa.cli.workbench.vlm_eval",
)


def test_cli_advertised_workflow_paths_exist() -> None:
    """Every `*_WORKFLOW_PATH` a CLI prints must be a real file."""

    from importlib import import_module
    from pathlib import Path as _Path

    missing: list[str] = []
    checked = 0
    for module_name in CLI_WORKFLOW_PATH_MODULES:
        module = import_module(module_name)
        for attr in dir(module):
            if not attr.endswith("WORKFLOW_PATH"):
                continue
            value = getattr(module, attr)
            if not isinstance(value, _Path):
                continue
            checked += 1
            if not (REPO_ROOT / value).is_file():
                missing.append(f"{module_name}.{attr} -> {value}")
    assert checked >= 8, f"expected to check several CLI workflow paths, saw {checked}"
    assert not missing, "CLI modules advertise workflow files that do not exist: " + ", ".join(
        missing
    )


def test_retirement_tally_is_monotonic() -> None:
    """The catalog started at 36 templates; it may only get smaller."""

    assert len(REMAINING) <= 36, (
        f"the SkyPilot catalog grew to {len(REMAINING)} templates; it is being retired"
    )
