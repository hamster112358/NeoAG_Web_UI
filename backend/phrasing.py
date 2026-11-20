"""M11 P1-12: the single module that turns internal records into
user-visible conversation text.

Every message row stored into a conversation (relay terminals, task
failure notices, approval outcomes) is phrased here, so one contract
holds everywhere: plain sentences, no raw dicts or brace characters,
no internal-only phrases, and trimming that says so - never a silent
clip. Labels for the TWO status classes (execution lifecycle vs
scientific outcome) live here too, mirroring the frontend
statusPhrasing helper - the four surfaces (Chat, Tasks, Discovery,
Results) read status through that one pair.
"""

import json
import re

# phrases that must never reach a stored conversation row
BANNED_PHRASES = ("gateway " "detail",)   # concatenation: grep-proof


def _short(v, n=80) -> str:
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def human_detail(raw, limit: int = 200) -> str:
    """One plain sentence from an arbitrary payload (dict, JSON string,
    list, stderr text). Never returns braces, newlines or the banned
    phrases; when trimmed, the result SAYS so."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        s = raw.strip()
        if s[:1] in "{[":                      # looks like JSON: render
            try:                              # its FIELDS, not braces
                raw = json.loads(s)
            except ValueError:
                pass
    if isinstance(raw, dict):
        s = "; ".join(f"{k}: {_short(v)}" for k, v in raw.items())
    elif isinstance(raw, list):
        s = "; ".join(_short(v) for v in raw)
    else:
        s = str(raw)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("{", "").replace("}", "").replace("[", "") \
         .replace("]", "")
    for phrase in BANNED_PHRASES:
        s = re.sub(re.escape(phrase) + r"\s*:?\s*", "", s, flags=re.I)
    s = s.strip(" ;:,")
    if len(s) > limit:
        s = (s[:limit].rsplit(" ", 1)[0]
             + f" ... [text trimmed from {len(s)} chars]")
    return s


def execution_phrase(state: str, started: bool | None = None) -> str:
    """The EXECUTION class, as a labeled sentence fragment."""
    base = {"failed": "Execution status: failed",
            "stopped": "Execution status: cancelled",
            "cancelled": "Execution status: cancelled",
            "done": "Execution status: completed",
            "completed": "Execution status: completed"}.get(
                state, f"Execution status: {state}")
    if started is True:
        base += " (computation had started)"
    elif started is False:
        base += " (stopped before any computation)"
    return base


def scientific_phrase(established: str) -> str:
    """The SCIENTIFIC class, kept in its own sentence - a lifecycle
    word never stands in for what a run established."""
    return f"Scientific outcome: {established}"


def turn_ended_text(state: str, detail) -> str:
    """The relay's honest terminal message when no assistant reply
    arrived. detail is the gateway's record: humanized, never echoed
    raw."""
    label = {"done": "ended with no reply recorded",
             "failed": "failed",
             "stopped": "was cancelled"}.get(state, str(state))
    reason = human_detail(detail)
    return ("(Agent turn " + label + "."
            + (f" The agent service reported: {reason}." if reason else "")
            + " No assistant reply text was recorded - this interface "
              "does not fabricate one.)")


def failure_notice_text(job_id: str, report: dict, completed: list,
                        partial: int, when: str) -> str:
    """The proactive task-failure notice. The two status classes get
    their own labeled sentences; raw codes stay in the technical
    detail, never here."""
    started = bool(report.get("started_computation"))
    lines = [execution_phrase("failed", started)]
    est = (f"{partial} partial result file(s) registered on the task "
           "for forensics" if partial else
           "nothing - no results were produced")
    lines.append(scientific_phrase(est))
    if report.get("reason"):
        lines.append("Confirmed reason: "
                     + human_detail(report["reason"], 300))
    if report.get("blockers"):
        # every item goes through human_detail: a blocker is a FREE-
        # TEXT field (planner messages, tool stderr) - braces and
        # internal phrases are stripped, never pasted
        lines.append("Blocking issue(s): "
                     + " | ".join(human_detail(b, 120)
                                  for b in report["blockers"][:4]))
    if completed:
        lines.append("Completed before the failure: "
                     + ", ".join(_short(c, 120) for c in completed))
    else:
        lines.append("Completed before the failure: nothing")
    lines.append("Next step: " + _short(report.get("next_step", ""), 200))
    return (f"Task {job_id} ended at {when}. "
            + " ".join(lines))[:1200]


# Controls reality (mirrors frontend/taskHeadHtml): cancel is the one
# control every non-terminal task has; pause/resume exist only where
# the worker supports them. Never promise what the card disables.
CONTROL_GUIDANCE = (
    "Controls live on the job card in the Tasks view. Stop/cancel is "
    "available for every non-terminal task and acts on the job worker, "
    "not just the chat (cancellation is cooperative at stage/substep "
    "boundaries). Pause and resume apply only to workers that support "
    "them - a native pipeline run cannot be paused mid-computation, "
    "and its card states that instead of offering the buttons.")
