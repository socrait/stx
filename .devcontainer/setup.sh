#!/usr/bin/env bash
# Runs in a fresh stx box as the box user: install the project's dependencies.
set -euo pipefail
uv sync --frozen
