# OpenPI Franka operation

## Deployment and cache boundary

Follow `docs/workbench/antioch-openpi-franka.md` for the user workflow and
`npa/examples/antioch-openpi-franka` for the pinned hosted scenario.

- Resolve the B200 policy and RTX adapter/runtime-fetch images to immutable
  digests. Pin the hosted scenario's NPA source revision separately.
- Warm the exact OpenPI checkpoint and tokenizer at runtime under the operator's
  accepted upstream terms. Key cache identities by provider, artifact, immutable
  generation/revision, and format. Serialize writers, download to unique
  temporary paths, verify files/checksums, atomically publish a ready marker,
  and refuse partial or mismatched identities.
- Default to pod/node-local ephemeral cache. Use a durable PVC only when
  configured, and prove a restarted policy pod reuses it. The warmer alone gets
  entitlement and read-write access; the server gets the verified cache
  read-only and no entitlement.
- Test cold population, concurrent writers, warm reuse, corruption refusal and
  recovery, revision separation, missing entitlement, and read-only serving.

## Private cross-GPU transport

Keep the B200 policy endpoint as ClusterIP port 8000. Use an ingress policy that
allows only the RTX bridge. For Antioch-hosted execution, use the declared
authenticated local port plus `policy_tunnel_connector.py`; do not expose the
policy publicly or copy Kubernetes credentials to Antioch.

Tunnel readiness requires all of:

1. supported Antioch service/API state is ready;
2. the B200 `/healthz` endpoint passes through the local port-forward;
3. the connector completes a session carrying a valid OpenPI handshake/request;
4. two non-empty camera frames and an exact finite `[15,8]` action chunk are
   observed; and
5. at least one rate-limited target is safely applied.

An empty accepted tunnel session commonly means the hosted frontend opened and
closed before the local connector reached the policy, the service port was not
converged, or one side restarted. Keep the policy port-forward alive, re-check
service/API and health state, then reconnect with bounded exponential backoff.
Do not loop without a cap, bypass auth, scrape cookies, or declare success from
an open socket.

## Compatibility failures to handle explicitly

- **Single environment camera batches:** accept ordinary RGB frames or the
  leading one-environment dimension; reject other rank/shape rather than
  silently squeezing arbitrary input.
- **Hosted viewport:** Antioch owns Kit startup. Run with authenticated streaming
  enabled, require an active viewport, render and advance the supported Kit app
  loop until the capture callback completes, and fail on timeout or malformed
  RGBA bytes. Do not create a second SimulationApp.
- **Standalone cameras:** launch Isaac with camera support and configure exterior
  and wrist sensors inside the same one-environment scene.
- **Franka assets:** probe the runtime-advertised immutable asset root. If its
  Franka sentinel is unpublished, use only the reviewed published compatibility
  root after probing it. Rewrite both module asset constants and any task config
  imported before the rewrite. Never guess a mutable asset URL or fall back to a
  local untracked asset.
- **Imports:** keep Isaac/control imports lazy so CPU render/CLI paths remain
  importable. Keep the policy health helper isolated from dataset, manager,
  storage, and optional control dependencies.
- **Identity:** pin hosted source, image digest, adapter version, engine, SDK, and
  asset compatibility independently in evidence. An image version is not the
  runtime/engine version.

## Control safety and evidence

Validate observation keys, two `uint8[224,224,3]` images, seven joints, one
normalized gripper value, and a bounded prompt before serialization. Validate
the response as finite absolute targets shaped `[15,8]`; enforce Franka joint
limits, gripper `[0,1]`, per-step joint delta, execution-step cap, connect and
inference timeouts, and bounded reconnect backoff.

Timeout, malformed MessagePack, wrong shape/dtype, non-finite or out-of-range
values, camera failure, rate-limit failure, and exhausted reconnects must yield
zero applied targets and a failed-no-action report. Capture sanitized camera
shapes/backend, compute capability, action shape, executed-target count, asset
identity, and fail-closed status. Never record frames containing customer data
without explicit artifact authorization.
