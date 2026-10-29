"""Screenshot Grafana and the installer previews for the README.

Run after `KEEP=true make e2e`, from a Playwright container on the e2e network:

  docker run --rm --network mmd-e2e -v "$PWD:/repo:ro" -v "$PWD/docs/images:/out" \
    mcr.microsoft.com/playwright/python:v1.64.1-noble \
    bash -c 'pip install -q --break-system-packages playwright==1.64.1 && python3 /repo/tests/e2e/screenshots.py'

Use a current Playwright: Grafana 13's front end doesn't load in older Chromium builds.
"""
import re, sys, time
from playwright.sync_api import sync_playwright

GRAFANA = "http://172.30.10.15:3000"
password = re.search(r"grafana_admin_password:\s*(\S+)", open("/repo/tests/e2e/answers/common.yml").read()).group(1)

DASHBOARDS = [
    ("grafana-overview", "/d/mmi-overview?orgId=1&from=now-1h&to=now&kiosk", 1600, 1500),
    ("grafana-servers", "/d/mmi-host?orgId=1&from=now-1h&to=now&kiosk", 1600, 1700),
    ("grafana-mattermost", "/d/mmi-mattermost?orgId=1&from=now-1h&to=now&kiosk", 1600, 1500),
    ("grafana-postgresql", "/d/mmi-postgresql?orgId=1&from=now-1h&to=now&kiosk", 1600, 1350),
    ("grafana-opensearch", "/d/mmi-opensearch?orgId=1&from=now-1h&to=now&kiosk", 1600, 1000),
    ("grafana-calls", "/d/mmi-calls?orgId=1&from=now-1h&to=now&kiosk", 1600, 1300),
    ("grafana-nginx", "/d/mmi-nginx?orgId=1&from=now-1h&to=now&kiosk", 1600, 800),
]
PAGES = [
    ("grafana-alert-rules", "/alerting/list?view=list&search=namespace:%22Mattermost%20Implement%22", 1600, 1200),
    ("grafana-alert-rule-detail", "/alerting/grafana/mmi-disk-used-warn/view?tab=details", 1600, 1100),
    ("grafana-contact-points", "/alerting/notifications", 1600, 700),
    ("grafana-notification-policies", "/alerting/routes", 1600, 700),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1, color_scheme="dark")
    page = ctx.new_page()
    page.goto(GRAFANA + "/login")
    page.fill('input[name="user"]', "admin")
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(r".*/(\?.*)?$|.*/home.*|.*/d/.*"), timeout=30000)
    for name, path, w, h in DASHBOARDS + PAGES:
        page.set_viewport_size({"width": w, "height": h})
        page.goto(GRAFANA + path)
        page.wait_for_load_state("networkidle")
        time.sleep(5)
        page.screenshot(path="/out/%s.png" % name, full_page=False)
        print("saved", name)
    for name in ("wrapper-preview", "source-check-preview"):
        page.set_viewport_size({"width": 1060, "height": 1200})
        page.goto("file:///repo/tests/render/out/%s.html" % name)
        time.sleep(1)
        page.locator(".win").screenshot(path="/out/installer-%s.png" % name.replace("-preview", ""))
        print("saved installer", name)
    browser.close()
