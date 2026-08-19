# Public OpenPI policy-server runtime. The image contains source and dependencies,
# never pi0.5/Gemma weights, credentials, or a populated cache.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04@sha256:24c8e3581ea6330038b0d374920721983312627f8adbfcf390bdb4b399d280ed AS build

ENV DEBIAN_FRONTEND=noninteractive UV_PROJECT_ENVIRONMENT=/opt/venv
SHELL ["/bin/bash", "-lc"]
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates git python3 python3-dev python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/uv \
    && /opt/uv/bin/pip install --no-cache-dir uv==0.8.13

RUN GIT_LFS_SKIP_SMUDGE=1 git clone --filter=blob:none \
      https://github.com/Physical-Intelligence/openpi.git /opt/byof \
    && git -C /opt/byof checkout --detach 15a9616a00943ada6c20a0f158e3adb39df2ccac \
    && test "$(git -C /opt/byof rev-parse HEAD)" = \
      15a9616a00943ada6c20a0f158e3adb39df2ccac \
    && cd /opt/byof \
    && /opt/uv/bin/uv sync --frozen --no-dev --no-editable \
    && /opt/venv/bin/python -m pip check \
    && rm -rf /opt/byof/.git /root/.cache /tmp/*

RUN printf '%s\n' \
      '#include <cuda_runtime.h>' \
      '#include <cstdio>' \
      'int main(){cudaDeviceProp p{}; if(cudaGetDeviceProperties(&p,0)!=cudaSuccess)return 2; std::printf("%s cc=%d.%d\\n",p.name,p.major,p.minor); return (p.major==10 && p.minor==0)?0:3;}' \
      > /tmp/npa-openpi-sm100.cu \
    && nvcc -O2 -arch=sm_100 /tmp/npa-openpi-sm100.cu \
      -o /usr/local/bin/npa-openpi-sm100-probe \
    && rm /tmp/npa-openpi-sm100.cu

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04@sha256:ac55d124da4882b497f732d8dfd9a702d5447a5f29d08d56da6f64f0a1eb34bc

ARG NPA_SOURCE_SHA
LABEL npa.tool="openpi-policy" \
      npa.version="pi05-polaris-runtime-cache-20260819-r1" \
      npa.model_provisioning="runtime-fetch" \
      npa.model_cache="immutable-gcs-generation-manifest" \
      org.opencontainers.image.source="https://github.com/nebius/nebius-physical-ai" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${NPA_SOURCE_SHA}"

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/workspace \
    OPENPI_DATA_HOME=/opt/npa-model-cache/openpi \
    JAX_COMPILATION_CACHE_DIR=/workspace/.cache/jax \
    XLA_PYTHON_CLIENT_PREALLOCATE=false \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin
SHELL ["/bin/bash", "-lc"]
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates libgl1 libglib2.0-0 passwd python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
    && groupadd --gid 1000 openpi \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /workspace openpi \
    && install -d -m 0755 /opt/npa-openpi /opt/npa/licenses/openpi-policy \
    && install -d -m 0775 -o 1000 -g 1000 /workspace /workspace/.cache \
      /opt/npa-model-cache/openpi

COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/byof /opt/byof
COPY --from=build /usr/local/bin/npa-openpi-sm100-probe /usr/local/bin/npa-openpi-sm100-probe
COPY src/npa/workflows/byof/openpi_checkpoint_cache.py /opt/npa-openpi/openpi_checkpoint_cache.py
COPY src/npa/workflows/byof/openpi_policy_server.py /opt/npa-openpi/openpi_policy_server.py
COPY docker/workbench/openpi-policy/THIRD_PARTY_NOTICES.md /opt/npa/licenses/openpi-policy/THIRD_PARTY_NOTICES.md
COPY docker/workbench/openpi-policy/REDISTRIBUTION.md /opt/npa/licenses/openpi-policy/REDISTRIBUTION.md

RUN test "$(printf %s "${NPA_SOURCE_SHA}" | wc -c)" -eq 40 \
    && test "$(sha256sum /opt/byof/uv.lock | cut -d' ' -f1)" = \
      793488b5a55bb87200db90a61fd0af51922b686d94e1da4f4c587ab119b37d74 \
    && chmod 0755 /opt/npa-openpi/openpi_checkpoint_cache.py \
      /opt/npa-openpi/openpi_policy_server.py \
      /usr/local/bin/npa-openpi-sm100-probe \
    && find /opt/byof /opt/venv /opt/npa-openpi /opt/npa/licenses/openpi-policy \
      -type d -exec chmod a+rx {} + \
    && find /opt/byof /opt/venv /opt/npa-openpi /opt/npa/licenses/openpi-policy \
      -type f -exec chmod a+r {} + \
    && test -z "$(find /opt/npa-model-cache/openpi -mindepth 1 -print -quit)" \
    && /opt/venv/bin/python -m py_compile /opt/npa-openpi/openpi_checkpoint_cache.py \
      /opt/npa-openpi/openpi_policy_server.py \
    && /opt/venv/bin/python -c 'import jax, openpi, openpi_client' \
    && rm -rf /root/.cache /tmp/*

USER 1000
WORKDIR /workspace
VOLUME ["/opt/npa-model-cache/openpi"]
EXPOSE 8000
CMD ["/opt/venv/bin/python", "/opt/npa-openpi/openpi_policy_server.py"]
