#!/usr/bin/env bash
# Build an offline bundle on a machine that has internet access, then carry the
# tarball to the air-gapped servers and point install.sh --offline-dir at it.
#
# Every artifact is checked against the vendor checksum before it goes in, and
# Mattermost/OpenSearch signatures are checked too when gpg is available.
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULTS="${REPO_DIR}/inventory/group_vars/all/defaults.yml"

ARCH=amd64
EDITION=enterprise
CHANNEL=both
MM_VERSION=""
COMPONENTS=all
OUT=""
WITH_RPMS=false
ALLOW_UNVERIFIED=false
declare -a PINS=()

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

  --arch amd64|arm64            Target CPU architecture (default amd64)
  --edition enterprise|team     Mattermost edition (default enterprise)
  --channel both|latest-stable|latest-esr
                                Which Mattermost builds to include (default both)
  --mattermost-version X.Y.Z    Include this exact Mattermost version instead
  --components LIST             Comma-separated components, or 'all' (default all)
  --version NAME=VERSION        Override a pinned third-party version (repeatable)
  --with-rpms                   Also download the RPMs install.sh needs. Run this on
                                RHEL with the same major version as the targets.
  --allow-unverified            Accept artifacts that have no published checksum
  --out DIR                     Output directory (default ./mm-bundle-<date>)
  -h, --help                    Show this help

The result is DIR and DIR.tar.gz. On the target run:
  ./install.sh --offline-dir /path/to/DIR
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch) ARCH="$2"; shift 2 ;;
    --edition) EDITION="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --mattermost-version) MM_VERSION="$2"; CHANNEL=pinned; shift 2 ;;
    --components) COMPONENTS="$2"; shift 2 ;;
    --version) PINS+=("$2"); shift 2 ;;
    --with-rpms) WITH_RPMS=true; shift ;;
    --allow-unverified) ALLOW_UNVERIFIED=true; shift ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown option $1" ;;
  esac
done

command -v python3 >/dev/null || die "python3 is required"
OUT="${OUT:-$PWD/mm-bundle-$(date +%Y%m%d)}"
mkdir -p "${OUT}"
OUT="$(cd "${OUT}" && pwd)"

# Third-party versions default to the pins in defaults.yml, so a bundle matches
# what an online install would fetch.
pinned_default() {
  sed -n "s/^$1:[[:space:]]*\"\{0,1\}\([^\"#]*\)\"\{0,1\}.*/\1/p" "${DEFAULTS}" | head -n1 | tr -d '[:space:]'
}
declare -a VERSION_ARGS=()
for name in opensearch prometheus node_exporter postgres_exporter grafana rtcd nginx_exporter opensearch_exporter blackbox_exporter loki otel_collector; do
  value="$(pinned_default "${name}_version")"
  [[ -n "${value}" ]] && VERSION_ARGS+=(--version "${name}=${value}")
done
for pin in "${PINS[@]+"${PINS[@]}"}"; do
  VERSION_ARGS+=(--version "${pin}")
done

log "Downloading and verifying artifacts into ${OUT}"
declare -a EXTRA=()
[[ "${ALLOW_UNVERIFIED}" == true ]] && EXTRA+=(--allow-unverified)
[[ -n "${MM_VERSION}" ]] && EXTRA+=(--mattermost-version "${MM_VERSION}")
python3 -I "${REPO_DIR}/tools/resolve_versions.py" fetch-bundle \
  --components "${COMPONENTS}" --arch "${ARCH}" \
  --mattermost-edition "${EDITION}" --mattermost-channel "${CHANNEL}" \
  "${VERSION_ARGS[@]}" "${EXTRA[@]+"${EXTRA[@]}"}" --out "${OUT}"

log "Copying vendor GPG keys"
mkdir -p "${OUT}/keys"
cp "${REPO_DIR}"/roles/artifacts/files/*.gpg "${REPO_DIR}"/roles/artifacts/files/*.pgp "${OUT}/keys/"

log "Downloading Ansible collections"
mkdir -p "${OUT}/collections"
if command -v ansible-galaxy >/dev/null; then
  ansible-galaxy collection download -r "${REPO_DIR}/requirements.yml" -p "${OUT}/collections"
else
  # No Ansible on this machine: ask Galaxy for the newest ansible.posix directly.
  python3 -I - "${OUT}/collections" <<'PY'
import json, sys, urllib.request
api = "https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index/ansible/posix/versions/?is_highest=true"
with urllib.request.urlopen(api, timeout=60) as r:
    item = json.load(r)["data"][0]
with urllib.request.urlopen(item["href"] if item["href"].startswith("http") else "https://galaxy.ansible.com" + item["href"], timeout=60) as r:
    detail = json.load(r)
url = detail["download_url"]
dest = sys.argv[1] + "/" + url.rsplit("/", 1)[-1]
urllib.request.urlretrieve(url, dest)
print("saved", dest)
PY
fi

# A mirror served over HTTP cannot be listed, so record the collection files by name.
for f in "${OUT}"/collections/*.tar.gz; do basename "${f}"; done | sort > "${OUT}/collections/manifest.txt"

if [[ "${WITH_RPMS}" == true ]]; then
  command -v dnf >/dev/null || die "--with-rpms needs dnf (run it on RHEL)"
  log "Downloading prerequisite RPMs"
  mkdir -p "${OUT}/rpms"
  dnf download --resolve --alldeps --destdir "${OUT}/rpms" \
    ansible-core python3 gnupg2 tar curl policycoreutils-python-utils firewalld openssl
fi

log "Packing ${OUT}.tar.gz"
tar -C "$(dirname "${OUT}")" -czf "${OUT}.tar.gz" "$(basename "${OUT}")"
log "Done. Copy ${OUT}.tar.gz to the target, unpack it, then run:"
echo "    ./install.sh --offline-dir /path/to/$(basename "${OUT}")"
