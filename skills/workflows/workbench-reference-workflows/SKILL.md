---
name: workbench-reference-workflows
description: Use when working on NPA reference workflows (npa.workflow specs), runner scripts, cookbooks, or customer-adaptable pipeline implementations, or when checking what a retired SkyPilot template became.
---

# Workbench Reference Workflows

> The catalog is the `npa.workflow` spec set under
> `npa/workflows/workbench/npa-workflows/`. It is the **only** authoring surface: the raw
> SkyPilot task catalog is retired, and the two templates still listed below arrived from
> other PRs while that retirement was in flight. Do not add more — a guardrail
> (`test_skypilot_catalog_retirement.py`) fails on new raw templates, and it has already
> caught two.
>
> SkyPilot remains the execution engine, and `npa workbench workflow submit` still accepts a
> customer's own SkyPilot YAML. What went away is the shipped catalog, not the capability.

## When To Use

Use this skill for repository workflow YAMLs, runner scripts, cookbooks,
artifact contracts, and customer-adaptable pipeline implementations.

## Procedure

1. Start from the closest `npa.workflow` spec under
   `npa/workflows/workbench/npa-workflows/`, and reuse a `toolRef` from
   `npa/src/npa/orchestration/npa_workflow/catalog.py` rather than adding a tool.
2. Keep the runner thin. A `run_*.py` wrapper materializes config, calls the submission
   helper, and reports artifacts; it does not re-implement the stage graph, which is the
   spec's job.
3. Keep all input and output paths configurable and run-scoped through S3, and never
   repo-relative — a stage runs in a pod with no checkout
   (`test_spec_paths_are_not_repo_relative.py`).
4. Declare `outputs:` where the tool actually writes. Ask its `*_result_uri_for()` helper;
   a stage that succeeds while its declared artifact does not exist is this repo's most
   expensive failure mode, and it happened eleven times.
5. Validate offline before submitting: `npa workbench workflow validate-spec`, then
   `plan-spec --run-id preview` for the wave shape without touching a cluster.

## Current Reference YAMLs

This list is machine-checked against the directory by
`npa/tests/guardrails/test_skypilot_catalog_retirement.py`, so it cannot drift as
templates retire. Both entries arrived from other PRs during the retirement; neither is a
survivor of the original catalog.

- `cosmos3-generate.yaml`: single-task Cosmos 3 omni-model generation in the
  `npa-cosmos3` image. Its npa.workflow twin of the same name is the declarative form.
- `nurec-reconstruct.yaml`: single-pod NuRec/NRE neural reconstruction. Its
  npa.workflow twin of the same name runs each state in its own pod and hands
  artifacts over through S3.

## Retired Templates

Thirty templates, each retired only after its `npa.workflow` spec reached a terminal
`SUCCEEDED` on real infrastructure (run ids in `EVIDENCE.md`). This list is the lookup table
for "what happened to X" — use the spec of the same name under
`npa/workflows/workbench/npa-workflows/` unless an entry says otherwise:

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
- `cosmos3-text-to-image-inference.yaml` — retired to
  `npa-workflows/cosmos3-text-to-image.yaml`. The procedure it carried as bash inside an `envs:`
  block is now `npa workbench cosmos3 text-to-image`.
- `bdd100k-pipeline.yaml` — retired to `npa-workflows/bdd100k-pipeline.yaml`. A live run needs
  both in-cluster services (`lancedb` and `detection-training`) deployed first.
- `dataset-ingest-curate.yaml` — retired to `npa-workflows/dataset-ingest-curate.yaml`, whose
  `register` stage reads back what `ingest` wrote to the in-cluster LanceDB service
  (`npa workbench lancedb deploy --runtime kubernetes --namespace workbench`).
- `sim-to-real-pipeline.yaml` / `sim-to-real-trigger.yaml` — retired. The pipeline ran the
  deprecated `npa.workflows.sim_to_real real-loop`; the maintained path is the staged engine's
  spec, `npa-workflows/sim2real-vlm-rl.yaml`, which is also what the watcher now submits.
- `cosmos2-transfer.yaml` — retired to `npa-workflows/cosmos2-transfer.yaml`, which runs the
  REAL Cosmos-Transfer2.5 model (`--execute`) instead of printing a `contract_ready` payload.
- `isaac-franka-capture-reason.yaml` — retired to
  `npa-workflows/isaac-franka-capture-reason.yaml`. The capture code moved into the package
  (`npa.workflows.isaac_capture`), so the stage no longer needs a repo mounted into the pod.
- `sim2real-actions.yaml` — retired into `npa-workflows/sim2real-envgen-shards.yaml` as its
  fourth stage, which conditions the train slice the `split` stage just wrote.
- `tokenfactory-scene-to-rollout-judge.yaml` — hosted reasoner, GPU rollout, hosted judge. Its
  twin keeps the chain: `vlm-eval run --task-from` reads the reasoner's artifact, so the judge
  scores the rollout against the plan rather than a literal string.
- `tokenfactory-rollout-judge.yaml` — GPU rollout then a hosted VLM judge. Its twin is
  `npa-workflows/tokenfactory-rollout-judge-combo.yaml`; note the older same-named spec is a
  *different* workflow (a Cosmos reasoner feeding a judge over externally-seeded rollouts).
- `tokenfactory-train-triage.yaml` — GPU LeRobot training then a hosted triage report. Its twin
  `npa-workflows/tokenfactory-train-triage.yaml` trains in the stage's own pod (the renderer
  switches to the vendor image's interpreter) and triages with
  `npa.workflows.token_factory_triage`. Needs a SkyPilot-hostable LeRobot image; 0.5.1 ships a
  torch/torchcodec ABI mismatch, 0.6.0 does not.
- `cosmos3-ea-fetch.yaml` — Cosmos source/checkpoint fetch. Its twin
  `npa-workflows/cosmos-fetch.yaml` is the two CLI commands the template wrapped in ~60 lines
  of setup bash; the renderer installs `huggingface_hub[cli]`, which was the only load-bearing
  line of that preamble.
- `sim2real-envgen-split.yaml` — raw env generation + 80/20 split. Its twin
  `npa-workflows/sim2real-envgen-shards.yaml` declares the shard fan-out as a `parallel:`
  group instead of relying on a Kubernetes Job completion index, and runs on CPU.
- `scenario-gen-adversarial.yaml` — adversarial scenario mining. Its twin
  `npa-workflows/scenario-gen-smoke.yaml` runs the same two CLI commands; the template's GPU
  image advertised an RL adversary the CLI cannot select.
- `sim-to-real-loop.yaml` — the rollout-SET loop. Retired via a new tool capability
  (`npa workbench vlm-eval loop`), because nothing else produced
  `task_success_report.json`; the spec is `npa-workflows/vlm-eval-loop.yaml`.
- `sonic-train-standalone.yaml`, `sonic-locomotion-finetuning.yaml` — SONIC training. Their
  twins train in the stage's own pod (`sonic train --runtime local`) instead of provisioning
  a Nebius Job from inside one. The vendor trainer runs when the image provides it and the
  operator has accepted NVIDIA's terms (`sonic_accept_nvidia_eula`, empty by default);
  otherwise a reference locomotion trainer fits a real policy on the job's GPU, which is what
  made the twins verifiable without NVIDIA's asset pipeline.
- `isaac-lab-cosmos-sdg-burst-smoke.yaml` — **relocated**, not retired: a single-task input to
  `npa burst submit-yaml`, now at `npa/src/npa/burst/examples/`. Burst is scoped to one
  executable task, so there is no plan or stage graph for a spec to describe.
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
- Workflow: the `npa.workflow` **spec** is the executable source of truth for stage order,
  resources, configuration, and artifact paths. This tier used to be a SkyPilot `envs:`
  block; it moved with the retirement, and `CapabilityContract` now records `spec_path` +
  `tool_ref`, with a `spec_gap` listing any CLI parameter the toolRef argv cannot reach.

## Gotchas

- SkyPilot `envs` does not support self-referencing interpolation. Use explicit
  values and comments for alternatives.
- `sky jobs launch` has no dry-run flag in the pinned path. Use local YAML
  parsing, command help, and mock-endpoint tests before live submission.
- Keep orchestration in the spec for SONIC locomotion; do not add a Python runner that
  re-implements the DAG.
- A toolRef argv is always flag-plus-value, so a capability a spec must carry cannot be a
  bare boolean flag — `sonic train --accept-nvidia-eula` takes a value for exactly this
  reason.

## Verify

```bash
npa/.venv/bin/python -m pytest npa/tests/guardrails/test_skills_index.py -q
```

The smoke test parses the listed workflow YAMLs and invokes workflow CLI help.
