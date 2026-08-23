FROM antioch-engine/isaac-sim-6.0.1:0.3.63

# Protocol-only dependencies. OpenPI source, model code, and weights are not
# copied into this image; the policy implementation remains on the B200 side.
RUN uv pip install --system --no-cache \
    "msgpack==1.1.1" \
    "websockets==15.0.1"
