"""Unit tests for tools/observability.py and the alert catalogue."""

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import observability as obs  # noqa: E402
from alert_catalogue import CATALOGUE, LOG_CATALOGUE, SEVERITIES  # noqa: E402

FULL = {
    "deployment": "test",
    "site_url": "https://chat.example.com",
    "servers": [
        {"name": "app01", "address": "10.0.0.2", "components": ["mattermost", "nginx"]},
        {"name": "db01", "address": "10.0.0.1", "components": ["postgresql"]},
        {"name": "calls01", "address": "10.0.0.3", "components": ["rtcd"]},
        {"name": "search01", "address": "10.0.0.4", "components": ["opensearch"]},
        {"name": "files01", "address": "10.0.0.5", "components": ["nfs"]},
        {"name": "mon01", "address": "127.0.0.1", "components": ["prometheus", "grafana"]},
    ],
}
FULL_LOGS = dict(FULL, loki=True)


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
        self.assertEqual(len(result["summary"]["dashboards"]), 8)
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


def log_line_matches(rule, line):
    """What Loki does with the pipeline observability.py builds, in Python."""
    params = obs.log_params({})
    match = [(f, obs.filled(r, {})) for f, r in rule.get("match", [])]
    exclude = [(f, obs.filled(r, {})) for f, r in rule.get("exclude", [])]
    for field, regex in match:
        if field == "line" and not re.search(regex, line):
            return False
        # The cheap prefilter on the raw line must never drop a line the field filter wants.
        if field != "line" and rule.get("parser") == "json" and not re.search('"%s":\\s*"(%s)"' % (field, regex), line):
            return False
    for field, regex in exclude:
        if field == "line" and re.search(regex, line):
            return False
    fields = {}
    if rule.get("parser") == "json":
        try:
            fields = json.loads(line)
        except ValueError:
            return False  # dropped by __error__=""
    for field, regex in match:
        if field != "line" and not re.fullmatch(regex, str(fields.get(field, ""))):
            return False
    for field, regex in exclude:
        if field != "line" and re.fullmatch(regex, str(fields.get(field, ""))):
            return False
    return bool(params)


class LogChecks(unittest.TestCase):
    def test_ids_unique_across_both_catalogues(self):
        ids = [r["id"] for r in CATALOGUE + LOG_CATALOGUE]
        self.assertEqual(len(ids), len(set(ids)))

    def test_fields_are_known(self):
        for r in LOG_CATALOGUE:
            self.assertIn(r["module"], obs.MODULE_NAMES, r["id"])
            self.assertIn(r.get("source"), (None, "mattermost", "postgresql", "nginx", "opensearch", "kernel"), r["id"])
            self.assertIn(r.get("parser"), (None, "json"), r["id"])
            self.assertIn(r.get("kind", "count"), ("count", "baseline", "new", "silent"), r["id"])
            self.assertRegex(r["window"], r"^\d+[smhd]$", r["id"])
            self.assertRegex(r["for"], r"^\d+[smh]$", r["id"])
            self.assertTrue(set(r["tiers"]) <= set(SEVERITIES), r["id"])
            for field, _ in r.get("match", []) + r.get("exclude", []):
                if field != "line":
                    self.assertEqual(r.get("parser"), "json", "%s filters on %s without parsing" % (r["id"], field))

    def test_regexes_stay_in_the_re2_subset(self):
        # Loki uses RE2: no lookarounds or backreferences.
        for r in LOG_CATALOGUE:
            for _, regex in r.get("match", []) + r.get("exclude", []):
                self.assertNotRegex(regex, r"\(\?[=!<]|\\[1-9]", r["id"])
                re.compile(obs.filled(regex, {}))

    def test_examples_match_and_counter_examples_do_not(self):
        for r in LOG_CATALOGUE:
            if r.get("kind") == "silent":
                continue
            self.assertTrue(r["examples"], "%s needs a real example line" % r["id"])
            self.assertTrue(r["counter_examples"], "%s needs a line it must not match" % r["id"])
            for line in r["examples"]:
                self.assertTrue(log_line_matches(r, line), "%s should match %s" % (r["id"], line))
            for line in r["counter_examples"]:
                self.assertFalse(log_line_matches(r, line), "%s should not match %s" % (r["id"], line))

    def test_no_log_rules_without_loki(self):
        ids = {r["labels"]["mmi_rule"] for r in rules_of(FULL)}
        self.assertFalse(ids & {r["id"] for r in LOG_CATALOGUE})
        self.assertNotIn("log-ship-failing", ids)
        self.assertEqual(obs.logs_on(dict(FULL, loki="False")), False)

    def test_log_rules_follow_the_estate(self):
        rules = rules_of(FULL_LOGS)
        ids = {r["labels"]["mmi_rule"] for r in rules}
        self.assertTrue({"mm-log-fatal", "pg-log-io", "unit-crash-loop", "log-host-silent", "log-ship-failing"} <= ids)
        db_only = {"loki": True, "servers": [{"name": "db01", "address": "x", "components": ["postgresql"]}]}
        modules = {r["labels"]["module"] for r in rules_of(db_only) if r["labels"]["signal"] == "logs"}
        self.assertEqual(modules, {"host", "postgresql"})

    def test_loki_rules_query_loki(self):
        for r in rules_of(FULL_LOGS):
            uid = r["data"][0]["datasourceUid"]
            if r["labels"]["signal"] == "logs":
                self.assertEqual(uid, "loki", r["uid"])
                self.assertEqual(r["data"][0]["model"]["queryType"], "instant")
                self.assertIn("The lines, for Explore", r["annotations"]["description"])
            else:
                self.assertEqual(uid, "prometheus", r["uid"])

    def test_new_errors_wait_for_a_week_of_history(self):
        rule = [r for r in rules_of(FULL_LOGS) if r["labels"]["mmi_rule"] == "mm-log-new-error"][0]
        refs = [d["refId"] for d in rule["data"]]
        self.assertEqual(refs, ["A", "B", "M", "C"])
        self.assertIn("unless", rule["data"][0]["model"]["expr"])
        self.assertIn("offset 7d", rule["data"][1]["model"]["expr"])
        self.assertEqual(rule["data"][3]["model"]["expression"], "M")

    def test_trend_rules_run_less_often(self):
        doc, _ = obs.build_alerts(FULL_LOGS)
        groups = {g["name"]: g for g in doc["groups"]}
        trend = {r["labels"]["mmi_rule"] for r in groups[obs.TREND_GROUP]["rules"]}
        self.assertEqual(trend, {"mm-log-error-spike", "mm-log-new-error", "pg-log-error-spike"})
        self.assertEqual(groups[obs.TREND_GROUP]["interval"], obs.TREND_INTERVAL)

    def test_baseline_survives_a_missing_week(self):
        rule = [r for r in obs.effective_rules(FULL_LOGS) if r["id"] == "mm-log-error-spike"][0]
        self.assertIn("offset 7d", rule["expr"])
        self.assertIn(") or (", rule["expr"])

    def test_ignores_apply_to_trends_only(self):
        spec = dict(FULL_LOGS, log_ignore=["websocket: close 1001"])
        rules = {r["id"]: r for r in obs.effective_rules(spec)}
        self.assertIn("websocket: close 1001", rules["mm-log-error-spike"]["expr"])
        self.assertIn("websocket: close 1001", rules["mm-log-new-error"]["expr"])
        self.assertNotIn("websocket: close 1001", rules["mm-log-fatal"]["expr"])

    def test_silent_hosts_are_built_from_the_estate(self):
        rule = [r for r in obs.effective_rules(FULL_LOGS) if r["id"] == "log-host-silent"][0]
        for server in FULL_LOGS["servers"]:
            self.assertIn('"host_name", "%s"' % server["name"], rule["expr"])

    def test_db_port_comes_from_the_answer_file(self):
        spec = dict(FULL_LOGS, params={"db_port": 6432})
        rule = [r for r in obs.effective_rules(spec) if r["id"] == "mm-log-db"][0]
        self.assertIn(":6432: connect", rule["expr"])

    def test_selftest_queries_only_see_the_selftest_host(self):
        summary = obs.build(FULL_LOGS)["summary"]
        tested = [q for q in summary["rule_queries"] if "selftest_expr" in q]
        self.assertTrue(tested)
        for q in tested:
            self.assertIn('host_name="%s"' % obs.SELFTEST_HOST, q["selftest_expr"])
            self.assertTrue(q["examples"])


if __name__ == "__main__":
    unittest.main()
