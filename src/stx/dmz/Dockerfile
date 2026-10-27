FROM debian:bookworm-slim

# Image requirements — see docs/box-contract.md: bash, iptables, ip6tables, ss
# (iproute2), tinyproxy, ulogd, jq, git, su (from login, already in the base
# image); ca-certificates/curl are build-time only, for installing the harness.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        iproute2 \
        iptables \
        jq \
        tinyproxy \
        ulogd2 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash dmzuser

# Pinned: an unpinned install would bake in whatever's latest at build time,
# drifting the harness version between otherwise-identical dmz images.
RUN su -s /bin/bash -c "curl -fsSL https://claude.ai/install.sh | bash -s 2.1.201" dmzuser
ENV PATH="/home/dmzuser/.local/bin:$PATH"
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# Root-owned, so a dmz box with no snapshot mounted still has a read-only
# /workspace; WORKDIR after USER would create it owned by dmzuser.
RUN mkdir /workspace

USER dmzuser
WORKDIR /workspace
