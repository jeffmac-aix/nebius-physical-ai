# npa-sonic: SONIC whole-body humanoid locomotion, with NVIDIA Isaac fetched at FIRST RUN.
#
# This image contains NO NVIDIA Isaac Sim or Isaac Lab code. It used to be built FROM
# nvcr.io/nvidia/isaac-lab (baked variant) or to pip-install 26 isaacsim wheels at build
# time (k8s variant); either way Omniverse Kit ended up in the layers, which made the
# image non-redistributable. Isaac is now downloaded from https://pypi.nvidia.com on first
# run, into a cache volume, under the OPERATOR's own EULA acceptance. See
# npa/docker/workbench/common/isaac_bootstrap.sh and
# docs/workbench/container-packaging.md.
#
# What IS baked, and is fine to redistribute:
#   * Docker Hub's CUDA base (baked variant) or npa-base (k8s variant)
#   * PyTorch and the OSS dependency closure Isaac needs
#   * gear_sonic from NVlabs/GR00T-WholeBodyControl at a pinned commit. Its source is
#     Apache-2.0 (the repo is dual-licensed: source Apache-2.0, model weights under the
#     NVIDIA Open Model License, and the weights are downloaded at run time by
#     download_from_hf.py with the operator's own HF token - never baked).
#   * gear_sonic installs cleanly WITHOUT Isaac Lab; its own pyproject says
#     "Training: full RL training stack (Isaac Lab must be installed separately)".
#
# Variants (npa/docker/workbench/sonic/build.sh):
#   baked  BASE_IMAGE = digest-pinned Docker Hub CUDA; this Dockerfile builds the
#          python3.11 + torch cu128 layer itself.
#   k8s    BASE_IMAGE = ${REGISTRY}/npa-base:cuda13-b300-...; reuses that image's
#          python3.11 venv and its cu130 torch (NPA_ISAAC_SKIP_TORCH=1).
#
ARG BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04@sha256:ad6d59a3bbf3e82c1c849c9ac09cfc2a3e0bbb8655042fd899be6681b3fe2a85
FROM --platform=linux/amd64 ${BASE_IMAGE}

ARG BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04@sha256:ad6d59a3bbf3e82c1c849c9ac09cfc2a3e0bbb8655042fd899be6681b3fe2a85
ARG ISAAC_LAB_VERSION=2.3.2.post1
ARG ISAAC_SIM_VERSION=5.1.0.0
ARG ISAAC_LAB_SRC_COMMIT=37ddf626871758333d6ed89cf64ad702aef127d0
ARG SONIC_REPO_URL=https://github.com/NVlabs/GR00T-WholeBodyControl.git
ARG SONIC_REPO_REF=0a87181c9106d0e49293400714b157676e0ec664
ARG SONIC_VERSION=0.1.2
# The Isaac interpreter is always the bootstrap shim; the baked venv is separate.
ARG ISAAC_LAB_PYTHON=/isaac-sim/python.sh
ARG NPA_ISAAC_VENV=/opt/npa/sim/venv
ARG NPA_ISAAC_SKIP_TORCH=0
ARG REQUIRE_TORCH_SM120=1
ARG NPA_DRIVER_PROVISIONING=host-mounted
ARG NPA_CUDA_ARCHITECTURES=sm80,sm90,sm120
ARG NPA_RUNTIME_USER=ubuntu
ARG BUILD_SONIC_DEPLOY=0

LABEL npa.tool="sonic" \
      npa.version="${SONIC_VERSION}" \
      npa.base_image="${BASE_IMAGE}" \
      npa.driver_provisioning="${NPA_DRIVER_PROVISIONING}" \
      npa.runtime_user="${NPA_RUNTIME_USER}" \
      npa.cuda_architectures="${NPA_CUDA_ARCHITECTURES}" \
      npa.isaac_provisioning="runtime-fetch" \
      npa.architecture="no-baked-omniverse-kit"

# NOTE the absence of ACCEPT_EULA / ISAACSIM_ACCEPT_EULA / OMNI_KIT_ACCEPT_EULA /
# PRIVACY_CONSENT, and of PIP_EXTRA_INDEX_URL=https://pypi.nvidia.com. All of those used
# to be baked here. Acceptance is now the operator's to give at run time (the whole legal
# mechanism), and leaving NVIDIA's index out of the image's pip config means a stray
# `pip install` can never quietly re-introduce a proprietary wheel.
ENV DEBIAN_FRONTEND=noninteractive \
    ISAAC_LAB_VERSION=${ISAAC_LAB_VERSION} \
    ISAAC_SIM_VERSION=${ISAAC_SIM_VERSION} \
    NPA_ISAAC_LAB_SRC_COMMIT=${ISAAC_LAB_SRC_COMMIT} \
    NPA_ISAAC_CACHE_DIR=/opt/isaac-cache \
    NPA_ISAAC_BASE_PYTHON=${NPA_ISAAC_VENV}/bin/python \
    NPA_IMAGE_PYTHON=${NPA_ISAAC_VENV}/bin/python \
    NPA_ISAAC_WHEELS_FILE=/opt/npa/docker/workbench/common/isaac-nvidia-wheels.txt \
    ISAAC_LAB_PYTHON=${ISAAC_LAB_PYTHON} \
    SONIC_HOME=/opt/sonic \
    SONIC_REPO_REF=${SONIC_REPO_REF} \
    NPA_CUDA_ARCHITECTURES=${NPA_CUDA_ARCHITECTURES} \
    NVIDIA_DRIVER_CAPABILITIES=all \
    __GLX_VENDOR_LIBRARY_NAME=nvidia \
    OMNI_USER_DIR=/tmp/isaac-sim-cache \
    OMNI_LOG_DIR=/tmp/isaac-sim-cache/logs \
    XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    PYTHONUNBUFFERED=1

SHELL ["/bin/bash", "-lc"]
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      clang \
      cmake \
      curl \
      git \
      git-lfs \
      libgtest-dev \
      nlohmann-json3-dev \
      pkg-config \
      wget \
    && rm -rf /var/lib/apt/lists/* \
    && git lfs install --system

COPY docker/workbench/common /opt/npa/docker/workbench/common

# python3.11 + (optionally) torch + the OSS closure Isaac needs + the bootstrap and the
# /isaac-sim/python.sh shim. The k8s variant passes NPA_ISAAC_SKIP_TORCH=1 so the base
# image's CUDA-matched torch is reused rather than replaced.
#
# NOTE: no NVIDIA driver userspace is installed. Verified on an RTX PRO 6000 pod: with
# NVIDIA_DRIVER_CAPABILITIES=all the container runtime injects libEGL_nvidia,
# libGLX_nvidia, libnvidia-glcore and the Vulkan ICD, and `vulkaninfo --summary` reports
# the discrete GPU at driver 580.95.05. The baked variant used to pin and hold
# libnvidia-*-580 packages; that was both unnecessary and a separate redistribution
# question from Omniverse Kit.
RUN chmod +x /opt/npa/docker/workbench/common/*.sh \
    && NPA_RUNTIME_USER="${NPA_RUNTIME_USER}" \
       NPA_ISAAC_VENV="${NPA_ISAAC_VENV}" \
       NPA_ISAAC_SKIP_TORCH="${NPA_ISAAC_SKIP_TORCH}" \
       REQUIRE_TORCH_SM120="${REQUIRE_TORCH_SM120}" \
       NPA_INSTALL_SKYPILOT_PREREQS=1 \
       /opt/npa/docker/workbench/common/install_isaac_runtime_base.sh

RUN mkdir -p /opt/npa/docker/workbench/sonic /tmp/npa-sonic-output
COPY docker/workbench/sonic/requirements.txt /opt/npa/docker/workbench/sonic/requirements.txt
RUN "${NPA_ISAAC_VENV}/bin/python" -m pip install --no-cache-dir \
      -r /opt/npa/docker/workbench/sonic/requirements.txt

RUN "${NPA_ISAAC_VENV}/bin/python" -m pip install --no-cache-dir uv \
    && curl -fsSL https://just.systems/install.sh | bash -s -- --to /usr/local/bin

# gear_sonic source is Apache-2.0. The sparse checkout excludes motionbricks/out, and the
# build asserts below that no model weights came along with the LFS pull - the weights are
# under the NVIDIA Open Model License and must be fetched at run time by the operator.
RUN git clone --filter=blob:none --no-checkout "${SONIC_REPO_URL}" "${SONIC_HOME}" \
    && cd "${SONIC_HOME}" \
    && git sparse-checkout init --no-cone \
    && git sparse-checkout set \
      "/download_from_hf.py" \
      "/gear_sonic/**" \
      "/gear_sonic_deploy/**" \
      "/decoupled_wbc/**" \
      "/LICENSE" \
    && git checkout "${SONIC_REPO_REF}" \
    && test "$(git rev-parse HEAD)" = "${SONIC_REPO_REF}" \
    && (git lfs pull --include="download_from_hf.py,gear_sonic/**,gear_sonic_deploy/**,decoupled_wbc/**" --exclude="motionbricks/out/**" || true) \
    && rm -rf "${SONIC_HOME}/.git"

# Constrain torch and friends so gear_sonic's transitive requirements cannot replace the
# CUDA-matched build with a generic PyPI one.
RUN "${NPA_ISAAC_VENV}/bin/python" - > /tmp/npa-torch-constraints.txt <<'PY'
from importlib import metadata

for dist in metadata.distributions():
    name = (dist.metadata["Name"] or "").lower().replace("_", "-")
    if name in {"torch", "torchvision", "torchaudio", "triton"} or name.startswith("nvidia-"):
        print(f"{name}==={dist.version}")
PY

RUN "${NPA_ISAAC_VENV}/bin/python" -m pip install --no-cache-dir \
      -c /tmp/npa-torch-constraints.txt \
      -e "${SONIC_HOME}/gear_sonic[training,teleop,sim]" \
    && rm -f /tmp/npa-torch-constraints.txt

# `import sonic` is an alias the workbench CLI and smokes rely on.
RUN "${NPA_ISAAC_VENV}/bin/python" - <<'PY'
import pathlib
import site

for site_dir in site.getsitepackages():
    path = pathlib.Path(site_dir) / "sonic.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("from gear_sonic import *\n", encoding="utf-8")
PY

COPY docker/workbench/sonic/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Build-time verification. Note what is NOT checked here any more: `import isaaclab` and
# `import isaaclab.app` used to be asserted at build time, which is exactly the assertion
# a runtime-fetch image must not be able to make. The bootstrap verifies them after it
# installs (isaac_bootstrap.sh verify_tree), and the golden eval exercises them on a GPU.
RUN "${NPA_ISAAC_VENV}/bin/python" - <<'PY'
import importlib
import os
from importlib import metadata
from pathlib import Path

for module in ("gear_sonic", "sonic", "torch", "numpy", "scipy"):
    importlib.import_module(module)

# No NVIDIA Isaac bytes may ship in this image; that is the whole point.
leaked = sorted(
    dist.metadata["Name"]
    for dist in metadata.distributions()
    if (dist.metadata["Name"] or "").lower().replace("_", "-").startswith(("isaacsim", "isaaclab"))
)
if leaked:
    raise SystemExit(f"FATAL: Isaac packages baked into the image: {leaked}")

# No gated model weights either: those are the operator's to download at run time with
# their own HF token (download_from_hf.py), under the NVIDIA Open Model License.
weights = [
    str(path)
    for path in Path(os.environ["SONIC_HOME"]).rglob("*")
    if path.is_file() and path.suffix in {".pt", ".pth", ".safetensors", ".ckpt", ".onnx"}
]
if weights:
    raise SystemExit(f"FATAL: model weights baked into the image: {weights[:10]}")

if os.environ.get("REQUIRE_TORCH_SM120") == "1":
    import torch

    # See install_isaac_runtime_base.sh: a driverless builder reports an empty arch list,
    # so the build asserts the CUDA toolkit carries sm_120 and the per-device arch check
    # happens on GPU (isaac-bootstrap verify / the golden eval).
    arches = sorted(set(torch.cuda.get_arch_list()))
    if arches:
        if "sm_120" not in arches:
            raise SystemExit(f"expected PyTorch with sm_120 kernels, found {arches}")
        print(f"NPA_SONIC_TORCH_SM120_OK torch={torch.__version__} arches={arches}")
    else:
        cuda_version = torch.version.cuda or ""
        major, _, minor = cuda_version.partition(".")
        if (int(major or 0), int(minor or 0)) < (12, 8):
            raise SystemExit(
                f"torch is built against CUDA {cuda_version!r}, which has no sm_120 "
                f"kernels; RTX PRO 6000 Blackwell needs CUDA >= 12.8"
            )
        print(f"NPA_SONIC_TORCH_CUDA_SUPPORTS_SM120_OK cuda={cuda_version}")
print("NPA_SONIC_BUILD_IMPORTS_OK no baked Isaac, no baked weights")
PY

# The C++ deploy stack is included for VM/container deployment paths, but TensorRT and
# ONNX Runtime discovery are environment-sensitive. Keep the default train/smoke image
# focused; deploy images can opt into this compile step.
RUN if [ "${BUILD_SONIC_DEPLOY}" = "1" ]; then \
      cd "${SONIC_HOME}/gear_sonic_deploy" \
      && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
      && cmake --build build -j"$(nproc)"; \
    else \
      echo "Skipping optional SONIC C++ deploy build (BUILD_SONIC_DEPLOY=0)"; \
    fi

# Kit writes caches, logs and shader compilations at run time. These used to live under
# /isaac-sim/kit/{logs,cache,data}, which existed because the NVIDIA base put Isaac Sim
# there. /isaac-sim now holds only our python.sh shim, so they go to container scratch.
RUN id -u "${NPA_RUNTIME_USER}" >/dev/null 2>&1 || useradd -m -s /bin/bash -u 1000 "${NPA_RUNTIME_USER}" \
    && install -d -m 0775 -o root -g isaac-sim \
      /tmp/isaac-sim-cache \
      /tmp/isaac-sim-cache/logs \
    && install -d -m 0700 -o "${NPA_RUNTIME_USER}" -g "${NPA_RUNTIME_USER}" /tmp/xdg-runtime \
    && chown "${NPA_RUNTIME_USER}:${NPA_RUNTIME_USER}" /opt/sonic \
    && chown -R "${NPA_RUNTIME_USER}:${NPA_RUNTIME_USER}" /tmp/npa-sonic-output

WORKDIR /opt/sonic
USER ${NPA_RUNTIME_USER}
ENTRYPOINT ["/entrypoint.sh"]
CMD ["smoke"]
