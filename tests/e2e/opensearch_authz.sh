#!/usr/bin/env bash
# Prove what Mattermost's OpenSearch account can and cannot do.
#
# The account's password sits in Mattermost's config, readable from every app server, so the role it
# holds is the blast radius of an app server compromise. This runs a real
# OpenSearch with the security plugin, applies the role, mapping and account
# roles/opensearch creates, and checks both halves: every call Mattermost makes
# in normal use works, and the cluster administration it has no business doing
# comes back refused.
#
#   tests/e2e/opensearch_authz.sh
#   IMAGE=opensearchproject/opensearch:2.19.1 tests/e2e/opensearch_authz.sh
#   ENGINE=podman tests/e2e/opensearch_authz.sh
#
# The role itself is read out of roles/opensearch/defaults/main.yml, so this
# tests the permissions the installer really grants rather than a copy of them.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${HERE}/../.." && pwd)"
ENGINE="${ENGINE:-$(command -v podman || command -v docker || true)}"
[[ -n "${ENGINE}" ]] || { echo "Need podman or docker" >&2; exit 1; }
PYTHON="${PYTHON:-${REPO_DIR}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || PYTHON=python3
# The role is read out of a YAML file below, so say what is missing here rather
# than failing with a traceback halfway through.
"${PYTHON}" -c 'import yaml' 2>/dev/null \
  || { echo "${PYTHON} has no PyYAML. Run 'make venv', or set PYTHON to one that has it." >&2; exit 1; }
IMAGE="${IMAGE:-opensearchproject/opensearch:2.19.1}"
NAME="${NAME:-mmd-authz}"
PORT="${PORT:-19200}"

# Throwaway, and never written anywhere. OpenSearch refuses a password that looks
# like the username it belongs to, so the Mattermost one is random-looking.
ADMIN_PW='Parthenon-Authz-1!'
MM_PW='Kq7wZr2mTv9xLb4n'

cleanup() { "${ENGINE}" rm -f "${NAME}" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "==> starting ${IMAGE}"
# system_indices.enabled matches roles/opensearch/templates/opensearch.yml.j2.
# It is what keeps the security index out of an ordinary search.
"${ENGINE}" run -d --name "${NAME}" -p "${PORT}:9200" \
  -e discovery.type=single-node \
  -e "OPENSEARCH_INITIAL_ADMIN_PASSWORD=${ADMIN_PW}" \
  -e "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m" \
  -e "plugins.security.system_indices.enabled=true" \
  "${IMAGE}" >/dev/null

BASE="https://127.0.0.1:${PORT}"
SEC="${BASE}/_plugins/_security/api"
A=(-s -k -u "admin:${ADMIN_PW}")
M=(-s -k -u "mattermost:${MM_PW}")

echo "==> waiting for the cluster"
for _ in $(seq 1 90); do
  [[ "$(curl "${A[@]}" -o /dev/null -w '%{http_code}' "${BASE}/_cluster/health" || true)" == 200 ]] && break
  sleep 2
done
[[ "$(curl "${A[@]}" -o /dev/null -w '%{http_code}' "${BASE}/_cluster/health" || true)" == 200 ]] \
  || { echo "OpenSearch never came up"; "${ENGINE}" logs "${NAME}" | tail -30; exit 1; }

ROLE_JSON="$("${PYTHON}" -I - "${REPO_DIR}/roles/opensearch/defaults/main.yml" <<'PY'
import json, re, sys, yaml
# An Ansible defaults file. The only template inside the role definition is the
# index prefix, which is empty by default and empty is what we want to test.
text = re.sub(r"\{\{[^}]*\}\}", "", open(sys.argv[1], encoding="utf-8").read())
print(json.dumps(yaml.safe_load(text)["opensearch_mattermost_role"]))
PY
)"
echo "==> role under test: ${ROLE_JSON}"

FAILED=0
code() { curl "$@" -o /dev/null -w '%{http_code}'; }
expect() {
  if [[ "$2" == "$3" ]]; then printf 'ok    %-40s %s\n' "$1" "$3"
  else printf 'FAIL  %-40s got %s, wanted %s\n' "$1" "$3" "$2"; FAILED=$((FAILED + 1)); fi
}
section() { printf '\n== %s\n' "$*"; }

section "applying the account, role and mapping"
curl "${A[@]}" -X PUT -H 'Content-Type: application/json' \
  -d "{\"password\":\"${MM_PW}\",\"description\":\"Mattermost server connection\"}" \
  "${SEC}/internalusers/mattermost"; echo
curl "${A[@]}" -X PUT -H 'Content-Type: application/json' \
  -d "${ROLE_JSON}" "${SEC}/roles/mattermost_search"; echo
curl "${A[@]}" -X PUT -H 'Content-Type: application/json' \
  -d '{"users":["mattermost"]}' "${SEC}/rolesmapping/mattermost_search"; echo

section "what Mattermost has to be able to do"
expect "create an index"        200 "$(code "${M[@]}" -X PUT "${BASE}/channels")"
expect "put a mapping"          200 "$(code "${M[@]}" -X PUT -H 'Content-Type: application/json' \
                                     -d '{"properties":{"name":{"type":"text"}}}' "${BASE}/channels/_mapping")"
expect "index a document"       201 "$(code "${M[@]}" -X PUT -H 'Content-Type: application/json' \
                                     -d '{"name":"town-square"}' "${BASE}/channels/_doc/1")"
expect "bulk index"             200 "$(code "${M[@]}" -X POST -H 'Content-Type: application/x-ndjson' \
                                     --data-binary $'{"index":{"_index":"posts_2026_11","_id":"1"}}\n{"message":"hello"}\n' \
                                     "${BASE}/_bulk")"
expect "put an index template"  200 "$(code "${M[@]}" -X PUT -H 'Content-Type: application/json' \
                                     -d '{"index_patterns":["posts_*"],"settings":{"number_of_shards":1}}' \
                                     "${BASE}/_template/mattermost_posts")"
expect "refresh an index"       200 "$(code "${M[@]}" -X POST "${BASE}/channels/_refresh")"
expect "search"                 200 "$(code "${M[@]}" "${BASE}/*/_search?size=0")"
expect "get field mappings"     200 "$(code "${M[@]}" "${BASE}/channels/_mapping/field/name")"
expect "get index settings"     200 "$(code "${M[@]}" "${BASE}/channels/_settings")"
expect "index stats"            200 "$(code "${M[@]}" "${BASE}/channels/_stats")"
expect "cat indices"            200 "$(code "${M[@]}" "${BASE}/_cat/indices/channels")"
expect "cluster health"         200 "$(code "${M[@]}" "${BASE}/_cluster/health")"
expect "delete an index"        200 "$(code "${M[@]}" -X DELETE "${BASE}/posts_2026_11")"

section "what it must not be able to do"
expect "read the security API"  403 "$(code "${M[@]}" "${SEC}/internalusers")"
expect "widen its own role"     403 "$(code "${M[@]}" -X PUT -H 'Content-Type: application/json' \
                                     -d '{"cluster_permissions":["*"]}' "${SEC}/roles/mattermost_search")"
expect "write cluster settings" 403 "$(code "${M[@]}" -X PUT -H 'Content-Type: application/json' \
                                     -d '{"persistent":{"action.auto_create_index":"false"}}' "${BASE}/_cluster/settings")"
expect "add a snapshot repo"    403 "$(code "${M[@]}" -X PUT -H 'Content-Type: application/json' \
                                     -d '{"type":"fs","settings":{"location":"/tmp/x"}}' "${BASE}/_snapshot/leak")"
expect "reroute the cluster"    403 "$(code "${M[@]}" -X POST "${BASE}/_cluster/reroute")"
# Mattermost uses the older _template API, so the composable one is left out.
expect "composable template"    403 "$(code "${M[@]}" -X PUT -H 'Content-Type: application/json' \
                                     -d '{"index_patterns":["posts_*"],"template":{"settings":{"number_of_shards":1}}}' \
                                     "${BASE}/_index_template/mattermost_posts")"
# System-index protection strips the security index out of a search rather than
# refusing it, so what matters is that nothing comes back.
expect "security index is empty" 0 "$(curl "${M[@]}" "${BASE}/.opendistro_security/_search" \
                                     | sed -n 's/.*"hits":{"total":{"value":\([0-9]*\).*/\1/p')"

section "the admin account is untouched"
expect "admin reads the security API" 200 "$(code "${A[@]}" "${SEC}/internalusers")"
expect "admin reads cluster state"    200 "$(code "${A[@]}" "${BASE}/_cluster/state/version")"

echo
[[ "${FAILED}" -eq 0 ]] && { echo "All checks passed."; exit 0; }
echo "${FAILED} check(s) failed."
exit 1
