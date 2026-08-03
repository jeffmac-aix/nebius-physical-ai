# Nebius Physical AI Workbench — Pipeline Guide

> Living document. Updated as new pipeline patterns are introduced.
> Last updated: 2026-08-02

## Overview

A Workbench pipeline is an **`npa.workflow/v0.0.1` spec**: a single YAML document that
declares *what* each stage does and *what it needs*, and lets the engine decide how to run
it. The engine renders SkyPilot tasks from the spec and schedules them on the Nebius MK8s
cluster.

This guide used to describe a multi-document SkyPilot file whose tasks called workbench
HTTP endpoints with `curl` and `jq`. That surface is retired — see `CHANGELOG.md` and
`EVIDENCE.md` — and the reference file named below is a spec, not a task list. Raw SkyPilot
YAML is still *accepted* by `npa workbench workflow submit` so a customer can bring their
own task, but it is no longer how pipelines in this repo are written.

The reference pipeline is `npa/workflows/workbench/npa-workflows/bdd100k-pipeline.yaml`.

## Pipeline Structure

A spec has five parts:

```yaml
apiVersion: npa.workflow/v0.0.1
kind: Workflow
metadata:
  name: bdd100k-pipeline
config:            # every value a stage needs, in one place
  bucket: example-bucket
resources:         # named pod shapes, referenced by stages
  cpu:
    cloud: kubernetes
    cpus: 4
    memory: 16Gi
  gpu-train:
    cloud: kubernetes
    accelerators: H100:1
    cpus: 16
    memory: 64Gi
initial: ingest    # where the run starts
states:
  ingest:
    toolRef: workbench.lancedb.import_bdd100k
    resources: cpu
    next: backfill-cpu
```

The important difference from the retired template: a state names a **`toolRef`**, not a
shell command. The toolRef's `argv_template` lives in
`npa/src/npa/orchestration/npa_workflow/catalog.py` and is what actually runs, with
`{{config.*}}` tokens resolved from the spec's `config:` block. A guardrail
(`test_tool_catalog_argv.py`) asserts every argv names real CLI options and passes values
those options can accept, which is not a claim a hand-written `run:` block could make.

### Fan-out

Stages that can run at once are declared, not implied:

```yaml
train-models:
  description: Train one detector per failure mode.
  needs: [curate-views]
  sequence: [train-rider, train-nighttime, train-distant]
  next: evaluate-models
```

The engine launches the group as one SkyPilot JobGroup and treats the next state as a
barrier. This is why the pipeline is no longer serial: the mixed dependency graph that
SkyPilot 0.12.2 could not express in a single template is expressed in the spec instead,
and the engine renders it.

### Multi-node stages

A resource profile may ask for more than one node:

```yaml
resources:
  gang:
    cloud: kubernetes
    accelerators: H100:1
    num_nodes: 2
```

The engine emits `num_nodes` at the SkyPilot **task** level and SkyPilot gang-schedules
that many identical pods, exporting `SKYPILOT_NODE_RANK` and `SKYPILOT_NODE_IPS` into each.

## Resources

A stage names a **profile**, and the profile is defined once. The BDD100K spec declares
four, so the same shape is not repeated across thirteen states:

| Profile | Used by | Shape |
|---|---|---|
| `cpu` | `ingest`, `backfill-cpu`, `curate-views`, `review` | `cloud: kubernetes`, CPU only |
| `gpu-embed` | `backfill-clip` (CLIP image embeddings) | `accelerators: H100:1` |
| `gpu-train` | `train-rider`, `train-nighttime`, `train-distant` | `accelerators: H100:1`, 16 CPU, 64Gi |
| `gpu-eval` | `eval-rider`, `eval-nighttime`, `eval-distant` | `accelerators: H100:1` |

Two things this buys that per-task `resources:` blocks did not: changing the training shape
is one edit rather than three, and a stage's requirements are separable from what it does,
which is what makes `num_nodes` and image routing decidable by the engine.

The detection-training deploy path uses the H100 Kubernetes node selector value
`gpu-h100-sxm`. A profile requests GPUs with SkyPilot's `accelerators:` field.

## Configuration (`config`)

The `config:` block is the contract between the spec and the toolRefs it invokes. A stage
does not build a request body; it names a toolRef whose `argv_template` references
`{{config.*}}` tokens, and the interpreter resolves them at plan time. A token with no
matching config key is a **validation error**, not a runtime surprise — `npa workbench
workflow validate-spec` catches it before anything is submitted.

Common run-scoped values in the BDD100K spec:

- `bucket`, `prefix`: artifact location; `{{run.id}}` is substituted per run.
- `lance_uri`: per-run LanceDB URI.
- `lancedb_endpoint`: LanceDB service URL.
- `detection_endpoint`: detection-training service URL.
- `detection_label_map`: the category-to-integer map (see below).

Where the retired template hand-built a JSON body with `jq`, the equivalent now lives in
the catalog:

```python
"workbench.detection_training.train_rider": ToolEntry(
    argv_template=[
        "npa", "workbench", "detection-training", "train",
        "--view", "{{config.rider_view}}",
        "--lance-uri", "{{config.lance_uri}}",
        "--label-map", "{{config.detection_label_map}}",
        ...
    ],
),
```

That is testable in a way a `jq` invocation inside a `run:` block was not: the guardrail
parses each argv against the real CLI, so a renamed flag fails a unit test instead of a
live GPU job.

### Label Map Injection (BDD100K Pattern)

Workbench tools that operate on labeled data accept a `label_map` that translates string
category names to integer IDs. The rationale is unchanged and still the right one:

- Training tools are dataset-agnostic; they do not hardcode any category schema.
- Dataset-specific configuration belongs in the pipeline, not in the tool.
- Any dataset can be supported by supplying its own map.

What changed is where it lives. It is a config key, and the toolRef passes it:

```yaml
config:
  # Synthetic BDD100K data — category names match the synthetic data generator.
  detection_label_map: '{"person":0,"rider":1,"car":2,"truck":3,"bus":4,"train":5,"motor":6,"bike":7,"traffic light":8,"traffic sign":9}'
```

One key, read by all three training stages **and** all three eval stages, instead of the
same JSON repeated in six `envs:` blocks.

That symmetry is not cosmetic. Under the template, `label_map` reached training but eval
had the field with **no flag to fill it** — so three trainings would succeed and eval would
die on `int('train')`, because BDD100K stores string categories and one of them is literally
`train`, the vehicle. The flag now exists on `eval` and reaches it through CLI, SDK and all
three eval toolRefs (`EVIDENCE.md` §R46).

Use the synthetic map for runs seeded with `NPA_E2E_BDD100K_SYNTHETIC_ROWS` or the runner's
`--synthetic` flag; use the real BDD100K map for runs that import label files. The IDs are
stable between the two, but three names differ:

| ID | Synthetic category | Real BDD100K category |
|---:|---|---|
| 0 | `person` | `pedestrian` |
| 6 | `motor` | `motorcycle` |
| 7 | `bike` | `bicycle` |

To switch a run to real labels, change the one `detection_label_map` value.

`num_classes` is auto-inferred as `len(label_map) + 1`, the extra class being background.
Do not pass it manually unless overriding the inferred value.

Extending to other datasets: supply that dataset's mapping. The detection-training tool
accepts any `label_map`; it is not BDD100K-specific.

## Service Endpoints

The pipeline uses cluster-internal Kubernetes DNS:

```text
http://<service-name>.workbench.svc.cluster.local:<port>
```

Services used by the BDD100K reference pipeline:

| Tool | Service | Port | Endpoints Used |
|---|---|---:|---|
| LanceDB | `npa-lancedb` | `8686` | `GET /health`, `POST /import-bdd100k`, `POST /backfill`, `POST /create-mv` |
| Detection training | `npa-detection-training` | `8790` | `GET /health`, `POST /train`, `GET /status`, `GET /runs`, `POST /eval` |

The `/train` request schema accepts:

- `view`: Lance materialized view name.
- `lance_uri`: LanceDB URI.
- `output_uri`: checkpoint and metrics output URI.
- `label_map`: optional string-label-to-integer mapping.
- `num_classes`: optional manual class count override.
- `epochs`, `batch_size`, `learning_rate`: training hyperparameters.
- `validation_filter_sql`: optional validation filter, currently not used by the
  committed BDD100K pipeline.

## S3 Artifact Paths

The runner renders per-run paths before submission. The convention is:

```text
s3://<bucket>/bdd100k-pipeline/<run-id>/
```

With `NPA_S3_BUCKET=your-bucket-name` and a run ID of `example-run`:

```text
s3://${NPA_S3_BUCKET}/bdd100k-pipeline/example-run/
```

Derived paths:

- LanceDB: `${PIPELINE_ROOT_URI}/lancedb/`
- Training: `${PIPELINE_ROOT_URI}/training/${VIEW_SLUG}`
- Evaluation: `${PIPELINE_ROOT_URI}/eval/${VIEW_SLUG}`

`npa/scripts/run_bdd100k_pipeline.py` renders these values into each task's
`envs` block. Cleanup is controlled by the runner's `--cleanup` flag, which calls
the SkyPilot cleanup path for the run after terminal workflow status.

## Durable Workflow State

For long-running SkyPilot workflows, prefer the generic durable monitor instead
of adding ad hoc log upload code to each YAML:

```bash
npa workbench workflow submit <workflow.yaml> \
  --durable-s3 \
  --workflow-s3-uri "s3://<bucket>/workflows/<run-id>/" \
  --infra "k8s/<context>"
```

The submit command injects an S3 MOUNT-mode `file_mount` into every task and
wraps each `run` block with redacted stdout/stderr teeing plus
`manifest.json`, `logs/<stage>/status.json`, and `artifacts/<stage>/` state.
The user-facing monitor is:

```bash
npa workbench workflow status "s3://<bucket>/workflows/<run-id>/"
npa workbench workflow logs "s3://<bucket>/workflows/<run-id>/" --stage <stage>
npa workbench workflow artifacts "s3://<bucket>/workflows/<run-id>/"
```

Do not use SkyPilot `logs.store` or CloudWatch for the Workbench durable monitor path; the
cluster pod writes the S3 state through the mounted run prefix.

## Standard Pipeline Stages (BDD100K Reference)

Thirteen states. The names below are the spec's **state** names, which is what
`npa workbench workflow status` reports and what the artifact prefixes use:

| # | State | toolRef | Profile |
|---:|---|---|---|
| 1 | `ingest` | `workbench.lancedb.import_bdd100k` | `cpu` |
| 2 | `backfill-cpu` | `workbench.lancedb.backfill_cpu_bundle` | `cpu` |
| 3 | `backfill-clip` | `workbench.lancedb.backfill_clip` | `gpu-embed` |
| 4 | `curate-views` | `workbench.lancedb.create_failure_views` | `cpu` |
| 5 | `train-models` | — (fan-out over the three below) | — |
| | `train-rider`, `train-nighttime`, `train-distant` | `workbench.detection_training.train_*` | `gpu-train` |
| 6 | `evaluate-models` | — (fan-out over the three below) | — |
| | `eval-rider`, `eval-nighttime`, `eval-distant` | `workbench.detection_training.eval_*` | `gpu-eval` |
| 7 | `review` | `workbench.fiftyone.launch_app` | `cpu` |

The two unnumbered rows are the fan-out groups: `train-models` and `evaluate-models` own no
work themselves, they declare which stages run together and act as the barrier before the
next one.

All eleven working stages have run green end to end on real infrastructure against
in-cluster LanceDB and detection-training services — see `EVIDENCE.md` §R46.

Related docs:

- `docs/workbench/getting-started.md`
- `docs/demos/bdd100k-lancedb-demo.md`
- `docs/workbench/cookbooks/bdd100k-pipeline.md`
- `npa/workflows/workbench/npa-workflows/bdd100k-pipeline.yaml`

## Isaac Lab RL Training

Isaac Lab RL jobs are batch training workloads, not persistent service calls.
Use the committed SkyPilot consumers:

- `npa/src/npa/workflows/byof/profiles/isaac-lab-rl-train.yaml` for one RSL-RL training
  job, submitted by `npa/scripts/run_isaac_lab_rl.py` (which renders per-run values).
- `npa/workflows/workbench/npa-workflows/isaac-lab-rl-sweep.yaml` for a parallel
  sweep. This is an **`npa.workflow` spec**, not a SkyPilot task YAML: it replaced the
  raw `execution: parallel` template, which is now retired. Submit it through the
  engine with `--runtime` so the four variants launch as a SkyPilot JobGroup and the
  ranking stage acts as a barrier.

Single run:

```bash
export NPA_S3_BUCKET=your-bucket-name
python npa/scripts/run_isaac_lab_rl.py \
  --yaml npa/src/npa/workflows/byof/profiles/isaac-lab-rl-train.yaml \
  --task Isaac-Cartpole-v0 \
  --iterations 10 \
  --run-id isaac-cartpole-smoke
```

The training command uses the Isaac Lab RSL-RL entry point:

```bash
/isaac-sim/python.sh scripts/reinforcement_learning/rsl_rl/train.py \
  --task "${ISAAC_LAB_TASK}" \
  --num_envs "${ISAAC_LAB_NUM_ENVS}" \
  --max_iterations "${ISAAC_LAB_ITERATIONS}" \
  --headless \
  --experiment_name "${ISAAC_LAB_EXPERIMENT_NAME}" \
  --run_name "${ISAAC_LAB_RUN_NAME}" \
  agent.save_interval=1
```

Parameter sweep (through the engine, **not** the single-job runner):

```bash
npa workbench workflow submit \
  npa/workflows/workbench/npa-workflows/isaac-lab-rl-sweep.yaml \
  --run-id isaac-cartpole-sweep --runtime \
  --var max_concurrency=2 \
  --secret-env AWS_ACCESS_KEY_ID --secret-env AWS_SECRET_ACCESS_KEY

# offline preview of the wave shape (which batches, which barrier):
npa workbench workflow plan-spec \
  npa/workflows/workbench/npa-workflows/isaac-lab-rl-sweep.yaml --run-id demo --waves
```

The spec's `parallel:` group renders as a SkyPilot JobGroup (`execution: parallel`),
which is the 0.12.2 pattern for independent parallel tasks; `maxConcurrency` splits a
group larger than the bound into batches submitted in order. Each variant writes
under:

```text
s3://<bucket>/isaac-lab-rl/<run-id>/<variant>/
```

Isaac Lab requires RT-core GPUs for simulation. The YAMLs request:

```yaml
resources:
  cloud: kubernetes
  accelerators: L40S:1
```

Use L40S first. RTX Pro 6000 is the fallback when exposed in the Kubernetes GPU
catalog. Do not run Isaac Lab on H100 or H200 for these jobs; those accelerators
do not provide the RT cores required by Isaac Sim rendering/simulation paths.

Custom Isaac Lab forks can be layered by overriding the image in the YAML:

```yaml
resources:
  image_id: "docker:cr.eu-north1.nebius.cloud/<registry>/flexion-isaac-lab:<tag>"
```

The replacement image must keep the Isaac Lab source tree at
`/workspace/isaaclab` or provide the same
`scripts/reinforcement_learning/rsl_rl/train.py` entry point. The runner also
accepts `--image cr.../custom-isaac-lab:<tag>` to rewrite `image_id` in the
rendered workflow.

## Adding a New Pipeline

Write an `npa.workflow/v0.0.1` spec. Do **not** start from a multi-document SkyPilot file —
that surface is retired, and a guardrail
(`npa/tests/guardrails/test_skypilot_catalog_retirement.py`) will fail if a new raw task
appears in the shipped catalog. It has already caught two.

1. **Start from a spec that resembles yours.** `bdd100k-pipeline.yaml` for a long
   service-backed pipeline, `sonic-export-eval.yaml` for a short artifact-chained one,
   `isaac-lab-rl-sweep.yaml` for a fan-out.
2. **Reuse a `toolRef` if one exists.** The catalog is the list of things a stage can do:
   `npa/src/npa/orchestration/npa_workflow/catalog.py`. Adding a spec should rarely require
   adding a tool.
3. **Put every value in `config:`.** Never a repo-relative path — a stage runs in a pod that
   has no checkout, and `test_spec_paths_are_not_repo_relative.py` enforces it.
4. **Declare `outputs:` where the tool actually writes.** Ask the tool's own
   `*_result_uri_for()` helper, do not guess: a stage that succeeds while its declared
   artifact does not exist is the most expensive failure mode in this repo, and
   `test_spec_declared_outputs.py` exists because it happened eleven times.
5. **Validate offline, then plan, then submit.**

   ```bash
   npa workbench workflow validate-spec <spec.yaml>
   npa workbench workflow plan-spec <spec.yaml> --run-id preview   # wave shape, no cluster
   npa workbench workflow submit <spec.yaml> --run-id <run-id>
   ```

6. **Add a live-matrix case** in `npa/src/npa/orchestration/npa_workflow/submit_matrix.py`.
   If it cannot run live, say why in `skip_reason` — an honest reason is worth more than an
   untested spec, and the field is read by reviewers.

If a stage needs a capability no tool has, add the capability to the tool and reach it
through a `toolRef`. Do not reach for an inline shell block: everything the guardrails check
— that flags exist, that values fit them, that declared artifacts are real — is invisible
inside `run:` bash, which is exactly how the retired templates accumulated defects that only
live runs could find.

New workbench tool endpoints should be documented here only after the endpoint is present in
committed source.

## Changelog

| Date | Change | Run |
|---|---|---|
| 2026-08-02 | Rewritten for the `npa.workflow` spec surface: the raw SkyPilot task catalog is retired, so the guide no longer teaches multi-document task YAML. | retire-skypilot-catalog |
| 2026-05-20 | Added Isaac Lab RSL-RL single-job and parallel sweep SkyPilot YAML patterns. | W9-isaac-lab-e2e-fix |
| 2026-05-16 | Initial guide. Label map injection pattern (BDD100K). | W9-label-schema-fix |
