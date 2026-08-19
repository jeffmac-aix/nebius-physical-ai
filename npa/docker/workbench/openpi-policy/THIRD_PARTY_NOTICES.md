# Third-party inventory inputs: npa-openpi-policy

Engineering review date: 2026-08-19. The built-image SBOM is the exact installed
inventory; package license files remain in their Python distribution metadata and
Debian copyright directories.

| Component | Immutable input | Shipped? | Classification |
| --- | --- | --- | --- |
| OpenPI source | `Physical-Intelligence/openpi@15a9616a00943ada6c20a0f158e3adb39df2ccac` | yes | Apache-2.0; upstream `LICENSE` remains in `/opt/byof`. |
| LeRobot source dependency | `huggingface/lerobot@0cf864870cf29f4738d3ade893e6fd13fbd7cdb5` via the pinned OpenPI `uv.lock` | yes | Apache-2.0. |
| CUDA base | CUDA 12.8.1 cuDNN Ubuntu 24.04, digest-pinned in the Dockerfile | yes | Redistributable CUDA container runtime under NVIDIA's CUDA/container terms; it is not Isaac, Omniverse Kit, or a host driver. |
| Python dependency closure | exact upstream `uv.lock` SHA-256 `793488b5…37d74` | yes | Individual OSS licenses retained in installed metadata and inventoried by the release SBOM. |
| pi0.5-DROID Polaris / Gemma-derived checkpoint | pinned GCS object-generation manifest `8b97388a…85218` | no; runtime cache only | Operator-scoped use under the Gemma Terms of Use and Prohibited Use Policy. No checkpoint, tokenizer/model payload, or populated cache is an image input. |
| Credentials, operator data, generated actions | operator supplied/runtime generated | no | Never build inputs or image-layer contents. |

The OpenPI checkout includes `LICENSE_GEMMA.txt` because it documents the terms
that govern separately downloaded model material. Its presence does not mean the
image contains that material or grant model redistribution rights.
