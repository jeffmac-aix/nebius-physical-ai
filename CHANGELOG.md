# Changelog

Releases are git tags `vX.Y.Z` matching `npa/pyproject.toml`; artifacts are
built and attached by `.github/workflows/release.yml`. See `docs/releasing.md`
for the release process. Entries accumulate under "Unreleased" and move under
a versioned heading when a release is cut.

## Unreleased

### Retiring the raw SkyPilot task catalog (36 → 26 templates)

`npa.workflow/v0.0.1` specs are becoming the only workflow authoring surface.
SkyPilot remains the execution engine, and `npa workbench workflow submit` still
accepts a customer's own SkyPilot YAML — what is going away is the shipped catalog
under `npa/src/npa/workflows/skypilot/`.

- **Retired 10 templates**, each only after its spec reached a terminal `SUCCEEDED` on
  real infrastructure (run ids in `EVIDENCE.md` §R2–R6, §R10): `cosmos3-reason.yaml`,
  `isaac-lab-rl-sweep.yaml`, `sonic-export.yaml`, `sonic-eval.yaml`,
  `sonic-export-eval.yaml`, `token-factory-caption.yaml`,
  `token-factory-generate.yaml`, `token-factory-cosmos-reason.yaml`,
  `vlm-eval-token-factory.yaml`, `mjlab-eval.yaml`.
  `test_skypilot_catalog_retirement.py` pins the remaining set, so the tally is
  machine-checked and a new raw template needs a deliberate edit.
- **New spec:** `npa-workflows/vlm-eval-token-factory.yaml` — zero-GPU VLM scoring
  through the hosted `api` backend. This is the VLM eval path that needs no vLLM
  server, and it is registered in the live matrix as a `cpu` case.
- **`outputs:` declarations corrected in seven specs (ten stages).** A stage can
  succeed while writing its result somewhere other than the URI the spec declares —
  `vlm-eval` writes `vlm_eval_stub.json`, `mjlab eval` writes `mjlab_eval.json`, the
  Cosmos reasoner writes `scene_reasoning.json`, and several specs declared
  `report.json` / `plan.json`. `test_spec_declared_outputs.py` now compares every
  stage's declared artifact against the tool's own `*_result_uri_for()` helper.
- `npa workbench {mjlab,retargeting,token-factory,vlm-eval} workflow|status` print
  npa.workflow spec paths instead of raw SkyPilot template paths, and
  `vlm-eval workflow|status` gain a `token_factory_workflow` key. A guardrail asserts
  every advertised path is a real file.
- **User-facing behaviour changes:**
  - `npa workbench sonic export` and `npa workbench sonic eval` now accept `s3://`
    URIs for `--checkpoint`, `--onnx`, `--obs-spec`, `--action-spec`, `--config` and
    `--output`, downloading and uploading as needed (including an ONNX's
    `<name>.onnx.data` external weights). Local paths behave exactly as before.
    `sonic eval` adds an `onnx_uri` field to its result when the input was an object
    URI.
  - `npa workbench isaac-lab train` is now invoked correctly by
    `workbench.rl.policy_train`: the toolRef passed `--learning-rate`, `--batch-size`
    and `--input-path`, none of which exist on that command. Trainer hyper-parameters
    go through Isaac Lab's repeatable Hydra `--override KEY=VALUE`, `batch_size`
    becomes the real `--num-envs`, and `--input-path` becomes `--data-path`. The three
    specs that use it rename their `batch_size` config key to `num_envs`.
  - `workbench.rl.evaluate_policy` passed `--episodes`; the CLI option is
    `--num-episodes`.
  - `workbench.sonic.eval` passed `--output json`, conflating the **result path** with
    the output format, so the eval result was written to a relative `json/` directory
    inside the pod and the artifact the spec declared never appeared. It now passes
    `--output <eval_uri> --output-format json`; `sonic-eval.yaml` and
    `sonic-export-eval.yaml` gain an `eval_uri` config key.
  - `solutions.toml`'s `sonic-locomotion-finetuning` solution now submits the
    npa.workflow spec instead of the raw template.
- **New guardrails** (none weakened): a catalog-wide check that every `toolRef` argv
  names real CLI options and passes values its options can mean; the three-tier
  contract's third tier moved from SkyPilot `envs` onto the spec + toolRef argv, with
  each contract pinning and *classifying* the parameters a spec cannot set yet; a
  live-matrix check that each case declares the secrets its plan hints at; and a
  `solutions.toml` check that every advertised `workflow submit <path>` exists.
- **Engine:** a `toolRef` can declare an npa extra (`TOOL_REF_PIP_EXTRAS`), installed
  from the same source tree npa came from, so a SONIC stage runs on SkyPilot's default
  image without a vendor image.
- **Images:** the SONIC Dockerfile gains the four SkyPilot-on-Kubernetes prerequisites
  the Isaac Lab image needed, plus a `Dockerfile.k8s-prereqs` for repairing a
  published tag in-cluster. The image guardrail now covers `sonic`.
- **Test fixtures:** `npa.workflows.sonic_fixture` + `scripts/stage-sonic-export-fixture.sh`
  build a real, tiny SONIC policy checkpoint **in-cluster**, so the SONIC twins are
  live-testable without NVIDIA's gated `GEAR-SONIC` weights.

### npa.workflow: real parallel execution and a runtime orchestrator

- **Parallel fan-out.** `npa.workflow/v0.0.1` specs can declare a `parallel:`
  group with an optional `maxConcurrency`. The group renders as a SkyPilot
  **JobGroup** (`execution: parallel`), so its members launch as genuinely
  concurrent jobs, and the group's `next` state is a barrier that starts only
  after every member reaches a terminal state. Groups larger than
  `maxConcurrency` are submitted in batches. Serial remains the default: the
  serial renderer and its guard are untouched, and `--plan-only` still renders
  the flattened serial plan for every spec.
- **`params:` per-state config overlay** so N members of a sweep can share one
  `toolRef` and still differ (learning rate, output prefix, ...).
- **Runtime orchestrator** (`npa workbench workflow submit --runtime`): submits
  each wave, polls it to a terminal state, reads the *real* decision artifact
  from S3 through the existing `decisions.py` contract, and replans — bounded
  loops with true early-exit, data-dependent `goto` branching, wave retry,
  timeout cancellation, and `--resume` on a durable ledger
  (`npa.workflow.runtime.v1` at `<prefix>/npa-workflow/runtime.json`). The
  plan-time `--assume-decision` path is unchanged and remains the offline mode.
- **`trigger:`** on a state makes the runtime driver wait for objects at an S3
  prefix before that state runs (watermark recorded in the ledger).
- **New CLI:** `submit --runtime/--resume/--poll-seconds/--max-wait-seconds/
  --retries/--max-concurrency/--cancel-on-timeout`, `plan-spec --waves`.
- **New specs:** `token-factory-parallel-fanout.yaml` (zero-GPU JobGroup + join
  barrier), `token-factory-gate-loop.yaml` (zero-GPU runtime gate loop with real
  early-exit and branch), `isaac-lab-rl-sweep.yaml` (port of the one
  `execution: parallel` SkyPilot template). All three are registered in
  `SUBMIT_LIVE_MATRIX` and covered by a live runtime e2e tier
  (`scripts/npa-workflow-runtime-live-e2e.sh`).
- Design: repo-root `DESIGN.md`; live evidence: repo-root `EVIDENCE.md`.

### First-time-user cold-start fixes

- `npa configure --interactive` no longer exits 0 having written nothing. When it
  cannot proceed (no authenticated Nebius CLI profile for provisioning) or is
  cancelled mid-flow (EOF/Ctrl-C), it now exits **non-zero** with actionable
  guidance. **Behavior change:** wrappers/CI that treated a cancelled or aborted
  `npa configure` as success will now see a failure. Setup guidance and the
  interactive prompts also link where to obtain the Hugging Face and NGC keys.
- Added `npa workbench health preflight`: a PASS/WARN/FAIL/SKIP check over
  Hugging Face, NVIDIA NGC, Nebius object storage (S3), and Token Factory
  credentials (`--checks`, `--offline`, `--warn-only`, `--json`). Replaces the
  deprecated hidden `npa workbench health sim2real` in the README preflight
  guidance.
- Added `npa agent preflight` and moved the terraform-binary and SSH-key-pair
  checks (plus the Token Factory 503 warning) ahead of any cloud IAM side effects
  in `npa agent deploy`, so Route C prerequisites fail fast instead of mid-run.

### Repo hardening

- Shipped SkyPilot examples and cookbooks now use the `<your-registry-id>`
  placeholder instead of the first-party registry ID; a guardrail test keeps
  concrete registry IDs out of shipped examples.
- The base `pip install npa` is now lightweight (offline paths only); heavy
  dependencies moved to `npa[data]`, `npa[lancedb]`, `npa[viz]`, with
  `npa[full]` covering the previous monolithic install. Over-narrow version
  pins were relaxed and the previously undeclared `pydantic` dependency is
  declared.
- Added `npa.__version__`, a tag-driven Release workflow that builds and
  attaches sdist/wheel artifacts, and `docs/releasing.md`.

### Cosmos e2e

- Validated Cosmos end-to-end on Nebius via serverless `train --smoke`.
  Run ID: `w13-cosmos-e2e-20260521T233523Z`. Output artifact:
  `s3://${NPA_S3_BUCKET}/w13-cosmos-e2e/w13-cosmos-e2e-20260521T233523Z/checkpoint.json`.
- Closes the 7/8 -> 8/8 Workbench tool verification matrix gap for the
  artifact-bearing Cosmos CLI workflow.
- Known constraints remain documented in `docs/testing/e2e-serverless.md`:
  NIM/Triton are not implemented, `finetune` is a placeholder, and deferred
  visual-generation/rendering paths still depend on the container EGL/DRI gap.

- Validated Isaac Lab bring-your-own-fork path: image override (Run ID:
  `w10-byof-image-only-20260520T232650Z`) and image+command override (Run ID:
  `w10-byof-image-and-cmd-20260520T233113Z`). Worked example at
  `docs/workbench/cookbooks/byof-isaac-lab/`. Checkpoint + sentinel:
  `s3://${NPA_S3_BUCKET}/checkpoints/isaac-lab-byof/w10-byof-image-and-cmd-20260520T233113Z/`.
- Fixed Isaac Lab train command construction to call the RSL-RL training script with `--num_envs` and `--max_iterations`; added SkyPilot single-job and parallel sweep YAMLs plus the Isaac Lab RL runner.
- Added BYOVM post-deploy SSH endpoint strategy persistence and transient SSH tunnel routing for live workbench commands; fixed GR00T S3 env injection/auditing, shortened BYOVM auto public health fallback, printed normal-deploy Hugging Face access status, suppressed successful FiftyOne readiness curl noise, and made template tests cwd-independent.
- Implemented demo pre-staging CLI fixes for shared credential injection, shell-safe and Docker-safe env files, BYOVM project storage inheritance, Hugging Face gated-model validation, BYOVM SSH health fallback, live status/readiness reporting, Cosmos progress output, GR00T gated-model fail-fast handling, FiftyOne video ingestion, deploy dry-runs, credential env audits, and cross-tool smoke-test scaffolding.
- Preserved Genesis BYOVM staging fixes with tests: EGL fallback for multi-GPU demo generation, Docker group/device access for Genesis containers, and BYOVM storage credential reuse.
- Added structured implementation prompts for the 14 NPA CLI demo pre-staging fixes.

## W9-W10 - Workbench maturity sequence

- fix(sonic): default serverless training to H100, not L40S (W12 condensed commit)
- feat(skypilot): `npa skypilot bootstrap/status/verify` with isolated venv
  pattern (W11 condensed commit)
- Isaac Lab SkyPilot orchestration validated end-to-end via BYOF runs
  (W10 condensed commit; see `docs/workbench/cookbooks/byof-isaac-lab/`)
- BYOF mechanism validated: image override and command override surfaces;
  worked example with verified S3 artifacts (run IDs in cookbook)
- Removed SONIC routing entry from `CONTRIBUTING.md` Known Deviations
