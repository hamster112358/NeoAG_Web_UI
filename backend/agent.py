"""Scripted demo agent (no LLM is called anywhere in the prototype).

Strict channel separation, per project requirements:
 - role="agent"      -> agent suggestion   (badge: "agent suggestion")
 - role="policy"     -> validated NeoAG policy text, quoted from policies.py
                        (badge: "validated policy", cites its source)
The agent never invents policy, never claims real computation happened, and
never references anything but synthetic DEMO- data.
"""
from __future__ import annotations

import re

from . import mock_data
from .policies import POLICY_BY_ID


class DemoAgent:
    def __init__(self, store, jobs):
        self.store = store
        self.jobs = jobs

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
                "I am the scripted DEMO assistant (no LLM, no real pipeline). I "
                "can walk you through a mock analysis of the synthetic cases "
                "DEMO-P001..P003: try \"analyze DEMO-P001\", \"status\", "
                "\"show top candidates\", \"what rules does NeoAG enforce\", "
                "\"research fusion tools\" or \"decision history\"."))
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
                f"Started a MOCK research digest ({scope} scope, "
                f"{target or 'presentation'}). It is example content only - no "
                "live search or benchmark was run, nothing is downloaded, and "
                "literature-derived rows stay separated from (absent) "
                "independently-benchmarked results. Open the Discovery panel."))
            return out

        if any(k in t for k in ("decision history", "decisions", "what did you decide")):
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
                "is in the Decisions panel."))
            return out

        if any(k in t for k in ("rule", "policy", "policies", "enforce", "validated")):
            out.append(self._policy("POL-002"))
            out.append(self._policy("POL-001"))
            out.append(self._policy("POL-003"))
            out.append(self._say(
                "Those three are quoted policy rules from the NeoAG repository "
                "(see the Policies panel for sources). Anything I add is a "
                "suggestion only."))
            return out

        if any(k in t for k in ("sample", "data", "case", "input", "file")):
            lines = "\n".join(
                f"- {s['id']}: {s['label']} - {s['status']}" for s in mock_data.SAMPLES)
            out.append(self._say(
                "Synthetic demo cases (all mock, browse them in the Data panel):\n"
                + lines + "\n\nDEMO-P002 has no RNA inputs, so in a real run the "
                "fusion/splice stages would be blocked; here you can see the "
                "stages you would need to deselect."))
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
                f"I prepared a MOCK analysis for {sample} with "
                f"{len(stage_ids)} stages ({mode} settings). It is waiting in the "
                "Approvals panel - I cannot queue execute-class work without a "
                "human click, even in the demo."))
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
            out.append(self._say("Latest jobs (live in the Tasks panel):\n" + lines))
            return out

        if any(k in t for k in ("pause", "stop", "cancel", "resume")):
            out.append(self._say(
                "Use the pause / stop / resume buttons on the job card in the "
                "Tasks panel - controls act on the job worker, not just the "
                "chat. Cancellation is cooperative at stage/substep boundaries "
                "(see README, job-control design)."))
            return out

        if any(k in t for k in ("real", "pipeline", "connect", "production")):
            out.append(self._policy("POL-007"))
            out.append(self._say(
                "This prototype is NOT connected to the real pipeline (by design "
                "for this milestone). The intended real path is the NeoAg "
                "controlled-execution gateway on 127.0.0.1 - routes "
                "/open/run + /job-status/<id>, approvals, audit - and the "
                "integration gaps (cancel endpoint!) are listed in the README."))
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
                "\n\nOpen the Results panel for full evidence columns. Reminder: "
                "these are synthetic demo numbers, not predictions about anything real."),
        ]
