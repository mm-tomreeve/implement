"""Checks the wizard's reference data against the repo it describes.

wizard/index.html carries its data inline (it can't fetch() a sibling file under
file://), so this is where that copy is held to account: profiles against
profiles.yml, sizing against catalogue.yml, port numbers against defaults.yml.
The wizard's own recommendation logic is client-side JS with nothing to run it in
CI, so this guards the data it reads rather than the code that reads it. The
derivation that actually has to be right lives in tools/estate_plan.py and is
tested in tests/test_estate_plan.py.
"""

import json
import os
import re
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INDEX_HTML = os.path.join(ROOT, "wizard", "index.html")
PROFILES = os.path.join(ROOT, "inventory", "group_vars", "all", "profiles.yml")
CATALOGUE = os.path.join(ROOT, "inventory", "group_vars", "all", "catalogue.yml")
DEFAULTS = os.path.join(ROOT, "inventory", "group_vars", "all", "defaults.yml")

# Which setting in defaults.yml each port in the wizard's data comes from.
# postgresql_port is itself "{{ mattermost_db_port }}", so that's the one to read.
PORT_SOURCE = {
    ("postgresql", 5432): "mattermost_db_port",
    ("mattermost", 8067): "mattermost_metrics_port",
    ("mattermost", 8074): "mattermost_cluster_port",
    ("mattermost", 8443): "mattermost_calls_port",
    ("rtcd", 8045): "rtcd_api_port",
    ("rtcd", 8443): "rtcd_rtc_port",
    ("opensearch", 9200): "opensearch_port",
    ("nfs", 2049): "nfs_port",
    ("prometheus", 9090): "prometheus_port",
    ("grafana", 3000): "grafana_port",
    ("loki", 3100): "loki_port",
    ("node_exporter", 9100): "node_exporter_port",
    ("postgres_exporter", 9187): "postgres_exporter_port",
    ("nginx_exporter", 9113): "nginx_exporter_port",
    ("opensearch_exporter", 9114): "opensearch_exporter_port",
    ("otel_collector", 8888): "otel_collector_metrics_port",
}
# Nginx listens on the web's own ports, which aren't settings.
LITERAL_PORTS = {("nginx", 80), ("nginx", 443)}


def load_embedded_data():
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    match = re.search(r'<script type="application/json" id="wizard-data">(.*?)</script>', html, re.DOTALL)
    if match is None:
        raise AssertionError("wizard/index.html has no #wizard-data script tag")
    return json.loads(match.group(1))


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class WizardData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_embedded_data()
        cls.profiles = load_yaml(PROFILES)["mmd_profiles"]
        cls.catalogue = load_yaml(CATALOGUE)["mmd_catalogue"]
        cls.defaults = load_yaml(DEFAULTS)

    # ------------------------------------------------------------ profiles

    def test_profile_components_match_profiles_yml(self):
        wizard = self.data["profile_components"]
        self.assertEqual(set(wizard), set(self.profiles))
        for name, components in wizard.items():
            self.assertEqual(components, self.profiles[name]["components"], name)

    def test_bands_only_use_real_profiles(self):
        for band in self.data["bands"]:
            for node in band["nodes"]:
                self.assertIn(node["role"], self.profiles, band["label"])

    def test_auto_components_are_real_components(self):
        auto = self.data["auto_components"]
        names = list(auto["alongside"]) + list(auto["alongside"].values())
        names += auto["every_monitored_server"] + auto["monitoring_server"]
        for name in names:
            self.assertIn(name, self.catalogue, name)
            self.assertIn(name, self.data["components"], name)

    # ------------------------------------------------------------ sizing

    def test_every_component_is_in_the_catalogue(self):
        for name in self.data["components"]:
            self.assertIn(name, self.catalogue, name)

    # Sizing is per seat band, not per component: a component's floor in the
    # catalogue is what it takes to start, which is the same at 200 seats and
    # 20,000 and no use to anyone buying servers.
    def test_every_band_sizes_every_profile(self):
        for band in self.data["bands"]:
            for profile in self.profiles:
                if profile == "all-in-one":
                    continue
                with self.subTest(band=band["label"], profile=profile):
                    self.assertIn(profile, band["sizing"])

    def test_sizing_entries_are_sane(self):
        for band in self.data["bands"]:
            for role, spec in band["sizing"].items():
                with self.subTest(band=band["label"], role=role):
                    self.assertGreaterEqual(spec["vcpu"], 1)
                    self.assertGreaterEqual(spec["ram_gb"], 2)
                    if spec["disk_gb"] is not None:
                        self.assertGreaterEqual(spec["disk_gb"], 20)

    def test_sizing_never_shrinks_as_seats_grow(self):
        for role in ("app", "database", "search"):
            previous = (0, 0)
            for band in self.data["bands"]:
                spec = band["sizing"][role]
                current = (spec["vcpu"], spec["ram_gb"])
                with self.subTest(band=band["label"], role=role):
                    self.assertGreaterEqual(current[0], previous[0])
                    self.assertGreaterEqual(current[1], previous[1])
                previous = current

    def test_every_profile_component_has_a_spec(self):
        for name, profile in self.profiles.items():
            for component in profile["components"]:
                self.assertIn(component, self.data["components"], f"{name} -> {component}")

    # ------------------------------------------------------------ ports

    def test_ports_match_defaults_yml(self):
        for name, spec in self.data["components"].items():
            for listen in spec["listens"]:
                key = (name, listen["port"])
                with self.subTest(component=name, port=listen["port"]):
                    if key in LITERAL_PORTS:
                        continue
                    self.assertIn(key, PORT_SOURCE, "no defaults.yml setting mapped for this port")
                    self.assertEqual(listen["port"], self.defaults[PORT_SOURCE[key]])

    def test_listen_sources_are_known(self):
        known = {"internet", "admins", "app", "app_peers", "monitoring", "db_replica", "estate"}
        for name, spec in self.data["components"].items():
            for listen in spec["listens"]:
                self.assertIn(listen["from"], known, name)

    # ------------------------------------------------------------ bands

    def test_bands_are_ordered_and_open_ended_last(self):
        bands = self.data["bands"]
        self.assertTrue(bands)
        capped = [b["max_seats"] for b in bands if b["max_seats"] is not None]
        self.assertEqual(capped, sorted(capped))
        self.assertTrue(all(b["max_seats"] is not None for b in bands[:-1]))

    def test_replica_flag_only_on_database(self):
        for band in self.data["bands"]:
            for node in band["nodes"]:
                if node.get("replica"):
                    self.assertEqual(node["role"], "database", band["label"])

    # Every band says which Mattermost reference architecture it came from, so
    # the figures in the wizard can always be traced back to a published one.
    def test_every_band_names_its_reference(self):
        for band in self.data["bands"]:
            with self.subTest(band=band["label"]):
                self.assertTrue(band["reference"].startswith("Mattermost's"))
                self.assertIsInstance(band["ha_required"], bool)
                self.assertIsInstance(band["search_nodes"], int)

    # Mattermost marks high availability as required from its 2,000-user tier up.
    def test_ha_is_required_above_the_smallest_band(self):
        for band in self.data["bands"]:
            with self.subTest(band=band["label"]):
                smallest = band["max_seats"] is not None and band["max_seats"] <= 200
                self.assertEqual(band["ha_required"], not smallest)

    # ------------------------------------------------------------ outbound

    def test_outbound_components_are_known(self):
        known = set(self.data["components"]) | {"calls", None}
        spec = self.data["outbound"]
        for group in ("runtime", "install"):
            for entry in spec[group]:
                with self.subTest(group=group, to=entry["to"]):
                    self.assertIn(entry["component"], known)
                    self.assertIn(entry["proto"], {"tcp", "udp", "tcp and udp"})
                    self.assertTrue(entry["purpose"])


if __name__ == "__main__":
    unittest.main()
