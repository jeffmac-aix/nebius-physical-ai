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
    "bdd100k-pipeline.yaml": (
        "runner PORTED: run_bdd100k_pipeline.py now renders the spec, and its "
        "--mock-endpoints mode drives every stage's real argv against stand-in "
        "services. Survives only because a LIVE run needs the LanceDB workbench "
        "service, which is not deployed (EVIDENCE.md \u00a7R16, \u00a7R26); docs still "
        "cite it as the raw-YAML authoring reference"
    ),
    "sim-to-real-pipeline.yaml": "npa/scripts/run_sim_to_real_pipeline.py DEFAULT_YAML",
    # --- referenced by a CLI/SDK path pointer or shipped data ---
    "sim-to-real-trigger.yaml": "three-tier contract (legacy YAML tier)",
    "sonic-train-standalone.yaml": "three-tier contract + standalone-policy guardrail",
    # --- no npa.workflow twin authored yet ---
    "cosmos2-transfer.yaml": "no twin; cosmos2.transfer is used via other specs",
    "cosmos3-ea-fetch.yaml": "no twin; access check, overlaps `npa workbench cosmos check`",
    "cosmos3-text-to-image-inference.yaml": "no twin; raw-sky e2e test targets it",
    "dataset-ingest-curate.yaml": "twin exists but has no live-matrix coverage yet",
    "isaac-franka-capture-reason.yaml": "no twin",
    "isaac-lab-cosmos-sdg-burst-smoke.yaml": "no twin; single-task burst reference",
    "sim2real-actions.yaml": "no twin",
    "sim2real-envgen-split.yaml": "no twin",
    "tokenfactory-rollout-judge.yaml": (
        "the same-named spec is NOT an equivalent twin: the template's first stage is a "
        "LeRobot eval rollout on a GPU that PRODUCES the rollouts its judge stage scores, "
        "while the spec's first stage is an unrelated Cosmos scene reasoner and its judge "
        "reads rollouts seeded from outside. Retiring needs a spec whose producer stage "
        "feeds the judge (workbench.lerobot.eval -> workbench.vlm_eval.run)"
    ),
    "tokenfactory-scene-to-rollout-judge.yaml": "no twin",
    "tokenfactory-train-triage.yaml": "no twin",
    # --- twin exists but is NOT live-verified yet ---
    "sonic-locomotion-finetuning.yaml": (
        "twin's retarget stage passes live (job 205) but its train stage asks the in-pod "
        "CLI to launch a Nebius SERVERLESS job (nested infrastructure) and fails with "
        "'--runtime serverless requires --project-id'; see EVIDENCE.md \u00a7R11"
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
    # mjlab-eval.yaml                  job 203  npa-wf-gpu-mjlab-eval-32c1efb5
    #
    # Phase 2b:
    # retargeting.yaml                 job 204  npa-wf-cpu-retargeting-b8e5bc8b
    #   (was FAILING before this change for lack of motion data - EVIDENCE §6.1)
    #
    # Phase 3b:
    # vlm-eval.yaml           job 219  npa-wf-gpu-vlm-eval-single-25906482
    # vlm-eval-benchmark.yaml job 220  npa-wf-gpu-vlm-eval-benchmark-e47bc877
    #   (both needed the renderer to START the vLLM server the spec asks for)
    # sim-to-real-loop.yaml   job 218  npa-wf-gpu-vlm-eval-loop-88da76ad
    #   retired via the NEW `npa workbench vlm-eval loop` capability, not via the staged
    #   engine: nothing else produced task_success_report.json. See EVIDENCE.md §R18-R20.
    #
    # Phase 3b (continued):
    # scenario-gen-adversarial.yaml  job 213  npa-wf-cpu-scenario-gen-smoke-bc5ed74b
    #   twin = scenario-gen-smoke.yaml, which runs the SAME two CLI commands. The template's
    #   Isaac Lab image + 200000 adversary steps selected no different code path: the RL
    #   adversary is a Python-API seam with no CLI flag. See EVIDENCE.md §R25.
    #
    # Phase 2c: RELOCATED (not deleted) to npa/src/npa/workflows/byof/profiles/ —
    # they are BYOF resource profiles reached through byof.yaml's toolRef, not
    # workflow templates. See that directory's README.md and
    # npa/tests/guardrails/test_byof_profiles.py.
    # byof-container-smoke-rtxpro.yaml, byof-datagen-rtxpro-smoke.yaml,
    # isaac-lab-rl-train.yaml, isaac-lab-rl-train-rtxpro.yaml,
    # isaac-lab-rl-train-rtxpro-smoke.yaml
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


#: The skill that lists the reference templates for an operator to start from. Its list
#: went stale the moment Phase 2 deleted six templates, which is exactly the drift a
#: reader would trust and be misled by.
REFERENCE_SKILL = (
    REPO_ROOT / "skills" / "workflows" / "workbench-reference-workflows" / "SKILL.md"
)


def test_reference_skill_lists_exactly_the_remaining_templates() -> None:
    """The skill's "Current Reference YAMLs" section must match the directory."""

    import re

    text = REFERENCE_SKILL.read_text(encoding="utf-8")
    start = text.index("## Current Reference YAMLs")
    section = text[start : text.index("## Retired Templates", start)]
    listed = set(re.findall(r"`([a-z0-9][a-z0-9.-]*\.yaml)`", section))

    assert listed == set(REMAINING), (
        "skills/workflows/workbench-reference-workflows/SKILL.md advertises a different set "
        f"of templates than the catalog holds. Only in the skill: {sorted(listed - set(REMAINING))}. "
        f"Only on disk: {sorted(set(REMAINING) - listed)}."
    )


def test_retirement_tally_is_monotonic() -> None:
    """The catalog started at 36 templates; it may only get smaller."""

    assert len(REMAINING) <= 36, (
        f"the SkyPilot catalog grew to {len(REMAINING)} templates; it is being retired"
    )
