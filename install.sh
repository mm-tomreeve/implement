#!/usr/bin/env bash
# Mattermost component installer for RHEL 9 and 10.
#
# Asks which components go on THIS server (or reads an answer file), installs
# Ansible and what it needs, deploys from verified tarballs, runs the post-deploy
# tests and writes an architecture document for the server.
#
#   sudo ./install.sh                         interactive
#   sudo ./install.sh --answers answers.yml   from an answer file
#   sudo ./install.sh --offline-dir /srv/mm-bundle-20261028
#   sudo ./install.sh --verify-only           re-run the tests and refresh the report
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR=/etc/mattermost-implement
STATE_DIR=/var/lib/mattermost-implement
LOG_DIR=/var/log/mattermost-implement
SAVED_ANSWERS="${CONFIG_DIR}/answers.yml"
MIN_ANSIBLE_CORE=2.14
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"

# Keep these in step with inventory/group_vars/all/profiles.yml and catalogue.yml.
# tests/test_repo_consistency.py fails if they drift.
PROFILE_NAMES=(core app database calls search monitoring all-in-one)
declare -A PROFILE_COMPONENTS=(
  [core]="postgresql mattermost nginx"
  [app]="mattermost nginx"
  [database]="postgresql"
  [calls]="rtcd"
  [search]="opensearch"
  [monitoring]="prometheus grafana"
  [all-in-one]="postgresql mattermost nginx rtcd opensearch prometheus grafana"
)
declare -A PROFILE_DESCRIPTIONS=(
  [core]="PostgreSQL, Mattermost and Nginx on one server. Fine for small teams."
  [app]="Mattermost application server with its Nginx proxy."
  [database]="Dedicated PostgreSQL server for Mattermost."
  [calls]="Dedicated Calls media server (rtcd)."
  [search]="Dedicated OpenSearch node for Enterprise search."
  [monitoring]="Prometheus and Grafana with the Mattermost dashboards."
  [all-in-one]="Everything on one box. Lab and proof-of-concept use only."
)
COMPONENT_NAMES=(postgresql mattermost nginx rtcd opensearch prometheus grafana node_exporter postgres_exporter nginx_exporter opensearch_exporter blackbox_exporter)

ANSWERS=""
PROFILES=""
COMPONENTS=""
OFFLINE_SOURCE=""
LOCAL_REPO=""
INTERNET_OVERRIDE=""
NON_INTERACTIVE=false
ASSUME_YES=false
CHECK_MODE=false
ALLOW_UPGRADE=false
MODE=install
ONLINE=false

# ------------------------------------------------------------------ output

# Mattermost brand colours: Denim #1E325C, Sapphire #1C58D9, Sunglow #FFBC1F.
# Truecolor where the terminal says it can, the nearest 256-colour match otherwise,
# and nothing at all for pipes, dumb terminals or NO_COLOR.
C_BOLD=""; C_DIM=""; C_RED=""; C_GRN=""; C_OFF=""; C_BLUE=""; C_YEL=""; C_BANNER=""; C_BAR=""
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != dumb ]]; then
  C_BOLD=$'\e[1m'; C_DIM=$'\e[2m'; C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_OFF=$'\e[0m'
  if [[ "${COLORTERM:-}" == truecolor || "${COLORTERM:-}" == 24bit ]]; then
    C_BLUE=$'\e[38;2;28;88;217m'                       # Sapphire text
    C_YEL=$'\e[38;2;255;188;31m'                       # Sunglow text
    C_BANNER=$'\e[48;2;30;50;92m\e[38;2;255;255;255m'  # white on Denim
    C_BAR=$'\e[48;2;30;50;92m\e[38;2;255;188;31m'      # Sunglow on Denim
  else
    C_BLUE=$'\e[38;5;26m'
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

say()  { printf '%s\n' "${C_BLUE}${C_BOLD}${G_ARROW}${C_OFF} ${C_BOLD}$*${C_OFF}"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s\n' "${C_YEL}${C_BOLD}WARNING:${C_OFF} $*" >&2; }
die()  { printf '%s\n' "${C_RED}${C_BOLD}ERROR:${C_OFF} $*" >&2; exit 1; }

no_input() {
  echo
  die "Ran out of input while asking questions. For unattended installs use --answers (see answers.example.yml)."
}

repeat() { local i out=""; for ((i = 0; i < $2; i++)); do out+="$1"; done; printf '%s' "${out}"; }

# A section heading in the interview: a Sapphire rule with the title in it.
section() {
  local title=" $1 " lead=3
  printf '\n%s%s%s%s%s%s\n' "${C_BLUE}${C_BOLD}" "$(repeat "${G_RULE}" "${lead}")" "${C_YEL}" "${title}" \
    "${C_BLUE}" "$(repeat "${G_RULE}" $((WIDTH - lead - ${#title})))${C_OFF}"
}

# The Denim banner with a Sunglow bar down the left edge.
banner() {
  local line pad
  echo
  for line in "" "  Mattermost Implement" "  $1" ""; do
    pad=$((WIDTH - 1 - ${#line})); (( pad < 0 )) && pad=0
    if [[ "${line}" == "  Mattermost Implement" ]]; then
      printf '%s%s%s%s%s%s\n' "${C_BAR}" "${G_BAR}" "${C_BANNER}${C_BOLD}" "${line}" "$(repeat ' ' "${pad}")" "${C_OFF}"
    else
      printf '%s%s%s%s%s%s\n' "${C_BAR}" "${G_BAR}" "${C_BANNER}" "${line}" "$(repeat ' ' "${pad}")" "${C_OFF}"
    fi
  done
  echo
}
trap 'die "install.sh stopped at line ${LINENO}. Logs are in ${LOG_DIR}."' ERR

usage() {
  cat <<EOF
Mattermost Implement: install Mattermost components on this RHEL server.

Usage: sudo ./install.sh [options]

What to install
  --answers FILE          Use an answer file (see answers.example.yml)
  --profile LIST          Profiles for this server, comma-separated:
                          ${PROFILE_NAMES[*]}
  --components LIST       Extra or individual components, comma-separated:
                          ${COMPONENT_NAMES[*]}

Where to get it
  --offline-dir DIR|URL   Offline bundle directory or internal mirror URL
  --local-repo PATH       Add a temporary dnf repo, e.g. a mounted RHEL ISO
  --online | --offline    Skip the internet check and say which it is

How to run
  --non-interactive       Never prompt. Needs --answers or --profile.
  -y, --yes               Do not ask for confirmation
  --upgrade               Let an installed Mattermost (and rtcd) move to a newer release
                          (it is stopped and its database dumped first)
  --check                 Dry run (Ansible check mode, best effort)
  --plan-only             Show what would be installed, then stop
  --verify-only           Re-run the post-deploy tests and refresh the report
  --report-only           Rebuild the report from the last test results
  -h, --help              Show this help

Logs go to ${LOG_DIR}. Answers are saved to ${SAVED_ANSWERS}.
EOF
}

# ------------------------------------------------------------------ args

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --answers) ANSWERS="$2"; shift 2 ;;
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
      -h|--help) usage; exit 0 ;;
      *) usage; die "Unknown option: $1" ;;
    esac
  done
  [[ -t 0 ]] || NON_INTERACTIVE=true
  if [[ -n "${ANSWERS}" ]]; then
    [[ -r "${ANSWERS}" ]] || die "Cannot read answer file ${ANSWERS}"
    ANSWERS="$(cd "$(dirname "${ANSWERS}")" && pwd)/$(basename "${ANSWERS}")"
  fi
}

# ------------------------------------------------------------------ host checks

require_root() {
  [[ ${EUID} -eq 0 ]] || die "Run this as root (sudo ./install.sh)."
}

check_os() {
  [[ -r /etc/os-release ]] || die "No /etc/os-release. This toolkit needs RHEL 9 or 10."
  # shellcheck disable=SC1091
  . /etc/os-release
  local major="${VERSION_ID%%.*}"
  case "${ID}" in
    rhel) ;;
    rocky|almalinux|ol|centos) warn "${PRETTY_NAME} is not RHEL. It should work, but test it before you rely on it." ;;
    *) die "${PRETTY_NAME} is not supported. Use RHEL 9 or 10." ;;
  esac
  [[ "${major}" == 9 || "${major}" == 10 ]] || die "${PRETTY_NAME} is not supported. Use RHEL 9 or 10."
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
  [github.com]="rtcd, Prometheus and the exporters"
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
    printf '      %s %s\n' "${C_BLUE}$(printf '%-38s' "${h}")${C_OFF}" "${C_DIM}${SOURCE_PURPOSE[${h}]}${C_OFF}"
  done
}

# Check every source in parallel. Any HTTP answer counts as reachable (api.github.com
# answers 403 when it rate-limits); only a network failure or timeout counts as down.
check_sources() {
  local tmp h failed=0
  tmp="$(mktemp -d)"
  for h in "${SOURCE_HOSTS[@]}"; do
    ( if curl -sS -o /dev/null --max-time 10 --retry 1 --retry-delay 2 "https://${h}/" 2>/dev/null; then
        echo ok; else echo fail; fi > "${tmp}/${h}" ) &
  done
  wait
  for h in "${SOURCE_HOSTS[@]}"; do
    SOURCE_OK[${h}]="$(cat "${tmp}/${h}")"
    if [[ "${SOURCE_OK[${h}]}" == ok ]]; then
      printf '      %s %s %s\n' "${C_GRN}${C_BOLD}ok  ${C_OFF}" "$(printf '%-38s' "${h}")" "${C_DIM}${SOURCE_PURPOSE[${h}]}${C_OFF}"
    else
      printf '      %s %s %s\n' "${C_RED}${C_BOLD}FAIL${C_OFF}" "$(printf '%-38s' "${h}")" "${C_DIM}${SOURCE_PURPOSE[${h}]}${C_OFF}"
      failed=$((failed + 1))
    fi
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
bold, dim, blue, yel, red, grn, off, src = sys.argv[1:9]
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
print("    Found in %s%s%s (%s, %s):" % (blue, src, off, "bundle with manifest" if inv["kind"] == "bundle" else "folder", inv["arch"]))
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
print("      %s%-20s%s %s" % (bold, "prerequisite RPMs", off, (grn + "present" + off) if ex["rpms"] else (dim + "not included" + off)))
' "${C_BOLD}" "${C_DIM}" "${C_BLUE}" "${C_YEL}" "${C_RED}" "${C_GRN}" "${C_OFF}" "${src}"
}

prompt_for_source() {
  local src
  info "Point me at the installers instead: a bundle made with tools/build-offline-bundle.sh,"
  info "an internal mirror URL, or a folder holding the vendors' files as downloaded."
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
      info "The answer file says this server has internet access, so I won't check."
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
    info "Per the configuration in ${SAVED_ANSWERS}, I'll check the installer sources without asking."
  elif [[ -z "${setting}" && "${NON_INTERACTIVE}" != true ]]; then
    info "An online install downloads from these sites:"
    list_sources
    [[ -n "${https_proxy:-${HTTPS_PROXY:-}}" ]] && info "${C_DIM}Using the proxy in https_proxy: ${https_proxy:-${HTTPS_PROXY:-}}${C_OFF}"
    if ! ask_yn "Can I check whether this server reaches them?" y; then
      info "No problem. Without the check we'll use a local copy of the installers."
      prompt_for_source
      return 0
    fi
  fi

  say "Checking the installer sources"
  check_sources || failed=$?
  if [[ "${failed}" -eq 0 ]]; then
    ONLINE=true
    info "All installer sources are reachable, so we'll download from the vendors."
    return 0
  fi

  echo
  warn "${failed} of ${#SOURCE_HOSTS[@]} installer sources can't be reached from this server."
  if [[ "${NON_INTERACTIVE}" == true ]]; then
    die "Set artifact_source in the answer file (or pass --offline-dir) to a bundle from tools/build-offline-bundle.sh, or open up the sites marked FAIL."
  fi
  ask_choice choice "What would you like to do?" "Use a local folder or mirror" \
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
  [[ -d "${LOCAL_REPO}" ]] || die "--local-repo ${LOCAL_REPO} is not a directory"
  say "Adding temporary dnf repos from ${LOCAL_REPO}"
  local repo_file=/etc/yum.repos.d/mattermost-implement-local.repo
  : > "${repo_file}"
  local sub
  for sub in BaseOS AppStream; do
    if [[ -d "${LOCAL_REPO}/${sub}/repodata" ]]; then
      printf '[mmd-local-%s]\nname=mattermost-implement local %s\nbaseurl=file://%s/%s\nenabled=1\ngpgcheck=1\ngpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release\n\n' \
        "${sub}" "${sub}" "${LOCAL_REPO}" "${sub}" >> "${repo_file}"
    fi
  done
  if [[ ! -s "${repo_file}" && -d "${LOCAL_REPO}/repodata" ]]; then
    printf '[mmd-local]\nname=mattermost-implement local\nbaseurl=file://%s\nenabled=1\ngpgcheck=0\n' "${LOCAL_REPO}" > "${repo_file}"
  fi
  [[ -s "${repo_file}" ]] || die "No repodata found under ${LOCAL_REPO}"
  info "Wrote ${repo_file}. Remove it when you no longer need it."
}

# ------------------------------------------------------------------ prerequisites

# curl is left out on purpose: minimal RHEL 9 ships curl-minimal, which conflicts
# with the curl package, and either gives us the command we need.
PREREQ_PACKAGES=(ansible-core python3 python3-libselinux gnupg2 tar openssl policycoreutils-python-utils firewalld)

install_prereqs() {
  say "Checking prerequisites"
  local missing=() pkg
  for pkg in "${PREREQ_PACKAGES[@]}"; do
    rpm -q "${pkg}" >/dev/null 2>&1 || missing+=("${pkg}")
  done
  command -v curl >/dev/null || missing+=(curl-minimal)
  if [[ ${#missing[@]} -gt 0 ]]; then
    info "Installing: ${missing[*]}"
    if ! dnf -y install "${missing[@]}" >> "${LOG_FILE}" 2>&1; then
      local rpm_dir=""
      [[ -n "${OFFLINE_SOURCE}" && -d "${OFFLINE_SOURCE}/rpms" ]] && rpm_dir="${OFFLINE_SOURCE}/rpms"
      if [[ -n "${rpm_dir}" ]]; then
        info "The configured repos failed; using RPMs from ${rpm_dir}"
        dnf -y --disablerepo='*' install "${rpm_dir}"/*.rpm >> "${LOG_FILE}" 2>&1 \
          || die "Could not install prerequisites from ${rpm_dir}. See ${LOG_FILE}."
      else
        die "Could not install ${missing[*]}. Check the dnf repos (Satellite, a local mirror or --local-repo), or build a bundle with --with-rpms. See ${LOG_FILE}."
      fi
    fi
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
  printf -v "${__var}" '%s' "${reply:-${default}}"
}

ask_addresses() {
  # ask_addresses VAR "Who connects" "What breaks when blank"
  local __var="$1" who="$2" why="$3" __addrs
  while true; do
    ask __addrs "${who} (comma-separated IPs or CIDRs, 0.0.0.0/0 = any)" ""
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
        echo "    There's no ${what} at ${__path} on this server. Copy it there and give the path again, or leave it blank."
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

ask_secret() {
  local __var="$1" prompt="$2" reply
  read -r -s -p "  ${C_YEL}${C_BOLD}?${C_OFF} ${prompt} ${C_DIM}(blank = generate)${C_OFF}: " reply || no_input
  echo
  printf -v "${__var}" '%s' "${reply}"
}

ask_yn() {
  local prompt="$1" default="${2:-y}" reply
  read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} ${prompt} ${C_DIM}[${C_OFF}${C_YEL}$( [[ ${default} == y ]] && echo Y/n || echo y/N )${C_OFF}${C_DIM}]${C_OFF}: " reply || no_input
  reply="${reply:-${default}}"
  [[ "${reply,,}" == y* ]]
}

ask_choice() {
  # ask_choice VAR "Question" default option1 option2 ...
  local __var="$1" prompt="$2" default="$3"; shift 3
  local options=("$@") i reply
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

has_component() { [[ " ${SELECTED} " == *" $1 "* ]]; }

choose_components() {
  local i reply name chosen_profiles=() extra=()
  section "What should this server run?"
  for i in "${!PROFILE_NAMES[@]}"; do
    name="${PROFILE_NAMES[$i]}"
    printf '      %s %s %s\n' "${C_YEL}$((i + 1)))${C_OFF}" "${C_BLUE}${C_BOLD}$(printf '%-11s' "${name}")${C_OFF}" "${PROFILE_DESCRIPTIONS[${name}]}"
  done
  printf '      %s %s %s\n' "${C_YEL}c)${C_OFF}" "${C_BLUE}${C_BOLD}$(printf '%-11s' custom)${C_OFF}" "pick individual components"
  echo
  info "${C_DIM}Several profiles can share a server, e.g. 1,3. We recommend one role per server for production.${C_OFF}"
  while true; do
    read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} Profiles ${C_YEL}${G_ARROW}${C_OFF} " reply || no_input
    [[ -n "${reply}" ]] || continue
    chosen_profiles=(); extra=()
    if [[ "${reply}" == c ]]; then
      for i in "${!COMPONENT_NAMES[@]}"; do
        printf '      %s %s\n' "${C_YEL}$((i + 1)))${C_OFF}" "${COMPONENT_NAMES[$i]}"
      done
      read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} Components ${C_DIM}(comma-separated numbers)${C_OFF} ${C_YEL}${G_ARROW}${C_OFF} " reply || no_input
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
    echo "    That did not match anything. Try again."
  done
  ANS_PROFILES="$(IFS=,; echo "${chosen_profiles[*]-}")"
  ANS_COMPONENTS="$(IFS=,; echo "${extra[*]-}")"
  SELECTED=""
  local p
  for p in "${chosen_profiles[@]+"${chosen_profiles[@]}"}"; do SELECTED+=" ${PROFILE_COMPONENTS[${p}]}"; done
  SELECTED+=" ${extra[*]-} "
}

interview() {
  local answers_out="$1"
  local fqdn
  fqdn="$(host_name)"
  choose_components

  {
    echo "# Mattermost Implement answer file, written by install.sh on $(date -u '+%Y-%m-%d %H:%M UTC') for ${fqdn}."
    echo "# Re-run with: sudo ./install.sh --answers ${SAVED_ANSWERS}"
    echo "# answers.example.yml in the repo explains every setting."
  } > "${answers_out}"
  emit() { printf '%s: %s\n' "$1" "$2" >> "${answers_out}"; }

  local name
  ask name "Deployment name (used in the report and labels)" "mattermost"
  emit deploy_name "$(yaml_quote "${name}")"
  emit deploy_profiles "$(yaml_list "${ANS_PROFILES}")"
  emit deploy_components "$(yaml_list "${ANS_COMPONENTS}")"

  if has_component mattermost; then
    section "Mattermost"
    local site edition channel version lic
    ask_url site "Site URL users will use" "https://${fqdn}"
    emit mattermost_site_url "$(yaml_quote "${site}")"
    MM_SITE_URL="${site}"
    ask_choice edition "Edition" enterprise enterprise team
    emit mattermost_edition "${edition}"
    ask_choice channel "Which build" latest-esr latest-esr latest-stable pinned
    emit mattermost_version_channel "${channel}"
    if [[ "${channel}" == pinned ]]; then
      ask version "Version to install (e.g. 11.7.14)" ""
      emit mattermost_version "$(yaml_quote "${version}")"
    fi
    if [[ "${edition}" == enterprise ]]; then
      ask_path lic "Path to the licence file on this server (blank to add later)" "" file optional
      emit mattermost_license_file "$(yaml_quote "${lic}")"
    fi

    local store
    ask_choice store "Where should uploaded files go" local local amazons3
    emit mattermost_file_driver "${store}"
    if [[ "${store}" == local ]]; then
      local data
      ask data "Data directory (local disk or an NFS mount)" "/var/opt/mattermost/data"
      emit mattermost_data_dir "$(yaml_quote "${data}")"
    else
      local ep bucket region ak sk
      ask ep "S3 endpoint (host[:port], no https://)" ""
      ask bucket "Bucket" ""
      ask region "Region (blank if not needed)" ""
      ask ak "Access key ID" ""
      read -r -s -p "    Secret access key: " sk || no_input; echo
      emit mattermost_s3_endpoint "$(yaml_quote "${ep}")"
      emit mattermost_s3_bucket "$(yaml_quote "${bucket}")"
      emit mattermost_s3_region "$(yaml_quote "${region}")"
      emit mattermost_s3_access_key "$(yaml_quote "${ak}")"
      emit mattermost_s3_secret_key "$(yaml_quote "${sk}")"
      if ask_yn "Use HTTPS to reach S3?" y; then
        emit mattermost_s3_ssl true
      else
        emit mattermost_s3_ssl false
      fi
    fi

    if ! has_component postgresql; then
      local dbh dbp dbpass
      section "Database (on another server)"
      ask dbh "PostgreSQL host" ""
      ask dbp "Port" "5432"
      read -r -s -p "    Password for mmuser (from secrets.yml on the database server): " dbpass || no_input; echo
      emit mattermost_db_host "$(yaml_quote "${dbh}")"
      emit mattermost_db_port "${dbp}"
      emit mattermost_db_password "$(yaml_quote "${dbpass}")"
      local dbreplicas
      ask dbreplicas "PostgreSQL read replica addresses for reads (comma-separated, blank = none)" ""
      emit mattermost_db_replica_hosts "$(yaml_list "${dbreplicas}")"
    fi
    if ! has_component nginx; then
      local proxies
      ask proxies "Reverse proxy addresses allowed to reach port 8065 (comma-separated, blank = any)" ""
      emit mattermost_proxy_sources "$(yaml_list "${proxies}")"
    fi
    if ! has_component rtcd; then
      local rurl
      ask rurl "rtcd URL if Calls runs on another server, e.g. http://rtcd01:8045 (blank = built in)" ""
      emit mattermost_rtcd_url "$(yaml_quote "${rurl}")"
    fi
    if ! has_component opensearch; then
      local osurl
      ask osurl "OpenSearch URL if search runs on another server (blank = none)" ""
      emit mattermost_opensearch_url "$(yaml_quote "${osurl}")"
      if [[ "${osurl}" == https://* ]]; then
        local ospass osca
        read -r -s -p "    Password for the OpenSearch 'mattermost' user: " ospass || no_input; echo
        ask_path osca "Path to the search CA on this server (copy /etc/opensearch/certs/root-ca.pem from the search server)" \
          "/etc/pki/tls/certs/opensearch-ca.pem" file
        emit mattermost_opensearch_password "$(yaml_quote "${ospass}")"
        emit mattermost_opensearch_ca_file "$(yaml_quote "${osca}")"
      fi
    fi
  fi

  if has_component postgresql; then
    section "PostgreSQL"
    local dbpass clients repl="none"
    if ! has_component mattermost; then
      ask_choice repl "Replication (a replica is a read-only copy of a primary; install the primary first)" none none primary replica
      emit postgresql_replication "${repl}"
    fi
    if [[ "${repl}" == replica ]]; then
      local primary rpass
      ask primary "Primary server address" ""
      read -r -s -p "    Password for mmuser (db_password in the primary's secrets.yml): " dbpass || no_input; echo
      read -r -s -p "    Replication password (replication_password in the primary's secrets.yml): " rpass || no_input; echo
      emit postgresql_primary_host "$(yaml_quote "${primary}")"
      emit postgresql_replication_password "$(yaml_quote "${rpass}")"
    else
      ask_secret dbpass "Password for the mmuser database role"
    fi
    emit mattermost_db_password "$(yaml_quote "${dbpass}")"
    if [[ "${repl}" == primary ]]; then
      local replicas
      ask replicas "Replica server addresses (comma-separated IPs)" ""
      emit postgresql_replica_hosts "$(yaml_list "${replicas}")"
    fi
    if ! has_component mattermost; then
      ask_addresses clients "Mattermost app server addresses allowed to connect" \
        "The app servers can't reach the database without at least one address."
      emit postgresql_allowed_clients "$(yaml_list "${clients}")"
    fi
  fi

  if has_component nginx; then
    section "Nginx and TLS"
    local tls
    ask_choice tls "TLS certificate" later later provided
    emit tls_mode "${tls}"
    if [[ "${tls}" == provided ]]; then
      local how
      ask_choice how "Where is it" "two files" "two files" "a directory"
      if [[ "${how}" == "two files" ]]; then
        local cert key chain
        ask_path cert "Certificate file (PEM, full chain preferred)" "" file
        ask_path key "Private key file" "" file
        ask_path chain "Intermediate chain file (blank if already in the certificate)" "" file optional
        emit tls_cert_file "$(yaml_quote "${cert}")"
        emit tls_key_file "$(yaml_quote "${key}")"
        emit tls_chain_file "$(yaml_quote "${chain}")"
      else
        local dir sname="${MM_SITE_URL#*://}"
        sname="${sname%%[/:]*}"
        ask_path dir "Directory holding ${sname:-<server name>}.crt/.key or tls.crt/tls.key" "/etc/pki/tls/mattermost-src" dir
        emit tls_source_dir "$(yaml_quote "${dir}")"
      fi
    fi
    if ! has_component mattermost; then
      local ups
      ask ups "Mattermost app servers to proxy to (host:8065, comma-separated)" ""
      emit nginx_upstreams "$(yaml_list "${ups}")"
    fi
  fi

  if has_component rtcd; then
    section "Calls (rtcd)"
    local ice srcs
    ask ice "Public IP or hostname clients use to reach this server (blank if not behind NAT)" ""
    emit rtcd_ice_host_override "$(yaml_quote "${ice}")"
    if ! has_component mattermost; then
      ask_addresses srcs "Mattermost app server addresses allowed to use the rtcd API" \
        "The app servers can't reach rtcd without at least one address, so calls would fail."
      emit rtcd_api_sources "$(yaml_list "${srcs}")"
    fi
  fi

  if has_component opensearch && ! has_component mattermost; then
    section "OpenSearch"
    local clients ospass
    ask_addresses clients "Mattermost app server addresses allowed to connect" \
      "The app servers can't reach OpenSearch without at least one address."
    emit opensearch_allowed_clients "$(yaml_list "${clients}")"
    ask_secret ospass "Password for the OpenSearch 'mattermost' user"
    emit mattermost_opensearch_password "$(yaml_quote "${ospass}")"
  fi

  # Grafana builds its alerts and dashboards from the same server list Prometheus
  # scrapes, so a Grafana server without Prometheus needs the list too.
  if has_component prometheus || has_component grafana; then
    section "Monitoring the estate"
    local build_q="Build monitors, alerts and dashboards for the estate?" site_q="Site URL users reach (checked from here; blank = skip)"
    if has_component prometheus; then
      info "We can build Prometheus jobs, tiered alerts (info, warning, critical) and dashboards"
      info "in Grafana for every server in this Mattermost estate. This server is included already."
    else
      info "We can build tiered alerts (info, warning, critical) and dashboards in Grafana for every"
      info "server in this Mattermost estate. This server is included already."
      info "Give the same site URL and server names as on the Prometheus server."
      build_q="Build alerts and dashboards for the estate?"
      site_q="Site URL the Prometheus server checks (blank = none)"
    fi
    if ask_yn "${build_q}" y; then
      emit monitoring_alerts_enabled true
      emit monitoring_dashboards_enabled true
      local site_default="" site ca joined
      has_component mattermost && site_default="${MM_SITE_URL:-}"
      info "${C_DIM}This is Mattermost's Site URL, including https://, e.g. https://chat.example.com${C_OFF}"
      ask_url site "${site_q}" "${site_default}" optional
      emit monitoring_site_url "$(yaml_quote "${site}")"
      if has_component prometheus && [[ "${site}" == https://* ]] && ! ask_yn "Does the site's certificate chain to a CA this server already trusts?" y; then
        ask_path ca "Path to that CA certificate on this server (blank = don't verify)" "" file optional
        if [[ -n "${ca}" ]]; then
          emit monitoring_site_ca_file "$(yaml_quote "${ca}")"
        else
          emit monitoring_site_tls_verify false
        fi
      fi

      echo
      info "${C_DIM}Now the other servers. For each one, give its name, address and what it runs.${C_OFF}"
      local estate_lines=() snames=() saddrs=() scomps_list=() sname saddr scomps n bad picked names
      local i short mods=(mattermost nginx postgresql rtcd opensearch)
      has_component prometheus || mods+=(prometheus)
      short="$(hostname -s 2>/dev/null || uname -n)"
      while true; do
        estate_lines=() snames=() saddrs=() scomps_list=()
        while true; do
          ask sname "Server name (blank when done)" ""
          [[ -z "${sname}" ]] && break
          if [[ "${sname}" == "${short}" || "${sname}" == "${fqdn}" ]]; then
            echo "    ${sname} is this server, which is included already."
            continue
          fi
          if printf '%s\n' "${snames[@]}" | grep -Fxq -- "${sname}"; then
            echo "    You've already added ${sname}."
            continue
          fi
          while true; do
            ask saddr "Address of ${sname} (IP or DNS name)" ""
            [[ -n "${saddr}" ]] && break
            echo "    Prometheus needs an address to reach ${sname}."
          done
          for i in "${!mods[@]}"; do
            printf '      %s %s\n' "${C_YEL}$((i + 1)))${C_OFF}" "${mods[$i]}"
          done
          while true; do
            read -r -p "  ${C_YEL}${C_BOLD}?${C_OFF} What runs on ${sname} ${C_DIM}(comma-separated numbers, blank = host metrics only)${C_OFF} ${C_YEL}${G_ARROW}${C_OFF} " scomps || no_input
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
        done
        [[ ${#estate_lines[@]} -eq 0 ]] && break
        echo
        for i in "${!snames[@]}"; do
          printf '    %-20s %-24s %s\n' "${snames[$i]}" "${saddrs[$i]}" "${scomps_list[$i]:-host metrics only}"
        done
        ask_yn "Look right?" y && break
        echo "    Let's go through the servers again."
      done
      if [[ ${#estate_lines[@]} -gt 0 ]]; then
        echo "monitoring_estate:" >> "${answers_out}"
        printf '%s\n' "${estate_lines[@]}" >> "${answers_out}"
      fi
      if [[ ${#estate_lines[@]} -gt 0 ]] && has_component prometheus; then
        local self_ip
        self_ip="$(primary_ip)"
        while [[ -z "${self_ip}" ]]; do
          ask self_ip "This server's address, as the other servers see it" ""
        done
        echo
        info "Install each of those servers with these two answers so this server can scrape them:"
        info "${C_YEL}deploy_monitoring_exporters: true${C_OFF}"
        info "${C_YEL}monitoring_scrape_sources: [${self_ip}]${C_OFF}"
      fi
    else
      emit monitoring_alerts_enabled false
      emit monitoring_dashboards_enabled false
    fi
  fi
  if has_component grafana; then
    local gpass
    ask_secret gpass "Grafana admin password"
    emit grafana_admin_password "$(yaml_quote "${gpass}")"
    if ! has_component prometheus; then
      local purl
      ask purl "Prometheus URL Grafana should use" "http://prometheus01:9090"
      emit grafana_prometheus_url "$(yaml_quote "${purl}")"
    fi
  fi

  if ! has_component prometheus; then
    section "Monitoring"
    info "Exporters let a monitoring server elsewhere watch this one (host, and each component here)."
    if ask_yn "Install exporters for a monitoring server elsewhere?" y; then
      local scr
      emit deploy_monitoring_exporters true
      ask_addresses scr "Prometheus server addresses allowed to scrape this server" \
        "Prometheus can't scrape the exporters without at least one address."
      emit monitoring_scrape_sources "$(yaml_list "${scr}")"
    fi
  fi

  if [[ "${ONLINE}" != true && -z "${OFFLINE_SOURCE}" ]]; then
    section "Offline artifacts"
    local src
    ask src "Bundle directory or mirror URL (from tools/build-offline-bundle.sh)" "/srv/mm-bundle"
    OFFLINE_SOURCE="${src}"
  fi
  [[ -n "${OFFLINE_SOURCE}" ]] && emit artifact_source "$(yaml_quote "${OFFLINE_SOURCE}")"
  if [[ "${ONLINE}" == true ]]; then emit deploy_internet auto; else emit deploy_internet "'no'"; fi
  chmod 0600 "${answers_out}"
}

resolve_answers() {
  if [[ -n "${ANSWERS}" ]]; then
    return
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
  if [[ -r "${SAVED_ANSWERS}" ]] && section "Answers" && ask_yn "Reuse the answers from the last run (${SAVED_ANSWERS})?" y; then
    ANSWERS="${SAVED_ANSWERS}"
    [[ -n "${OFFLINE_SOURCE}" ]] || OFFLINE_SOURCE="$(yaml_get artifact_source "${ANSWERS}")"
    return 0
  fi
  local tmp
  tmp="$(mktemp "${CONFIG_DIR}/answers.XXXXXX")"
  interview "${tmp}"
  if [[ -f "${SAVED_ANSWERS}" ]]; then
    cp -p "${SAVED_ANSWERS}" "${SAVED_ANSWERS}.${RUN_ID}.bak"
  fi
  mv "${tmp}" "${SAVED_ANSWERS}"
  chmod 0600 "${SAVED_ANSWERS}"
  ANSWERS="${SAVED_ANSWERS}"
  info "Saved your answers to ${SAVED_ANSWERS}"
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
if args.get("profiles"):
    out["deploy_profiles"] = [p.strip() for p in args["profiles"].split(",") if p.strip()]
if args.get("components"):
    out["deploy_components"] = [c.strip() for c in args["components"].split(",") if c.strip()]
if args.get("plan_file"):
    out["plan_output_file"] = args["plan_file"]
if args.get("upgrade") == "true":
    out["mattermost_allow_upgrade"] = True
    out["rtcd_allow_upgrade"] = True
print(json.dumps(out))
PY
}

run_playbook() {
  local playbook="$1"; shift
  local extra
  extra="$(extra_vars_json "online=${ONLINE}" "version=${TOOL_VERSION}" "run_id=${RUN_ID}" \
    "answers=${ANSWERS}" "source=${OFFLINE_SOURCE}" "profiles=${PROFILES}" "components=${COMPONENTS}" \
    "upgrade=${ALLOW_UPGRADE}" "$@")"
  local args=(-e "@${ANSWERS}" -e "${extra}")
  [[ "${CHECK_MODE}" == true && "${playbook}" == site.yml ]] && args+=(--check --diff)
  (
    cd "${REPO_DIR}"
    ANSIBLE_CONFIG="${REPO_DIR}/ansible.cfg" ANSIBLE_LOG_PATH="${LOG_FILE}" \
      ansible-playbook "playbooks/${playbook}" "${args[@]}"
  )
}

show_plan() {
  local plan_file
  plan_file="$(mktemp)"
  say "Working out the plan"
  if ! run_playbook plan.yml "plan_file=${plan_file}" > "${LOG_DIR}/plan-${RUN_ID}.out" 2>&1; then
    rm -f "${plan_file}"
    grep -E '"msg"|fatal|ERROR' "${LOG_DIR}/plan-${RUN_ID}.out" | tail -n 5 >&2 || true
    die "The plan failed. Fix the answer file and try again. Full output: ${LOG_DIR}/plan-${RUN_ID}.out"
  fi
  section "The plan"
  python3 -I - "${plan_file}" "${OS_PRETTY}" "${C_BOLD}" "${C_DIM}" "${C_BLUE}" "${C_YEL}" "${C_RED}" "${C_OFF}" <<'PY'
import json, sys, textwrap
plan = json.load(open(sys.argv[1]))
os_name, bold, dim, blue, yel, red, off = sys.argv[2:9]

def row(label, value):
    print(f"    {blue}{bold}{label:<12}{off}{value}")

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
  ask_yn "Go ahead and deploy?" n || { say "Stopped. Nothing was changed."; exit 0; }
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
  banner "${TOOL_VERSION} ${G_DOT} $(host_name) ${G_DOT} ${OS_PRETTY}"
  [[ -z "${LOCAL_REPO}" ]] || setup_local_repo
  # Only installs need installers; re-testing and reporting work from what is on disk.
  if [[ "${MODE}" == install || "${MODE}" == plan ]]; then
    choose_installer_source
  fi
  install_prereqs
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

main "$@"
