# Image ↔ Nebius GPU compatibility matrix

Every Workbench container image against every Nebius GPU platform, and — separately — which of those cells has actually been run on real hardware.

**Last measured:** 2026-08-03

Two things are deliberately kept apart here, because conflating them is how "Blackwell ready" claims go wrong:

- **Can it execute there?** Decided by the architectures baked into the image's torch wheel plus any source-compiled CUDA extensions. Measurable without a GPU.
- **Has it been proven there?** Only a real capability run on that GPU answers this. An import check is not a proof — see [the flash-attn finding](#the-import-check-that-lied).

Machine-readable source of record: [`npa/docker/workbench/blackwell-dc-images.json`](../../npa/docker/workbench/blackwell-dc-images.json). Companion runbook: [Blackwell datacenter image compatibility](blackwell-datacenter-image-compatibility.md).

## Nebius GPU platforms

| GPU | Platform id | Family | Compute capability | SM |
| --- | --- | --- | --- | --- |
| L40S | `gpu-l40s-a` / `gpu-l40s-d` | Ada | 8.9 | `sm_89` |
| H100 | `gpu-h100-sxm` | Hopper | 9.0 | `sm_90` |
| H200 | `gpu-h200-sxm` | Hopper | 9.0 | `sm_90` |
| RTX PRO 6000 Blackwell | `gpu-rtx6000` | Blackwell workstation (GB20x) | 12.0 | `sm_120` |
| B200 | `gpu-b200-sxm` | Blackwell datacenter (GB100) | 10.0 | `sm_100` |
| B300 (Blackwell Ultra) | `gpu-b300-sxm` | Blackwell datacenter (GB300) | 10.3 | `sm_103` |

H100 and H200 are both `sm_90`, so they share a column below.

Also offered: `gpu-gb300` (Grace-Blackwell Ultra). Its GPU is the same `sm_103`, but the host is aarch64, and the x86_64 workbench images do not run there.

Two compatibility rules govern every cell:

- **SASS does not cross a CUDA major.** `sm_120` (major 12) and `sm_100`/`sm_103` (major 10) binaries are mutually incompatible, so a green run on RTX PRO 6000 says nothing about B200/B300.
- **Within a major, forward compatibility holds.** `sm_86` SASS runs on an `sm_89` device (L40S), and `sm_100` SASS runs on an `sm_103` device (B300). Not the reverse.

## Measured torch stack per image

`arch_list` is `torch._C._cuda_getArchFlags()` read out of the published image. It is fixed when the wheel is built — `TORCH_CUDA_ARCH_LIST` cannot change it — so it decides which GPUs the image can execute on. Reproduce any row with `npa/scripts/validate_blackwell_image.sh <image> --target b200`.

| Image | Tag measured | Torch / CUDA | Measured SASS set | Covers `sm_100`? |
| --- | --- | --- | --- | --- |
| `npa-base` | `cuda13-b300-sm80-sm90-sm100-sm103-sm120-20260802T234708Z` | 2.9.0+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` + `compute_120` PTX | yes |
| `npa-lerobot` | `…-0.5.1-…-20260803T000551Z` | 2.9.0+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` + `compute_120` PTX | yes |
| `npa-lerobot-policy` | `0.1.1` | 2.12.1+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` | yes |
| `npa-lancedb` | `0.30.3` | 2.12.1+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` | yes |
| `npa-detection-training` | `bdd100k-golden-eval-smoke-20260614T210000Z` | 2.12.1+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` | yes |
| `npa-cosmos3-reason` | `…-3.0.1-…-20260803T000551Z` | 2.9.0+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` + `compute_120` PTX | yes |
| `npa-genesis` | `…-0.4.6-…-20260803T000551Z` | 2.9.0+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` + `compute_120` PTX | yes |
| `npa-envgen` / `npa-reference-policy` / `npa-lerobot-vlm-rl` / `npa-loop-eval` | `…-20260803T000551Z` | inherited 2.9.0+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` + `compute_120` PTX | yes |
| `npa-sonic` | `…-0.1.2-k8s-runtime-…-20260803T012052Z` | 2.9.0+cu130 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` + `compute_120` PTX | yes |
| `npa-cosmos` | `cu128-torch27-sm100-1.0.9-20260803T002017Z` | 2.7.0+cu128 | `sm_75 sm_80 sm_86 sm_90 sm_100 sm_120` + `compute_120` PTX | yes |

The old `npa-cosmos:1.0.9` cu126 image stopped at Hopper. Its additive cu128/torch-2.7 replacement now carries `sm_100`, and the custom kernels passed on B200. Predict2 v1.0.9 still has a separate software allowlist that rejects L40S, RTX PRO 6000, and B300 before dispatch, so wheel coverage alone does not make those cells supported. The rebuilt Genesis/sim2real/SONIC images likewise have a correct torch arch set while their datacenter verdicts remain blocked on Taichi or the NVIDIA Isaac vendor stack. Not measured yet: `npa-workbench-cuda-base` (covered through its children), `npa-isaac-lab`, and `npa-groot`.

## Compatibility matrix

| Image | L40S `sm_89` | H100 / H200 `sm_90` | RTX PRO 6000 `sm_120` | B200 `sm_100` | B300 `sm_103` |
| --- | --- | --- | --- | --- | --- |
| `npa-base` | supported | **verified** [1] | **verified** [2] | **verified** [5] | **verified** [6] |
| `npa-workbench-cuda-base` | supported | supported | supported | supported | supported |
| `npa-lerobot` | supported | **verified** [11] | supported | **verified** [10] | **verified** [4] |
| `npa-lerobot-policy` | supported | supported | supported | supported | supported |
| `npa-lancedb` | supported | supported | supported | supported | supported |
| `npa-detection-training` | supported | supported | supported | **verified** [7] | supported |
| `npa-cosmos3` | supported | supported | supported | supported | supported |
| `npa-cosmos3-reason` | supported | supported | **verified** [13] | **verified** [12] | supported |
| `npa-cosmos2-transfer` | supported | supported | supported | **verified** [9] | supported |
| `npa-cosmos` | blocked (Predict2 allowlist) | supported | blocked (Predict2 allowlist) | **verified** [19] | blocked (Predict2 allowlist) |
| `npa-genesis` | supported | supported | **verified** [14] | blocked (Taichi) | blocked (Taichi) |
| `npa-envgen` | supported | supported | **verified** [15] | blocked (Taichi) | blocked (Taichi) |
| `npa-reference-policy` | supported | supported | **verified** [16] | blocked (Taichi) | blocked (Taichi) |
| `npa-loop-eval` | supported | supported | **verified** [18] | blocked (Taichi) | blocked (Taichi) |
| `npa-lerobot-vlm-rl` | supported | supported | **verified** [17] | blocked (Taichi) | blocked (Taichi) |
| `npa-isaac-lab` | supported | supported (headless) | supported | blocked | blocked |
| `npa-sonic` | supported | supported (headless) | supported | blocked | blocked |
| `npa-sonic-mujoco` | supported | supported (headless) | supported | blocked | blocked |
| `npa-groot` | supported | supported | supported | blocked | blocked |
| `npa-cosmos-curate` | CPU | CPU | CPU | CPU | CPU |
| `npa-cosmos-evaluator` | CPU | CPU | CPU | CPU | CPU |
| `npa-fiftyone` | CPU | CPU | CPU | CPU | CPU |
| `npa-retargeting` | CPU | CPU | CPU | CPU | CPU |
| `npa-rerun-viewer` | CPU | CPU | CPU | CPU | CPU |
| `npa-lichtblick` | CPU | CPU | CPU | CPU | CPU |
| `npa-foxglove-embed` | CPU | CPU | CPU | CPU | CPU |
| `npa-sonic-export` | CPU | CPU | CPU | CPU | CPU |

**verified** — run on that GPU with a real capability smoke; see [Verified runs](#verified-runs).
**supported** — the toolchain can execute there, but no capability run on that GPU has been recorded.
**no SASS** — measured wheel does not carry the architecture; the image cannot run there until it is ported.
**blocked** — an upstream dependency does not support the architecture. Reason and tracking link are in the manifest's per-image fields or `known_gaps`.
**CPU** — CPU-only image. It runs on a host with any of these GPUs; only node-pool scheduling matters.

### Rendering is not portable across these columns

Isaac Lab and SONIC rasterized rendering needs RT cores. L40S and RTX PRO 6000 have them; H100, H200, B200, and B300 do not. The "supported (headless)" cells above mean state-based training only. `npa.workbench.sonic.routing` enforces this and rejects a render workload routed to a datacenter part.

### Blackwell datacenter hardware status

Managed-Kubernetes nodes were placed successfully for both B200 in us-central1 and B300 in uk-south1 on 2026-08-03. The temporary nodes enabled the first current-hardware validation runs below. Cells remain merely **supported** unless that exact image completed its real capability smoke; hardware availability alone does not flip a cell.

## Verified runs

| # | Date | Image | GPU | What ran | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | 2026-08-02 | `npa-base` `…-20260802T181419Z` | H100 80GB HBM3 (`sm_90`) | positive arch check, negative cross-major check, capability smoke (bf16 matmul, torch SDPA, flash-attn-4 CuTe forward vs SDPA) | `ALL_GPU_VALIDATION_PASSED`; flash-attn max abs error 0.00186 |
| 2 | 2026-08-02 | `npa-base` `…-20260802T181419Z` | RTX PRO 6000 Blackwell Server Edition (`sm_120`) | same three checks | `ALL_GPU_VALIDATION_PASSED`; flash-attn recorded as the known TMA gap |
| 3 | 2026-05-14 | `npa-base` cuda13-b300 | 8× B300 (`sm_103`), driver 580.126.09 | torch import, device capability `(10, 3)`, flash-attn-4 forward pass, NCCL init | PASS — [B300 validation matrix](../b300-validation-matrix.md) |
| 4 | 2026-05-14 | `npa-lerobot` cuda13-b300 | B300 (`sm_103`) | ACT on `lerobot/pusht_image`, batch 8, 100 steps | PASS, 71 s wall — [B300 validation matrix](../b300-validation-matrix.md) |
| 5 | 2026-08-03 | `npa-base` `…-20260802T181419Z` | NVIDIA B200 (`sm_100`), driver 580.159.04 | positive native-SASS check, negative `sm_120` cross-major check, bf16 matmul, torch SDPA, flash-attn-4 CuTe forward vs SDPA | capability `(10, 0)`, `sass_covered=True`, cross-major check failed as required, flash-attn max abs error 0.00206, `ALL_GPU_VALIDATION_PASSED` |
| 6 | 2026-08-03 | `npa-base` `…-20260802T181419Z` | NVIDIA B300 SXM6 AC (`sm_103`) | positive same-major SASS check, negative `sm_120` cross-major check, bf16 matmul, torch SDPA, flash-attn-4 CuTe forward vs SDPA | capability `(10, 3)`, `sm_100` SASS covered `sm_103`, cross-major check failed as required, flash-attn max abs error 0.00206, `ALL_GPU_VALIDATION_PASSED` |
| 7 | 2026-08-03 | `npa-detection-training:bdd100k-golden-eval-smoke-20260614T210000Z` | NVIDIA B200 (`sm_100`) | real Faster R-CNN forward, backward, and optimizer step on synthetic detector data | `DETECTOR_TRAIN_STEP_PASSED`; classifier, box-regression, objectness, and RPN losses produced |
| 8 | 2026-08-03 | `npa-base` `…-20260802T234708Z` | NVIDIA B300 SXM6 AC (`sm_103`) | repeated positive same-major SASS check, negative `sm_120` cross-major check, bf16 matmul, torch SDPA, and flash-attn-4 CuTe forward on the rebuilt/published image | capability `(10, 3)`, `sm_100` SASS covered `sm_103`, flash-attn max abs error 0.00206, `ALL_GPU_VALIDATION_PASSED` |
| 9 | 2026-08-03 | `npa-cosmos2-transfer:2.5.1-skypilot-ready-20260801T053000Z` | NVIDIA B200 (`sm_100`) | real depth-conditioned video-to-video transfer, including two 35-step generation passes and prompt/video guardrails | PASS; generated `robot_depth.mp4` (3,891,548 bytes) |
| 10 | 2026-08-03 | rebuilt `npa-lerobot` `…-20260803T000551Z` | NVIDIA B200 (`sm_100`) | base and child native-SASS checks, datacenter flash-attn-4 CuTe kernel, then official ACT PushT: 50 training steps, checkpoint, and one evaluation episode | PASS; 5/5 functional checks and flash-attn max abs error 0.00206 |
| 11 | 2026-08-03 | same rebuilt `npa-lerobot` | NVIDIA H100 (`sm_90`) | same ACT train→checkpoint→evaluation smoke plus native H100 SASS and flash-attn | PASS; 5/5 functional checks and flash-attn max abs error 0.00186 |
| 12 | 2026-08-03 | rebuilt `npa-cosmos3-reason` `…-20260803T000551Z` | NVIDIA B200 (`sm_100`) | base validators plus a real gated `nvidia/Cosmos-Reason2-8B` VLM reason pass over two frames | PASS; datacenter flash-attn kernel passed and the VLM emitted a completed judgment |
| 13 | 2026-08-03 | same rebuilt `npa-cosmos3-reason` | RTX PRO 6000 (`sm_120`) | native SASS/base controls plus the same real VLM reason pass | PASS; expected non-TMA flash-attn gap recorded, real VLM inference completed |
| 14 | 2026-08-03 | rebuilt `npa-genesis` `…-20260803T000551Z` | RTX PRO 6000 (`sm_120`) | native SASS/base controls, raw environment generation, Genesis CUDA scene construction, and a physics step | PASS; `gs.cuda` on the physical GPU |
| 15 | 2026-08-03 | rebuilt `npa-envgen` `…-20260803T000551Z` | RTX PRO 6000 (`sm_120`) | validators, real environment generation, and Genesis CUDA step | PASS |
| 16 | 2026-08-03 | rebuilt `npa-reference-policy` `…-20260803T000551Z` | RTX PRO 6000 (`sm_120`) | validators and a real reference-policy rollout in a generated environment | PASS |
| 17 | 2026-08-03 | rebuilt `npa-lerobot-vlm-rl` `…-20260803T000551Z` | RTX PRO 6000 (`sm_120`) | validators and a real VLM-guided RL step in Genesis | PASS |
| 18 | 2026-08-03 | rebuilt `npa-loop-eval` `…-20260803T000551Z` | RTX PRO 6000 (`sm_120`) | validators and a scored two-environment Franka pick-place rollout | PASS; rollout ran at 29.23 FPS |
| 19 | 2026-08-03 | `npa-cosmos:cu128-torch27-sm100-1.0.9-20260803T002017Z` | NVIDIA B200 (`sm_100`) | measured wheel arches, flash-attn 2.7.3 forward vs torch SDPA, and the exact Predict2 v1.0.9 `NeighborhoodAttention` module using the first shipped 2B Video2World NATTEN configuration | `ALL_COSMOS_CU128_KERNEL_VALIDATION_PASSED`; flash-attn max abs error 0.0009765625; NATTEN output `(1, 256, 4, 128)` |

Runs 1, 2, 5, 6, and 8 used [`npa/scripts/blackwell-gpu-validation-job.yaml`](../../npa/scripts/blackwell-gpu-validation-job.yaml). Each does three checks, so a pass means something: the target architecture must pass *with native SASS* (including same-major `sm_100` coverage for `sm_103`), a different CUDA major must **fail** (proving the checker cannot hand out a false "Blackwell ready" on the wrong GPU family), and then the capability smoke runs real kernels.

The job runs non-root with dropped capabilities and a read-only root filesystem. flash-attn-4's CuTe kernels JIT-compile at runtime, so `HOME` and every torch/CUDA cache point at a scratch `emptyDir`; the H100 run above confirms the kernel still compiles and executes under those constraints.

The original READY-set images were also tested before rebuild. `npa-lancedb:0.30.3` remains unverified because its published CLIP smoke fails on a current transformers return type; the superseded `npa-lerobot:0.5.1` failed a torchcodec/FFmpeg mismatch, and the superseded Cosmos3 Reason image lacked the functional smoke module. The rebuilt SONIC image passes its native-SASS controls and reaches real environment construction after fixing two undeclared upstream dependencies, but both cold and warm fine-tune attempts fail inside Isaac's runtime-fetched URDF extension while opening a temporary pelvis USD layer, before a checkpoint. Those failures are recorded in `validation_evidence`; they were not converted into verified cells. The Cosmos kernel run is not a generated-video claim: checkpoint-backed Video2World was attempted but remains unverified because the available Hugging Face identity received HTTP 403 for NVIDIA's gated checkpoint.

## The import check that lied

`npa-base`'s golden eval used to be `python -c "import torch; assert torch.cuda.is_available(); import flash_attn"`. It passed on every Blackwell part for months. The first time anyone executed the kernel — run 2 above — it failed on `sm_120`.

flash-attn-4's CuTe forward kernel partitions its epilogue with a TMA (Tensor Memory Accelerator) copy atom. TMA is a datacenter feature: `sm_90`, `sm_100`, and `sm_103` have it; RTX PRO 6000 does not. On `sm_120` the atom is `None` and the kernel raises `AttributeError: 'NoneType' object has no attribute '_trait'`.

What makes that conclusion safe rather than a guess:

- All four configurations tried (bf16/fp16 × head_dim 64/128 × seqlen 64/256) fail at the identical line — architecture-wide, not a config quirk.
- torch SDPA and bf16 matmul both pass on the same `sm_120` device, ruling out the GPU and the wheel.
- The same image passes the kernel on H100 (run 1) and the previously published image passed it on B300 (run 3) — both TMA-capable.
- The previously published `npa-base:cuda13-b300-sm80-sm90-sm120-latest` fails identically on `sm_120`, so this is pre-existing rather than a regression.

Callers on `sm_120` should use torch SDPA. The eval now runs [`gpu_capability_smoke.py`](../../npa/docker/workbench/base/cuda13-b300/scripts/gpu_capability_smoke.py), which executes the kernel; `--allow-no-tma` records the `sm_120` gap without excusing a TMA failure on a datacenter part, where it would be real.

## Reproducing a cell

```bash
# Wheel arch set only - no GPU needed, catches a Hopper-capped wheel for free
npa/scripts/validate_blackwell_image.sh "$NPA_REGISTRY/npa-lerobot:0.5.1" --target b200

# Full check on a real node
npa/scripts/validate_blackwell_image.sh "$NPA_REGISTRY/npa-base:<tag>" --target b300 --gpu

# On an already-deployed Kubernetes GPU pool
# (substitute NPA_IMAGE / NPA_GPU_INSTANCE / NPA_TARGET_* and apply)
kubectl apply -f npa/scripts/blackwell-gpu-validation-job.yaml
```
