# Antioch + OpenPI live example

This public-source project runs a real Isaac Sim Franka scene in an Antioch
livestream and sends its two current 224x224 camera frames and robot state to an
external OpenPI pi0.5 DROID policy. It applies only validated finite `[15, 8]`
chunks, enters safe hold on stale/malformed/unsafe responses, and reconnects with
bounded exponential backoff. Antioch telemetry and a viewport overlay report the
live counters; neither is reconstructed from a recording.

The client uses a 90-second response-age safety deadline because a cold request
can take tens of seconds even though warmed B200 requests are normally tens of
milliseconds. The reviewed `pi05_droid_jointpos_polaris` output contract is seven
absolute arm joints plus one raw gripper command. Upstream `DroidOutputs` returns
that eighth dimension unchanged, so the client requires it to be finite and
saturates it to the normalized actuator range before mapping it into the Franka
finger range. The saturation count is reported alongside strict shape, finite,
arm-limit, and per-target step validation for every `[15, 8]` chunk. Five returned
targets are applied at a nominal 15 Hz. The observation-to-action loop is
best-effort and not hard real time.

The checked-in project ID is deliberately unusable. The live controller creates a
private runtime copy with an assigned Antioch project ID, starts the supported sim
service, and copies a 0600 run bundle into the running sim service with
`antioch services cp`. The bundle contains the B200 gateway CA/API key/endpoint plus a
separate short-lived CA, certificate, key, and API key for the service-side bridge.
Credentials are never passed through scenario parameters, tmux commands, Git, or
images.

The sim declares an Antioch-managed port that is reachable only at the operator's
localhost while services are up. A bounded authenticated WSS rendezvous runs in
the persistent `sim` service. The streamed scenario connects to its `simulation`
role first; a second process in the same named tmux session connects to its
`operator` role and only then connects to the persistent B200 gateway by verified
WSS on port 443. This double-WSS route is the supported fallback when the
simulation network cannot route directly to a managed load-balancer address; it
is not a public unauthenticated proxy. Both legs reconnect independently and the
operator relay writes only fixed counters and error classes to its private state
file.

The project Dockerfile adds only pinned `msgpack` and `websockets` wire-protocol
dependencies to Antioch's version-matched Isaac Sim base. The small local codec
is adapted from the pinned Apache-2.0 OpenPI client and rejects object arrays;
neither OpenPI model code nor weights are included in the sim image.
The controller copies the reviewed scenario, codec, and bounded WSS bridge through supported
`services cp` and verifies their readability before dispatch, avoiding dependence
on a retained remote build or source-sync cache. Dockerfile changes retain a
separate rebuild rule, and the baked bridge entrypoint has its own explicit
rebuild rule.

The scenario is continuous within one Antioch run. Since scenario runs have a
finite supported timeout, the controller renews them in tmux until explicitly
stopped. A renewal resets the simulated episode and briefly interrupts the
viewport; it is service continuity, not one infinitely lived simulator process.
The supervisor also verifies every private bundle file and swaps a complete
staged generation into place atomically because Antioch may legitimately recreate
the sim container. A machine recycle can also discard its machine-local built
service image; in that case the supervisor runs the supported service build before
bringing the exact service back, re-staging source and credentials, or dispatching
another scenario. The bridge is the detached service container's entrypoint and
waits for the supported runtime bundle staging before accepting traffic. Separate
bridge-health and relay tmux windows remain supervised across replacement; the
health window uses only short service-exec socket probes, while the bridge remains
bound to the replaceable service container instead of the CLI exec lifetime. If
Antioch accepts an interactive scenario but the
foreground CLI loses attachment while reporting the occupied stream lease, the
supervisor reconciles the exact run through supported project-scoped
`scenario list` and `machine status` JSON. It adopts only the matching stream
owner, waits for terminal state, and never dispatches a duplicate renewal while
that accepted run is active.

Mission Control can report the livestream as `ready` until an authenticated
viewer opens the supported console link. Isaac's first rendered camera frame may
wait at that boundary; the controller never fabricates a viewer session or reads
browser authentication storage.

The source is original Apache-2.0 NPA example code. Isaac Sim is supplied by the
Antioch-managed runtime under the operator-accepted NVIDIA terms. OpenPI source and
the pi0.5 checkpoint remain governed by their own runtime contracts; no model
weights or proprietary simulator payloads are present here.
