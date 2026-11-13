"""Scripted demo agent (no LLM is called anywhere in the prototype).

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
        self._context: list[str] = []  # rolling context for real runs
        self._run_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Real-agent integration (CodeBuddy gateway). OFF by default; the
    # operator switches modes in the UI (POST /api/agent/mode), and the
    # switch is audited. See backend/codebuddy_client.py for the API map
    # and its documented limits (no approval relay, rate limits).
    # ------------------------------------------------------------------
    def _ensure_client(self):
        if self._client is None:
            from .codebuddy_client import CodeBuddyClient, CodeBuddyError
            self._client = CodeBuddyClient()
        return self._client

    def handle_real(self, text: str) -> list[dict]:
        """Send the user text to the REAL server-side CodeBuddy agent and
        start a streaming relay thread. Returns the immediate message(s);
        agent/tool output arrives asynchronously via SSE ('chat' events)."""
        try:
            client = self._ensure_client()
        except Exception as e:
            return [self._say(
                f"Real-agent mode is ON but the CodeBuddy gateway is not "
                f"reachable from this server ({e}). Nothing was executed.",
                meta={"badges": ["agent suggestion", "integration error"]})]
        try:
            run_id = client.create_run(text, context=self._context)
        except Exception as e:
            return [self._say(
                f"Could not start a real-agent run: {e} Nothing was "
                "executed. (The gateway rate-limits run creation and "
                "allows one active run.)",
                meta={"badges": ["agent suggestion", "integration error"]})]
        note = self.store.msg_add(
            "agent",
            f"Real-agent run started on the server-side CodeBuddy gateway "
            f"(run {run_id[:8]}...). Output streams below as it arrives. "
            "Tool actions needing approval must still be approved in the "
            "operator's CodeBuddy Web UI - this interface never bypasses "
            "that gate.",
            {"badges": ["real agent", "streaming"],
             "source": "codebuddy", "run_id": run_id})
        self._context.append(f"user: {text[:140]}")
        self._run_thread = threading.Thread(
            target=self._relay_run, args=(run_id,), daemon=True)
        self._run_thread.start()
        return [note]

    def _relay_run(self, run_id: str) -> None:
        import html as _html
        from .server import publish
        client = self._client
        final_texts: list[str] = []
        n_tool = 0
        for ev in client.stream(run_id):
            etype = str(ev.get("status") or ev.get("type") or "")
            md = ""
            content = ev.get("content")
            if isinstance(content, dict):
                md = str(content.get("markdown") or "")
            elif isinstance(ev.get("markdown"), str):
                md = ev["markdown"]
            if ev.get("stream_error"):
                self.store.msg_add(
                    "system",
                    f"Real-agent stream ended abnormally: {ev['stream_error']}",
                    {"badges": ["integration error"], "run_id": run_id})
            elif ev.get("run_left_registry"):
                self.store.msg_add(
                    "agent",
                    f"(Run {run_id[:8]} finished; the gateway no longer "
                    "streams it and does not replay transcripts. Its "
                    "transcript, if the run was processed, is in the "
                    "operator's CodeBuddy Web UI.)",
                    {"badges": ["real agent", "integration note"],
                     "run_id": run_id})
            elif ev.get("stream_idle_timeout"):
                self.store.msg_add(
                    "agent",
                    f"(No streamed output arrived for {ev['stream_idle_timeout']}s "
                    f"while the gateway still reports run {run_id[:8]} active. "
                    "This interface will not fabricate the transcript - "
                    "check the operator's CodeBuddy Web UI for whether the "
                    "run was processed.)",
                    {"badges": ["real agent", "integration note"],
                     "run_id": run_id})
            elif etype == "completed" or md:
                final_texts.append(md or json.dumps(ev)[:400])
            elif etype:
                n_tool += 1
                self.store.msg_add(
                    "tool", f"[{etype}] {_html.escape(json.dumps(ev)[:200])}",
                    {"badges": ["tool output", "real agent"],
                     "run_id": run_id})
            publish({"type": "chat"})
        if final_texts:
            body = "\n\n".join(t for t in final_texts if t.strip()) or "(empty)"
            self.store.msg_add(
                "agent", body,
                {"badges": ["real agent"], "source": "codebuddy",
                 "run_id": run_id})
            self._context.append(f"agent: {body[:140]}")
        elif not n_tool:
            self.store.msg_add(
                "agent",
                f"(Run {run_id[:8]} finished without streamed content - "
                "check the operator CodeBuddy Web UI for the transcript.)",
                {"badges": ["real agent"], "run_id": run_id})
        publish({"type": "chat"})

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
