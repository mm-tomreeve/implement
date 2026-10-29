#!/usr/bin/env bash
# pass and fail never return non-zero, so A && pass || fail is safe here.
# shellcheck disable=SC2015
# End-to-end functional checks, run inside the app server container after every
# server is installed. Exercises the whole path: Nginx (TLS) -> Mattermost ->
# PostgreSQL on db01, file storage, search, and the Calls plugin's view of rtcd.
set -euo pipefail

SITE="${SITE:-app01.e2e.test}"
USER_NAME=e2eadmin
USER_PASS='E2e-Passw0rd!x'
MMCTL=(/opt/mattermost/bin/mmctl --local)
CURL=(curl -sk --max-time 20 --resolve "${SITE}:443:127.0.0.1")
BASE="https://${SITE}/api/v4"
failures=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s (%s)\n' "$1" "$2"; failures=$((failures + 1)); }
json() { python3 -I -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# Set up a user and a team through the local admin socket.
"${MMCTL[@]}" user create --email e2e@example.test --username "${USER_NAME}" --password "${USER_PASS}" --system-admin >/dev/null 2>&1 || true
"${MMCTL[@]}" team create --name e2e --display-name E2E >/dev/null 2>&1 || true
"${MMCTL[@]}" team users add e2e "${USER_NAME}" >/dev/null 2>&1 || true

# Log in through Nginx over HTTPS.
headers="$(mktemp)"
"${CURL[@]}" -D "${headers}" -o /dev/null -H 'Content-Type: application/json' \
  -d "{\"login_id\":\"${USER_NAME}\",\"password\":\"${USER_PASS}\"}" "${BASE}/users/login"
TOKEN="$(awk 'tolower($1)=="token:" {print $2}' "${headers}" | tr -d '\r')"
if [[ -n "${TOKEN}" ]]; then pass "Log in over HTTPS through Nginx"; else fail "Log in over HTTPS through Nginx" "no token"; exit 1; fi
AUTH=(-H "Authorization: Bearer ${TOKEN}")

TEAM_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE}/teams/name/e2e" | json "d['id']")"
CHANNEL_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE}/teams/${TEAM_ID}/channels/name/town-square" | json "d['id']")"
[[ -n "${CHANNEL_ID}" ]] && pass "Team and channel exist in the remote database" || fail "Team and channel exist" "no channel id"

# Post, then read it back.
MARK="e2e-check-$(date +%s)"
POST_ID="$("${CURL[@]}" "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"channel_id\":\"${CHANNEL_ID}\",\"message\":\"hello from ${MARK}\"}" "${BASE}/posts" | json "d.get('id','')")"
READ_BACK="$("${CURL[@]}" "${AUTH[@]}" "${BASE}/posts/${POST_ID}" | json "d.get('message','')")"
[[ "${READ_BACK}" == "hello from ${MARK}" ]] && pass "Post a message and read it back" || fail "Post a message and read it back" "got '${READ_BACK}'"

# Upload a file and check it landed in the data directory.
upload="$(mktemp)"; echo "attachment ${MARK}" > "${upload}"
FILE_ID="$("${CURL[@]}" "${AUTH[@]}" -F "channel_id=${CHANNEL_ID}" -F "files=@${upload};filename=${MARK}.txt" "${BASE}/files" \
  | json "d['file_infos'][0]['id'] if d.get('file_infos') else ''")"
if [[ -n "${FILE_ID}" ]] && grep -rqs "attachment ${MARK}" /var/opt/mattermost/data; then
  pass "Upload a file and find it in the data directory"
else
  fail "Upload a file and find it in the data directory" "file id '${FILE_ID}'"
fi

# Search (database search without a licence; OpenSearch takes over once licensed).
sleep 2
HITS="$("${CURL[@]}" "${AUTH[@]}" -H 'Content-Type: application/json' -d "{\"terms\":\"${MARK}\"}" \
  "${BASE}/teams/${TEAM_ID}/posts/search" | json "len(d.get('order', []))")"
[[ "${HITS}" -ge 1 ]] && pass "Search finds the new post" || fail "Search finds the new post" "${HITS} hits"

# Websocket through Nginx with a real token.
WS="$("${CURL[@]}" -o /dev/null -w '%{http_code}' --max-time 3 --http1.1 -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "${AUTH[@]}" "https://${SITE}/api/v4/websocket" || true)"
[[ "${WS}" == 101 ]] && pass "Authenticated websocket upgrade" || fail "Authenticated websocket upgrade" "HTTP ${WS}"

# Calls plugin is running and configured for the remote rtcd.
CALLS="$("${CURL[@]}" "${AUTH[@]}" "${BASE}/plugins/statuses" | json "[p['state'] for p in d if p['plugin_id']=='com.mattermost.calls'][0] if any(p['plugin_id']=='com.mattermost.calls' for p in d) else -1")"
if [[ "${CALLS}" == 2 ]]; then
  pass "Calls plugin is running"
elif [[ "${CALLS}" == -1 && "$(uname -m)" == aarch64 ]]; then
  # Mattermost's arm64 tarballs ship no prepackaged plugins, Calls included.
  printf 'n/a   %s (%s)\n' "Calls plugin is running" "arm64 tarball has no prepackaged plugins"
else
  fail "Calls plugin is running" "state ${CALLS}"
fi
RTCD="$("${MMCTL[@]}" config get 'PluginSettings.Plugins' 2>/dev/null | python3 -I -c "import json,sys; print(json.load(sys.stdin).get('com.mattermost.calls',{}).get('rtcdserviceurl',''))")"
[[ "${RTCD}" == http://172.30.10.13:8045 ]] && pass "Calls uses rtcd on calls01" || fail "Calls uses rtcd on calls01" "'${RTCD}'"

echo
if [[ ${failures} -eq 0 ]]; then echo "All functional checks passed"; else echo "${failures} functional checks failed"; fi
exit "${failures}"
