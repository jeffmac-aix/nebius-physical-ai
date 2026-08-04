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
run/task/device, fixed ports, public IP endpoints, expiry, source commit, and
digest-pinned image, then verifies the live service's matching nonce
attestation. Any absent, stale, malformed, unreachable, mismatched, or
non-ready session leaves the tab absent. Switching runs repeats this check.

The browser receives no service credential, nonce, or internal endpoint.
Signaling is relayed through the agent's authenticated same-origin
`/api/leisaac/signal` WebSocket. The NVIDIA browser client JavaScript is also
proxied through an authenticated route and must match the agent's exact pinned
SHA-256 before it can execute. WebRTC media uses only the session's
public UDP `47998` endpoint. Status/client TCP `8080` and signaling TCP `49100`
and UDP media `47998` are source-restricted to the operator and agent IPs at
their load balancers.

## Runtime and licensing

`npa-leisaac` derives from the digest-pinned public runtime-fetch
`npa-isaac-lab:2.3.2.post1` image. Its compatibility set is:

- Isaac Sim `5.1.0.0`;
- Isaac Lab `2.3.2.post1` and source commit `37ddf626…`;
- LeIsaac `0.4.0` / commit `1651c321…` (upstream requests Isaac Lab 2.3.0;
  NPA uses the compatible patched 2.3.x release and validates the real task);
- NVIDIA Omniverse WebRTC streaming client `5.18.11` (the NVIDIA 5.x release
  line, with `forceWSS` enabled for authenticated public-IP HTTPS access).

The image bakes only Apache-2.0 LeIsaac source and OSS dependencies. The
unlicensed optional Feetech SDK used by physical leader hardware is not
redistributed; an explicit packaging-only patch removes that dependency edge,
and this browser service uses upstream's unmodified software keyboard path. It
refuses to start until the operator explicitly sets both
`OMNI_KIT_ACCEPT_EULA=YES` and `ISAACSIM_ACCEPT_EULA=YES`. Only then are Isaac,
the NVIDIA client, and the two task assets fetched into mounted caches. The
assets and client are hash-verified and recorded in runtime `provenance.json`;
EULA acceptance and proprietary bytes are never baked into an image.

## Launch

Rendering requires an RT-core GPU. Use L40S or RTX PRO 6000; the Kubernetes
launcher hard-selects RTX PRO 6000 and never routes this path to H100/H200.
The image must be pinned by digest and at least one public `/32` source range
must be provided for the agent/operator. The operator chooses the capability
expiry explicitly; the CLI imposes no session lifetime.

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
  --source-range AGENT_PUBLIC_IP/32 \
  --source-range OPERATOR_PUBLIC_IP/32 \
  --artifact-uri s3://BUCKET/leisaac \
  --expires-at 2026-08-05T12:00:00Z
```

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

Destroy removes only that run's transient Deployment and load balancers. It
preserves the S3 manifest/log/evidence record. Once the service is gone, live
health fails and the agent UI removes the tab even if the historical manifest
still exists.

Durable validation evidence and screenshots live under
`docs/evidence/leisaac/`.
