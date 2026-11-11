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
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alert_catalogue import BLOCKDEV, CATALOGUE, FS, LOG_CATALOGUE, NETDEV, SEVERITIES  # noqa: E402

MODULE_COMPONENTS = {
    "mattermost": "mattermost",
    "postgresql": "postgresql",
    "nginx": "nginx",
    "opensearch": "opensearch",
    "rtcd": "rtcd",
    "nfs": "nfs",
    "monitoring": "prometheus",
}
MODULE_NAMES = {
    "host": "Servers", "site": "Site availability", "mattermost": "Mattermost", "postgresql": "PostgreSQL",
    "nginx": "Nginx", "opensearch": "OpenSearch", "rtcd": "Calls (rtcd)", "nfs": "NFS storage",
    "monitoring": "Monitoring",
}
# Log lines the e2e self-test pushes carry this host name, so its queries see only them.
SELFTEST_HOST = "mmi-selftest"
DURATION = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# Log rules that look back a week are costlier, so their group runs less often.
TREND_GROUP = "Log trends"
TREND_INTERVAL = "15m"
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
    logs = logs_on(spec)
    rules = []
    for entry in CATALOGUE + (LOG_CATALOGUE if logs else []):
        if entry["module"] not in present or entry["id"] in disabled:
            continue
        if entry.get("requires") == "logs" and not logs:
            continue
        rule = dict(entry)
        over = overrides.get(entry["id"], {})
        rule["tiers"] = dict(entry["tiers"], **(over.get("tiers") or {}))
        rule["tiers"] = {k: v for k, v in rule["tiers"].items() if v is not None}
        rule["for"] = over.get("for", entry["for"])
        if "source" in entry:
            rule["datasource"] = "loki"
            rule["expr"] = logql_for(rule, spec)
            rule["logs_query"] = lines_query(rule, spec)
        else:
            rule["datasource"] = "prometheus"
            if "%(" in rule["expr"]:
                rule["expr"] = rule["expr"] % params
        if rule["tiers"]:
            rules.append(rule)
    return rules


# ------------------------------------------------------------------ logql

def logs_on(spec):
    """Ansible may hand booleans over as strings."""
    return str(spec.get("loki", False)).strip().lower() in ("true", "1", "yes")


def seconds(duration):
    return int(duration[:-1]) * DURATION[duration[-1]]


def log_params(spec):
    params = spec.get("params") or {}
    return {"db_port": re.escape(str(params.get("db_port", 5432))),
            "data_dir": re.escape(str(params.get("data_dir", "/var/opt/mattermost/data")))}


def filled(regex, spec):
    return regex % log_params(spec) if "%(" in regex else regex


def q(text):
    """A LogQL string. Backticks keep regexes readable; fall back to an escaped string."""
    return "`%s`" % text if "`" not in text else json.dumps(text)


def selector(rule, selftest=False, host=None):
    matchers = ['service_name="%s"' % rule["source"]] if rule.get("source") else []
    if selftest or host:
        matchers.append('host_name="%s"' % (SELFTEST_HOST if selftest else host))
    return "{%s}" % ", ".join(matchers or ['host_name=~".+"'])


def pipeline(rule, spec):
    """Line filters first (cheap), then the parser, then exact field filters."""
    parts = []
    match = [(f, filled(r, spec)) for f, r in rule.get("match", [])]
    exclude = [(f, filled(r, spec)) for f, r in rule.get("exclude", [])]
    for field, regex in match:
        if field == "line":
            parts.append("|~ " + q(regex))
        elif rule.get("parser") == "json":
            # A prefilter on the raw line, so Loki only parses lines that could match.
            parts.append("|~ " + q('"%s":\\s*"(%s)"' % (field, regex)))
    for field, regex in exclude:
        if field == "line":
            parts.append("!~ " + q(regex))
    if rule.get("kind") in ("baseline", "new"):
        for regex in spec.get("log_ignore") or []:
            parts.append("!~ " + q(regex))
    if rule.get("parser") == "json":
        parts.append('| json | __error__=""')
    for field, regex in match:
        if field != "line":
            parts.append("| %s=~%s" % (field, q(regex)))
    for field, regex in exclude:
        if field != "line":
            parts.append("| %s!~%s" % (field, q(regex)))
    return " ".join(parts)


def count_query(rule, spec, by=None, window=None, offset="", selftest=False):
    labels = ", ".join(["host_name"] + rule.get("by", []) if by is None else by)
    return "sum by (%s) (count_over_time(%s %s [%s]%s))" % (
        labels, selector(rule, selftest), pipeline(rule, spec), window or rule["window"],
        " offset " + offset if offset else "")


def logql_for(rule, spec, selftest=False):
    kind = rule.get("kind", "count")
    if kind == "count":
        return count_query(rule, spec, selftest=selftest)
    if kind == "baseline":
        # Now against the same window a week ago. The smoothing stops a quiet site alerting
        # on 3 errors after 0, and a server with no history yet is judged on the smoothing alone.
        now = count_query(rule, spec, selftest=selftest)
        past = count_query(rule, spec, offset="7d", selftest=selftest)
        smooth = spec.get("log_smoothing", 20)
        return "(%s / (%s + %s)) or (%s / %s)" % (now, past, smooth, now, smooth)
    if kind == "new":
        by = rule.get("by", [])
        now = count_query(rule, spec, by=by, selftest=selftest)
        week = count_query(rule, spec, by=by, window="7d", offset=rule["window"], selftest=selftest)
        return "%s unless %s" % (now, week)
    if kind == "silent":
        names = [s["name"] for s in spec.get("servers", [])]
        expr = 'sum by (host_name) (count_over_time({host_name=~"%s"} [%s]))' % (
            "|".join(re.escape(n) for n in names), rule["window"])
        for name in names:
            expr += ' or label_replace(vector(0), "host_name", "%s", "", "")' % name
        return expr
    raise ValueError("unknown kind %s" % kind)


def history_gate(rule):
    """Only judge "new" errors once Loki holds a week of this source's logs."""
    return "sum(count_over_time(%s [1h] offset 7d))" % selector(rule)


def lines_query(rule, spec):
    """The log query behind an alert, for its description: paste it into Explore."""
    if rule.get("kind") == "silent":
        return '{host_name="{{ $labels.host_name }}"}'
    # "new" alerts are per message, not per server, so they carry no host_name.
    sel = selector(rule, host=None if rule.get("kind") == "new" else "{{ $labels.host_name }}")
    return ("%s %s" % (sel, pipeline(rule, spec))).strip()


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


def query(ref, expr, rule, spec):
    if rule["datasource"] == "loki":
        window = seconds(rule.get("window", "15m"))
        return {"refId": ref, "relativeTimeRange": {"from": window, "to": 0},
                "datasourceUid": spec.get("loki_datasource_uid", "loki"),
                "model": {"expr": expr, "queryType": "instant", "editorMode": "code", "refId": ref,
                          "intervalMs": 60000, "maxDataPoints": 43200}}
    return {"refId": ref, "relativeTimeRange": {"from": 900, "to": 0},
            "datasourceUid": spec.get("datasource_uid", "prometheus"),
            "model": {"expr": expr, "instant": True, "range": False, "refId": ref,
                      "intervalMs": 15000, "maxDataPoints": 43200}}


def rule_json(rule, tier, evaluator, spec):
    uid = ("mmi-%s-%s" % (rule["id"], tier[:4]))[:40]
    threshold = evaluator["params"]
    shown = (" %s %s" % ({"gt": ">", "lt": "<"}.get(evaluator["type"], "between"),
                         " and ".join("%g" % p for p in threshold)))
    data = [query("A", rule["expr"], rule, spec)]
    judged = "A"
    if rule.get("kind") == "new":
        # B: did this source log anything a week ago? Until it did, every error looks new.
        data.append(query("B", history_gate(rule), rule, spec))
        data.append({"refId": "M", "relativeTimeRange": {"from": 0, "to": 0}, "datasourceUid": "__expr__",
                     "model": {"type": "math", "expression": "$A * ($B > 0)", "refId": "M"}})
        judged = "M"
    data.append({"refId": "C", "relativeTimeRange": {"from": 0, "to": 0}, "datasourceUid": "__expr__",
                 "model": {"type": "threshold", "expression": judged, "refId": "C",
                           "conditions": [{"evaluator": evaluator}]}})
    description = "%s\n\nThreshold:%s%s. Tier: %s." % (rule["action"], shown, rule.get("unit", ""), tier)
    if rule.get("logs_query"):
        description += "\n\nThe lines, for Explore (Loki): %s" % rule["logs_query"]
    return {
        "uid": uid,
        "title": "%s [%s]" % (rule["title"], tier),
        "condition": "C",
        "data": data,
        "noDataState": "OK",
        "execErrState": "Error",
        "for": rule["for"],
        "annotations": {
            "summary": rule["summary"],
            "description": description,
        },
        "labels": {
            "severity": tier,
            "module": rule["module"],
            "mmi_rule": rule["id"],
            "signal": "logs" if rule["datasource"] == "loki" else "metrics",
            "deployment": spec.get("deployment", "mattermost"),
        },
        "isPaused": False,
    }


def group_of(rule):
    if rule.get("kind") in ("baseline", "new"):
        return TREND_GROUP, TREND_INTERVAL
    if rule["datasource"] == "loki":
        return "%s logs" % MODULE_NAMES[rule["module"]], "1m"
    return MODULE_NAMES[rule["module"]], "1m"


def build_alerts(spec):
    groups = {}
    count = {s: 0 for s in SEVERITIES}
    for rule in effective_rules(spec):
        for tier, evaluator in evaluators(rule["direction"], rule["tiers"]).items():
            groups.setdefault(group_of(rule), []).append(rule_json(rule, tier, evaluator, spec))
            count[tier] += 1
    doc = {
        "apiVersion": 1,
        "groups": [
            {"orgId": 1, "name": name, "folder": spec.get("folder", "Mattermost Implement"),
             "interval": interval, "rules": rules}
            for (name, interval), rules in sorted(groups.items())
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

    def alerts(self, title, w=24, h=8, labels=""):
        self.add({"type": "alertlist", "title": title,
                  "options": {"viewMode": "list", "groupMode": "default", "maxItems": 50, "sortOrder": 3,
                              "stateFilter": {"firing": True, "pending": True, "noData": False, "normal": False, "error": True},
                              "alertInstanceLabelFilter": labels, "folder": None, "showInstances": True}}, w, h)

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
    # min by (instance): a host counts once here even if several of its exporters are down.
    b.stat("Targets down", 'count(min by (instance) (up) == 0) or vector(0)', w=6, steps={"critical": 1},
           desc="Hosts with at least one exporter not answering. \"Up by job and server\" below shows which.")
    b.stat("Services not running", 'count(node_systemd_unit_state{state="active"} == 0) or vector(0)', w=6, steps={"critical": 1})
    b.stat("Firing critical alerts", 'count(ALERTS{severity="critical", alertstate="firing"}) or vector(0)', w=6,
           steps={"critical": 1}, desc="Grafana-managed alerts are listed in the panel below.")
    b.stat("Servers", 'count(up{job="node"})', w=6)
    b.alerts("Firing and pending alerts")
    if logs_on(spec):
        b.alerts("From the logs", h=6, labels='{signal="logs"}')
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
    # Empty unless a primary feeds a read replica.
    b.timeseries("Replica lag", [("pg_replication_lag_seconds{%s}" % sel, "{{instance}}")], unit="s",
                 steps=tiers_of("pg-replication-lag", spec))
    b.timeseries("WAL held for replicas", [("pg_replication_slots_pg_wal_lsn_diff{%s}" % sel, "{{instance}} slot {{slot_name}}"),
                                           ("pg_settings_max_slot_wal_keep_size_bytes{%s} > 0" % sel, "{{instance}} limit")], unit="bytes")
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


def nfs_board(spec):
    b = Board("mmi-nfs", "NFS storage", spec, ["nfs"], [instance_var(spec, "node_uname_info")])
    sel = 'instance=~"$instance"'
    b.text("The top half is the NFS server (nfsd). The bottom half is what the servers mounting the share see, "
           "so pick an app server in the Server list to look at its side of the same traffic.", h=2)
    b.row("The NFS server")
    b.stat("nfsd threads", "max(node_nfsd_server_threads{%s})" % sel, w=6)
    b.stat("Share read", "sum(rate(node_nfsd_disk_bytes_read_total{%s}[5m]))" % sel, unit="Bps", w=6)
    b.stat("Share written", "sum(rate(node_nfsd_disk_bytes_written_total{%s}[5m]))" % sel, unit="Bps", w=6)
    b.stat("Stale handles (15m)", "sum(increase(node_nfsd_file_handles_stale_total{%s}[15m])) or vector(0)" % sel,
           w=6, steps=tiers_of("nfs-stale-handles", spec))
    b.timeseries("Share throughput", [("rate(node_nfsd_disk_bytes_read_total{%s}[5m])" % sel, "{{instance}} read"),
                                      ("rate(node_nfsd_disk_bytes_written_total{%s}[5m])" % sel, "{{instance}} written")],
                 unit="Bps")
    b.timeseries("Calls per second by operation",
                 [('topk(10, sum by (method) (rate(node_nfsd_requests_total{proto="4",%s}[5m])))' % sel, "{{method}}")],
                 unit="ops", desc="NFSv4 operations the server handled. v3 is switched off by the nfs role.")
    b.timeseries("RPC errors", [("sum by (instance, error) (rate(node_nfsd_rpc_errors_total{%s}[5m]))" % sel, "{{instance}} {{error}}")],
                 unit="ops", steps=tiers_of("nfs-rpc-errors", spec),
                 desc="auth means a client the export list doesn't allow, or a mismatched sec= setting.")
    b.timeseries("Reply cache", [("rate(node_nfsd_reply_cache_hits_total{%s}[5m])" % sel, "{{instance}} hits"),
                                 ("rate(node_nfsd_reply_cache_misses_total{%s}[5m])" % sel, "{{instance}} misses")], unit="ops")
    b.row("The servers mounting it")
    per_mount = "sum by (instance, export) (rate(node_mountstats_nfs_operations_%s{%s}[5m]))"
    b.timeseries("Operation latency",
                 [("1000 * %s / clamp_min(%s, 1)" % (per_mount % ("request_time_seconds_total", sel),
                                                     per_mount % ("requests_total", sel)), "{{instance}} {{export}}")],
                 unit="ms", steps=tiers_of("nfs-mount-latency", spec))
    b.timeseries("Timeouts waiting for the server", [("%s * 60" % (per_mount % ("major_timeouts_total", sel)), "{{instance}} {{export}}")],
                 unit="cpm", steps=tiers_of("nfs-mount-timeouts", spec),
                 desc="A major timeout means the client gave up waiting and started again. Uploads hang while this happens.")
    b.timeseries("Retransmissions", [("100 * rate(node_nfs_rpc_retransmissions_total{%s}[5m]) / clamp_min(rate(node_nfs_rpcs_total{%s}[5m]), 1)" % (sel, sel), "{{instance}}")],
                 unit="percent", steps=tiers_of("nfs-retransmits", spec))
    b.timeseries("Client throughput by mount",
                 [("sum by (instance, export) (rate(node_mountstats_nfs_total_read_bytes_total{%s}[5m]))" % sel, "{{instance}} {{export}} read"),
                  ("sum by (instance, export) (rate(node_mountstats_nfs_total_write_bytes_total{%s}[5m]))" % sel, "{{instance}} {{export}} written")],
                 unit="Bps")
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
    if "nfs" in present:
        boards["mmi-nfs.json"] = nfs_board(spec)
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
            "rule_queries": [rule_query(r, spec) for r in rules],
        },
    }


def rule_query(rule, spec):
    """What the e2e check needs to run a rule's query, and to self-test a log rule."""
    out = {"id": rule["id"], "module": rule["module"], "expr": rule["expr"], "datasource": rule["datasource"],
           # Log signatures only have data once the failure happens.
           "quiet": bool(rule.get("quiet")) or (rule["datasource"] == "loki" and rule.get("kind") != "silent")}
    if rule["datasource"] == "loki" and rule.get("kind", "count") == "count" and rule.get("examples"):
        out["selftest_expr"] = logql_for(rule, spec, selftest=True)
        out["selftest_service"] = rule.get("source") or "journal"
        out["examples"] = rule["examples"]
    return out


def catalogue_markdown(heading="###", suffix=""):
    """Per-module tables for docs/alerts.md and the README. tests/ checks both match."""
    out = []
    for module in ["host", "site", "mattermost", "postgresql", "nginx", "opensearch", "rtcd", "nfs", "monitoring"]:
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
    for module in ["host", "mattermost", "postgresql", "nginx", "opensearch", "monitoring"]:
        rules = [r for r in LOG_CATALOGUE if r["module"] == module]
        if not rules:
            continue
        out += ["%s %s log checks" % (heading, MODULE_NAMES[module]), "",
                "| Check | Watches for | Window | Info | Warning | Critical | For | What to do |",
                "|---|---|---|---|---|---|---|---|"]
        for r in rules:
            sign = ">" if r["direction"] == "above" else "<"
            cells = []
            for tier in SEVERITIES:
                value = r["tiers"].get(tier)
                cells.append("" if value is None else "%s %g%s" % (sign, value, r.get("unit", "")))
            out.append("| %s `%s` | %s | %s | %s | %s | %s |" % (
                r["title"], r["id"], watches_for(r).replace("|", "\\|"), r["window"], " | ".join(cells), r["for"],
                r["action"].replace("|", "\\|")))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def watches_for(rule):
    """One line for the docs: what a log check matches, in its own terms."""
    def part(field, regex, negate=False):
        regex = filled(regex, {})
        if field == "line":
            return "line %s `%s`" % ("doesn't match" if negate else "matches", regex)
        return "`%s` %s `%s`" % (field, "isn't" if negate else "is", regex)
    source = "`%s` logs" % rule["source"] if rule.get("source") else "any service's logs"
    kind = rule.get("kind", "count")
    if kind == "silent":
        return "a server in the estate that sent no log lines at all"
    what = " and ".join([part(f, r) for f, r in rule.get("match", [])]
                        + [part(f, r, True) for f, r in rule.get("exclude", [])])
    text = "%s where %s" % (source, what)
    if rule.get("by"):
        text += ", per %s" % ", ".join("`%s`" % b for b in rule["by"])
    if kind == "baseline":
        text += ", as a multiple of the same window a week earlier"
    if kind == "new":
        text += ", not seen in the 7 days before"
    return text


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
