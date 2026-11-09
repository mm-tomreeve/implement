#!/usr/bin/env bash
# Full end-to-end test: five containers on one network, one per role, wired
# together through answer files, each installed with install.sh. Then a
# functional test through the whole stack and the per-server reports copied out.
#
#   tests/e2e/run.sh                 AlmaLinux 9
#   IMAGE=docker.io/almalinux/10-init tests/e2e/run.sh
#   KEEP=true tests/e2e/run.sh       leave the containers running afterwards
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${HERE}/../.." && pwd)"
IMAGE="${IMAGE:-docker.io/almalinux/9-init}"
ENGINE="${ENGINE:-$(command -v podman || command -v docker || true)}"
[[ -n "${ENGINE}" ]] || { echo "Need podman or docker" >&2; exit 1; }
KEEP="${KEEP:-false}"
NET=mmd-e2e
SUBNET=172.30.10.0/24
OUT="${HERE}/out"
SERVERS=(db01 calls01 search01 app01 mon01)
# A function rather than an associative array: macOS still ships bash 3.2.
ip_of() {
  case "$1" in
    db01) echo 172.30.10.11 ;; app01) echo 172.30.10.12 ;; calls01) echo 172.30.10.13 ;;
    search01) echo 172.30.10.14 ;; mon01) echo 172.30.10.15 ;;
  esac
}

log() { printf '==> %s\n' "$*"; }
box() { echo "mmd-e2e-$1"; }
in_box() { "${ENGINE}" exec "$(box "$1")" bash -c "$2"; }

# shellcheck disable=SC2329  # called from the EXIT trap
cleanup() {
  [[ "${KEEP}" == true ]] && { log "KEEP=true, leaving containers running"; return; }
  for s in "${SERVERS[@]}"; do "${ENGINE}" rm -f "$(box "${s}")" >/dev/null 2>&1 || true; done
  "${ENGINE}" network rm "${NET}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

rm -rf "${OUT}"; mkdir -p "${OUT}"
for s in "${SERVERS[@]}"; do "${ENGINE}" rm -f "$(box "${s}")" >/dev/null 2>&1 || true; done
"${ENGINE}" network rm "${NET}" >/dev/null 2>&1 || true
"${ENGINE}" network create --subnet "${SUBNET}" "${NET}" >/dev/null

log "Starting ${#SERVERS[@]} containers from ${IMAGE}"
for s in "${SERVERS[@]}"; do
  publish=()
  # With KEEP=true you can open mon01's Grafana afterwards on http://localhost:13000.
  [[ "${KEEP}" == true && "${s}" == mon01 ]] && publish=(-p "127.0.0.1:${GRAFANA_PORT:-13000}:3000")
  "${ENGINE}" run -d --name "$(box "${s}")" --hostname "${s}.e2e.test" --network "${NET}" --ip "$(ip_of "${s}")" \
    --network-alias "${s}.e2e.test" --privileged --cgroupns=host --shm-size 256m "${publish[@]+"${publish[@]}"}" \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "${REPO_DIR}:/src:ro" "${IMAGE}" >/dev/null
done
for s in "${SERVERS[@]}"; do
  for _ in $(seq 1 30); do
    in_box "${s}" 'systemctl is-system-running 2>/dev/null | grep -qE "running|degraded"' && break
    sleep 2
  done
  # Each server gets the shared settings plus its own.
  in_box "${s}" "rm -rf /opt/mmd && cp -r /src /opt/mmd && rm -rf /opt/mmd/collections /opt/mmd/tests/*/out \
    && { cat /opt/mmd/tests/e2e/answers/common.yml; grep -v '^---$' /opt/mmd/tests/e2e/answers/${s}.yml; } > /root/answers.yml"
done

install() {
  local s="$1" token=()
  # GitHub rate-limits anonymous API calls to 60 an hour. Pass your token through if you have one set.
  [[ -n "${GITHUB_TOKEN:-}" ]] && token=(-e "GITHUB_TOKEN=${GITHUB_TOKEN}")
  if "${ENGINE}" exec "${token[@]+"${token[@]}"}" "$(box "${s}")" bash -c 'cd /opt/mmd && ./install.sh --answers /root/answers.yml --non-interactive --yes' > "${OUT}/${s}-install.log" 2>&1; then
    echo 0 > "${OUT}/${s}.rc"; log "${s}: installed"
  else
    echo 1 > "${OUT}/${s}.rc"; log "${s}: install FAILED (see ${OUT}/${s}-install.log)"
  fi
}

log "Phase 1: database, calls and search in parallel"
install db01 & install calls01 & install search01 & wait

log "Handing the search CA to app01 and making it a TLS certificate"
if "${ENGINE}" cp "$(box search01):/etc/opensearch/certs/root-ca.pem" "${OUT}/search-ca.pem" 2>/dev/null; then
  "${ENGINE}" cp "${OUT}/search-ca.pem" "$(box app01):/root/search-ca.pem"
else
  log "No search CA to copy (search01 install failed?)"
fi
in_box app01 'set -e; mkdir -p /root/certs /root/ca && cd /root/ca
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=e2e test CA" -keyout ca.key -out ca.crt 2>/dev/null
  openssl req -new -newkey rsa:2048 -nodes -subj "/CN=app01.e2e.test" -keyout /root/certs/tls.key -out app.csr 2>/dev/null
  printf "subjectAltName=DNS:app01.e2e.test\n" > ext
  openssl x509 -req -in app.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 180 -extfile ext -out /root/certs/tls.crt 2>/dev/null
  cp ca.crt /root/certs/chain.crt'

"${ENGINE}" cp "$(box app01):/root/ca/ca.crt" "${OUT}/site-ca.pem"
"${ENGINE}" cp "${OUT}/site-ca.pem" "$(box mon01):/root/site-ca.pem"

log "Phase 2: app server"
install app01

log "Phase 3: monitoring"
install mon01

log "Functional test through the whole stack"
functional_rc=0
in_box app01 'bash /opt/mmd/tests/e2e/functional.sh' | tee "${OUT}/functional.log" || functional_rc=$?

log "Every alert query against live data on mon01 (after two scrape intervals)"
sleep 70
observability_rc=0
in_box mon01 'python3 -I /opt/mmd/tests/e2e/observability_check.py --selftest' | tee "${OUT}/observability.log" || observability_rc=$?

log "Re-running every server's checks now the whole estate is up"
for s in "${SERVERS[@]}"; do
  in_box "${s}" 'cd /opt/mmd && ./install.sh --verify-only --non-interactive' > "${OUT}/${s}-verify.log" 2>&1 \
    && echo 0 > "${OUT}/${s}.verify.rc" || echo 1 > "${OUT}/${s}.verify.rc"
  in_box "${s}" 'cat /var/lib/mattermost-implement/reports/latest.md' > "${OUT}/ARCHITECTURE-${s}.md" 2>/dev/null || true
  in_box "${s}" 'cat /var/lib/mattermost-implement/results/latest.json' > "${OUT}/results-${s}.json" 2>/dev/null || true
done

log "Summary"
rc=0
for s in "${SERVERS[@]}"; do
  inst="$(cat "${OUT}/${s}.rc" 2>/dev/null || echo 1)"; ver="$(cat "${OUT}/${s}.verify.rc" 2>/dev/null || echo 1)"
  counts="$(python3 -I -c 'import json,sys
try:
    r=json.load(open(sys.argv[1]))["results"]
except Exception:
    print("no results"); sys.exit()
c={k:sum(1 for x in r if x["status"]==k) for k in ("pass","warn","fail","skip")}
print("%(pass)d pass, %(warn)d warn, %(fail)d fail, %(skip)d n/a" % c)' "${OUT}/results-${s}.json")"
  printf '    %-9s install %-4s verify %-4s %s\n' "${s}" "$([[ ${inst} == 0 ]] && echo ok || echo FAIL)" "$([[ ${ver} == 0 ]] && echo ok || echo FAIL)" "${counts}"
  [[ "${inst}" == 0 && "${ver}" == 0 ]] || rc=1
done
printf '    %-9s %s\n' functional "$([[ ${functional_rc} == 0 ]] && echo ok || echo FAIL)"
printf '    %-9s %s\n' alerts "$([[ ${observability_rc} == 0 ]] && echo ok || echo FAIL)"
[[ "${functional_rc}" == 0 && "${observability_rc}" == 0 ]] || rc=1
log "Reports and logs: ${OUT}"
if [[ "${KEEP}" == true ]]; then
  log "Grafana: http://localhost:${GRAFANA_PORT:-13000} (admin; password in mon01's /etc/mattermost-implement/secrets.yml)"
fi
exit "${rc}"
