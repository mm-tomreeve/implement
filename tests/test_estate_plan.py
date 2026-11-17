"""Tests tools/estate_plan.py: turning one estate plan into a server's answers.

Needs PyYAML (it comes with ansible-core).
"""

import copy
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "roles", "common", "filter_plugins"))

import yaml  # noqa: E402

import estate_plan as ep  # noqa: E402
from answers import answer_problems  # noqa: E402

DEFAULTS = os.path.join(ROOT, "inventory", "group_vars", "all", "defaults.yml")
PROFILES = os.path.join(ROOT, "inventory", "group_vars", "all", "profiles.yml")
EXAMPLE = os.path.join(ROOT, "estate.example.yml")

# The split estate from README.md's "Example: a split estate", reproduced as a
# plan. Same topology as tests/e2e/answers/{db01,app01,calls01,search01,mon01}.yml.
SPLIT_ESTATE = {
    "estate_version": 1,
    "name": "chat-prod",
    "mattermost_edition": "enterprise",
    "mattermost_site_url": "https://chat.acme.example",
    "tls_mode": "provided",
    "file_storage": "local",
    "observability": "managed",
    "nodes": [
        {"name": "db01", "address": "10.10.1.11", "profiles": ["database"]},
        {"name": "app01", "address": "10.10.1.12", "profiles": ["app"]},
        {"name": "calls01", "address": "10.10.1.13", "profiles": ["calls"]},
        {"name": "search01", "address": "10.10.1.14", "profiles": ["search"]},
        {"name": "mon01", "address": "10.10.1.15", "profiles": ["monitoring"]},
    ],
}


class ProfileComponentsStayInStep(unittest.TestCase):
    def test_matches_profiles_yml(self):
        with open(PROFILES, encoding="utf-8") as f:
            profiles = yaml.safe_load(f)["mmd_profiles"]
        for name, components in ep.PROFILE_COMPONENTS.items():
            self.assertEqual(components, profiles[name]["components"], name)
        self.assertEqual(set(ep.PROFILE_COMPONENTS), set(profiles))


class EncodeDecode(unittest.TestCase):
    def test_round_trip(self):
        text = yaml.safe_dump(SPLIT_ESTATE)
        blob = ep.encode(text)
        self.assertTrue(blob.startswith(ep.ENCODING_PREFIX))
        self.assertEqual(ep.decode(blob), SPLIT_ESTATE)

    def test_plain_yaml_is_accepted_too(self):
        text = yaml.safe_dump(SPLIT_ESTATE)
        self.assertEqual(ep.decode(text), SPLIT_ESTATE)

    def test_corrupt_blob(self):
        with self.assertRaises(ep.EstatePlanError):
            ep.decode(ep.ENCODING_PREFIX + "not-base64!!!")

    def test_shipped_example_decodes_clean(self):
        with open(EXAMPLE, encoding="utf-8") as f:
            plan = ep.decode(f.read())
        self.assertEqual(ep.plan_problems(plan), [])


class PlanProblems(unittest.TestCase):
    def test_wrong_version(self):
        plan = copy.deepcopy(SPLIT_ESTATE)
        plan["estate_version"] = 2
        self.assertIn("estate_version should be 1.", ep.plan_problems(plan))

    def test_duplicate_name_case_insensitive(self):
        plan = copy.deepcopy(SPLIT_ESTATE)
        plan["nodes"].append({"name": "DB01", "address": "10.10.1.99", "profiles": ["database"]})
        self.assertTrue(any("both called" in p for p in ep.plan_problems(plan)))

    def test_unknown_profile(self):
        plan = copy.deepcopy(SPLIT_ESTATE)
        plan["nodes"][0]["profiles"] = ["not-a-profile"]
        self.assertTrue(any("isn't a profile" in p for p in ep.plan_problems(plan)))

    def test_two_nodes_on_one_address(self):
        plan = copy.deepcopy(SPLIT_ESTATE)
        plan["nodes"][1]["address"] = plan["nodes"][0]["address"]
        self.assertTrue(any("share the address" in p for p in ep.plan_problems(plan)))

    def test_empty_nodes(self):
        plan = copy.deepcopy(SPLIT_ESTATE)
        plan["nodes"] = []
        self.assertTrue(any("non-empty list" in p for p in ep.plan_problems(plan)))


class SelectNode(unittest.TestCase):
    def test_matches_by_address(self):
        node = ep.select_node(SPLIT_ESTATE, local_addresses=["10.10.1.12"])
        self.assertEqual(node["name"], "app01")

    def test_matches_by_name(self):
        node = ep.select_node(SPLIT_ESTATE, local_names=["calls01"])
        self.assertEqual(node["name"], "calls01")

    def test_no_match(self):
        with self.assertRaises(ep.NoMatchingNode) as ctx:
            ep.select_node(SPLIT_ESTATE, local_addresses=["10.10.1.254"])
        self.assertEqual(set(ctx.exception.names), {n["name"] for n in SPLIT_ESTATE["nodes"]})

    def test_ambiguous(self):
        plan = copy.deepcopy(SPLIT_ESTATE)
        plan["nodes"].append({"name": "search02", "address": "10.10.1.12", "profiles": ["search"]})
        with self.assertRaises(ep.AmbiguousNode) as ctx:
            ep.select_node(plan, local_addresses=["10.10.1.12"])
        self.assertEqual(set(ctx.exception.names), {"app01", "search02"})

    def test_explicit_override_wins(self):
        node = ep.select_node(SPLIT_ESTATE, node_name="db01", local_addresses=["10.10.1.12"])
        self.assertEqual(node["name"], "db01")

    def test_explicit_override_unknown_name(self):
        with self.assertRaises(ep.EstatePlanError):
            ep.select_node(SPLIT_ESTATE, node_name="nope")


class DeriveSplitEstate(unittest.TestCase):
    """Reproduces README.md's "Example: a split estate" field-for-field, for
    every setting the plan can and should supply. Secrets (mattermost_db_password,
    mattermost_opensearch_password) and local file paths (mattermost_opensearch_ca_file,
    tls_source_dir) are intentionally never derived - README shows them as
    "<copied from secrets.yml on ...>" or a local path, so they're asserted absent
    here and present as a followup instead.
    """

    def test_db01(self):
        answers, _followups, _notes = ep.derive_answers(SPLIT_ESTATE, "db01")
        self.assertEqual(answers, {
            "deploy_name": "chat-prod",
            "deploy_profiles": ["database"],
            "postgresql_allowed_clients": ["10.10.1.12"],
            "deploy_monitoring_exporters": True,
            "monitoring_scrape_sources": ["10.10.1.15"],
            "logging_loki_url": "http://10.10.1.15:3100",
        })

    def test_app01(self):
        answers, followups, _notes = ep.derive_answers(SPLIT_ESTATE, "app01")
        self.assertEqual(answers, {
            "deploy_name": "chat-prod",
            "deploy_profiles": ["app"],
            "mattermost_site_url": "https://chat.acme.example",
            "mattermost_edition": "enterprise",
            "mattermost_db_host": "10.10.1.11",
            "mattermost_rtcd_url": "http://10.10.1.13:8045",
            "mattermost_opensearch_url": "https://10.10.1.14:9200",
            "tls_mode": "provided",
            "deploy_monitoring_exporters": True,
            "monitoring_scrape_sources": ["10.10.1.15"],
            "logging_loki_url": "http://10.10.1.15:3100",
        })
        for key in ("mattermost_db_password", "mattermost_opensearch_password", "mattermost_opensearch_ca_file",
                    "tls_source_dir", "tls_cert_file", "tls_key_file"):
            self.assertNotIn(key, answers)
        self.assertTrue(any("db01" in f and "secrets.yml" in f for f in followups))
        self.assertTrue(any("search01" in f and "secrets.yml" in f for f in followups))
        self.assertTrue(any("root-ca.pem" in f for f in followups))
        self.assertTrue(any("certificate and key" in f for f in followups))

    def test_app_servers_list_each_other_as_cluster_peers(self):
        plan = dict(SPLIT_ESTATE)
        plan["nodes"] = list(SPLIT_ESTATE["nodes"]) + [
            {"name": "app02", "address": "10.10.1.16", "profiles": ["app"]}]
        a1, f1, _notes = ep.derive_answers(plan, "app01")
        a2, _, _notes = ep.derive_answers(plan, "app02")
        self.assertEqual(a1["mattermost_cluster_peers"], ["10.10.1.16"])
        self.assertEqual(a2["mattermost_cluster_peers"], ["10.10.1.12"])
        self.assertTrue(any("mattermost_license_file" in f for f in f1))

    def test_single_app_server_has_no_peers(self):
        answers, _, _notes = ep.derive_answers(SPLIT_ESTATE, "app01")
        self.assertNotIn("mattermost_cluster_peers", answers)

    def test_calls01(self):
        answers, _followups, _notes = ep.derive_answers(SPLIT_ESTATE, "calls01")
        self.assertEqual(answers, {
            "deploy_name": "chat-prod",
            "deploy_profiles": ["calls"],
            "rtcd_api_sources": ["10.10.1.12"],
            "deploy_monitoring_exporters": True,
            "monitoring_scrape_sources": ["10.10.1.15"],
            "logging_loki_url": "http://10.10.1.15:3100",
        })

    def test_search01(self):
        answers, _followups, _notes = ep.derive_answers(SPLIT_ESTATE, "search01")
        self.assertEqual(answers, {
            "deploy_name": "chat-prod",
            "deploy_profiles": ["search"],
            "opensearch_allowed_clients": ["10.10.1.12"],
            "deploy_monitoring_exporters": True,
            "monitoring_scrape_sources": ["10.10.1.15"],
            "logging_loki_url": "http://10.10.1.15:3100",
        })

    def test_mon01(self):
        answers, _followups, _notes = ep.derive_answers(SPLIT_ESTATE, "mon01")
        self.assertEqual(answers, {
            "deploy_name": "chat-prod",
            "deploy_profiles": ["monitoring"],
            "monitoring_site_url": "https://chat.acme.example",
            "monitoring_estate": [
                {"name": "db01", "address": "10.10.1.11", "components": ["postgresql"]},
                {"name": "app01", "address": "10.10.1.12", "components": ["mattermost", "nginx"]},
                {"name": "calls01", "address": "10.10.1.13", "components": ["rtcd"]},
                {"name": "search01", "address": "10.10.1.14", "components": ["opensearch"]},
            ],
        })

    def test_every_node_passes_answer_problems(self):
        for node in SPLIT_ESTATE["nodes"]:
            with self.subTest(node=node["name"]):
                answers, _followups, _notes = ep.derive_answers(SPLIT_ESTATE, node["name"])
                text = "---\n" + yaml.safe_dump(answers, sort_keys=False)
                with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
                    f.write(text)
                self.addCleanup(os.remove, f.name)
                self.assertEqual(answer_problems(f.name, DEFAULTS), [])


class DeriveReadReplica(unittest.TestCase):
    """README.md's "Example: a database with a read replica"."""

    PLAN = {
        "estate_version": 1,
        "name": "chat-prod",
        "mattermost_edition": "enterprise",
        "mattermost_site_url": "https://chat.acme.example",
        "tls_mode": "later",
        "file_storage": "local",
        "observability": "external",
        "monitoring_scrape_sources": [],
        "logging_loki_url": "",
        "nodes": [
            {"name": "db01", "address": "10.10.1.11", "profiles": ["database"]},
            {"name": "db02", "address": "10.10.1.21", "profiles": ["database"], "primary": "db01"},
            {"name": "app01", "address": "10.10.1.12", "profiles": ["app"]},
        ],
    }

    def test_primary(self):
        answers, _followups, _notes = ep.derive_answers(self.PLAN, "db01")
        self.assertEqual(answers["postgresql_replication"], "primary")
        self.assertEqual(answers["postgresql_replica_hosts"], ["10.10.1.21"])
        self.assertEqual(answers["postgresql_allowed_clients"], ["10.10.1.12"])

    def test_replica(self):
        answers, followups, _notes = ep.derive_answers(self.PLAN, "db02")
        self.assertEqual(answers["postgresql_replication"], "replica")
        self.assertEqual(answers["postgresql_primary_host"], "10.10.1.11")
        self.assertEqual(answers["postgresql_allowed_clients"], ["10.10.1.12"])
        self.assertTrue(any("db01" in f and "secrets.yml" in f for f in followups))

    def test_app_reads_from_the_replica(self):
        # Building the replica isn't enough: Mattermost only sends reads to it
        # once the app server names it in SqlSettings.DataSourceReplicas.
        answers, _followups, _notes = ep.derive_answers(self.PLAN, "app01")
        self.assertEqual(answers["mattermost_db_host"], "10.10.1.11")
        self.assertEqual(answers["mattermost_db_replica_hosts"], ["10.10.1.21"])

    # The plan is a list, and nothing says the primary comes first in it.
    def test_the_primary_is_found_wherever_it_sits_in_the_list(self):
        plan = dict(self.PLAN, nodes=list(reversed(self.PLAN["nodes"])))
        answers, _followups, _notes = ep.derive_answers(plan, "app01")
        self.assertEqual(answers["mattermost_db_host"], "10.10.1.11")
        self.assertEqual(answers["mattermost_db_replica_hosts"], ["10.10.1.21"])

    def test_reads_only_go_to_the_replica_on_enterprise(self):
        plan = dict(self.PLAN, licence="professional")
        _answers, _followups, notes = ep.derive_answers(plan, "app01")
        self.assertTrue(any("Enterprise" in n and "replica" in n for n in notes))

    def test_a_plan_with_no_primary_is_refused(self):
        plan = dict(self.PLAN, nodes=[
            {"name": "db02", "address": "10.10.1.21", "profiles": ["database"], "primary": "db01"},
            {"name": "app01", "address": "10.10.1.12", "profiles": ["app"]},
        ])
        problems = ep.plan_problems(plan)
        self.assertTrue(any("primary" in p for p in problems), problems)


class DeriveCalls(unittest.TestCase):
    """Calls runs inside the app servers up to about 50 people, on rtcd past that."""

    def plan(self, calls, calls_nodes=0):
        nodes = [
            {"name": "db01", "address": "10.10.1.11", "profiles": ["database"]},
            {"name": "app01", "address": "10.10.1.12", "profiles": ["app"]},
        ]
        for i in range(calls_nodes):
            nodes.append({"name": "calls%02d" % (i + 1), "address": "10.10.2.%d" % (i + 1),
                          "profiles": ["calls"]})
        return {
            "estate_version": 1, "name": "chat", "mattermost_edition": "enterprise",
            "mattermost_site_url": "https://chat.example", "tls_mode": "later",
            "file_storage": "local", "observability": "external", "calls": calls, "nodes": nodes,
        }

    def test_not_using_calls_switches_it_off(self):
        answers, _, _notes = ep.derive_answers(self.plan("none"), "app01")
        self.assertIs(answers["mattermost_calls_integrated"], False)
        self.assertNotIn("mattermost_rtcd_url", answers)

    def test_integrated_leaves_it_on_and_warns_about_the_port(self):
        answers, _followups, notes = ep.derive_answers(self.plan("integrated"), "app01")
        self.assertNotIn("mattermost_calls_integrated", answers)   # the default is already on
        self.assertNotIn("mattermost_rtcd_url", answers)
        self.assertTrue(any("8443" in n and "proxy" in n for n in notes))

    def test_rtcd_points_the_app_server_at_it(self):
        answers, _, _notes = ep.derive_answers(self.plan("rtcd", calls_nodes=1), "app01")
        self.assertEqual(answers["mattermost_rtcd_url"], "http://10.10.2.1:8045")
        self.assertNotIn("mattermost_calls_integrated", answers)

    def test_more_than_one_rtcd_says_it_needs_a_load_balancer(self):
        _, followups, _notes = ep.derive_answers(self.plan("rtcd", calls_nodes=2), "app01")
        self.assertTrue(any("load balancer" in f for f in followups))

    def test_the_rtcd_server_allows_the_app_servers_in(self):
        answers, _, _notes = ep.derive_answers(self.plan("rtcd", calls_nodes=1), "calls01")
        self.assertEqual(answers["rtcd_api_sources"], ["10.10.1.12"])

    def test_derived_answers_pass_answer_problems(self):
        for calls, count in (("none", 0), ("integrated", 0), ("rtcd", 1)):
            plan = self.plan(calls, count)
            for node in plan["nodes"]:
                with self.subTest(calls=calls, node=node["name"]):
                    answers, _, _notes = ep.derive_answers(plan, node["name"])
                    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
                        f.write("---\n" + yaml.safe_dump(answers, sort_keys=False))
                    self.addCleanup(os.remove, f.name)
                    self.assertEqual(answer_problems(f.name, DEFAULTS), [])


class DeriveSplitMonitoring(unittest.TestCase):
    """Prometheus and Loki on one box, Grafana on another."""

    PLAN = {
        "estate_version": 1,
        "name": "chat-prod",
        "mattermost_edition": "enterprise",
        "mattermost_site_url": "https://chat.acme.example",
        "tls_mode": "later",
        "file_storage": "local",
        "observability": "managed",
        "nodes": [
            {"name": "db01", "address": "10.10.1.11", "profiles": ["database"]},
            {"name": "app01", "address": "10.10.1.12", "profiles": ["app"]},
            {"name": "metrics01", "address": "10.10.1.15", "profiles": ["metrics"]},
            {"name": "graf01", "address": "10.10.1.16", "profiles": ["dashboards"]},
        ],
    }

    def test_metrics_node_holds_the_estate(self):
        answers, _, _notes = ep.derive_answers(self.PLAN, "metrics01")
        self.assertEqual(answers["deploy_profiles"], ["metrics"])
        self.assertEqual(answers["monitoring_site_url"], "https://chat.acme.example")
        self.assertEqual([n["name"] for n in answers["monitoring_estate"]], ["db01", "app01", "graf01"])
        # Prometheus and Loki are both local, so it scrapes and stores for itself.
        self.assertNotIn("monitoring_scrape_sources", answers)
        self.assertNotIn("logging_loki_url", answers)

    def test_dashboards_node_points_at_the_metrics_node(self):
        answers, _, _notes = ep.derive_answers(self.PLAN, "graf01")
        self.assertEqual(answers["grafana_prometheus_url"], "http://10.10.1.15:9090")
        # Grafana needs the same estate list to build its alerts and dashboards.
        self.assertEqual([n["name"] for n in answers["monitoring_estate"]], ["db01", "app01", "metrics01"])
        # It is a server like any other, so it is scraped and ships its logs.
        self.assertEqual(answers["monitoring_scrape_sources"], ["10.10.1.15"])
        self.assertEqual(answers["logging_loki_url"], "http://10.10.1.15:3100")

    def test_other_nodes_use_the_metrics_node(self):
        answers, _, _notes = ep.derive_answers(self.PLAN, "app01")
        self.assertEqual(answers["monitoring_scrape_sources"], ["10.10.1.15"])
        self.assertEqual(answers["logging_loki_url"], "http://10.10.1.15:3100")
        self.assertNotIn("grafana_prometheus_url", answers)

    def test_derived_answers_pass_answer_problems(self):
        for node in self.PLAN["nodes"]:
            with self.subTest(node=node["name"]):
                answers, _, _notes = ep.derive_answers(self.PLAN, node["name"])
                with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
                    f.write("---\n" + yaml.safe_dump(answers, sort_keys=False))
                self.addCleanup(os.remove, f.name)
                self.assertEqual(answer_problems(f.name, DEFAULTS), [])


class EstateSecrets(unittest.TestCase):
    """Which passwords each node takes from an estate secrets file."""

    SECRETS = {
        "db_password": "DbPass",
        "replication_password": "ReplPass",
        "opensearch_mattermost_password": "SearchPass",
        "opensearch_admin_password": "AdminPass",
        "grafana_admin_password": "GrafPass",
    }

    def test_app_node_gets_the_passwords_it_connects_with(self):
        answers, followups, _notes = ep.derive_answers(SPLIT_ESTATE, "app01", self.SECRETS)
        self.assertEqual(answers["mattermost_db_password"], "DbPass")
        self.assertEqual(answers["mattermost_opensearch_password"], "SearchPass")
        # No point telling someone to copy what has already been filled in.
        self.assertFalse(any("secrets.yml" in f for f in followups))
        # The CA is a file, so that one stays.
        self.assertTrue(any("root-ca.pem" in f for f in followups))

    def test_search_node_gets_both_opensearch_passwords(self):
        answers, _, _notes = ep.derive_answers(SPLIT_ESTATE, "search01", self.SECRETS)
        self.assertEqual(answers["mattermost_opensearch_password"], "SearchPass")
        self.assertEqual(answers["opensearch_admin_password"], "AdminPass")

    def test_nodes_get_nothing_they_do_not_need(self):
        answers, _, _notes = ep.derive_answers(SPLIT_ESTATE, "calls01", self.SECRETS)
        for key in answers:
            self.assertNotIn("password", key, "the Calls server needs no estate password")

    def test_monitoring_node_gets_the_grafana_password(self):
        answers, _, _notes = ep.derive_answers(SPLIT_ESTATE, "mon01", self.SECRETS)
        self.assertEqual(answers["grafana_admin_password"], "GrafPass")

    def test_generated_secrets_match_the_roles_file(self):
        made = ep.generate_secrets()
        self.assertEqual(set(made), set(ep.SECRET_LENGTHS))
        for name, value in made.items():
            self.assertEqual(len(value), ep.SECRET_LENGTHS[name], name)
            self.assertTrue(value.isalnum(), name)

    def test_derived_answers_with_secrets_pass_answer_problems(self):
        for node in SPLIT_ESTATE["nodes"]:
            with self.subTest(node=node["name"]):
                answers, _, _notes = ep.derive_answers(SPLIT_ESTATE, node["name"], self.SECRETS)
                with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
                    f.write("---\n" + yaml.safe_dump(answers, sort_keys=False))
                self.addCleanup(os.remove, f.name)
                self.assertEqual(answer_problems(f.name, DEFAULTS), [])


class Preflight(unittest.TestCase):
    def test_app_node_checks_what_it_has_to_reach(self):
        checks = ep.checks_for_node(SPLIT_ESTATE, "app01")
        tcp = {(c["target"], c["port"]) for c in checks if c["kind"] == "tcp"}
        self.assertIn(("10.10.1.11", 5432), tcp)     # database
        self.assertIn(("10.10.1.14", 9200), tcp)     # search
        self.assertIn(("10.10.1.13", 8045), tcp)     # calls
        self.assertIn(("10.10.1.15", 3100), tcp)     # logs to Loki
        names = {c["target"] for c in checks if c["kind"] == "resolve"}
        self.assertEqual(names, {"10.10.1.11", "10.10.1.13", "10.10.1.14", "10.10.1.15"})

    def test_nothing_local_is_checked_over_the_network(self):
        single = {
            "estate_version": 1, "name": "small", "mattermost_edition": "enterprise",
            "mattermost_site_url": "https://chat.example", "tls_mode": "later",
            "file_storage": "local", "observability": "external",
            "nodes": [{"name": "core01", "address": "10.0.0.5", "profiles": ["core"]}],
        }
        checks = ep.checks_for_node(single, "core01")
        self.assertEqual([c for c in checks if c["kind"] == "tcp"], [])

    def test_requirements_come_from_the_catalogue(self):
        checks = ep.checks_for_node(SPLIT_ESTATE, "search01")
        ram = next(c for c in checks if c["kind"] == "ram")
        cpus = next(c for c in checks if c["kind"] == "cpus")
        catalogue = ep.catalogue()
        expected_ram = sum(catalogue[c]["min_ram_mb"]
                           for c in ep.installed_components(SPLIT_ESTATE, SPLIT_ESTATE["nodes"][3]))
        self.assertEqual(ram["target"], expected_ram)
        self.assertGreaterEqual(cpus["target"], 1)


class DeriveNfsStorage(unittest.TestCase):
    """README.md's "Example: shared file storage over NFS"."""

    PLAN = {
        "estate_version": 1,
        "name": "chat-prod",
        "mattermost_edition": "enterprise",
        "mattermost_site_url": "https://chat.acme.example",
        "tls_mode": "later",
        "file_storage": "nfs",
        "observability": "external",
        "monitoring_scrape_sources": [],
        "logging_loki_url": "",
        "nodes": [
            {"name": "files01", "address": "10.10.1.16", "profiles": ["storage"]},
            {"name": "app01", "address": "10.10.1.12", "profiles": ["app"]},
            {"name": "app02", "address": "10.10.1.22", "profiles": ["app"]},
        ],
    }

    def test_storage_node_gets_both_app_clients(self):
        answers, _followups, _notes = ep.derive_answers(self.PLAN, "files01")
        self.assertEqual(answers["nfs_allowed_clients"], ["10.10.1.12", "10.10.1.22"])

    def test_app_node_gets_mount_followup(self):
        _answers, followups, _notes = ep.derive_answers(self.PLAN, "app01")
        self.assertTrue(any("files01" in f and "Mount" in f for f in followups))


class DeriveColocatedComponents(unittest.TestCase):
    """A server that holds both ends of a connection is its own neighbour.

    The core profile is the small estate's version of what a lab does: PostgreSQL
    and Mattermost on one box, with other servers around it. Everything that
    exists to hand one server's details to another has to stop at that boundary,
    or the installer asks you to copy a password out of your own secrets.yml and
    opens PostgreSQL to the network to reach itself.
    """

    PLAN = {
        "estate_version": 1,
        "name": "chat-small",
        "mattermost_edition": "enterprise",
        "mattermost_site_url": "https://chat.acme.example",
        "tls_mode": "later",
        "file_storage": "local",
        "calls": "rtcd",
        "observability": "managed",
        "nodes": [
            {"name": "core01", "address": "10.10.1.11", "profiles": ["core", "search"]},
            {"name": "calls01", "address": "10.10.1.13", "profiles": ["calls"]},
            {"name": "mon01", "address": "10.10.1.15", "profiles": ["monitoring"]},
        ],
    }

    def test_its_own_database_is_not_a_followup(self):
        _answers, followups, _notes = ep.derive_answers(self.PLAN, "core01")
        self.assertFalse([f for f in followups if "secrets.yml" in f], followups)

    def test_its_own_database_is_reached_on_localhost(self):
        answers, _followups, _notes = ep.derive_answers(self.PLAN, "core01")
        self.assertNotIn("mattermost_db_host", answers)
        # Nothing connects from another server, so PostgreSQL has no reason to
        # listen anywhere but localhost. Naming itself here is what would make it.
        self.assertNotIn("postgresql_allowed_clients", answers)
        self.assertNotIn("opensearch_allowed_clients", answers)

    def test_components_on_other_servers_are_still_wired(self):
        answers, _followups, _notes = ep.derive_answers(self.PLAN, "core01")
        self.assertEqual(answers["mattermost_rtcd_url"], "http://10.10.1.13:8045")

    def test_the_other_servers_still_point_back(self):
        answers, _followups, _notes = ep.derive_answers(self.PLAN, "calls01")
        self.assertEqual(answers["rtcd_api_sources"], ["10.10.1.11"])


class DeriveLabServer(unittest.TestCase):
    """A lab plan from the wizard: every role the wizard was asked for, one box.

    The wizard writes several profiles on the one node rather than the all-in-one
    profile, so a lab that leaves OpenSearch or Calls out says so in the plan.
    Either way it is one server talking to itself, and that is what makes a lab
    different: there is no other server to point at, to open a port to, or to
    copy a password from, so the plan derives none of those and the install has
    nothing left to ask for.
    """

    PLAN = {
        "estate_version": 1,
        "name": "chat-lab",
        "deployment": "lab",
        "mattermost_edition": "enterprise",
        "mattermost_site_url": "https://lab.acme.example",
        "tls_mode": "later",
        "file_storage": "local",
        "calls": "rtcd",
        "recording": "own",
        "observability": "managed",
        "nodes": [
            {"name": "lab01", "address": "10.0.0.50",
             "profiles": ["core", "search", "calls", "jobs", "monitoring"]},
        ],
    }

    def test_the_plan_is_valid(self):
        self.assertEqual(ep.plan_problems(self.PLAN), [])

    def test_every_component_lands_on_the_one_node(self):
        components = ep.expand_components(self.PLAN["nodes"][0]["profiles"])
        for name in ("postgresql", "mattermost", "nginx", "opensearch", "rtcd",
                     "calls_offloader", "prometheus", "loki", "grafana"):
            self.assertIn(name, components)

    def test_all_in_one_is_the_same_set_of_components(self):
        self.assertEqual(sorted(ep.expand_components(self.PLAN["nodes"][0]["profiles"])),
                         sorted(ep.PROFILE_COMPONENTS["all-in-one"]))

    def test_nothing_is_wired_over_the_network(self):
        """Every address is left out, which is what makes roles/common/tasks/wiring.yml
        reach for 127.0.0.1. Writing the box's own public address instead would work
        too, but it would make PostgreSQL listen on every interface, turn SSL on for a
        connection that never leaves the machine, and open ports to itself."""
        answers, _followups, _notes = ep.derive_answers(self.PLAN, "lab01")
        for key in ("mattermost_db_host", "mattermost_rtcd_url", "mattermost_job_service_url",
                    "mattermost_opensearch_url", "postgresql_allowed_clients",
                    "opensearch_allowed_clients", "rtcd_api_sources",
                    "calls_offloader_api_sources"):
            self.assertNotIn(key, answers)
        # Nothing else to watch, so the monitoring server has an empty estate.
        self.assertEqual(answers["monitoring_estate"], [])

    def test_nothing_is_still_needed(self):
        """The point of the whole thing: a lab box installs from the plan alone.
        Every password is generated here by roles/common/tasks/secrets.yml, so there
        is no secrets file to make, carry or decrypt."""
        _answers, followups, _notes = ep.derive_answers(self.PLAN, "lab01")
        self.assertEqual(followups, [])

    def test_nothing_is_checked_over_the_network(self):
        checks = ep.checks_for_node(self.PLAN, "lab01")
        self.assertEqual([c for c in checks if c["kind"] in ("tcp", "resolve")], [])

    # The wizard shows this figure before anyone buys the box, by adding up the
    # same catalogue floors. If the two ever part company the wizard is promising
    # a server install.sh will then refuse to be happy with.
    def test_sizing_is_the_sum_of_the_catalogue_floors(self):
        checks = ep.checks_for_node(self.PLAN, "lab01")
        catalogue = ep.catalogue()
        installed = ep.installed_components(self.PLAN, self.PLAN["nodes"][0])
        ram = next(c for c in checks if c["kind"] == "ram")
        self.assertEqual(ram["target"], sum(catalogue[c]["min_ram_mb"] for c in installed))


if __name__ == "__main__":
    unittest.main()
