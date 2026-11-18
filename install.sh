#!/usr/bin/env bash
# Mattermost component installer for RHEL 9 and 10, Debian 12 and later, and Ubuntu 24.04 and later.
#
# Asks which components go on THIS server (or reads an answer file), installs
# Ansible and what it needs, deploys from verified tarballs, runs the post-deploy
# tests and writes an architecture document for the server.
#
#   sudo ./install.sh                         interactive
#   sudo ./install.sh --answers answers.yml   from an answer file
#   sudo ./install.sh --estate estate.yml     from an architecture wizard plan (see estate.example.yml)
#   sudo ./install.sh --offline-dir /srv/mm-bundle-20261028
#   sudo ./install.sh --verify-only           re-run the tests and refresh the report
#   sudo ./install.sh --uninstall             take components off this server again
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR=/etc/mattermost-parthenon
STATE_DIR=/var/lib/mattermost-parthenon
LOG_DIR=/var/log/mattermost-parthenon
SAVED_ANSWERS="${CONFIG_DIR}/answers.yml"
MIN_ANSIBLE_CORE=2.14
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"

# Keep these in step with inventory/group_vars/all/profiles.yml and catalogue.yml.
# tests/test_repo_consistency.py fails if they drift.
PROFILE_NAMES=(core app database calls jobs search storage monitoring metrics dashboards all-in-one)
declare -A PROFILE_COMPONENTS=(
  [core]="postgresql mattermost nginx"
  [app]="mattermost nginx"
  [database]="postgresql"
  [calls]="rtcd"
  [jobs]="calls_offloader"
  [search]="opensearch"
  [storage]="nfs"
  [monitoring]="prometheus loki grafana"
  [metrics]="prometheus loki"
  [dashboards]="grafana"
  [all-in-one]="postgresql mattermost nginx rtcd calls_offloader opensearch prometheus loki grafana"
)
declare -A PROFILE_DESCRIPTIONS=(
  [core]="PostgreSQL, Mattermost and Nginx on one server. Fine for small teams."
  [app]="Mattermost application server with its Nginx proxy."
  [database]="Dedicated PostgreSQL server for Mattermost."
  [calls]="Dedicated Calls media server (rtcd)."
  [jobs]="Call recording, transcription and live captions (calls-offloader)."
  [search]="Dedicated OpenSearch node for Enterprise search."
  [storage]="NFS file storage for the Mattermost data directory."
  [monitoring]="Prometheus, Loki and Grafana with the Mattermost dashboards and logs."
  [metrics]="Prometheus and Loki only, for estates big enough to keep Grafana separate."
  [dashboards]="Grafana on its own, pointed at a metrics server."
  [all-in-one]="Everything on one box. Lab and proof-of-concept use only."
)
COMPONENT_NAMES=(postgresql mattermost nginx rtcd calls_offloader opensearch nfs prometheus loki grafana node_exporter postgres_exporter nginx_exporter opensearch_exporter blackbox_exporter otel_collector)

ANSWERS=""
ESTATE=""
ESTATE_NODE=""
ESTATE_SECRETS=""
VAULT_PASSWORD_FILE=""
# Working state for the estate helpers: the plan on disk, this server's addresses,
# and the last run's output.
ESTATE_FILE=""
ESTATE_ADDR_ARGS=()
ESTATE_OUT=""
ESTATE_ERRFILE=""
UNINSTALL_STUB=""
UNINSTALL_PLAN=""
PROFILES=""
COMPONENTS=""
OFFLINE_SOURCE=""
LOCAL_REPO=""
OS_FAMILY=rhel
INTERNET_OVERRIDE=""
NON_INTERACTIVE=false
ASSUME_YES=false
CHECK_MODE=false
ALLOW_UPGRADE=false
MODE=install
PURGE=false
ONLINE=false
EDIT_ANSWERS=false

# While going through earlier answers: what each key was, and the estate servers as
# "name|address|component,component".
EDITING=false
declare -A PREV=()
PREV_ESTATE=()
PREV_FILE=""

# ------------------------------------------------------------------ output

# Mattermost brand colours: Denim #1E325C, Sunglow #FFBC1F.
#
# Nothing you have to read is coloured. Terminals come in every shade, and a
# colour that reads well on one is unreadable on another: Sapphire manages
# 2.7:1 on a dark background and Sunglow 1.7:1 on a light one, against a 4.5:1
# bar. So text keeps the terminal's own foreground, and colour is left for the
# short marks beside it, where the glyph and its position carry the meaning
# anyway. Red and green stay for failed and passed, which everyone reads the
# same way.
#
# Truecolor where the terminal says it can, the nearest 256-colour match
# otherwise, and nothing at all for pipes, dumb terminals or NO_COLOR.
C_BOLD=""; C_DIM=""; C_RED=""; C_GRN=""; C_OFF=""; C_YEL=""; C_BANNER=""; C_BAR=""
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != dumb ]]; then
  C_BOLD=$'\e[1m'; C_DIM=$'\e[2m'; C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_OFF=$'\e[0m'
  if [[ "${COLORTERM:-}" == truecolor || "${COLORTERM:-}" == 24bit ]]; then
    C_YEL=$'\e[38;2;255;188;31m'                       # Sunglow
    C_BANNER=$'\e[48;2;30;50;92m\e[38;2;255;255;255m'  # white on Denim
    C_BAR=$'\e[48;2;30;50;92m\e[38;2;255;188;31m'      # Sunglow on Denim
  else
    C_YEL=$'\e[38;5;214m'
    C_BANNER=$'\e[48;5;17m\e[38;5;231m'
    C_BAR=$'\e[48;5;17m\e[38;5;214m'
  fi
fi
# Box-drawing characters only when the locale can show them.
if [[ "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" == *[Uu][Tt][Ff]-8* || "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" == *[Uu][Tt][Ff]8* ]]; then
  G_RULE="━"; G_BAR="▌"; G_ARROW="▸"; G_DOT="·"
else
  G_RULE="="; G_BAR="|"; G_ARROW=">"; G_DOT="-"
fi
WIDTH=64

# Steps, warnings and errors also go to the run log, so a run that stops before
# Ansible starts still says how far it got and why. Nothing is logged until
# setup_logging has created the file.
log_line() {
  [[ -n "${LOG_FILE:-}" ]] || return 0
  printf '%s install.sh | %s\n' "$(date '+%F %T')" "$*" | sed $'s/\e\\[[0-9;]*m//g' >> "${LOG_FILE}" 2>/dev/null || true
}
say()  { log_line "$*"; printf '%s\n' "${C_YEL}${C_BOLD}${G_ARROW}${C_OFF} ${C_BOLD}$*${C_OFF}"; }
info() { printf '    %s\n' "$*"; }
warn() { log_line "WARNING: $*"; printf '%s\n' "${C_YEL}${C_BOLD}WARNING:${C_OFF} $*" >&2; }
die()  { log_line "ERROR: $*"; printf '%s\n' "${C_RED}${C_BOLD}ERROR:${C_OFF} $*" >&2; exit 1; }

no_input() {
  echo
  die "Ran out of input while asking questions. For unattended installs use --answers (see answers.example.yml)."
}

repeat() { local i out=""; for ((i = 0; i < $2; i++)); do out+="$1"; done; printf '%s' "${out}"; }

# A section heading in the interview: a Sapphire rule with the title in it.
section() {
  local title=" $1 " lead=3
  printf '\n%s%s%s%s%s%s\n' "${C_YEL}" "$(repeat "${G_RULE}" "${lead}")" "${C_OFF}${C_BOLD}" "${title}" \
    "${C_OFF}${C_YEL}" "$(repeat "${G_RULE}" $((WIDTH - lead - ${#title})))${C_OFF}"
}

# The Denim banner with a Sunglow bar down the left edge.
banner() {
  local line pad
  echo
  for line in "" "  Mattermost Parthenon" "  $1" ""; do
    pad=$((WIDTH - 1 - ${#line})); (( pad < 0 )) && pad=0
    if [[ "${line}" == "  Mattermost Parthenon" ]]; then
      printf '%s%s%s%s%s%s\n' "${C_BAR}" "${G_BAR}" "${C_BANNER}${C_BOLD}" "${line}" "$(repeat ' ' "${pad}")" "${C_OFF}"
    else
      printf '%s%s%s%s%s%s\n' "${C_BAR}" "${G_BAR}" "${C_BANNER}" "${line}" "$(repeat ' ' "${pad}")" "${C_OFF}"
    fi
  done
  echo
}
# The terminal's settings from before one was changed, to put back on the way out.
TTY_STATE=""
tty_restore() {
  if [[ -n "${TTY_STATE}" ]]; then stty "${TTY_STATE}" 2>/dev/null || true; fi
  TTY_STATE=""
  return 0
}
trap 'die "install.sh stopped at line ${LINENO}. Logs are in ${LOG_DIR}."' ERR
trap 'echo; log_line "Stopped with Ctrl-C."; exit 130' INT
trap tty_restore EXIT

usage() {
  cat <<EOF
Mattermost Parthenon: install Mattermost components on this RHEL, Debian or Ubuntu server.

  Parthenon /ˈpɑːθənɒn/ (n.) A temple built to house something important.

Usage: sudo ./install.sh [options]

What to install
  --answers FILE          Use an answer file (see answers.example.yml)
  --estate FILE|-         An estate plan from the architecture wizard, as a file or
                          - to paste it in (see estate.example.yml)
  --estate-node NAME      Which node in the plan this server is, when it can't tell
  --estate-secrets FILE   Take this server's passwords from an estate secrets file
                          made with tools/estate_plan.py new-secrets
  --vault-password-file F Passphrase file for that estate secrets file
  --edit                  Ask the questions again with the saved answers (or --answers FILE)
                          as the defaults, so you only change what you need
  --profile LIST          Profiles for this server, comma-separated:
                          ${PROFILE_NAMES[*]}
  --components LIST       Extra or individual components, comma-separated:
                          ${COMPONENT_NAMES[*]}

Where to get it
  --offline-dir DIR|URL   Offline bundle directory or internal mirror URL
  --local-repo PATH       RHEL only: add a temporary dnf repo, e.g. a mounted RHEL ISO
  --online | --offline    Skip the internet check and say which it is

How to run
  --non-interactive       Never prompt. Needs --answers or --profile.
  -y, --yes               Do not ask for confirmation
  --upgrade               Let an installed Mattermost (and rtcd, calls-offloader) move to a newer release
                          (it is stopped and its database dumped first)
  --check                 Dry run (Ansible check mode, best effort)
  --preflight             With --estate: check this server can resolve and reach
                          the rest of the estate, and has the memory and CPU for
                          its role. Changes nothing.
  --plan-only             Show what would be installed, then stop
  --verify-only           Re-run the post-deploy tests and refresh the report
  --report-only           Rebuild the report from the last test results

Taking components off
  --uninstall             Remove components from this server: stop and disable the
                          services, close their ports, delete the installs, units and
                          generated config, and remove the PostgreSQL and Nginx packages. With no
                          --profile or --components it removes everything the last install
                          put here. The database, uploaded files, search indices, metrics
                          and logs are kept.
  --purge                 With --uninstall: delete that data too, plus the service users
                          and, once nothing is left, the secrets and saved answers. This
                          cannot be undone.
  -h, --help              Show this help

Logs go to ${LOG_DIR}. Answers are saved to ${SAVED_ANSWERS}.
EOF
}

# ------------------------------------------------------------------ args

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --answers) ANSWERS="$2"; shift 2 ;;
      --estate) ESTATE="$2"; shift 2 ;;
      --estate-node) ESTATE_NODE="$2"; shift 2 ;;
      --estate-secrets) ESTATE_SECRETS="$2"; shift 2 ;;
      --vault-password-file) VAULT_PASSWORD_FILE="$2"; shift 2 ;;
      --preflight) MODE=preflight; shift ;;
      --edit) EDIT_ANSWERS=true; shift ;;
      --profile|--profiles) PROFILES="$2"; shift 2 ;;
      --components) COMPONENTS="$2"; shift 2 ;;
      --offline-dir) OFFLINE_SOURCE="$2"; shift 2 ;;
      --local-repo) LOCAL_REPO="$2"; shift 2 ;;
      --online) INTERNET_OVERRIDE=yes; shift ;;
      --offline) INTERNET_OVERRIDE=no; shift ;;
      --non-interactive) NON_INTERACTIVE=true; shift ;;
      -y|--yes) ASSUME_YES=true; shift ;;
      --upgrade) ALLOW_UPGRADE=true; shift ;;
      --check) CHECK_MODE=true; shift ;;
      --plan-only) MODE=plan; shift ;;
      --verify-only) MODE=verify; shift ;;
      --report-only) MODE=report; shift ;;
      --uninstall) MODE=uninstall; shift ;;
      --purge) PURGE=true; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage; die "Unknown option: $1" ;;
    esac
  done
  [[ -t 0 ]] || NON_INTERACTIVE=true
  if [[ "${EDIT_ANSWERS}" == true ]]; then
    [[ "${MODE}" == install || "${MODE}" == plan ]] || die "--edit goes with an install or --plan-only."
    [[ "${NON_INTERACTIVE}" != true ]] || die "--edit asks the questions again, so it needs a terminal."
  fi
  if [[ -n "${ANSWERS}" ]]; then
    [[ -r "${ANSWERS}" ]] || die "Cannot read answer file ${ANSWERS}"
    ANSWERS="$(cd "$(dirname "${ANSWERS}")" && pwd)/$(basename "${ANSWERS}")"
  fi
  if [[ -n "${ESTATE}" ]]; then
    [[ -z "${ANSWERS}" ]] || die "--estate and --answers don't go together."
    [[ "${EDIT_ANSWERS}" != true ]] || die "Run --estate first, then ./install.sh --edit as a second step to add secrets and file paths."
    if [[ "${ESTATE}" != - ]]; then
      [[ -r "${ESTATE}" ]] || die "Cannot read estate plan ${ESTATE}"
      ESTATE="$(cd "$(dirname "${ESTATE}")" && pwd)/$(basename "${ESTATE}")"
    fi
  fi
  [[ -z "${ESTATE_NODE}" || -n "${ESTATE}" ]] || die "--estate-node only makes sense with --estate."
}

# ------------------------------------------------------------------ host checks

require_root() {
  [[ ${EUID} -eq 0 ]] || die "Run this as root (sudo ./install.sh)."
}

check_os() {
  [[ -r /etc/os-release ]] || die "No /etc/os-release. This toolkit needs RHEL 9 or 10, Debian 12 or later, or Ubuntu 24.04 or later."
  # shellcheck disable=SC1091
  . /etc/os-release
  local major="${VERSION_ID%%.*}"
  case "${ID}" in
    rhel)
      OS_FAMILY=rhel
      [[ "${major}" == 9 || "${major}" == 10 ]] || die "${PRETTY_NAME} is not supported. Use RHEL 9 or 10." ;;
    rocky|almalinux|ol|centos)
      OS_FAMILY=rhel
      warn "${PRETTY_NAME} is not RHEL. It should work, but test it before you rely on it."
      [[ "${major}" == 9 || "${major}" == 10 ]] || die "${PRETTY_NAME} is not supported. Use version 9 or 10." ;;
    debian)
      OS_FAMILY=debian
      [[ "${major}" =~ ^[0-9]+$ && "${major}" -ge 12 ]] || die "${PRETTY_NAME} is not supported. Use Debian 12 or later." ;;
    ubuntu)
      OS_FAMILY=debian
      [[ "$(printf '%s\n%s\n' 24.04 "${VERSION_ID}" | sort -V | head -n1)" == 24.04 ]] || die "${PRETTY_NAME} is not supported. Use Ubuntu 24.04 or later." ;;
    *) die "${PRETTY_NAME} is not supported. Use RHEL 9 or 10, Debian 12 or later, or Ubuntu 24.04 or later." ;;
  esac
  case "$(uname -m)" in
    x86_64|aarch64) ;;
    *) die "CPU architecture $(uname -m) is not supported." ;;
  esac
  OS_PRETTY="${PRETTY_NAME}"
}

setup_logging() {
  mkdir -p "${LOG_DIR}" "${CONFIG_DIR}" "${STATE_DIR}"
  chmod 0700 "${CONFIG_DIR}"
  chmod 0750 "${LOG_DIR}" "${STATE_DIR}"
  LOG_FILE="${LOG_DIR}/run-${RUN_ID}.log"
  : > "${LOG_FILE}"
  chmod 0640 "${LOG_FILE}"
}

# Minimal images may not have the hostname command; uname is always there.
host_name() { hostname -f 2>/dev/null || uname -n; }

tool_version() {
  local base="unknown" sha=""
  [[ -r "${REPO_DIR}/VERSION" ]] && base="$(tr -d '[:space:]' < "${REPO_DIR}/VERSION")"
  if command -v git >/dev/null; then
    sha="$(git -C "${REPO_DIR}" describe --always --dirty 2>/dev/null || true)"
  fi
  echo "${base}${sha:+ (${sha})}"
}

# Read a top-level scalar from a simple YAML file. Good enough for our own keys.
yaml_get() {
  local key="$1" file="$2"
  [[ -r "${file}" ]] || return 0
  sed -n -E "s/^${key}:[[:space:]]*['\"]?([^'\"#]*)['\"]?[[:space:]]*(#.*)?$/\1/p" "${file}" | head -n1 | sed 's/[[:space:]]*$//'
}

# ------------------------------------------------------------------ connectivity

# ------------------------------------------------------------------ installer sources

# Every site an online install may download from, and why.
SOURCE_HOSTS=(releases.mattermost.com docs.mattermost.com github.com release-assets.githubusercontent.com
              api.github.com artifacts.opensearch.org dl.grafana.com grafana.com galaxy.ansible.com)

declare -A SOURCE_PURPOSE=(
  [releases.mattermost.com]="Mattermost server tarballs and their signatures"
  [docs.mattermost.com]="the list of current releases (latest and ESR)"
  [github.com]="rtcd, calls-offloader, Prometheus and the exporters"
  [release-assets.githubusercontent.com]="where GitHub actually serves those files from"
  [api.github.com]="looking up 'latest' versions"
  [artifacts.opensearch.org]="OpenSearch and its ICU plugin"
  [dl.grafana.com]="Grafana"
  [grafana.com]="Grafana checksums"
  [galaxy.ansible.com]="the ansible.posix collection Ansible needs"
)
declare -A SOURCE_OK=()
GALAXY_OK=false

list_sources() {
  local h
  for h in "${SOURCE_HOSTS[@]}"; do
    printf '      %s %s\n' "${C_BOLD}$(printf '%-38s' "${h}")${C_OFF}" "${C_DIM}${SOURCE_PURPOSE[${h}]}${C_OFF}"
  done
  # The components aren't chosen yet when this list is shown, so the job service's
  # two sites are listed with the condition rather than left out. Neither is needed
  # during the install: it pulls its runner images at the first recording.
  printf '      %s %s\n' "${C_BOLD}$(printf '%-38s' 'download.docker.com')${C_OFF}" \
    "${C_DIM}Docker Engine on RHEL, if this server runs the Calls job service${C_OFF}"
  printf '      %s %s\n' "${C_BOLD}$(printf '%-38s' 'registry-1.docker.io, auth.docker.io')${C_OFF}" \
    "${C_DIM}the Calls recorder and transcriber images, at the first recording${C_OFF}"
}

# Check every source in parallel and print each result as it comes in. Any HTTP
# answer counts as reachable (api.github.com answers 403 when it rate-limits); only
# a network failure or timeout counts as down.
check_sources() {
  local tmp h failed=0
  tmp="$(mktemp -d)"
  for h in "${SOURCE_HOSTS[@]}"; do
    ( status=fail mark="${C_RED}${C_BOLD}FAIL${C_OFF}"
      if curl -sS -o /dev/null --max-time 5 --retry 1 --retry-delay 2 "https://${h}/" 2>/dev/null; then
        status=ok mark="${C_GRN}${C_BOLD}ok  ${C_OFF}"
      fi
      echo "${status}" > "${tmp}/${h}"
      printf '      %s %s %s\n' "${mark}" "$(printf '%-38s' "${h}")" "${C_DIM}${SOURCE_PURPOSE[${h}]}${C_OFF}" ) &
  done
  wait
  for h in "${SOURCE_HOSTS[@]}"; do
    SOURCE_OK[${h}]="$(cat "${tmp}/${h}")"
    [[ "${SOURCE_OK[${h}]}" == ok ]] || failed=$((failed + 1))
  done
  rm -rf "${tmp}"
  [[ "${SOURCE_OK[galaxy.ansible.com]}" == ok ]] && GALAXY_OK=true
  return "${failed}"
}

# Show what a folder or mirror holds. Returns non-zero if there is nothing usable.
show_inventory() {
  local src="$1" arch
  arch="$( [[ "$(uname -m)" == aarch64 ]] && echo arm64 || echo amd64 )"
  if ! command -v python3 >/dev/null; then
    [[ -d "${src}" || "${src}" =~ ^https?:// ]] && return 0
    warn "${src} does not exist."; return 1
  fi
  python3 -I "${REPO_DIR}/tools/resolve_versions.py" inventory --source "${src}" --arch "${arch}" 2>&1 \
    | python3 -I -c '
import json, sys
raw = sys.stdin.read()
bold, dim, yel, red, grn, off, src = sys.argv[1:8]
try:
    inv = json.loads(raw)
except ValueError:
    print("    %s%s%s" % (red, raw.strip().replace("ERROR: ", ""), off))
    sys.exit(1)
arts = inv["artifacts"]
if not arts:
    print("    %sNothing I recognise in %s for %s.%s" % (red, src, inv["arch"], off))
    print("    %sExpected a bundle from tools/build-offline-bundle.sh, or files named as the vendors publish them,%s" % (dim, off))
    print("    %se.g. mattermost-11.7.14-linux-%s.tar.gz with its .sig next to it.%s" % (dim, inv["arch"], off))
    sys.exit(1)
print("    Found in %s%s%s (%s, %s):" % (bold, src, off, "bundle with manifest" if inv["kind"] == "bundle" else "folder", inv["arch"]))
for a in arts:
    extra = " ".join(x for x in [a["edition"], "/".join(a["channels"])] if x)
    checks = []
    checks.append((grn + "checksum" + off) if a["checksum"] else (yel + "no checksum" + off))
    if a["name"] in ("mattermost", "opensearch", "opensearch_icu"):
        checks.append((grn + "signature" + off) if a["signature"] else (red + "no signature" + off))
    version = a["version"] if a["version"] != "unknown" else "(no version in name)"
    print("      %s%-20s%s %-10s %-24s %s" % (bold, a["name"], off, version, extra, ", ".join(checks)))
ex = inv["extras"]
print("      %s%-20s%s %s" % (bold, "ansible collections", off, (grn + "present" + off) if ex["collections"] else (dim + "not included" + off)))
print("      %s%-20s%s %s" % (bold, "prerequisite packages", off, (grn + "present" + off) if (ex["rpms"] or ex["debs"]) else (dim + "not included" + off)))
' "${C_BOLD}" "${C_DIM}" "${C_YEL}" "${C_RED}" "${C_GRN}" "${C_OFF}" "${src}"
}

prompt_for_source() {
  local src
  info "Give a bundle made with tools/build-offline-bundle.sh,"
  info "an internal mirror URL, or a folder of the vendors' files as downloaded."
  while true; do
    ask src "Folder or URL with the installers (q to stop)" "${OFFLINE_SOURCE:-/srv/mm-bundle}"
    if [[ "${src}" == q ]]; then
      say "Stopped. Nothing was changed."
      exit 0
    fi
    if show_inventory "${src}" && ask_yn "Use these installers?" y; then
      OFFLINE_SOURCE="${src}"
      ONLINE=false
      return 0
    fi
  done
}

# Decide where installers come from: the internet (after asking), or a folder.
# An answer file or flags settle it without questions.
choose_installer_source() {
  local setting="${INTERNET_OVERRIDE}" failed=0 choice
  if [[ -n "${ANSWERS}" ]]; then
    [[ -n "${setting}" ]] || setting="$(yaml_get deploy_internet "${ANSWERS}")"
    [[ -n "${OFFLINE_SOURCE}" ]] || OFFLINE_SOURCE="$(yaml_get artifact_source "${ANSWERS}")"
  fi
  section "Where the installers come from"

  if [[ -n "${OFFLINE_SOURCE}" && "${setting}" != yes ]]; then
    show_inventory "${OFFLINE_SOURCE}" || die "Can't use ${OFFLINE_SOURCE} for the installers. Fix artifact_source or --offline-dir."
    ONLINE=false
    return 0
  fi

  case "${setting}" in
    yes|true|online)
      ONLINE=true; GALAXY_OK=true
      info "The answer file says this server has internet access, so the check is skipped."
      return 0 ;;
    no|false|offline)
      ONLINE=false
      info "The answer file says this server has no internet access."
      [[ "${NON_INTERACTIVE}" == true ]] && die "No artifact_source set. Add it to the answer file or pass --offline-dir."
      prompt_for_source
      return 0 ;;
  esac

  # A run that went online saved deploy_internet: auto, so that answer stands as
  # agreement to check again rather than asking every time.
  if [[ -z "${setting}" && -z "${ANSWERS}" && "$(yaml_get deploy_internet "${SAVED_ANSWERS}")" == auto ]]; then
    info "Checking the installer sources, as set in ${SAVED_ANSWERS}."
  elif [[ -z "${setting}" && "${NON_INTERACTIVE}" != true ]]; then
    info "An online install downloads from these sites:"
    list_sources
    [[ -n "${https_proxy:-${HTTPS_PROXY:-}}" ]] && info "${C_DIM}Using the proxy in https_proxy: ${https_proxy:-${HTTPS_PROXY:-}}${C_OFF}"
    if ! ask_yn "Check that this server can reach them?" y; then
      info "Skipped. Using a local copy of the installers."
      prompt_for_source
      return 0
    fi
  fi

  say "Checking the installer sources"
  check_sources || failed=$?
  if [[ "${failed}" -eq 0 ]]; then
    ONLINE=true
    info "All installer sources are reachable. Downloading from the vendors."
    return 0
  fi

  echo
  warn "${failed} of ${#SOURCE_HOSTS[@]} installer sources can't be reached from this server."
  if [[ "${NON_INTERACTIVE}" == true ]]; then
    die "Set artifact_source in the answer file (or pass --offline-dir) to a bundle from tools/build-offline-bundle.sh, or open up the sites marked FAIL."
  fi
  ask_choice choice "How do you want to continue?" "Use a local folder or mirror" \
    "Use a local folder or mirror" "Carry on online anyway" "Stop here"
  case "${choice}" in
    "Use a local folder or mirror") prompt_for_source ;;
    "Carry on online anyway")
      ONLINE=true
      warn "Carrying on. Anything this server needs from a FAIL site will stop the install later." ;;
    *) say "Stopped. Nothing was changed."; exit 0 ;;
  esac
}

setup_local_repo() {
  [[ "${OS_FAMILY}" == rhel ]] || die "--local-repo is for RHEL. On ${OS_PRETTY}, point apt at your mirror in /etc/apt/sources.list.d instead."
  [[ -d "${LOCAL_REPO}" ]] || die "--local-repo ${LOCAL_REPO} is not a directory"
  say "Adding temporary dnf repos from ${LOCAL_REPO}"
  local repo_file=/etc/yum.repos.d/mattermost-parthenon-local.repo
  : > "${repo_file}"
  local sub
  for sub in BaseOS AppStream; do
    if [[ -d "${LOCAL_REPO}/${sub}/repodata" ]]; then
      printf '[mmd-local-%s]\nname=mattermost-parthenon local %s\nbaseurl=file://%s/%s\nenabled=1\ngpgcheck=1\ngpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release\n\n' \
        "${sub}" "${sub}" "${LOCAL_REPO}" "${sub}" >> "${repo_file}"
    fi
  done
  if [[ ! -s "${repo_file}" && -d "${LOCAL_REPO}/repodata" ]]; then
    printf '[mmd-local]\nname=mattermost-parthenon local\nbaseurl=file://%s\nenabled=1\ngpgcheck=0\n' "${LOCAL_REPO}" > "${repo_file}"
  fi
  [[ -s "${repo_file}" ]] || die "No repodata found under ${LOCAL_REPO}"
  info "Wrote ${repo_file}. Remove it when you no longer need it."
}

# ------------------------------------------------------------------ prerequisites

# curl is left out of the RHEL list on purpose: minimal RHEL 9 ships curl-minimal, which
# conflicts with the curl package, and either gives us the command we need.
# firewalld is not a Debian or Ubuntu prerequisite: installing it starts it, and the roles
# first check that ufw isn't already in charge.
PREREQ_PACKAGES_RHEL=(ansible-core python3 python3-libselinux gnupg2 tar openssl policycoreutils-python-utils firewalld iproute)
PREREQ_PACKAGES_DEBIAN=(ansible-core python3 python3-apt gnupg tar openssl iproute2 curl ca-certificates)

deb_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed$'; }

install_prereqs_rhel() {
  local missing=() pkg
  for pkg in "${PREREQ_PACKAGES_RHEL[@]}"; do
    rpm -q "${pkg}" >/dev/null 2>&1 || missing+=("${pkg}")
  done
  command -v curl >/dev/null || missing+=(curl-minimal)
  [[ ${#missing[@]} -gt 0 ]] || return 0
  info "Installing: ${missing[*]}"
  dnf -y install "${missing[@]}" >> "${LOG_FILE}" 2>&1 && return 0
  local rpm_dir=""
  [[ -n "${OFFLINE_SOURCE}" && -d "${OFFLINE_SOURCE}/rpms" ]] && rpm_dir="${OFFLINE_SOURCE}/rpms"
  if [[ -n "${rpm_dir}" ]]; then
    info "The configured repos failed; using RPMs from ${rpm_dir}"
    dnf -y --disablerepo='*' install "${rpm_dir}"/*.rpm >> "${LOG_FILE}" 2>&1 \
      || die "Could not install prerequisites from ${rpm_dir}. See ${LOG_FILE}."
  else
    die "Could not install ${missing[*]}. Check the dnf repos (Satellite, a local mirror or --local-repo), or build a bundle with --with-rpms. See ${LOG_FILE}."
  fi
}

# The check of the installer sources runs before the prerequisites, and a bare Debian or
# Ubuntu server has no curl, so every site would show as down.
ensure_curl() {
  command -v curl >/dev/null && return 0
  [[ "${OS_FAMILY}" == debian ]] || return 0
  info "Installing curl, to check the installer sources"
  export DEBIAN_FRONTEND=noninteractive
  if ! { apt-get update && apt-get -y --no-install-recommends install curl ca-certificates; } >> "${LOG_FILE}" 2>&1; then
    warn "Could not install curl. The source check will fail; point --offline-dir at a bundle, or fix the apt sources."
  fi
}

install_prereqs_debian() {
  local missing=() pkg
  for pkg in "${PREREQ_PACKAGES_DEBIAN[@]}"; do
    deb_installed "${pkg}" || missing+=("${pkg}")
  done
  [[ ${#missing[@]} -gt 0 ]] || return 0
  info "Installing: ${missing[*]}"
  export DEBIAN_FRONTEND=noninteractive
  if apt-get update >> "${LOG_FILE}" 2>&1 \
     && apt-get -y --no-install-recommends install "${missing[@]}" >> "${LOG_FILE}" 2>&1; then
    return 0
  fi
  local deb_dir=""
  [[ -n "${OFFLINE_SOURCE}" && -d "${OFFLINE_SOURCE}/debs" ]] && deb_dir="${OFFLINE_SOURCE}/debs"
  if [[ -n "${deb_dir}" ]]; then
    info "The configured repos failed; using packages from ${deb_dir}"
    apt-get -y --no-install-recommends install "${deb_dir}"/*.deb >> "${LOG_FILE}" 2>&1 \
      || die "Could not install prerequisites from ${deb_dir}. See ${LOG_FILE}."
  else
    die "Could not install ${missing[*]}. Check the apt sources (a local mirror, say), or build a bundle with --with-debs. See ${LOG_FILE}."
  fi
}

install_prereqs() {
  say "Checking prerequisites"
  if [[ "${OS_FAMILY}" == debian ]]; then
    install_prereqs_debian
  else
    install_prereqs_rhel
  fi

  local have
  have="$(ansible --version 2>/dev/null | sed -n -E 's/.*core ([0-9]+\.[0-9]+(\.[0-9]+)?).*/\1/p' | head -n1)"
  [[ -n "${have}" ]] || die "ansible-core installed but 'ansible --version' does not work."
  if [[ "$(printf '%s\n%s\n' "${MIN_ANSIBLE_CORE}" "${have}" | sort -V | head -n1)" != "${MIN_ANSIBLE_CORE}" ]]; then
    die "ansible-core ${have} is too old. We need ${MIN_ANSIBLE_CORE} or later."
  fi
  info "ansible-core ${have}"
}

install_collections() {
  local dest="${REPO_DIR}/collections"
  if [[ -d "${dest}/ansible_collections/ansible/posix" ]]; then
    info "ansible.posix collection already present"
    return
  fi
  say "Installing the ansible.posix collection"
  mkdir -p "${dest}"
  if [[ -n "${OFFLINE_SOURCE}" && -d "${OFFLINE_SOURCE}/collections" ]] && compgen -G "${OFFLINE_SOURCE}/collections/ansible-posix-*.tar.gz" >/dev/null; then
    ansible-galaxy collection install -p "${dest}" "${OFFLINE_SOURCE}"/collections/ansible-posix-*.tar.gz >> "${LOG_FILE}" 2>&1 \
      || die "Could not install ansible.posix from the bundle. See ${LOG_FILE}."
  elif [[ "${OFFLINE_SOURCE}" =~ ^https?:// ]]; then
    local tmp name
    tmp="$(mktemp -d)"
    curl -fsSL "${OFFLINE_SOURCE%/}/collections/manifest.txt" -o "${tmp}/manifest.txt" \
      || die "No collections/manifest.txt at ${OFFLINE_SOURCE}. Was the mirror built with tools/build-offline-bundle.sh?"
    while read -r name; do
      [[ -n "${name}" ]] && curl -fsSL "${OFFLINE_SOURCE%/}/collections/${name}" -o "${tmp}/${name}"
    done < "${tmp}/manifest.txt"
    ansible-galaxy collection install -p "${dest}" "${tmp}"/*.tar.gz >> "${LOG_FILE}" 2>&1 \
      || die "Could not install ansible.posix from ${OFFLINE_SOURCE}. See ${LOG_FILE}."
    rm -rf "${tmp}"
  elif [[ "${ONLINE}" == true || "${GALAXY_OK}" == true ]]; then
    ansible-galaxy collection install -r "${REPO_DIR}/requirements.yml" -p "${dest}" >> "${LOG_FILE}" 2>&1 \
      || die "Could not download ansible.posix from Galaxy. See ${LOG_FILE}."
  else
    die "ansible.posix is missing and this server is offline. Use --offline-dir with a bundle from tools/build-offline-bundle.sh."
  fi
}

# ------------------------------------------------------------------ interview

yaml_quote() { local v="${1//\'/\'\'}"; printf "'%s'" "${v}"; }

# Turn "a, b,c" into ['a', 'b', 'c'].
yaml_list() {
  local IFS=',' item out=() trimmed
  for item in $1; do
    trimmed="$(printf '%s' "${item}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -n "${trimmed}" ]] && out+=("$(yaml_quote "${trimmed}")")
  done
  local joined
  joined="$(IFS=','; printf '%s' "${out[*]-}")"
  printf '[%s]' "${joined//,/, }"
}

ask() {
  # ask VAR "Question" "default"
  local __var="$1" prompt="$2" default="${3-}" reply
  if [[ -n "${default}" ]]; then
    read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} ${prompt} ${C_DIM}[${C_OFF}${C_YEL}${default}${C_OFF}${C_DIM}]${C_OFF}: " reply || no_input
  else
    read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} ${prompt}: " reply || no_input
  fi
  reply="${reply:-${default}}"
  # Going through earlier answers, Enter keeps the old one, so '-' is how to clear it.
  [[ "${EDITING}" == true && "${reply}" == - ]] && reply=""
  printf -v "${__var}" '%s' "${reply}"
}

ask_addresses() {
  # ask_addresses VAR "Who connects" "What breaks when blank" [default]
  local __var="$1" who="$2" why="$3" default="${4-}" __addrs
  while true; do
    ask __addrs "${who} (comma-separated IPs or CIDRs, 0.0.0.0/0 = any)" "${default}"
    [[ "$(yaml_list "${__addrs}")" != "[]" ]] && break
    echo "    ${why}"
  done
  printf -v "${__var}" '%s' "${__addrs}"
}

ask_path() {
  # ask_path VAR "Question" "default" file|dir [optional]
  # Re-asks until the path exists here, so a typo shows up now rather than at the plan.
  local __var="$1" prompt="$2" default="$3" kind="$4" optional="${5:-}" __path what=file
  [[ "${kind}" == dir ]] && what=directory
  while true; do
    ask __path "${prompt}" "${default}"
    [[ -z "${__path}" && -n "${optional}" ]] && break
    if [[ -n "${__path}" ]]; then
      [[ "${kind}" == dir && -d "${__path}" ]] && break
      [[ "${kind}" == file && -f "${__path}" ]] && break
      if [[ -n "${optional}" ]]; then
        echo "    There's no ${what} at ${__path} on this server. Copy it there and give the path again, or $( [[ "${EDITING}" == true ]] && echo "type - to leave it out" || echo "leave it blank")."
      else
        echo "    There's no ${what} at ${__path} on this server. Copy it there and press Enter, or give another path."
        default="${__path}"
      fi
    else
      echo "    A path is needed."
    fi
  done
  printf -v "${__var}" '%s' "${__path}"
}

ask_url() {
  # ask_url VAR "Question" "default" [optional]
  # Re-asks until the answer starts with http:// or https://. Optional allows blank.
  local __var="$1" prompt="$2" default="$3" optional="${4:-}" __url
  while true; do
    ask __url "${prompt}" "${default}"
    [[ -z "${__url}" && -n "${optional}" ]] && break
    [[ "${__url}" == http://* || "${__url}" == https://* ]] && break
    echo "    Start it with https:// (or http://), e.g. https://${__url:-chat.example.com}"
  done
  printf -v "${__var}" '%s' "${__url}"
}

# This server's main IPv4 address, or empty when it can't be worked out.
primary_ip() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}' || true)"
  [[ -z "${ip}" ]] && ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  # No default route and no usable hostname -I: read the kernel's local addresses directly.
  [[ -z "${ip}" ]] && ip="$(awk '/\/32 host LOCAL/ && prev !~ /^127\./ {print prev; exit} {prev = $2}' /proc/net/fib_trie 2>/dev/null || true)"
  printf '%s' "${ip}"
}

# Every address this server could be reached on, so an estate plan can match any
# of them, not just the one primary_ip() would pick.
local_addresses() {
  { ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
    ip -o -6 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
    primary_ip
  } | sort -u
}

ask_secret() {
  # ask_secret VAR "Question" [KEY]
  # Blank generates one, or keeps KEY's earlier answer when going through them again.
  local __var="$1" prompt="$2" key="${3-}" reply old="" hint="blank = generate"
  [[ -n "${key}" ]] && old="$(prev "${key}")"
  [[ -n "${old}" ]] && hint="blank = keep the current one"
  read -r -s -p "  ${C_YEL}${C_BOLD}?${C_OFF} ${prompt} ${C_DIM}(${hint})${C_OFF}: " reply || no_input
  echo
  printf -v "${__var}" '%s' "${reply:-${old}}"
}

ask_password() {
  # ask_password VAR "Question" [KEY]
  # A password typed blind. Blank keeps KEY's earlier answer when going through them again.
  local __var="$1" prompt="$2" key="${3-}" reply old=""
  [[ -n "${key}" ]] && old="$(prev "${key}")"
  read -r -s -p "    ${prompt}${old:+ ${C_DIM}(blank = keep the current one)${C_OFF}}: " reply || no_input
  echo
  printf -v "${__var}" '%s' "${reply:-${old}}"
}

ask_yn() {
  local prompt="$1" default="${2:-y}" reply
  while true; do
    read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} ${prompt} ${C_DIM}[${C_OFF}${C_YEL}$( [[ ${default} == y ]] && echo Y/n || echo y/N )${C_OFF}${C_DIM}]${C_OFF}: " reply || no_input
    reply="${reply:-${default}}"
    case "${reply,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
    esac
    echo "    Answer y or n."
  done
}

ask_choice() {
  # ask_choice VAR "Question" default option1 option2 ...
  local __var="$1" prompt="$2" default="$3"; shift 3
  local options=("$@") i reply
  # An earlier answer that isn't one of the options (a hand-edited file) can't be the default.
  printf '%s\n' "${options[@]}" | grep -Fxq -- "${default}" || default="${options[0]}"
  printf '  %s %s\n' "${C_YEL}${C_BOLD}?${C_OFF}" "${prompt}"
  for i in "${!options[@]}"; do
    printf '      %s %s%s\n' "${C_YEL}$((i + 1)))${C_OFF}" "${options[$i]}" \
      "$( [[ ${options[$i]} == "${default}" ]] && printf ' %s' "${C_DIM}(default)${C_OFF}")"
  done
  while true; do
    read -r -p "    ${C_DIM}Choice${C_OFF} ${C_YEL}${G_ARROW}${C_OFF} " reply || no_input
    [[ -z "${reply}" ]] && { printf -v "${__var}" '%s' "${default}"; return; }
    if [[ "${reply}" =~ ^[0-9]+$ ]] && (( reply >= 1 && reply <= ${#options[@]} )); then
      printf -v "${__var}" '%s' "${options[$((reply - 1))]}"; return
    fi
    echo "    Pick a number from the list."
  done
}

# ------------------------------------------------------------------ going through earlier answers

# A Python that can read YAML. ansible-core brings PyYAML, though sometimes for the
# Python it runs on rather than the system python3.
yaml_python() {
  local bin line cand
  bin="$(command -v ansible-playbook 2>/dev/null || true)"
  [[ -n "${bin}" ]] && IFS= read -r line < "${bin}" || line=""
  line="${line#\#!}"; line="${line# }"
  [[ "${line}" == */env\ * ]] && line="${line#*/env }"
  for cand in python3 "${line%% *}"; do
    [[ -n "${cand}" ]] && "${cand}" -I -c 'import yaml' 2>/dev/null && { printf '%s' "${cand}"; return 0; }
  done
  return 1
}

# Read an earlier answer file so every question can offer its old answer as the default.
load_previous() {
  local file="$1" py out key value
  py="$(yaml_python)" || die "Can't read ${file}: there's no Python with PyYAML here. Edit it by hand and use --answers."
  out="$("${py}" -I - "${file}" <<'PY'
import sys, yaml

def flat(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ", ".join(flat(x) for x in v if not isinstance(x, (dict, list)))
    return str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")

data = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
if not isinstance(data, dict):
    sys.exit("not a set of settings")
for key, value in data.items():
    if key == "monitoring_estate":
        for s in value or []:
            if isinstance(s, dict):
                comps = ",".join(str(c) for c in s.get("components") or [])
                print("@estate\t%s|%s|%s" % (flat(s.get("name")), flat(s.get("address")), comps))
    elif not isinstance(value, dict):
        print("%s\t%s" % (key, flat(value)))
PY
)" || die "Can't read ${file}. Fix the YAML in it, or start again with blank answers."
  PREV=(); PREV_ESTATE=()
  while IFS=$'\t' read -r key value; do
    [[ -n "${key}" ]] || continue
    if [[ "${key}" == @estate ]]; then PREV_ESTATE+=("${value}"); else PREV[${key}]="${value}"; fi
  done <<< "${out}"
  PREV_FILE="${file}"
  EDITING=true
  info "Each question shows your last answer in brackets. Press Enter to keep it, type a new"
  info "one to change it, or type - to clear it. Passwords left blank stay as they were."
}

# prev KEY [FALLBACK]: going through earlier answers, what KEY was; otherwise FALLBACK.
prev() {
  if [[ "${EDITING}" == true && -n "${PREV[$1]+set}" ]]; then
    printf '%s' "${PREV[$1]}"
  else
    printf '%s' "${2-}"
  fi
}

# prev_yn KEY y|n: an earlier true or false as the default for ask_yn.
prev_yn() {
  case "$(prev "$1")" in
    true|yes) echo y ;;
    false|no) echo n ;;
    *) echo "$2" ;;
  esac
}

# How to leave an answer out, for prompts: Enter keeps an earlier answer, so it's '-'.
blank() { if [[ "${EDITING}" == true ]]; then printf '%s' -; else printf '%s' blank; fi; }

# " [default]" for a prompt, or nothing when there's no default.
default_hint() {
  [[ -n "$1" ]] && printf ' %s' "${C_DIM}[${C_OFF}${C_YEL}$1${C_OFF}${C_DIM}]${C_OFF}"
  return 0
}

# numbers_of "a, c" a b c: where the names sit in the list, as "1,3".
numbers_of() {
  local want=",${1// /}," out="" i=0 o
  shift
  for o in "$@"; do
    i=$((i + 1))
    [[ "${want}" == *",${o},"* ]] && out+="${out:+,}${i}"
  done
  printf '%s' "${out}"
}

# Settings in the earlier file that no question covers (thresholds, tuning, anything
# added by hand) go on the end of the new file, so editing doesn't lose them.
carry_over_answers() {
  local from="$1" to="$2" py kept
  py="$(yaml_python)" || return 0
  kept="$("${py}" -I - "${from}" "${to}" "${BASH_SOURCE[0]}" <<'PY'
import re, sys, yaml
src, dest, script = sys.argv[1:4]
old = yaml.safe_load(open(src, encoding="utf-8")) or {}
new = yaml.safe_load(open(dest, encoding="utf-8")) or {}
asked = set(re.findall(r"\bemit ([a-z_]+) ", open(script, encoding="utf-8").read())) | {"monitoring_estate"}
keep = {k: v for k, v in old.items() if k not in new and k not in asked}
if keep:
    with open(dest, "a", encoding="utf-8") as out:
        out.write("# Kept from the earlier answers; the questions don't cover these.\n")
        yaml.safe_dump(keep, out, default_flow_style=False, sort_keys=False, allow_unicode=True)
print(", ".join(keep))
PY
)" || { warn "Couldn't copy the other settings across from ${from}. Check the new answers before deploying."; return 0; }
  [[ -n "${kept}" ]] && info "Kept the settings the questions don't cover: ${kept}"
  return 0
}

# The components going on this server, set by choose_components. Empty until then,
# so has_component answers "no" for anything asked before the questions are done.
SELECTED=""

has_component() { [[ " ${SELECTED} " == *" $1 "* ]]; }

choose_components() {
  local i reply name chosen_profiles=() extra=() pdefault="" cdefault=""
  if [[ "${EDITING}" == true ]]; then
    pdefault="$(numbers_of "$(prev deploy_profiles)" "${PROFILE_NAMES[@]}")"
    cdefault="$(numbers_of "$(prev deploy_components)" "${COMPONENT_NAMES[@]}")"
    [[ -z "${pdefault}" && -n "${cdefault}" ]] && pdefault=c
  fi
  section "What should this server run?"
  for i in "${!PROFILE_NAMES[@]}"; do
    name="${PROFILE_NAMES[$i]}"
    printf '      %s %s %s\n' "${C_YEL}$((i + 1)))${C_OFF}" "${C_BOLD}$(printf '%-11s' "${name}")${C_OFF}" "${PROFILE_DESCRIPTIONS[${name}]}"
  done
  printf '      %s %s %s\n' "${C_YEL}c)${C_OFF}" "${C_BOLD}$(printf '%-11s' custom)${C_OFF}" "pick individual components"
  echo
  info "${C_DIM}Several profiles can share a server, e.g. 1,3. We recommend one role per server for production.${C_OFF}"
  while true; do
    read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} Profiles$(default_hint "${pdefault}") ${C_YEL}${G_ARROW}${C_OFF} " reply || no_input
    reply="${reply:-${pdefault}}"
    [[ -n "${reply}" ]] || continue
    chosen_profiles=(); extra=()
    if [[ "${reply}" == c ]]; then
      for i in "${!COMPONENT_NAMES[@]}"; do
        printf '      %s %s\n' "${C_YEL}$((i + 1)))${C_OFF}" "${COMPONENT_NAMES[$i]}"
      done
      read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} Components ${C_DIM}(comma-separated numbers)${C_OFF}$(default_hint "${cdefault}") ${C_YEL}${G_ARROW}${C_OFF} " reply || no_input
      reply="${reply:-${cdefault}}"
      local n
      for n in ${reply//,/ }; do
        [[ "${n}" =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#COMPONENT_NAMES[@]} )) && extra+=("${COMPONENT_NAMES[$((n - 1))]}")
      done
      [[ ${#extra[@]} -gt 0 ]] && break
    else
      local ok=true n
      for n in ${reply//,/ }; do
        if [[ "${n}" =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#PROFILE_NAMES[@]} )); then
          chosen_profiles+=("${PROFILE_NAMES[$((n - 1))]}")
        else
          ok=false
        fi
      done
      [[ "${ok}" == true && ${#chosen_profiles[@]} -gt 0 ]] && break
    fi
    echo "    No match. Try again."
  done
  ANS_PROFILES="$(IFS=,; echo "${chosen_profiles[*]-}")"
  ANS_COMPONENTS="$(IFS=,; echo "${extra[*]-}")"
  SELECTED=""
  local p
  for p in "${chosen_profiles[@]+"${chosen_profiles[@]}"}"; do SELECTED+=" ${PROFILE_COMPONENTS[${p}]}"; done
  SELECTED+=" ${extra[*]-} "
}

# What Prometheus scrapes on this server, as "what (port)", one per line. Keep in step
# with roles/common/tasks/selection.yml and the firewall tasks of each role.
scrape_ports() {
  echo "host metrics, node_exporter (9100)"
  has_component postgresql && echo "PostgreSQL metrics, postgres_exporter (9187)"
  has_component nginx && echo "Nginx metrics, nginx_exporter (9113)"
  has_component opensearch && echo "OpenSearch metrics, opensearch_exporter (9114)"
  has_component mattermost && [[ "${MM_EDITION:-enterprise}" == enterprise ]] && echo "Mattermost metrics (8067)"
  has_component rtcd && echo "rtcd metrics (8045)"
  has_component calls_offloader && echo "the Calls job service API (4545, probed rather than scraped)"
  has_component loki && echo "Loki metrics (3100)"
  has_component grafana && echo "Grafana metrics (3000)"
  [[ "$1" == true ]] && echo "the log collector's metrics (8888)"
  return 0
}

# Turn what someone typed for Loki into a URL: a URL as given, host:port, or a bare
# address or name that gets the default port.
loki_url_from() {
  local v="$1"
  if [[ "${v}" == http://* || "${v}" == https://* ]]; then printf '%s' "${v%/}"
  elif [[ "${v}" =~ ^[^:]+:[0-9]+$ ]]; then printf 'http://%s' "${v}"
  elif [[ "${v}" == *:* ]]; then printf 'http://[%s]:3100' "${v}"
  else printf 'http://%s:3100' "${v}"
  fi
}

# Monitoring and logs on a server that doesn't run Prometheus: should the monitoring
# server watch it, and should its logs go to Loki? Each yes installs what's needed
# here, opens only the ports that needs, and points it at the right server. The
# monitoring server then has to list this one, which the end of this says how to do.
monitoring_and_logs() {
  local watched=false shipped=false scr="" prom_addr="" lsrc lurl line
  if ! has_component prometheus; then
    section "Monitoring and logs"
    info "A monitoring server (Prometheus, Loki and Grafana) can watch this server and keep its logs."
    info "${C_DIM}For each yes we install what's needed here and open only the ports it needs.${C_OFF}"
    echo
    if ask_yn "Should Prometheus on your monitoring server watch this server?" "$(prev_yn deploy_monitoring_exporters y)"; then
      watched=true
      ask_addresses scr "Address of the Prometheus server (several: comma-separated)" \
        "Prometheus can't reach this server's metrics without at least one address." "$(prev monitoring_scrape_sources)"
      emit deploy_monitoring_exporters true
      emit monitoring_scrape_sources "$(yaml_list "${scr}")"
      prom_addr="${scr%%,*}"
      prom_addr="${prom_addr// /}"
      [[ "${prom_addr}" == */* ]] && prom_addr=""   # a CIDR isn't a server
    else
      emit deploy_monitoring_exporters false
    fi
  fi

  if has_component loki; then
    if ! has_component prometheus; then
      # Nobody else tells this Loki who sends to it, so ask here.
      echo
      info "Loki runs here. Its port (3100) opens only to the servers you list."
      ask lsrc "Servers that send logs here, and the Grafana server (comma-separated IPs; $(blank) = this server only)" \
        "$(prev loki_allowed_sources)"
      emit loki_allowed_sources "$(yaml_list "${lsrc}")"
    fi
  else
    echo
    if ask_yn "Send this server's logs to Loki on your monitoring server?" "$(prev_yn deploy_log_shipping y)"; then
      shipped=true
      local ldefault
      ldefault="$(prev logging_loki_url)"
      while true; do
        ask lurl "Address of the Loki server (or its URL)" "${ldefault:-${prom_addr}}"
        [[ -n "${lurl}" ]] && break
        echo "    Give the Loki server's address, e.g. 10.10.1.15, or http://10.10.1.15:3100."
      done
      lurl="$(loki_url_from "${lurl}")"
      emit logging_loki_url "$(yaml_quote "${lurl}")"
      emit deploy_log_shipping true
    else
      emit deploy_log_shipping false
      if has_component grafana; then
        ask lurl "Loki server for Grafana to search (address or URL; $(blank) = none)" "$(prev logging_loki_url "${prom_addr}")"
        [[ -n "${lurl}" ]] && lurl="$(loki_url_from "${lurl}")"
      else
        lurl=""
      fi
      emit logging_loki_url "$(yaml_quote "${lurl}")"
    fi
  fi

  [[ "${watched}" == true || "${shipped}" == true ]] || return 0
  echo
  say "What this sets up here"
  if [[ "${watched}" == true ]]; then
    info "Prometheus at ${scr} gets in to:"
    while IFS= read -r line; do info "  ${line}"; done < <(scrape_ports "${shipped}")
  fi
  if [[ "${shipped}" == true ]]; then
    info "The OpenTelemetry Collector sends this server's logs to ${lurl}."
    info "${C_DIM}It connects out, so no port opens here for that.${C_OFF}"
  fi

  # The monitoring server has to know about this one: its firewall lets it in to
  # Loki, and Prometheus only scrapes the servers it's told about.
  local self_ip comps=() c short
  self_ip="$(primary_ip)"
  while [[ -z "${self_ip}" ]]; do
    ask self_ip "This server's address, as the monitoring server sees it" ""
  done
  short="$(hostname -s 2>/dev/null || uname -n)"
  for c in mattermost nginx postgresql rtcd calls_offloader opensearch prometheus loki grafana; do
    has_component "${c}" && comps+=("${c}")
  done
  local joined
  joined="$(IFS=,; echo "${comps[*]-}")"
  echo
  say "Then, on the monitoring server"
  info "Add this server when the installer asks for the other servers, or add this line"
  info "to monitoring_estate in its answer file, and run it again:"
  info "${C_YEL}  - {name: ${short}, address: ${self_ip}, components: [${joined//,/, }]}${C_OFF}"
  info "${C_DIM}That adds this server's checks, alerts and dashboards there, and opens its firewall to this server.${C_OFF}"
  [[ "${shipped}" == true ]] && info "${C_DIM}  Loki (3100), so the logs get in${C_OFF}"
  [[ "${shipped}" != true && -n "${lurl:-}" ]] && has_component grafana \
    && info "${C_DIM}  Loki (3100), so Grafana here can search the logs${C_OFF}"
  has_component grafana && info "${C_DIM}  Prometheus (9090), so Grafana here can query it${C_OFF}"
  has_component prometheus && info "${C_DIM}  Prometheus and the exporters, so this Prometheus can scrape them${C_OFF}"
  return 0
}

interview() {
  local answers_out="$1"
  local fqdn
  fqdn="$(host_name)"
  choose_components

  {
    echo "# Mattermost Parthenon answer file, written by install.sh on $(date -u '+%Y-%m-%d %H:%M UTC') for ${fqdn}."
    echo "# Re-run with: sudo ./install.sh --answers ${SAVED_ANSWERS}"
    echo "# Change some answers with: sudo ./install.sh --edit"
    echo "# answers.example.yml in the repo explains every setting."
  } > "${answers_out}"
  emit() { printf '%s: %s\n' "$1" "$2" >> "${answers_out}"; }

  local name
  ask name "Deployment name (used in the report and labels)" "$(prev deploy_name mattermost)"
  emit deploy_name "$(yaml_quote "${name}")"
  emit deploy_profiles "$(yaml_list "${ANS_PROFILES}")"
  emit deploy_components "$(yaml_list "${ANS_COMPONENTS}")"

  if has_component mattermost; then
    section "Mattermost"
    local site edition channel version lic
    ask_url site "Site URL users will use" "$(prev mattermost_site_url "https://${fqdn}")"
    emit mattermost_site_url "$(yaml_quote "${site}")"
    MM_SITE_URL="${site}"
    ask_choice edition "Edition" "$(prev mattermost_edition enterprise)" enterprise team
    emit mattermost_edition "${edition}"
    MM_EDITION="${edition}"
    ask_choice channel "Which build" "$(prev mattermost_version_channel latest-esr)" latest-esr latest-stable pinned
    emit mattermost_version_channel "${channel}"
    if [[ "${channel}" == pinned ]]; then
      ask version "Version to install (e.g. 11.7.14)" "$(prev mattermost_version)"
      emit mattermost_version "$(yaml_quote "${version}")"
    fi
    if [[ "${edition}" == enterprise ]]; then
      ask_path lic "Path to the licence file on this server ($(blank) to add later)" "$(prev mattermost_license_file)" file optional
      emit mattermost_license_file "$(yaml_quote "${lic}")"
    fi

    local store
    ask_choice store "Where should uploaded files go" "$(prev mattermost_file_driver local)" local amazons3
    emit mattermost_file_driver "${store}"
    if [[ "${store}" == local ]]; then
      local data
      ask data "Data directory (local disk or an NFS mount)" "$(prev mattermost_data_dir /var/opt/mattermost/data)"
      emit mattermost_data_dir "$(yaml_quote "${data}")"
    else
      local ep bucket region ak sk
      ask ep "S3 endpoint (host[:port], no https://)" "$(prev mattermost_s3_endpoint)"
      ask bucket "Bucket" "$(prev mattermost_s3_bucket)"
      ask region "Region ($(blank) if not needed)" "$(prev mattermost_s3_region)"
      ask ak "Access key ID" "$(prev mattermost_s3_access_key)"
      ask_password sk "Secret access key" mattermost_s3_secret_key
      emit mattermost_s3_endpoint "$(yaml_quote "${ep}")"
      emit mattermost_s3_bucket "$(yaml_quote "${bucket}")"
      emit mattermost_s3_region "$(yaml_quote "${region}")"
      emit mattermost_s3_access_key "$(yaml_quote "${ak}")"
      emit mattermost_s3_secret_key "$(yaml_quote "${sk}")"
      if ask_yn "Use HTTPS to reach S3?" "$(prev_yn mattermost_s3_ssl y)"; then
        emit mattermost_s3_ssl true
      else
        emit mattermost_s3_ssl false
      fi
    fi

    if ! has_component postgresql; then
      local dbh dbp dbpass
      section "Database (on another server)"
      ask dbh "PostgreSQL host" "$(prev mattermost_db_host)"
      ask dbp "Port" "$(prev mattermost_db_port 5432)"
      ask_password dbpass "Password for mmuser (from secrets.yml on the database server)" mattermost_db_password
      emit mattermost_db_host "$(yaml_quote "${dbh}")"
      emit mattermost_db_port "${dbp}"
      emit mattermost_db_password "$(yaml_quote "${dbpass}")"
      local dbreplicas
      ask dbreplicas "PostgreSQL read replica addresses for reads (comma-separated, $(blank) = none)" \
        "$(prev mattermost_db_replica_hosts)"
      emit mattermost_db_replica_hosts "$(yaml_list "${dbreplicas}")"
    fi
    if ! has_component nginx; then
      local proxies
      ask proxies "Load balancer or reverse proxy addresses allowed to reach port 8065 (comma-separated, $(blank) = any)" \
        "$(prev mattermost_proxy_sources)"
      emit mattermost_proxy_sources "$(yaml_list "${proxies}")"
    fi
    local peers
    ask peers "Other Mattermost app servers to cluster with, needs a licence (comma-separated, $(blank) = single app server)" \
      "$(prev mattermost_cluster_peers)"
    emit mattermost_cluster_peers "$(yaml_list "${peers}")"
    if ! has_component rtcd; then
      local rurl
      ask rurl "rtcd URL if Calls runs on another server, e.g. http://rtcd01:8045 ($(blank) = built in)" \
        "$(prev mattermost_rtcd_url)"
      emit mattermost_rtcd_url "$(yaml_quote "${rurl}")"
    fi
    if ! has_component calls_offloader; then
      local jurl
      ask jurl "Calls job service URL for recording, e.g. http://jobs01:4545 ($(blank) = no recording)" \
        "$(prev mattermost_job_service_url)"
      emit mattermost_job_service_url "$(yaml_quote "${jurl}")"
    fi
    if ! has_component opensearch; then
      local osurl
      ask osurl "OpenSearch URL if search runs on another server ($(blank) = none)" "$(prev mattermost_opensearch_url)"
      emit mattermost_opensearch_url "$(yaml_quote "${osurl}")"
      if [[ "${osurl}" == https://* ]]; then
        local ospass osca
        ask_password ospass "Password for the OpenSearch 'mattermost' user" mattermost_opensearch_password
        ask_path osca "Path to the search CA on this server (copy /etc/opensearch/certs/root-ca.pem from the search server)" \
          "$(prev mattermost_opensearch_ca_file /etc/pki/tls/certs/opensearch-ca.pem)" file
        emit mattermost_opensearch_password "$(yaml_quote "${ospass}")"
        emit mattermost_opensearch_ca_file "$(yaml_quote "${osca}")"
      fi
    fi
  fi

  if has_component postgresql; then
    section "PostgreSQL"
    local dbpass clients repl="none"
    if ! has_component mattermost; then
      ask_choice repl "Replication (a replica is a read-only copy of a primary; install the primary first)" \
        "$(prev postgresql_replication none)" none primary replica
      emit postgresql_replication "${repl}"
    fi
    if [[ "${repl}" == replica ]]; then
      local primary rpass
      ask primary "Primary server address" "$(prev postgresql_primary_host)"
      ask_password dbpass "Password for mmuser (db_password in the primary's secrets.yml)" mattermost_db_password
      ask_password rpass "Replication password (replication_password in the primary's secrets.yml)" \
        postgresql_replication_password
      emit postgresql_primary_host "$(yaml_quote "${primary}")"
      emit postgresql_replication_password "$(yaml_quote "${rpass}")"
    else
      ask_secret dbpass "Password for the mmuser database role" mattermost_db_password
    fi
    emit mattermost_db_password "$(yaml_quote "${dbpass}")"
    if [[ "${repl}" == primary ]]; then
      local replicas
      ask replicas "Replica server addresses (comma-separated IPs)" "$(prev postgresql_replica_hosts)"
      emit postgresql_replica_hosts "$(yaml_list "${replicas}")"
    fi
    if ! has_component mattermost; then
      ask_addresses clients "Mattermost app server addresses allowed to connect" \
        "The app servers can't reach the database without at least one address." "$(prev postgresql_allowed_clients)"
      emit postgresql_allowed_clients "$(yaml_list "${clients}")"
    fi
  fi

  if has_component nginx; then
    section "Nginx and TLS"
    local tls
    ask_choice tls "TLS certificate" "$(prev tls_mode later)" later provided
    emit tls_mode "${tls}"
    if [[ "${tls}" == provided ]]; then
      local how
      how="two files"
      [[ -n "$(prev tls_source_dir)" ]] && how="a directory"
      ask_choice how "Where is it" "${how}" "two files" "a directory"
      if [[ "${how}" == "two files" ]]; then
        local cert key chain
        ask_path cert "Certificate file (PEM, full chain preferred)" "$(prev tls_cert_file)" file
        ask_path key "Private key file" "$(prev tls_key_file)" file
        ask_path chain "Intermediate chain file ($(blank) if already in the certificate)" "$(prev tls_chain_file)" file optional
        emit tls_cert_file "$(yaml_quote "${cert}")"
        emit tls_key_file "$(yaml_quote "${key}")"
        emit tls_chain_file "$(yaml_quote "${chain}")"
      else
        local dir sname="${MM_SITE_URL#*://}"
        sname="${sname%%[/:]*}"
        ask_path dir "Directory holding ${sname:-<server name>}.crt/.key or tls.crt/tls.key" \
          "$(prev tls_source_dir /etc/pki/tls/mattermost-src)" dir
        emit tls_source_dir "$(yaml_quote "${dir}")"
      fi
    fi
    if ! has_component mattermost; then
      local ups
      ask ups "Mattermost app servers to proxy to (host:8065, comma-separated)" "$(prev nginx_upstreams)"
      emit nginx_upstreams "$(yaml_list "${ups}")"
    fi
  fi

  if has_component rtcd; then
    section "Calls (rtcd)"
    local ice srcs
    ask ice "Public IP or hostname clients use to reach this server ($(blank) if not behind NAT)" \
      "$(prev rtcd_ice_host_override)"
    emit rtcd_ice_host_override "$(yaml_quote "${ice}")"
    if ! has_component mattermost; then
      ask_addresses srcs "Mattermost app server addresses allowed to use the rtcd API" \
        "The app servers can't reach rtcd without at least one address, so calls would fail." "$(prev rtcd_api_sources)"
      emit rtcd_api_sources "$(yaml_list "${srcs}")"
    fi
  fi

  if has_component calls_offloader; then
    section "Call recording (calls-offloader)"
    info "Recording, transcription and live captions all run here, one container per job."
    info "This server needs Docker: the toolkit installs it, from the distribution's own"
    info "archive on Debian and Ubuntu, and from Docker's repository on RHEL."
    local jsrcs jquality jminutes jsite
    if ! has_component mattermost; then
      ask_addresses jsrcs "Mattermost app server addresses allowed to use the job service API" \
        "The app servers can't start a recording without at least one address." "$(prev calls_offloader_api_sources)"
      emit calls_offloader_api_sources "$(yaml_list "${jsrcs}")"
    fi
    ask_choice jquality "Recording quality" "$(prev mattermost_calls_recording_quality medium)" low medium high
    emit mattermost_calls_recording_quality "$(yaml_quote "${jquality}")"
    ask jminutes "Longest recording in minutes (15-180)" "$(prev mattermost_calls_max_recording_minutes 60)"
    emit mattermost_calls_max_recording_minutes "${jminutes}"
    info "A ${jminutes}-minute ${jquality}-quality recording uploads to Mattermost as a file"
    info "when the call ends, so the upload limit is raised to match on the app servers."
    if ask_yn "Transcribe recordings once the call ends?" "$(prev_yn mattermost_calls_transcriptions y)"; then
      emit mattermost_calls_transcriptions true
      if ask_yn "Also show live captions during calls? Costs a transcriber per call" \
        "$(prev_yn mattermost_calls_live_captions n)"; then
        emit mattermost_calls_live_captions true
      else
        emit mattermost_calls_live_captions false
      fi
    else
      emit mattermost_calls_transcriptions false
      emit mattermost_calls_live_captions false
    fi
    ask jsite "Address job containers use to reach Mattermost ($(blank) = the site URL)" \
      "$(prev mattermost_calls_internal_site_url)"
    emit mattermost_calls_internal_site_url "$(yaml_quote "${jsite}")"
  fi

  if has_component opensearch && ! has_component mattermost; then
    section "OpenSearch"
    local clients ospass
    ask_addresses clients "Mattermost app server addresses allowed to connect" \
      "The app servers can't reach OpenSearch without at least one address." "$(prev opensearch_allowed_clients)"
    emit opensearch_allowed_clients "$(yaml_list "${clients}")"
    ask_secret ospass "Password for the OpenSearch 'mattermost' user" mattermost_opensearch_password
    emit mattermost_opensearch_password "$(yaml_quote "${ospass}")"
  fi

  if has_component nfs; then
    section "NFS file storage"
    info "This server exports a directory over NFSv4 for Mattermost's data directory, so every"
    info "app server sees the same uploaded files. Mount it on the app servers afterwards: the"
    info "report this run writes gives the mount command and the fstab line."
    local nfsdir nfsclients squash
    ask nfsdir "Directory to export" "$(prev nfs_export_dir /srv/mattermost/data)"
    emit nfs_export_dir "$(yaml_quote "${nfsdir}")"
    ask_addresses nfsclients "Mattermost app server addresses allowed to mount it" \
      "Nothing can mount the share without at least one address." "$(prev nfs_allowed_clients)"
    emit nfs_allowed_clients "$(yaml_list "${nfsclients}")"
    info "${C_DIM}all:  every client write lands as the share's owner, so uids needn't match.${C_OFF}"
    info "${C_DIM}none: the mattermost user on each app server must have this server's uid.${C_OFF}"
    ask_choice squash "How should client users be mapped" "$(prev nfs_squash all)" all none
    emit nfs_squash "$(yaml_quote "${squash}")"
  fi

  # Grafana builds its alerts and dashboards from the same server list Prometheus
  # scrapes, so a Grafana server without Prometheus needs the list too.
  if has_component prometheus || has_component grafana; then
    section "Monitoring the estate"
    local build_q="Build monitors, alerts and dashboards for the estate?" site_q
    site_q="Site URL users reach (checked from here; $(blank) = skip)"
    if has_component prometheus; then
      info "This builds Prometheus jobs, tiered alerts (info, warning, critical) and dashboards"
      info "in Grafana for every server in this Mattermost estate. This server is included already."
    else
      info "This builds tiered alerts (info, warning, critical) and dashboards in Grafana for every"
      info "server in this Mattermost estate. This server is included already."
      info "Give the same site URL and server names as on the Prometheus server."
      build_q="Build alerts and dashboards for the estate?"
      site_q="Site URL the Prometheus server checks ($(blank) = none)"
    fi
    if ask_yn "${build_q}" "$(prev_yn monitoring_alerts_enabled y)"; then
      emit monitoring_alerts_enabled true
      emit monitoring_dashboards_enabled true
      local site_default="" site ca joined trusted=y
      has_component mattermost && site_default="${MM_SITE_URL:-}"
      site_default="$(prev monitoring_site_url "${site_default}")"
      [[ -n "$(prev monitoring_site_ca_file)" || "$(prev monitoring_site_tls_verify)" == false ]] && trusted=n
      info "${C_DIM}This is Mattermost's Site URL, including https://, e.g. https://chat.example.com${C_OFF}"
      ask_url site "${site_q}" "${site_default}" optional
      emit monitoring_site_url "$(yaml_quote "${site}")"
      if has_component prometheus && [[ "${site}" == https://* ]] && ! ask_yn "Does the site's certificate chain to a CA this server already trusts?" "${trusted}"; then
        ask_path ca "Path to that CA certificate on this server ($(blank) = don't verify)" "$(prev monitoring_site_ca_file)" file optional
        if [[ -n "${ca}" ]]; then
          emit monitoring_site_ca_file "$(yaml_quote "${ca}")"
        else
          emit monitoring_site_tls_verify false
        fi
      fi

      echo
      info "${C_DIM}Now the other servers. For each one, give its name, address and what it runs.${C_OFF}"
      local estate_lines=() snames=() saddrs=() scomps_list=() sname saddr scomps n bad picked names
      # Going through earlier answers, each server already listed comes up first with
      # its old answers as the defaults; then new ones can be added.
      local defaults=() entered=() d dname daddr dcomps cdefault chint
      [[ "${EDITING}" == true ]] && defaults=("${PREV_ESTATE[@]+"${PREV_ESTATE[@]}"}")
      [[ ${#defaults[@]} -gt 0 ]] && info "${C_DIM}Press Enter to keep each server as it was, or type - as its name to drop it.${C_OFF}"
      # Prometheus, Loki and Grafana elsewhere get scraped too, and this server's
      # firewall opens Prometheus to the ones that query it (see wiring.yml).
      local i short mods=(mattermost nginx postgresql rtcd calls_offloader opensearch nfs prometheus loki grafana)
      short="$(hostname -s 2>/dev/null || uname -n)"
      while true; do
        estate_lines=() snames=() saddrs=() scomps_list=() entered=()
        d=0
        while true; do
          dname="" daddr="" dcomps=""
          if (( d < ${#defaults[@]} )); then
            IFS='|' read -r dname daddr dcomps <<< "${defaults[$d]}"
            d=$((d + 1))
            ask sname "Server name (- to drop it)" "${dname}"
            [[ -z "${sname}" || "${sname}" == - ]] && continue
          else
            ask sname "Server name (blank when done)" ""
            [[ -z "${sname}" ]] && break
          fi
          if [[ "${sname}" == "${short}" || "${sname}" == "${fqdn}" ]]; then
            echo "    ${sname} is this server, which is included already."
            continue
          fi
          if printf '%s\n' "${snames[@]}" | grep -Fxq -- "${sname}"; then
            echo "    ${sname} is already in the list."
            continue
          fi
          while true; do
            ask saddr "Address of ${sname} (IP or DNS name)" "${daddr}"
            [[ -n "${saddr}" ]] && break
            echo "    Prometheus needs an address to reach ${sname}."
          done
          for i in "${!mods[@]}"; do
            printf '      %s %s\n' "${C_YEL}$((i + 1)))${C_OFF}" "${mods[$i]}"
          done
          # With an earlier answer as the default, Enter keeps it, so '-' means host metrics only.
          cdefault="$(numbers_of "${dcomps}" "${mods[@]}")"
          chint="blank"
          [[ -n "${cdefault}" ]] && chint="-"
          while true; do
            read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} What runs on ${sname} ${C_DIM}(comma-separated numbers, ${chint} = host metrics only)${C_OFF}$(default_hint "${cdefault}") ${C_YEL}${G_ARROW}${C_OFF} " scomps || no_input
            scomps="${scomps:-${cdefault}}"
            [[ "${scomps}" == - ]] && scomps=""
            picked=() names=() bad=""
            for n in ${scomps//,/ }; do
              if [[ "${n}" =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#mods[@]} )); then
                picked+=("$(yaml_quote "${mods[$((n - 1))]}")")
                names+=("${mods[$((n - 1))]}")
              else
                bad+=" ${n}"
              fi
            done
            [[ -z "${bad}" ]] && break
            echo "    Not on the list:${bad}. Use numbers from 1 to ${#mods[@]}."
          done
          joined="$(IFS=,; echo "${picked[*]-}")"
          estate_lines+=("  - {name: $(yaml_quote "${sname}"), address: $(yaml_quote "${saddr}"), components: [${joined//,/, }]}")
          snames+=("${sname}")
          saddrs+=("${saddr}")
          joined="$(IFS=,; echo "${names[*]-}")"
          scomps_list+=("${joined//,/, }")
          entered+=("${sname}|${saddr}|${joined}")
        done
        [[ ${#estate_lines[@]} -eq 0 ]] && break
        echo
        for i in "${!snames[@]}"; do
          printf '    %-20s %-24s %s\n' "${snames[$i]}" "${saddrs[$i]}" "${scomps_list[$i]:-host metrics only}"
        done
        ask_yn "Is the list correct?" y && break
        echo "    Going through the servers again. Enter keeps each as entered."
        defaults=("${entered[@]}")
      done
      if [[ ${#estate_lines[@]} -gt 0 ]]; then
        echo "monitoring_estate:" >> "${answers_out}"
        printf '%s\n' "${estate_lines[@]}" >> "${answers_out}"
      fi
      if [[ ${#estate_lines[@]} -gt 0 ]] && has_component prometheus; then
        local self_ip scrapers grafanas="" queriers=""
        self_ip="$(primary_ip)"
        while [[ -z "${self_ip}" ]]; do
          ask self_ip "This server's address, as the other servers see it" ""
        done
        # Every Prometheus in the estate scrapes every server, so they all go in
        # monitoring_scrape_sources. Grafana and other Prometheus servers query this one.
        scrapers="${self_ip}"
        for i in "${!snames[@]}"; do
          [[ ", ${scomps_list[$i]}, " == *", prometheus, "* ]] && scrapers+=", ${saddrs[$i]}"
          [[ ", ${scomps_list[$i]}, " == *", grafana, "* ]] && grafanas+="${grafanas:+, }${snames[$i]}"
          [[ ", ${scomps_list[$i]}, " == *", grafana, "* || ", ${scomps_list[$i]}, " == *", prometheus, "* ]] \
            && queriers+="${queriers:+, }${snames[$i]}"
        done
        echo
        if has_component loki; then
          info "When you install each of those servers, answer yes to being watched by Prometheus"
          info "and to sending its logs to Loki, and give ${self_ip}. In an answer file that's:"
        else
          info "When you install each of those servers, answer yes to being watched by Prometheus"
          info "and give ${self_ip}. In an answer file that's:"
        fi
        info "${C_YEL}deploy_monitoring_exporters: true${C_OFF}"
        info "${C_YEL}monitoring_scrape_sources: [${scrapers}]${C_OFF}"
        has_component loki && info "${C_YEL}logging_loki_url: http://${self_ip}:3100${C_OFF}"
        if [[ -n "${grafanas}" ]]; then
          info "On ${grafanas}, also:"
          info "${C_YEL}grafana_prometheus_url: http://${self_ip}:9090${C_OFF}"
        fi
        echo
        info "${C_DIM}This server's firewall will be opened for them:${C_OFF}"
        [[ -n "${queriers}" ]] && info "${C_DIM}  Prometheus (9090) to ${queriers}${C_OFF}"
        has_component loki && info "${C_DIM}  Loki (3100) to every server listed, for log shipping and queries${C_OFF}"
        [[ "${scrapers}" != "${self_ip}" ]] && info "${C_DIM}  exporters and metrics ports to the other Prometheus servers${C_OFF}"
        [[ -z "${queriers}" ]] && ! has_component loki && info "${C_DIM}  nothing extra: this server only reaches out to them${C_OFF}"
      fi
    else
      emit monitoring_alerts_enabled false
      emit monitoring_dashboards_enabled false
    fi
  fi
  if has_component grafana; then
    local gpass
    ask_secret gpass "Grafana admin password" grafana_admin_password
    emit grafana_admin_password "$(yaml_quote "${gpass}")"
    if ! has_component prometheus; then
      local purl
      ask purl "Prometheus URL Grafana should use" "$(prev grafana_prometheus_url http://prometheus01:9090)"
      emit grafana_prometheus_url "$(yaml_quote "${purl}")"
    fi
  fi

  monitoring_and_logs

  if [[ "${ONLINE}" != true && -z "${OFFLINE_SOURCE}" ]]; then
    section "Offline artifacts"
    local src
    src="$(prev artifact_source)"
    ask src "Bundle directory or mirror URL (from tools/build-offline-bundle.sh)" "${src:-/srv/mm-bundle}"
    OFFLINE_SOURCE="${src}"
  fi
  [[ -n "${OFFLINE_SOURCE}" ]] && emit artifact_source "$(yaml_quote "${OFFLINE_SOURCE}")"
  if [[ "${ONLINE}" == true ]]; then emit deploy_internet auto; else emit deploy_internet "'no'"; fi
  chmod 0600 "${answers_out}"
}

# Read the plan, check it parses, and work out this server's addresses. Both the
# estate install and the preflight start here.
estate_prepare() {
  [[ -z "${ESTATE_FILE}" ]] || return 0
  local err line a
  ESTATE_FILE="${ESTATE}"
  if [[ "${ESTATE}" == - ]]; then
    info "Paste the estate plan the architecture wizard gave you, then press Enter:"
    # While a terminal edits a line it holds no more than 4095 characters and drops
    # the rest without a word. A plan for a dozen servers is longer than that, so
    # take the line as it arrives instead. EXIT puts the terminal back, even on Ctrl-C.
    if [[ -t 0 ]]; then
      TTY_STATE="$(stty -g 2>/dev/null || true)"
      if [[ -n "${TTY_STATE}" ]]; then stty -icanon 2>/dev/null || TTY_STATE=""; fi
    fi
    read -r line || no_input
    tty_restore
    ESTATE_FILE="$(mktemp "${CONFIG_DIR}/estate.XXXXXX")"
    printf '%s\n' "${line}" > "${ESTATE_FILE}"
  fi
  err="$(mktemp "${CONFIG_DIR}/estate-err.XXXXXX")"
  if ! python3 -I "${REPO_DIR}/tools/estate_plan.py" decode --plan-file "${ESTATE_FILE}" > /dev/null 2> "${err}"; then
    line="$(cat "${err}")"; rm -f "${err}"; die "${line#ERROR: }"
  fi
  rm -f "${err}"
  ESTATE_ADDR_ARGS=()
  while IFS= read -r a; do [[ -n "${a}" ]] && ESTATE_ADDR_ARGS+=(--local-address "${a}"); done < <(local_addresses)
}

# Run an estate_plan.py subcommand that has to know which node this server is,
# asking when the plan can't tell on its own. Leaves stdout in ESTATE_OUT and
# stderr in ESTATE_ERRFILE for the caller to read.
estate_run() {
  local sub="$1"; shift
  local rc reason names=() node_args=()
  ESTATE_ERRFILE="$(mktemp "${CONFIG_DIR}/estate-err.XXXXXX")"
  while true; do
    node_args=()
    [[ -n "${ESTATE_NODE}" ]] && node_args=(--node "${ESTATE_NODE}")
    # The status has to be caught on the same line. A failing command substitution
    # ends the script under errexit before a following rc=$? could run, and exit
    # codes 3 and 4 are how the helper asks which node this server is.
    rc=0
    ESTATE_OUT="$(python3 -I "${REPO_DIR}/tools/estate_plan.py" "${sub}" --plan-file "${ESTATE_FILE}" \
      "${node_args[@]}" --local-name "$(hostname -s 2>/dev/null || true)" --local-name "$(host_name)" \
      "${ESTATE_ADDR_ARGS[@]}" "$@" 2> "${ESTATE_ERRFILE}")" || rc=$?
    [[ ${rc} -eq 0 ]] && return 0
    # The helper's messages start with "ERROR: ", which die and warn add themselves.
    reason="$(sed 's/^ERROR: //' "${ESTATE_ERRFILE}")"
    [[ ${rc} -eq 3 || ${rc} -eq 4 ]] || die "${reason}"
    mapfile -t names < <(tail -n +2 "${ESTATE_ERRFILE}")
    reason="$(head -n1 "${ESTATE_ERRFILE}")"; reason="${reason#ERROR: }"
    if [[ "${NON_INTERACTIVE}" == true ]]; then
      die "${reason} Pass --estate-node NAME, one of: ${names[*]}."
    fi
    warn "${reason}"
    ask_choice ESTATE_NODE "Which node in the plan is this server?" "${names[0]}" "${names[@]}"
  done
}

# Decode an estate plan, work out which node in it is this server, and derive
# this server's answers from it. The result is a normal answer file, so
# everything after this behaves as it does for --answers.
resolve_estate() {
  local secret_args=() node tmp backup="" followups notes line
  estate_prepare
  if [[ -n "${ESTATE_SECRETS}" ]]; then
    secret_args=(--secrets "${ESTATE_SECRETS}")
    [[ -n "${VAULT_PASSWORD_FILE}" ]] && secret_args+=(--vault-password-file "${VAULT_PASSWORD_FILE}")
    info "Taking this server's passwords from ${ESTATE_SECRETS}."
  fi
  estate_run apply "${secret_args[@]}"

  # Written to SAVED_ANSWERS rather than a run-specific path, so a later --edit
  # finds it on its own, the way a normal interactive run's answers do.
  tmp="$(mktemp "${CONFIG_DIR}/answers.XXXXXX")"
  printf '%s\n' "${ESTATE_OUT}" > "${tmp}"
  chmod 0600 "${tmp}"
  if [[ -f "${SAVED_ANSWERS}" ]]; then
    backup="${SAVED_ANSWERS}.${RUN_ID}.bak"
    cp -p "${SAVED_ANSWERS}" "${backup}"
  fi
  mv "${tmp}" "${SAVED_ANSWERS}"
  chmod 0600 "${SAVED_ANSWERS}"
  ANSWERS="${SAVED_ANSWERS}"
  # The script's first line of output says which node it matched.
  node="$(head -n1 "${ANSWERS}" | sed -E 's/^# Derived from .* for node //')"
  info "Wrote this server's answers to ${ANSWERS} from the estate plan (node ${node})${backup:+, and kept the earlier ones in ${backup}}."

  # grep exits 1 when nothing matches, which pipefail would turn into a crash.
  # Either kind of line can be absent: a lab has notes and no follow-ups.
  followups="$({ grep '^# FOLLOWUP: ' "${ESTATE_ERRFILE}" || true; } | sed 's/^# FOLLOWUP: //')"
  notes="$({ grep '^# NOTE: ' "${ESTATE_ERRFILE}" || true; } | sed 's/^# NOTE: //')"
  rm -f "${ESTATE_ERRFILE}"
  if [[ -n "${followups}" ]]; then
    section "Still needed on this server"
    while IFS= read -r line; do info "${line}"; done <<< "${followups}"
    info "The plan still runs with these blank. An install stops if it needs one, such as a TLS"
    info "certificate path. Run ./install.sh --edit here: it keeps what the plan settled and asks the rest."
  fi
  # Things worth knowing that need no answer. A server whose components all sit
  # on it - a lab, or a core profile - settles its own passwords and reaches
  # everything on localhost, so it reaches this point with nothing to fill in,
  # and saying so beats an empty "still needed" heading.
  if [[ -n "${notes}" ]]; then
    section "Worth knowing"
    while IFS= read -r line; do info "${line}"; done <<< "${notes}"
  fi
}

# ------------------------------------------------------------------ preflight

preflight_line() {
  # preflight_line ok|note|fail SUBJECT DETAIL
  local status="$1" subject="$2" detail="$3" mark
  case "${status}" in
    ok) mark="${C_GRN}${C_BOLD}ok  ${C_OFF}" ;;
    note) mark="${C_YEL}${C_BOLD}note${C_OFF}" ;;
    *) mark="${C_RED}${C_BOLD}FAIL${C_OFF}" ;;
  esac
  printf '      %s %s %s\n' "${mark}" "$(printf '%-26s' "${subject}")" "${C_DIM}${detail}${C_OFF}"
}

preflight_resolve() {
  local target="$1" label="$2"
  if [[ "${target}" =~ ^[0-9.]+$ || "${target}" == *:* ]]; then
    preflight_line ok "${target}" "${label} (an address, nothing to resolve)"
    return 0
  fi
  if getent ahosts "${target}" > /dev/null 2>&1; then
    preflight_line ok "${target}" "${label}"
    return 0
  fi
  preflight_line fail "${target}" "${label}, but this name doesn't resolve here"
  return 1
}

preflight_tcp() {
  # The plan is pasted text, so the address goes in as an argument, never into
  # the script bash runs.
  local target="$1" port="$2" label="$3" rc=0
  # shellcheck disable=SC2016  # single quotes on purpose: the address reaches
  # bash as an argument, never as part of the script it runs.
  timeout 3 bash -c 'exec 3<>/dev/tcp/"$1"/"$2"' _ "${target}" "${port}" 2>/dev/null || rc=$?
  case "${rc}" in
    0) preflight_line ok "${target}:${port}" "${label}" ;;
    124) preflight_line fail "${target}:${port}" "${label}: timed out, something is dropping it"; return 1 ;;
    *) preflight_line note "${target}:${port}" "${label}: refused, route open and nothing listening yet" ;;
  esac
  return 0
}

preflight_ram() {
  local need="$1" label="$2" have
  have="$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null || echo 0)"
  if [[ "${have:-0}" -ge "${need}" ]]; then
    preflight_line ok "RAM ${have} MB" "${need} MB is enough for ${label}"
    return 0
  fi
  preflight_line fail "RAM ${have} MB" "${label} wants ${need} MB"
  return 1
}

preflight_cpus() {
  local need="$1" label="$2" have
  have="$(nproc 2>/dev/null || echo 0)"
  if [[ "${have:-0}" -ge "${need}" ]]; then
    preflight_line ok "${have} vCPU" "${need} is enough for ${label}"
    return 0
  fi
  preflight_line fail "${have} vCPU" "${label} wants ${need}"
  return 1
}

# Everything this server needs before an install can work: can it resolve and
# reach the other servers, and has it the memory and CPU for its own role.
# Changes nothing.
run_preflight() {
  [[ -n "${ESTATE}" ]] || die "--preflight reads an estate plan, so it needs --estate FILE as well."
  estate_prepare
  estate_run checks
  rm -f "${ESTATE_ERRFILE}"

  local kind target port label failed=0 checked=0 node
  node="${ESTATE_NODE:-this server}"
  section "Preflight for ${node}"
  info "Nothing is installed yet, so a refused connection is the expected result: the route is open and"
  info "nothing is listening. A timeout means something is dropping the traffic."
  echo
  while IFS=$'\t' read -r kind target port label; do
    [[ -n "${kind}" ]] || continue
    checked=$((checked + 1))
    case "${kind}" in
      resolve) preflight_resolve "${target}" "${label}" || failed=$((failed + 1)) ;;
      tcp) preflight_tcp "${target}" "${port}" "${label}" || failed=$((failed + 1)) ;;
      ram) preflight_ram "${target}" "${label}" || failed=$((failed + 1)) ;;
      cpus) preflight_cpus "${target}" "${label}" || failed=$((failed + 1)) ;;
      *) checked=$((checked - 1)) ;;
    esac
  done <<< "${ESTATE_OUT}"

  echo
  if [[ ${failed} -eq 0 ]]; then
    say "All ${checked} checks passed. Install this server with ./install.sh --estate."
    return 0
  fi
  warn "${failed} of ${checked} checks failed. Fix them before you install."
  return 1
}

resolve_answers() {
  if [[ "${MODE}" == uninstall ]]; then
    # Removal needs no questions. The saved answers still matter, since they hold any
    # port you changed, and the firewall rules to close follow those ports.
    [[ -n "${ANSWERS}" ]] && return 0
    if [[ -r "${SAVED_ANSWERS}" ]]; then
      ANSWERS="${SAVED_ANSWERS}"
    else
      ANSWERS="$(mktemp)"; UNINSTALL_STUB="${ANSWERS}"
      printf -- '---\ndeploy_name: mattermost\n' > "${ANSWERS}"
    fi
    return 0
  fi
  if [[ -n "${ESTATE}" ]]; then
    resolve_estate
    return
  fi
  if [[ "${EDIT_ANSWERS}" == true ]]; then
    # --edit: the questions again, with the saved answers (or --answers FILE) as defaults.
    local from="${ANSWERS:-${SAVED_ANSWERS}}"
    [[ -r "${from}" ]] || die "There are no earlier answers to change (${from}). Run ./install.sh without --edit first."
    section "Answers"
    info "Going through the answers in ${from} again."
    load_previous "${from}"
    ANSWERS=""
    ask_and_save
    return 0
  fi
  if [[ -n "${ANSWERS}" ]]; then
    return
  fi
  if [[ ( "${MODE}" == install || "${MODE}" == plan ) && "${NON_INTERACTIVE}" != true && ! -r "${SAVED_ANSWERS}" ]]; then
    section "Answers"
    if ask_yn "Paste an estate plan from the architecture wizard, instead of answering these one at a time?" n; then
      ESTATE=-
      resolve_estate
      return
    fi
  fi
  if [[ "${MODE}" != install && "${MODE}" != plan ]] || [[ "${NON_INTERACTIVE}" == true && -z "${PROFILES}${COMPONENTS}" ]]; then
    [[ -r "${SAVED_ANSWERS}" ]] || die "No answer file. Pass --answers, or run interactively first."
    ANSWERS="${SAVED_ANSWERS}"
    return
  fi
  if [[ "${NON_INTERACTIVE}" == true ]]; then
    # Profiles or components on the command line, defaults for everything else.
    ANSWERS="${CONFIG_DIR}/answers-cli-${RUN_ID}.yml"
    # Ansible rejects an empty extra-vars file, so always write at least one key.
    printf -- '---\ndeploy_name: mattermost\n' > "${ANSWERS}"; chmod 0600 "${ANSWERS}"
    if [[ -n "${OFFLINE_SOURCE}" ]]; then
      printf 'artifact_source: %s\n' "$(yaml_quote "${OFFLINE_SOURCE}")" >> "${ANSWERS}"
    fi
    return 0
  fi
  if [[ -r "${SAVED_ANSWERS}" ]]; then
    local how
    section "Answers"
    info "This server has answers from an earlier run in ${SAVED_ANSWERS}."
    ask_choice how "What would you like to do with them?" "Use them as they are" \
      "Use them as they are" "Go through them and change what you need" "Start again with blank answers"
    case "${how}" in
      "Use them as they are")
        ANSWERS="${SAVED_ANSWERS}"
        [[ -n "${OFFLINE_SOURCE}" ]] || OFFLINE_SOURCE="$(yaml_get artifact_source "${ANSWERS}")"
        return 0 ;;
      "Go through them"*)
        load_previous "${SAVED_ANSWERS}" ;;
    esac
  fi
  ask_and_save
}

# Run the interview and save what it gives, keeping the old answers as a .bak.
ask_and_save() {
  local tmp
  tmp="$(mktemp "${CONFIG_DIR}/answers.XXXXXX")"
  interview "${tmp}"
  [[ "${EDITING}" == true ]] && carry_over_answers "${PREV_FILE}" "${tmp}"
  chmod 0600 "${tmp}"
  local backup=""
  if [[ -f "${SAVED_ANSWERS}" ]]; then
    backup="${SAVED_ANSWERS}.${RUN_ID}.bak"
    cp -p "${SAVED_ANSWERS}" "${backup}"
  fi
  mv "${tmp}" "${SAVED_ANSWERS}"
  chmod 0600 "${SAVED_ANSWERS}"
  ANSWERS="${SAVED_ANSWERS}"
  info "Saved your answers to ${SAVED_ANSWERS}${backup:+ (the earlier ones are in ${backup})}"
}

save_answers_copy() {
  # Keep a copy of whatever answer file drove this run, for the record.
  if [[ "${ANSWERS}" != "${SAVED_ANSWERS}" && "${MODE}" == install ]]; then
    [[ -f "${SAVED_ANSWERS}" ]] && cp -p "${SAVED_ANSWERS}" "${SAVED_ANSWERS}.${RUN_ID}.bak"
    cp "${ANSWERS}" "${SAVED_ANSWERS}"
    chmod 0600 "${SAVED_ANSWERS}"
  fi
}

# ------------------------------------------------------------------ ansible

extra_vars_json() {
  python3 -I - "$@" <<'PY'
import json, sys
args = dict(a.split("=", 1) for a in sys.argv[1:])
out = {
    "deploy_online": args["online"] == "true",
    "deploy_tool_version": args["version"],
    "deploy_run_id": args["run_id"],
    "mmd_answers_file": args["answers"],
}
if args.get("source"):
    out["artifact_source"] = args["source"]
# Removal takes its own names, so an answer file's deploy_* lists never widen it.
prefix = "uninstall" if args.get("uninstall") == "true" else "deploy"
if args.get("profiles"):
    out[prefix + "_profiles"] = [p.strip() for p in args["profiles"].split(",") if p.strip()]
if args.get("components"):
    out[prefix + "_components"] = [c.strip() for c in args["components"].split(",") if c.strip()]
if args.get("purge") == "true":
    out["uninstall_purge"] = True
if args.get("plan_file"):
    out["plan_output_file"] = args["plan_file"]
if args.get("upgrade") == "true":
    out["mattermost_allow_upgrade"] = True
    out["rtcd_allow_upgrade"] = True
    out["calls_offloader_allow_upgrade"] = True
print(json.dumps(out))
PY
}

run_playbook() {
  local playbook="$1"; shift
  local extra
  extra="$(extra_vars_json "online=${ONLINE}" "version=${TOOL_VERSION}" "run_id=${RUN_ID}" \
    "answers=${ANSWERS}" "source=${OFFLINE_SOURCE}" "profiles=${PROFILES}" "components=${COMPONENTS}" \
    "upgrade=${ALLOW_UPGRADE}" "uninstall=$([[ "${MODE}" == uninstall ]] && echo true || echo false)" \
    "purge=${PURGE}" "$@")"
  local args=(-e "@${ANSWERS}" -e "${extra}")
  [[ "${CHECK_MODE}" == true && "${playbook}" == site.yml ]] && args+=(--check --diff)
  (
    cd "${REPO_DIR}"
    ANSIBLE_CONFIG="${REPO_DIR}/ansible.cfg" ANSIBLE_LOG_PATH="${LOG_FILE}" \
      ansible-playbook "playbooks/${playbook}" "${args[@]}"
  )
}

# Why an Ansible run failed, from its output: the message of the last failed task.
# Ansible prints it as YAML, wrapped over several lines, under a "failed:" or
# "fatal:" line.
failure_reason() {
  awk '
    /^(failed|fatal):/ { failing = 1; next }
    failing && /^[[:space:]]+msg:/ {
      match($0, /^[[:space:]]*/); ind = RLENGTH
      sub(/^[[:space:]]*msg:[[:space:]]*/, ""); cur = $0; inmsg = 1; failing = 0; next
    }
    inmsg {
      match($0, /^[[:space:]]*/)
      if (RLENGTH > ind && $0 !~ /^[[:space:]]*$/) { line = $0; sub(/^[[:space:]]+/, "", line); cur = cur " " line; next }
      last = cur; inmsg = 0
    }
    END { if (inmsg) last = cur; gsub(/^\047|\047$/, "", last); if (last != "") print last }
  ' "$1" 2>/dev/null || true
}

show_plan() {
  local plan_file reason
  plan_file="$(mktemp)"
  say "Working out the plan"
  if ! run_playbook plan.yml "plan_file=${plan_file}" > "${LOG_DIR}/plan-${RUN_ID}.out" 2>&1; then
    rm -f "${plan_file}"
    reason="$(failure_reason "${LOG_DIR}/plan-${RUN_ID}.out")"
    [[ -z "${reason}" ]] || warn "${reason}"
    die "The plan failed. Fix the answer file and try again. Full output: ${LOG_DIR}/plan-${RUN_ID}.out"
  fi
  section "The plan"
  python3 -I - "${plan_file}" "${OS_PRETTY}" "${C_BOLD}" "${C_DIM}" "${C_YEL}" "${C_RED}" "${C_OFF}" <<'PY'
import json, sys, textwrap
plan = json.load(open(sys.argv[1]))
os_name, bold, dim, yel, red, off = sys.argv[2:8]

def row(label, value):
    print(f"    {bold}{label:<12}{off}{value}")

row("Server", os_name)
row("Components", ", ".join(plan["components"]))
if plan["profiles"]:
    row("Profiles", ", ".join(plan["profiles"]))
s = plan["sizing"]
row("Sizing", f"{s['ram_have_mb']} MB RAM, {s['cpus_have']} vCPUs here; "
              f"components want {s['ram_needed_mb']} MB, {s['cpus_needed']} vCPUs")
if "nginx" in plan["components"]:
    row("TLS", "certificate from the answer file" if plan["tls_mode"] == "provided"
        else f"{yel}not yet{off} (HTTP only until you add a certificate)")
row("Artifacts", plan["artifact_source"] or ("vendor downloads" if plan["online"] else f"{red}none configured{off}"))
if plan.get("mattermost_installed"):
    if plan["mattermost_hold"]:
        row("Mattermost", f"{plan['mattermost_installed']} installed; staying on it "
                          f"{dim}(--upgrade moves to the newest {plan['mattermost_channel']}){off}")
    else:
        row("Mattermost", f"{yel}{plan['mattermost_installed']} installed; may be upgraded{off} "
                          f"{dim}(stopped and database dumped first){off}")
if plan["findings"]:
    print()
    for f in plan["findings"]:
        colour = red if f["severity"] == "high" else yel
        print(f"    {colour}{bold}{f['severity'].upper():<7}{off}{bold}{f['message']}{off}")
        for line in textwrap.wrap(f["advice"], 70):
            print(f"           {dim}{line}{off}")
print()
PY
  rm -f "${plan_file}"
}

confirm() {
  [[ "${ASSUME_YES}" == true || "${NON_INTERACTIVE}" == true ]] && return 0
  ask_yn "Deploy this plan?" n || { say "Stopped. Nothing was changed."; exit 0; }
}

# What an uninstall would do, worked out by the playbook without changing anything.
show_uninstall_plan() {
  UNINSTALL_PLAN="$(mktemp)"
  say "Working out what to remove"
  if ! run_playbook uninstall.yml "plan_file=${UNINSTALL_PLAN}" > "${LOG_DIR}/uninstall-plan-${RUN_ID}.out" 2>&1; then
    rm -f "${UNINSTALL_PLAN}"
    local reason
    reason="$(failure_reason "${LOG_DIR}/uninstall-plan-${RUN_ID}.out")"
    [[ -z "${reason}" ]] || warn "${reason}"
    die "Could not work out what to remove. Full output: ${LOG_DIR}/uninstall-plan-${RUN_ID}.out"
  fi
  section "To be removed"
  python3 -I - "${UNINSTALL_PLAN}" "${C_BOLD}" "${C_DIM}" "${C_YEL}" "${C_RED}" "${C_OFF}" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
bold, dim, yel, red, off = sys.argv[2:7]

def row(label, value):
    print(f"    {bold}{label:<12}{off}{value}")

row("Remove", ", ".join(plan["remove"]))
row("Keep", ", ".join(plan["remaining"]) or "nothing, this server will be empty")
if plan["purge"]:
    row("Data", f"{red}{bold}deleted too (--purge){off}")
else:
    row("Data", f"kept {dim}(the database, uploaded files, search indices, metrics and logs){off}")
for n in plan["notes"]:
    print(f"    {yel}{bold}NOTE   {off}{bold}{n}{off}")
print()
PY
}

# What is left on the server after the removal, as a comma-separated list.
uninstall_remaining() {
  python3 -I - "${UNINSTALL_PLAN}" <<'PY'
import json, sys
print(",".join(json.load(open(sys.argv[1]))["remaining"]))
PY
}

# Taking things off a server is the one step here that cannot be walked back, so it
# never goes ahead unattended without --yes.
confirm_uninstall() {
  [[ "${ASSUME_YES}" == true ]] && return 0
  [[ "${NON_INTERACTIVE}" != true ]] || die "Removing components needs --yes when there is nobody to ask."
  if [[ "${PURGE}" == true ]]; then
    local typed host
    host="$(host_name)"
    warn "--purge deletes the data listed above, and it cannot be recovered."
    ask typed "Type this server's name (${host}) to go ahead"
    [[ "${typed}" == "${host}" ]] || { say "Stopped. Nothing was changed."; exit 0; }
    return 0
  fi
  ask_yn "Remove these components?" n || { say "Stopped. Nothing was changed."; exit 0; }
}

# After some components go and others stay, point the answer file at what is left, so
# the next run does not put them back.
update_answers_after_uninstall() {
  local remaining
  remaining="$(uninstall_remaining)"
  [[ -n "${remaining}" ]] || return 0
  if [[ -z "${UNINSTALL_STUB}" ]] && grep -qE '^deploy_(profiles|components):' "${ANSWERS}"; then
    cp -p "${ANSWERS}" "${ANSWERS}.${RUN_ID}.bak"
    python3 -I - "${ANSWERS}" "${remaining}" <<'PY'
import re, sys
path, remaining = sys.argv[1], sys.argv[2].split(",")
text = open(path).read()
text = re.sub(r"(?m)^deploy_profiles:.*$", "deploy_profiles: []", text)
line = "deploy_components: [" + ", ".join(remaining) + "]"
if re.search(r"(?m)^deploy_components:", text):
    text = re.sub(r"(?m)^deploy_components:.*$", line, text)
else:
    # Answer files may leave the key out. Without it the profiles line would be all that selected anything.
    text = text.rstrip("\n") + "\n" + line + "\n"
open(path, "w").write(text)
PY
    info "Updated deploy_profiles and deploy_components in ${ANSWERS} (the earlier file is ${ANSWERS}.${RUN_ID}.bak)"
  else
    warn "Could not update the answers. Make sure they select only: ${remaining//,/, }"
  fi
}

uninstall_message() {
  local rc="$1" remaining
  echo
  if [[ "${rc}" -ne 0 ]]; then
    say "${C_RED}Removal stopped with problems.${C_OFF} Ansible log: ${LOG_FILE}"
    return
  fi
  say "${C_GRN}Removed.${C_OFF}"
  remaining="$(uninstall_remaining)"
  if [[ "${PURGE}" != true ]]; then
    info "Kept: databases, uploaded files, search indices, metrics, logs, secrets and saved answers."
    info "Run again with --purge to delete them."
  fi
  if [[ "${OS_FAMILY}" == debian ]]; then
    info "Not undone: the baseline packages (firewalld, chrony and the rest), since other things may"
    info "depend on them."
  else
    info "Not undone: the SELinux booleans httpd_can_network_connect and nfs_export_all_rw, and the"
    info "baseline packages (firewalld, chrony and the rest), since other things may depend on them."
  fi
  if [[ -n "${remaining}" ]]; then
    info "Still here: ${remaining//,/, }. Run ./install.sh to refresh their configuration."
  fi
  info "Other servers in the estate still point at what was removed: change the Prometheus"
  info "scrape targets, Loki address, database host or search address in their answers."
  info "Ansible log: ${LOG_FILE}"
  rm -f "${UNINSTALL_PLAN}"
  [[ -z "${UNINSTALL_STUB}" ]] || rm -f "${UNINSTALL_STUB}"
}

finish_message() {
  local rc="$1" report="${STATE_DIR}/reports/latest.md"
  echo
  if [[ "${rc}" -eq 0 ]]; then
    say "${C_GRN}Done.${C_OFF}"
  else
    say "${C_RED}Finished with problems.${C_OFF} The report says what failed."
  fi
  [[ -e "${report}" ]] && info "Architecture document: $(readlink -f "${report}")"
  info "Ansible log: ${LOG_FILE}"
  info "Answers: ${ANSWERS}"
  info "Secrets: ${CONFIG_DIR}/secrets.yml (root only)"
}

# ------------------------------------------------------------------ main

main() {
  parse_args "$@"
  require_root
  check_os
  setup_logging
  TOOL_VERSION="$(tool_version)"
  log_line "Started: install.sh $* (${TOOL_VERSION}, $(host_name), ${OS_PRETTY})"
  banner "${TOOL_VERSION} ${G_DOT} $(host_name) ${G_DOT} ${OS_PRETTY}"
  [[ -z "${LOCAL_REPO}" ]] || setup_local_repo
  # Only installs need installers; re-testing and reporting work from what is on disk.
  if [[ "${MODE}" == install || "${MODE}" == plan ]]; then
    ensure_curl
    choose_installer_source
  fi
  install_prereqs
  if [[ "${MODE}" == preflight ]]; then
    local pf=0
    trap - ERR
    run_preflight || pf=$?
    exit "${pf}"
  fi
  resolve_answers
  [[ -n "${OFFLINE_SOURCE}" ]] || OFFLINE_SOURCE="$(yaml_get artifact_source "${ANSWERS}")"
  install_collections

  local rc=0
  trap - ERR
  case "${MODE}" in
    plan)
      show_plan
      ;;
    verify)
      say "Re-running the post-deploy tests"
      run_playbook verify.yml || rc=$?
      finish_message "${rc}"
      ;;
    report)
      say "Rebuilding the architecture document"
      run_playbook report.yml || rc=$?
      finish_message "${rc}"
      ;;
    uninstall)
      show_uninstall_plan
      confirm_uninstall
      say "Removing. The full log is in ${LOG_FILE}."
      run_playbook uninstall.yml || rc=$?
      [[ "${rc}" -ne 0 ]] || update_answers_after_uninstall
      uninstall_message "${rc}"
      ;;
    install)
      show_plan
      confirm
      save_answers_copy
      say "Deploying. This takes a few minutes; the full log is in ${LOG_FILE}."
      run_playbook site.yml || rc=$?
      finish_message "${rc}"
      ;;
  esac
  exit "${rc}"
}

# Run only when executed, so tests can source this file and call its functions.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
