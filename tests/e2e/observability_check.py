"""Run on the monitoring server after the estate is up.

Every alert rule's query must return data from the live exporters, unless the rule
is marked quiet (it only has data once there is traffic or a licence). This is what
catches a wrong metric name, which would otherwise leave an alert silently dead.
"""

import json
import re
import sys
import urllib.parse
import urllib.request

PROM = "http://127.0.0.1:9090"
GRAFANA = "http://127.0.0.1:3000"


def get(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def main():
    summary = json.load(open("/var/lib/mattermost-implement/observability.json"))
    failures = 0
    print("%-5s %-26s %s" % ("", "rule", "series"))
    for rule in summary["rule_queries"]:
        data = get(PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": rule["expr"]}))
        count = len(data["data"]["result"]) if data.get("status") == "success" else -1
        if count > 0:
            mark = "PASS"
        elif count == 0 and rule["quiet"]:
            mark = "n/a"
        else:
            mark = "FAIL"
            failures += 1
        print("%-5s %-26s %s" % (mark, rule["id"], count if count >= 0 else data.get("error")))

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
        print("  [%s, %s] %s %s" % (sev, a.get("state"), a["labels"].get("alertname"), a["labels"].get("instance", "")))
        if sev == "critical":
            failures += 1

    print("\n%s" % ("All alert queries return data and nothing critical is firing" if not failures else "%d problems" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
