---
name: workbench-reference-workflows
description: Use when working on NPA reference SkyPilot YAMLs, runner scripts, cookbooks, or customer-adaptable pipeline implementations.
---

# Workbench Reference Workflows

> The supported, customer-facing catalog is the `npa.workflow` spec set under
> `npa/workflows/workbench/npa-workflows/`. The raw SkyPilot task YAMLs below are
> internal runtime templates relocated to `npa/src/npa/workflows/skypilot/`; they
> back the `run_*.py` wrappers and SkyPilot-only capabilities, and must not be
> re-added to the shown `npa/workflows/workbench/` catalog (guardrail-enforced).

## When To Use

Use this skill for repository workflow YAMLs, runner scripts, cookbooks,
artifact contracts, and customer-adaptable pipeline implementations.

## Procedure

1. Start from the checked-in SkyPilot YAML under
   `npa/src/npa/workflows/skypilot/`.
2. Keep the runner thin. Python runners should materialize config, call the
   workflow submission helper, and report artifacts; they should not duplicate
   YAML orchestration logic.
3. Keep all input and output paths configurable and run-scoped through S3.
4. Validate YAML parsing and command help locally before live submission.

## Current Reference YAMLs

This list is machine-checked against the directory by
`npa/tests/guardrails/test_skypilot_catalog_retirement.py`, so it cannot drift as
templates retire.

- `bdd100k-pipeline.yaml`: BDD100K ingest, backfill, CLIP embedding,
  materialized views, training, and evaluation.
- `cosmos2-transfer.yaml`: Cosmos Transfer augmentation.
- `cosmos3-ea-fetch.yaml`: Cosmos3 source/checkpoint fetch.
- `cosmos3-text-to-image-inference.yaml`: H100 text-to-image smoke inference.
- `dataset-ingest-curate.yaml`: dataset-of-record ingest and curation.
- `isaac-franka-capture-reason.yaml`: Franka capture plus Cosmos reasoning.
- `isaac-lab-cosmos-sdg-burst-smoke.yaml`: Isaac Lab plus Cosmos SDG burst smoke.
- `sim2real-actions.yaml`: sim2real action-contract stage.
- `sim2real-envgen-split.yaml`: sim2real environment-generation split.
- `sim-to-real-pipeline.yaml`: full sim-to-real pipeline.
- `sim-to-real-trigger.yaml`: trigger wrapper for sim-to-real work.
- `sonic-train-standalone.yaml`: standalone SONIC training.
- `sonic-locomotion-finetuning.yaml`: retargeting, SONIC, and MJLab flow.
- `tokenfactory-rollout-judge.yaml`: Token Factory rollout judging.
- `tokenfactory-scene-to-rollout-judge.yaml`: scene-to-rollout judging.
- `tokenfactory-train-triage.yaml`: Token Factory training triage.

## Retired Templates

These raw templates were retired once their `npa.workflow` spec had a live run
(run ids in `EVIDENCE.md`). Use the spec under
`npa/workflows/workbench/npa-workflows/`:

- `isaac-lab-rl-sweep.yaml` — parallel GPU sweep (`--runtime`).
- `cosmos3-reason.yaml` — Cosmos3 reason-stage manifest.
- `sonic-export.yaml`, `sonic-eval.yaml`, `sonic-export-eval.yaml` — SONIC
  export/eval. The tools now accept `s3://` inputs and outputs directly, which is
  what the templates' inline download/upload bash used to do.
- `token-factory-caption.yaml`, `token-factory-generate.yaml`,
  `token-factory-cosmos-reason.yaml` — hosted Token Factory stages.
- `mjlab-eval.yaml` — MJLab locomotion evaluation.
- `retargeting.yaml` — motion retargeting. The harness synthesizes a SOMA-CSV clip
  (`npa.workflows.motion_fixture`) when no real motion set is staged.
- `vlm-eval.yaml`, `vlm-eval-benchmark.yaml` — self-hosted VLM scoring and the labeled
  sweep. The renderer now starts and health-checks the vLLM server the spec asks for, so
  no prebuilt serving image is needed.
- `scenario-gen-adversarial.yaml` — adversarial scenario mining. Its twin
  `npa-workflows/scenario-gen-smoke.yaml` runs the same two CLI commands; the template's GPU
  image advertised an RL adversary the CLI cannot select.
- `sim-to-real-loop.yaml` — the rollout-SET loop. Retired via a new tool capability
  (`npa workbench vlm-eval loop`), because nothing else produced
  `task_success_report.json`; the spec is `npa-workflows/vlm-eval-loop.yaml`.
- `isaac-lab-rl-train-rtxpro.yaml`, `isaac-lab-rl-train-rtxpro-smoke.yaml`,
  `isaac-lab-rl-train.yaml`, `byof-datagen-rtxpro-smoke.yaml`,
  `byof-container-smoke-rtxpro.yaml` — **relocated**, not retired: they are BYOF
  *resource profiles* (a pod shape), not workflows, and now live beside their
  runner at `npa/src/npa/workflows/byof/profiles/`.

The remaining templates are pinned in
`npa/tests/guardrails/test_skypilot_catalog_retirement.py`; do not add new ones.

## Three-Tier Contract

- CLI: use `npa workbench workflow ...` and tool-specific workflow commands
  such as `npa workbench mjlab workflow` or `npa workbench retargeting workflow`.
- SDK: route through shared workflow submission helpers rather than shelling out
  from business logic.
- YAML: SkyPilot YAML is the executable source of truth for workflow order,
  resources, environment, and artifact paths.

## Gotchas

- SkyPilot `envs` does not support self-referencing interpolation. Use explicit
  values and comments for alternatives.
- `sky jobs launch` has no dry-run flag in the pinned path. Use local YAML
  parsing, command help, and mock-endpoint tests before live submission.
- Keep orchestration in YAML for SONIC locomotion; do not add a Python runner
  that re-implements the DAG.

## Verify

```bash
npa/.venv/bin/python -m pytest npa/tests/guardrails/test_skills_index.py -q
```

The smoke test parses the listed workflow YAMLs and invokes workflow CLI help.
