"""Tests the answer file check in roles/common/filter_plugins/answers.py.

Needs PyYAML (it comes with ansible-core).
"""

import glob
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "roles", "common", "filter_plugins"))

from answers import answer_problems  # noqa: E402

DEFAULTS = os.path.join(ROOT, "inventory", "group_vars", "all", "defaults.yml")


class AnswerProblems(unittest.TestCase):
    def check(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.remove, f.name)
        return answer_problems(f.name, DEFAULTS)

    def test_shipped_answer_files_are_clean(self):
        files = [os.path.join(ROOT, "answers.example.yml"), os.path.join(ROOT, "tests", "smoke", "answers-core.yml")]
        files += glob.glob(os.path.join(ROOT, "tests", "e2e", "answers", "*.yml"))
        for path in files:
            with self.subTest(path=os.path.relpath(path, ROOT)):
                self.assertEqual(answer_problems(path, DEFAULTS), [])

    def test_typo_suggests_the_setting(self):
        self.assertEqual(
            self.check("postgres_allowed_clients: [10.0.0.5]\n"),
            ["postgres_allowed_clients isn't a setting. Did you mean postgresql_allowed_clients?"],
        )

    def test_internal_fact_is_not_a_setting(self):
        self.assertEqual(self.check("mmd_selected: [nginx]\n"), ["mmd_selected isn't a setting."])

    def test_wrapper_setting_is_allowed(self):
        self.assertEqual(self.check("deploy_internet: 'no'\n"), [])

    def test_string_for_list(self):
        self.assertEqual(
            self.check("postgresql_allowed_clients: 10.0.0.5\n"),
            ["postgresql_allowed_clients should be a list, e.g. [a, b], not text."],
        )

    def test_profiles_may_be_a_string(self):
        self.assertEqual(self.check("deploy_profiles: app,calls\n"), [])

    def test_quoted_bool(self):
        self.assertEqual(self.check('firewall_manage: "yes"\n'), ["firewall_manage should be true or false, not text."])

    def test_quoted_number(self):
        self.assertEqual(
            self.check('mattermost_db_port: "5432"\n'),
            ["mattermost_db_port should be a whole number, not text."],
        )

    def test_bool_is_not_a_number(self):
        self.assertEqual(
            self.check("mattermost_db_port: true\n"),
            ["mattermost_db_port should be a whole number, not true or false."],
        )

    def test_auto_or_number_settings_take_either(self):
        self.assertEqual(self.check("postgresql_max_connections: 300\npostgresql_ssl: true\n"), [])

    def test_list_for_text(self):
        self.assertEqual(
            self.check("mattermost_db_host: [db01, db02]\n"),
            ["mattermost_db_host should be text, not a list, e.g. [a, b]."],
        )

    def test_empty_value(self):
        self.assertEqual(
            self.check("artifact_source:\n"),
            ['artifact_source is empty. Write "" or [], or remove the line.'],
        )

    def test_empty_file(self):
        self.assertEqual(self.check(""), [])

    def test_not_a_mapping(self):
        self.assertEqual(
            self.check("- deploy_name: x\n"),
            ["The answer file should be a list of 'setting: value' lines."],
        )


if __name__ == "__main__":
    unittest.main()
