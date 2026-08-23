# Antioch + OpenPI live example

This public-source project runs a real Isaac Sim Franka scene in an Antioch
livestream and sends its two current 224x224 camera frames and robot state to an
external OpenPI pi0.5 DROID policy. It applies only validated finite `[15, 8]`
chunks, enters safe hold on stale/malformed/unsafe responses, and reconnects with
bounded exponential backoff. Antioch telemetry and a viewport overlay report the
live counters; neither is reconstructed from a recording.

The checked-in project ID is deliberately unusable. The live controller creates a
private runtime copy with an assigned Antioch project ID, starts the supported sim
service, and copies `ca.crt`, `api-key`, and `endpoint.json` into the running sim
service with `antioch services cp`. Credentials are never passed through scenario
parameters, tmux commands, Git, or images.

The scenario is continuous within one Antioch run. Since scenario runs have a
finite supported timeout, the controller renews them in tmux until explicitly
stopped. A renewal resets the simulated episode and briefly interrupts the
viewport; it is service continuity, not one infinitely lived simulator process.

The source is original Apache-2.0 NPA example code. Isaac Sim is supplied by the
Antioch-managed runtime under the operator-accepted NVIDIA terms. OpenPI source and
the pi0.5 checkpoint remain governed by their own runtime contracts; no model
weights or proprietary simulator payloads are present here.
