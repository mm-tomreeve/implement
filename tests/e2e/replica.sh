#!/usr/bin/env bash
# PostgreSQL streaming replication end to end: a primary and a read replica in
# two containers, each installed with install.sh. Then a write on the primary
# has to show up on the replica, the exporters have to publish the series the
# replication alerts use, and a second run of both has to pass.
#
#   tests/e2e/replica.sh                 AlmaLinux 9
#   IMAGE=docker.io/almalinux/10-init tests/e2e/replica.sh
#   KEEP=true tests/e2e/replica.sh       leave the containers running afterwards
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${HERE}/../.." && pwd)"
IMAGE="${IMAGE:-docker.io/almalinux/9-init}"
ENGINE="${ENGINE:-$(command -v podman || command -v docker || true)}"
[[ -n "${ENGINE}" ]] || { echo "Need podman or docker" >&2; exit 1; }
KEEP="${KEEP:-false}"
NET=mmd-replica
SUBNET=172.30.20.0/24
OUT="${HERE}/out/replica"
SERVERS=(pg01 pg02)
ip_of() { case "$1" in pg01) echo 172.30.20.21 ;; pg02) echo 172.30.20.22 ;; esac; }

log() { printf '==> %s\n' "$*"; }
box() { echo "mmd-replica-$1"; }
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

COMMON='deploy_name: replica-e2e
deploy_internet: auto
firewall_manage: false
selinux_manage: false
mattermost_db_password: E2eDbPassw0rd
postgresql_replication_password: E2eReplPassw0rd
deploy_profiles: [database]
deploy_monitoring_exporters: true
monitoring_scrape_sources: [172.30.20.0/24]
postgresql_allowed_clients: [172.30.20.30]
postgresql_app_servers: 1
mattermost_db_max_open_conns: 50'

log "Starting ${#SERVERS[@]} containers from ${IMAGE}"
for s in "${SERVERS[@]}"; do
  "${ENGINE}" run -d --name "$(box "${s}")" --hostname "${s}.e2e.test" --network "${NET}" --ip "$(ip_of "${s}")" \
    --privileged --cgroupns=host --shm-size 256m \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "${REPO_DIR}:/src:ro" "${IMAGE}" >/dev/null
done
for s in "${SERVERS[@]}"; do
  for _ in $(seq 1 30); do
    in_box "${s}" 'systemctl is-system-running 2>/dev/null | grep -qE "running|degraded"' && break
    sleep 2
  done
  in_box "${s}" "rm -rf /opt/mmd && cp -r /src /opt/mmd && rm -rf /opt/mmd/collections /opt/mmd/tests/*/out"
done
printf '%s\npostgresql_replication: primary\npostgresql_replica_hosts: [%s]\n' "${COMMON}" "$(ip_of pg02)" \
  | "${ENGINE}" exec -i "$(box pg01)" bash -c 'cat > /root/answers.yml'
printf '%s\npostgresql_replication: replica\npostgresql_primary_host: %s\n' "${COMMON}" "$(ip_of pg01)" \
  | "${ENGINE}" exec -i "$(box pg02)" bash -c 'cat > /root/answers.yml'

install() {
  local s="$1" tag="$2" token=()
  [[ -n "${GITHUB_TOKEN:-}" ]] && token=(-e "GITHUB_TOKEN=${GITHUB_TOKEN}")
  if "${ENGINE}" exec "${token[@]+"${token[@]}"}" "$(box "${s}")" \
      bash -c 'cd /opt/mmd && ./install.sh --answers /root/answers.yml --non-interactive --yes' > "${OUT}/${s}-${tag}.log" 2>&1; then
    log "${s}: ${tag} ok"; return 0
  fi
  log "${s}: ${tag} FAILED (see ${OUT}/${s}-${tag}.log)"; return 1
}

rc=0
check() {
  local name="$1"; shift
  if "$@" >> "${OUT}/checks.log" 2>&1; then printf '    ok    %s\n' "${name}"; else printf '    FAIL  %s\n' "${name}"; rc=1; fi
}
# shellcheck disable=SC2329  # used by the checks, which run in child shells
psql_on() { in_box "$1" "runuser -u postgres -- psql -X -tAc \"$2\" ${3:-postgres}"; }
# The checks run in child shells, so they need these too.
export -f psql_on in_box box
export ENGINE

log "Installing the primary, then the replica"
install pg01 install || rc=1
install pg02 install || rc=1

log "Checks"
check "pg02 is in recovery" bash -c "[[ \$(psql_on pg02 'SELECT pg_is_in_recovery()') == t ]]"
check "pg01 sees pg02 streaming" bash -c "[[ \$(psql_on pg01 \"SELECT state FROM pg_stat_replication WHERE application_name = 'pg02'\") == streaming ]]"
check "pg01 caps the WAL a slot may hold" bash -c "[[ \$(psql_on pg01 'SHOW max_slot_wal_keep_size') != -1 ]]"
check "A write on pg01 reaches pg02" bash -c "
  psql_on pg01 'CREATE TABLE IF NOT EXISTS replica_probe (v text); TRUNCATE replica_probe; INSERT INTO replica_probe VALUES (now()::text)' mattermost
  want=\$(psql_on pg01 'SELECT v FROM replica_probe' mattermost)
  for _ in \$(seq 1 20); do [[ \$(psql_on pg02 'SELECT v FROM replica_probe' mattermost 2>/dev/null) == \"\${want}\" ]] && exit 0; sleep 1; done
  exit 1"
check "pg02 refuses writes" bash -c "! psql_on pg02 'CREATE TABLE should_fail (v int)' mattermost"
check "mmuser can log in on pg02" in_box pg02 "PGPASSWORD=E2eDbPassw0rd psql -X -h 127.0.0.1 -U mmuser -d mattermost -tAc 'SELECT 1' | grep -qx 1"
check "pg02 regenerated its own TLS key" bash -c "[[ \$(in_box pg01 'sha256sum < /var/lib/pgsql/data/server.key') != \$(in_box pg02 'sha256sum < /var/lib/pgsql/data/server.key') ]]"
check "Exporter on pg01 publishes the slot series" in_box pg01 \
  "curl -s localhost:9187/metrics | grep -q '^pg_replication_slots_slot_is_active{slot_name=\"pg02\",slot_type=\"physical\"} 1' \
   && curl -s localhost:9187/metrics | grep -q '^pg_replication_slots_safe_wal_size_bytes{slot_name=\"pg02\"' \
   && curl -s localhost:9187/metrics | grep -q '^pg_settings_max_slot_wal_keep_size_bytes [1-9]'"
check "Exporter on pg02 reports a replica" in_box pg02 \
  "curl -s localhost:9187/metrics | grep -q '^pg_up 1' && curl -s localhost:9187/metrics | grep -q '^pg_replication_is_replica 1'"
log "Second run of both"
install pg01 rerun || rc=1
install pg02 rerun || rc=1
for s in "${SERVERS[@]}"; do
  in_box "${s}" 'cat /var/lib/mattermost-implement/reports/latest.md' > "${OUT}/ARCHITECTURE-${s}.md" 2>/dev/null || true
  in_box "${s}" 'cat /var/lib/mattermost-implement/results/latest.json' > "${OUT}/results-${s}.json" 2>/dev/null || true
done

log "Rebuilding the replica after it has gone"
in_box pg02 'systemctl stop postgresql && mv /var/lib/pgsql/data /var/lib/pgsql/data.old && install -d -o postgres -g postgres -m 0700 /var/lib/pgsql/data'
check "The old slot shows inactive on pg01" bash -c "[[ \$(psql_on pg01 \"SELECT active FROM pg_replication_slots WHERE slot_name = 'pg02'\") == f ]]"
install pg02 rebuild || rc=1
check "pg02 streams again after the rebuild" bash -c "
  for _ in \$(seq 1 20); do [[ \$(psql_on pg01 \"SELECT state FROM pg_stat_replication WHERE application_name = 'pg02'\") == streaming ]] && exit 0; sleep 1; done
  exit 1"

log "Logs and reports: ${OUT}"
exit "${rc}"
