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
WITH_DEBS=false
WITH_JOB_IMAGES=auto
declare -a IMAGE_PINS=()
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
  --with-debs                   The same for Debian or Ubuntu: download the .debs install.sh
                                needs. Run this on Debian or Ubuntu, the same release as the
                                targets.
  --with-job-images             Also save the Calls recorder and transcriber container
                                images, so an air-gapped job server can record without
                                reaching Docker Hub. Needs Docker here. On by default when
                                the components include calls_offloader; --no-job-images
                                turns it off.
  --no-job-images               Leave the job runner images out of the bundle
  --job-image NAME=REF          Override a job image, e.g.
                                recorder=mattermost/calls-recorder:v0.8.8. These tags follow
                                the Calls plugin, not calls-offloader, so check them when
                                you upgrade Mattermost.
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
    --with-debs) WITH_DEBS=true; shift ;;
    --with-job-images) WITH_JOB_IMAGES=true; shift ;;
    --no-job-images) WITH_JOB_IMAGES=false; shift ;;
    --job-image) IMAGE_PINS+=("$2"); shift 2 ;;
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

# The job runner images are the one thing calls-offloader fetches at run time
# rather than at install: Mattermost names the image, and the service pulls it when
# the first recording starts. Save them here so an air-gapped job server can record.
if [[ "${WITH_JOB_IMAGES}" == auto ]]; then
  if [[ "${COMPONENTS}" == all || ",${COMPONENTS}," == *,calls_offloader,* ]]; then
    WITH_JOB_IMAGES=true
  else
    WITH_JOB_IMAGES=false
  fi
fi

if [[ "${WITH_JOB_IMAGES}" == true ]]; then
  if ! command -v docker >/dev/null; then
    log "Skipping the Calls job images: no docker here. The job server will pull them itself,"
    log "which needs a route to Docker Hub. Re-run this on a machine with Docker to include them."
  else
    log "Saving the Calls job runner images"
    mkdir -p "${OUT}/images"
    recorder="$(pinned_default calls_offloader_recorder_image)"
    transcriber="$(pinned_default calls_offloader_transcriber_image)"
    for pin in "${IMAGE_PINS[@]+"${IMAGE_PINS[@]}"}"; do
      case "${pin%%=*}" in
        recorder) recorder="${pin#*=}" ;;
        transcriber) transcriber="${pin#*=}" ;;
        *) die "--job-image takes recorder=REF or transcriber=REF" ;;
      esac
    done
    : > "${OUT}/images/manifest.txt"
    for ref in "${recorder}" "${transcriber}"; do
      [[ -z "${ref}" ]] && continue
      log "  ${ref}"
      docker pull --platform "linux/${ARCH}" "${ref}" >/dev/null \
        || die "could not pull ${ref}. Check the tag, or pass --job-image to change it."
      file="${OUT}/images/$(printf '%s' "${ref}" | tr '/:' '__').tar"
      docker save --output "${file}" "${ref}"
      printf '%s  %s  %s\n' "${ref}" "$(basename "${file}")" \
        "$(docker image inspect --format '{{index .RepoDigests 0}}' "${ref}" 2>/dev/null || echo no-digest)" \
        >> "${OUT}/images/manifest.txt"
    done
    log "Job images saved. The tags above follow the Calls plugin, so check them after a Mattermost upgrade."
  fi
fi

if [[ "${WITH_RPMS}" == true ]]; then
  command -v dnf >/dev/null || die "--with-rpms needs dnf (run it on RHEL)"
  log "Downloading prerequisite RPMs"
  mkdir -p "${OUT}/rpms"
  dnf download --resolve --alldeps --destdir "${OUT}/rpms" \
    ansible-core python3 gnupg2 tar curl policycoreutils-python-utils firewalld openssl iproute
fi

if [[ "${WITH_DEBS}" == true ]]; then
  command -v apt-get >/dev/null || die "--with-debs needs apt (run it on Debian or Ubuntu)"
  log "Downloading prerequisite packages"
  mkdir -p "${OUT}/debs"
  # Everything the prerequisites depend on, whether or not this machine has it already,
  # so a bare target can be installed from the folder alone.
  prereqs=(ansible-core python3 python3-apt gnupg tar openssl iproute2 curl ca-certificates)
  mapfile -t deb_names < <(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
    --no-breaks --no-replaces --no-enhances "${prereqs[@]}" | grep '^[a-z0-9]' | sort -u)
  (cd "${OUT}/debs" && apt-get download "${deb_names[@]}")
fi

log "Packing ${OUT}.tar.gz"
tar -C "$(dirname "${OUT}")" -czf "${OUT}.tar.gz" "$(basename "${OUT}")"
log "Done. Copy ${OUT}.tar.gz to the target, unpack it, then run:"
echo "    ./install.sh --offline-dir /path/to/$(basename "${OUT}")"
