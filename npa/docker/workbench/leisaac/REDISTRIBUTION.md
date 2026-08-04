# LeIsaac container redistribution

`npa-leisaac:0.4.0` is an additive image over NPA's public, runtime-fetch
`npa-isaac-lab:2.3.2.post1` image. It bakes the Apache-2.0 LeIsaac source at
commit `1651c321e9b0c1bb54233211fc7b3cd70d8373d5` and its OSS Python
dependencies. It does not bake Isaac Sim, Isaac Lab, Omniverse Kit, the NVIDIA
WebRTC browser client, or LeIsaac task assets.

At container startup, the operator must explicitly provide both
`OMNI_KIT_ACCEPT_EULA=YES` and `ISAACSIM_ACCEPT_EULA=YES`. Only then does the
shared NPA bootstrap fetch the pinned Isaac Sim 5.1.0.0 / Isaac Lab
2.3.2.post1 runtime. The service also fetches the two pinned LeIsaac v0.1.0
assets and NVIDIA WebRTC client 5.6.0 into its mounted cache, verifies their
cryptographic hashes, and writes `provenance.json`. The pristine client source
hash is checked before one exact transport-only patch makes numeric hosts use
WSS on signaling port 443; provenance records both source and served hashes.
EULA acceptance is never a Docker `ARG` or `ENV` and is not persisted in an
image layer.

The browser service uses upstream LeIsaac's software keyboard device. The
unlicensed `feetech-servo-sdk` package used only by physical SO101 leader
hardware is intentionally neither installed nor redistributed. NPA applies one
packaging-only patch that removes that mandatory dependency edge from upstream's
`pyproject.toml`; the lazy-loaded hardware implementation and the real
`SO101Keyboard`/task source are otherwise unchanged. The build runs `pip check`
after installation.

Before publication, scan the built image with
`npa/scripts/scan_image_omniverse_payload.py`. A valid result contains no
Omniverse/Isaac payload and no staged WebRTC client or task assets.
