# Antioch / Isaac Sim Franka with an OpenPI B200 policy server

This path keeps rendering and inference on different GPU workloads:

| Workload | GPU | Image and responsibility |
| --- | --- | --- |
| simulator bridge | RTX PRO 6000 (`sm_120`, RT cores) | digest-pinned `npa-isaac-lab`; runtime-fetches Isaac under the operator's NVIDIA acceptance, captures exterior and wrist RGB plus Franka state, validates and applies bounded position targets |
| policy server | B200 (`sm_100`) | digest-pinned private OpenPI BYOF image; runtime-fetches `pi05_droid_jointpos_polaris` only after the exact run-scoped Gemma gate, serves upstream MessagePack/WebSocket on port 8000 |

The policy Service is `ClusterIP`; no Ingress, NodePort, or load balancer is
created. A NetworkPolicy admits port 8000 only from the run's bridge pod. The
bridge bypasses ambient HTTP proxies only for the generated in-cluster service
and cluster-local DNS suffixes. The
Antioch configuration Secret, Isaac acceptance Secret, and optional S3 Secret
are mounted only into the simulator bridge. The policy pod receives only the
separate Gemma-terms Secret. Neither workload receives the other's credential.

## Protocol and fail-closed control

The bridge uses upstream OpenPI's MessagePack NumPy encoding without pickle.
Every request must contain exactly:

- `observation/exterior_image_1_left`: `uint8[224,224,3]`
- `observation/wrist_image_left`: `uint8[224,224,3]`
- `observation/joint_position`: finite `float32[7]`
- `observation/gripper_position`: finite `float32[1]`
- `prompt`: non-empty string

Every response must contain finite absolute targets with exact shape `[15,8]`.
The seven arm targets must be inside Franka position limits and the gripper must
be in `[0,1]`. The bridge executes five targets by default, limits each joint to
at most `0.08` radians of change per control step, and then re-observes. A
timeout, disconnect, text frame, malformed MessagePack, wrong shape, non-finite
value, or unsafe target closes the connection and produces `failed-no-action`;
no stale or random action is substituted. Connect and inference calls have
explicit deadlines. Transient failures reconnect with bounded exponential
backoff (four attempts, 0.5 to 8 seconds).

Before Isaac starts, a non-GPU init container polls the private policy health
endpoint with bounded requests and exponential backoff. Its readiness deadline
defaults to 1,800 seconds and is configurable with
`--policy-ready-timeout-seconds`; expiry prevents simulator startup and action
application.

## Build and deployment

Build the policy image through the existing pinned `byof-openpi.yaml` path. It
pins OpenPI source revision `15a9616a00943ada6c20a0f158e3adb39df2ccac`, retains
the CUDA/JAX stack proven on B200, compiles the `sm_100` probe, and keeps the
checkpoint out of its layers. Build the bridge with the existing Isaac build
script; this change adds only the MessagePack/WebSocket client to its
resolver-closed control dependencies. Isaac and Antioch vendor bytes remain
runtime-only.

Resolve both pushed tags to registry digests, create the Secrets out of band,
and render the stack with generic, configurable selectors:

```bash
npa workbench antioch openpi-stack \
  --run-id <unique-run> \
  --policy-image '<private-registry>/openpi@sha256:<digest>' \
  --bridge-image '<private-registry>/npa-isaac-lab@sha256:<digest>' \
  --policy-terms-secret <gemma-acceptance-secret> \
  --isaac-acceptance-secret <isaac-acceptance-secret> \
  --image-pull-secret <private-registry-pull-secret> \
  --antioch-config-secret <antioch-runtime-config-secret> \
  --s3-credentials-secret <runtime-s3-secret> \
  --output-path 's3://<bucket>/<run-prefix>/bridge.json' \
  --output json
```

Inspect the manifest, then repeat with `--apply`. Secret objects and values are
never rendered. The command rejects mutable image tags. After collecting the
report, rerun the same command with `--delete` to remove the Deployment,
Service, Job, and NetworkPolicy; `--apply` and `--delete` are mutually
exclusive.

## Antioch-hosted execution and the token gate

`npa/examples/antioch-openpi-franka` is a thin Antioch scenario over the same
bridge function. Antioch's runner owns Kit startup; the wrapper does not fork a
second simulator or duplicate control logic. The exact NPA revision must be
installed in the private project image.

An Antioch account session is required only to allocate/start that hosted
engine and publish its managed scenario record. When no session is available,
do not recover credentials or weaken the gate. The strongest independent proof
is the private Kubernetes run: real runtime-fetched Isaac on RTX PRO 6000, real
B200 checkpoint inference, camera/state serialization, cross-GPU ClusterIP
transport, safe target application, and failure-path smokes. The single
deferred check is then: authenticate through the supported Antioch CLI, package
the example at the tested NPA revision, run suite `openpi_franka_smoke` in the
private Isaac Lab engine, and verify its two checks and managed artifact record.

Antioch does not accept secret mounts in `antioch.yaml`. For a hosted smoke,
keep the Kubernetes credential on the operator host: the example's loopback
reverse relay uses Antioch's authenticated port tunnel, and the local connector
pairs it with `kubectl port-forward` to the ClusterIP policy Service. This gives
the hosted simulator a private byte stream without copying a kubeconfig into the
Antioch service or creating an Ingress, NodePort, or load balancer.

## Licensing and artifacts

- NPA and OpenPI source are Apache-2.0.
- The bridge image is eligible for public redistribution only because its built
  layers contain no Isaac, Omniverse Kit, Antioch SDK, checkpoint, cache, or
  credential bytes, and because it uses distro FFmpeg instead of the separately
  licensed static executable bundled in the `imageio-ffmpeg` wheel. Publish an
  exact scanned digest only after the repository's guarded GHCR procedure.
- The OpenPI policy image is independently public-eligible only when its layers
  contain source/runtime dependencies but no pi0.5/Gemma checkpoint, model
  cache, access credential, or live infrastructure value. The operator fetches
  the checkpoint at runtime under the run-scoped Gemma acceptance.
- Isaac/Omniverse and Antioch runtime caches remain private runtime state and
  must never be committed or copied into a derived image.
- Polaris weights contain Gemma-derived material, are fetched only after the
  exact `NPA_OPENPI_ACCEPT_GEMMA_TERMS=YES` runtime gate, and remain private.

Scan the built bridge with `scan_image_omniverse_payload.py`; scan the OpenPI
image with the BYOF/OpenPI built-byte checks. Acceptance changes permission to
run this workload, not redistribution rights.
