# Bounded agent benchmark

`npa agent benchmark` measures an OpenAI-compatible text model operating a real
NPA environment. It is an operator-side benchmark surface, not an arbitrary
shell agent: the model can call only a fixed catalog of NPA operations.

The current scenario takes `paidf-cosmos3.yaml` from environment inspection
through credential/access checks, offline validation and planning, additive
infrastructure verification, image preflight, one repository-generated seed
variant, durable status, and artifact inspection. The workflow continues to use
real Cosmos 3, Cosmos Evaluator, Cosmos Curator, FiftyOne Brain, and Rerun
components. The benchmark does not substitute mocks for workflow stages.

## Safety contract

- The provider URL must use HTTPS; certificate verification cannot be disabled.
- Provider keys are read from an environment variable or owner-controlled file
  and are never put in prompts, state summaries, reports, or command arguments.
- There is no shell tool. Every executor uses a fixed argv template for a normal
  NPA command, and the workflow path is restricted to the repository's
  `paidf-cosmos3.yaml`.
- `infra_provision`, `skypilot_bootstrap`, and `workflow_submit` are mutating.
  Each requires an explicit repeatable `--confirm-action` scope. At execution,
  NPA records the normalized action digest and the benchmark operation digest;
  an omitted or mismatched digest fails closed.
- Runtime submission passes secret *names* through `--secret-env`, never values.
- The seed fixture is repository-generated synthetic media. It contains no
  customer or private production data.
- Runtime submission uses `--max-wait-seconds 0`; the benchmark does not add a
  workflow deadline or cancel a healthy long-running stage.

## Run

Complete normal first-run configuration, health/EULA access, GPU selection, and
reserved-capacity setup first. Supply live identifiers only as runtime values:

```bash
npa agent benchmark \
  --project <project-alias> \
  --cluster <cluster-context> \
  --bucket <bucket> \
  --accelerator <requestable-accelerator>:1 \
  --endpoint https://<provider>/v1 \
  --model <model> \
  --api-key-file /owner-only/provider-key.txt \
  --state-path /owner-only/run/benchmark-state.json \
  --report-path /owner-only/run/benchmark-report.json \
  --confirm-action infra_provision \
  --confirm-action skypilot_bootstrap \
  --confirm-action workflow_submit \
  --output-format json
```

Both state and report files are written atomically with mode `0600`. The state
file is resumable and may contain exact operational identity; keep it outside
Git in access-controlled storage. The report replaces project, cluster, bucket,
and run identity with roles or hashes and is suitable for a sanitized handoff
after running the confidentiality scan.

Re-run the same command with the same state path after a process interruption.
The operation fingerprint prevents a state file from being reused for a
different project, cluster, bucket, spec, accelerator, seed posture, or secret
set. Durable NPA run identity remains fixed across resumes.

## Measurements

The `npa.agent.benchmark.v1` report includes:

- action-loop rounds, completed tools, errors, replans, and digest-bound
  authorization events;
- end-to-end and per-tool wall time;
- per-provider-call latency, observable time to first streamed token, output
  throughput, exact provider-reported prompt/completion/total tokens, retries,
  and errors;
- a representative high-context probe built from AGENTS, agent/Cosmos/PAIDF
  skills, the guide, and the real workflow spec, with file hashes and actual
  included character counts;
- a concurrent provider probe, reported separately from the sequential action
  loop;
- a deterministic read-only baseline using the same NPA executors, clearly
  marked `agentic: false`;
- sanitized workflow observations from status and artifacts, plus explicit
  stage-duration and GPU/resource-second measurements whenever those NPA
  responses expose them (otherwise marked `not_reported`).

The schema is [agent-benchmark-report.schema.json](agent-benchmark-report.schema.json).
No token or GPU price is inferred. When the provider or cloud path returns no
authoritative billing record, `cost.monetary` is `null` and measured token and
resource evidence remains available for later billing reconciliation.

## Teardown

The benchmark intentionally has no destroy tool. Cleanup remains an operator
lifecycle boundary: cancel the exact run, then remove the agent/controller,
cluster, storage, service account, and task-owned project through normal NPA
commands in the order documented in [teardown](../teardown.md). Audit every
resource class afterward.
