"""Checks that the wrapper, catalogue, profiles, roles and resolver agree.

Needs PyYAML (it comes with ansible-core).
"""

import os
import re
import sys
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import resolve_versions as rv  # noqa: E402


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return yaml.safe_load(handle)


PROFILES = load("inventory/group_vars/all/profiles.yml")
CATALOGUE = load("inventory/group_vars/all/catalogue.yml")["mmd_catalogue"]
with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as handle:
    INSTALL_SH = handle.read()


class Consistency(unittest.TestCase):
    def test_wrapper_profiles_match_profiles_yml(self):
        block = re.search(r"declare -A PROFILE_COMPONENTS=\((.*?)\n\)", INSTALL_SH, re.S).group(1)
        wrapper = {m.group(1): m.group(2).split() for m in re.finditer(r'\[([\w-]+)\]="([^"]*)"', block)}
        yml = {name: p["components"] for name, p in PROFILES["mmd_profiles"].items()}
        self.assertEqual(wrapper, yml)
        names = re.search(r"PROFILE_NAMES=\(([^)]*)\)", INSTALL_SH).group(1).split()
        self.assertEqual(sorted(names), sorted(yml))

    def test_wrapper_components_match_catalogue(self):
        names = re.search(r"COMPONENT_NAMES=\(([^)]*)\)", INSTALL_SH).group(1).split()
        self.assertEqual(sorted(names), sorted(CATALOGUE))

    def test_estate_choices_have_scrape_jobs(self):
        # Whatever the interview lets you say another server runs, Prometheus must scrape.
        mods = re.search(r"local i short mods=\(([^)]*)\)", INSTALL_SH).group(1).split()
        with open(os.path.join(ROOT, "roles/prometheus/templates/prometheus.yml.j2"), encoding="utf-8") as handle:
            jobs = set(re.findall(r"'component': '(\w+)'", handle.read()))
        # NFS has no exporter of its own. The node job scrapes every server in the estate,
        # and node_exporter's nfsd, nfs and mountstats collectors carry the NFS metrics.
        jobs.add("nfs")
        self.assertEqual(sorted(set(mods) - jobs), [])
        self.assertTrue({"prometheus", "loki", "grafana"} <= set(mods))

    def test_install_order_covers_every_component(self):
        self.assertEqual(sorted(PROFILES["mmd_component_order"]), sorted(CATALOGUE))

    def test_every_component_has_a_role_with_verify(self):
        for component in CATALOGUE:
            for part in ("tasks/main.yml", "tasks/verify.yml", "meta/main.yml"):
                path = os.path.join(ROOT, "roles", component, part)
                self.assertTrue(os.path.isfile(path), path)

    def test_resolver_and_artifacts_role_agree(self):
        role = load("roles/artifacts/defaults/main.yml")["artifacts_tarball_components"]
        self.assertEqual(sorted(role), sorted(rv.COMPONENT_ARTIFACTS))
        self.assertTrue(set(rv.COMPONENT_ARTIFACTS) <= set(CATALOGUE))

    def test_vendored_keys_exist(self):
        for key in (rv.MATTERMOST_KEY["gpg_key"], rv.OPENSEARCH_KEY["gpg_key"]):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "roles", "artifacts", "files", key)))

    def test_fingerprint_matches_defaults(self):
        defaults = load("inventory/group_vars/all/defaults.yml")
        self.assertEqual(defaults["mattermost_gpg_fingerprint"], rv.MATTERMOST_KEY["gpg_fingerprint"])
        self.assertEqual(defaults["opensearch_gpg_fingerprint"], rv.OPENSEARCH_KEY["gpg_fingerprint"])


if __name__ == "__main__":
    unittest.main()


class Templating(unittest.TestCase):
    def test_ansible_managed_only_in_templates(self):
        # ansible_managed only exists inside the template module. In a copy task it fails at runtime.
        offenders = []
        for root, _dirs, files in os.walk(os.path.join(ROOT, "roles")):
            if os.sep + "tasks" not in root:
                continue
            for name in files:
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    if "ansible_managed" in handle.read():
                        offenders.append(os.path.relpath(os.path.join(root, name), ROOT))
        self.assertEqual(offenders, [])
