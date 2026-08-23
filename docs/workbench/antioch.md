# Antioch Workbench integration

The Antioch adapter is a CPU-only control-plane service. It stages immutable
projects from S3, submits scenarios or suites with Antioch's supported structured
CLI, reconciles retries, collects checks/logs/results/Rerun files, and optionally
publishes a strict LeRobotDataset v3 for offline policy training. Antioch executes
simulation on its managed infrastructure; this image contains no simulator.

## Authentication and runtime boundary

Install Antioch's CLI normally and authenticate once as the operator. Confirm the
existing session without printing identity data:

```bash
export NPA_ANTIOCH_ACCEPT_TERMS=YES
npa workbench antioch terms-preflight --output json
npa workbench antioch health --output json
```

`NPA_ANTIOCH_ACCEPT_TERMS=YES` is an exact, explicit attestation that the
operator reviewed the [Antioch Terms of Service](https://antioch.com/terms)
(version dated 2026-02-28) for the scoped use of `antioch-sim==0.3.63` and the
Antioch Service. Any customer MSA or order form remains controlling. Other
spellings fail closed. The adapter records only the agreement name, public URL,
version, scope, and accepted boolean in durable operation state; it never stores
the environment value in an image, cache, project, dataset, or credentials file.

Do not copy the Antioch config into an image. For Kubernetes, create a secret from
the existing config out of band and mount it read-only with `deploy
--antioch-config-secret`; create a separate secret whose `token` key protects the
adapter HTTP API. Create another runtime-only secret whose `accepted` key is the
exact value `YES`, and pass its name through `--terms-acceptance-secret`. The
deploy command prints secret *names*, never values.
Provide S3 credentials through a pre-created `--s3-credentials-secret`, or omit
that option when the pod workload identity supplies S3 access. The adapter's
self-contained resolver uses `--storage-endpoint` and leaves credentials to
boto's workload-identity chain; it does not import host-only NPA configuration
modules. The optional secret uses
the ordinary `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and
optional `AWS_SESSION_TOKEN` keys.

The public adapter pins `antioch-sim==0.3.63` and its reviewed SHA-256. On first
use it fetches the wheel directly from the vendor's PyPI delivery into
`NPA_ANTIOCH_RUNTIME_CACHE`, verifies it, and installs it in that writable volume.
`NPA_ANTIOCH_RUNTIME_OFFLINE=1` fails closed when the cache is cold. Neither the
wheel nor runtime cache belongs in the adapter image. The operator's direct
delivery and use remain subject to the operator's Antioch/NVIDIA terms.

On a host, the default cache is `$XDG_CACHE_HOME/npa/antioch` or
`~/.cache/npa/antioch`. The adapter container explicitly keeps its writable
`/workspace/.cache/npa/antioch` mount. The virtual environment is created at its
final versioned path under a file lock and publishes `.complete` last; moving a
prepared virtualenv is invalid because its generated command shebangs are absolute.

Today the CLI session is personal OAuth stored in Antioch's config directory.
That is suitable for a human-operated smoke, but not a production unattended
identity. Production deployment should use an Antioch service identity when the
vendor exposes one; until then, token expiry requires an operator to refresh the
mounted session. The adapter never initiates interactive login.

## Immutable input and S3 output

`--input-path` names a prefix containing:

```text
project-manifest.json
project.tar.gz
```

The manifest uses schema `npa.antioch.project.v1` and records archive name, size,
SHA-256, source name/revision/license/digest, and asset hashes. Extraction rejects links,
traversal, device nodes, credentials, key files, and projects without exactly one
`antioch.yaml`. The adapter rewrites only its project id to a deterministic value
derived from workflow run and state identities.

`--output-path` must be a run-scoped S3 prefix. Durable state is under `_control/`.
Collected bytes are under `artifacts/<scenario-run>/`, normalized training data
under `dataset/`, and the versioned manifest at `manifests/v1.json`. `_SUCCESS.json`
is created immutably only after every preceding write and checksum check succeeds.
Consumers must gate on that marker. Never reuse one output prefix across unrelated
workflow runs.

## Operations and recovery

```bash
npa workbench antioch submit --input-path s3://BUCKET/input \
  --output-path s3://BUCKET/runs/RUN/simulation \
  --workflow-run RUN --state-id simulate --robot-type ROBOT \
  --task "TASK DESCRIPTION" --suite SUITE --output json
npa workbench antioch status --output-path s3://BUCKET/runs/RUN/simulation \
  --workflow-run RUN --state-id simulate --output json
npa workbench antioch resume --output-path s3://BUCKET/runs/RUN/simulation \
  --workflow-run RUN --state-id simulate --output json
npa workbench antioch cancel --output-path s3://BUCKET/runs/RUN/simulation \
  --workflow-run RUN --state-id simulate --output json
```

`run` is submit, monitor, and collect. A conditional S3 claim and deterministic
Antioch project id ensure a pod retry reconnects rather than creating another
billable suite. `reconcile` repairs the submission-to-state crash window. `resume`
does not rerun terminal work unless `--rerun-terminal` is explicit. HTTP 429 and
5xx failures are retryable; authentication failures, malformed JSON, conflicting
identity, invalid artifacts, and schema failures are terminal. Cancel is
idempotent. Cancelling a completed, failed, or already-cancelled operation is a
no-op that preserves its status and immutable completion/dataset records. Cancel
and rerun failures retain the CLI's retryable/terminal classification in both the
returned error envelope and durable operation state. Cancel test work before
releasing any machine it used.

The sanitized operation record contains the vendor run id. Open that run in the
Antioch Mission Control console using the authenticated account; never paste a
signed console URL into logs, manifests, issues, or pull requests. If a supported
CLI response supplies a non-signed console URL, the adapter may expose its
redacted form. It does not construct undocumented Rome URLs.

## Continuing OpenPI live demonstration

`npa/examples/antioch-openpi-live` is a separate live-control example, not an
offline dataset claim. It renders a real Franka scene and two current policy
cameras, sends observations through an authenticated TLS connection to the
persistent OpenPI service, requires exact finite `[15, 8]` action chunks, and
applies at most five validated targets per observation at a nominal 15 Hz. Joint
limits, a per-target delta bound, a response-age deadline, and reconnect backoff
fail closed into safe hold. Report measured rates and latency; this is not hard
real-time control.

The live viewer includes the normal streamed Isaac viewport, current camera images
in Antioch telemetry, and counters for observation sequence/time, requests, round
trips, latency, action shape/index, safe hold, reconnects, and safely applied
targets. These are emitted by the running scenario, not inferred from an `.rrd`.

The OpenPI side is built by `npa.workflows.byof.openpi_live`: a single B200
Deployment with readiness/liveness, `Recreate` rollout semantics, and a PVC-backed
runtime checkpoint cache. Only a bounded TLS WebSocket gateway is exposed; an
API-key Secret and TLS Secret are generated per live deployment, while the raw
policy and diagnostic ports remain outside the Service and blocked by ingress
policy. The checkpoint, keys, CA private material, credentials, and simulator
payload never enter the public image or project source.

The controller uses supported `antioch services up|exec|cp` commands to place the
0600 client bundle in the sim service, then runs `antioch scenario run --stream
--verbose` under a named tmux session with `pipe-pane`. Scenario timeout is a
finite platform boundary, so the supervisor renews indefinitely until explicitly
stopped. Renewal resets the simulated episode and briefly interrupts the viewport;
it is continuous service supervision, not one immortal scenario process.
The supervisor rechecks and re-stages the private client bundle after container
recreation at a renewal boundary. A Mission Control stream in `ready` state is
published but waiting for an authenticated viewer; do not describe it as actively
viewed until the viewer connects and the first rendered frame advances.

## Policy data contract

Arbitrary logs or telemetry are not training data. Every collected `.npz` episode
must carry arrays `observation_state`, `observation_image_workspace`,
`observation_image_wrist`, `action`, `reward`, `terminated`, `truncated`,
`timestamp`, plus JSON `provenance`. Lengths must agree; timestamps must increase;
observations/actions must be finite; and exactly one of terminated/truncated must
be true on the final frame only. The action width must match `action_schema`.
The pinned LeRobot ACT path currently requires at least two physically meaningful
action channels; collection fails closed rather than padding or duplicating a
single-channel control.

The `npa.antioch.episode.v1` provenance includes scenario, case, seed, parameters,
engine and SDK versions, source SHA-256, asset hashes, observation/action schemas,
and FPS. Incompatible episodes fail collection, leaving no completion marker.
Validated episodes are converted by the real NPA LeRobot v3 adapter, with
`meta/antioch-provenance.json` retaining provenance. This supports static offline
imitation training. It does **not** turn the export into an online PPO/RSL-RL
environment.

`--robot-type` and `--task` are required at submit/run time and are bound into
the idempotent operation record before the remote run starts. Collection always
uses those immutable values. Missing metadata fails before submission; there is
no cartpole fallback and a later collector cannot silently relabel a dataset.

The executable example
`npa/workflows/workbench/npa-workflows/antioch-offline-policy-train.yaml` follows
collection with real LeRobot ACT training and publishes a genuine checkpoint.

## Security and cleanup

The adapter filters sensitive keys and bearer/JWT/signed-URL forms from CLI errors
and log objects. It never emits environment dumps, identity fields, config files,
tokens, or customer metadata. Use only synthetic/public projects for validation.
After a smoke, cancel only the run ids created for that smoke, then release only
the associated project machine if one was allocated; queued managed execution
normally requires no persistent operator machine.
