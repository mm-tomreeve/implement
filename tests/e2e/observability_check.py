"""Run on the monitoring server after the estate is up.

Every alert rule's query must return data from the live exporters, unless the rule
is marked quiet (it only has data once there is traffic or a licence). This is what
catches a wrong metric name, which would otherwise leave an alert silently dead.

Log rules run against Loki. Most are quiet (a signature only matches once the failure
happens), so for those this checks the LogQL parses and runs.

  observability_check.py              the checks above
  observability_check.py --selftest   also push every log rule's example lines into Loki
                                      (as host mmi-selftest) and check each rule's query
                                      finds them. Run it last: the real rules see those
                                      lines too, so alerts for mmi-selftest fire for a while.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROM = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
GRAFANA = "http://127.0.0.1:3000"


def get(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # Loki answers a bad query with 400 and a plain-text reason.
        return {"status": "error", "error": exc.read().decode("utf-8", "replace").strip()[:300]}


def run_query(rule, expr):
    base = LOKI + "/loki/api/v1/query?" if rule.get("datasource") == "loki" else PROM + "/api/v1/query?"
    data = get(base + urllib.parse.urlencode({"query": expr}))
    if data.get("status") != "success":
        return -1, data.get("error")
    return len(data["data"]["result"]), data["data"]["result"]


def selftest(summary):
    """Push each log rule's examples and check its query finds them."""
    tested = [r for r in summary["rule_queries"] if r.get("selftest_expr")]
    now = time.time_ns()
    streams = {}
    for i, rule in enumerate(tested):
        values = streams.setdefault(rule["selftest_service"], [])
        for j, line in enumerate(rule["examples"]):
            values.append([str(now + i * 1000 + j), line])
    body = {"streams": [{"stream": {"service_name": service, "host_name": "mmi-selftest", "deployment": "selftest"},
                         "values": sorted(values)} for service, values in streams.items()]}
    req = urllib.request.Request(LOKI + "/loki/api/v1/push", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=30).close()
    time.sleep(5)  # let the ingester make them queryable
    failures = 0
    print("\nLog rule self-test: example lines pushed as host mmi-selftest")
    for rule in tested:
        count, result = run_query(rule, rule["selftest_expr"])
        found = sum(float(s["value"][1]) for s in result) if count > 0 else 0
        mark = "PASS" if found >= len(rule["examples"]) else "FAIL"
        if mark == "FAIL":
            failures += 1
        print("%-5s %-26s %s of %d lines%s" % (mark, rule["id"], "%g" % found, len(rule["examples"]),
                                              "" if count >= 0 else " (%s)" % result))
    return failures


def main(argv):
    summary = json.load(open("/var/lib/mattermost-implement/observability.json"))
    failures = 0
    print("%-5s %-26s %-10s %s" % ("", "rule", "source", "series"))
    for rule in summary["rule_queries"]:
        count, result = run_query(rule, rule["expr"])
        if count > 0:
            mark = "PASS"
        elif count == 0 and rule["quiet"]:
            mark = "n/a"
        else:
            mark = "FAIL"
            failures += 1
        print("%-5s %-26s %-10s %s" % (mark, rule["id"], rule.get("datasource", "prometheus"),
                                       count if count >= 0 else result))

    targets = get(PROM + "/api/v1/targets?state=active")["data"]["activeTargets"]
    down = ["%s/%s" % (t["labels"]["job"], t["labels"].get("instance")) for t in targets if t["health"] != "up"]
    print("\nScrape targets: %d, down: %s" % (len(targets), ", ".join(down) or "none"))
    failures += len(down)

    secrets = open("/etc/mattermost-implement/secrets.yml").read()
    password = re.search(r"grafana_admin_password:\s*(\S+)", secrets).group(1).strip("'\"")
    # Pending counts too: a rule still inside its "for" window would fire minutes later.
    alerts = get(GRAFANA + "/api/prometheus/grafana/api/v1/alerts", "admin:" + password)["data"]["alerts"]
    active = [a for a in alerts if a.get("state", "").lower().split()[0] in ("alerting", "pending", "firing")]
    print("\nGrafana alerts firing or pending now: %d" % len(active))
    for a in active:
        sev = a["labels"].get("severity")
        where = a["labels"].get("instance") or a["labels"].get("host_name", "")
        print("  [%s, %s] %s %s" % (sev, a.get("state"), a["labels"].get("alertname"), where))
        if sev == "critical":
            failures += 1

    if "--selftest" in argv:
        failures += selftest(summary)

    print("\n%s" % ("All alert queries return data and nothing critical is firing" if not failures else "%d problems" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
