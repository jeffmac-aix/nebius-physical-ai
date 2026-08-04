# LeIsaac browser teleoperation

NPA exposes [LightwheelAI/LeIsaac](https://github.com/LightwheelAI/leisaac)
as a separate agent-UI tab only while the selected run has a usable live
session. The integration runs upstream LeIsaac v0.4.0 at commit
`1651c321e9b0c1bb54233211fc7b3cd70d8373d5`, the real
`LeIsaac-SO101-PickOrange-v0` environment, and upstream `SO101Keyboard`.
It is not a Cartpole or synthetic viewer demo.

## What makes the tab appear

The `LeIsaac` tab is not present in the initial HTML. The browser asks the
authenticated agent backend about the selected run. The backend discovers
exactly one `reports/leisaac-session.json` artifact, validates its schema,
run/task/device, fixed transport endpoints, expiry, source commit, and
digest-pinned image, then verifies the live service's matching nonce
attestation. Any absent, stale, malformed, unreachable, mismatched, or
non-ready session leaves the tab absent. Switching runs repeats this check.

The browser receives no service nonce, agent credential, or internal endpoint.
For an agent-relayed session, its authenticated, no-store status request does
receive one derived, session-scoped TURN credential. Signaling is relayed
through the agent's authenticated same-origin
`/api/leisaac/signal` WebSocket. The NVIDIA browser client JavaScript is also
proxied through an authenticated route and must match the agent's exact pinned
SHA-256 before it can execute.

Two transport modes preserve that browser contract. `public-load-balancer`
source-restricts status/client TCP `8080`, signaling TCP `49100`, and UDP media
`47998` on dedicated load balancers. `agent-relay` consumes no additional
public IPv4 allocation: Kubernetes uses a private `ClusterIP` service, the saved
NPA agent runs a hardened systemd relay, and a non-GPU sidecar in the simulation
pod initiates an authenticated WSS backhaul through nginx `443` to it. The
backhaul uses the agent's existing basic-auth credential, pins the public HTTPS
certificate SHA-256, and authenticates again with a random session nonce. The
relay binds status to `127.0.0.1:48080`, signaling to `127.0.0.1:49100`, and
its raw backhaul socket to `127.0.0.1:48081`. The sidecar also reports its
validated public egress IPv4 through a loopback-only control endpoint. Status
and signaling use the authenticated WSS backhaul. Media uses the browser and
upstream NVIDIA client's standard WebRTC path through a session-scoped coturn
service on the public agent: allocation requests reach UDP `3478` only from
explicit operator CIDRs, and the single relay port UDP `47999` accepts media
only from the observed GPU egress route. Nebius's default egress gateway uses
a dynamic address from a shared regional pool, so NPA resolves that address's
announced route through RIPEstat and accepts only a public IPv4 `/22` or
narrower whose announced origin holder is Nebius. Resolution or ownership
ambiguity fails closed; this is never widened to all sources. The UI forces `iceTransportPolicy` to
`relay` for that session. TURN long-term authentication, a one-user/one-allocation
quota, and the exact security-group rules prevent the public relay from acting
as an open proxy. Because the GPU sends media outbound to the TURN allocation,
the agent and cluster may remain in separate VPCs and no GPU-node ingress or
host port is required. The
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
- NVIDIA Omniverse WebRTC streaming client `5.6.0`, the version documented by
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
fail-safe observability patch that publishes readiness only after the real task
reset and counts accepted upstream keyboard events. Browser teleoperation does
not consume policy image tensors, so the same patch disables those two unused
observation terms to avoid Isaac's camera/DirectGpu interoperability fault on
`sm_120`. Physics for this single interactive environment runs on CPU because
Isaac Sim 5.1 does not ship `sm_120` PhysX kernels; the real environment, RTX
rendering, and WebRTC encoding remain active on the selected RT-core GPU. The
session supervisor starts Kit in an isolated process session with closed stdin
so HTTP-service signal handling cannot interfere with upstream teleoperation.
The browser service pins upstream seed `42` and reports it in `/status`; this
avoids nondeterministic PickOrange reset states and makes evidence reproducible.
On a cold pod, the existing liveness probe preserves the licensed runtime fetch
but restarts a simulator that remains in its first pre-ready reset. Kubernetes
keeps the pod's `emptyDir` Isaac caches, so the deterministic retry uses the
warmed collision/shader cache while `/status` remains unavailable until reset.
The exact patch is commit-locked in the image build and named in runtime
provenance. It
refuses to start until the operator explicitly sets both
`OMNI_KIT_ACCEPT_EULA=YES` and `ISAACSIM_ACCEPT_EULA=YES`. Only then are Isaac,
the NVIDIA client, and the two task assets fetched into mounted caches. The
assets and client are hash-verified and recorded in runtime `provenance.json`;
EULA acceptance and proprietary bytes are never baked into an image.

## Launch

Rendering requires an RT-core GPU. Use L40S or RTX PRO 6000; the Kubernetes
launcher hard-selects RTX PRO 6000 and never routes this path to H100/H200.
The image must be pinned by digest and at least one public `/32` operator source
range must be provided. The session has no implicit lifetime;
an operator may add `--expires-at` as an explicit security policy, otherwise
the live service lifecycle controls tab availability.

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

npa workbench leisaac launch \
  --run-id leisaac-teleop-example \
  --image cr.us-central1.nebius.cloud/REGISTRY/npa-leisaac@sha256:DIGEST \
  --context YOUR_KUBECTL_CONTEXT \
  --source-range OPERATOR_PUBLIC_IP/32 \
  --transport agent-relay \
  --agent-project PROJECT_ALIAS \
  --agent-name AGENT_NAME \
  --artifact-uri s3://BUCKET/leisaac
```

`agent-relay` resolves the agent IP from live provider state and refuses a
stale saved address, missing SSH key or agent auth, unrestricted source range, TLS
certificate mismatch, invalid session nonce, invalid GPU public egress address,
unattested or overly broad GPU egress route, missing coturn package, or a second
active relay/TURN session. The supported
agent bootstrap installs coturn but does not start or expose it until a LeIsaac
session has passed live attestation. Use
`--transport public-load-balancer` only when dedicated Kubernetes public IPv4
allocations are intended; in that mode repeat `--source-range` for the agent
and operator because the agent reaches the status/signaling load balancer.

This is an interactive, lifecycle-bearing service rather than a finite batch
stage, so it is intentionally launched and destroyed through the Workbench
lifecycle command, not represented as an `npa.workflow` step that would report
completion while the browser session still needs to remain alive.

Select that run in the agent UI, open `LeIsaac`, and choose **Connect
teleoperation**. Click the simulation to focus it. Controls are the upstream
bindings: `W/S`, `A/D`, `Q/E` translate; `J/L`, `K/I` rotate; `U/O` open/close
the gripper; `R` resets; `N` marks success and resets.

## Status and cleanup

```bash
npa workbench leisaac status --run-id leisaac-teleop-example --context YOUR_KUBECTL_CONTEXT
npa workbench leisaac destroy --run-id leisaac-teleop-example --context YOUR_KUBECTL_CONTEXT
```

Destroy removes only that run's transient Deployment and Services. For an
agent-relayed run it reads the owning agent and source CIDRs from Kubernetes
metadata, stops only the matching relay unit, and deletes only the matching
relay and TURN units, and deletes only the matching NPA-managed UDP `3478` and
`47999` rules. It preserves the S3
manifest/log/evidence record.
Once the service is gone, live health fails and the agent UI removes the tab
even if the historical manifest still exists.

Durable validation evidence and screenshots live under
`docs/evidence/leisaac/`.
