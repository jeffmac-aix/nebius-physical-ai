# Changelog

Releases are git tags `vX.Y.Z` matching `npa/pyproject.toml`; artifacts are
built and attached by `.github/workflows/release.yml`. See `docs/releasing.md`
for the release process. Entries accumulate under "Unreleased" and move under
a versioned heading when a release is cut.

## Unreleased

### Retiring the raw SkyPilot task catalog (36 → 15 templates)

`npa.workflow/v0.0.1` specs are becoming the only workflow authoring surface.
SkyPilot remains the execution engine, and `npa workbench workflow submit` still
accepts a customer's own SkyPilot YAML — what is going away is the shipped catalog
under `npa/src/npa/workflows/skypilot/`.

- **Retired 14 templates**, each only after its spec reached a terminal `SUCCEEDED` on
  real infrastructure (run ids in `EVIDENCE.md` §R2–R6, §R10, §R22): `cosmos3-reason.yaml`,
  `isaac-lab-rl-sweep.yaml`, `sonic-export.yaml`, `sonic-eval.yaml`,
  `sonic-export-eval.yaml`, `token-factory-caption.yaml`,
  `token-factory-generate.yaml`, `token-factory-cosmos-reason.yaml`,
  `vlm-eval-token-factory.yaml`, `mjlab-eval.yaml`, `retargeting.yaml`,
  `vlm-eval.yaml`, `vlm-eval-benchmark.yaml`, `sim-to-real-loop.yaml`,
  `scenario-gen-adversarial.yaml`, `sim2real-envgen-split.yaml`.
  `test_skypilot_catalog_retirement.py` pins the remaining set, so the tally is
  machine-checked and a new raw template needs a deliberate edit.
- **Multi-node stages.** A resource profile can declare `num_nodes`, so a spec can ask
  for a real gang-scheduled block; previously that was only reachable through
  `npa burst submit --nodes`, outside the workflow surface. Additive: a 1-node profile
  renders exactly as before. Reference spec `npa-workflows/multi-node-probe.yaml`
  verifies one report per rank from distinct hosts.
- **BYOF resource profiles relocated** from `npa/src/npa/workflows/skypilot/` to
  `npa/src/npa/workflows/byof/profiles/` (they are pod shapes reached through
  `byof.yaml`, not workflow templates), and the three BYOF runner scripts gained
  `--secret-env`, defaulting to the S3 credentials their profiles need for uploads —
  without which a run provisioned, trained, and then died on `NoCredentialsError`.
- **Live-matrix coverage:** the two insights specs (`insights-smoke`,
  `insights-aggregate`) gained entries and now run live — the harness seeds the two
  artifact shapes `workbench.insights.ingest_run` recognises, which is what was
  missing; and the two dataset-of-record specs, for which
  `npa.workflows.dataset_fixture` generates raw sensor records satisfying both specs'
  quality gates. Uncovered specs: 17 -> 12; matrix cases: 24 -> 31.
  `scenario-gen-smoke` needed no fixture at all — its adversary backend is
  deterministic and GPU-free. `dataset-ingest-curate` is `plan_only` for a stated
  infrastructure reason (its `register` stage needs the LanceDB workbench service,
  which is not deployed); its other four stages did pass live.
- **New test fixture:** `npa.workflows.motion_fixture` +
  `scripts/stage-sonic-motion-fixture.sh` synthesize a valid SOMA-CSV G1 motion clip
  using only the standard library, so the retargeting-backed specs are live-testable
  without NVIDIA's dual-licensed motion dataset. `retargeting.yaml`'s live case was
  previously **failing** for lack of input data.
- **New spec:** `npa-workflows/vlm-eval-token-factory.yaml` — zero-GPU VLM scoring
  through the hosted `api` backend. This is the VLM eval path that needs no vLLM
  server, and it is registered in the live matrix as a `cpu` case.
- **`outputs:` declarations corrected in eight specs (eleven stages).** A stage can
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
- **`npa workbench vlm-eval loop`** — score every rollout under a prefix and write the
  aggregate `task_success_report.json` the sim-to-real loop gates on. `vlm-eval run`
  scores *one* rollout (it discovers frames recursively), so this capability existed only
  as ~80 lines of bash inside `sim-to-real-loop.yaml` and, separately, as Python inside a
  gated GPU test. The report is field-compatible with the template's, including the
  distinction that `task_success` gates on the **mean** score rather than the pass rate.
  New spec `npa-workflows/vlm-eval-loop.yaml`.
- **A self-hosted VLM stage now serves the model it calls.** `vlm_backend: self-hosted`
  makes the tool POST to localhost, and nothing in a spec started a server — the stage
  failed with `Connection refused`. The renderer gained a per-`toolRef` **run preamble**
  (the sibling of its setup hook; a background service cannot start in `setup:`, which
  SkyPilot runs in a different shell) that starts vLLM, health-checks `/health`, fails
  fast with the server log if it dies, and traps `EXIT` so no GPU-resident server leaks.
  It requires nothing of the task image: `ninja` and a CUDA compiler both come from pip,
  and the JIT-dependent sampler falls back to its pure-PyTorch equivalent.
  `config.vlm_serve_ready_seconds` (default 900 s) tunes the readiness window.
- **`detection-training train` gained `--wait` and `--label-map`** — the poll-until-done
  loop and the category map that `bdd100k-pipeline.yaml`'s template did in bash and that
  no spec could reach. `--wait` is opt-in; the BDD100K and AV night-scene specs use it, so
  their eval stages no longer race a checkpoint that does not exist yet.
- **`vlm-eval-benchmark.yaml`'s twin matched its template in name only:** it passed a
  **repo path** as `--dataset` (unresolvable in a pod) and ran the `stub` backend, so it
  never touched a VLM. Both fixed, and the repo-path class of bug is now machine-checked
  by `test_spec_paths_are_not_repo_relative.py`, which immediately found five `byof-*`
  specs doing the same thing. `resolve_byof_profile_path()` accepts a packaged profile
  **name**, so an installed wheel resolves what a checkout does.
- **`detection-training eval` gained `--discover-checkpoint` and
  `--write-canonical-metrics`**, and now fails on a non-numeric `mAP`. All three were bash
  and `jq` inside `bdd100k-pipeline.yaml`, so no spec could reach them: without discovery the
  eval stage scored the training *directory* instead of the checkpoint training wrote, and
  without the canonical write the BDD100K spec declared a `metrics.json` nothing produced.
- **`run_bdd100k_pipeline.py` renders the spec** (`--spec`, with `--yaml` kept as an alias)
  instead of injecting env vars into raw SkyPilot documents, and its `--mock-endpoints`
  validation now executes **each plan step's resolved argv** against stand-in services and
  checks the call *order* — every `POST /train` followed by `GET /status`, every `POST /eval`
  preceded by `GET /runs`. That drive immediately found two real defects: the
  `create_failure_views` toolRef passed `--table` to a command whose option is
  `--source-table` (so `curate-views` could never have run), and the eval prefixes lacked a
  trailing slash, so the declared artifact URI was
  `…/eval/bdd100k_rider_train` + `metrics.json` concatenated.
- **`workbench.sim2real_envgen.raw_shard` could never have run.** It omitted `--run-id`,
  which the module's parser requires, so every stage using it died on a usage error; three
  shipped specs referenced it. It was also handed the raw-env prefix where the module expects
  the **run root** (from which it derives `envs/raw`, `envs/train`, `envs/heldout`,
  `envs/manifest`), and the four specs using it declared a `manifest.json` that subcommand
  never writes. All fixed, plus a new `workbench.sim2real_envgen.split` toolRef.
- **New spec `sim2real-envgen-shards.yaml`** declares the shard fan-out the retired template
  drove from a Kubernetes Job completion index: a `parallel:` group whose members differ only
  through `params.shard_index`, with the split as a barrier. Live proof records
  `max_concurrent_observed: 2` and a split manifest that saw all 64 envs, 32 from each shard.
- **New guardrails** (none weakened): a catalog-wide check that every `toolRef` argv
  names real CLI options and passes values its options can mean — including the `npa …`
  commands **inside** a `bash -c` toolRef, a blind spot where a real defect had shipped;
  a check that a `python -m` toolRef argv parses against its module's own argparse parser,
  where a second one hid (a missing required `--run-id`); a
  check that no spec hands a stage a path inside the repo checkout; a check that the
  reference-workflows skill's template list matches the directory; the three-tier
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
