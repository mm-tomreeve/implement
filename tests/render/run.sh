#!/usr/bin/env bash
# Render every scenario in tests/render/scenarios to tests/render/out/<name>.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${HERE}/../.." && pwd)"
OUT="${HERE}/out"
PLAYBOOK="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
mkdir -p "${OUT}/state"
export ANSIBLE_CONFIG="${REPO_DIR}/ansible.cfg"
export ANSIBLE_LIBRARY="${HERE}/library"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-auto_silent}"
rc=0
for scenario in "${HERE}"/scenarios/*.yml; do
  name="$(basename "${scenario}" .yml)"
  printf '%-28s ' "${name}"
  if "${PLAYBOOK}" -i "${REPO_DIR}/inventory/localhost.yml" "${HERE}/render.yml" \
      -e "@${scenario}" -e "render_name=${name}" -e "render_out=${OUT}" \
      -e "deploy_state_dir=${OUT}/state" -e "deploy_config_dir=${OUT}/state" \
      -e "mmd_answers_file=${scenario}" -e "report_owner=$(id -un)" -e "report_group=$(id -gn)" > "${OUT}/${name}.log" 2>&1; then
    echo "ok   ${OUT}/${name}.md"
  else
    echo "FAIL ${OUT}/${name}.log"; rc=1
  fi
done
exit "${rc}"
