# LeIsaac browser teleoperation

NPA exposes [LightwheelAI/LeIsaac](https://github.com/LightwheelAI/leisaac)
as a separate agent-UI tab while the agent has a registered, usable live
session. The integration runs upstream LeIsaac v0.4.0 at commit
`1651c321e9b0c1bb54233211fc7b3cd70d8373d5`, the real
upstream `SO101Keyboard`. The checked-in registry advertises only the two tasks
at that commit that use this exact single-arm control and asset path:

- `LeIsaac-SO101-PickOrange-v0`;
- `LeIsaac-SO101-LiftCube-v0`.

Inspect the machine-readable source of truth with
`npa workbench leisaac list-tasks --output json`. A session runs one environment
at a time. `--num-envs` is intentionally restricted to `1`; collect named
sequential instances with stable `--environment-id`, `--environment-index`, and
`--seed` values into the same dataset prefix instead of presenting unsupported
parallel control routing.

## What makes the tab appear

The `LeIsaac` tab is not present in the initial HTML. An agent-relay launch
registers its live run through the agent's authenticated, certificate-pinned
HTTPS API. The browser first asks about the selected artifact run, then falls
back to that independently registered LeIsaac run. This keeps the tab visible
when an operator opens an unrelated Rerun or Voxel51 artifact. The backend discovers
exactly one `reports/leisaac-session.json` artifact, validates its schema,
run/task/device, fixed transport endpoints, expiry, source commit, and
digest-pinned image, registry fingerprint, task/environment identity, and S3
dataset destination, then verifies the live service's matching one-way nonce
attestation. The raw nonce never appears in `/status` or the browser. Any
absent, stale, malformed, unreachable, mismatched, or
non-ready session leaves the tab absent. Selecting another live LeIsaac run
updates the registered capability; switching unrelated artifact runs does not
discard it.

The browser receives no service nonce or agent credential. The live Isaac Sim
5.1 path returns same-origin, authenticated `/api/leisaac/frame.jpg` and
`/api/leisaac/input` routes. The agent resolves only the selected run's
validated capability, authenticates to the private service with the hidden
session nonce, rejects malformed JPEG bytes and unknown controls, and relays
the response through HTTPS. The service reports accepted inputs separately
from inputs consumed by upstream `SO101Keyboard`, so live validation proves
that controls reached the simulator rather than only the public API.

The older WebRTC path remains available as a compatibility transport. In that
mode, the no-store status response includes one derived session-scoped TURN
credential, signaling uses `/api/leisaac/signal`, and the hash-pinned NVIDIA
browser client is proxied through an authenticated route.

Two transport modes preserve that browser contract. `public-load-balancer`
source-restricts status/client TCP `8080`, signaling TCP `49100`, and UDP media
`47998` on dedicated load balancers. `agent-relay` consumes no additional
public IPv4 allocation: Kubernetes uses a private `ClusterIP` service, the saved
NPA agent runs a hardened systemd relay, and a non-GPU sidecar in the simulation
pod initiates an authenticated WSS backhaul through nginx `443` to it. A
digest-pinned coturn sidecar shares the simulator's pod network for the
compatibility WebRTC path. The
backhaul uses the agent's existing basic-auth credential, pins the public HTTPS
certificate SHA-256, and authenticates again with a random session nonce. The
relay binds status to `127.0.0.1:48080`, signaling to `127.0.0.1:49100`, and
its raw backhaul socket to `127.0.0.1:48081`. Status, signaling, and public
UDP `3478` TURN control datagrams use the authenticated WSS backhaul. The
coturn allocation's private UDP `47999-48015` relay range and Isaac Sim's
UDP `47998`
media peer communicate directly inside the shared pod network namespace.
Only explicit operator CIDRs can reach public UDP `3478`; UDP
`47999-48015`, the
GPU pod, and the GPU node remain private. WebRTC sessions force
`iceTransportPolicy=relay`. TURN long-term authentication,
one session-scoped user with a bounded 16-allocation quota, and the exact
security-group rule prevent the public control relay from acting as an open
proxy. The agent and cluster may
remain in separate VPCs because their only cross-VPC path is the pod-initiated
WSS backhaul; no GPU-node ingress or host port is required. The
backhaul script, agent auth, certificate hash, and nonce are mounted into the
pod through a Kubernetes Secret. The UI and TCP APIs remain behind nginx HTTPS
and basic authentication; port `8787`, `8080`, `49100`, cluster ports, and the
GPU pod are never publicly reachable.

## Runtime and licensing

`npa-leisaac` derives from the digest-pinned public runtime-fetch
`npa-isaac-lab:2.3.2.post1` image. Its compatibility set is:

- Isaac Sim `5.1.0.0`;
- Isaac Lab `2.3.2.post1` and source commit `37ddf626…`;
- LeIsaac `0.4.0` / commit `1651c321…` (upstream requests Isaac Lab 2.3.0;
  NPA uses the compatible patched 2.3.x release and validates the real task);
- NVIDIA Omniverse WebRTC streaming client `5.6.0` for the retained
  compatibility path, the version documented by
  NVIDIA's [web viewer sample](https://github.com/NVIDIA-Omniverse/web-viewer-sample)
  for Kit 107.3.1+ and compatible with Isaac Sim 5.1. Its pristine
  runtime-fetched JavaScript is hash-verified, then receives one exact
  transport-only patch so a numeric signaling host on port 443 selects WSS.
  Both source and served hashes are recorded in provenance. The browser still
  requests `forceWSS` as defense in depth for clients that expose that option.

The image bakes only Apache-2.0 LeIsaac source and OSS dependencies. The
unlicensed optional Feetech SDK used by physical leader hardware is not
redistributed; an explicit packaging-only patch removes that dependency edge,
and this browser service uses upstream's software keyboard path with a narrow,
fail-safe integration patch that publishes readiness only after the real task
reset and first non-empty RTX frame. It drains a bounded, validated input queue
inside upstream `SO101Keyboard`, applies each press for eight simulation steps,
and records the consumed-input count separately. Browser teleoperation uses
the RTX viewport rather than policy camera tensors, so the same patch removes
the two unused tiled-camera sensors, their observation terms, and the now-unused
front-camera randomizer to avoid Isaac's camera/DirectGpu interoperability fault on
`sm_120`. Physics for this single interactive environment runs on CPU because
Isaac Sim 5.1 does not ship `sm_120` PhysX kernels; the real environment, RTX
rendering, viewport capture, and JPEG production remain active on the selected
RT-core GPU. The browser path disables Isaac Lab Fabric so CPU PhysX synchronizes through the
supported USD I/O path. It also disables Isaac Lab's texture-loading wait: the
headless session uses the active viewport rather than RTX camera observations,
and the default asset-loading loop does not terminate reliably on this path.
The pod requests 16 CPU cores and may use up to 32 so
the USD-backed first reset is not throttled by the previous eight-core limit. The
session supervisor starts Kit in an isolated process session with closed stdin
so HTTP-service signal handling cannot interfere with upstream teleoperation.
The browser service uses the explicit launch seed (default `42`) and reports it
with the stable environment identity in `/status` and every recorded frame.
On a cold pod, liveness remains healthy while the supervised simulator process
is alive, including during the licensed runtime fetch and first reset. Readiness
and `/status` remain unavailable until the real reset and a non-empty captured
frame are both ready; a failed or exited simulator still fails liveness so
Kubernetes can
restart it while preserving the pod-local `emptyDir` caches.
The exact patch is commit-locked in the image build and named in runtime
provenance. It
refuses to start until the operator explicitly sets both
`OMNI_KIT_ACCEPT_EULA=YES` and `ISAACSIM_ACCEPT_EULA=YES`. Only then are Isaac,
the NVIDIA client, the SO101 asset, and both scene assets fetched into mounted caches. The
assets and client are hash-verified and recorded in runtime `provenance.json`;
EULA acceptance and proprietary bytes are never baked into an image.

## Launch

Rendering requires an RT-core GPU. Use L40S or RTX PRO 6000; the Kubernetes
launcher hard-selects RTX PRO 6000 and never routes this path to H100/H200.
The image must be pinned by digest and at least one public `/32` operator source
range must be provided. The session has no implicit lifetime;
an operator may add `--expires-at` as an explicit security policy, otherwise
the live service lifecycle controls tab availability.
Before applying the deployment, `launch` refreshes the selected Kubernetes pull
secret with a newly minted Nebius IAM token and verifies that the secret exists.
If credential minting or the secret apply fails, launch stops before scheduling
the GPU workload instead of relying on a warm node image cache.

Deploy or re-bootstrap the agent through the supported lifecycle command. A
fresh deployment provisions a Nebius public IP; re-bootstrap resolves the
existing VM's current public IP from provider state and persists the canonical
customer URL. The operator-facing endpoint is always
`https://<agent-public-ip>/` when public HTTPS is enabled.

```bash
npa agent fresh-setup --project PROJECT_ALIAS --name AGENT_NAME \
  --project-id PROJECT_ID --tenant-id TENANT_ID --region REGION
npa agent status --project PROJECT_ALIAS --name AGENT_NAME --json
```

The public endpoint terminates HTTPS in nginx. `/healthz` is the intentionally
unauthenticated liveness probe; the UI, API, LeIsaac client, and WebSocket
signaling relay require the agent's basic-auth credentials. The FastAPI backend
listens only on VM loopback and is never exposed directly. Accept the
self-signed certificate for the public IP, then verify the endpoint from the
operator host:

Deploy and re-bootstrap also remove a dedicated legacy `allow-npa-*` rule for
the internal backend port if an older deployment left one behind. NPA refuses
to rewrite an unmanaged or mixed-purpose rule and fails closed instead. HTTPS
ingress is ensured through the existing agent security group; this path does
not broaden SSH or publish the backend listener.

```bash
AUTH_SECRET_PATH=/secure/path/reported-by-agent-deploy
source "${AUTH_SECRET_PATH}"
AGENT_URL="$(npa agent status --project PROJECT_ALIAS --name AGENT_NAME --json \
  | npa/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["public_url"].rstrip("/"))')"
curl -sk "${AGENT_URL}/healthz"
curl -sk -u "${AGENT_USER}:${AGENT_PASSWORD}" "${AGENT_URL}/api/health"
```

```bash
export OMNI_KIT_ACCEPT_EULA=YES
export ISAACSIM_ACCEPT_EULA=YES
# On shared operator hosts, select the registry-authorized identity separately
# from the Nebius/Kubernetes access profile used by the rest of the command.
export NPA_NEBIUS_PROFILE=agent-sa

npa workbench leisaac launch \
  --run-id leisaac-teleop-example \
  --image cr.us-central1.nebius.cloud/REGISTRY/npa-leisaac@sha256:DIGEST \
  --context YOUR_KUBECTL_CONTEXT \
  --source-range OPERATOR_PUBLIC_IP/32 \
  --transport agent-relay \
  --agent-project PROJECT_ALIAS \
  --agent-name AGENT_NAME \
  --task LeIsaac-SO101-PickOrange-v0 \
  --environment-id kitchen-a \
  --environment-index 0 \
  --seed 42 \
  --num-envs 1 \
  --output-path s3://BUCKET/datasets/leisaac-demo \
  --manifest-prefix s3://BUCKET/checkpoints
```

`--output-path` is always a dataset prefix. `--manifest-prefix` is the
capability publication prefix; it may also be an exact
`.../reports/leisaac-session.json` leaf. The deprecated `--artifact-uri` alias
retains those exact leaf semantics, fixing the older duplicated
`leisaac-session.json/<run>/reports/leisaac-session.json` behavior without
hiding already-written historical objects from discovery.

`agent-relay` resolves the agent IP from live provider state and refuses a
stale saved address, missing SSH key or agent auth, unrestricted source range,
TLS certificate mismatch, invalid session nonce, or a second active relay
session. The supported deployment uses a digest-pinned, non-root coturn sidecar
and exposes no coturn port from the GPU cluster. Use
`--transport public-load-balancer` only when dedicated Kubernetes public IPv4
allocations are intended; in that mode repeat `--source-range` for the agent
and operator because the agent reaches the status/signaling load balancer.

This is an interactive, lifecycle-bearing service rather than a finite batch
stage, so it is intentionally launched and destroyed through the Workbench
lifecycle command, not represented as an `npa.workflow` step that would report
completion while the browser session still needs to remain alive.

Reload the agent UI after launch, open `LeIsaac`, and choose **Connect
teleoperation**. No run-ID entry is required. Click the simulation to focus it.
Controls are the upstream bindings: `W/S`, `A/D`, `Q/E` translate; `J/L`,
`K/I` rotate; `U/O` open/close the gripper. Episode state is explicit: **Start
episode**, **Mark success** or **Mark failure**, then **Finalize & upload**.
Finalize is disabled until an outcome is selected. Upload errors stay visible
and do not discard the pod-local episode; **Finalize & upload** becomes a retry
when the prior conditional publication attempt failed. Marking an outcome
freezes the episode boundary before upload.

## LeRobot demonstration dataset

Every captured JPEG comes from the real RTX viewport and is paired with the
most recent completed real `env.step`. Its Parquet record contains the real
six-joint observation, exact eight-dimensional action passed to `env.step`,
reward, terminated/truncated/done values, simulator step, seed,
task/environment identity, source-frame SHA-256, and monotonic
and wall-clock nanosecond timestamps. The fixed 16 FPS `timestamp` field is the
LeRobot video clock; the original clocks remain separate audit features.

Finalization encodes synchronized JPEGs as H.264 and uploads both the ordered
raw JPEGs and a unique raw episode bundle. Their per-frame hashes are tied to
the records and commit, so frame/action alignment remains independently
auditable after lossy H.264 encoding. A conditional
`commits/episode-NNNNNN.json` object makes the
episode durable before a new immutable `versions/vNNNNNN-UUID/` LeRobot tree is
published. A crash can leave an unreferenced bundle but cannot overwrite a
completed commit or prior version. The next session resumes numbering from the
commit objects and can add a different supported task/environment to the same
prefix. `latest.json` is only a pointer; version contents are immutable.

The output targets `LeRobotDataset` 0.5.1 / format `v3.0`. Download one
immutable version and validate it with the supported loader (Python 3.12+):

```bash
python3.12 -m venv /tmp/lerobot-051
/tmp/lerobot-051/bin/pip install 'lerobot==0.5.1'
/tmp/lerobot-051/bin/python - <<'PY'
from lerobot.datasets.lerobot_dataset import LeRobotDataset
dataset = LeRobotDataset(repo_id="local/leisaac", root="/path/to/version")
print(len(dataset), dataset.meta.total_episodes, dataset.meta.video_keys)
PY
```

## PAIDF appearance augmentation

Export a finalized episode directly from its immutable S3 version; no local
operator download is needed:

```bash
npa workbench leisaac export-paidf \
  --dataset-uri s3://BUCKET/datasets/leisaac-demo/versions/v000002-UUID \
  --episode 0 --run-id paidf-leisaac-001 \
  --output-path s3://BUCKET/physical-ai-data-factory/paidf-leisaac-001 \
  --output json
```

The result reports the exact runnable
`npa/workflows/physical-ai-data-factory.yaml` command with
`NPA_COSMOS_CONDITION_ON_INPUT=1`. The workflow invokes the real
`workbench.cosmos2.transfer_execute` toolRef with
`--condition-on-input --execute`; a manifest-only stub is not accepted.
Source dataset/version/episode/task/environment/checksums are written to
`input/leisaac-lineage.json` and carried into the PAIDF final report. Config
generation detects that lineage and uses an orange-pick or cube-lift
appearance-only prompt instead of the blueprint's default cloth-folding scene.
An agent-scoped base prefix is allowed before the exact
`physical-ai-data-factory/<run-id>` suffix.

After a real run, materialize one variant:

```bash
npa workbench leisaac materialize-paidf \
  --dataset-uri s3://BUCKET/datasets/leisaac-demo/versions/v000002-UUID \
  --episode 0 \
  --paidf-run-uri s3://BUCKET/physical-ai-data-factory/paidf-leisaac-001 \
  --variant 0 --output-path s3://BUCKET/datasets/leisaac-derived \
  --output json
```

Materialization requires `mode=cosmos_transfer2.5_gpu`, `status=executed`, and
`input_conditioned=true`, decodes a nonblank augmented clip, and rejects any
frame-count or timestamp difference greater than 1 ms. Only then does it copy
the parent Parquet labels byte-for-byte and replace the selected visual object
in a new immutable version. Cosmos Transfer appearance conditioning preserves
the demonstrated motion structurally; it is not action augmentation and adds
no robot state, reward, label, or task-success evidence.

## Status and cleanup

```bash
npa workbench leisaac status --run-id leisaac-teleop-example --context YOUR_KUBECTL_CONTEXT
npa workbench leisaac destroy --run-id leisaac-teleop-example --context YOUR_KUBECTL_CONTEXT
```

Destroy removes only that run's transient Deployment, Services, relay Secret,
and dedicated recorder credential Secret. Completed S3 episodes, dataset
versions, and PAIDF evidence are preserved. For an
agent-relayed run it reads the owning agent and source CIDRs from Kubernetes
metadata, stops only the matching relay unit, and deletes only the matching
relay and any compatibility TURN unit, and deletes only the matching
NPA-managed UDP `3478` rule (plus a legacy `47999` rule when an older
session recorded one). It preserves the S3
manifest/log/evidence record.
Once the service is gone, live health fails and the agent UI removes the tab
even if the historical manifest still exists.

Durable validation evidence and screenshots live under
`docs/evidence/leisaac/`.
