# Sandbox the SRE agent's tool calls run in. The worker starts one throwaway
# container per attempt, mounts only the repo work tree at /workspace, passes
# no env vars, and (by default) no network. Add the toolchains your monitored
# repos need to run their tests. Build: make sandbox-image
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv pytest

# The container runs as the worker's uid, which has no passwd entry or home.
ENV HOME=/tmp \
    UV_CACHE_DIR=/tmp/.uv-cache \
    npm_config_cache=/tmp/.npm \
    PIP_NO_CACHE_DIR=1

WORKDIR /workspace
CMD ["sleep", "infinity"]
