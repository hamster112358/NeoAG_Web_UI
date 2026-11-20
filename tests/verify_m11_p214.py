#!/usr/bin/env python3
"""M11 P2-14 battery: visual consistency + accessibility pass.

No browser automation exists on this host (matrix-stated method), so
every check below is a SERVED-MARKUP / SERVED-CSS probe: the layering,
focus, typography and responsive contracts are written so a static
probe can pin them, and the final visual sweep is the operator's
(stated at the end of this report).

Pinned behaviour:
  * keyboard focus is visible everywhere (:focus-visible ring; the one
    control that removed outlines gets its ring pinned back);
  * a keyboard skip link reaches #work-body and is translated;
  * the toast can no longer cover the approval card / composer: it is
    corner-parked, its z-layer is strictly BELOW the modal dialog and
    strictly below the click-transient context menu;
  * one toast element (one live region), one modal system (one
    role=dialog), no internal/debug badges in the default markup;
  * zh typography: html[lang=zh] rule exists, the sans stack ends in
    CJK families, and documentElement.lang is synced at runtime;
  * responsive: viewport meta, the 760px tier exists AFTER the 1000px
    one (cascade order), and the served CSS equals the on-disk file.
Run: python3 tests/verify_m11_p214.py   (isolated :8815)
"""
import atexit
import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = 8815
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

APPJS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSSF = ROOT / "frontend" / "styles.css"
CSS = CSSF.read_text(encoding="utf-8")

srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p214.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p214-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p214-taskq"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p214.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree("/tmp/neoag-m11p214-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p214-taskq", ignore_errors=True)


atexit.register(_cleanup)


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


def raw_get(path, timeout=20):
    # everything (static included) rides behind Basic auth on this server
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        headers={"Authorization": AUTH_HEADER})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def api_ok():
    try:
        return raw_get("/api/bootstrap")[0] == 200
    except Exception:
        return False


up = False
for _ in range(80):
    try:
        if api_ok():
            up = True
            break
    except Exception:
        pass
    time.sleep(0.5)
if not up:
    print("FAIL  server never came up"); sys.exit(1)


def css_block(sel):
    """the text of the CSS rule whose selector starts with sel"""
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else ""


def z_of(sel):
    # a selector may repeat (e.g. inside a media query); cascade order
    # means the LAST block that actually defines z-index wins.
    zs = [int(m.group(1)) for blk in
          re.findall(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
          for m in [re.search(r"z-index:\s*(\d+)", blk)] if m]
    return zs[-1] if zs else None


# ---------- 1. keyboard focus ----------
check("V1 a global :focus-visible ring exists (WCAG 2.4.7)",
      ":focus-visible" in CSS and "outline: 2px solid var(--accent)"
      in css_block(":focus-visible"))
check("V2 the one control that removes outlines (chat-input, "
      "outline:none) gets its ring pinned back",
      "#chat-input:focus-visible" in CSS
      and "outline: none" in css_block("#chat-input"))
check("V3 focus language is the SAME everywhere (single token, not "
      "per-control ad-hoc colors)",
      CSS.count("outline: 2px solid var(--accent)") >= 2
      and not re.search(r":focus-visible[^{]*\{[^}]*outline:\s*(?!none)\d+px solid (?!var\(--accent\))", CSS))

# ---------- 2. skip link ----------
check("V4 skip link present, first focusable, targets the work pane",
      '<a id="skip-link" href="#work-body"' in INDEX
      and INDEX.index("skip-link") < INDEX.index("<header"))
check("V5 skip target is focusable so browsers actually move focus "
      "there", 'id="work-body" tabindex="-1"' in INDEX)
check("V6 skip link is off-screen until keyboard focus",
      "left: -9999px" in css_block("#skip-link")
      and "#skip-link:focus" in CSS)
check("V7 skip link is translated (data-i18n + both dictionaries)",
      'data-i18n="skip.main"' in INDEX
      and '"skip.main"' in APPJS.split("  zh: {", 1)[0]
      and '"skip.main"' in APPJS.split("  zh: {", 1)[1])

# ---------- 3. toast: no more covering the approval card ----------
tb = css_block(".toast")
check("V8 toast no longer parks bottom-CENTER over the composer / "
      "newest approval card",
      "translateX(-50%)" not in tb and "right: 20px" in tb
      and "bottom: 20px" in tb)
zt, zd, zm = z_of(".toast"), z_of("#dialog-root"), z_of(".conv-menu")
check("V9 z-layer is PINNED: toast strictly below the modal dialog",
      zt is not None and zd is not None and zt < zd, f"{zt} vs {zd}")
check("V10 the click-transient context menu still rides above both",
      zm is not None and zd is not None and zm > zd > zt,
      f"menu={zm} dialog={zd} toast={zt}")
check("V11 exactly ONE toast element, exposed as a live region",
      INDEX.count('class="toast') == 1 and 'role="status"' in INDEX)
check("V12 narrow widths re-adapt the toast (span, not a skewed "
      "floating card)",
      ".toast { left: 12px; right: 12px" in CSS)

# ---------- 4. one modal system, no internal badges by default ----------
check("V13 exactly one modal overlay (role=dialog) in the markup",
      INDEX.count('role="dialog"') == 1)
check("V14 the attention badge starts hidden (no internal state on "
      "screen by default)",
      'attn-badge hidden' in INDEX)
lowall = (APPJS + INDEX + CSS).lower()
check("V15 no debug/raw-dump affordance anywhere in the shipped "
      "frontend",
      "debug" not in lowall and "raw json" not in lowall)

# ---------- 5. zh typography ----------
check("V16 html[lang=zh] typographic rule exists",
      re.search(r'html\[lang="zh"\][^{]*\{[^}]*line-height', CSS))
check("V17 the shared sans stack ends in CJK families (font fallback "
      "DERIVED from the stack, not per-widget font lists)",
      re.search(r'"Noto Sans CJK SC".*sans-serif', CSS, re.S))
check("V18 html lang is synced to the LIVE language at runtime",
      "document.documentElement.lang" in APPJS)
check("V19 static default lang matches the code default (no "
      "pre-JS mismatch flash)",
      '<html lang="zh">' in INDEX and 'let LANG = "zh"' in APPJS)

# ---------- 6. responsive ----------
check("V20 viewport meta present", 'name="viewport"' in INDEX)
i1000 = CSS.index("@media (max-width: 1000px)")
i760 = CSS.index("@media (max-width: 760px)")
check("V21 the 760px tier exists AFTER the 1000px one (cascade "
      "order makes it win)", i760 > i1000)
check("V22 the 760 tier stacks the shell and re-homes dialog+toast",
      all(k in CSS[i760:] for k in
          ("grid-template-rows", "#dialog-root", ".toast")))

# ---------- 7. SERVED equals ON-DISK ----------
s, css_served = raw_get("/static/styles.css")
check("V23 SERVED /static/styles.css equals the on-disk file (the "
      "probes above measured the shipped bytes)",
      s == 200 and css_served == CSS)
s, idx_served = raw_get("/")
check("V24 SERVED / carries the skip link + lang attribute",
      s == 200 and 'id="skip-link"' in idx_served
      and '<html lang="zh">' in idx_served)

en_txt = APPJS.split("  en: {", 1)[1].split("  zh: {", 1)[0]
zh_txt = APPJS.split("  zh: {", 1)[1].split("};", 1)[0]
en_keys = set(re.findall(r'^\s*"([^"]+)":', en_txt, re.M))
zh_keys = set(re.findall(r'^\s*"([^"]+)":', zh_txt, re.M))
check("V25 en/zh dictionaries carry the identical key set "
      "(this pass added skip.main to BOTH)",
      en_keys == zh_keys,
      f"en-only={sorted(en_keys - zh_keys)[:6]} "
      f"zh-only={sorted(zh_keys - en_keys)[:6]}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if not FAIL:
    print("OPERATOR VISUAL PASS (stated method, no browser automation "
          "on this host): open the app, (1) press Tab from the page "
          "top - the first stop is the skip link at the top-left; "
          "(2) Tab through header/composer/cards - every stop shows "
          "the blue ring; (3) trigger any error toast and confirm it "
          "appears bottom-RIGHT, clear of the composer and approval "
          "card; (4) narrow the window under ~760px - toast spans, "
          "alerts stack, dialogs stay inside the viewport; (5) switch "
          "language - text stays readable, skip link translates.")
sys.exit(1 if FAIL else 0)
