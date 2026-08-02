"""Live-submit matrix for npa.workflow twins.

Shared by e2e tests and the operator runner. SkyPilot-only exceptions (burst,
sim-to-real monolithic, etc.) are intentionally absent — see
``npa/workflows/workbench/npa-workflows/README.md``.

Parallel sweeps are no longer such an exception: ``isaac-lab-rl-sweep.yaml`` is an
``npa.workflow`` spec in this matrix, verified live on four GPUs, and the raw SkyPilot
template it was ported from has been retired.

The raw SkyPilot task catalog is being retired one live-verified twin at a time; the
remaining templates are pinned in
``npa/tests/guardrails/test_skypilot_catalog_retirement.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SubmitLiveCase:
    """One npa.workflow twin to submit live through SkyPilot."""

    spec: str
    tier: str  # cpu | gpu | multi
    secret_envs: tuple[str, ...] = ()
    requires_token_factory: bool = False
    plan_only: bool = False
    #: Skip this twin in the bounded daily GPU rotation because it cannot pass as
    #: a standalone submit today (needs a prior workflow's artifact, an input not
    #: staged into the job, or infra the npa.workflow render doesn't yet wire).
    #: The twin stays in the matrix for manual/plan runs; ``skip_reason`` explains
    #: the gap so it can be re-included once fixed.
    rotation_skip: bool = False
    skip_reason: str = ""
    notes: str = ""
    #: Submit through the runtime orchestrator (``submit --runtime``) instead of
    #: the one-shot serial path. Required for specs with a ``parallel:`` group or
    #: a loop that must early-exit on the real decision artifact.
    runtime: bool = False
    #: Config overrides applied at submit time (``--var k=v``), e.g. to drive a
    #: gate threshold in one live run.
    config_vars: tuple[tuple[str, str], ...] = ()
    #: Expected number of concurrent tasks in the spec's largest parallel wave
    #: (0 when the spec has no fan-out); asserted from the live job timeline.
    expected_parallel_tasks: int = 0
    #: Workbench tool whose image every task of this spec needs (resolved against
    #: the live registry at submit time). Set for specs whose stages run inside a
    #: baked image instead of the default SkyPilot image + staged npa source.
    image_tool: str = ""
    #: Per-wave deadline for this case, in seconds. 0 = use
    #: ``NPA_E2E_NPA_WORKFLOW_SUBMIT_MAX_WAIT_SECONDS``. Set it when one case is
    #: much slower than the rest (a 8 GB image pull plus GPU training) so the whole
    #: runtime tier does not have to run with the slowest case's deadline.
    max_wait_seconds: int = 0


SUBMIT_LIVE_MATRIX: tuple[SubmitLiveCase, ...] = (
    # --- CPU / zero-GPU (Token Factory hosted) ---
    SubmitLiveCase(
        "token-factory-caption.yaml",
        "cpu",
        secret_envs=("NEBIUS_TOKEN_FACTORY_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        requires_token_factory=True,
        notes="Cheapest live path; validates render→submit without a GPU.",
    ),
    SubmitLiveCase(
        "token-factory-generate.yaml",
        "cpu",
        secret_envs=("NEBIUS_TOKEN_FACTORY_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        requires_token_factory=True,
    ),
    SubmitLiveCase(
        "token-factory-cosmos-reason.yaml",
        "cpu",
        secret_envs=("NEBIUS_TOKEN_FACTORY_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        requires_token_factory=True,
    ),
    SubmitLiveCase(
        "token-factory-parallel-fanout.yaml",
        "cpu",
        secret_envs=("NEBIUS_TOKEN_FACTORY_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        requires_token_factory=True,
        runtime=True,
        expected_parallel_tasks=3,
        notes=(
            "Cheapest live PARALLEL path: three caption shards launch as one "
            "SkyPilot JobGroup, then an insights barrier. Needs --runtime."
        ),
    ),
    SubmitLiveCase(
        "token-factory-gate-loop.yaml",
        "cpu",
        secret_envs=("NEBIUS_TOKEN_FACTORY_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        requires_token_factory=True,
        runtime=True,
        config_vars=(("grade_threshold", "0.0"),),
        notes=(
            "Cheapest live RUNTIME-GATE path: the loop reads the real decision "
            "artifact and early-exits on iteration 1 with grade_threshold=0.0 "
            "(raise it above the achievable score to run the full budget)."
        ),
    ),
    SubmitLiveCase(
        "token-factory-trigger-watch.yaml",
        "cpu",
        secret_envs=("NEBIUS_TOKEN_FACTORY_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        requires_token_factory=True,
        runtime=True,
        notes=(
            "Trigger/watch reference: the driver polls the inbox prefix and only "
            "submits the stage once data lands. The live harness seeds the inbox "
            "AFTER the run starts, so the wait is real."
        ),
    ),
    SubmitLiveCase(
        "vlm-eval-token-factory.yaml",
        "cpu",
        secret_envs=(
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ),
        requires_token_factory=True,
        notes=(
            "Zero-GPU VLM eval through the hosted `api` backend. This is the VLM eval "
            "case that can always run: vlm-eval-single asks for `self-hosted`, and "
            "nothing in that spec starts a vLLM server (pre-existing gap)."
        ),
    ),
    SubmitLiveCase(
        "scenario-gen-smoke.yaml",
        "cpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes=(
            "Adversarial scenario mining + ranking on the default heuristic adversary "
            "backend, which is GPU-free and needs no seeded inputs: the policy/base-config "
            "URIs are recorded in lineage, not read. The rank stage consumes the manifest "
            "the generate stage wrote."
        ),
    ),
    SubmitLiveCase(
        "dataset-of-record-smoke.yaml",
        "cpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes=(
            "Dataset-of-record smoke: ingest -> validate -> quality gate -> curate -> "
            "query. CPU-only; the harness seeds real raw sensor records. Dynamic gate, "
            "so it is also in DYNAMIC_SPECS."
        ),
    ),
    SubmitLiveCase(
        "dataset-ingest-curate.yaml",
        "cpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes=(
            "Stricter dataset-of-record variant (completeness_min 0.5, max_corruption_rate "
            "0.1, location filter). Its `register` stage writes to the in-cluster LanceDB "
            "service at http://npa-lancedb.workbench.svc.cluster.local:8686 — deploy it with "
            "`npa workbench lancedb deploy --runtime kubernetes --namespace workbench`."
        ),
    ),
    SubmitLiveCase(
        "insights-smoke.yaml",
        "cpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes=(
            "Insights lineage + metrics store: ingest a run prefix, compare two runs, "
            "render a dashboard. CPU-only. The harness seeds a real dataset manifest and "
            "a decision artifact, the two shapes the ingester recognises."
        ),
    ),
    SubmitLiveCase(
        "insights-aggregate.yaml",
        "cpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes="Insights ingest + dashboard over one run prefix. CPU-only.",
    ),
    SubmitLiveCase(
        "cosmos3-text-to-image.yaml",
        "gpu",
        secret_envs=("HF_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        image_tool="cosmos3-reason",
        notes=(
            "Clones the Cosmos framework, syncs its uv environment, downloads Cosmos3-Nano and "
            "generates an image. Needs the Cosmos image rather than SkyPilot's default: "
            "transformer_engine links against glibc >= 2.32 (job 301), which no LD_LIBRARY_PATH "
            "can supply."
        ),
    ),
    SubmitLiveCase(
        "cosmos2-transfer.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN"),
        image_tool="cosmos2-transfer",
        notes=(
            "The REAL Cosmos-Transfer2.5 model, not a manifest: --execute makes a missing "
            "transfer runtime a hard error rather than a silent fall back. Replaces a template "
            "that held a GPU to print `\"status\": \"contract_ready\"`."
        ),
    ),
    SubmitLiveCase(
        "isaac-franka-capture-reason.yaml",
        "multi",
        secret_envs=(
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ),
        requires_token_factory=True,
        image_tool="isaac-lab",
        notes=(
            "Isaac Lab renders Franka frames on a GPU, then a hosted Cosmos3 reasoner plans "
            "from them on CPU. Needs no seeded input: the first stage produces the second's."
        ),
    ),
    SubmitLiveCase(
        "tokenfactory-scene-to-rollout-judge.yaml",
        "multi",
        secret_envs=(
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "HF_TOKEN",
        ),
        requires_token_factory=True,
        image_tool="lerobot",
        notes=(
            "Three stages, one chain: a hosted reasoner plans from a seeded scene, a GPU rolls "
            "out a policy, and a hosted VLM judges that rollout AGAINST THAT PLAN "
            "(`--task-from` reads the reasoner's artifact). Only the middle stage holds a GPU. "
            "Same LeRobot image requirement as the rollout-judge combo."
        ),
    ),
    SubmitLiveCase(
        "tokenfactory-rollout-judge-combo.yaml",
        "multi",
        secret_envs=(
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "HF_TOKEN",
        ),
        requires_token_factory=True,
        image_tool="lerobot",
        notes=(
            "The real rollout-judge twin: the GPU stage rolls out a public LeRobot policy in its "
            "own pod and publishes the rendered episodes, then a hosted VLM scores exactly that "
            "prefix with no GPU. Needs a hostable LeRobot image whose torch and torchcodec agree "
            "(NPA_E2E_IMAGE_OVERRIDE_LEROBOT=<registry>/npa-lerobot:0.6.0-k8s-runtime)."
        ),
    ),
    SubmitLiveCase(
        "tokenfactory-train-triage.yaml",
        "multi",
        secret_envs=(
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "HF_TOKEN",
        ),
        requires_token_factory=True,
        image_tool="lerobot",
        notes=(
            "The producer/consumer combo: LeRobot trains in the stage's own pod (the vendor "
            "image's LeRobot, one step) and publishes the run's checkpoint AND textual "
            "artifacts, then a hosted text model triages that run with no GPU. The train stage "
            "materialises its own dataset from `--dataset-repo-id`, because stages do not share "
            "a filesystem. Requires a SkyPilot-hostable LeRobot image AND one whose torch and "
            "torchcodec agree: run with "
            "NPA_E2E_IMAGE_OVERRIDE_LEROBOT=<registry>/npa-lerobot:0.6.0-k8s-runtime. The 0.5.1 "
            "image fails at training step 0 with a torchcodec ABI mismatch. Six live iterations "
            "and five engine gaps to get here - see EVIDENCE.md \u00a7R32-R33."
        ),
    ),
    SubmitLiveCase(
        "cosmos-fetch.yaml",
        "cpu",
        # setup stages the npa source from S3 with boto3, so the keys are needed even
        # though nothing in this plan touches object storage.
        secret_envs=("HF_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        config_vars=(
            # The spec's defaults name Cosmos3 assets that are gated behind early access and
            # a licence acceptance. Substituting public ones exercises the identical code
            # path — a real git clone and a real Hugging Face download into the cache — which
            # is what a live run of this twin is meant to prove.
            ("cosmos_source_repo", "https://github.com/githubtraining/hellogitworld.git"),
            ("cosmos_model_id", "hf-internal-testing/tiny-random-gpt2"),
        ),
        notes=(
            "Cosmos access check then fetch. CPU. Run with public substitutes for the gated "
            "Cosmos3 source repo and checkpoint; the commands, flags and cache layout are "
            "the same ones the retired template invoked."
        ),
    ),
    SubmitLiveCase(
        "sim2real-envgen-shards.yaml",
        "multi",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        runtime=True,
        expected_parallel_tasks=2,
        notes=(
            "Shard fan-out: two raw env shards as one JobGroup, then a barrier that splits "
            "the combined catalog 80/20. Replaces a template that read its shard index from "
            "a Kubernetes Job completion index. CPU — generation writes env descriptors, it "
            "does not render. Needs the runtime tier so the group really is concurrent."
        ),
    ),
    SubmitLiveCase(
        "multi-node-probe.yaml",
        "cpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes=(
            "Multi-node reference: `resources.gang.num_nodes` gang-schedules a real "
            "2-node stage, then a single-node stage verifies one report per rank landed "
            "on a distinct host. CPU on purpose — the property is the node count."
        ),
    ),
    SubmitLiveCase(
        "retargeting.yaml",
        "cpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes="CPU resources in spec; still needs cluster image pull.",
    ),
    # --- Single-tool GPU ---
    SubmitLiveCase(
        "vlm-eval-single.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        rotation_skip=True,
        skip_reason=(
            "vlm_backend=self-hosted. The render now DOES start a background "
            "vLLM server and the client waits up to NPA_VLM_READY_TIMEOUT_S "
            "(=1800s, set by the render) for readiness. Confirmed live on "
            "RTXPRO-6000 (Blackwell/sm_120): the cold start — heavy vLLM+CUDA13 "
            "install, ~16GB Qwen2-VL-7B weight download, and first-run compile — "
            "exceeds even the 30-min window, so it is not a fit for the bounded "
            "daily rotation. Re-include with the model pre-baked/pre-cached into "
            "the image or a faster node. (The old instant connection-refused is "
            "fixed; failure is now a clean, bounded 'not ready' diagnostic.)"
        ),
        notes="Self-hosted VLM; render starts vLLM, but cold start > 30min on this node.",
    ),
    SubmitLiveCase(
        "vlm-eval-benchmark.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes=(
            "Labeled sweep on the self-hosted backend, like the template it replaces. The "
            "harness seeds two rollouts with known outcomes plus an S3 benchmark manifest."
        ),
    ),
    SubmitLiveCase(
        "vlm-eval-loop.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        notes=(
            "Rollout-SET scoring plus the aggregate task_success report. `run` scores one "
            "rollout, so this is the capability that let sim-to-real-loop.yaml retire: the "
            "harness seeds several rollout directories and the report must count them all."
        ),
    ),
    SubmitLiveCase(
        "mjlab-eval.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN"),
    ),
    SubmitLiveCase(
        "sonic-train.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN", "NGC_API_KEY"),
        # The stage runs the SONIC image's own /entrypoint.sh, so it needs that image. Live job
        # 322 proved the in-job runtime works by reporting exactly what was missing:
        # "/entrypoint.sh not found in SONIC image" on SkyPilot's default one.
        image_tool="sonic",
        rotation_skip=True,
        skip_reason=(
            "The launcher problem is fixed — `sonic train --runtime in-job` trains in the pod "
            "instead of provisioning a Nebius Job from inside it (EVIDENCE.md \u00a7R42) — but a "
            "real SONIC training run is a long GPU job, not a bounded daily-rotation fit. Run "
            "it manually."
        ),
    ),
    SubmitLiveCase(
        "sonic-export.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN", "NGC_API_KEY"),
    ),
    SubmitLiveCase(
        "sonic-eval.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN", "NGC_API_KEY"),
    ),
    SubmitLiveCase(
        "cosmos3-reason.yaml",
        "gpu",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN"),
    ),
    SubmitLiveCase(
        "tokenfactory-rollout-judge.yaml",
        "gpu",
        secret_envs=(
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ),
        requires_token_factory=True,
    ),
    # --- Multi-stage GPU ---
    SubmitLiveCase(
        "sonic-export-eval.yaml",
        "multi",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN", "NGC_API_KEY"),
    ),
    SubmitLiveCase(
        "sonic-locomotion-finetuning.yaml",
        "multi",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN", "NGC_API_KEY"),
        rotation_skip=True,
        skip_reason=(
            "retarget → train → mjlab: hits the same SONIC train in-job runtime "
            "gap as sonic-train, and needs a staged motion source. Re-include "
            "once SONIC train has an in-job runtime and inputs are staged."
        ),
        notes="retarget → train → mjlab",
    ),
    SubmitLiveCase(
        "isaac-lab-rl-sweep.yaml",
        "multi",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        runtime=True,
        expected_parallel_tasks=4,
        image_tool="isaac-lab",
        # The Isaac Lab image is ~8 GB per node and the variants train on GPU.
        max_wait_seconds=5400,
        # Cost control for the live tier: hold two GPUs at a time instead of four.
        # This also exercises the multi-batch path (4 members / maxConcurrency 2).
        config_vars=(("max_concurrency", "2"),),
        notes=(
            "Parallel GPU reference case (port of the execution:parallel SkyPilot "
            "template): four RSL-RL variants as one JobGroup + ranking barrier. "
            "Needs --runtime and the Isaac Lab image (run branch code on top with "
            "NPA_SRC_OVERLAY=1); cap GPUs with --var max_concurrency=N."
        ),
    ),
    SubmitLiveCase(
        "bdd100k-pipeline.yaml",
        "multi",
        secret_envs=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        rotation_skip=True,
        skip_reason=(
            "11-stage AV pipeline and the longest wall-clock twin; not a bounded daily-rotation "
            "fit. Run it manually with NPA_E2E_BDD100K_SYNTHETIC_ROWS to skip staging the real "
            "dataset, and the LanceDB service deployed "
            "(`npa workbench lancedb deploy --runtime kubernetes --namespace workbench`)."
        ),
        notes="11-stage AV pipeline; longest wall-clock.",
    ),
    SubmitLiveCase(
        "tokenfactory-cosmos-gate.yaml",
        "multi",
        secret_envs=(
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
                    "HF_TOKEN",
        ),
        requires_token_factory=True,
        rotation_skip=True,
        skip_reason=(
            "Confirmed live: FAILED — dynamic Cosmos gate loop needs a staged "
            "scene input and --assume-decision; not runnable as a bounded "
            "standalone rotation submit. Run manually with a staged scene."
        ),
        notes="Dynamic gate; needs --assume-decision.",
    ),
    # --- Plan-only / stub twins (do not burn GPUs on stubs) ---
    SubmitLiveCase(
        "physical-ai-data-factory.yaml",
        "multi",
        secret_envs=(
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
                    "HF_TOKEN",
        ),
        requires_token_factory=True,
        plan_only=True,
        notes=(
            "Physical AI Data Factory blueprint. Dynamic gate (needs "
            "--assume-decision). All stages are real (augment = cosmos2."
            "transfer_execute on GPU; curate/finalize/grade = real run.shell). "
            "Plan-only in CI because a real Cosmos Transfer 2.5 run is heavy "
            "(gated-weight download + diffusion) and needs the npa-cosmos2-transfer "
            "image rebuilt from this branch; live render/submit-prep is validated "
            "without burning a GPU."
        ),
    ),
    SubmitLiveCase(
        "sim2real-vlm-rl.yaml",
        "multi",
        secret_envs=("HF_TOKEN",),
        plan_only=True,
        notes="Stub toolRefs; plan-only until engine wiring lands.",
    ),
    SubmitLiveCase(
        "byof.yaml",
        "multi",
        plan_only=True,
        notes="Delegates to run_byof_repo.py; covered by byof live e2e.",
    ),
    SubmitLiveCase(
        "rl-policy-training-sim-success.yaml",
        "multi",
        plan_only=True,
        notes="Partial Isaac twin; plan-only until Hydra parity.",
    ),
)


def selected_submit_cases() -> list[SubmitLiveCase]:
    """Filter SUBMIT_LIVE_MATRIX by env tier / spec allowlists."""

    tiers_raw = os.environ.get("NPA_E2E_NPA_WORKFLOW_SUBMIT_TIERS", "cpu,gpu,multi")
    tiers = {t.strip().lower() for t in tiers_raw.split(",") if t.strip()}
    specs_raw = os.environ.get("NPA_E2E_NPA_WORKFLOW_SUBMIT_SPECS", "")
    specs = {s.strip() for s in specs_raw.split(",") if s.strip()}
    return [
        case
        for case in SUBMIT_LIVE_MATRIX
        if case.tier in tiers and (not specs or case.spec in specs)
    ]


def gpu_submit_cases(
    *, include_plan_only: bool = False, include_skipped: bool = False
) -> list[SubmitLiveCase]:
    """Real-GPU-launching twins, sorted by spec for a deterministic rotation.

    Excludes ``plan_only`` stub twins (they never launch a GPU) and
    ``rotation_skip`` twins (they cannot pass as a standalone submit today — see
    each ``skip_reason``), unless asked, so the daily rotation only ever picks a
    case that actually exercises a GPU and can succeed on its own.
    """

    cases = [
        case
        for case in SUBMIT_LIVE_MATRIX
        if case.tier in {"gpu", "multi"}
        and (include_plan_only or not case.plan_only)
        and (include_skipped or not case.rotation_skip)
    ]
    return sorted(cases, key=lambda c: c.spec)


def rotating_gpu_submit_case(day_index: int) -> SubmitLiveCase | None:
    """Pick one real-GPU twin for ``day_index`` (round-robins over days).

    Lets the daily runner exercise a *different* real GPU workflow E2E each day
    at bounded cost (one managed job) instead of the whole ``gpu and e2e`` blast,
    cycling through every GPU twin over the rotation window.
    """

    cases = gpu_submit_cases()
    if not cases:
        return None
    return cases[day_index % len(cases)]


def runtime_submit_cases() -> list[SubmitLiveCase]:
    """Selected cases that must be driven by the runtime orchestrator.

    These are the specs with a ``parallel:`` group or a loop that has to
    early-exit on the real decision artifact; they are submitted with
    ``submit --runtime``.
    """

    return [case for case in selected_submit_cases() if case.runtime and not case.plan_only]


def one_shot_submit_cases() -> list[SubmitLiveCase]:
    """Selected cases for the classic one-shot submit path.

    Runtime cases are excluded on purpose: submitting them one-shot would render
    the flattened serial plan, which is valid but proves nothing about
    concurrency or early-exit — and would run a GPU sweep serially. They are
    still covered by the plan-only matrix and by the runtime live test.
    """

    return [case for case in selected_submit_cases() if not case.runtime]
