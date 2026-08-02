# npa-isaac-lab: Isaac Lab RL simulation, with NVIDIA Isaac fetched at FIRST RUN.
#
# This image contains NO NVIDIA Isaac Sim or Isaac Lab code. It used to be built FROM
# nvcr.io/nvidia/isaac-lab, which baked Omniverse Kit into the layers and made the image
# non-redistributable: publishing it would have made us the third-party redistributor of
# NVIDIA-proprietary software. A download token could not have fixed that, because a
# token gates a download and the bytes were already in the layers.
#
# So the statement is made true instead of argued around. The base is Docker Hub's
# freely-redistributable CUDA image, and Isaac Sim + Isaac Lab are downloaded from
# https://pypi.nvidia.com on first run, into a cache volume, under the OPERATOR's own
# EULA acceptance - the same "the vendor delivers to the customer" pattern the workbench
# already uses for gated model weights (Cosmos, GR00T N1, Cosmos-Reason).
#
# Consequences an operator should know about, measured on RTX PRO 6000:
#   * first run downloads ~4.5 GB and materialises ~10.0 GiB of cache (111 s in-cluster)
#   * subsequent runs add 32 ms
#   * the container REFUSES TO START Isaac unless OMNI_KIT_ACCEPT_EULA=YES and
#     ISAACSIM_ACCEPT_EULA=YES are both set. That refusal is deliberate and load-bearing;
#     see npa/docker/workbench/common/isaac_bootstrap.sh.
#   * pre-warm the cache once per node/PVC to avoid paying the cold start per pod:
#     npa/docker/workbench/common/warm-isaac-cache.yaml
#
# Digest pinned 2026-07-31. Tag: nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04
# Freely redistributable, already covered by the base-image CVE scan matrix in
# .github/workflows/image-security-scan.yml, and already proven to host Isaac Sim 5.1
# (npa-groot uses the same base). To update, resolve the digest via the registry HTTP
# API per docs/security/image-reproducibility.md and update the CI scan matrix too.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04@sha256:ad6d59a3bbf3e82c1c849c9ac09cfc2a3e0bbb8655042fd899be6681b3fe2a85

ARG ISAAC_LAB_VERSION=2.3.2.post1
ARG ISAAC_SIM_VERSION=5.1.0.0
# Isaac Lab v2.3.2. Pinned by COMMIT because git tags are mutable; the bootstrap asserts
# `git rev-parse HEAD` equals this. The GitHub repo is BSD-3-Clause and is the only
# source of scripts/reinforcement_learning/, which the SkyPilot Isaac tasks invoke - the
# isaaclab wheel ships the library but no scripts/.
ARG ISAAC_LAB_SRC_COMMIT=37ddf626871758333d6ed89cf64ad702aef127d0
ARG NPA_RUNTIME_USER=ubuntu

LABEL npa.tool="isaac-lab" \
      npa.version="${ISAAC_LAB_VERSION}" \
      npa.isaac_sim_version="${ISAAC_SIM_VERSION}" \
      npa.isaac_provisioning="runtime-fetch" \
      npa.architecture="no-baked-omniverse-kit"

# NOTE the absence of ACCEPT_EULA / ISAACSIM_ACCEPT_EULA / OMNI_KIT_ACCEPT_EULA /
# PRIVACY_CONSENT. Every one of those used to be baked here. They are now the operator's
# to set, which is the entire legal mechanism, and
# npa/tests/docker/test_packaging_contract.py fails the build if any of them reappears.
ENV DEBIAN_FRONTEND=noninteractive \
    ISAAC_LAB_VERSION=${ISAAC_LAB_VERSION} \
    ISAAC_SIM_VERSION=${ISAAC_SIM_VERSION} \
    NPA_ISAAC_LAB_SRC_COMMIT=${ISAAC_LAB_SRC_COMMIT} \
    NPA_ISAAC_CACHE_DIR=/opt/isaac-cache \
    NPA_ISAAC_BASE_PYTHON=/opt/npa/sim/venv/bin/python \
    NPA_ISAAC_WHEELS_FILE=/opt/npa/docker/workbench/common/isaac-nvidia-wheels.txt \
    ISAAC_LAB_PYTHON=/isaac-sim/python.sh \
    NPA_ISAAC_LAB_OUTPUT_DIR=/workspace/isaaclab/npa-runs \
    NPA_ISAAC_LAB_WORKDIR=/workspace/isaaclab \
    OMNI_USER_DIR=/tmp/isaac-sim-cache \
    OMNI_LOG_DIR=/tmp/isaac-sim-cache/logs \
    XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    NVIDIA_DRIVER_CAPABILITIES=all \
    PYTHONUNBUFFERED=1

SHELL ["/bin/bash", "-lc"]
USER root

COPY docker/workbench/common /opt/npa/docker/workbench/common
COPY docker/workbench/isaac-lab/smoke_env.py /opt/npa/docker/workbench/isaac-lab/smoke_env.py
COPY docker/workbench/isaac-lab/smoke_functional.py /opt/npa/docker/workbench/isaac-lab/smoke_functional.py

# python3.11 + PyTorch cu128 (sm_120 for Blackwell) + the complete OSS dependency closure
# + the bootstrap, the /isaac-sim/python.sh shim and the SkyPilot k8s prerequisites. The
# script ends by asserting that the bootstrap REFUSES without EULA acceptance and that no
# isaacsim/isaaclab distribution is importable - the build proves the absence of the thing
# it used to prove the presence of.
RUN chmod +x /opt/npa/docker/workbench/common/*.sh \
    && NPA_RUNTIME_USER="${NPA_RUNTIME_USER}" \
       NPA_ISAAC_VENV=/opt/npa/sim/venv \
       /opt/npa/docker/workbench/common/install_isaac_runtime_base.sh

RUN install -d -m 0755 -o "${NPA_RUNTIME_USER}" -g "${NPA_RUNTIME_USER}" \
      /workspace/isaaclab \
      /workspace/isaaclab/npa-runs \
      /tmp/isaac-sim-cache \
      /tmp/isaac-sim-cache/logs \
    && install -d -m 0700 -o "${NPA_RUNTIME_USER}" -g "${NPA_RUNTIME_USER}" /tmp/xdg-runtime \
    && chown -R "${NPA_RUNTIME_USER}:${NPA_RUNTIME_USER}" /opt/npa/docker/workbench/isaac-lab \
    && /opt/npa/sim/venv/bin/python -m py_compile \
      /opt/npa/docker/workbench/isaac-lab/smoke_env.py \
      /opt/npa/docker/workbench/isaac-lab/smoke_functional.py

expected_lab = os.environ["ISAAC_LAB_VERSION"]
actual_lab = metadata.version("isaaclab")
if actual_lab != expected_lab:
    raise RuntimeError(f"expected isaaclab {expected_lab}, found {actual_lab}")
if not Path("/isaac-sim/python.sh").is_file():
    raise RuntimeError("official Isaac Sim Python interpreter not found at /isaac-sim/python.sh")
print(f"ISAAC_LAB_BUILD_VERSION_OK isaaclab={actual_lab} isaac_sim_python=/isaac-sim/python.sh")
PY

RUN id -u ubuntu >/dev/null 2>&1 || useradd -m -s /bin/bash -u 1000 ubuntu \
    && chown -R ubuntu:ubuntu /opt/npa/docker/workbench/isaac-lab /workspace/isaaclab/npa-runs

# --- Schedulable-on-Kubernetes prerequisites -----------------------------------
#
# Without these the image cannot host a SkyPilot task at all: provisioning dies with
#   KubernetesError: Failed to get ssh user for pod ...: container not found ("ray-node")
# because the container exits during SkyPilot's runtime bootstrap. Verified on
# npa-rtxpro-mk8s by bisecting a derived image one ingredient at a time; ALL FOUR are
# required, and the last two are the ones that are easy to miss:
#
#   1. a system python3 (+ rsync/curl/ssh client) - the NVIDIA base ships none, only
#      /isaac-sim/python.sh;
#   2. /isaac-sim readable by the runtime user - it is mode 750 isaac-sim:isaac-sim, so
#      the user joins that GROUP (a recursive chown/chmod would rewrite multi-GB layers);
#   3. passwordless sudo for the runtime user - SkyPilot's in-pod setup shells out to
#      sudo, and Debian's default %sudo rule prompts for a password (this alone kept a
#      non-root image failing while an otherwise identical root image worked);
#   4. the system interpreter FIRST on PATH - otherwise python3 resolves to Isaac's kit
#      interpreter, which cannot import its own site-packages outside python.sh.
#
# Training is unaffected: `npa workbench isaac-lab train` and npa.workflows.rl_sweep
# still run through ISAAC_LAB_PYTHON (/isaac-sim/python.sh). The image stays non-root.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-venv python3-pip rsync curl openssh-client sudo netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && (getent group isaac-sim >/dev/null 2>&1 && usermod -aG isaac-sim ubuntu || true) \
    && printf 'ubuntu ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/99-npa-runtime-user \
    && chmod 0440 /etc/sudoers.d/99-npa-runtime-user \
    && python3 --version

# Kit needs writable data/cache/log directories, and the runtime user does not have them.
#
# /isaac-sim is mode 750 isaac-sim:isaac-sim, `kit/data` and `kit/logs` do not exist, and
# `kit/cache` is root-owned 0755 — so as `ubuntu` the app cannot create or write any of them.
# Isaac still BOOTS: it logs
#     [Error] [omni.kit.app.plugin] failed to open file
#         '/isaac-sim/kit/data/Kit/Isaac-Sim/5.1/user.config.json' - unable to save the dictionary
#     [Error] [omni.kvdb.plugin] Unexpected key-value database error
#     [Warning] [omni.kit.material.library.material_library] get_mdl_list_async: mdl_list_cache
#         is not complete
# and then never renders a frame. Live job 271 sat at 170% CPU for 45 minutes past
# "Completed setting up the environment" with an empty output directory, which reads like a slow
# simulation and is actually a stalled one.
#
# Three empty directories, so the layer is tiny. Nothing else about the image changes: it stays
# non-root, and this does not touch the multi-GB payload under /isaac-sim.
RUN if [ -d /isaac-sim ]; then \
      mkdir -p /isaac-sim/kit/data /isaac-sim/kit/logs /isaac-sim/kit/cache \
      && chown -R ubuntu:ubuntu /isaac-sim/kit/data /isaac-sim/kit/logs /isaac-sim/kit/cache; \
    fi

# SkyPilot's Kubernetes bootstrap must find the SYSTEM interpreter (python3.10 here), not
# the Isaac venv's python3.11, which cannot import its own site-packages when invoked
# outside the venv. Isaac work always goes through ISAAC_LAB_PYTHON.
ENV PATH=/usr/bin:$PATH

WORKDIR /workspace/isaaclab
USER ${NPA_RUNTIME_USER}
ENTRYPOINT ["/bin/bash"]
