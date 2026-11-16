"""Durable state store (sqlite, stdlib only).

Job states are persisted so a restart never loses queued/running work silently:
on startup any job still marked queued/running/paused is transitioned to
'failed' with error 'interrupted by server restart' (visible in the UI).
States: queued | running | paused | blocked | completed | failed | cancelled.

Conversations (ChatGPT-style persistence): chats live in `conversations`
(messages carry conv_id); deleting is soft (deleted=1) - rows stay for
retention/audit. The store also honors the NEOAG_DEMO_DB environment
override so test batteries run against isolated fresh databases.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_NAME = "state.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, started_at TEXT,
  finished_at TEXT, state TEXT, kind TEXT, sample_id TEXT, spec_json TEXT,
  stage TEXT, progress REAL DEFAULT 0, error TEXT, approval_id TEXT,
  result_json TEXT, cancel_requested INTEGER DEFAULT 0,
  pause_requested INTEGER DEFAULT 0, cancel_reason TEXT
);
CREATE TABLE IF NOT EXISTS job_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, ts TEXT, kind TEXT,
  text TEXT, data_json TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, role TEXT, text TEXT,
  meta_json TEXT
);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, created_at TEXT, decided_at TEXT, job_id TEXT,
  action TEXT, risk TEXT, detail TEXT, state TEXT, decided_by TEXT
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT,
  object TEXT, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value_json TEXT
);
CREATE TABLE IF NOT EXISTS decisions(
  id TEXT PRIMARY KEY, ts TEXT, decided_at TEXT, job_id TEXT, stage TEXT,
  decision_type TEXT, question TEXT, options_json TEXT, chosen TEXT,
  rationale TEXT, evidence_json TEXT, policy_id TEXT, policy_version TEXT,
  decided_by TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS research(
  id TEXT PRIMARY KEY, created_at TEXT, finished_at TEXT, status TEXT,
  scope TEXT, target TEXT, report_json TEXT
);
CREATE TABLE IF NOT EXISTS research_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, research_id TEXT, ts TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS manifests(
  id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, description TEXT,
  status TEXT, proposal_json TEXT, confirmed_json TEXT, job_id TEXT
);
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, title TEXT,
  job_id TEXT, job_state TEXT, deleted INTEGER DEFAULT 0
);
"""

JOB_STATES = {"queued", "running", "paused", "blocked", "completed", "failed",
              "cancelled"}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        db_name = os.environ.get("NEOAG_DEMO_DB") or DB_NAME
        self.db_path = str(data_dir / db_name)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            try:  # migration for pre-conversation databases
                self.db.execute("ALTER TABLE messages ADD COLUMN conv_id "
                                "TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already present
            self.db.commit()

    # ---------- audit ----------
    def audit(self, actor: str, action: str, obj: str, detail: dict | None = None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO audit(ts,actor,action,object,detail_json) VALUES(?,?,?,?,?)",
                (now(), actor, action, obj, json.dumps(detail or {}, ensure_ascii=False)))
            self.db.commit()

    def audit_list(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- jobs ----------
    def job_create(self, job_id: str, kind: str, sample_id: str, spec: dict,
                   state: str, approval_id: str | None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO jobs(id,created_at,updated_at,state,kind,sample_id,"
                "spec_json,approval_id) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, now(), now(), state, kind, sample_id,
                 json.dumps(spec, ensure_ascii=False), approval_id))
            self.db.commit()

    def job_set(self, job_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?",
                            (*fields.values(), job_id))
            self.db.commit()

    def job_get(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def job_list(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recover_interrupted(self) -> list[str]:
        """Mark jobs that were mid-flight when the server died."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id,state FROM jobs WHERE state IN "
                "('queued','running','paused')").fetchall()
            ids = []
            for r in rows:
                self.db.execute(
                    "UPDATE jobs SET state='failed', error=?, finished_at=?, updated_at=?"
                    " WHERE id=?",
                    ("interrupted by server restart (prototype: no auto-resume)",
                     now(), now(), r["id"]))
                ids.append(r["id"])
            # blocked jobs whose execute-approval was already granted lost
            # their worker thread with the old process - they can never
            # resume, so fail them too (and cancel their pending decisions);
            # blocked jobs with a PENDING approval are legitimately still
            # waiting for a human click and stay blocked.
            brows = self.db.execute(
                "SELECT j.id FROM jobs j JOIN approvals a ON a.id=j.approval_id "
                "WHERE j.state='blocked' AND a.state!='pending'").fetchall()
            for r in brows:
                self.db.execute(
                    "UPDATE jobs SET state='failed', error=?, finished_at=?, "
                    "updated_at=? WHERE id=?",
                    ("interrupted by server restart while awaiting a "
                     "decision (prototype: no auto-resume)", now(), now(),
                     r["id"]))
                self.db.execute(
                    "UPDATE decisions SET chosen='aborted', decided_at=?, "
                    "rationale='job interrupted by server restart', "
                    "decided_by='system', status='cancelled' "
                    "WHERE job_id=? AND status='pending'",
                    (now(), r["id"]))
                ids.append(r["id"])
            self.db.commit()
        return ids

    # ---------- job events ----------
    def event_add(self, job_id: str, kind: str, text: str, data: dict | None = None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO job_events(job_id,ts,kind,text,data_json) VALUES(?,?,?,?,?)",
                (job_id, now(), kind, text, json.dumps(data or {}, ensure_ascii=False)))
            self.db.commit()

    def event_list(self, job_id: str | None = None, after: int = 0,
                   limit: int = 500) -> list[dict]:
        q = "SELECT * FROM job_events WHERE id > ?"
        args: list = [after]
        if job_id:
            q += " AND job_id=?"
            args.append(job_id)
        q += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    # ---------- conversations (ChatGPT-style history) ----------
    def conv_create(self, title: str = "") -> dict:
        cid = new_id("conv")
        with self.lock:
            self.db.execute(
                "INSERT INTO conversations(id,created_at,updated_at,title) "
                "VALUES(?,?,?,?)", (cid, now(), now(), title))
            self.db.commit()
        return self.conv_get(cid)

    def conv_get(self, cid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM conversations WHERE id=?",
                                  (cid,)).fetchone()
        return dict(row) if row else None

    def conv_list(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM messages m "
                " WHERE m.conv_id=c.id) AS n_messages "
                "FROM conversations c WHERE c.deleted=0 "
                "ORDER BY c.updated_at DESC, c.rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def conv_touch(self, cid: str, job_state: str | None = None) -> None:
        with self.lock:
            if job_state is not None:
                self.db.execute(
                    "UPDATE conversations SET updated_at=?, job_state=? "
                    "WHERE id=?", (now(), job_state, cid))
            else:
                self.db.execute("UPDATE conversations SET updated_at=? "
                                "WHERE id=?", (now(), cid))
            self.db.commit()

    def conv_set_title(self, cid: str, title: str) -> dict | None:
        with self.lock:
            self.db.execute("UPDATE conversations SET title=?, updated_at=? "
                            "WHERE id=?", (title[:120], now(), cid))
            self.db.commit()
        return self.conv_get(cid)

    def conv_set_job(self, cid: str, job_id: str | None,
                     state: str = "unknown") -> None:
        with self.lock:
            self.db.execute(
                "UPDATE conversations SET job_id=?, job_state=?, updated_at=? "
                "WHERE id=?", (job_id, state, now(), cid))
            self.db.commit()

    def conv_delete(self, cid: str) -> bool:
        """Soft delete: hides from history; backend rows retained."""
        with self.lock:
            cur = self.db.execute(
                "UPDATE conversations SET deleted=1, updated_at=? "
                "WHERE id=? AND deleted=0", (now(), cid))
            self.db.commit()
        return cur.rowcount > 0

    def conv_recent_text(self, cid: str, n: int = 12) -> list[str]:
        """Recent user/agent turns as 'role: text' lines (context
        reconstruction after an agent job expired)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT role, text FROM messages WHERE conv_id=? "
                "AND role IN ('user','agent') ORDER BY id DESC LIMIT ?",
                (cid, n)).fetchall()
        return [f"{r['role']}: {(r['text'] or '')[:200]}"
                for r in reversed(rows)]

    # ---------- messages ----------
    def msg_add(self, role: str, text: str, meta: dict | None = None,
                conv_id: str = "") -> dict:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages(ts,role,text,meta_json,conv_id) "
                "VALUES(?,?,?,?,?)",
                (now(), role, text, json.dumps(meta or {}, ensure_ascii=False),
                 conv_id))
            self.db.commit()
            mid = cur.lastrowid
        return {"id": mid, "ts": now(), "role": role, "text": text,
                "meta": meta or {}, "conv_id": conv_id}

    def msg_list(self, limit: int = 200, conv_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM messages"
        args: list = []
        if conv_id is not None:
            q += " WHERE conv_id=?"
            args.append(conv_id)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
            out.append(d)
        return out

    def msg_purge_legacy_notices(self) -> int:
        """One-time cleanup: drop pre-redesign 'MOCK PROTOTYPE' system banners
        from persisted history (the redesign replaced them with the single
        header demo indicator). Returns the number of rows removed."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id, meta_json FROM messages").fetchall()
            stale = [r["id"] for r in rows
                     if json.loads(r["meta_json"] or "{}").get("badges")
                     == ["mock prototype"]
                     or (r["meta_json"] or "").startswith('{"badges": ["mock prototype"')]
            if not stale:
                return 0
            self.db.executemany("DELETE FROM messages WHERE id = ?",
                                [(i,) for i in stale])
            self.db.commit()
        return len(stale)

    # ---------- approvals ----------
    def approval_create(self, appr_id: str, job_id: str, action: str, risk: str,
                        detail: str) -> dict:
        with self.lock:
            self.db.execute(
                "INSERT INTO approvals(id,created_at,job_id,action,risk,detail,state)"
                " VALUES(?,?,?,?,?,?, 'pending')",
                (appr_id, now(), job_id, action, risk, detail))
            self.db.commit()
        return {"id": appr_id, "created_at": now(), "decided_at": None,
                "job_id": job_id, "action": action, "risk": risk, "detail": detail,
                "state": "pending", "decided_by": None}

    def approval_decide(self, appr_id: str, approve: bool, decided_by: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM approvals WHERE id=?",
                                  (appr_id,)).fetchone()
            if not row or row["state"] != "pending":
                return None
            self.db.execute(
                "UPDATE approvals SET state=?, decided_at=?, decided_by=? WHERE id=?",
                ("approved" if approve else "denied", now(), decided_by, appr_id))
            self.db.commit()
            row = self.db.execute("SELECT * FROM approvals WHERE id=?",
                                  (appr_id,)).fetchone()
        return dict(row)

    def approval_list(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM approvals ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------- settings ----------
    def setting(self, key: str, default):
        with self.lock:
            row = self.db.execute("SELECT value_json FROM settings WHERE key=?",
                                  (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def setting_set(self, key: str, value) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO settings(key,value_json) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, json.dumps(value, ensure_ascii=False)))
            self.db.commit()

    # ---------- decisions (persistent decision history) ----------
    def decision_add(self, d: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO decisions(id,ts,decided_at,job_id,stage,decision_type,"
                "question,options_json,chosen,rationale,evidence_json,policy_id,"
                "policy_version,decided_by,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["id"], d["ts"], d.get("decided_at"), d["job_id"], d["stage"],
                 d["decision_type"], d["question"],
                 json.dumps(d["options"], ensure_ascii=False), d.get("chosen"),
                 d.get("rationale"), json.dumps(d.get("evidence", []), ensure_ascii=False),
                 d.get("policy_id"), d.get("policy_version"), d.get("decided_by"),
                 d.get("status", "pending")))
            self.db.commit()

    def decision_get(self, d_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM decisions WHERE id=?",
                                  (d_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["options"] = json.loads(d.pop("options_json") or "[]")
        d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
        return d

    def decision_set(self, d_id: str, chosen: str, rationale: str,
                     decided_by: str, status: str = "resolved") -> None:
        d = self.decision_get(d_id)
        if not d:
            return
        d["chosen"] = chosen
        d["decided_at"] = now()
        d["rationale"] = rationale
        d["decided_by"] = decided_by
        d["status"] = status
        with self.lock:
            self.db.execute(
                "UPDATE decisions SET chosen=?, decided_at=?, rationale=?, "
                "decided_by=?, status=? WHERE id=?",
                (chosen, d["decided_at"], rationale, decided_by, status, d_id))
            self.db.commit()
        self.audit("user" if decided_by == "user" else "engine",
                   f"decision.{status}", d_id,
                   {"type": d["decision_type"], "chosen": chosen,
                    "job": d["job_id"]})

    def decision_list(self, job_id: str | None = None, limit: int = 300) -> list[dict]:
        q = "SELECT * FROM decisions"
        args: list = []
        if job_id:
            q += " WHERE job_id=?"
            args.append(job_id)
        q += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = json.loads(d.pop("options_json") or "[]")
            d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
            out.append(d)
        return out

    # ---------- research reports ----------
    def research_add(self, rid: str, scope: str, target: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO research(id,created_at,status,scope,target) "
                "VALUES(?,?, 'queued', ?, ?)", (rid, now(), scope, target))
            self.db.commit()

    def research_set(self, rid: str, status: str, report: dict | None = None,
                     finished: bool = False) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE research SET status=?, report_json=?, finished_at=? "
                "WHERE id=?",
                (status,
                 json.dumps(report, ensure_ascii=False) if report else None,
                 now() if finished else None, rid))
            self.db.commit()

    def research_get(self, rid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM research WHERE id=?",
                                  (rid,)).fetchone()
        if not row:
            return None
        r = dict(row)
        raw = r.pop("report_json", None)
        r["report"] = json.loads(raw) if raw else None
        return r

    def research_list(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,created_at,finished_at,status,scope,target FROM "
                "research ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- input manifests ----------
    def manifest_add(self, mid: str, description: str, proposal: dict) -> dict:
        with self.lock:
            self.db.execute(
                "INSERT INTO manifests(id,created_at,updated_at,description,"
                "status,proposal_json) VALUES(?,?,?,?, 'proposed', ?)",
                (mid, now(), now(), description,
                 json.dumps(proposal, ensure_ascii=False)))
            self.db.commit()
            row = self.db.execute("SELECT * FROM manifests WHERE id=?",
                                  (mid,)).fetchone()
        m = dict(row)
        m["proposal"] = json.loads(m.pop("proposal_json") or "{}")
        return m

    def manifest_get(self, mid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM manifests WHERE id=?",
                                  (mid,)).fetchone()
        if not row:
            return None
        m = dict(row)
        m["proposal"] = json.loads(m.pop("proposal_json") or "{}")
        m["confirmed"] = json.loads(m.pop("confirmed_json") or "null")
        return m

    def manifest_set(self, mid: str, status: str, confirmed: dict | None = None,
                     job_id: str | None = None) -> dict | None:
        with self.lock:
            self.db.execute(
                "UPDATE manifests SET status=?, confirmed_json=?, job_id=?, "
                "updated_at=? WHERE id=?",
                (status, json.dumps(confirmed, ensure_ascii=False) if confirmed
                 is not None else None, job_id, now(), mid))
            self.db.commit()
            row = self.db.execute("SELECT * FROM manifests WHERE id=?",
                                  (mid,)).fetchone()
        if not row:
            return None
        m = dict(row)
        m["proposal"] = json.loads(m.pop("proposal_json") or "{}")
        m["confirmed"] = json.loads(m.pop("confirmed_json") or "null")
        return m

    def manifest_list(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,created_at,updated_at,description,status,job_id "
                "FROM manifests ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- reversible archive cleanup (developer records) ----------
    def archive_and_purge(self, keep_audit: bool = True) -> dict:
        """Export every user-facing table to data/archive-<ts>.json, then
        delete those rows (audit + settings stay). Reversible: the JSON
        file contains full rows; re-import with restore_archive()."""
        tables = ["messages", "jobs", "job_events", "approvals", "decisions",
                  "research", "research_events", "manifests", "conversations"]
        dump = {"archived_at": now(), "reason": "developer demo records "
                "moved out of the visible workspace (reversible)",
                "tables": {}}
        with self.lock:
            for t in tables:
                rows = [dict(r) for r in
                        self.db.execute(f"SELECT * FROM {t}").fetchall()]
                dump["tables"][t] = rows
                self.db.execute(f"DELETE FROM {t}")
            self.db.commit()
        fname = f"archive-{datetime.now():%Y%m%d-%H%M%S}.json"
        p = Path(self.db_path).parent / fname
        p.write_text(json.dumps(dump, ensure_ascii=False, indent=1))
        p.chmod(0o600)
        counts = {t: len(v) for t, v in dump["tables"].items()}
        self.audit("server", "store.archive_and_purge", fname, counts)
        return {"file": str(p), "counts": counts}

    def restore_archive(self, path: str) -> int:
        """Re-import an archive written by archive_and_purge. Returns the
        number of rows restored."""
        dump = json.loads(Path(path).read_text())
        n = 0
        with self.lock:
            for t, rows in dump.get("tables", {}).items():
                for r in rows:
                    cols = ", ".join(r.keys())
                    qm = ", ".join("?" * len(r))
                    self.db.execute(
                        f"INSERT OR REPLACE INTO {t}({cols}) VALUES({qm})",
                        tuple(r.values()))
                    n += 1
            self.db.commit()
        self.audit("server", "store.restore_archive", path, {"rows": n})
        return n
