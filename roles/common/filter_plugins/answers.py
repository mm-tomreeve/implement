"""Checks an answer file against the settings in group_vars/all/defaults.yml.

defaults.yml holds every setting the answer file can override, so a key that
isn't there is a typo, and a value should have the same type as its default.
"""

import difflib

import yaml

# Read by install.sh, not by Ansible.
WRAPPER_ONLY = {"deploy_internet"}
# roles/common/tasks/selection.yml also takes these as a comma-separated string.
LIST_OR_STRING = {"deploy_profiles", "deploy_components"}


def answer_problems(answers_path, defaults_path):
    with open(defaults_path, encoding="utf-8") as f:
        defaults = yaml.safe_load(f)
    with open(answers_path, encoding="utf-8") as f:
        answers = yaml.safe_load(f) or {}
    if not isinstance(answers, dict):
        return ["The answer file should be a list of 'setting: value' lines."]

    problems = []
    for key, value in answers.items():
        if key in WRAPPER_ONLY:
            continue
        if key not in defaults:
            close = difflib.get_close_matches(str(key), defaults, n=1)
            problems.append(f"{key} isn't a setting." + (f" Did you mean {close[0]}?" if close else ""))
            continue
        if value is None:
            problems.append(f'{key} is empty. Write "" or [], or remove the line.')
            continue
        want = type(defaults[key])
        if want is str:
            # Plain strings, but also 'auto'-or-a-number settings and templated defaults.
            ok = not isinstance(value, (list, dict))
        elif want is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif want is list and key in LIST_OR_STRING:
            ok = isinstance(value, (list, str))
        else:
            ok = isinstance(value, want)
        if not ok:
            problems.append(f"{key} should be {TYPE_NAMES[want]}, not {TYPE_NAMES.get(type(value), type(value).__name__)}.")
    return problems


TYPE_NAMES = {
    str: "text",
    int: "a whole number",
    float: "a number",
    bool: "true or false",
    list: "a list, e.g. [a, b]",
    dict: "a mapping of 'key: value' lines",
}


class FilterModule:
    def filters(self):
        return {"answer_problems": answer_problems}
