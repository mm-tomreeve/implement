"""Capture the architecture wizard as raw screenshots and write estate.yml.

Needs playwright and Google Chrome. Output goes to RAW_DIR (default /tmp/wizard-raw);
run frame_screenshots.py --wizard afterwards to frame them into docs/images.
"""
import sys, time
from playwright.sync_api import sync_playwright
import os
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "docs", "images") + "/"
RAW = os.environ.get("RAW_DIR", "/tmp/wizard-raw") + "/"
os.makedirs(RAW, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_context(viewport={"width":1100,"height":900}, device_scale_factor=1.5).new_page()
    pg.goto("file://" + ROOT + "/wizard/index.html")
    pg.fill("#f-name","chat-prod"); pg.fill("#f-site","https://chat.acme.example")
    pg.select_option("#f-tier","enterprise"); pg.fill("#f-seats","2000")
    pg.check("#f-ha"); pg.fill("#f-app-servers","2"); pg.fill("#f-db-replicas","1")
    pg.check("#f-search-opensearch"); pg.check("#f-calls-rtcd")
    pg.select_option("#f-tls","provided")
    pg.wait_for_timeout(500)
    pg.locator("#step-1").screenshot(path=RAW+"wizard-1-deployment.png")
    # A lab asks the same questions as a tick list, so step 1 looks different
    # enough to be worth its own shot. Taken before the estate run goes on, and
    # the mode is put back so nothing below sees a lab.
    pg.check('input[name="deployment"][value="lab"]'); pg.wait_for_timeout(400)
    pg.locator("#group-lab-components").screenshot(path=RAW+"wizard-1-lab-components.png")
    pg.check('input[name="deployment"][value="estate"]'); pg.wait_for_timeout(400)
    pg.click("#to-step-2"); pg.wait_for_timeout(3000)
    pg.locator("#step-2").screenshot(path=RAW+"wizard-2-recommendation.png")
    pg.click("#to-step-3"); pg.wait_for_timeout(500)
    ins = pg.locator("#address-list input")
    print("inputs", ins.count())
    pg.locator("#step-3").screenshot(path=RAW+"_x.png")
    for i in range(1, ins.count(), 2):
        ins.nth(i).fill("10.10.1.%d" % (11+i//2))
    pg.locator("#step-3").screenshot(path=RAW+"wizard-3-addresses.png")
    pg.click("#to-step-4"); pg.wait_for_timeout(800)
    pg.wait_for_timeout(500)
    pg.locator("#step-4").screenshot(path=RAW+"wizard-4-plan.png")
    fw = pg.locator("#step-4 details", has_text="Firewall requirements")
    fw.locator("summary").first.click(); pg.wait_for_timeout(400)
    bb = fw.bounding_box()
    sy = pg.evaluate("window.scrollY")  # bounding_box is viewport-relative, clip is page-relative
    pg.screenshot(path=RAW+"wizard-5-firewall.png", full_page=True, clip={"x":bb["x"],"y":bb["y"]+sy,"width":bb["width"],"height":min(bb["height"],820)})
    open(RAW + 'estate.yml','w').write(pg.input_value('#yaml-out'))
    b.close()
