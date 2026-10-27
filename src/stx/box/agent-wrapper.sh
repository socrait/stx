#!/bin/bash
# Status + capture file — see docs/reference.md (Dispatch).
set -u

STATUS_DIR="$(getent passwd "${STX_BOX_USER}" | cut -d: -f6)/.stx-agent"
LOCK_DIR="${STATUS_DIR}/lock"
STATUS_FILE="${STATUS_DIR}/status.json"
LOGS_DIR="/logs"
mkdir -p "${STATUS_DIR}" "${LOGS_DIR}"

# One dispatch per box at a time — see docs/reference.md (Dispatch). `mkdir`
# is atomic at the kernel level, so exactly one concurrent wrapper wins it.
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    exit 1
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null' EXIT

task="$1"
model="$2"
index="$3"

capture_file="${LOGS_DIR}/${index}.jsonl"
stderr_file="${LOGS_DIR}/${index}.stderr.log"

printf '{"state":"working","pid":%d,"started_at":"%s","index":%s}\n' \
    "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${index}" > "${STATUS_FILE}"

# No permission prompts: nobody can answer them in a headless box, and the box's own
# confinement is the guard (see docs/reference.md, Dispatch).
claude -p "${task}" --model "${model}" --output-format stream-json --verbose \
    --permission-mode bypassPermissions \
    2> "${stderr_file}" \
    | while IFS= read -r line; do
        [ -z "${line}" ] && continue
        printf '{"t":"%s","e":%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${line}"
      done > "${capture_file}"
exit_code=${PIPESTATUS[0]}

printf '{"state":"done","pid":%d,"exit_code":%d,"finished_at":"%s","index":%s}\n' \
    "$$" "${exit_code}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${index}" > "${STATUS_FILE}"
