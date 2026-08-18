# Antioch OpenPI Franka camera bridge

This project is the Antioch-hosted wrapper for the shared implementation in
`npa.workbench.antioch.openpi_isaac`. It must run in a private Antioch Isaac Lab
engine after the project image installs the exact NPA revision being validated.
The OpenPI endpoint is supplied as `OPENPI_POLICY_HOST` and remains private.

Pass `NPA_SOURCE_REF=<exact-40-character-commit>` as the project build argument.
The engine and SDK are pinned to `0.3.47`; its derived image contains the vendor
Isaac runtime and must remain in the operator's private Antioch registry.

The project does not contain credentials. Authenticate with Antioch outside the
project, mount or inject the vendor-supported runtime session, and submit suite
`openpi_franka_smoke`. The operator must also provide the run-scoped NVIDIA and
Gemma acceptances through their respective secret channels. Never add either
acceptance, an Antioch session, or policy/checkpoint bytes to this directory.

Without an Antioch account session, validate the same bridge code in the
operator-owned Kubernetes stack rendered by:

```bash
npa workbench antioch openpi-stack --help
```

The rendered Kubernetes bridge waits for policy health before launching Isaac,
then fails closed on any protocol or control error. Remove all four run objects
with the same arguments plus `--delete` after collecting the report.
