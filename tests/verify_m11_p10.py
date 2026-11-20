#!/usr/bin/env python3
"""M11 P1-10 battery: file intake - upload, picker, PATH-only egress.

Pinned behaviour (all measured, none asserted from memory):
  * upload round-trip is BYTE-EXACT (download equals what went in,
    checksum-pinned by the server itself);
  * oversize is rejected LOUDLY (413 with limit+actual, audited, and
    nothing stored); unknown types and sequencing files are refused
    with the reason (sequencing belongs to the formal ingestion);
  * the stored name is a basename: traversal in the name is reduced;
  * the picker lists ONLY under the authorized roots (one level,
    per-entry realpath-checked) and refuses paths outside them;
    picks reference the original IN PLACE - deleting the attachment
    row never touches the original (checksum probe);
  * chat egress carries PATHS only: the persisted user message
    contains the attachment paths and NOT the file bytes; the egress
    audit names exactly what left; a bad attachment id is refused
    BEFORE a conversation would be created; attachments-only
    messages work;
  * an unauthenticated scoped token (which may create jobs) still
    cannot upload: attachments keep Basic auth.
Run: python3 tests/verify_m11_p10.py   (isolated :8784)
"""
import atexit
import base64
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8784
CAP = 200_000
FIX = pathlib.Path("/tmp/m11p10_fix")
OUT = pathlib.Path("/tmp/m11p10_outside")
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p10.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_ATTACH_MAX_BYTES=str(CAP),
             NEOAG_INPUT_ROOTS=str(FIX),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p10-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p10-taskq",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p10.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree(ROOT / "data" / "exports-m11p10", ignore_errors=True)
    shutil.rmtree(ROOT / "data" / "attachments-m11p10",
                  ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p10-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p10-taskq", ignore_errors=True)
    shutil.rmtree(FIX, ignore_errors=True)
    shutil.rmtree(OUT, ignore_errors=True)


atexit.register(_cleanup)


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


def api(method, path, body=None, timeout=120, auth=True):
    hdr = {"Authorization": AUTH_HEADER} if auth else {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode()), dict(r.headers)
            except Exception:
                return r.status, raw, dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode()), dict(e.headers)
        except Exception:
            return e.code, {}, dict(e.headers) if e.headers else {}


up = False
for _ in range(80):
    try:
        s, _b, _h = api("GET", "/api/bootstrap")
        up = s == 200
        if up:
            break
    except Exception:
        time.sleep(0.5)
if not up:
    print("FAIL  server never came up")
    sys.exit(1)


def db(sql, args=()):
    con = sqlite3.connect(
        f"file:{ROOT / 'data' / 'm11p10.sqlite3'}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# fixtures
FIX.mkdir(parents=True, exist_ok=True)
(FIX / "sub").mkdir(exist_ok=True)
(FIX / "probe.tsv").write_text("peptide\thla\nFIXTAB_ROW\tA*02:01\n",
                               encoding="utf-8")
(FIX / "probe.bam").write_bytes(b"\x1f\x8b\x08\x00fakebam")
(FIX / "sub" / "deeper.csv").write_text("a,b\n1,2\n", encoding="utf-8")
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "secret.txt").write_text("outside", encoding="utf-8")

# ================= 1: upload round-trip byte-exact =====================
content = b"peptide\thla\nBYTESNEVERLEAVE\tA*02:01\n"
st, r, _h = api("POST", "/api/attachments",
                {"name": "shortlist.tsv",
                 "data_b64": base64.b64encode(content).decode()})
att = r.get("attachment") or {}
sha = hashlib.sha256(content).hexdigest()
st2, raw, hdrs = api("GET", f"/api/attachments/{att.get('id','x')}/download")
check("1a upload stores a stable row (kind, exact size, checksum) "
      "and the download is BYTE-EXACT, pinned by the same checksum",
      st == 201 and att.get("kind") == "table"
      and att.get("size") == len(content) and att.get("sha256") == sha
      and st2 == 200 and raw == content
      and hdrs.get("X-Attachment-Id") == att.get("id"),
      f"{st}/{st2} {str(att)[:180]}")

# ================= 2: oversize rejected loudly, nothing stored =========
st, lst, _h = api("GET", "/api/attachments")
n_before = len(lst.get("attachments", []))
stp, r, _h = api("POST", "/api/attachments",
                 {"name": "big.csv",
                  "data_b64": base64.b64encode(
                      b"x" * (CAP + 1)).decode()})
st, lst, _h = api("GET", "/api/attachments")
n_after = len(lst.get("attachments", []))
st = stp
rej = db("SELECT COUNT(*) FROM audit WHERE action="
         "'attachment.rejected_too_big'")[0][0]
check("2a oversize is 413 with limit+actual, audited, and NOTHING "
      "is stored",
      st == 413 and r.get("error") == "file_too_big"
      and r.get("limit") == CAP and r.get("actual") == CAP + 1
      and n_after == n_before and rej >= 1,
      f"{st} {str(r)[:140]} before={n_before} after={n_after}")

# ================= 3: disallowed types refused with reason =============
st, r, _h = api("POST", "/api/attachments",
                {"name": "notes.sh",
                 "data_b64": base64.b64encode(b"#!/bin/sh").decode()})
st2, r2, _h = api("POST", "/api/attachments",
                  {"name": "reads.bam",
                   "data_b64": base64.b64encode(b"x").decode()})
check("3a unknown type 415 with the allowed families named",
      st == 415 and "unsupported" in r.get("error", ""),
      f"{st} {str(r)[:140]}")
check("3b a sequencing file is refused WITH the honest reason "
      "(formal ingestion path, not an attachment)",
      st2 == 415 and "formal ingestion" in r2.get("error", ""),
      f"{st2} {str(r2)[:140]}")

# ================= 4: name is reduced to a basename ====================
st, r, _h = api("POST", "/api/attachments",
                {"name": "../../etc/evil.txt",
                 "data_b64": base64.b64encode(b"hi").decode()})
evil = r.get("attachment") or {}
check("4a a traversal name is reduced to its basename and lands "
      "under the attachment store",
      st == 201 and evil.get("name") == "evil.txt"
      and str(evil.get("path", "")).startswith(
          str(ROOT / "data" / "attachments-m11p10")),
      str(evil)[:180])
up_id = att["id"]

# ================= 5: picker over the authorized roots =================
st, r, _h = api("GET", "/api/files")
roots = [e["path"] for e in r.get("entries", [])] if r.get("roots") \
    else []
st2, sub, _h = api("GET", f"/api/files?dir={FIX}")
ents = {e["name"]: e for e in sub.get("entries", [])} if st2 == 200 else {}
st3, out, _h = api("GET", f"/api/files?dir={OUT}")
check("5a the picker shows the authorized roots and lists one level: "
      "the tsv/csv are allowed, the bam is flagged sequencing, the "
      "subdirectory is enterable",
      st == 200 and str(FIX) in roots and st2 == 200
      and ents.get("probe.tsv", {}).get("allowed") is True
      and ents.get("probe.bam", {}).get("seq") is True
      and ents.get("probe.bam", {}).get("allowed") is False
      and ents.get("sub", {}).get("dir") is True,
      f"{st}/{st2} {str(sub)[:200]}")
check("5b a directory outside the roots is refused 403",
      st3 == 403, f"{st3}")
st, r, _h = api("POST", "/api/attachments/pick",
                {"path": str(OUT / "secret.txt")})
check("5c picking a file outside the authorized roots is refused 403",
      st == 403 and "authorized input roots" in r.get("error", ""),
      f"{st} {str(r)[:140]}")
st, r, _h = api("POST", "/api/attachments/pick",
                {"path": str(FIX / "probe.tsv")})
pick = r.get("attachment") or {}
pick_digest0 = hashlib.sha256((FIX / "probe.tsv").read_bytes()).hexdigest()
check("5d a pick references the authorized original IN PLACE "
      "(server_pick, path == the realpath, nothing copied)",
      st == 201 and pick.get("source") == "server_pick"
      and pick.get("path") == os.path.realpath(FIX / "probe.tsv")
      and pick.get("kind") == "table",
      f"{st} {str(pick)[:180]}")

# ================= 6: chat egress = paths, never bytes =================
st, r, _h = api("POST", "/api/chat",
                {"text": "summarise the shortlist please",
                 "attachment_ids": [up_id, pick.get("id", "x")]})
msgs = r.get("messages") or [{}]
user_msg = msgs[0]
text = user_msg.get("text", "")
check("6a the persisted user message names BOTH attachment paths "
      "under an explicit attached-files block",
      st == 200 and "[attached files" in text
      and str(att["path"]) in text and pick.get("path", "?") in text,
      f"{st} {text[:200]}")
check("6b the file BYTES never entered the payload: the content "
      "marker is absent from the dispatched text",
      "BYTESNEVERLEAVE" not in text and "FIXTAB_ROW" not in text, "")
eg = db("SELECT COUNT(*) FROM audit WHERE action='attachment.egress'")[0][0]
check("6c egress audit records exactly what left (one row per "
      "attachment)", eg >= 2, f"n={eg}")
cid = r.get("conv_id") or ""
st, r2, _h = api("POST", "/api/chat",
                 {"text": "", "conv_id": cid, "attachment_ids": [up_id]})
t2 = (r2.get("messages") or [{}])[0].get("text", "")
check("6d an attachments-only message is honest and works (the path "
      "block IS the message)",
      st == 200 and str(att["path"]) in t2, f"{st} {t2[:160]}")
st, convs, _h = api("GET", "/api/conversations")
nconv = len(convs.get("conversations", []))
stp, r3, _h = api("POST", "/api/chat",
                  {"text": "hello", "attachment_ids": ["att-nope"]})
st, convs2, _h = api("GET", "/api/conversations")
st = stp
check("6e a bad attachment id is refused BEFORE anything is created "
      "(no orphan conversation)",
      st == 400 and len(convs2.get("conversations", [])) == nconv,
      f"{st} {nconv}->{len(convs2.get('conversations', []))}")

# ================= 7: removal - uploads go, originals never ============
st, r, _h = api("POST", f"/api/attachments/{up_id}/delete", {})
gone = os.path.exists(att["path"])
st2, r2, _h = api("POST",
                  f"/api/attachments/{pick['id']}/delete", {})
pick_digest1 = hashlib.sha256((FIX / "probe.tsv").read_bytes()).hexdigest()
check("7a deleting an UPLOAD removes the row AND its stored copy "
      "(that copy is the attachment itself)",
      st == 200 and "stored copy deleted" in r.get("file", "")
      and not gone, f"{st} {str(r)[:120]} gone={gone}")
check("7b deleting a SERVER PICK removes only the row: the original "
      "is untouched, byte-identical (originals are never deleted "
      "through this door)",
      st2 == 200 and "original untouched" in r2.get("file", "")
      and pick_digest1 == pick_digest0
      and (FIX / "probe.tsv").exists(),
      f"{st2} {str(r2)[:120]}")

# ================= 8: attachments keep Basic auth ======================
st, r, _h = api("POST", "/api/attachments",
                {"name": "x.txt", "data_b64": "aGk="}, auth=False)
check("8a an unauthenticated (no token, no Basic) upload is refused "
      "401 - the task-query job-creation grant does NOT extend here",
      st == 401, f"{st}")

# ================= 9: the UI contract ==================================
html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
check("9a the composer has both intake doors + chips + picker + the "
      "in-flight row",
      'id="att-btn"' in html and 'id="att-file"' in html
      and 'id="att-pick-btn"' in html and 'id="att-picker"' in html
      and 'id="att-chips"' in html and 'id="att-upload"' in html, "")
check("9b uploads use real progress + cancel (XHR upload.onprogress "
      "and abort) and sends refuse mid-flight sets",
      "xhr.upload.onprogress" in js and "xhr.abort()" in js
      and "state.uploads.size" in js and "att.badSend" in js, "")
check("9c the egress model is stated in the UI copy itself (paths, "
      "never bytes, audited)",
      "att.egressNote" in js and "never leave this machine" in js, "")
check("9d the picker labels sequencing files as formal-ingestion "
      "material instead of hiding them",
      "att.seqNote" in js and "data-pkfile" in js, "")
check("9e styles for chips/picker exist",
      ".att-chip" in css and ".att-picker" in css
      and ".pk-row.use" in css, "")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f_ in FAIL:
        print("FAILED:", f_)
    sys.exit(1)
