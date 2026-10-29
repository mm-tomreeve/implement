#!/usr/bin/env python3
"""Build Grafana alert rules and dashboards for a Mattermost estate.

Reads a JSON spec on stdin describing the estate (which servers run which
components), applies tools/alert_catalogue.py and prints JSON:

  {"files": {"alerting/mattermost-implement.json": "...", "dashboards/mmi-host.json": "..."},
   "summary": {...}}

The grafana role writes those files under /etc/grafana/provisioning and the
dashboards directory. Standard library only.

  observability.py build < spec.json
  observability.py catalogue            Markdown table of every rule and tier
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alert_catalogue import BLOCKDEV, CATALOGUE, FS, NETDEV, SEVERITIES  # noqa: E402

MODULE_COMPONENTS = {
    "mattermost": "mattermost",
    "postgresql": "postgresql",
    "nginx": "nginx",
    "opensearch": "opensearch",
    "rtcd": "rtcd",
    "monitoring": "prometheus",
}
MODULE_NAMES = {
    "host": "Servers", "site": "Site availability", "mattermost": "Mattermost", "postgresql": "PostgreSQL",
    "nginx": "Nginx", "opensearch": "OpenSearch", "rtcd": "Calls (rtcd)", "monitoring": "Monitoring",
}
# Mattermost brand colours for thresholds: Sapphire, Sunglow, and red for critical.
TIER_COLOURS = {"ok": "#3db887", "info": "#1C58D9", "warning": "#FFBC1F", "critical": "#D24B4E"}


# ------------------------------------------------------------------ alerts

def modules_present(spec):
    present = {"host"} if spec.get("servers") else set()
    components = {c for s in spec.get("servers", []) for c in s.get("components", [])}
    for module, component in MODULE_COMPONENTS.items():
        if component in components:
            present.add(module)
    if spec.get("site_url"):
        present.add("site")
    return present


def effective_rules(spec):
    present = modules_present(spec)
    overrides = spec.get("overrides") or {}
    disabled = set(spec.get("disabled") or [])
    params = dict({"mm_max_conns": 300, "db_name": "mattermost"}, **(spec.get("params") or {}))
    rules = []
    for entry in CATALOGUE:
        if entry["module"] not in present or entry["id"] in disabled:
            continue
        rule = dict(entry)
        over = overrides.get(entry["id"], {})
        rule["tiers"] = dict(entry["tiers"], **(over.get("tiers") or {}))
        rule["tiers"] = {k: v for k, v in rule["tiers"].items() if v is not None}
        rule["for"] = over.get("for", entry["for"])
        if "%(" in rule["expr"]:
            rule["expr"] = rule["expr"] % params
        rules.append(rule)
    return rules


def evaluators(direction, tiers):
    """Non-overlapping bands, so only the worst tier fires.

    above: info in [info, warning), warning in [warning, critical), critical above critical.
    below: the mirror image.
    """
    ordered = [t for t in SEVERITIES if t in tiers]
    out = {}
    for i, tier in enumerate(ordered):
        value = tiers[tier]
        worse = [tiers[t] for t in ordered[i + 1:]]
        if not worse:
            out[tier] = {"type": "gt" if direction == "above" else "lt", "params": [value]}
        elif direction == "above":
            out[tier] = {"type": "within_range", "params": [value, worse[0]]}
        else:
            out[tier] = {"type": "within_range", "params": [worse[0], value]}
    return out


def rule_json(rule, tier, evaluator, spec):
    uid = ("mmi-%s-%s" % (rule["id"], tier[:4]))[:40]
    threshold = evaluator["params"]
    shown = (" %s %s" % ({"gt": ">", "lt": "<"}.get(evaluator["type"], "between"),
                         " and ".join("%g" % p for p in threshold)))
    return {
        "uid": uid,
        "title": "%s [%s]" % (rule["title"], tier),
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 900, "to": 0},
                "datasourceUid": spec.get("datasource_uid", "prometheus"),
                "model": {"expr": rule["expr"], "instant": True, "range": False, "refId": "A",
                          "intervalMs": 15000, "maxDataPoints": 43200},
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {"type": "threshold", "expression": "A", "refId": "C",
                          "conditions": [{"evaluator": evaluator}]},
            },
        ],
        "noDataState": "OK",
        "execErrState": "Error",
        "for": rule["for"],
        "annotations": {
            "summary": rule["summary"],
            "description": "%s\n\nThreshold:%s%s. Tier: %s." % (rule["action"], shown, rule.get("unit", ""), tier),
        },
        "labels": {
            "severity": tier,
            "module": rule["module"],
            "mmi_rule": rule["id"],
            "deployment": spec.get("deployment", "mattermost"),
        },
        "isPaused": False,
    }


def build_alerts(spec):
    groups = {}
    count = {s: 0 for s in SEVERITIES}
    for rule in effective_rules(spec):
        for tier, evaluator in evaluators(rule["direction"], rule["tiers"]).items():
            groups.setdefault(rule["module"], []).append(rule_json(rule, tier, evaluator, spec))
            count[tier] += 1
    doc = {
        "apiVersion": 1,
        "groups": [
            {"orgId": 1, "name": MODULE_NAMES[m], "folder": spec.get("folder", "Mattermost Implement"),
             "interval": "1m", "rules": rules}
            for m, rules in sorted(groups.items())
        ],
    }
    return doc, count


# ------------------------------------------------------------------ dashboards

class Board:
    """Tiny helper that lays panels out on Grafana's 24-column grid."""

    def __init__(self, uid, title, spec, tags, variables=None):
        self.uid, self.title, self.spec, self.tags = uid, title, spec, tags
        self.panels, self.x, self.y, self.row_h, self.next_id = [], 0, 0, 0, 1
        self.variables = variables or []

    def ds(self):
        return {"type": "prometheus", "uid": self.spec.get("datasource_uid", "prometheus")}

    def row(self, title):
        self.newline()
        self.panels.append({"type": "row", "title": title, "collapsed": False, "id": self.next_id,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []})
        self.next_id += 1
        self.y += 1

    def newline(self):
        if self.x:
            self.y += self.row_h
            self.x, self.row_h = 0, 0

    def add(self, panel, w, h):
        if self.x + w > 24:
            self.newline()
        panel["id"] = self.next_id
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.next_id += 1
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def thresholds(self, steps, below=False):
        """Colour steps that match the alert tiers: green, then Sapphire, Sunglow and red."""
        if not below:
            out = [{"color": TIER_COLOURS["ok"], "value": None}]
            for tier in SEVERITIES:
                if tier in steps:
                    # Alerts fire strictly above the threshold; a colour step starts at it.
                    # Nudge zero thresholds so an exact 0 stays green.
                    value = steps[tier] if steps[tier] != 0 else 0.001
                    out.append({"color": TIER_COLOURS[tier], "value": value})
            return {"mode": "absolute", "steps": out}
        # Smaller is worse: red at the bottom, each threshold steps up to the next better colour.
        present = [t for t in ("critical", "warning", "info") if t in steps]
        out = [{"color": TIER_COLOURS[present[0]] if present else TIER_COLOURS["ok"], "value": None}]
        for i, tier in enumerate(present):
            better = present[i + 1] if i + 1 < len(present) else "ok"
            out.append({"color": TIER_COLOURS[better], "value": steps[tier]})
        return {"mode": "absolute", "steps": out}

    def targets(self, queries):
        return [{"expr": q[0], "legendFormat": q[1] if len(q) > 1 else "", "refId": chr(65 + i), "datasource": self.ds()}
                for i, q in enumerate(queries)]

    def timeseries(self, title, queries, unit="short", w=12, h=8, steps=None, below=False, desc="", stack=False):
        defaults = {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never",
                                             "stacking": {"mode": "normal" if stack else "none"},
                                             "thresholdsStyle": {"mode": "dashed" if steps else "off"}}}
        if steps:
            defaults["thresholds"] = self.thresholds(steps, below)
        self.add({"type": "timeseries", "title": title, "description": desc, "datasource": self.ds(),
                  "targets": self.targets(queries), "fieldConfig": {"defaults": defaults, "overrides": []},
                  "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                              "tooltip": {"mode": "multi", "sort": "desc"}}}, w, h)

    def stat(self, title, query, unit="short", w=6, h=4, steps=None, below=False, legend="", mappings=None,
             reducer="lastNotNull", desc=""):
        defaults = {"unit": unit, "thresholds": self.thresholds(steps or {}, below), "mappings": mappings or []}
        self.add({"type": "stat", "title": title, "description": desc, "datasource": self.ds(),
                  "targets": self.targets([(query, legend)]),
                  "fieldConfig": {"defaults": defaults, "overrides": []},
                  "options": {"colorMode": "background", "graphMode": "none", "textMode": "auto",
                              "reduceOptions": {"calcs": [reducer], "fields": "", "values": False}}}, w, h)

    def gauge(self, title, query, unit="percent", w=6, h=6, steps=None, below=False, legend="{{instance}}"):
        defaults = {"unit": unit, "min": 0, "max": 100, "thresholds": self.thresholds(steps or {}, below)}
        self.add({"type": "bargauge", "title": title, "datasource": self.ds(),
                  "targets": self.targets([(query, legend)]),
                  "fieldConfig": {"defaults": defaults, "overrides": []},
                  "options": {"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                              "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}, w, h)

    def alerts(self, title, w=24, h=8):
        self.add({"type": "alertlist", "title": title,
                  "options": {"viewMode": "list", "groupMode": "default", "maxItems": 50, "sortOrder": 3,
                              "stateFilter": {"firing": True, "pending": True, "noData": False, "normal": False, "error": True},
                              "alertInstanceLabelFilter": "", "folder": None, "showInstances": True}}, w, h)

    def text(self, content, w=24, h=3):
        self.add({"type": "text", "title": "", "options": {"mode": "markdown", "content": content}}, w, h)

    def json(self):
        self.newline()
        return {
            "uid": self.uid, "title": self.title, "tags": ["mattermost-implement"] + self.tags,
            "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
            "refresh": "1m", "time": {"from": "now-6h", "to": "now"},
            "templating": {"list": self.variables}, "annotations": {"list": []},
            "links": [{"type": "dashboards", "tags": ["mattermost-implement"], "asDropdown": True,
                       "title": "Mattermost Implement", "includeVars": False, "keepTime": True}],
            "panels": self.panels,
        }


def instance_var(spec, metric):
    return {"name": "instance", "label": "Server", "type": "query", "datasource": {"type": "prometheus", "uid": spec.get("datasource_uid", "prometheus")},
            "query": {"query": "label_values(%s, instance)" % metric, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
            "definition": "label_values(%s, instance)" % metric, "refresh": 2, "includeAll": True, "multi": True,
            "current": {"selected": True, "text": "All", "value": "$__all"}}


def tiers_of(rule_id, spec):
    for rule in effective_rules(spec):
        if rule["id"] == rule_id:
            return rule["tiers"]
    return {}


def overview(spec, present):
    b = Board("mmi-overview", "Mattermost estate overview", spec, ["overview"])
    servers = ", ".join("%s (%s)" % (s["name"], ", ".join(s.get("components", [])) or "host") for s in spec.get("servers", []))
    b.text("**%s**. Servers watched: %s. Alerts live in the *Mattermost Implement* folder under Alerting; "
           "add a contact point there to get notified." % (spec.get("deployment", "Mattermost"), servers or "none"), h=3)
    if "site" in present:
        b.stat("Site reachable", 'min(probe_success{job="blackbox_http"})', w=6, steps={"critical": 1}, below=True,
               mappings=[{"type": "value", "options": {"0": {"text": "DOWN"}, "1": {"text": "UP"}}}])
        b.stat("Health check time", 'max(probe_duration_seconds{job="blackbox_http"})', unit="s", w=6,
               steps=tiers_of("site-slow", spec))
        b.stat("Certificate days left", 'min((probe_ssl_earliest_cert_expiry{job="blackbox_http"} - time()) / 86400)',
               unit="d", w=6, steps=tiers_of("tls-expiry", spec), below=True)
        b.stat("Availability (24h)", 'avg_over_time(probe_success{job="blackbox_http"}[24h]) * 100', unit="percent", w=6,
               steps={"warning": 99.9, "critical": 99}, below=True)
    b.stat("Targets down", 'count(up == 0) or vector(0)', w=6, steps={"critical": 1})
    b.stat("Services not running", 'count(node_systemd_unit_state{state="active"} == 0) or vector(0)', w=6, steps={"critical": 1})
    b.stat("Firing critical alerts", 'count(ALERTS{severity="critical", alertstate="firing"}) or vector(0)', w=6,
           steps={"critical": 1}, desc="Grafana-managed alerts are listed in the panel below.")
    b.stat("Servers", 'count(up{job="node"})', w=6)
    b.alerts("Firing and pending alerts")
    b.row("Servers")
    b.gauge("Disk used (fullest filesystem)", 'max by (instance) (100 * (1 - node_filesystem_avail_bytes{%s} / node_filesystem_size_bytes{%s}))' % (FS, FS),
            w=8, steps=tiers_of("disk-used", spec))
    b.gauge("Memory available", "100 * node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes", w=8,
            steps=tiers_of("memory-available", spec), below=True)
    b.gauge("CPU busy (5m)", '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))', w=8,
            steps=tiers_of("cpu-busy", spec))
    b.timeseries("Up by job and server", [("up", "{{job}} {{instance}}")], w=24, h=7)
    if "site" in present:
        b.row("What users see")
        b.timeseries("Health check time", [('probe_duration_seconds{job="blackbox_http"}', "{{instance}}")], unit="s",
                     steps=tiers_of("site-slow", spec))
        b.timeseries("Health check result", [('probe_success{job="blackbox_http"}', "{{instance}}")], unit="bool_yes_no")
    return b.json()


def host_board(spec):
    b = Board("mmi-host", "Servers", spec, ["host"], [instance_var(spec, "node_uname_info")])
    sel = 'instance=~"$instance"'
    fs = '%s,%s' % (FS, sel)
    b.timeseries("CPU busy", [('100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",%s}[5m])))' % sel, "{{instance}}")],
                 unit="percent", steps=tiers_of("cpu-busy", spec))
    b.timeseries("Load per core", [('node_load5{%s} / on (instance) count by (instance) (node_cpu_seconds_total{mode="idle",%s})' % (sel, sel), "{{instance}}")],
                 steps=tiers_of("load-per-core", spec))
    b.timeseries("Memory available", [("100 * node_memory_MemAvailable_bytes{%s} / node_memory_MemTotal_bytes{%s}" % (sel, sel), "{{instance}}")],
                 unit="percent", steps=tiers_of("memory-available", spec), below=True)
    b.timeseries("Swap used", [("node_memory_SwapTotal_bytes{%s} - node_memory_SwapFree_bytes{%s}" % (sel, sel), "{{instance}}")], unit="bytes")
    b.timeseries("Disk used by filesystem", [("100 * (1 - node_filesystem_avail_bytes{%s} / node_filesystem_size_bytes{%s})" % (fs, fs), "{{instance}} {{mountpoint}}")],
                 unit="percent", steps=tiers_of("disk-used", spec))
    b.timeseries("Free space forecast (24h)", [("predict_linear(node_filesystem_avail_bytes{%s}[6h], 86400)" % fs, "{{instance}} {{mountpoint}}")], unit="bytes")
    b.timeseries("Disk busy", [('rate(node_disk_io_time_seconds_total{%s,%s}[5m]) * 100' % (BLOCKDEV, sel), "{{instance}} {{device}}")],
                 unit="percent", steps=tiers_of("disk-io-busy", spec))
    b.timeseries("Disk throughput", [('rate(node_disk_read_bytes_total{%s,%s}[5m])' % (BLOCKDEV, sel), "{{instance}} {{device}} read"),
                                     ('rate(node_disk_written_bytes_total{%s,%s}[5m])' % (BLOCKDEV, sel), "{{instance}} {{device}} write")], unit="Bps")
    b.timeseries("Network traffic", [('rate(node_network_receive_bytes_total{%s,%s}[5m]) * 8' % (NETDEV, sel), "{{instance}} {{device}} in"),
                                     ('rate(node_network_transmit_bytes_total{%s,%s}[5m]) * 8' % (NETDEV, sel), "{{instance}} {{device}} out")], unit="bps")
    b.timeseries("Network and UDP errors", [('rate(node_network_receive_errs_total{%s,%s}[5m])' % (NETDEV, sel), "{{instance}} {{device}} rx errors"),
                                            ('rate(node_netstat_Udp_RcvbufErrors{%s}[5m])' % sel, "{{instance}} UDP buffer overflows")], unit="pps")
    b.timeseries("Clock offset", [("node_timex_offset_seconds{%s} * 1000" % sel, "{{instance}}")], unit="ms")
    b.timeseries("File descriptors used", [("100 * node_filefd_allocated{%s} / node_filefd_maximum{%s}" % (sel, sel), "{{instance}}")],
                 unit="percent", steps=tiers_of("fd-used", spec))
    b.timeseries("Service state (1 = active)", [('node_systemd_unit_state{state="active",%s}' % sel, "{{instance}} {{name}}")], w=24)
    return b.json()


def postgres_board(spec):
    db = (spec.get("params") or {}).get("db_name", "mattermost")
    b = Board("mmi-postgresql", "PostgreSQL", spec, ["postgresql"], [instance_var(spec, "pg_up")])
    sel = 'instance=~"$instance"'
    dsel = 'datname="%s",%s' % (db, sel)
    b.stat("Up", "min(pg_up{%s})" % sel, w=6, steps={"critical": 1}, below=True,
           mappings=[{"type": "value", "options": {"0": {"text": "DOWN"}, "1": {"text": "UP"}}}])
    b.stat("Connections used", "100 * sum(pg_stat_activity_count{%s}) / max(pg_settings_max_connections{%s})" % (sel, sel),
           unit="percent", w=6, steps=tiers_of("pg-connections", spec))
    b.stat("Database size", "max(pg_database_size_bytes{%s})" % dsel, unit="bytes", w=6)
    b.stat("Cache hit ratio (30m)", '100 * sum(rate(pg_stat_database_blks_hit{%s}[30m])) / clamp_min(sum(rate(pg_stat_database_blks_hit{%s}[30m])) + sum(rate(pg_stat_database_blks_read{%s}[30m])), 1)' % (dsel, dsel, dsel),
           unit="percent", w=6, steps=tiers_of("pg-cache-hit", spec), below=True)
    b.timeseries("Connections by state", [("sum by (state) (pg_stat_activity_count{%s})" % sel, "{{state}}")], stack=True)
    b.timeseries("Transactions per second", [("rate(pg_stat_database_xact_commit{%s}[5m])" % dsel, "commits"),
                                             ("rate(pg_stat_database_xact_rollback{%s}[5m])" % dsel, "rollbacks")], unit="ops")
    b.timeseries("Longest open transaction", [("max(pg_stat_activity_max_tx_duration{%s})" % dsel, "seconds")], unit="s",
                 steps=tiers_of("pg-long-transaction", spec))
    b.timeseries("Rows read and written", [("rate(pg_stat_database_tup_fetched{%s}[5m])" % dsel, "fetched"),
                                           ("rate(pg_stat_database_tup_inserted{%s}[5m])" % dsel, "inserted"),
                                           ("rate(pg_stat_database_tup_updated{%s}[5m])" % dsel, "updated"),
                                           ("rate(pg_stat_database_tup_deleted{%s}[5m])" % dsel, "deleted")], unit="ops")
    b.timeseries("Locks by mode", [("sum by (mode) (pg_locks_count{%s})" % dsel, "{{mode}}")])
    b.timeseries("Deadlocks and conflicts", [("increase(pg_stat_database_deadlocks{%s}[15m])" % dsel, "deadlocks"),
                                             ("increase(pg_stat_database_conflicts{%s}[15m])" % dsel, "conflicts")])
    b.timeseries("Database size", [("pg_database_size_bytes{%s}" % dsel, "{{instance}}")], unit="bytes", w=24)
    return b.json()


def mattermost_board(spec):
    b = Board("mmi-mattermost", "Mattermost application", spec, ["mattermost"], [instance_var(spec, "mattermost_system_server_start_time")])
    sel = 'instance=~"$instance"'
    b.text("Mattermost publishes these metrics when an Enterprise licence is installed and metrics are on. "
           "The community dashboards (Performance Monitoring v2 and Collapsed Reply Threads) sit alongside this one.", h=2)
    b.stat("API error rate", '100 * (sum(rate(mattermost_api_time_count{status_code=~"5..",%s}[5m])) or vector(0)) / clamp_min(sum(rate(mattermost_api_time_count{%s}[5m])), 0.001)' % (sel, sel),
           unit="percent", w=6, steps=tiers_of("mm-api-errors", spec))
    b.stat("API p95", 'histogram_quantile(0.95, sum by (le) (rate(mattermost_api_time_bucket{%s}[5m])))' % sel, unit="s", w=6,
           steps=tiers_of("mm-api-latency", spec))
    b.stat("Websocket connections", "sum(mattermost_http_websockets_total{%s})" % sel, w=6)
    b.stat("DB pool used", "100 * max(mattermost_db_master_connections_total{%s}) / %s" % (sel, (spec.get("params") or {}).get("mm_max_conns", 300)),
           unit="percent", w=6, steps=tiers_of("mm-db-connections", spec))
    b.timeseries("API requests per second by status", [('sum by (status_code) (rate(mattermost_api_time_count{%s}[5m]))' % sel, "{{status_code}}")], unit="reqps")
    b.timeseries("API latency", [('histogram_quantile(0.5, sum by (le) (rate(mattermost_api_time_bucket{%s}[5m])))' % sel, "p50"),
                                 ('histogram_quantile(0.95, sum by (le) (rate(mattermost_api_time_bucket{%s}[5m])))' % sel, "p95"),
                                 ('histogram_quantile(0.99, sum by (le) (rate(mattermost_api_time_bucket{%s}[5m])))' % sel, "p99")],
                 unit="s", steps=tiers_of("mm-api-latency", spec))
    b.timeseries("Slowest handlers (p95)", [('topk(10, histogram_quantile(0.95, sum by (le, handler) (rate(mattermost_api_time_bucket{%s}[15m]))))' % sel, "{{handler}}")], unit="s")
    b.timeseries("Websocket connections", [("mattermost_http_websockets_total{%s}" % sel, "{{instance}}")])
    b.timeseries("Logins and failures", [("rate(mattermost_login_logins_total{%s}[5m]) * 60" % sel, "logins/min"),
                                         ("rate(mattermost_login_logins_fail_total{%s}[5m]) * 60" % sel, "failures/min")])
    b.timeseries("Push notifications", [('sum by (type) (rate(mattermost_notifications_success{%s}[5m]))' % sel, "{{type}} ok"),
                                        ('sum by (type) (rate(mattermost_notifications_error{%s}[5m]))' % sel, "{{type}} error")], unit="ops")
    b.timeseries("Database connections", [("mattermost_db_master_connections_total{%s}" % sel, "{{instance}}")])
    b.timeseries("Go heap in use", [('go_memstats_heap_inuse_bytes{job="mattermost",%s}' % sel, "{{instance}}")], unit="bytes")
    b.timeseries("Goroutines", [('go_goroutines{job="mattermost",%s}' % sel, "{{instance}}")])
    return b.json()


def nginx_board(spec):
    b = Board("mmi-nginx", "Nginx", spec, ["nginx"], [instance_var(spec, "nginx_up")])
    sel = 'instance=~"$instance"'
    b.stat("Up", "min(nginx_up{%s})" % sel, w=6, steps={"critical": 1}, below=True,
           mappings=[{"type": "value", "options": {"0": {"text": "DOWN"}, "1": {"text": "UP"}}}])
    b.stat("Active connections", "sum(nginx_connections_active{%s})" % sel, w=6)
    b.stat("Requests per second", "sum(rate(nginx_http_requests_total{%s}[5m]))" % sel, unit="reqps", w=6)
    b.stat("Dropped connections/s", "sum(rate(nginx_connections_accepted{%s}[5m]) - rate(nginx_connections_handled{%s}[5m]))" % (sel, sel),
           w=6, steps=tiers_of("nginx-dropped", spec))
    b.timeseries("Connections by state", [("nginx_connections_reading{%s}" % sel, "{{instance}} reading"),
                                          ("nginx_connections_writing{%s}" % sel, "{{instance}} writing"),
                                          ("nginx_connections_waiting{%s}" % sel, "{{instance}} waiting (websockets, keep-alive)")])
    b.timeseries("Requests per second", [("rate(nginx_http_requests_total{%s}[5m])" % sel, "{{instance}}")], unit="reqps")
    return b.json()


def opensearch_board(spec):
    b = Board("mmi-opensearch", "OpenSearch", spec, ["opensearch"], [instance_var(spec, "elasticsearch_clusterinfo_up")])
    sel = 'instance=~"$instance"'
    b.stat("Cluster status", 'max(elasticsearch_cluster_health_status{color="green",%s}) * 1 + max(elasticsearch_cluster_health_status{color="yellow",%s}) * 2 + max(elasticsearch_cluster_health_status{color="red",%s}) * 3' % (sel, sel, sel),
           w=6, steps={"info": 2, "critical": 3},
           mappings=[{"type": "value", "options": {"1": {"text": "GREEN"}, "2": {"text": "YELLOW"}, "3": {"text": "RED"}}}])
    b.stat("Heap used", '100 * max(elasticsearch_jvm_memory_used_bytes{area="heap",%s} / elasticsearch_jvm_memory_max_bytes{area="heap",%s})' % (sel, sel),
           unit="percent", w=6, steps=tiers_of("os-heap", spec))
    b.stat("Data disk used", "100 * max(1 - elasticsearch_filesystem_data_available_bytes{%s} / elasticsearch_filesystem_data_size_bytes{%s})" % (sel, sel),
           unit="percent", w=6, steps=tiers_of("os-disk", spec))
    b.stat("Documents", "sum(elasticsearch_indices_docs{%s})" % sel, w=6)
    b.timeseries("Heap used", [('100 * elasticsearch_jvm_memory_used_bytes{area="heap",%s} / elasticsearch_jvm_memory_max_bytes{area="heap",%s}' % (sel, sel), "{{instance}}")],
                 unit="percent", steps=tiers_of("os-heap", spec))
    b.timeseries("Search and indexing rate", [("rate(elasticsearch_indices_search_query_total{%s}[5m])" % sel, "{{instance}} searches"),
                                              ("rate(elasticsearch_indices_indexing_index_total{%s}[5m])" % sel, "{{instance}} indexed")], unit="ops")
    b.timeseries("Rejected work", [('rate(elasticsearch_thread_pool_rejected_count{type=~"search|write",%s}[5m])' % sel, "{{instance}} {{type}}")], unit="ops")
    b.timeseries("GC time", [("rate(elasticsearch_jvm_gc_collection_seconds_sum{%s}[5m])" % sel, "{{instance}} {{gc}}")], unit="s")
    return b.json()


def calls_board(spec):
    b = Board("mmi-calls", "Calls (rtcd)", spec, ["rtcd"], [instance_var(spec, "rtcd_process_start_time_seconds")])
    sel = 'instance=~"$instance"'
    b.stat("Active sessions", "sum(rtcd_rtc_sessions_total{%s}) or vector(0)" % sel, w=6)
    b.stat("rtcd CPU (host share)", '100 * sum(rate(rtcd_process_cpu_seconds_total{%s}[5m])) / count(node_cpu_seconds_total{mode="idle",%s})' % (sel, sel),
           unit="percent", w=6, steps=tiers_of("rtcd-cpu", spec))
    b.stat("UDP overflows/s", "sum(rate(node_netstat_Udp_RcvbufErrors{%s}[5m]))" % sel, w=6, steps=tiers_of("udp-buffer-errors", spec))
    b.stat("Media errors (15m)", "sum(increase(rtcd_rtc_errors_total{%s}[15m])) or vector(0)" % sel, w=6, steps=tiers_of("rtcd-errors", spec))
    b.timeseries("Sessions", [("rtcd_rtc_sessions_total{%s}" % sel, "{{instance}}")])
    b.timeseries("Client packet loss p95", [("100 * histogram_quantile(0.95, sum by (le, instance) (rate(rtcd_rtc_client_loss_rate_bucket{%s}[10m])))" % sel, "{{instance}}")],
                 unit="percent", steps=tiers_of("rtcd-packet-loss", spec))
    b.timeseries("Client round-trip time p95", [("histogram_quantile(0.95, sum by (le, instance) (rate(rtcd_rtc_client_rtt_bucket{%s}[10m])))" % sel, "{{instance}}")], unit="s")
    b.timeseries("Host network throughput", [('rate(node_network_receive_bytes_total{%s,%s}[5m]) * 8' % (NETDEV, sel), "{{instance}} in"),
                                             ('rate(node_network_transmit_bytes_total{%s,%s}[5m]) * 8' % (NETDEV, sel), "{{instance}} out")], unit="bps")
    b.timeseries("rtcd CPU and memory", [('rate(rtcd_process_cpu_seconds_total{%s}[5m])' % sel, "{{instance}} CPU cores"),
                                         ('rtcd_process_resident_memory_bytes{%s} / 1024 / 1024 / 1024' % sel, "{{instance}} RSS GiB")])
    b.timeseries("rtcd file descriptors used", [("100 * rtcd_process_open_fds{%s} / rtcd_process_max_fds{%s}" % (sel, sel), "{{instance}}")],
                 unit="percent", steps=tiers_of("rtcd-fds", spec))
    b.timeseries("Errors by type", [("sum by (type) (rate(rtcd_rtc_errors_total{%s}[5m]))" % sel, "{{type}}")], unit="ops")
    return b.json()


def build_dashboards(spec):
    present = modules_present(spec)
    boards = {"mmi-overview.json": overview(spec, present), "mmi-host.json": host_board(spec)}
    if "postgresql" in present:
        boards["mmi-postgresql.json"] = postgres_board(spec)
    if "mattermost" in present:
        boards["mmi-mattermost.json"] = mattermost_board(spec)
    if "nginx" in present:
        boards["mmi-nginx.json"] = nginx_board(spec)
    if "opensearch" in present:
        boards["mmi-opensearch.json"] = opensearch_board(spec)
    if "rtcd" in present:
        boards["mmi-calls.json"] = calls_board(spec)
    return boards


# ------------------------------------------------------------------ cli

def build(spec):
    alerts, count = build_alerts(spec)
    files = {"alerting/mattermost-implement.json": json.dumps(alerts, indent=2, sort_keys=True) + "\n"}
    for name, board in build_dashboards(spec).items():
        files["dashboards/" + name] = json.dumps(board, indent=2, sort_keys=True) + "\n"
    rules = effective_rules(spec)
    return {
        "files": files,
        "summary": {
            "modules": sorted(modules_present(spec)),
            "checks": len(rules),
            "rules": sum(count.values()),
            "by_severity": count,
            "dashboards": sorted(n[:-5] for n in build_dashboards(spec)),
            "rule_queries": [{"id": r["id"], "module": r["module"], "expr": r["expr"], "quiet": bool(r.get("quiet"))} for r in rules],
        },
    }


def catalogue_markdown(heading="###", suffix=""):
    """Per-module tables for docs/alerts.md and the README. tests/ checks both match."""
    out = []
    for module in ["host", "site", "mattermost", "postgresql", "nginx", "opensearch", "rtcd", "monitoring"]:
        rules = [r for r in CATALOGUE if r["module"] == module]
        if not rules:
            continue
        out += ["%s %s%s" % (heading, MODULE_NAMES[module], suffix), "",
                "| Check | Info | Warning | Critical | For | What to do |", "|---|---|---|---|---|---|"]
        for r in rules:
            sign = ">" if r["direction"] == "above" else "<"
            cells = []
            for tier in SEVERITIES:
                value = r["tiers"].get(tier)
                cells.append("" if value is None else "%s %g%s" % (sign, value, r.get("unit", "")))
            action = r["action"].replace("|", "\\|")
            out.append("| %s `%s` | %s | %s | %s |" % (r["title"], r["id"], " | ".join(cells), r["for"], action))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv):
    if len(argv) > 1 and argv[1] == "catalogue":
        sys.stdout.write(catalogue_markdown())
        return 0
    if len(argv) > 1 and argv[1] == "build":
        print(json.dumps(build(json.load(sys.stdin))))
        return 0
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
