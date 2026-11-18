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
-- Per-run OUTPUT manifest (A3): every artifact a formal run produced,
-- with an honest availability status. A job is only ever described as
-- having "delivered" what this table shows as available.
CREATE TABLE IF NOT EXISTS artifacts(
  id TEXT PRIMARY KEY, job_id TEXT, created_at TEXT, type TEXT, name TEXT,
  strategy TEXT, path TEXT, status TEXT, rows INTEGER, checksum TEXT,
  meta_json TEXT
);
-- Immutable per-run effective-config snapshot (A5): written ONCE when a
-- job starts and never updated afterwards (INSERT OR IGNORE), so later
-- default/policy/skill changes cannot rewrite what a past run recorded.
CREATE TABLE IF NOT EXISTS run_snapshots(
  job_id TEXT PRIMARY KEY, created_at TEXT, snapshot_json TEXT
);
-- Agent Skills (PART C): editable advisory instructions with full
-- version history. Deployment to the agent workspace is explicit and
-- only affects NEW agent sessions; enforcement (gate/policies) lives
-- elsewhere and is NOT stored here.
CREATE TABLE IF NOT EXISTS skills(
  id TEXT PRIMARY KEY, slug TEXT UNIQUE, name TEXT, title TEXT,
  category TEXT, content TEXT, enabled INTEGER DEFAULT 1,
  version INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT,
  deployed_version INTEGER DEFAULT 0, deployed_at TEXT, deployed_hash TEXT
);
CREATE TABLE IF NOT EXISTS skill_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT, version INTEGER,
  content TEXT, created_at TEXT, source TEXT
);
-- Downloadable exports (Milestone 5 PART 7): each row pins one rendered
-- export FILE on disk to the exact stored content it was rendered from
-- (source_hash). Downloads serve the persisted bytes - re-downloading
-- never re-renders science; a new version row exists only when the
-- stored report/job content itself changed.
CREATE TABLE IF NOT EXISTS exports(
  id TEXT PRIMARY KEY, kind TEXT, object_id TEXT, format TEXT,
  filename TEXT, path TEXT, bytes INTEGER, sha256 TEXT, source_hash TEXT,
  version INTEGER, created_at TEXT, meta_json TEXT
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
        self.data_dir = data_dir
        # export files live next to the db they were rendered from and are
        # isolated per database, so parallel verification batteries never
        # read each other's persisted exports
        _stem = Path(db_name).stem
        self.export_dir = data_dir / (
            "exports" if _stem == "state" else f"exports-{_stem}")
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            for stmt in (
                # migration for pre-conversation databases
                "ALTER TABLE messages ADD COLUMN conv_id TEXT DEFAULT ''",
                # Milestone 4 migrations (each idempotent via except):
                "ALTER TABLE jobs ADD COLUMN conv_id TEXT DEFAULT ''",
                "ALTER TABLE jobs ADD COLUMN outcome TEXT",
                "ALTER TABLE jobs ADD COLUMN native_plan_json TEXT",
                "ALTER TABLE jobs ADD COLUMN gw_job_id TEXT DEFAULT ''",
                "ALTER TABLE decisions ADD COLUMN key TEXT",
                "ALTER TABLE decisions ADD COLUMN category "
                "TEXT DEFAULT 'pipeline'",
                "ALTER TABLE decisions ADD COLUMN trigger TEXT",
                "ALTER TABLE decisions ADD COLUMN source TEXT",
                "ALTER TABLE decisions ADD COLUMN mandatory INTEGER "
                "DEFAULT 1",
                "ALTER TABLE decisions ADD COLUMN effect_class TEXT",
                "ALTER TABLE decisions ADD COLUMN downstream_json "
                "TEXT DEFAULT '{}'",
                "ALTER TABLE research ADD COLUMN engine "
                "TEXT DEFAULT 'example'",
                "ALTER TABLE research ADD COLUMN conv_id TEXT DEFAULT ''",
                "ALTER TABLE research ADD COLUMN cancel_requested "
                "INTEGER DEFAULT 0",
                "ALTER TABLE research ADD COLUMN progress_json TEXT",
                # Milestone 6 PART F: conversation organisation (the
                # sidebar menu offers Rename / Pin / Archive / Delete)
                "ALTER TABLE conversations ADD COLUMN pinned "
                "INTEGER DEFAULT 0",
                "ALTER TABLE conversations ADD COLUMN archived "
                "INTEGER DEFAULT 0",
                # Milestone 9: user-facing display titles. Presentation
                # only - the row id, sample_id and stored provenance stay
                # authoritative; an empty title means "derive from the
                # real metadata in the API layer".
                "ALTER TABLE jobs ADD COLUMN display_title "
                "TEXT DEFAULT ''",
                "ALTER TABLE research ADD COLUMN display_title "
                "TEXT DEFAULT ''",
            ):
                try:
                    self.db.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already present
            self.db.commit()

    def n_jobs_in_states(self, states: tuple) -> int:
        q = ("SELECT COUNT(*) AS n FROM jobs WHERE state IN ("
             + ",".join("?" * len(states)) + ")")
        with self.lock:
            row = self.db.execute(q, tuple(states)).fetchone()
        return row["n"] if row else 0

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
                   state: str, approval_id: str | None,
                   conv_id: str = "") -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO jobs(id,created_at,updated_at,state,kind,sample_id,"
                "spec_json,approval_id,conv_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, now(), now(), state, kind, sample_id,
                 json.dumps(spec, ensure_ascii=False), approval_id, conv_id))
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

    def job_list(self, limit: int = 50, conv_id: str | None = None,
                 kinds: list[str] | None = None) -> list[dict]:
        q = "SELECT * FROM jobs"
        where, args = [], []
        if conv_id:
            where.append("conv_id=?")
            args.append(conv_id)
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            args.extend(kinds)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
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
                "ORDER BY c.pinned DESC, c.updated_at DESC, c.rowid DESC "
                "LIMIT ?",
                (limit,)).fetchall()
        # archived rows stay in the result (flagged) so the sidebar can
        # offer an honest Unarchive instead of hiding them unrecoverably
        return [dict(r) for r in rows]

    def conv_set_flags(self, cid: str, pinned: int | None = None,
                       archived: int | None = None) -> dict | None:
        fields, args = {}, []
        if pinned is not None:
            fields["pinned"] = 1 if pinned else 0
            args.append(1 if pinned else 0)
        if archived is not None:
            fields["archived"] = 1 if archived else 0
            args.append(1 if archived else 0)
        if not fields:
            return self.conv_get(cid)
        args += [now(), cid]
        with self.lock:
            self.db.execute(
                "UPDATE conversations SET "
                + ", ".join(f"{k}=?" for k in fields)
                + ", updated_at=? WHERE id=?",
                args)
            self.db.commit()
        return self.conv_get(cid)

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

    def msg_set_meta(self, msg_id: int, meta: dict) -> None:
        """Replace one message's meta (used to take a real-agent kickoff
        notice from 'streaming' to a terminal turn_state)."""
        with self.lock:
            self.db.execute(
                "UPDATE messages SET meta_json=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), msg_id))
            self.db.commit()

    def msg_sweep_stale_streaming(self) -> int:
        """Startup honesty sweep: any persisted message still labelled
        'streaming' belongs to a relay that died with the previous
        server process - mark it terminal, do not resurrect it."""
        n = 0
        with self.lock:
            for r in self.db.execute(
                    "SELECT id, meta_json FROM messages").fetchall():
                meta = json.loads(r["meta_json"] or "{}")
                if meta.get("turn_state") == "streaming":
                    meta["turn_state"] = "interrupted by server restart"
                    meta["badges"] = [
                        b for b in meta.get("badges", [])
                        if b != "streaming"] + ["interrupted"]
                    self.db.execute(
                        "UPDATE messages SET meta_json=? WHERE id=?",
                        (json.dumps(meta, ensure_ascii=False), r["id"]))
                    n += 1
            self.db.commit()
        return n

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

    def approval_get(self, appr_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM approvals WHERE id=?",
                                  (appr_id,)).fetchone()
        return dict(row) if row else None

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
                "policy_version,decided_by,status,key,category,trigger,source,"
                "mandatory,effect_class,downstream_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["id"], d["ts"], d.get("decided_at"), d["job_id"], d["stage"],
                 d["decision_type"], d["question"],
                 json.dumps(d["options"], ensure_ascii=False), d.get("chosen"),
                 d.get("rationale"), json.dumps(d.get("evidence", []), ensure_ascii=False),
                 d.get("policy_id"), d.get("policy_version"), d.get("decided_by"),
                 d.get("status", "pending"),
                 d.get("key"), d.get("category", "pipeline"),
                 d.get("trigger"), d.get("source"),
                 1 if d.get("mandatory", True) else 0,
                 d.get("effect_class"),
                 json.dumps(d.get("downstream") or {}, ensure_ascii=False)))
            self.db.commit()

    def decision_set_effect(self, d_id: str, downstream: dict) -> None:
        """Record what the resolved choice actually mapped to downstream
        (the applied option->execution effect)."""
        with self.lock:
            self.db.execute("UPDATE decisions SET downstream_json=? WHERE id=?",
                            (json.dumps(downstream, ensure_ascii=False), d_id))
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
        d["downstream"] = json.loads(d.pop("downstream_json") or "{}")
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
            d["downstream"] = json.loads(d.pop("downstream_json") or "{}")
            out.append(d)
        return out

    # ---------- research reports ----------
    def research_add(self, rid: str, scope: str, target: str,
                     engine: str = "example", conv_id: str = "",
                     targets: list[str] | None = None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO research(id,created_at,status,scope,target,engine,"
                "conv_id,progress_json) VALUES(?,?, 'queued', ?,?,?,?,?)",
                (rid, now(), scope, target, engine, conv_id,
                 json.dumps({"steps": [], "targets": targets or [target],
                             "sources_done": [], "note": "queued"},
                            ensure_ascii=False)))
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

    def research_set_title(self, rid: str, title: str) -> bool:
        """Milestone 9: display title only (see the display_title
        migration). The id, scope and target stay the record."""
        with self.lock:
            cur = self.db.execute(
                "UPDATE research SET display_title=? WHERE id=?",
                (title[:200], rid))
            self.db.commit()
        return cur.rowcount > 0

    def research_event_add(self, rid: str, text: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO research_events(research_id,ts,text) "
                "VALUES(?,?,?)", (rid, now(), text))
            self.db.commit()

    def research_events(self, rid: str, after: int = 0) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM research_events WHERE research_id=? AND id>?"
                " ORDER BY id ASC", (rid, after)).fetchall()
        return [dict(r) for r in rows]

    def research_set_progress(self, rid: str, progress: dict) -> None:
        """Durable progress for live research jobs (cancellation check and
        resume-after-restart both read this)."""
        with self.lock:
            self.db.execute("UPDATE research SET progress_json=? WHERE id=?",
                            (json.dumps(progress, ensure_ascii=False), rid))
            self.db.commit()

    def research_progress(self, rid: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT progress_json,cancel_requested,status,engine,scope,"
                "target,conv_id FROM research WHERE id=?", (rid,)).fetchone()
        if not row:
            return {}
        p = json.loads(row["progress_json"] or "{}")
        p["cancel_requested"] = bool(row["cancel_requested"])
        p["status"] = row["status"]
        return p

    def research_cancel_request(self, rid: str) -> bool:
        with self.lock:
            cur = self.db.execute(
                "UPDATE research SET cancel_requested=1 WHERE id=? "
                "AND status IN ('queued','running')", (rid,))
            self.db.commit()
        return cur.rowcount > 0

    def research_get(self, rid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM research WHERE id=?",
                                  (rid,)).fetchone()
        if not row:
            return None
        r = dict(row)
        raw = r.pop("report_json", None)
        r["report"] = json.loads(raw) if raw else None
        r["progress"] = json.loads(r.get("progress_json") or "{}")
        return r

    def research_list(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,created_at,finished_at,status,scope,target,engine,"
                "conv_id,progress_json,display_title FROM research "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
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

    # ---------- output artifacts (per-run output manifest, A3) ----------
    def artifact_add(self, job_id: str, type_: str, name: str,
                     strategy: str | None, path: str, status: str,
                     rows: int | None, checksum: str | None,
                     meta: dict | None = None) -> dict:
        aid = new_id("art")
        with self.lock:
            self.db.execute(
                "INSERT INTO artifacts(id,job_id,created_at,type,name,strategy,"
                "path,status,rows,checksum,meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (aid, job_id, now(), type_, name, strategy, path, status,
                 rows, checksum, json.dumps(meta or {}, ensure_ascii=False)))
            self.db.commit()
        return self.artifact_get(aid)

    def artifact_get(self, aid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM artifacts WHERE id=?",
                                  (aid,)).fetchone()
        if not row:
            return None
        a = dict(row)
        a["meta"] = json.loads(a.pop("meta_json") or "{}")
        return a

    def artifact_list(self, job_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM artifacts"
        args: list = []
        if job_id:
            q += " WHERE job_id=?"
            args.append(job_id)
        q += " ORDER BY rowid ASC"
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            a = dict(r)
            a["meta"] = json.loads(a.pop("meta_json") or "{}")
            out.append(a)
        return out

    # ---------- immutable run snapshots (A5) ----------
    def snapshot_add(self, job_id: str, snapshot: dict) -> None:
        """Write-once: later calls with the same job_id are ignored, so a
        run's recorded inputs/config/versions can never drift."""
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO run_snapshots(job_id,created_at,"
                "snapshot_json) VALUES(?,?,?)",
                (job_id, now(), json.dumps(snapshot, ensure_ascii=False)))
            self.db.commit()

    def snapshot_get(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM run_snapshots WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        return {"job_id": row["job_id"], "created_at": row["created_at"],
                "snapshot": json.loads(row["snapshot_json"] or "{}")}

    # ---------- downloadable exports (M5 PART 7) ----------
    def export_add(self, kind: str, object_id: str, fmt: str,
                   filename: str, path: str, size: int, sha: str,
                   source_hash: str, meta: dict | None = None) -> dict:
        eid = new_id("exp")
        with self.lock:
            v = self.db.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM exports "
                "WHERE kind=? AND object_id=? AND format=?",
                (kind, object_id, fmt)).fetchone()["v"]
            self.db.execute(
                "INSERT INTO exports(id,kind,object_id,format,filename,"
                "path,bytes,sha256,source_hash,version,created_at,"
                "meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, kind, object_id, fmt, filename, path, size, sha,
                 source_hash, v, now(),
                 json.dumps(meta or {}, ensure_ascii=False)))
            self.db.commit()
        return self.export_get(eid)

    def export_get(self, eid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM exports WHERE id=?",
                                  (eid,)).fetchone()
        if not row:
            return None
        e = dict(row)
        e["meta"] = json.loads(e.pop("meta_json") or "{}")
        return e

    def export_versions(self, kind: str, object_id: str,
                        fmt: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM exports WHERE kind=? AND object_id=? AND "
                "format=? ORDER BY version ASC",
                (kind, object_id, fmt)).fetchall()
        out = []
        for r in rows:
            e = dict(r)
            e["meta"] = json.loads(e.pop("meta_json") or "{}")
            out.append(e)
        return out

    def export_latest(self, kind: str, object_id: str,
                      fmt: str) -> dict | None:
        vs = self.export_versions(kind, object_id, fmt)
        return vs[-1] if vs else None

    def export_list(self, kind: str, object_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM exports WHERE kind=? AND object_id=? "
                "ORDER BY format ASC, version ASC",
                (kind, object_id)).fetchall()
        out = []
        for r in rows:
            e = dict(r)
            e["meta"] = json.loads(e.pop("meta_json") or "{}")
            out.append(e)
        return out

    # ---------- agent skills (advisory, PART C) ----------
    def skill_create(self, slug: str, name: str, title: str, category: str,
                     content: str, source: str = "created in Skills editor",
                     enabled: bool = True) -> dict:
        sid = new_id("skill")
        with self.lock:
            self.db.execute(
                "INSERT INTO skills(id,slug,name,title,category,content,enabled,"
                "version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
                (sid, slug, name, title, category, content,
                 1 if enabled else 0, now(), now()))
            self.db.execute(
                "INSERT INTO skill_versions(skill_id,version,content,created_at,"
                "source) VALUES(?,1,?,?,?)", (sid, content, now(), source))
            self.db.commit()
        return self.skill_get(sid)

    def skill_get(self, sid: str, by_slug: bool = False) -> dict | None:
        col = "slug" if by_slug else "id"
        with self.lock:
            row = self.db.execute(
                f"SELECT * FROM skills WHERE {col}=?", (sid,)).fetchone()
        return dict(row) if row else None

    def skill_list(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM skills ORDER BY category, name").fetchall()
        return [dict(r) for r in rows]

    def skill_update(self, sid: str, content: str, source: str,
                     title: str | None = None,
                     category: str | None = None) -> dict | None:
        s = self.skill_get(sid)
        if not s:
            return None
        new_v = int(s["version"]) + 1
        with self.lock:
            self.db.execute(
                "UPDATE skills SET content=?, version=?, updated_at=?, "
                "title=COALESCE(?,title), category=COALESCE(?,category) "
                "WHERE id=?",
                (content, new_v, now(), title, category, sid))
            self.db.execute(
                "INSERT INTO skill_versions(skill_id,version,content,"
                "created_at,source) VALUES(?,?,?,?,?)",
                (sid, new_v, content, now(), source))
            self.db.commit()
        return self.skill_get(sid)

    def skill_set_enabled(self, sid: str, enabled: bool) -> dict | None:
        with self.lock:
            self.db.execute("UPDATE skills SET enabled=?, updated_at=? "
                            "WHERE id=?", (1 if enabled else 0, now(), sid))
            self.db.commit()
        return self.skill_get(sid)

    def skill_versions(self, sid: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,version,created_at,source,length(content) AS "
                "chars FROM skill_versions WHERE skill_id=? "
                "ORDER BY version DESC", (sid,)).fetchall()
        return [dict(r) for r in rows]

    def skill_version_content(self, sid: str, version: int) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT content FROM skill_versions WHERE skill_id=? AND "
                "version=?", (sid, version)).fetchone()
        return row["content"] if row else None

    def skill_set_deployed(self, sid: str, version: int, digest: str) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE skills SET deployed_version=?, deployed_at=?, "
                "deployed_hash=? WHERE id=?", (version, now(), digest, sid))
            self.db.commit()

    # ---------- reversible archive cleanup (developer records) ----------
    def archive_and_purge(self, keep_audit: bool = True) -> dict:
        """Export every user-facing table to data/archive-<ts>.json, then
        delete those rows (audit + settings stay). Reversible: the JSON
        file contains full rows; re-import with restore_archive()."""
        tables = ["messages", "jobs", "job_events", "approvals", "decisions",
                  "research", "research_events", "manifests", "conversations",
                  "artifacts", "run_snapshots", "skills", "skill_versions"]
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
