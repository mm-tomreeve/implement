"""Checks that the wrapper, catalogue, profiles, roles and resolver agree.

Needs PyYAML (it comes with ansible-core).
"""

import base64
import hashlib
import os
import re
import sys
import unittest

import jinja2
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
    def test_version_is_the_same_everywhere(self):
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as handle:
            version = handle.read().strip()
        with open(os.path.join(ROOT, "wizard", "index.html"), encoding="utf-8") as handle:
            self.assertIn(">v%s<" % version, handle.read())
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            self.assertIn("**Version %s**" % version, handle.read())

    def test_wizard_loads_nothing_from_the_network(self):
        """The wizard tells people it keeps what they type to itself.

        It can only say that while it fetches nothing: a script pulled from a CDN
        could be swapped for one that forwards every address and hostname typed
        into the page, and the page would run it. The libraries live in
        wizard/vendor, which is also what makes the page work behind an air gap.
        """
        with open(os.path.join(ROOT, "wizard", "index.html"), encoding="utf-8") as handle:
            page = handle.read()
        remote = re.findall(r'<(?:script|link)[^>]*\b(?:src|href)="(https?://[^"]+)"', page)
        self.assertEqual(remote, [], "the wizard should carry these rather than fetch them")

    def test_vendored_libraries_match_their_checksums(self):
        """A damaged or swapped vendored file should fail here, not in a browser."""
        folder = os.path.join(ROOT, "wizard", "vendor")
        with open(os.path.join(folder, "README.md"), encoding="utf-8") as handle:
            recorded = dict(re.findall(r"^(\S+\.js)\s+sha384-(\S+)$", handle.read(), re.M))
        self.assertTrue(recorded, "vendor/README.md records no checksums")
        for name, want in recorded.items():
            with open(os.path.join(folder, name), "rb") as handle:
                got = base64.b64encode(hashlib.sha384(handle.read()).digest()).decode()
            self.assertEqual(got, want, name + " does not match the hash in vendor/README.md")

        # Every library the page loads should be one of the files we checked.
        with open(os.path.join(ROOT, "wizard", "index.html"), encoding="utf-8") as handle:
            loaded = re.findall(r'<script[^>]*\bsrc="vendor/([^"]+)"', handle.read())
        self.assertEqual(sorted(loaded), sorted(recorded))

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
        # calls-offloader publishes no metrics at all, so Prometheus probes its API
        # through blackbox instead of scraping it.
        self.assertIn("blackbox_http_jobs", open(
            os.path.join(ROOT, "roles/prometheus/templates/prometheus.yml.j2"), encoding="utf-8").read())
        jobs.add("calls_offloader")
        self.assertEqual(sorted(set(mods) - jobs), [])
        self.assertTrue({"prometheus", "loki", "grafana"} <= set(mods))

    def test_install_order_covers_every_component(self):
        self.assertEqual(sorted(PROFILES["mmd_component_order"]), sorted(CATALOGUE))

    def test_every_component_has_a_role_with_verify_and_uninstall(self):
        for component in CATALOGUE:
            for part in ("tasks/main.yml", "tasks/verify.yml", "tasks/uninstall.yml", "meta/main.yml"):
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


class StartWait(unittest.TestCase):
    """The answer file sets how long the install waits for Mattermost to start."""

    def wait_seconds(self, answers):
        tasks = load("roles/mattermost/tasks/main.yml")
        wait = next(t for t in tasks if t["name"].startswith("Wait for Mattermost to answer"))
        values = dict(load("inventory/group_vars/all/defaults.yml"), **answers)
        retries = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(wait["retries"]).render(values)
        return int(retries) * wait["delay"]

    def test_default_is_a_minute(self):
        self.assertEqual(self.wait_seconds({}), 60)

    def test_answer_raises_it(self):
        self.assertEqual(self.wait_seconds({"mattermost_start_wait_seconds": 600}), 600)
