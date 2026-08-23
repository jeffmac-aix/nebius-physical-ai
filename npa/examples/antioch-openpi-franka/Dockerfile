FROM antioch-engine/isaac-lab-3.0:0.3.47

# The operator passes the exact reviewed NPA commit. It is source provenance,
# never a credential. Antioch saves the resulting image only in the operator's
# private organization registry because the parent contains its Isaac runtime.
ARG NPA_SOURCE_REF
RUN printf '%s' "${NPA_SOURCE_REF}" | grep -Eq '^[0-9a-f]{40}$' \
    && python -m pip install --no-cache-dir msgpack==1.1.2 websocket-client==1.9.0 \
    && python -m pip install --no-cache-dir --no-deps \
      "git+https://github.com/nebius/nebius-physical-ai.git@${NPA_SOURCE_REF}#subdirectory=npa"

# Interactive and queued suite runs both execute authored scenarios from
# /workspace/project. Copy only the runtime modules: the local tunnel connector
# stays on the operator host and no Antioch runtime state enters this image.
COPY scenarios.py reverse_policy_relay.py /workspace/project/

# The hosted engine grants the runtime account device and project access; the
# scenario and relay need no root privileges after the build has installed them.
# The parent image retains root's HOME, so give the runtime user an owned home
# before dropping privileges; Kit otherwise attempts to create /root/.cache.
ENV HOME=/workspace/npa-runtime-home \
    XDG_CACHE_HOME=/workspace/npa-runtime-home/.cache
RUN install -d -m 0755 -o 1000 -g 1000 \
      /workspace/npa-runtime-home /workspace/npa-runtime-home/.cache
USER 1000:1000
