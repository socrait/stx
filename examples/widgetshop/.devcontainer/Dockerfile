FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash git iptables iproute2 iputils-ping tinyproxy ulogd2 jq sudo curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

RUN useradd --create-home --shell /bin/bash devuser

WORKDIR /workspace
COPY pyproject.toml uv.lock* ./

USER devuser
