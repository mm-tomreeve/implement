#!/usr/bin/env bash
# Run install.sh with the core profile inside a systemd container, then re-run the
# tests and copy the report out. Needs podman or docker and internet access.
#   IMAGE=docker.io/almalinux/10-init tests/smoke/run.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${IMAGE:-docker.io/almalinux/9-init}"
ENGINE="${ENGINE:-$(command -v podman || command -v docker || true)}"
[[ -n "${ENGINE}" ]] || { echo "Need podman or docker" >&2; exit 1; }
NAME="${NAME:-mmd-smoke}"
OUT="${REPO_DIR}/tests/smoke/out"

KEEP="${KEEP:-false}"
cleanup() {
  if [[ "${KEEP}" == true ]]; then echo "==> KEEP=true, container ${NAME} left running"; return; fi
  "${ENGINE}" rm -f "${NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${ENGINE}" rm -f "${NAME}" >/dev/null 2>&1 || true
rm -rf "${OUT}"

echo "==> Starting ${IMAGE}"
"${ENGINE}" run -d --name "${NAME}" --hostname smoke.example.test --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "${REPO_DIR}:/src:ro" "${IMAGE}" >/dev/null

in_box() { "${ENGINE}" exec "${NAME}" bash -c "$1"; }

echo "==> Waiting for systemd"
for _ in $(seq 1 30); do
  in_box 'systemctl is-system-running 2>/dev/null | grep -qE "running|degraded"' && break
  sleep 2
done

echo "==> Installing"
in_box 'rm -rf /opt/mmd && cp -r /src /opt/mmd && rm -rf /opt/mmd/collections'
in_box 'cd /opt/mmd && ./install.sh --answers tests/smoke/answers-core.yml --non-interactive --yes'

echo "==> Re-running the tests"
in_box 'cd /opt/mmd && ./install.sh --verify-only --non-interactive'

echo "==> Copying the report"
mkdir -p "${OUT}"
# latest.* are symlinks, so read them through the container rather than docker cp.
in_box 'cat /var/lib/mattermost-implement/reports/latest.md' > "${OUT}/ARCHITECTURE-smoke.md"
in_box 'cat /var/lib/mattermost-implement/results/latest.json' > "${OUT}/results.json"
test -s "${OUT}/ARCHITECTURE-smoke.md"
echo "==> Report: ${OUT}/ARCHITECTURE-smoke.md"
