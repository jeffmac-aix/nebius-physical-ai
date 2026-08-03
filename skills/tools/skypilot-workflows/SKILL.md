---
name: skypilot-workflows
description: Use when running or debugging how the engine renders and submits SkyPilot from an npa.workflow spec — the invocation path, SkyPilot 0.12.2 limits, JobGroups, num_nodes, and the runner scripts. Not for authoring: pipelines are npa.workflow specs (see author-npa-workflow).
---

# SkyPilot Workflows

SkyPilot is the sole workflow **execution engine** in this repo. Argo is deprecated; do not add or revive Argo workflows.

It is no longer the authoring surface. Pipelines are `npa.workflow/v0.0.1` specs, and the engine renders SkyPilot tasks from them — so this skill is about how that rendering and submission behave, not about writing task YAML. To author a pipeline, use `skills/workflows/author-npa-workflow/SKILL.md`. The shipped raw-task catalog is retired (`CHANGELOG.md`); `npa workbench workflow submit` still accepts a customer's own SkyPilot YAML, which is what the runner-script path below exercises.

## Invocation

SkyPilot lives in an isolated virtualenv outside NPA's main Python environment. Invoke it through `NPA_SKYPILOT_BIN`; never rely on `sky` from `PATH`.

Use `npa skypilot bootstrap` to create or reuse the pinned SkyPilot `0.12.2`
venv, then set `NPA_SKYPILOT_BIN="$(npa skypilot status --bin-path)"`.

The Kubernetes controller is the default path (`W9-skypilot-k8s-controller`). The VM controller exists only as a fallback.

## Known SkyPilot 0.12.2 Limits

- `envs` does not support self-referencing variable interpolation. Use explicit comment blocks for alternatives, following the `BDD100K_LABEL_MAP` pattern in `npa/workflows/workbench/npa-workflows/bdd100k-pipeline.yaml`.
- `sky jobs launch` has no dry-run flag. Use mock-endpoint mode for validation before live submission.
- Mixed serial/parallel task groups in one YAML are not fully supported. Serialize the workflow if needed.
- Managed-job Python API `Dag` support is effectively single-task for this repo's burst path. Use `npa burst submit-yaml` only for rendered single-task SkyPilot YAMLs; use `npa workbench workflow submit` for multi-stage workbench YAMLs.
- Direct Nebius burst jobs pull `resources.image_id` before YAML `setup` runs. For private `cr.*.nebius.cloud` images, the submitter must inject SkyPilot Docker login config (`SKYPILOT_DOCKER_SERVER`, `SKYPILOT_DOCKER_USERNAME`, `SKYPILOT_DOCKER_PASSWORD`) into task secrets before launch. `npa burst submit-yaml` does this by minting a short-lived Nebius IAM token when the submitter has Nebius credentials.

## What the Renderer Emits

`npa/src/npa/orchestration/npa_workflow/skypilot_render.py` turns a planned spec into SkyPilot documents. The parts worth knowing when debugging a live run:

- **Fan-out becomes a JobGroup.** A state with a `sequence:` list launches as one SkyPilot JobGroup; the next state is a barrier.
- **`num_nodes` is a task-level field, not a resource one.** A profile declaring `num_nodes: 2` makes SkyPilot gang-schedule two identical pods and export `SKYPILOT_NODE_RANK` / `SKYPILOT_NODE_IPS`.
- **Setup is per-toolRef.** Extras (`npa[sonic]`), third-party pip requirements, and vendor interpreters are chosen from the toolRef, so a stage on the default image gets what it needs without the spec asking.
- **Some stages need a *run* preamble, not just setup.** A self-hosted VLM stage starts and health-checks a vLLM server inside the `run:` script, because a server started in `setup` does not survive into the command.
- **Isaac stages carry NVIDIA's EULA gate**, declared empty unless the operator set `OMNI_KIT_ACCEPT_EULA` / `ISAACSIM_ACCEPT_EULA`, so the task fails closed with an actionable message rather than an unexplained exit 78.

## Traps That Cost Live Jobs

- A vendor image can ship a **stale npa source tree on `PYTHONPATH`**, which shadows every install. The render shims `npa` to the recorded interpreter and puts the staged source first.
- `assert_no_unresolved_placeholders` rejects `${var}` in rendered setup — compose paths with `printf`, not braced expansion.
- The e2e source overlay at `NPA_SRC_S3_URI` carries **no provenance**: a stale tree is indistinguishable from a fresh one until a renamed flag surfaces it. Re-sync after every merge.

## Reference Pattern

- Canonical spec: `npa/workflows/workbench/npa-workflows/bdd100k-pipeline.yaml` (13 states, two fan-out groups, two in-cluster services).
- Runner script pattern: `npa/scripts/run_bdd100k_pipeline.py`, a thin wrapper around `npa.orchestration.skypilot.submit_workflow`.
- Isaac Lab runners follow the same shape through `npa/scripts/run_isaac_lab_rl.py`.

## Commit And Cleanup

Acquire `/tmp/npa-commit-lock/workflows-skypilot` before committing workflow files in parallel-run contexts.

Cleanup is best-effort and must not raise. `also_teardown_controller=False` is the safe default; only opt into controller teardown when no other run can be using it.
