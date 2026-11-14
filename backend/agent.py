"""Agent layer with two transports (see POST /api/agent/mode, audited):

 - mock (default): scripted demo agent - no LLM is called at all.
 - real: the genuine server-side CodeBuddy agent via the gateway Jobs
   API, plan-mode (execution tools denied gateway-side). See
   handle_real/_relay_job below and backend/codebuddy_client.py.

Channel separation, per project requirements:
 - role="agent"      -> agent suggestion   (badge: "agent suggestion")
 - role="policy"     -> validated NeoAG policy text, quoted from policies.py
                        (badge: "validated policy", cites its source)
The agent never invents policy, never claims real computation happened, and
never references anything but synthetic DEMO- data. Honesty about the demo
nature lives in the header indicator and artifact badges - NOT in repeated
chat banners (removed in this redesign).
"""
from __future__ import annotations

import json
import re
import threading

from . import mock_data
from .policies import POLICY_BY_ID


class DemoAgent:
    def __init__(self, store, jobs):
        self.store = store
        self.jobs = jobs
        self._client = None            # lazy CodeBuddyClient (real mode)
        self._context: list[str] = []  # rolling context for re-dispatch
        self._job_id: str | None = None  # persistent real chat job
        self._relay_thread: threading.Thread | None = None
        self._busy = False             # one in-flight real exchange

    # ------------------------------------------------------------------
    # Real-agent integration via the CodeBuddy gateway JOBS API (the
    # runs API could not guarantee transcript delivery - see
    # backend/codebuddy_client.py for the verified API map). OFF by
    # default; the operator switches modes in the UI
    # (POST /api/agent/mode), and the switch is audited. The chat agent
    # runs as a plan-mode job instance: a REAL agent whose execution
    # tools (Bash/edits) are denied gateway-side, so this interface can
    # propose but never execute. Tool denial text is relayed verbatim.
    # ------------------------------------------------------------------
    CHAT_CWD = "/tmp"                  # deliberately inert working dir
    CHAT_PREAMBLE = (
        "You are the real CodeBuddy agent embedded as the assistant of "
        "the NeoAG web workspace (a front-end for the NeoAG neoantigen "
        "pipeline; the workspace's own demo jobs run on synthetic data "
        "only). This session is permissionMode=plan: Bash and file "
        "edits are denied by the gateway - when the user asks for an "
        "action, describe the exact command or change instead of "
        "attempting it. Never invent results; say when you are unsure. "
        "The user's message follows.\n\n")

    def _ensure_client(self):
        if self._client is None:
            from .codebuddy_client import CodeBuddyClient
            self._client = CodeBuddyClient()
        return self._client

    def handle_real(self, text: str) -> list[dict]:
        """Send the user text to the REAL server-side CodeBuddy agent
        (persistent plan-mode job; multi-turn via /reply). Immediate
        acknowledgement returns here; the agent's real output arrives
        asynchronously from the job transcript ('chat' SSE events)."""
        try:
            client = self._ensure_client()
        except Exception as e:
            return [self._say(
                f"Real-agent mode is ON but the CodeBuddy gateway is not "
                f"reachable from this server ({e}). Nothing was executed.",
                meta={"badges": ["agent suggestion", "integration error"]})]
        if self._busy:
            return [self._say(
                "The real agent is still working on the previous message "
                "(one exchange at a time). Wait for its reply.",
                meta={"badges": ["real agent", "integration note"]})]
        detail = client.job(self._job_id) if self._job_id else {}
        alive = bool(detail) and not detail.get("unknown") and \
            detail.get("state") not in (None, "stopped", "failed")
        try:
            if alive:
                r = client.reply(self._job_id, text)
                if r.get("delivered"):
                    job_id, started = self._job_id, "continued"
                else:
                    raise RuntimeError(f"reply failed: "
                                       f"{r.get('error') or r}")
            else:
                prompt = self.CHAT_PREAMBLE + self._context_block() + text
                d = client.dispatch(prompt, cwd=self.CHAT_CWD,
                                    name="neoag-web-chat",
                                    permission_mode="plan")
                job_id, started = d.get("id"), "started"
                self._job_id = job_id
        except Exception as e:
            self._job_id = None
            return [self._say(
                f"Could not reach the real agent: {e} Nothing was "
                "executed. (The gateway rate-limits job dispatch; try "
                "again shortly.)",
                meta={"badges": ["agent suggestion", "integration error"]})]
        self._busy = True
        self._context.append(f"user: {text[:140]}")
        note = self.store.msg_add(
            "agent",
            f"Real agent {started} (job {str(job_id)[:8]}..., read-only "
            "plan mode - it can inspect and propose but not execute). "
            "Its answer and any tool attempts stream in below from the "
            "gateway transcript.",
            {"badges": ["real agent", "streaming"],
             "source": "codebuddy", "job_id": job_id})
        self._relay_thread = threading.Thread(
            target=self._relay_job, args=(job_id,), daemon=True)
        self._relay_thread.start()
        return [note]

    def _context_block(self) -> str:
        if not self._context:
            return ""
        tail = "\n".join(f"- {c}" for c in self._context[-8:])
        return ("Rolling transcript of this chat (the job was "
                f"re-dispatched, so prior turns ride along):\n{tail}\n\n")

    def _relay_job(self, job_id: str) -> None:
        """Poll the job transcript, post NEW tool events immediately and
        the assistant's reply when the turn settles. Never fabricates:
        content comes only from the gateway transcript/detail fields."""
        import html as _html
        import time as _time
        from .server import publish
        client = self._client
        seen: set[str] = set()
        reply_texts: dict[str, str] = {}
        t0 = _time.time()
        max_seconds = 600
        try:
            while _time.time() - t0 < max_seconds:
                _time.sleep(2.5)
                detail = client.job(job_id)
                state = detail.get("state")
                for u in client.transcript(job_id):
                    kind = str(u.get("sessionUpdate") or "")
                    mid = str(u.get("messageId") or u.get("toolCallId") or "")
                    if kind in ("tool_call", "tool_call_update"):
                        key = f"{kind}|{u.get('toolCallId')}|{u.get('status')}"
                        if key in seen:
                            continue
                        seen.add(key)
                        if kind == "tool_call":
                            body = (f"[attempted] {u.get('title') or 'tool'}"
                                    f" (status {u.get('status')})")
                        else:
                            out = str(u.get("rawOutput") or "")[:300]
                            body = (f"[tool {u.get('status')}] "
                                    f"{_html.escape(out)}")
                        self.store.msg_add(
                            "tool", body,
                            {"badges": ["tool output", "real agent"],
                             "job_id": job_id})
                        publish({"type": "chat"})
                    elif kind == "agent_message_chunk" and mid:
                        txt = (u.get("content") or {}).get("text") or ""
                        if txt.strip():
                            reply_texts[mid] = txt
                if state in ("done", "failed", "stopped"):
                    body = max(reply_texts.values(),
                               key=len) if reply_texts else ""
                    if state == "done" and body:
                        self.store.msg_add(
                            "agent", body,
                            {"badges": ["real agent"],
                             "source": "codebuddy", "job_id": job_id})
                        self._context.append(f"agent: {body[:140]}")
                    else:
                        live = str(detail.get("detail") or "")[:200]
                        self.store.msg_add(
                            "agent",
                            f"(Real-agent turn ended: {state}."
                            + (f" Gateway detail: {live}" if live else "")
                            + " No assistant reply text was recorded - "
                              "this interface does not fabricate one.)",
                            {"badges": ["real agent", "integration note"],
                             "job_id": job_id})
                    publish({"type": "chat"})
                    return
            self.store.msg_add(
                "agent",
                f"(No terminal job state seen within {max_seconds}s; the "
                "gateway still holds the job. Its transcript remains "
                "readable - send another message or check the operator's "
                "CodeBuddy Web UI.)",
                {"badges": ["real agent", "integration note"],
                 "job_id": job_id})
            publish({"type": "chat"})
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    # Input-manifest proposal (mock integration point). In demo mode the
    # proposal is SYNTHETIC and labelled; with a real agent the same
    # manifest shape would be produced by actual server-side inspection.
    # ------------------------------------------------------------------
    def propose_manifest(self, description: str) -> dict:
        t = description.lower()
        sample = next((s["id"] for s in mock_data.SAMPLES
                       if s["id"].lower() in t or s["label"].lower() in t),
                      "DEMO-P001")
        has_rna = "no rna" not in t
        manifest = {
            "mock": True,
            "note": ("MOCK proposal - not derived from a real directory "
                     "scan. With the real CodeBuddy agent connected, the "
                     "same manifest shape is produced from actual "
                     "server-side inspection of the named directory."),
            "source_description": description[:400],
            "sample_id": sample,
            "inputs": [
                {"role": "tumor_dna_bam",
                 "path": f"/demo/{sample}/tumor_WGS.bam",
                 "confidence": 0.92,
                 "basis": "filename pattern + read-group sample tag (mock)"},
                {"role": "normal_dna_bam",
                 "path": f"/demo/{sample}/normal_WGS.bam",
                 "confidence": 0.88,
                 "basis": "paired normal from sample sheet (mock)"},
            ],
            "missing": [],
            "ambiguous": [],
        }
        if has_rna:
            manifest["inputs"].append(
                {"role": "tumor_rna_bam",
                 "path": f"/demo/{sample}/tumor_RNA.bam",
                 "confidence": 0.74,
                 "basis": "RNA-seq naming convention (mock)"})
        else:
            manifest["missing"].append(
                "tumor RNA input - fusion/splice stages would be blocked")
        manifest["ambiguous"].append(
            "two candidate normal BAMs share the sample prefix - confirm "
            "which run is the matched normal")
        return manifest

    def _say(self, text: str, role: str = "agent", meta: dict | None = None) -> dict:
        meta = dict(meta or {})
        meta.setdefault("badges", ["agent suggestion"] if role == "agent"
                        else ["validated policy"])
        return self.store.msg_add(role, text, meta)

    def _policy(self, pol_id: str) -> dict:
        p = POLICY_BY_ID[pol_id]
        return self.store.msg_add("policy", p["text"],
                                  {"policy_id": pol_id, "source": p["source"],
                                   "badges": ["validated policy"]})

    # ------------------------------------------------------------------
    def handle(self, text: str) -> list[dict]:
        t = text.lower().strip()
        out: list[dict] = []

        if re.search(r"^\s*(hi|hello|hey|help|what can you)\b", t):
            out.append(self._say(
                "I am the scripted DEMO assistant (no LLM, no real pipeline). "
                "I can explain NeoAG stages and settings, propose an input "
                "manifest from a directory description, submit a formal "
                "analysis, and walk results. Try: \"analyze DEMO-P001\", "
                "\"status\", \"show top candidates\", \"what rules does "
                "NeoAG enforce\", \"research fusion tools\"."))
            return out

        if any(k in t for k in ("research", "discover", "sota", "alternativ",
                                "compare tool", "benchmark tool")):
            import threading
            from .server import publish  # prototype wiring
            from .store import new_id
            target = next((x for x in ("fusion", "splice", "hla", "stability",
                                       "presentation") if x in t), None)
            scope = "full" if not target or "pipeline" in t else "targeted"
            rid = new_id("res")
            self.store.research_add(rid, scope, target or "presentation")
            threading.Thread(
                target=__import__("backend.research", fromlist=["run_research"])
                .run_research, args=(rid, scope, target or "presentation",
                                     self.store, publish), daemon=True).start()
            out.append(self._policy("POL-002"))
            out.append(self._say(
                f"Started a SOTA discovery digest ({scope} scope, "
                f"{target or 'presentation'}). Every row is labelled "
                "literature-derived example content - no live search, no "
                "downloads, no benchmarks. Open the Discovery view."))
            return out

        if any(k in t for k in ("decision history", "decisions",
                                "what did you decide")):
            ds = self.store.decision_list(50)
            if not ds:
                out.append(self._say("No decisions recorded yet."))
                return out
            pend = [d for d in ds if d["status"] == "pending"]
            lines = "\n".join(
                f"- {d['ts'][:16]} [{d['decided_by'] or 'PENDING'}] "
                f"{d['stage']}: {d['decision_type']} -> "
                f"{d['chosen'] or 'waiting'}" for d in ds[:5])
            out.append(self._say(
                f"Decision history ({len(ds)} recorded, {len(pend)} pending): \n"
                + lines + "\n\nFull detail (options, evidence, policy version) "
                "is in each job's Decision History view."))
            return out

        if any(k in t for k in ("rule", "policy", "policies", "enforce", "validated")):
            out.append(self._policy("POL-002"))
            out.append(self._policy("POL-001"))
            out.append(self._policy("POL-003"))
            out.append(self._say(
                "Those three are quoted policy rules from the NeoAG "
                "repository. Anything I add is a suggestion only."))
            return out

        if any(k in t for k in ("manifest", "input", "directory", "where are")):
            lines = "\n".join(
                f"- {s['id']}: {s['label']} - {s['status']}"
                for s in mock_data.SAMPLES)
            out.append(self._say(
                "Describe where your sequencing data live and I will "
                "propose an input manifest for review. Demo cases: \n"
                + lines + "\n\nDEMO-P002 has no RNA inputs - the fusion and "
                "splice stages would be blocked in a real run."))
            return out

        m = re.search(r"demo-p00[0-9]", t)
        sample = m.group(0).upper() if m else None
        if sample and any(k in t for k in ("run", "start", "analyz", "process", "submit")):
            settings = self.store.setting("ui", {})
            mode = settings.get("mode", "auto")
            if mode == "advanced" and settings.get("stages"):
                stage_ids = settings["stages"]
            else:
                stage_ids = [s["id"] for s in mock_data.STAGES if s["required"]]
            res = self.jobs.submit(sample, stage_ids, mode,
                                   simulate_failure=bool(settings.get("simulate_failure")))
            out.append(self._say(
                f"I prepared a formal analysis for {sample} with "
                f"{len(stage_ids)} stages ({mode} settings). It is blocked on "
                "your approval - the agent cannot queue execute-class work "
                "without a human click (POL-005)."))
            out.append(self._policy("POL-005"))
            return out

        if sample and any(k in t for k in ("why", "explain", "candidate", "result", "top")):
            return self._results("latest")
        if any(k in t for k in ("result", "candidate", "top ", "top-", "ranking")):
            return self._results("latest")

        if any(k in t for k in ("status", "progress", "running", "job")):
            jobs = self.store.job_list(5)
            if not jobs:
                out.append(self._say("No jobs yet. Try \"analyze DEMO-P001\"."))
                return out
            lines = "\n".join(
                f"- {j['id']}: {j['state']}"
                + (f", {j['progress']}%, stage {j['stage']}" if j["stage"] else "")
                + (f" ({j['error']})" if j["error"] else "") for j in jobs)
            out.append(self._say("Latest jobs (live in the Tasks view):\n" + lines))
            return out

        if any(k in t for k in ("pause", "stop", "cancel", "resume")):
            out.append(self._say(
                "Use the pause / stop / resume buttons on the job card in "
                "the Tasks view - controls act on the job worker, not just "
                "the chat. Cancellation is cooperative at stage/substep "
                "boundaries."))
            return out

        if any(k in t for k in ("real", "pipeline", "connect", "production")):
            out.append(self._policy("POL-007"))
            out.append(self._say(
                "This prototype is NOT connected to the real pipeline (by "
                "design for this milestone). The intended real path is the "
                "NeoAg controlled-execution gateway on 127.0.0.1 - routes "
                "/open/run + /job-status/<id>, approvals, audit - and the "
                "integration gaps (cancel endpoint!) are listed in the "
                "README."))
            return out

        out.append(self._say(
            "DEMO agent here (scripted, no model). Try: \"analyze DEMO-P001\", "
            "\"what rules does NeoAG enforce\", \"status\", \"show top "
            "candidates\", \"what data do we have\"."))
        return out

    def _results(self, _which: str) -> list[dict]:
        done = [j for j in self.store.job_list(20) if j["state"] == "completed"]
        if not done:
            out = [self._say(
                "No completed mock run yet - run \"analyze DEMO-P001\" and "
                "approve it in the Approvals panel.")]
            return out
        job = done[0]
        import json
        rows = json.loads(job["result_json"] or "{}").get("candidates", [])[:3]
        lines = "\n".join(
            f"- #{r['rank']} {r['peptide']} ({r['gene']}, {r['hla']}, "
            f"{r['branch']}): mock score {r['mock_combined_score']}"
            for r in rows)
        return [
            self._policy("POL-002"),
            self._say(
                f"Top mock candidates from {job['id']} ({job['sample_id']}):\n"
                + lines +
                "\n\nOpen the Results view for full evidence columns. "
                "These are synthetic demo numbers, not predictions about "
                "anything real."),
        ]
