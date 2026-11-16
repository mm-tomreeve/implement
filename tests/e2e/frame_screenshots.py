"""Render captured installer output and the wizard shots as framed images.

  python3 frame_screenshots.py DIR [--wizard]

DIR holds preflight.raw, estate.raw, wrapper.raw, source.raw and help.raw: install.sh output
captured from a pty with TERM=xterm-256color COLORTERM=truecolor. With --wizard it also
frames the raw shots from wizard_screenshots.py. Needs playwright and Google Chrome.
"""
import os, re, html, base64, sys
from playwright.sync_api import sync_playwright
S = sys.argv[1]  # directory holding the captured *.raw installer output
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "images") + "/"
WIZ = os.environ.get("RAW_DIR", "/tmp/wizard-raw") + "/"
FG = {30:"#484f58",31:"#ff7b72",32:"#56d364",33:"#e3b341",34:"#79c0ff",35:"#d2a8ff",36:"#76e3ea",37:"#c9d1d9"}
def sgr_to_html(text):
    text = text.replace("\r", "")
    out = []; st = {"b":False,"d":False,"fg":None,"bg":None}
    def span(chunk):
        if not chunk: return ""
        css = []
        if st["b"]: css.append("font-weight:700")
        if st["d"]: css.append("opacity:.6")
        if st["fg"]: css.append("color:"+st["fg"])
        if st["bg"]: css.append("background:"+st["bg"]+";display:inline-block;height:1.5em;vertical-align:top")
        c = html.escape(chunk)
        if chunk.startswith("▌") and st["bg"]: css.append("background:linear-gradient(90deg,#ffbc1f 50%,rgb(30,50,92) 50%);color:transparent")
        return '<span style="%s">%s</span>' % (";".join(css), c) if css else c
    pos = 0
    for m in re.finditer(r"\x1b\[([0-9;]*)m", text):
        out.append(span(text[pos:m.start()])); pos = m.end()
        p = [int(x) if x else 0 for x in m.group(1).split(";")]
        i = 0
        while i < len(p):
            n = p[i]
            if n == 0: st.update(b=False,d=False,fg=None,bg=None)
            elif n == 1: st["b"]=True
            elif n == 2: st["d"]=True
            elif 30 <= n <= 37: st["fg"]=FG[n]
            elif n in (38,48) and p[i+1]==2:
                col = "rgb(%d,%d,%d)" % tuple(p[i+2:i+5]); st["fg" if n==38 else "bg"]=col; i += 4
            elif n in (38,48) and p[i+1]==5:
                k = p[i+2]; pal = {17:"rgb(30,50,92)",214:"rgb(255,188,31)",231:"#fff"}.get(k,"#c9d1d9")
                st["fg" if n==38 else "bg"]=pal; i += 2
            i += 1
    out.append(span(text[pos:]))
    return "".join(out)

BASE = """<style>
html,body{margin:0;background:transparent}
.canvas{display:inline-block;padding:36px 40px 44px;background:linear-gradient(135deg,#e9edf5,#d8dfee)}
.win{border-radius:14px;overflow:hidden;box-shadow:0 18px 50px rgba(20,32,70,.28),0 2px 6px rgba(20,32,70,.18);display:inline-block}
.bar{height:38px;display:flex;align-items:center;padding:0 16px;gap:8px;background:#21262d;color:#9aa4b2;font:13px -apple-system,Helvetica,Arial,sans-serif}
.bar i{width:12px;height:12px;border-radius:50%;display:block}
.bar b{font-weight:500;margin-left:10px;color:#c3cad4}
pre{margin:0;padding:22px 26px 26px;background:#0d1117;color:#c9d1d9;font:13.5px/1.5 "SF Mono",Menlo,Consolas,monospace;white-space:pre}
.shot{display:block}
</style>"""
def term_page(title, ansi, note=""):
    return BASE + '<div class="canvas"><div class="win"><div class="bar"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i><i style="background:#27c93f"></i><b>%s</b></div><pre>%s</pre></div></div>' % (html.escape(title), sgr_to_html(ansi))
def img_page(path, title=""):
    b64 = base64.b64encode(open(path,"rb").read()).decode()
    return BASE + '<div class="canvas"><div class="win" style="background:#fff"><div class="bar" style="background:#e8ebf0;color:#6b7280"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i><i style="background:#27c93f"></i><b style="color:#4b5563">%s</b></div><img class="shot" style="width:%dpx" src="data:image/png;base64,%s"></div></div>' % (html.escape(title), IMGW, b64)
def read(n):
    t = open("%s/%s.raw" % (S, n)).read().replace("(f9c7717-dirty) ", "").replace("/scr2/", "")
    return re.sub(r"(  \d+\.\d+\.\d+[^\x1b\r\n]*?) *\x1b\[0m", lambda m: m.group(1).ljust(63) + "\x1b[0m", t)
def squeeze(t):
    strip = lambda x: re.sub(r"\x1b\[[0-9;]*m", "", x)
    L = t.split("\n")
    a = next(i for i,l in enumerate(L) if "\u2501\u2501\u2501 Mattermost \u2501" in strip(l))
    b = next(i for i,l in enumerate(L) if "\u2501\u2501\u2501 Monitoring and logs" in strip(l))
    c = next(i for i,l in enumerate(L) if "Checking the installer sources" in strip(l))
    d = next(i for i,l in enumerate(L) if "Checking prerequisites" in strip(l))
    L = L[:a] + ["\x1b[2m    \u2026 the Mattermost, PostgreSQL and Nginx questions, all answered with the defaults \u2026\x1b[0m", ""] + L[b:]
    return "\n".join(L)
def between(t, start=None, end=None):
    if start: t = t[t.index(start):]
    if end: t = t[:t.index(end)]
    return t.rstrip()+"\n"
# find the start of the WARNING line in each (skip any leading junk)
def trim(t, end=None):
    t = t[t.index("\x1b[38;2;255;188;31m\x1b[1mWARNING") if "\x1b[38;2;255;188;31m\x1b[1mWARNING" in t else 0:]
    if end and end in t: t = t[:t.index(end)]
    return t.rstrip()+"\n"
scenes = [
 ("installer-wrapper", "sudo ./install.sh", squeeze(trim(read("wrapper")))),
 ("installer-source-check", "sudo ./install.sh  ·  GitHub blocked, so it asks for a local folder", trim(read("source"), "\x1b[38;2;255;188;31m\x1b[1m━")) ,
 ("installer-estate", "sudo ./install.sh --estate my-estate.yml --estate-secrets estate-secrets.yml", trim(read("estate"))),
 ("installer-preflight", "sudo ./install.sh --estate my-estate.yml --preflight", trim(read("preflight"))),
 ("installer-help", "./install.sh --help", read("help").strip()+"\n"),
]
shots = [("wizard-1-deployment","Architecture wizard"),("wizard-2-recommendation","Architecture wizard"),
         ("wizard-3-addresses","Architecture wizard"),("wizard-4-plan","Architecture wizard"),("wizard-5-firewall","Architecture wizard")]
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    ctx = b.new_context(viewport={"width":1500,"height":900}, device_scale_factor=2)
    pg = ctx.new_page()
    for name, title, ansi in scenes:
        if name == "installer-source-check":
            # keep up to and including the 'Use these installers' answer
            i = ansi.find("Checking prerequisites")
            ansi = ansi[:ansi.rfind("\n", 0, i)] + "\n" if i > 0 else ansi
        pg.set_content(term_page(title, ansi)); pg.wait_for_timeout(200)
        pg.locator(".canvas").screenshot(path=OUT+name+".png", omit_background=True); print("saved", name)
    for name, title in (shots if '--wizard' in sys.argv else []):
        src = WIZ+name+".png"
        from struct import unpack
        d = open(src,"rb").read(); w = unpack(">I", d[16:20])[0]
        IMGW = w // 2 if w > 1500 else w // 1.5  # wizard captured at 1.5x
        IMGW = int(IMGW)
        pg.set_content(img_page(src, title)); pg.wait_for_timeout(300)
        pg.locator(".canvas").screenshot(path=OUT+name+".png", omit_background=True); print("framed", name)
    b.close()
