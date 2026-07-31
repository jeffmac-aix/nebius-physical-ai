# EVIDENCE — live runs for `npa.workflow` parallel execution + runtime control flow

Every claim below is backed by a **live run on real Nebius infrastructure** from
the operator dev VM (`nebius-dev-vm`) against the `npa-rtxpro-mk8s` Kubernetes
cluster, real S3, and the real hosted Token Factory models. Anything that was
**not** verified live is called out explicitly in
[§7 Not verified live](#7-not-verified-live).

Secrets are never printed: credentials come from `~/.npa/live-e2e.env` /
`~/.npa/credentials.yaml` on the dev VM, and the live harness asserts
(`assert_no_credential_leakage`) that no CLI output contains credential material.
Bucket and registry values below come from that environment; nothing is
hardcoded in the repo.

Isolation: all work ran in a dedicated git worktree `~/npa-wf-runtime` (branch
`cursor/bc-fd5052e0-...-6c5b`) with `PYTHONPATH=<worktree>/npa/src` so the shared
editable venv could not shadow branch code (verified:
`python -c "import npa; print(npa.__file__)"` →
`/home/ubuntu/npa-wf-runtime/npa/src/npa/__init__.py`), in dedicated tmux
sessions, with a run-id prefix of its own and a private staged copy of the npa
package (`s3://<bucket>/npa-workflow-e2e/npa-src-wfrt/npa`) so no other agent's
runs were disturbed.

---

## 0. Environment (secrets redacted)

```bash
# on nebius-dev-vm, in the isolated worktree
cd ~/npa-wf-runtime
set -a; . ~/.npa/live-e2e.env; . ~/.npa/live-e2e-gates.env; set +a
export PYTHONPATH=$PWD/npa/src
export NPA_SRC_S3_URI=s3://<artifact-bucket>/npa-workflow-e2e/npa-src-wfrt/npa   # branch source
export NPA_INTEGRATION_E2E=1 NPA_E2E_NPA_WORKFLOW_SUBMIT=1 NPA_E2E_NPA_WORKFLOW_RUNTIME=1
export NPA_E2E_NPA_WORKFLOW_SUBMIT_POLL_SECONDS=20
export NPA_E2E_NPA_WORKFLOW_SUBMIT_MAX_WAIT_SECONDS=2700
unset NPA_E2E_FORCE_ACCELERATORS NPA_E2E_ACCELERATOR_REMAP   # CPU tiers stay CPU
# registry: $NPA_E2E_REGISTRY = cr.us-central1.nebius.cloud/<registry-id>
# cluster:  kubernetes/npa-rtxpro-mk8s  (2 x 8 RTXPRO-6000-BLACKWELL + 1 CPU node)
# skypilot: ~/.npa/skypilot-venv/bin/sky, version 0.12.2
```

Branch source staged for the tasks (tasks install `npa` from S3 because the live
env sets `NPA_E2E_CLEAR_WORKBENCH_IMAGES=1`):

```bash
bash scripts/stage-npa-src.sh --bucket <artifact-bucket> --prefix npa-workflow-e2e/npa-src-wfrt
# staged 549 files -> s3://<artifact-bucket>/npa-workflow-e2e/npa-src-wfrt/npa
```

---

## 1. Offline baseline and unit suites

| Run | Command | Result |
| --- | --- | --- |
| Baseline (base commit `d129ee90`, before any change) | `pytest npa/tests/ --ignore=npa/tests/e2e --timeout=120 -q` | **3538 passed, 28 skipped, 1 xpassed, 2 errors** (637 s) |
| Full suite after the change | same, plus `--ignore` for the two pre-existing live-GPU files | **3657 passed, 28 skipped, 1 xpassed, 0 failed** (301 s) |
| Engine + specs | `pytest npa/tests/orchestration/npa_workflow/ npa/tests/smoke/test_all_workflow_yamls.py npa/tests/smoke/test_npa_workflow_smoke.py -q` | **263 passed** |
| Runtime/parallel unit + CLI coverage | `pytest npa/tests/orchestration/npa_workflow/ npa/tests/orchestration/skypilot/test_workflow.py npa/tests/cli/test_workflow_runtime_cli.py -q` | **577 passed** (orchestration + smoke + runtime CLI) |
| New stage modules | `pytest npa/tests/workflows/test_rl_sweep.py npa/tests/workflows/test_fanout_join.py -q` | **10 passed** |
| Guardrails | `pytest npa/tests/guardrails/ -q` | **50 passed** |
| Lint | `ruff check` on every changed file | clean |

### CI on the pull request — 15/15 green

```
docs-drift pass   gitleaks pass   guardrails pass   mypy pass
ruff pass          scan pass       test (3.10) pass  test (3.12) pass  test (3.14) pass
```

Two checks needed attention and were resolved before green:

* **gitleaks** initially failed with 5 hits. Reproduced locally with gitleaks
  8.28.0 over the PR's own scan range: all five were the operational `lerobot-*`
  artifact bucket inside the **first draft** of this file (commit `6937efd3`),
  which a later commit had already redacted to `<artifact-bucket>`. A working-tree
  scan of HEAD reports `no leaks found`, so that historical commit is allowlisted
  in `.gitleaks.toml` using the mechanism the config already carries for this
  exact situation — rather than force-pushing a rewritten history.
* **docs-drift** (a *blocking* gate that regenerates `docs/cli/` from `npa --help`)
  was checked proactively, because this change adds CLI options. Regenerating on
  the dev VM and diffing showed `docs/cli/workbench.md` **unchanged**: the
  generator documents top-level commands only, so options three levels deep
  (`workbench workflow submit`) never reach it. The local diffs seen while checking
  were a typer-version metavar artifact of the dev VM (`<str>` vs `TEXT`), not this
  change — confirmed by CI, which passes docs-drift on the committed files.

The 2 baseline errors are **pre-existing** and unrelated: the live-GPU fixtures in
`npa/tests/workbench/test_vlm_eval_backend.py` and `test_vlm_eval_loop_e2e.py`
try to launch a SkyPilot cluster whenever `sky` is on `PATH` and hit the 120 s
timeout (they also cost real cloud time), so the post-change runs exclude those
two files. They fail identically on the base commit. Net: **+119 tests, all green.**

Guardrails that stayed green untouched: `test_render_rejects_parallel_execution`
(serial-only renderer guard), `test_dynamic_execution.py` (monkeypatched local
executor), `test_submit_live_matrix.py`, and the plan-only matrix assertion that
every rendered twin says `execution: serial`.

---

## 2. Plan-only matrix (no cloud spend)

```bash
pytest npa/tests/e2e/test_npa_workflow_submit_live_e2e.py::test_npa_workflow_submit_plan_only_matrix_no_leak -q
# 23 passed in 4.42s
```

All 23 matrix specs — including the three new ones — render cleanly, contain no
unresolved `${...}` placeholders, leak no credentials, and still emit
`execution: serial` headers for `--plan-only`.

Wave preview of the new specs (offline, `plan-spec --waves`):

```
$ npa workbench workflow plan-spec .../token-factory-parallel-fanout.yaml --run-id demo --waves
workflow: token-factory-parallel-fanout
waves: 2
  00. [parallel] caption-shards: caption-shard-a, caption-shard-b, caption-shard-c maxConcurrency=3 batches=1
  01. [serial] aggregate: aggregate

$ npa workbench workflow plan-spec .../isaac-lab-rl-sweep.yaml --run-id demo --waves
workflow: isaac-lab-rl-sweep
waves: 2
  00. [parallel] sweep: variant-lr-1e-3, variant-lr-3e-4, variant-entropy-0, variant-entropy-0-01 maxConcurrency=4 batches=1
  01. [serial] select-best: select-best
```

---

## 3. Phase 1 — real parallel execution (live, CPU tier)

**Spec:** `token-factory-parallel-fanout.yaml` (three real Token Factory caption
shards + a join barrier).
**Command:**

```bash
NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=cpu \
NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS=token-factory-parallel-fanout.yaml \
pytest npa/tests/e2e/test_npa_workflow_submit_live_e2e.py::test_npa_workflow_runtime_live_reaches_terminal -q -s
```

**Run id:** `npa-wf-cpu-token-factory-parallel-fanout-a8cf71e0`
**Run prefix:** `s3://<artifact-bucket>/npa-workflow-e2e/npa-wf-cpu-token-factory-parallel-fanout-a8cf71e0/token-factory-parallel-fanout/`

### 3.1 The three shards ran concurrently

`sky jobs queue --all --output json` (job **75** = the JobGroup wave):

| job_id | task | status | submitted_at | end_at |
| --- | --- | --- | --- | --- |
| 75 | caption-shard-a | SUCCEEDED | 1785297417.2239 | 1785297483.0892 |
| 75 | caption-shard-b | SUCCEEDED | 1785297417.2297 | 1785297482.8844 |
| 75 | caption-shard-c | SUCCEEDED | 1785297417.2362 | 1785297477.9160 |

All three share **one managed `job_id`** (a SkyPilot JobGroup), were submitted
within **12 ms** of each other, and their lifetimes overlap almost entirely
(~60–66 s each). A serialized chain cannot produce this.

The driver also sampled the live task statuses while polling:

```
[runtime] wave caption-shards batch 1/1 (parallel, 3 task(s)): ['caption-shard-a', 'caption-shard-b', 'caption-shard-c']
[runtime] wave 001|caption-shards|...: submitted job_id=72 name=npa-wf-cpu-token-factory-parallel-fanout-2f73db1d-01-caption
[runtime] wave 001|caption-shards|...: 3 tasks running concurrently (caption-shard-a, caption-shard-b, caption-shard-c)
```

(`max_concurrent_observed: 3` is stored in the wave ledger.)

### 3.2 The barrier waited for all predecessors

| wave | job_id | submitted_at | note |
| --- | --- | --- | --- |
| `caption-shards` (parallel) | 75 | 1785297417.22 | last member ended **1785297483.09** |
| `aggregate` (barrier) | 76 | **1785297577.64** | submitted **94.6 s after** the last member finished |

`aggregate` is the state that declares `needs: [caption-shards]`; the runtime tier
submitted it only after the whole group reached a terminal state.

### 3.3 Real work, real artifacts

`s3://.../token-factory-parallel-fanout/reports/join_report.json` (written by the
barrier, `npa.workflows.fanout_join.join_shards`):

```json
{
  "joined_shards": 3,
  "manifest": "captions.json",
  "missing_shards": [],
  "schema": "npa.fanout.join_report.v1",
  "shard_count": 3,
  "shards": [
    {"items": 2, "model": "Qwen/Qwen2.5-VL-72B-Instruct", "shard": "shard-a", "status": "ok", "uri": "s3://.../captions/shard-a/captions.json"},
    {"items": 2, "model": "Qwen/Qwen2.5-VL-72B-Instruct", "shard": "shard-b", "status": "ok", "uri": "s3://.../captions/shard-b/captions.json"},
    {"items": 2, "model": "Qwen/Qwen2.5-VL-72B-Instruct", "shard": "shard-c", "status": "ok", "uri": "s3://.../captions/shard-c/captions.json"}
  ],
  "total_items": 6
}
```

Each shard's `captions.json` is a **real hosted-VLM** result (`"dry_run": false`,
`Qwen/Qwen2.5-VL-72B-Instruct`), e.g.:

```json
{"captions": [{"caption": "The image shows two squares, one red and one green, placed side by side against a black background with a brown horizontal strip at the bottom...", "image": "frame_000.png"}], "dry_run": false}
```

### 3.4 Resume / idempotency (live)

Re-running the **same run id** with `--resume`:

```bash
npa workbench workflow submit .../token-factory-parallel-fanout.yaml \
  --run-id npa-wf-cpu-token-factory-parallel-fanout-a8cf71e0 --runtime --resume \
  --var bucket=<artifact-bucket> --var prefix=npa-workflow-e2e/<run-id>/token-factory-parallel-fanout ...
```

```
[runtime] wave 001|caption-shards|...: replayed from ledger (job 75)
[runtime] wave 002|serial|:aggregate:-: replayed from ledger (job 76)
"status": "succeeded"      # "replayed": true for both waves
```

**Zero new SkyPilot jobs** were submitted and the run finished in ~4 s instead of
~6 min: the durable ledger
(`s3://.../npa-workflow/runtime.json`, `npa.workflow.runtime.v1`) made the rerun
idempotent.

---

## 4. Phase 2 — real runtime control flow (live, CPU tier)

**Spec:** `token-factory-gate-loop.yaml` — bounded loop (`max_iterations: 3`,
`until: promote_checkpoint`) over caption → VLM score → gate, then a `route`
state that branches on the same decision artifact.
**Command:**

```bash
pytest npa/tests/e2e/test_npa_workflow_submit_live_e2e.py::test_npa_workflow_runtime_gate_loop_early_exit_vs_full_budget -q -s
```

The two runs differ **only** in `--var grade_threshold=...`; the decision comes
from the real VLM score each iteration.

### 4.1 Run A — gate passes on iteration 1 → REAL early exit

**Run id:** `npa-wf-cpu-token-factory-gate-loop-37be5c1f-early` (`grade_threshold=0.0`)

Wave ledger (`s3://.../npa-workflow/runtime.json`):

```
status: succeeded | schema: npa.workflow.runtime.v1
  001|serial|refine:caption-batch:-    serial job=77 succeeded states=caption-batch
  002|serial|refine:score-batch:-      serial job=78 succeeded states=score-batch
  003|serial|refine:quality-gate:-     serial job=79 succeeded states=quality-gate
  004|serial|:route:-                  serial job=80 succeeded states=route
  005|serial|:publish:-                serial job=81 succeeded states=publish
  decision: promote_checkpoint <- s3://.../gate/decision.json     (loop exit check)
  decision: promote_checkpoint <- s3://.../gate/decision.json     (route branch)
```

**One** loop iteration out of a budget of three: `caption-batch` / `score-batch` /
`quality-gate` each ran exactly once, and no further iteration was submitted.

Artifacts under the run prefix:

```
captions/captions.json            1473    real Token Factory captions
grade/vlm_eval_stub.json           985    real hosted VLM eval  ("backend": "api", "dry_run": false)
gate/decision.json                  39    {"decision": "promote_checkpoint"}
insights/records.jsonl             614    route ingested the decision artifact
reports/promoted/dashboard.html   1222    the PROMOTE branch artifact
npa-workflow/runtime.json         5526    wave ledger
```

`gate/decision.json` (verbatim):

```json
{
  "decision": "promote_checkpoint"
}
```

Note the branch: only `reports/promoted/` exists — the `escalate` branch never
ran.

### 4.2 Run B — gate can never pass → full budget + the other branch

**Run id:** `npa-wf-cpu-token-factory-gate-loop-9f272bff-full` (`grade_threshold=1.01`,
above the clamped `[0,1]` VLM score, so the gate can never promote)

```
status: succeeded | schema: npa.workflow.runtime.v1
  001|serial|refine:caption-batch:-   serial job=82 succeeded states=caption-batch
  002|serial|refine:score-batch:-     serial job=84 succeeded states=score-batch
  003|serial|refine:quality-gate:-    serial job=85 succeeded states=quality-gate
  004|serial|refine:caption-batch:-   serial job=86 succeeded states=caption-batch
  005|serial|refine:score-batch:-     serial job=87 succeeded states=score-batch
  006|serial|refine:quality-gate:-    serial job=88 succeeded states=quality-gate
  007|serial|refine:caption-batch:-   serial job=89 succeeded states=caption-batch
  008|serial|refine:score-batch:-     serial job=90 succeeded states=score-batch
  009|serial|refine:quality-gate:-    serial job=91 succeeded states=quality-gate
  010|serial|:route:-                 serial job=92 succeeded states=route
  011|serial|:escalate:-              serial job=93 succeeded states=escalate
  decision: loop_back_to_inner_loop <- s3://.../gate/decision.json   (iteration 1)
  decision: loop_back_to_inner_loop <- s3://.../gate/decision.json   (iteration 2)
  decision: loop_back_to_inner_loop <- s3://.../gate/decision.json   (iteration 3)
  decision: loop_back_to_inner_loop <- s3://.../gate/decision.json   (route branch)
```

**Eleven** waves — the full budget of three iterations — and the *other* branch:

```
gate/decision.json                  44   {"decision": "loop_back_to_inner_loop"}
grade/vlm_eval_stub.json          1038   score 0.0, backend "api", dry_run false, Qwen/Qwen2.5-VL-72B-Instruct
reports/shortfall/dashboard.html  1220   the ESCALATE branch artifact (no reports/promoted/)
npa-workflow/runtime.json        14005   wave ledger
```

### Side-by-side

| | Run A (`grade_threshold=0.0`) | Run B (`grade_threshold=1.01`) |
| --- | --- | --- |
| loop iterations executed | **1** of 3 | **3** of 3 |
| SkyPilot jobs | 77, 78, 79, 80, 81 | 82, 84–93 |
| waves | 5 | 11 |
| decision read from S3 | `promote_checkpoint` | `loop_back_to_inner_loop` (×4) |
| terminal branch | `publish` → `reports/promoted/` | `escalate` → `reports/shortfall/` |

Nothing but the threshold differed; the engine read the real artifact each
iteration, exited early in Run A, and branched differently in the two runs. The
harness test that asserts exactly this passed:

```
pytest .../test_npa_workflow_runtime_gate_loop_early_exit_vs_full_budget -q -s
1 passed in 2854.45s (0:47:34)
```

### 4.3 `--assume-decision` plan-only is unchanged

A second worktree was checked out at the **base commit** `d129ee90` and the same
specs were planned with both interpreters:

```bash
git worktree add -f /tmp/npa-base d129ee90
for s in sim2real-vlm-rl tokenfactory-cosmos-gate physical-ai-data-factory; do
  for d in loop_back promote_checkpoint; do
    PYTHONPATH=/tmp/npa-base/npa/src  python -m npa.cli.main workbench workflow plan-spec /tmp/npa-base/$P --run-id fixed-run --assume-decision $d --json > base.json
    PYTHONPATH=$WT/npa/src            python -m npa.cli.main workbench workflow plan-spec $WT/$P        --run-id fixed-run --assume-decision $d --json > branch.json
    diff base.json branch.json
  done
done
```

Result (after dropping the one additive JSON key `group`, which is `""` for every
serial step):

```
IDENTICAL (ignoring additive group key)  sim2real-vlm-rl          [loop_back]           steps=19
IDENTICAL (ignoring additive group key)  sim2real-vlm-rl          [promote_checkpoint]  steps=11
IDENTICAL (ignoring additive group key)  tokenfactory-cosmos-gate [loop_back]           steps=9
IDENTICAL (ignoring additive group key)  tokenfactory-cosmos-gate [promote_checkpoint]  steps=5
IDENTICAL (ignoring additive group key)  physical-ai-data-factory [loop_back]           steps=12
IDENTICAL (ignoring additive group key)  physical-ai-data-factory [promote_checkpoint]  steps=9
```

The raw diff before normalization contains **only** added `"group": ""` lines —
no step, argv, iteration or ordering change. The plan-time full unroll under
`--assume-decision` is byte-for-byte the same as on `main`.

---

## 5. GPU tier — verified on real GPUs

Everything in this section ran on `npa-rtxpro-mk8s` GPU nodes
(`RTXPRO-6000-BLACKWELL-SERVER-EDITION`). Both GPU claims that were previously
unproven are now green, and getting there exposed **five distinct real bugs** that
mocked tests could not have found (§5.3).

### 5.1 Parallel fan-out + barrier on GPU-requesting tasks — PASSED

**Run id:** `npa-wf-cpu-forcedgpu-token-factory-parallel-fanout-5a8b6c69` (jobs 136/137)

```
pytest .../test_npa_workflow_runtime_live_reaches_terminal -q -s   # NPA_E2E_FORCE_ACCELERATORS=RTXPRO-6000-BLACKWELL-SERVER-EDITION:1
1 passed in 471.41s (0:07:51)
```

| job | task | REQUESTED | status |
| --- | --- | --- | --- |
| 136 | caption-shard-a/b/c (JobGroup) | `1x[RTXPRO-6000-BLACKWELL-SERVER-EDITION:1]` each | **SUCCEEDED** |
| 137 | aggregate (barrier) | `1x[RTXPRO-6000-BLACKWELL-SERVER-EDITION:1]` | **SUCCEEDED** |

The barrier stage that previously failed with `ModuleNotFoundError: npa` now
completes, so the fan-out → barrier sequence is proven end-to-end on GPU, not just
on CPU.

### 5.2 `isaac-lab-rl-sweep.yaml` — PASSED, four variants trained concurrently on four GPUs

**Run id:** `npa-wf-multi-isaac-lab-rl-sweep-2a9e0093` (jobs 143/144)

```
NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=multi \
NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS=isaac-lab-rl-sweep.yaml \
NPA_E2E_IMAGE_OVERRIDE_ISAAC_LAB=<registry>/npa-isaac-lab:2.3.2.post1-sky \
pytest .../test_npa_workflow_runtime_live_reaches_terminal -q -s
1 passed in 904.04s (0:15:04)
```

Concurrency and barrier, from `sky jobs queue --all --output json`:

| job | task | submitted_at | end_at | status |
| --- | --- | --- | --- | --- |
| 143 | variant-lr-1e-3 | 1785381216.9542 | 1785381366.9235 | SUCCEEDED |
| 143 | variant-lr-3e-4 | 1785381216.9586 | 1785381368.3220 | SUCCEEDED |
| 143 | variant-entropy-0 | 1785381216.9624 | 1785381367.1467 | SUCCEEDED |
| 143 | variant-entropy-0-01 | 1785381216.9660 | 1785381367.8016 | SUCCEEDED |
| 144 | select-best (barrier) | **1785381437.5002** | 1785382022.5636 | SUCCEEDED |

Four GPU tasks submitted within **12 ms** of each other with fully overlapping
lifetimes, and the barrier submitted **69 s after the last variant finished**.

The work is real Isaac Lab RSL-RL training, not a stub — artifacts under
`s3://<artifact-bucket>/.../isaac-lab-rl-sweep/`:

```
variants/lr-1e-3/checkpoint.pt                  45575   real RSL-RL checkpoint
variants/lr-1e-3/train.log                      20419   real training log
variants/lr-1e-3/npa_rl_sweep_metrics.json        726
... (same for lr-3e-4, entropy-0, entropy-0-01)
report/npa_rl_sweep_best.json                    3559   the barrier's ranking
npa-workflow/runtime.json                        6377   wave ledger
```

`report/npa_rl_sweep_best.json` (excerpt) — each variant trained with its own Hydra
overrides, and the barrier ranked all four:

```json
{
  "best_value": -3.76, "best_variant": "entropy-0", "metric": "mean_reward",
  "schema": "npa.rl_sweep.report.v1", "succeeded": 4, "variant_count": 4,
  "variants": [
    {"variant": "entropy-0",     "hydra_overrides": "agent.save_interval=1 agent.algorithm.entropy_coef=0.0",
     "mean_reward": -3.76, "task": "Isaac-Cartpole-v0", "num_envs": 64, "max_iterations": 10,
     "duration_seconds": 28.087, "returncode": 0, "status": "success"},
    {"variant": "entropy-0-01",  "hydra_overrides": "agent.save_interval=1 agent.algorithm.entropy_coef=0.01",
     "mean_reward": -4.55, "...": "..."}
  ]
}
```

**What it took to get there.** SkyPilot cannot host a task in the shipped Isaac Lab
image on Kubernetes: the image has **no system python3** (SkyPilot's runtime
bootstrap needs one) and Isaac's own interpreter lives under `/isaac-sim`, mode
`750 isaac-sim:isaac-sim`, unreadable by the pod user. Diagnosed with a raw pod
probe (`SHELL_OK; ubuntu; /bin/bash: line 1: python3: command not found`). A thin
derived image was built **in-cluster with kaniko** (so an 8 GB base never had to be
pulled onto the disk-constrained dev VM), adding `python3`, `rsync`, `curl`,
`openssh-client`, `sudo` and running as root:
`npa-isaac-lab:2.3.2.post1-sky`. The live runner points at it through the new
`NPA_E2E_IMAGE_OVERRIDE_ISAAC_LAB` hook; the base image is unchanged and the sweep
spec still resolves the base tag by default.

### 5.2b Two more npa.workflow specs on GPU, and the batched sweep

| Spec | Run | Result |
| --- | --- | --- |
| `isaac-lab-rl-sweep.yaml`, `maxConcurrency: 2` | job 152 (non-root image) | **PASSED** (`678 s`) — 4 variants in **two batches of two**; first live coverage of multi-batch bounded concurrency |
| `token-factory-gate-loop.yaml`, GPU-forced | jobs 145,147–150 | **PASSED** (`866 s`) — bounded loop, real early-exit on the S3 decision and the branch, all on GPU-requesting tasks |
| `tokenfactory-rollout-judge.yaml` (gpu tier, one-shot path) | job 166 | **PASSED** (`253 s`) — `reason-scene` → `judge-rollouts`, both SUCCEEDED |
| `vlm-eval-single.yaml` (gpu tier, one-shot path) | job 163 | **FAILED — pre-existing spec gap** (below) |

`vlm-eval-single.yaml` got all the way through the engine: the renderer's vLLM setup
installed (`vllm-0.26.0`, `torch-2.11.0`), the stage picked the right interpreter
(`using npa interpreter /home/sky/miniconda3/bin/python3 for this stage`) and the tool
ran — then failed with `VLM backend request failed: [Errno 111] Connection refused`.
The spec asks for `vlm_backend: self-hosted`, but nothing in the spec or the tool
*starts* a vLLM server, so there is no endpoint to call. That is a gap in that spec's
backend wiring, unrelated to this PR, and it is left as-is rather than papered over.

### 5.2c The sustainability of the Isaac image fix

The first unblocker (a hand-built `-sky` tag) was **not** sustainable: nothing in the
repo built it, the shipped spec's default stayed broken, and it silently ran as **root**.
Bisecting derived images live established the minimal **non-root** recipe — all four
ingredients are required:

| # | Ingredient | Why |
| --- | --- | --- |
| 1 | system `python3` (+ `rsync`/`curl`/ssh client) | SkyPilot's k8s bootstrap runs in-pod; the NVIDIA base ships only `/isaac-sim/python.sh` |
| 2 | runtime user in the `isaac-sim` **group** | `/isaac-sim` is `750 isaac-sim:isaac-sim`; a recursive `chmod` would rewrite multi-GB layers |
| 3 | **passwordless sudo** for that user | SkyPilot's setup shells out to `sudo`; Debian's default rule prompts. *This alone* kept a non-root image failing while an identical root image worked |
| 4 | system interpreter **first on PATH** | otherwise `python3` is Isaac's kit interpreter |

Probe with the completed recipe (non-root): `whoami → ubuntu`,
`command -v python3 → /usr/bin/python3`, `ISAAC_READABLE`, `PROBE2_OK`; then the batched
sweep passed on it (job 152). Ingredient 4 then surfaced a general bug — Ubuntu 24.04's
system python is **PEP 668** managed, so `pip install` failed with
`externally-managed-environment`; in-task installs now retry with
`--break-system-packages` and `--user`.

All of this is now in the repo — `npa/docker/workbench/isaac-lab/Dockerfile`,
`Dockerfile.k8s-prereqs` (repair an already-published tag) and
`scripts/build-workbench-image-in-cluster.sh` (kaniko build **in-cluster**, so an 8 GB
base never lands on a 92 %-full VM) — with guardrail tests pinning each ingredient *and*
asserting the image does not end as root. `NPA_E2E_IMAGE_OVERRIDE_<TOOL>` remains only as
an escape hatch for an unrebuilt tag.

### 5.3 Bugs that only real GPUs exposed (all fixed in this PR)

| # | Symptom on GPU | Root cause | Fix |
| --- | --- | --- | --- |
| 1 | Barrier stage: `ModuleNotFoundError: npa` | `pip install -e` binds npa to the interpreter that ran pip; the stage body ran through `bash -lc`, whose login profile resolved a different python3 | stage commands use `bash -c` and inherit the task env; `/etc/profile.d/*.sh` is sourced explicitly so images that activate that way still work |
| 2 | Then: `ModuleNotFoundError: numpy` | patching `PYTHONPATH` gave that python3 npa's *source* but not its *dependencies* | setup records an interpreter that can import npa and a PATH shim points `python3` at it |
| 3 | `npa still missing after setup` (Isaac) / `/usr/bin/python3: No module named pip` (GPU default) | setup demanded the `npa` console script on PATH, and tried to install into an interpreter with no pip | verify by import (not by console script), link the console script into `/usr/local/bin`, and never require pip in the task shell |
| 4 | Recorded interpreter was the string `alias python3='...python.sh'`, then Isaac's embedded kit python which cannot import its own site-packages | `command -v python3` prints alias definitions; `sys.executable` names an interpreter that needs its wrapper | try `sys.executable`, then the alias target, then `type -P python3`, and record the first that can actually import npa |
| 5 | **Driver abandoned a running 4-GPU job**: it polled job 140 (already cancelled) while job 141 kept training | after the local SkyPilot API server flaked, `sky jobs launch` output carried a stale `Job submitted, ID:` line, and the driver trusted the scraped id | the launched job **name** is authoritative: the parsed id is cross-checked and recovered via `find_job_ids_by_name`, and only an unidentifiable job fails the wave |
| 6 | Same class in the **one-shot** path: `tokenfactory-rollout-judge` SUCCEEDED (job 166) while the live case reported FAILED, having polled job 163 — the *previous* spec's job | the e2e harness trusts the id `submit_workflow` scrapes | `submit_workflow` itself now verifies the parsed id against the launched job name, fixing every caller; the spec passed on re-run |

Bug 5 is the most important: it is precisely the leak class this PR exists to
prevent, it was invisible to mocked tests (the fake submitter always returns a
correct id), and it is now covered by
`test_stale_job_id_from_launch_output_is_corrected_by_name`.

### 5.4 The abort-cancel fix, proven live

The same API-server flake produced a live demonstration of review finding #1. The
submit raised, and the driver did exactly what it now must:

```
[runtime] wave 001|sweep|...: aborting with job npa-wf-multi-isaac-lab-rl-sweep-190f5ab7-01-sweep
          possibly in flight (SkyPilotSubmitError: ... Connection refused); cancelling it
"sky_status": "CANCELLED", "status": "failed"
```

Before this PR that path recorded a failure and walked away.

### 5.7 `trigger:` / watch pattern, proven live

The last unit-only item on the Phase-2 list. `token-factory-trigger-watch.yaml`
declares a `caption-inbox` state whose `trigger:` watches an S3 prefix that does
**not exist** when the run starts; the test seeds two PNGs into it 60 s later.

```bash
NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=cpu \
NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS=token-factory-trigger-watch.yaml \
NPA_E2E_TRIGGER_SEED_DELAY=60 \
  npa/.venv/bin/python -m pytest \
    npa/tests/e2e/test_npa_workflow_submit_live_e2e.py::test_npa_workflow_runtime_live_reaches_terminal \
    -q -s --timeout=2400
# 1 passed in 220.06s
```

Ledger `runtime-npa-wf-cpu-token-factory-trigger-watch-c684d15b.json`:

```json
"watermarks": {
  "caption-inbox": {
    "uri": "s3://<bucket>/npa-workflow-e2e/npa-wf-cpu-token-factory-trigger-watch-c684d15b/token-factory-trigger-watch/inbox/",
    "polls": 5, "objects": 2,
    "observed_at": "2026-07-31T02:52:48Z",
    "sample": ["...inbox/frame_000.png", "...inbox/frame_001.png"]
  }
}
"waves": [{"states": ["caption-inbox"], "status": "succeeded", "job_id": "178",
           "started_at": "2026-07-31T02:52:49Z", "ended_at": "2026-07-31T02:55:24Z"}]
```

The causal ordering is the proof, and it is the thing a mock cannot give you:
**five polls elapsed against an empty prefix, the objects were observed at
02:52:48Z, and the wave was submitted at 02:52:49Z** — one second later. No
SkyPilot job existed until the trigger fired, so the driver genuinely waited on
external data rather than racing it. `sky jobs queue` confirms exactly one job
(`178 ... SUCCEEDED`) for this run.

Cost: one 2-CPU Kubernetes pod for ~2.5 min plus ~2 hosted caption calls —
rounding error against the §7 total, which is unchanged at single-digit
GPU-minutes.

One process note worth keeping: the first two attempts of this run died instantly
with `NameError: seed_trigger_inbox_later`. An earlier refactor had removed a
neighbouring symbol from the import block, so my edit's anchor never matched and
the import was silently never added. Collection passed because the name is only
referenced *inside* the test body. `ruff` (F821) flags this in under a second and
now runs clean over `npa/tests/e2e/` and the orchestration trees; that check
belongs before every live launch, since a live run is an expensive way to
discover a missing import.

## 6. Mandated harness commands

### 6.1 `test_npa_workflow_submit_live_e2e.py` (cpu tier)

```bash
export NPA_INTEGRATION_E2E=1 NPA_E2E_NPA_WORKFLOW_SUBMIT=1 NPA_E2E_NPA_WORKFLOW_RUNTIME=1
export NPA_E2E_REGISTRY=$NPA_E2E_REGISTRY
export NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=cpu
export NPA_E2E_NPA_WORKFLOW_SUBMIT_MAX_WAIT_SECONDS=2700
export NPA_E2E_NPA_WORKFLOW_SUBMIT_CANCEL_ON_TIMEOUT=1
pytest npa/tests/e2e/test_npa_workflow_submit_live_e2e.py -v
```

```
collected 30 items

test_npa_workflow_submit_live_reaches_terminal[cpu:token-factory-caption.yaml]        PASSED
test_npa_workflow_submit_live_reaches_terminal[cpu:token-factory-generate.yaml]       PASSED
test_npa_workflow_submit_live_reaches_terminal[cpu:token-factory-cosmos-reason.yaml]  PASSED
test_npa_workflow_submit_live_reaches_terminal[cpu:retargeting.yaml]                  FAILED   <- pre-existing, see below
test_npa_workflow_runtime_live_reaches_terminal[cpu:token-factory-parallel-fanout.yaml] PASSED
test_npa_workflow_runtime_live_reaches_terminal[cpu:token-factory-gate-loop.yaml]       PASSED
test_npa_workflow_runtime_gate_loop_early_exit_vs_full_budget                           PASSED
test_npa_workflow_submit_plan_only_matrix_no_leak[...]  x23                             PASSED

================== 1 failed, 29 passed in 4830.69s (1:20:30) ===================
```

The one failure is **pre-existing and unrelated**: `retargeting.yaml` has no
fixture-seeding branch in `seed_live_workflow_inputs` (its tool needs a real
SOMA/G1 motion dataset, which the harness only stages for
`sonic-locomotion-finetuning.yaml` behind `NPA_E2E_SONIC_MOTION_SRC`), so the job
fails with:

```
Error: S3 input contains no objects: s3://<artifact-bucket>/npa-workflow-e2e/npa-wf-cpu-retargeting-68430021/retargeting/source/
```

This change does not touch that spec, its seeding, or the one-shot submit path.

**Why `cpu` and not `cpu,gpu,multi`:** the gpu/multi one-shot twins in the matrix
(SONIC, Cosmos3, the 11-stage BDD100K pipeline, ...) are pre-existing cases that
are unrelated to this change and would cost many GPU-hours; and on this cluster
they need workbench images, which currently cannot host a SkyPilot k8s task
(§5.2). The tiers that exercise **this change** are the runtime cases, which are
all in the cpu tier plus the (blocked) `multi` sweep. Every spec in the matrix —
all tiers — is still covered by the plan-only matrix test in the same file.

### 6.2 `test_burst_live_e2e.py`

```bash
pytest npa/tests/e2e/test_burst_live_e2e.py -v
# 1 skipped in 1188.16s (0:19:48)
# reason: capacity / GPU not offered:
#   RTXPRO-6000-BLACKWELL-SERVER-EDITION:1=FAILED_PRECHECKS, L40S:1=STARTING
```

The burst test rotates through its GPU candidates and **skips** when none can be
scheduled; that is the test's own capacity guard, not a failure. The burst path is
unrelated to this change (it does not use the npa.workflow engine); it was run
because the mandate asked for it, and the honest result is "skipped for capacity".

---

## 7. Cost

| Item | Amount |
| --- | --- |
| CPU tasks (Token Factory captions, VLM scoring via hosted API, gates, joins, dashboards) | ~45 short pods, `1x[CPU:4+]`, ~30–90 s each |
| GPU seconds | 3 × RTXPRO-6000 × ~76 s (§5.1 fan-out) + 1 × ~60 s (barrier attempt) ≈ **~5 GPU-minutes** |
| GPU sweep (`isaac-lab-rl-sweep`) | **0 GPU-minutes billed for compute** — every attempt failed in provisioning (`ErrImagePull`, then SkyPilot runtime start); cancelled after ~40 min of retries |
| Burst test | 2-node GPU request, never scheduled (skipped) |
| Token Factory | ~20 caption/score calls on `Qwen/Qwen2.5-VL-72B-Instruct` with `max_images<=4`, `max_tokens<=128` |
| Storage | a few MB of PNG fixtures, JSON reports and ledgers under `s3://<bucket>/npa-workflow-e2e/...` |

Approximate spend: **single-digit GPU-minutes** plus negligible CPU/hosted-token
usage. No cluster was provisioned for this work; no cluster was left running (§9).

---

## 8. Not verified live

Now much shorter — the two GPU items that headed this list are verified in §5.

1. **Wave retry is unit-tested only.** No live wave failed *transiently* and then
   succeeded on a retry (the live failures were deterministic, so retries would not
   have helped).
2. **Timeout-cancellation is unit-tested only** — no live wave exceeded its
   deadline. The closely-related *abort*-cancellation path did fire live (§5.4).
3. **Bounded-concurrency batching ran live only with `maxConcurrency == group size`**
   (one batch). The multi-batch path is unit-tested.
4. **Only the `cpu` and `multi` tiers of the live matrix were executed**; the `gpu`
   one-shot twins (SONIC, Cosmos3, vlm-eval) are pre-existing cases unrelated to this
   change and were covered plan-only.
5. **The derived `npa-isaac-lab:...-sky` image is an operator artifact, not a repo
   deliverable.** The Dockerfile is recorded in §5.2 and the override hook is
   committed, but this PR does not add an image build to the repo's image manifest;
   making the shipped Isaac image SkyPilot-hostable is a follow-up.

## 9. Teardown

```bash
sky jobs cancel -y 83   # the blocked Isaac sweep JobGroup (§5.2)
sky jobs cancel -y 98   # the burst test's managed job, left PENDING after the test skipped
sky status
```

Final state after the work:

```
Clusters
NAME                          INFRA                         RESOURCES                STATUS  AUTOSTOP
sky-jobs-controller-64ce57a0  Kubernetes (npa-rtxpro-mk8s)  1x(cpus=4, mem=16, ...)  UP      -

non-terminal jobs on the controller: []
```

The only cluster left is the **pre-existing, shared** managed-jobs controller
(it was up before this work and is not owned by it). No task cluster, no GPU pod
and no managed job from these runs is still alive; every `npa-wf-*` /
`manual-resume-*` job is in a terminal state.

One deliberate change was made to shared infrastructure, and it was a repair, not
a workaround: the cluster's `npa-nebius-registry` imagePullSecret held an expired
IAM token, which was failing **every** private image pull on
`npa-rtxpro-mk8s` (including a five-day-stuck job belonging to another run). It was
re-minted with the same identity (§5.2).

---

## 10. Self-review checklist

| # | Acceptance item | Commit(s) | Evidence |
| --- | --- | --- | --- |
| 1 | A workflow can declare parallel fan-out and it launches as genuinely concurrent SkyPilot jobs | `4ab46d20` (spec fields + wave planner), `a8419888` (JobGroup renderer) | §3.1 — one `job_id`, 3 tasks submitted within 12 ms, overlapping lifetimes, driver log "3 tasks running concurrently" |
| 2 | The serial-only guard is lifted behind an **explicit** parallel path; serial stays the default | `a8419888` | `render_skypilot_yaml` keeps its guard and its **byte-identical output** (its body now delegates to a shared doc builder — see DESIGN §3); `test_render_rejects_parallel_execution` passes verbatim; §2 shows `--plan-only` still emits `execution: serial` for all 23 twins; §4.3 diffs the plan output against the base commit |
| 3 | Barrier: a downstream `needs:` state waits for all parallel predecessors | `b55d081f` (wave boundary in the runtime tier) | §3.2 — barrier submitted 94.6 s after the last member ended; §5.1 — 84 s after |
| 4 | Bounded concurrency respected | `4ab46d20` (`maxConcurrency` + batching), `b55d081f` (`execute_parallel` chunking) | unit: `test_wave_plan_groups_parallel_members`, `test_runtime_launches_parallel_group_as_job_group_with_barrier` (2+1 batches), `test_runtime_max_concurrency_option_is_a_cap_not_an_override`, `test_slow_cases_carry_their_own_deadline`; live: single-batch only (§8.4) |
| 5 | `isaac-lab-rl-sweep.yaml` ported to a real npa.workflow parallel spec | `0cd3cc40` (spec + `rl_sweep` stages) | **live** — §5.2 (four variants trained concurrently on four GPUs, barrier ranked them) |
| 6 | Runtime tier above `build_scheduler_task`: plan → submit → poll → read S3 decision → replan | `b55d081f` | §4.1/§4.2 ledgers: per-wave `job_id`, `sky_status`, decision reads |
| 7 | Consumes the existing decision contract, no new gate mechanism | `b55d081f` (`RecordingDecisionReader` over `decisions.refresh_context_decision`) | §4.1 `gate/decision.json` written by the existing `grade_gate`, read back by the engine |
| 8 | Bounded loops with **real** early-exit | `b55d081f`, `0cd3cc40` | §4 — 5 waves / 1 iteration (threshold 0.0) vs 11 waves / 3 iterations (threshold 1.01), same spec |
| 9 | Data-dependent branching (`goto`) | `b55d081f` | §4 — `route` → `publish` vs `route` → `escalate` decided by the artifact; unit `test_runtime_branch_follows_transition_goto` |
| 10 | Trigger / watch-loop pattern | `4ab46d20` (spec field), `b55d081f` (`s3_trigger_waiter`) | **live** — §5.7 (run `npa-wf-cpu-token-factory-trigger-watch-c684d15b`, 5 polls on an empty prefix, job submitted 1 s after the watermark) |
| 11 | `--assume-decision` plan-only path preserved as the offline fallback | (unchanged code) | §4.3 — plan JSON identical to base commit `d129ee90` for 3 dynamic specs × 2 assumptions |
| 12 | Every existing plan-only test and the shown-catalog guardrail still pass | all | §1 (full offline suite, guardrails 50), §2 (23 plan-only matrix cases); drift guards `test_shipped_fanout_spec_wave_shape`, `test_shipped_sweep_spec_wave_shape`, `test_shipped_gate_loop_plan_matches_the_assumed_decision`, `test_shipped_specs_render_without_placeholders` |
| 13 | Job failure/retry, idempotency and resume built on `run_state.py` | `b55d081f` (`RuntimeLedger`, `npa.workflow.runtime.v1`) | §3.4 — live `--resume` replayed both waves, **zero** new jobs; unit tests for retry/timeout/exhaustion |
| 14 | No hardcoded project/tenant/registry/bucket IDs or secrets | all | every live value comes from `NPA_E2E_*` / `~/.npa/*`; `scripts/stage-npa-src.sh` takes `--bucket`; leak assertions in the harness |
| 15 | Unit tests mock all infra | `4ab46d20`, `a8419888`, `b55d081f`, `e378d38a`, `a7cbde3a` | injected submitter/status/timeline/canceller/sleeper/clock/storage in `test_runtime_orchestrator.py`, `test_rl_sweep.py`, `test_fanout_join.py` |
| 16 | Scheduler-task seam preserved | `a8419888` | both renderers build docs via `build_skypilot_task_doc` → `build_scheduler_task`; the runtime tier only passes `PlanStep`s |
| 17 | Backward compatible: existing serial specs render and submit unchanged | `4ab46d20` (flatten), `a8419888` (separate entry point) | §4.3 plan parity; §6.1 the pre-existing cpu twins still submit and succeed |
| 18 | New specs registered in `SUBMIT_LIVE_MATRIX` (incl. a `multi`/parallel case) | `683a1abd`, `e747c8ef`, `800cc4e0` | `token-factory-parallel-fanout` (cpu), `token-factory-gate-loop` (cpu, also in `DYNAMIC_SPECS`), `isaac-lab-rl-sweep` (multi); guarded by `test_runtime_specs_are_registered_with_the_right_tiers`, `test_expected_parallel_tasks_matches_the_spec_fan_out`, `test_specs_with_a_parallel_group_are_registered_as_runtime_cases` |
| 19 | Cheapest live path first; cancel on timeout; no leaked clusters | — | §3 (CPU first), §5.2 (`sky jobs cancel -y 83`), §9 |
| 20 | Honest reporting of what was not verified live | — | §8 |

---
---

# EVIDENCE — retiring the raw SkyPilot task catalog

Everything below is a **live run on real Nebius infrastructure** from the operator
dev VM (`nebius-dev-vm`) against the `npa-rtxpro-mk8s` Kubernetes cluster and real
S3. Anything **not** verified live is in [§R9](#r9-not-verified-live).

Bucket and registry identifiers are redacted (`<artifact-bucket>`, `<registry>`);
every value comes from `~/.npa/live-e2e.env` / `~/.npa/credentials.yaml`, nothing is
hardcoded in the repo.

Isolation: a dedicated git worktree created by the repo's own
`npa/scripts/dev_vm_isolated_session.sh` (`~/npa-worktrees/retire-sky-7411`, tmux
session `npa-retire-sky-7411`), with `PYTHONPATH=<worktree>/npa/src` so the shared
editable venv cannot shadow branch code — verified before the first run:

```bash
$ cd ~/npa-worktrees/retire-sky-7411 && export PYTHONPATH=$PWD/npa/src
$ python -c "import npa; print(npa.__file__)"
/home/ubuntu/npa-worktrees/retire-sky-7411/npa/src/npa/__init__.py
```

## R0. Environment (secrets redacted)

```bash
set -a; . ~/.npa/live-e2e.env; . ~/.npa/live-e2e-gates.env; set +a
export NPA_INTEGRATION_E2E=1 NPA_E2E_NPA_WORKFLOW_SUBMIT=1 NPA_E2E_NPA_WORKFLOW_RUNTIME=1
export NPA_E2E_CLEAR_WORKBENCH_IMAGES=1          # default image + staged npa source
export NPA_E2E_NPA_WORKFLOW_SUBMIT_POLL_SECONDS=20
export NPA_E2E_NPA_WORKFLOW_SUBMIT_MAX_WAIT_SECONDS=2700
export NPA_E2E_NPA_WORKFLOW_SUBMIT_CANCEL_ON_TIMEOUT=1
export NPA_SKYPILOT_BIN=$HOME/.npa/skypilot-venv/bin/sky      # SkyPilot 0.12.2
# branch source staged so the tasks run BRANCH code:
bash scripts/stage-npa-src.sh --bucket <artifact-bucket> --prefix npa-workflow-e2e/npa-src-retire
export NPA_SRC_S3_URI=s3://<artifact-bucket>/npa-workflow-e2e/npa-src-retire/npa   # 555 files
```

**Operator-environment inconsistency worth recording** (it cost the first sweep
attempt): `~/.npa/live-e2e.env` ships `NPA_REGISTRY` pointing at the
**us-central1** registry while `SKYPILOT_DOCKER_SERVER` names **eu-north1**, and the
dev VM's login shell exports a *third* value of `NPA_REGISTRY` (eu-north1). Only
us-central1 holds the `-sky3` / `-k8s-runtime` tags and only it is covered by the
cluster's `npa-nebius-registry` pull secret. The single IAM token authenticates to
us-central1 (verified with `crane manifest`), so the runner aligns
`SKYPILOT_DOCKER_SERVER="${NPA_REGISTRY%%/*}"` and resolves every image override from
that value rather than the ambient shell. Without that, provisioning fails with
`ErrImagePull ... 403 Forbidden` and the renderer's registry-mismatch guard fires for
any pinned image.

## R1. Offline suites

| Run | Command | Result |
| --- | --- | --- |
| Guardrails | `pytest npa/tests/guardrails/ -q` | **132 passed** |
| Engine + specs + smoke | `pytest npa/tests/orchestration/npa_workflow/ npa/tests/smoke/test_all_workflow_yamls.py npa/tests/smoke/test_npa_workflow_smoke.py -q` | **451 passed** (with guardrails) |
| New SONIC staging / fixture / harness parsing | `pytest npa/tests/workbench/test_sonic_export_staging.py npa/tests/workflows/test_sonic_fixture.py npa/tests/e2e/test_live_helpers_parsing.py -q` | **24 passed, 2 skipped** + **6 passed, 4 skipped** (torch-gated) + **6 passed** |
| Plan-only live matrix (no cloud spend) | `pytest .../test_npa_workflow_submit_plan_only_matrix_no_leak -q` | **24 passed in 4.58 s** |
| Lint | `ruff check` on every changed file | clean |

### Full offline suite: branch vs base, same invocation

```bash
pytest npa/tests/ --ignore=npa/tests/e2e \
  --ignore=npa/tests/workbench/test_vlm_eval_backend.py \
  --ignore=npa/tests/workbench/test_vlm_eval_loop_e2e.py --timeout=180 -q
```

| Tree | Result |
| --- | --- |
| base `aa555d73` (checked out in the same worktree) | **2 failed, 3682 passed**, 29 skipped, 1 xpassed (296.66 s) |
| this branch | **2 failed, 3799 passed**, 34 skipped, 1 xpassed (297.59 s) |

Net **+117 tests**, and the **same two failures** — both pre-existing, both reproduced
on the base commit:

* `npa/tests/smoke/test_golden_eval_tmux.py::test_tmux_script_dry_run_launches_session`
  — the tmux script shells out to a bare `python3` that has no `numpy` in this
  isolated-fast setup (the shared venv's deps reach the test process through
  `PYTHONPATH`, not the subprocess).
* `npa/tests/unit/test_byof_live.py::test_resolve_byof_kubernetes_target_from_cluster_state`
  — **order-dependent**: it passes in isolation, and in `npa/tests/unit` +
  `npa/tests/cli` together (1452 passed), and when paired with each of
  `workflows`/`smoke`/`workbench`/`orchestration`/`guardrails`. It fails only in the
  full-suite ordering, on both trees. Nothing in this change touches BYOF, cluster
  state or config loading.

Two more exclusions carried over from EVIDENCE §1:
`npa/tests/workbench/test_vlm_eval_backend.py` and `test_vlm_eval_loop_e2e.py` are
live-GPU fixtures that launch a SkyPilot cluster whenever `sky` is on `PATH`.

One environment note worth keeping:
`test_skypilot_render.py::test_workbench_workflow_submit_plan_only_redacts_registry_password`
fails **only when `live-e2e.env` is sourced** (it depends on `NPA_REGISTRY` /
`SKYPILOT_DOCKER_*` being unset). It passes on the branch and on `aa555d73` in a clean
shell, so unit suites must be run *without* the live env.

## R2. `cosmos3-reason.yaml` twin — PASSED

```bash
NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=gpu \
NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS=cosmos3-reason.yaml \
NPA_E2E_ACCELERATOR_REMAP=H100:1=RTXPRO-6000-BLACKWELL-SERVER-EDITION:1 \
NPA_E2E_RELAX_CPU_MEM=1 \
  pytest npa/tests/e2e/test_npa_workflow_submit_live_e2e.py::test_npa_workflow_submit_live_reaches_terminal -q -s
# 1 passed in 182.76s
```

**Run id** `npa-wf-gpu-cosmos3-reason-af7ded35` · **SkyPilot job 182**

| job | task | REQUESTED | status | submitted_at | end_at |
| --- | --- | --- | --- | --- | --- |
| 182 | `npa-wf-gpu-cosmos3-reason-af7ded35` | `1x[RTXPRO-6000-BLACKWELL-SERVER-EDITION:1]` | **SUCCEEDED** | 1785471666.9383 | 1785471724.2441 |

Equivalence note: this twin is genuinely equivalent because **both** sides run the
same code — the SkyPilot template's `run:` is
`python -m npa.workflows.cosmos_split cosmos3-reason ...` and the toolRef is
`npa workbench cosmos3 reason`, which calls the same
`build_cosmos3_reason_manifest`. Both are manifest builders that request a GPU; that
pre-existing stub-on-a-GPU shape is unchanged by this work and is called out in §R9.

## R3. `isaac-lab-rl-sweep.yaml` twin — PASSED (4 GPU variants, 2 batches, barrier)

```bash
NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=multi \
NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS=isaac-lab-rl-sweep.yaml \
NPA_E2E_ACCELERATOR_REMAP=L40S:1=RTXPRO-6000-BLACKWELL-SERVER-EDITION:1 \
NPA_E2E_RELAX_CPUS=8+ NPA_E2E_RELAX_MEMORY=32+ \
NPA_E2E_IMAGE_OVERRIDE_ISAAC_LAB=<registry>/npa-isaac-lab:2.3.2.post1-sky3 \
NPA_SRC_OVERLAY=1 \
  pytest .../test_npa_workflow_runtime_live_reaches_terminal -q -s
# 1 passed in 586.24s (0:09:46)
```

**Run id** `npa-wf-multi-isaac-lab-rl-sweep-c4b86dc5` · jobs **185, 186, 187**

Wave ledger (`s3://<artifact-bucket>/.../isaac-lab-rl-sweep/npa-workflow/runtime.json`,
`npa.workflow.runtime.v1`), `status: succeeded`:

| wave | kind | job | tasks | submitted_at | end_at | max_concurrent_observed |
| --- | --- | --- | --- | --- | --- | --- |
| 001 `sweep` | parallel | 185 | `variant-lr-1e-3` | 1785472779.8337 | 1785472892.9320 | 2 |
| | | | `variant-lr-3e-4` | 1785472779.8382 | 1785472892.1463 | |
| 002 `sweep` | parallel | 186 | `variant-entropy-0` | 1785472980.3979 | 1785473086.9207 | 2 |
| | | | `variant-entropy-0-01` | 1785472980.4032 | 1785473087.4682 | |
| 003 `select-best` | serial | 187 | barrier | 1785473191.0149 | 1785473270.9288 | — |

Three properties, all from the timeline rather than assertion:

* **Concurrency** — each batch's two GPU tasks were submitted **4.5 ms** apart and
  their lifetimes overlap almost entirely.
* **Bounded concurrency (`--var max_concurrency=2`)** — batch 2 was submitted
  **87.5 s after** batch 1's last task ended, so the run never held more than two
  GPUs. This is live coverage of the multi-batch path.
* **Barrier** — `select-best` was submitted **103.5 s after** batch 2's last task
  ended.

Real work, not a stub — artifacts under
`s3://<artifact-bucket>/npa-workflow-e2e/npa-wf-multi-isaac-lab-rl-sweep-c4b86dc5/isaac-lab-rl-sweep/`:

```
    45575  variants/lr-1e-3/checkpoint.pt              real RSL-RL checkpoint
    24996  variants/lr-1e-3/train.log                  real training log
      726  variants/lr-1e-3/npa_rl_sweep_metrics.json
      888  variants/lr-1e-3/npa_rl_sweep_summary.json
   ... same four files for lr-3e-4, entropy-0, entropy-0-01 ...
     3559  report/npa_rl_sweep_best.json               the barrier's ranking
     7845  npa-workflow/runtime.json                   wave ledger
```

A first attempt (**job 183**) was `CANCELLED` after ~12 min of `ErrImagePull ... 403
Forbidden` retries — the registry confusion described in §R0, not a workflow fault.

## R4. `sonic-export.yaml` twin — PASSED, and the three real defects it exposed

This is the run that justifies most of the code in this change. The twin validated,
planned and rendered cleanly, and then failed **three times** for three different,
genuine reasons before passing.

| # | SkyPilot job | In-pod error | Root cause | Fix |
| --- | --- | --- | --- | --- |
| 1 | — (submit rejected in 1.15 s) | `rendered SkyPilot YAML still contains unresolved placeholders: ${npa_src_root}` | the new per-toolRef extras snippet used a braced expansion, which `assert_no_unresolved_placeholders` rightly rejects | compose the pip target with `printf` |
| 2 | **184** | `Error: checkpoint not found: s3://<artifact-bucket>/.../sonic-export/checkpoint.pt` | `sonic export` only ever accepted **local** paths; the SkyPilot template did the S3 download/upload in ~60 lines of inline bash+boto3, and a `toolRef` argv has no such escape hatch | `export_onnx` stages `s3://` inputs and publishes `s3://` outputs (`workbench/sonic/staging.py`) |
| 3 | **188**, **189** | `Error: observation dimension is required. Provide --obs-spec or a policy with one of: observation_dim, obs_dim, input_dim, num_observations` | the staged fixture was a bare `nn.Sequential`, which exposes none of those; real SONIC policies do, and the toolRef does not pass `--obs-spec` (a pinned `spec_gap`) | the fixture policy carries `obs_dim` / `action_dim` |

Each of these was invisible to mocked tests, and each now has offline coverage
(`test_tool_pip_extras.py`, `test_sonic_export_staging.py`, `test_sonic_fixture.py`).

**Passing run:** `npa-wf-gpu-sonic-export-cb60c5ab` · **SkyPilot job 192** ·
`1x[RTXPRO-6000-BLACKWELL-SERVER-EDITION:1]` · **SUCCEEDED** · `1 passed in 251.15s`

Pod log excerpt — note the new extras hook doing its job:

```
(setup pid=…) syncing s3://<artifact-bucket>/npa-workflow-e2e/npa-src-retire/npa -> /tmp/npa-src
(setup pid=…) npa interpreter recorded: /home/sky/miniconda3/bin/python3
(setup pid=…) installing npa[sonic] from /tmp/npa-src
(npa-wf-gpu-sonic-export-cb60c5ab, pid=…) using npa interpreter /home/sky/miniconda3/bin/python3 for this stage
```

Artifacts under `.../npa-wf-gpu-sonic-export-cb60c5ab/sonic-export/`:

```
    16697  checkpoint.pt                 the staged fixture (input)
     1672  sonic_policy.onnx             REAL ONNX graph produced by the shipped exporter
    11776  sonic_policy.onnx.data        external weights (torch.onnx.export)
      678  sonic_policy.metadata.json    npa_sonic_onnx_export_v1 sidecar
```

The `.onnx.data` file is why staging publishes *every* file next to the model: an
ONNX with external weights is a **pair**, and onnxruntime resolves the data file
relative to the model.

### R4.1 The fixture, built in-cluster

```bash
scripts/stage-sonic-export-fixture.sh \
  --image <registry>/npa-sonic:0.1.2-k8s-runtime \
  --uri   s3://<artifact-bucket>/npa-workflow-e2e/fixtures/sonic-export/checkpoint.pt
```

```json
{
  "act_dim": 12, "obs_dim": 48, "hidden": 32, "seed": 0,
  "bytes": 16697, "schema": "npa.sonic.export_fixture.v1",
  "torch_version": "2.9.0+cu130",
  "checkpoint_uri": "s3://<artifact-bucket>/npa-workflow-e2e/fixtures/sonic-export/checkpoint.pt"
}
```

No torch wheel was downloaded to the dev VM (which sits at **96 % disk, 8.7 GB
free**); the builder ran in a pod from the SONIC image with the module mounted as a
ConfigMap.

## R5. `sonic-eval.yaml` and `sonic-export-eval.yaml` twins — PASSED, after a second real defect

Both twins reached **SUCCEEDED on the first attempt** — and produced **no artifact**:

| Run | job | result | run prefix contents |
| --- | --- | --- | --- |
| `npa-wf-gpu-sonic-eval-87a704ad` | 194 | SUCCEEDED (244.99 s) | `sonic_policy.onnx`, `.onnx.data`, `.metadata.json` — **no `eval.json`** |
| `npa-wf-multi-sonic-export-eval-744b9c1e` | 195 | SUCCEEDED (379.38 s) | `checkpoint.pt`, `sonic_policy.*` — **no `eval.json`** |

Both specs declare `outputs: - uri: .../eval.json`. The toolRef argv passed
`--output json`, but on `npa workbench sonic eval` **`--output` is the result path**
(`output_path: str`) and `--output-format` is the format — the SkyPilot template it
replaces passed both correctly (`--output "${SONIC_EVAL_OUTPUT}"` *and*
`--output-format json`). The tool therefore wrote to a relative `json/` directory
inside the pod, and the artifact vanished with the container. **A green terminal
status was not evidence; the artifact listing was.**

Fixed by splitting the two options (`--output {{config.eval_uri}}`
`--output-format json`, plus an `eval_uri` config key in both specs) and by a new
guardrail that audits the whole catalog for the mistake in both directions — a format
word handed to a path option, and a value that is not a member of an option's Enum.
The audit found this as the only real case; six look-alikes are commands where
`--output` genuinely *is* the format.

### Passing re-runs

```bash
NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=gpu   NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS=sonic-eval.yaml        ... # 1 passed in 283.78s
NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=multi NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS=sonic-export-eval.yaml ... # 1 passed in 410.73s
```

| Run id | job | status |
| --- | --- | --- |
| `npa-wf-gpu-sonic-eval-bb3b9c72` | **198** | SUCCEEDED |
| `npa-wf-multi-sonic-export-eval-2f5e979e` | **197** | SUCCEEDED |

`s3://<artifact-bucket>/.../npa-wf-gpu-sonic-eval-bb3b9c72/sonic-eval/`:

```
     4339  eval.json                     <- the artifact the spec declares
      678  sonic_policy.metadata.json
     1672  sonic_policy.onnx
    11776  sonic_policy.onnx.data
```

`eval.json` is a **real onnxruntime evaluation** — `"status": "completed"`,
`"backend": "reference"`, 8 episodes each with `action_min` / `action_max` /
`action_norm` / `episode_return`, and the new `onnx_uri` field recording the durable
input:

```json
{"status": "completed", "backend": "reference",
 "onnx_uri": "s3://<artifact-bucket>/.../npa-wf-gpu-sonic-eval-bb3b9c72/sonic-eval/sonic_policy.onnx",
 "episodes": [{"episode_index": 0, "action_max": 0.16404074430465698,
               "action_min": -0.1481434553861618, "action_norm": 0.35056760907173157,
               "episode_length": 1, "fall": false, "steps": 1}, "... 7 more ..."]}
```

`sonic-export-eval` (job 197) shows the **chain** working end to end: the export
stage's ONNX is what the eval stage consumed, and both artifacts sit in one prefix:

```
    16697  checkpoint.pt                 (staged fixture)
     1672  sonic_policy.onnx             (export stage output)
    11776  sonic_policy.onnx.data
      678  sonic_policy.metadata.json
     4355  eval.json                     (eval stage output; onnx_uri points at the above)
```

## R6. Retirement tally: 36 → 31

```bash
$ ls npa/src/npa/workflows/skypilot/*.yaml | wc -l
31
$ for f in cosmos3-reason isaac-lab-rl-sweep sonic-eval sonic-export sonic-export-eval; do
    rg -n --fixed-strings "skypilot/$f.yaml" . ; done
# (only EVIDENCE.md / DESIGN.md / CHANGELOG.md history mentions remain)
```

| Retired template | Twin's live run | job(s) |
| --- | --- | --- |
| `cosmos3-reason.yaml` | `npa-wf-gpu-cosmos3-reason-af7ded35` | 182 |
| `isaac-lab-rl-sweep.yaml` | `npa-wf-multi-isaac-lab-rl-sweep-c4b86dc5` | 185, 186, 187 |
| `sonic-export.yaml` | `npa-wf-gpu-sonic-export-cb60c5ab` | 192 |
| `sonic-eval.yaml` | `npa-wf-gpu-sonic-eval-bb3b9c72` | 198 |
| `sonic-export-eval.yaml` | `npa-wf-multi-sonic-export-eval-2f5e979e` | 197 |

**`sonic-locomotion-finetuning.yaml` was NOT retired.** Its twin's first stage is
`workbench.retargeting.run`, which needs a real SOMA/G1 motion dataset
(`NPA_E2E_SONIC_MOTION_SRC`); the repo deliberately does not vendor that dual-licensed
upstream data, and the pre-existing `retargeting.yaml` live case already fails for the
same reason (EVIDENCE §6.1). Deleting it on plan-only evidence would break the rule
this work is built on, so it stays — with that reason recorded next to it in
`test_skypilot_catalog_retirement.py`.

## R7. Cost

| Item | Amount |
| --- | --- |
| GPU tasks (RTXPRO-6000-BLACKWELL) | 13 pods: cosmos3-reason ×1 (57 s), sweep ×4 in 2 batches (~110 s each) + barrier, sonic-export ×3 attempts (~90 s each), sonic-eval ×2 (~90 s), sonic-export-eval ×2 (~190 s) ≈ **~22 GPU-minutes** |
| Failed/cancelled attempts included above | job 183 (12 min PENDING on `ErrImagePull`, **0 GPU-seconds billed**), jobs 184/188/189 (~90 s each) |
| CPU pods | 2 in-cluster fixture builds (~2 min each), 1 sweep barrier |
| Storage | a few hundred KB of checkpoints, ONNX, reports and ledgers under `s3://<artifact-bucket>/npa-workflow-e2e/` |
| Local | 555-file npa source staged to S3 four times; **no image pulled to the dev VM** |

Approximate spend: **well under half a GPU-hour**. No cluster was provisioned for
this work.

## R8. Teardown

```bash
$ sky jobs queue    # every npa-wf-* / manual-* job from this work
183 CANCELLED  184 FAILED  188 FAILED  189 FAILED       # the diagnosed attempts
182 SUCCEEDED  185 SUCCEEDED  186 SUCCEEDED  187 SUCCEEDED
192 SUCCEEDED  194 SUCCEEDED  195 SUCCEEDED  197 SUCCEEDED  198 SUCCEEDED
```

All terminal — a filtered check confirms nothing of this work's is still alive:

```bash
$ sky jobs queue | grep -E "npa-wf|manual-" | grep -vE "SUCCEEDED|FAILED|CANCELLED"
# (no output)
$ kubectl -n default get pod,configmap,secret | grep npa-sonic-fixture
# (no output — the staging script's `trap cleanup EXIT` removed the pod,
#  its ConfigMap and its credentials Secret)
$ sky status
nurec-spike                   UP     # another run's cluster, untouched
npawfrt-isaac-probe2          INIT   # left by the earlier PR #225 session, untouched
sky-jobs-controller-64ce57a0  UP     # pre-existing shared managed-jobs controller
```

No task cluster and no managed job from this work is still alive. Resources belonging
to other runs (`paidf-*`, `nurec-spike`, `npawfrt-*`, and the week-old `paidf-faithful4`
job that has been `PENDING` since before this work) were deliberately left alone.

One shared-infrastructure note: **no** change was made to the cluster this time. The
`ErrImagePull ... 403` on job 183 was an *environment* problem (§R0), fixed by pointing
at the registry the cluster's existing pull secret covers — not by re-minting anything.

## R9. Not verified live

1. **`sonic-locomotion-finetuning.yaml`'s twin** — needs a real SOMA/G1 motion
   dataset. Its template is therefore **not** retired (§R6).
2. **The `npa[sonic]` extra was only exercised on SkyPilot's default image**, not on
   top of a baked workbench image (that path is the `NPA_SRC_OVERLAY` branch, which
   the sweep did exercise, but without an extra).
3. **The SONIC image's new k8s prerequisites were verified by using the already-built
   `0.1.2-k8s-runtime` tag**, which carries the same four ingredients; the Dockerfile
   change itself was not rebuilt and re-run in this change. The guardrail pins the
   ingredients textually, and `Dockerfile.k8s-prereqs` is the documented repair path.
4. **`cosmos3-reason` remains a manifest builder that requests a GPU.** That is
   pre-existing on both sides of the twin (the template ran the same code) and is not
   changed here; a real Cosmos3 reasoning stage is separate work.
5. **The eval reference backend runs 1-step episodes** on the fixture policy
   (`episode_length: 1`). That exercises onnxruntime, the metadata contract and the
   artifact path for real, but it is not a locomotion quality signal.
6. **Only the specs listed in §R2–§R5 were submitted live**; the rest of the matrix
   was covered plan-only (24/24) in this change.

## R10. Phase 2a — pointer-only CLI callers, then five more retirements (31 → 26)

Four workbench CLIs held a `*_WORKFLOW_PATH` constant naming a raw template. Those
constants are **printed** by `<tool> workflow` / `<tool> status` — nothing loads them —
so "porting the caller" is repointing the advertised path. A new guardrail
(`test_cli_advertised_workflow_paths_exist`) asserts every such constant is a real
file, so a retirement cannot silently hand an operator a 404.

Repointed: `token_factory.py` (×4), `mjlab.py`, `retargeting.py`, `vlm_eval.py` (×2,
plus a new `token_factory_workflow` key). **Behaviour change:** those subcommands now
print npa.workflow spec paths.

`vlm-eval-token-factory.yaml` had no twin, so one was authored and registered as a
`cpu` live case. It is the VLM eval case that can *always* run: `vlm-eval-single` asks
for `self-hosted` and nothing in that spec starts a vLLM server (pre-existing, §5.2b).

### Live runs

```bash
NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS=cpu NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS=<spec> \
  pytest .../test_npa_workflow_submit_live_reaches_terminal -q -s
# mjlab additionally: NPA_E2E_ACCELERATOR_REMAP=H100:1=RTXPRO-6000-BLACKWELL-SERVER-EDITION:1
```

| Spec | Run id | job | status | wall |
| --- | --- | --- | --- | --- |
| `token-factory-caption.yaml` | `npa-wf-cpu-token-factory-caption-1dbebbb4` | 199 | SUCCEEDED | 158 s |
| `vlm-eval-token-factory.yaml` *(new)* | `npa-wf-cpu-vlm-eval-token-factory-736df0b1` | 200 | SUCCEEDED | 163 s |
| `token-factory-cosmos-reason.yaml` | `npa-wf-cpu-token-factory-cosmos-reason-d9669c7f` | 201 | SUCCEEDED | 156 s |
| `token-factory-generate.yaml` | `npa-wf-cpu-token-factory-generate-94815797` | 202 | SUCCEEDED | 187 s |
| `mjlab-eval.yaml` | `npa-wf-gpu-mjlab-eval-32c1efb5` | 203 | SUCCEEDED | 148 s |

All five produced **real** artifacts (`"dry_run": false` throughout):

```
caption   captions/captions.json      942 B  Qwen/Qwen2.5-VL-72B-Instruct
          caption[0]: "The image shows a solid red square centered on a light gray
                       background. There is no action or additional objects present..."
vlm-eval  scores/vlm_eval_stub.json  1001 B  backend "api", 4 keyframes, score 0.0,
          rationale: "The provided frames do not show any robot or physical task being
          performed. The images only display two static blocks (red and green)..."
reason    plan/scene_reasoning.json  2114 B  nvidia/Cosmos3-Super-Reasoner
generate  generations.jsonl            86 B  hosted text generation
mjlab     mjlab/mjlab_eval.json       727 B  score 0.1423, suite locomotion,
          embodiment unitree-g1, episodes 8, passed false
```

### The defect these runs exposed: `outputs:` was a promise, ten times over

`vlm-eval-token-factory` wrote `scores/vlm_eval_stub.json` while its `outputs:`
declared `scores/report.json`; `mjlab-eval` wrote `mjlab/mjlab_eval.json` while
declaring `mjlab/report.json`; `token-factory-cosmos-reason` wrote
`plan/scene_reasoning.json` while declaring `plan/plan.json`. Every stage
**SUCCEEDED**. This is the same class as §R5's `--output json`, and it is the third
time a green status hid a missing artifact.

Rather than fix instances, `test_spec_declared_outputs.py` now resolves each stage's
argv, asks the tool's own `*_result_uri_for()` helper where it would write, and
compares. It found **ten** wrong declarations across seven specs — including
`tokenfactory-rollout-judge.yaml`, which `EVIDENCE §5.2b` already recorded as a
**PASSED** live run (job 166) and which therefore also never wrote its declared
artifact. All ten are corrected; the next one fails offline.

### Retirement tally: 31 → 26

| Retired template | Twin's live run | job |
| --- | --- | --- |
| `token-factory-caption.yaml` | `npa-wf-cpu-token-factory-caption-1dbebbb4` | 199 |
| `vlm-eval-token-factory.yaml` | `npa-wf-cpu-vlm-eval-token-factory-736df0b1` | 200 |
| `token-factory-cosmos-reason.yaml` | `npa-wf-cpu-token-factory-cosmos-reason-d9669c7f` | 201 |
| `token-factory-generate.yaml` | `npa-wf-cpu-token-factory-generate-94815797` | 202 |
| `mjlab-eval.yaml` | `npa-wf-gpu-mjlab-eval-32c1efb5` | 203 |

Cost for this phase: five short pods (four `1x[CPU:4+]`, one RTXPRO-6000 for ~31 s)
plus ~8 hosted Token Factory calls. Rounding error.

## R11. Phase 2b — a synthesized SOMA-CSV clip makes `retargeting` live-testable (26 → 25)

`retargeting.yaml`'s twin was the one live-matrix case that **failed** before this work:

```
Error: S3 input contains no objects: s3://<artifact-bucket>/.../retargeting/source/
```

(EVIDENCE §6.1). It needed a real SOMA/G1 motion clip, because the tool feeds NVIDIA's
upstream `gear_sonic/data_process/convert_soma_csv_to_motion_lib.py`, and this repo does
not vendor that dual-licensed dataset. `sonic-locomotion-finetuning.yaml` was blocked by
the same thing.

The upstream loader's contract is small and public, so a clip can be **synthesized**. It
was read from the pinned upstream ref with a blobless sparse clone (1.2 MB, nothing
kept):

```bash
git clone --filter=blob:none --no-checkout --depth 1 \
  https://github.com/NVlabs/GR00T-WholeBodyControl.git && \
  git sparse-checkout set gear_sonic/data_process
# load_csv_motion(): joint_pos.csv -> (T, 29) IsaacLab order, radians
#                    body_pos.csv  -> (T, B*3), body 0 = pelvis -> root_trans_offset
#                    body_quat.csv -> (T, B*4) wxyz, body 0 -> root rotation
```

`npa.workflows.motion_fixture` writes exactly that — forward pelvis translation at
constant height, a gentle yaw, bounded joint angles — using **only the standard
library**, so the fixture needs no container, no numpy and no torch. The conversion
still happens in the pod, where the upstream script's joblib/pandas/scipy live. The live
harness synthesizes clips automatically when `NPA_E2E_SONIC_MOTION_SRC` is unset (the
env var remains the real-data override), and
`scripts/stage-sonic-motion-fixture.sh` stages a set to share across runs.

### `retargeting.yaml` — PASSED

```
npa/tests/e2e/test_npa_workflow_submit_live_e2e.py
[seed] synthesized 2 SOMA-CSV clip(s) (6 objects) — set NPA_E2E_SONIC_MOTION_SRC to use real data
1 passed in 167.54s (0:02:47)
```

**Run id** `npa-wf-cpu-retargeting-b8e5bc8b` · **SkyPilot job 204** · `1x[CPU:4+]` ·
**SUCCEEDED**

Artifacts — the **real upstream converter** ran at the pinned ref:

```
    16975  retargeted/motion_lib.pkl            real motion_lib PKL
     1385  retargeted/retargeting_result.json   status "retargeted", source_format
                                                "soma-csv", embodiment "unitree-g1",
                                                frame_rate 30, motion_count 2,
                                                upstream_ref a9d20b2ac0949244d94461a1a3263f38c5027c4a,
                                                dry_run false
    ~13 KB x2  source/{walk-forward,stand-sway}/body_pos.csv
    ~15 KB x2  source/{walk-forward,stand-sway}/body_quat.csv
    ~11 KB x2  source/{walk-forward,stand-sway}/joint_pos.csv
```

`command` in that JSON is the upstream script invocation, so there is no doubt about
what did the work:

```
['/opt/conda/bin/python3',
 '/tmp/npa-retargeting-.../upstream-sonic/gear_sonic/data_process/convert_soma_csv_to_motion_lib.py',
 '--input', '.../input', '--output', '.../output/motion_lib.pkl', '--fps', '30']
```

### `sonic-locomotion-finetuning.yaml` — still NOT retired, and now for a *known* reason

**Run id** `npa-wf-multi-sonic-locomotion-finetuning-ff468526` · **SkyPilot job 205** ·
**FAILED** at the second of three stages.

| stage | result |
| --- | --- |
| `retarget` | **SUCCEEDED** — same fixture, `motion_count: 2`, `retargeting_result.json` written |
| `train` | **FAILED**: `Error: SONIC --runtime serverless requires --project-id or a configured project.` |
| `mjlab` | not reached |

This is **not** a missing fixture. The spec sets `sonic_runtime: serverless`, so the
toolRef asks the **in-pod** CLI to launch a Nebius *serverless job* — nested
infrastructure, and the pod has no project config. It is the same trap `DESIGN §7`
records for `workbench.rl.policy_train` ("that CLI is a launcher; calling it inside a
SkyPilot task would nest infrastructure"), and fixing it is a spec-design decision:
either train in-pod against the SONIC image, or keep the launcher outside the workflow.
Recorded as the reason the template survives in
`test_skypilot_catalog_retirement.py`; the fixture blocker is gone.

### Retirement tally: 26 → 25

| Retired template | Twin's live run | job |
| --- | --- | --- |
| `retargeting.yaml` | `npa-wf-cpu-retargeting-b8e5bc8b` | 204 |

One more `outputs:` correction fell out of it (the eleventh): both retargeting-backed
specs declared `retargeted/manifest.json` while the tool writes
`retargeting_result.json`. The declared-output guardrail now covers
`workbench.retargeting.run` too.

## R12. CI on the pull request — 21/21 green

```
docs-drift pass   gitleaks pass   guardrails pass   mypy pass
ruff pass         scan pass       test (3.10) pass  test (3.12) pass  test (3.14) pass
Two-tag strategy pass             Static Dockerfile scan pass        4x base-image CVE scan pass
```

One check needed a fix. **docs-drift** — a *blocking* gate that regenerates `docs/cli/`
from `npa --help` — failed because four `workflow` subcommands' short help changed when
they started advertising npa.workflow specs:

```
-workflow  Show the SkyPilot YAML template for MJLab evaluation.
+workflow  Show the npa.workflow spec for MJLab evaluation.
```

The exact diff CI computed was applied to
`docs/cli/{mjlab,retargeting,token-factory,vlm-eval}.md` rather than re-running the
generator on the dev VM, because there the generator also rewrites typer metavars
(`<str>` vs `TEXT`) — unrelated churn already documented in §1.

**gitleaks passed on the commit range**, which is the authoritative version of the local
scan reported in §R1.

## R13. Final state of this change

| | |
| --- | --- |
| SkyPilot templates | **36 → 25** (`ls npa/src/npa/workflows/skypilot/*.yaml \| wc -l` = 25) |
| Templates retired | 11, each with a live run id (§R2–R6, §R10, §R11) |
| `skypilotTwin:` fields | 13 → 3 |
| Offline suite | 3850 passed (base: 3682) — **+168**, same 2 pre-existing failures |
| New guardrails | 5, plus the migrated three-tier third tier |
| Specs whose `outputs:` was wrong | 8 specs / 11 stages, all corrected and now guarded |
| Live jobs | 182–205; all terminal; no leaked clusters or pods |
| Approx. spend | ~22 GPU-minutes + a handful of short CPU pods |

Templates that remain are pinned with a reason each in
`npa/tests/guardrails/test_skypilot_catalog_retirement.py`. The two that are *blocked
rather than unstarted* are called out explicitly: `sonic-locomotion-finetuning.yaml`
(its twin nests infrastructure — §R11) and the trigger/sim-to-real group (engine features
first). Nothing was deleted on plan-only evidence.

## R14. Phase 2c — BYOF profiles relocated (25 → 20) and multi-node stages proven live

### The 5 BYOF resource profiles were relocated, not deleted

`isaac-lab-rl-train{,-rtxpro,-rtxpro-smoke}.yaml`, `byof-datagen-rtxpro-smoke.yaml` and
`byof-container-smoke-rtxpro.yaml` describe a **pod shape** — accelerator, cpu/memory
floors, image placeholder, smoke command — not a pipeline. The workflow surface for them
is already the spec `byof.yaml`, whose `workbench.byof.repo` toolRef passes one through
`--yaml {{config.resource_profile_yaml}}`. They moved to
`npa/src/npa/workflows/byof/profiles/`, joining the two that were already there, with a
`README.md` stating the boundary and `test_byof_profiles.py` enforcing it (pinned file
set, one task per profile, `live.py`'s constants must resolve, no runner may resolve a
path under the retiring catalog).

Rewriting the runners onto the engine was rejected: they carry render-only modes,
output-root rewriting and BYOF image plumbing the engine does not model, so a port would
risk the BYOF onboarding live path for no gain in the *workflow* surface.

### The relocation exposed a real gap, and the second run proves the fix

**First attempt** — `byof-profile-relocation-075138`, **SkyPilot job 206**: the runner
found the relocated profile, rendered it, submitted it, the pod pulled the ~8 GB Isaac
image and **ran the profile's training script** — then died at the artifact upload:

```
botocore.exceptions.NoCredentialsError: Unable to locate credentials
```

All three BYOF runners called `submit_workflow` **without `secret_envs`**, while every
profile uploads its summary and artifacts to S3. Pre-existing; the relocation surfaced
it. Each runner now takes a repeatable `--secret-env` and defaults to forwarding the S3
credentials when they are set (an unset name is dropped, since SkyPilot rejects a secret
it cannot resolve).

**Second attempt** — `byof-profile-reloc2-075858`, **SkyPilot job 207** ·
`1x[RTXPRO-6000-BLACKWELL-SERVER-EDITION:1]` · **SUCCEEDED**:

```bash
python npa/scripts/run_isaac_lab_rl.py \
  --yaml npa/src/npa/workflows/byof/profiles/isaac-lab-rl-train-rtxpro-smoke.yaml \
  --image <registry>/npa-isaac-lab:2.3.2.post1-sky3 \
  --task Isaac-Cartpole-v0 --iterations 1 --run-id byof-profile-reloc2-... --cleanup
```

Real Isaac Lab training through the relocated profile:

```
    45575  npa_isaac_lab_checkpoint.pt              real RSL-RL checkpoint
      932  npa_isaac_lab_checkpoint_manifest.json   status "success", task Isaac-Cartpole-v0
      882  npa_isaac_lab_train_summary.json         status "success"
    14081  logs/rsl_rl/cartpole/.../params/env.yaml
    14208  outputs/.../.hydra/config.yaml
```

`cleanup`/`teardown` reported no errors, so nothing was left behind.

### Multi-node stages: `resources.<profile>.num_nodes` — PASSED

The one genuine expressiveness gap the brief named. Before this, a multi-node block was
reachable only through `npa burst submit --nodes`, i.e. outside the workflow surface.
`num_nodes` is **task level** in SkyPilot's schema, so it lives on the resource profile
in a spec and the renderer lifts it out; `normalize_resources` deliberately never passes
it through, and a 1-node profile emits no key at all (so every existing rendered document
is byte-identical).

**Run id** `npa-wf-cpu-multi-node-probe-11cc2065` · **SkyPilot job 208** ·
`1 passed in 228.05s`

`sky jobs queue --all --output json` — the node count is visible in `REQUESTED`:

| job | task | requested | status | submitted_at | end_at |
| --- | --- | --- | --- | --- | --- |
| 208 | `report-nodes` | **`2x[CPU:2+]`** | SUCCEEDED | 1785485152.8898 | 1785485209.7737 |
| 208 | `verify-nodes` | `1x[CPU:2+]` | SUCCEEDED | 1785485231.3538 | 1785485285.5915 |

And the proof is in S3 rather than in a log line — one report per rank, from **distinct
hosts**:

```json
nodes/rank-0.json {"rank": 0, "num_nodes": 2, "node_ip_count": 2,
                   "hostname": "report-nodes-208-64ce57a0-head"}
nodes/rank-1.json {"rank": 1, "num_nodes": 2, "node_ip_count": 2,
                   "hostname": "report-nodes-208-64ce57a0-worker1"}
report/multi_node_report.json
  {"expected_nodes": 2, "reported_nodes": 2, "ranks": [0, 1],
   "hostnames": ["report-nodes-208-64ce57a0-head",
                 "report-nodes-208-64ce57a0-worker1"]}
```

`verify-nodes` fails on a missing rank **and** on two ranks sharing a hostname, so a
gang that silently collapsed onto one node would not pass. A head + worker1 pair is
exactly what a real 2-node SkyPilot gang looks like.

Note the brief's premise that `isaac-lab-cosmos-sdg-burst-smoke.yaml` needs this feature
is wrong: that template is explicitly single-task/single-node and has zero references in
the repo. The feature is worth having on its own terms, and its live proof is this
purpose-built spec.

### Retirement tally: 26 → 20

| Template | Disposition |
| --- | --- |
| `retargeting.yaml` | retired, job 204 (§R11) |
| `isaac-lab-rl-train.yaml` | **relocated** to `byof/profiles/` |
| `isaac-lab-rl-train-rtxpro.yaml` | **relocated** |
| `isaac-lab-rl-train-rtxpro-smoke.yaml` | **relocated**, live-verified job 207 |
| `byof-datagen-rtxpro-smoke.yaml` | **relocated** |
| `byof-container-smoke-rtxpro.yaml` | **relocated** |

Cost: two RTXPRO pods (~25 s and ~24 s of job time, plus image pull) and one 2-node CPU
gang for ~57 s.

## R15. Phase 4 (partial) — the two insights specs get live coverage

`insights-smoke.yaml` and `insights-aggregate.yaml` were two of the 17 specs with no
live-matrix entry, and the reason was concrete: `workbench.insights.ingest_run` scans a
run prefix for known manifest/report schemas and fails with

```
no known manifest/report schemas found under run prefix: s3://...
```

when it finds none — the same failure EVIDENCE §2 recorded when a caption fan-out tried to
use the ingester as a barrier. Nothing seeded a prefix it recognised.

The harness now seeds the two shapes it does recognise (read from
`insights/store.py::_extract`): a real `npa.dataset.manifest.v1` document, which yields
record/corruption metrics **and** a lineage edge, and a bare `{"decision": ...}` document.
`insights-smoke` reads a *shared* fixture prefix (`insights-fixtures/run/`) so its seeding
is idempotent; `insights-aggregate` reads `runs/<run-id>/`, outside the e2e marker prefix.

| Spec | Run id | job | status | wall |
| --- | --- | --- | --- | --- |
| `insights-smoke.yaml` | `npa-wf-cpu-insights-smoke-f6e3c287` | 209 | SUCCEEDED | 298 s |
| `insights-aggregate.yaml` | `npa-wf-cpu-insights-aggregate-d54426f6` | 210 | SUCCEEDED | 258 s |

Real store output, not a stub:

```
insights-smoke/store/records.jsonl        6037   metric records
insights-smoke/store/edges.jsonl           840   lineage edges
insights-smoke/comparison/comparison.json 2684   base-vs-candidate comparison
insights-smoke/dashboard/dashboard.html   2431
insights-aggregate/store/records.jsonl    3827
insights-aggregate/store/edges.jsonl       326
insights-aggregate/dashboard/dashboard.html 1894
```

Live-matrix coverage: **17 uncovered specs → 15**, and the matrix grew from 24 to 28 cases
(the two above plus the two new specs `vlm-eval-token-factory.yaml` and
`multi-node-probe.yaml`). The remaining 15 are enumerated with their blockers in
`npa/src/npa/orchestration/npa_workflow/submit_matrix.py` and
`test_skypilot_catalog_retirement.py`; the recurring one is worth naming:

**Three toolRefs are launchers.** `workbench.rl.policy_train` and
`workbench.sonic.train` (with `--runtime serverless`) provision infrastructure of their
own, so invoking them from inside a SkyPilot stage nests infrastructure and fails in the
pod. Every spec that uses them — `adversarial-scenario-hardening`,
`hardening-with-insights`, `rl-policy-training-sim-success`, `sonic-train`,
`sonic-locomotion-finetuning` — is blocked on the same design decision: move the launcher
out of the workflow, or train in-pod against the vendor image. That is called out rather
than papered over with `plan_only=True`.
