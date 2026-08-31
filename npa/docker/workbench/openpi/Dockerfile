# Apache-2.0 OpenPI source and pinned RLDS/JAX runtime. Gemma/pi0.5 weights,
# DROID data, credentials, terms acceptance, and populated caches are absent.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04@sha256:24c8e3581ea6330038b0d374920721983312627f8adbfcf390bdb4b399d280ed

ARG OPENPI_SOURCE_URL=https://github.com/Physical-Intelligence/openpi.git
ARG OPENPI_SOURCE_REF=15a9616a00943ada6c20a0f158e3adb39df2ccac
ARG NPA_SOURCE_SHA=unknown

LABEL npa.tool="openpi" \
      npa.version="pi05-full-droid-rlds-cu128" \
      npa.source.repo="${OPENPI_SOURCE_URL}" \
      npa.source.ref="${OPENPI_SOURCE_REF}" \
      npa.weights="operator-runtime-fetch" \
      npa.dataset="operator-runtime-fetch" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${NPA_SOURCE_SHA}"

ENV DEBIAN_FRONTEND=noninteractive \
    GIT_LFS_SKIP_SMUDGE=1 \
    HOME=/home/ubuntu \
    OPENPI_DATA_HOME=/workspace/.cache/openpi \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH=/opt/venv/bin:/opt/gsutil-venv/bin:${PATH}

SHELL ["/bin/bash", "-lc"]
USER root

RUN rm -f /etc/apt/sources.list.d/*.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates git git-lfs openssh-server python3 python3-dev \
      python3-pip python3-venv rsync sudo \
    && rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
    && (id -u ubuntu >/dev/null 2>&1 || useradd -m -s /bin/bash -u 1000 ubuntu) \
    && printf '%s\n' 'ubuntu ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/npa-runtime \
    && chmod 0440 /etc/sudoers.d/npa-runtime

RUN git clone --filter=blob:none --no-checkout "${OPENPI_SOURCE_URL}" /opt/byof \
    && git -C /opt/byof checkout --detach "${OPENPI_SOURCE_REF}" \
    && test "$(git -C /opt/byof rev-parse HEAD)" = "${OPENPI_SOURCE_REF}" \
    && test -s /opt/byof/LICENSE \
    && git -C /opt/byof lfs uninstall \
    && rm -rf /opt/byof/.git \
    && printf '%s\n' \
      '{' \
      '  "source": "oss-byof",' \
      '  "repo": "https://github.com/Physical-Intelligence/openpi.git",' \
      '  "ref": "15a9616a00943ada6c20a0f158e3adb39df2ccac"' \
      '}' > /opt/byof/npa_source_metadata.json

# Upstream's frozen lock is authoritative. Keep gsutil isolated from the
# TensorFlow/JAX environment so its older transitive pins cannot perturb it.
RUN python3 -m venv /opt/uv \
    && /opt/uv/bin/pip install --no-cache-dir 'uv==0.9.7' \
    && /opt/uv/bin/uv python install 3.11 \
    && /opt/uv/bin/uv venv --python 3.11 /opt/venv \
    && cd /opt/byof \
    && VIRTUAL_ENV=/opt/venv /opt/uv/bin/uv sync --active --frozen --group rlds --no-dev \
    && /opt/uv/bin/uv pip install --python /opt/venv/bin/python --no-cache 'boto3>=1.34' \
    && /opt/uv/bin/uv venv --python 3.11 /opt/gsutil-venv \
    && /opt/uv/bin/uv pip install --python /opt/gsutil-venv/bin/python --no-cache gsutil \
    && build_hash="$(sha256sum /opt/byof/uv.lock | cut -d' ' -f1)" \
    && printf '{"schema":"npa.byof.build.v1","build_command_executed":true,"build_command_sha256":"%s"}\n' \
      "$build_hash" > /opt/byof/npa_build_metadata.json

RUN printf '%s\n' \
      '#include <cuda_runtime.h>' \
      '#include <cstdio>' \
      'int main(){int n=0;if(cudaGetDeviceCount(&n)!=cudaSuccess)return 2;if(n!=1)return 3;cudaDeviceProp p{};if(cudaGetDeviceProperties(&p,0)!=cudaSuccess||p.major!=12||p.minor!=0)return 4;std::printf("devices=%d cc=12.0\\n",n);return 0;}' \
      > /tmp/npa-openpi-sm120.cu \
    && nvcc -O2 -arch=sm_120 /tmp/npa-openpi-sm120.cu -o /usr/local/bin/npa-openpi-sm120-probe \
    && rm -f /tmp/npa-openpi-sm120.cu

COPY pyproject.toml README.md /opt/npa-src/
COPY src/npa /opt/npa-src/src/npa
COPY workflows/workbench/npa-workflows/sim2real.yaml \
  /opt/npa-src/workflows/workbench/npa-workflows/sim2real.yaml
COPY workflows/workbench/npa-workflows/physical-ai-data-factory.yaml \
  /opt/npa-src/workflows/workbench/npa-workflows/physical-ai-data-factory.yaml
COPY workflows/workbench/npa-workflows/openpi-pi05-full-droid-finetune.yaml \
  /opt/npa-src/workflows/workbench/npa-workflows/openpi-pi05-full-droid-finetune.yaml
RUN /opt/uv/bin/uv pip install --python /opt/venv/bin/python --no-cache --no-deps /opt/npa-src \
    && /opt/venv/bin/python -m npa.workflows.byof.openpi_full_droid --help >/dev/null \
    && test -z "$(find /opt/byof /opt/npa-src /opt/venv \
      \( -iname '*.safetensors' -o -iname '*.ckpt' -o -iname 'pytorch_model*.bin' \
         -o -iname '*.tfrecord' -o -iname '*.tfrecord-*' \) -print -quit)" \
    && rm -rf /root/.cache /tmp/uv-cache /tmp/pip-*

COPY --chmod=0755 docker/workbench/openpi/entrypoint.sh /usr/local/bin/openpi-entrypoint
COPY --chmod=0444 docker/workbench/openpi/REDISTRIBUTION.md /usr/share/doc/npa-openpi/REDISTRIBUTION.md
COPY --chmod=0444 docker/workbench/openpi/THIRD_PARTY_NOTICES.md /usr/share/doc/npa-openpi/THIRD_PARTY_NOTICES.md

RUN install -d -m 0755 -o ubuntu -g ubuntu \
      /workspace /workspace/.cache /workspace/.cache/openpi \
    && chown -R ubuntu:ubuntu /workspace /opt/byof \
    && test -r /opt/byof/LICENSE \
    && test -r /usr/share/doc/npa-openpi/REDISTRIBUTION.md

VOLUME ["/workspace/.cache/openpi"]
WORKDIR /opt/byof
USER ubuntu
ENTRYPOINT ["openpi-entrypoint"]
CMD ["bash"]
