"""Unit tests for tools/observability.py and the alert catalogue."""

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import observability as obs  # noqa: E402
from alert_catalogue import CATALOGUE, SEVERITIES  # noqa: E402

FULL = {
    "deployment": "test",
    "site_url": "https://chat.example.com",
    "servers": [
        {"name": "app01", "address": "10.0.0.2", "components": ["mattermost", "nginx"]},
        {"name": "db01", "address": "10.0.0.1", "components": ["postgresql"]},
        {"name": "calls01", "address": "10.0.0.3", "components": ["rtcd"]},
        {"name": "search01", "address": "10.0.0.4", "components": ["opensearch"]},
        {"name": "mon01", "address": "127.0.0.1", "components": ["prometheus", "grafana"]},
    ],
}


def rules_of(spec):
    doc, _ = obs.build_alerts(spec)
    return [r for g in doc["groups"] for r in g["rules"]]


class Catalogue(unittest.TestCase):
    def test_ids_unique_and_modules_known(self):
        ids = [r["id"] for r in CATALOGUE]
        self.assertEqual(len(ids), len(set(ids)))
        for r in CATALOGUE:
            self.assertIn(r["module"], obs.MODULE_NAMES, r["id"])
            self.assertIn(r["direction"], ("above", "below"), r["id"])
            self.assertTrue(r["tiers"], r["id"])
            self.assertTrue(set(r["tiers"]) <= set(SEVERITIES), r["id"])
            self.assertRegex(r["for"], r"^\d+[smh]$", r["id"])

    def test_tiers_get_worse_in_the_right_direction(self):
        for r in CATALOGUE:
            values = [r["tiers"][t] for t in SEVERITIES if t in r["tiers"]]
            expected = sorted(values) if r["direction"] == "above" else sorted(values, reverse=True)
            self.assertEqual(values, expected, r["id"])

    def test_queries_have_no_threshold_baked_in(self):
        # Thresholds belong in Grafana so they show in the UI and can be overridden.
        for r in CATALOGUE:
            self.assertIsNone(re.search(r"\)\s*(>|<|==)\s*[\d.]+\s*$", r["expr"]), r["id"])

    def test_readme_matches_catalogue(self):
        with open(os.path.join(HERE, "..", "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        block = readme.split("tests fail if this drifts) -->\n", 1)[1].split("<!-- alerts:end -->", 1)[0]
        self.assertEqual(block.strip(), obs.catalogue_markdown("####", " alerts").strip(),
                         "README alert tables are stale: regenerate them from tools/observability.py")

    def test_docs_match_catalogue(self):
        with open(os.path.join(HERE, "..", "docs", "alerts.md"), encoding="utf-8") as handle:
            doc = handle.read()
        generated = doc.split("<!-- generated: python3 tools/observability.py catalogue -->\n", 1)[1]
        self.assertEqual(generated, obs.catalogue_markdown(),
                         "docs/alerts.md is stale: regenerate the table with tools/observability.py catalogue")


class Tiers(unittest.TestCase):
    def test_above_bands_do_not_overlap(self):
        ev = obs.evaluators("above", {"info": 70, "warning": 80, "critical": 95})
        self.assertEqual(ev["info"], {"type": "within_range", "params": [70, 80]})
        self.assertEqual(ev["warning"], {"type": "within_range", "params": [80, 95]})
        self.assertEqual(ev["critical"], {"type": "gt", "params": [95]})

    def test_below_bands_mirror(self):
        ev = obs.evaluators("below", {"info": 30, "warning": 14, "critical": 7})
        self.assertEqual(ev["info"], {"type": "within_range", "params": [14, 30]})
        self.assertEqual(ev["warning"], {"type": "within_range", "params": [7, 14]})
        self.assertEqual(ev["critical"], {"type": "lt", "params": [7]})

    def test_single_tier(self):
        self.assertEqual(obs.evaluators("below", {"critical": 1}), {"critical": {"type": "lt", "params": [1]}})


class Building(unittest.TestCase):
    def test_only_modules_in_the_estate(self):
        db_only = {"servers": [{"name": "db01", "address": "x", "components": ["postgresql"]}]}
        modules = {r["labels"]["module"] for r in rules_of(db_only)}
        self.assertEqual(modules, {"host", "postgresql"})

    def test_full_estate_covers_every_module(self):
        modules = {r["labels"]["module"] for r in rules_of(FULL)}
        self.assertEqual(modules, set(obs.MODULE_NAMES))

    def test_overrides_and_disabling(self):
        spec = dict(FULL, overrides={"disk-used": {"tiers": {"warning": 85, "info": None}, "for": "30m"}},
                    disabled=["host-rebooted"])
        rules = rules_of(spec)
        disk = {r["labels"]["severity"]: r for r in rules if r["labels"]["mmi_rule"] == "disk-used"}
        self.assertEqual(set(disk), {"warning", "critical"})
        self.assertEqual(disk["warning"]["data"][1]["model"]["conditions"][0]["evaluator"]["params"], [85, 95])
        self.assertEqual(disk["warning"]["for"], "30m")
        self.assertFalse([r for r in rules if r["labels"]["mmi_rule"] == "host-rebooted"])

    def test_rule_shape(self):
        rules = rules_of(FULL)
        uids = [r["uid"] for r in rules]
        titles = [r["title"] for r in rules]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertEqual(len(titles), len(set(titles)))
        for r in rules:
            self.assertLessEqual(len(r["uid"]), 40)
            self.assertIn(r["labels"]["severity"], SEVERITIES)
            self.assertEqual(r["condition"], "C")
            self.assertNotIn("%(", r["data"][0]["model"]["expr"])

    def test_dashboards_are_valid_grids(self):
        result = obs.build(FULL)
        self.assertEqual(len(result["summary"]["dashboards"]), 7)
        for name, content in result["files"].items():
            data = json.loads(content)
            if not name.startswith("dashboards/"):
                continue
            ids = [p["id"] for p in data["panels"]]
            self.assertEqual(len(ids), len(set(ids)), name)
            for panel in data["panels"]:
                grid = panel["gridPos"]
                self.assertLessEqual(grid["x"] + grid["w"], 24, (name, panel.get("title")))

    def test_summary_counts_match(self):
        result = obs.build(FULL)
        doc = json.loads(result["files"]["alerting/mattermost-implement.json"])
        total = sum(len(g["rules"]) for g in doc["groups"])
        self.assertEqual(total, result["summary"]["rules"])
        self.assertEqual(total, sum(result["summary"]["by_severity"].values()))


if __name__ == "__main__":
    unittest.main()
