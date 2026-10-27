# syntax=docker/dockerfile:1
# The box stx's own contributors work in: stx's sandbox.yaml builds it.
FROM python:3.12-slim-trixie

# Box runtime tools stx needs to enforce egress (see docs/box-contract.md), plus git and curl.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        iproute2 \
        iptables \
        jq \
        tinyproxy \
        ulogd2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /usr/local/bin/

RUN useradd --create-home --uid 1000 dev \
    && mkdir -p /workspace /uv-cache \
    && chown dev:dev /workspace /uv-cache

# Pinned: an unpinned install bakes in whatever is latest at build time.
RUN su -s /bin/bash -c "curl -fsSL https://claude.ai/install.sh | bash -s 2.1.201" dev
ENV PATH="/home/dev/.local/bin:$PATH" \
    UV_CACHE_DIR=/uv-cache \
    UV_LINK_MODE=copy

WORKDIR /workspace
USER dev
