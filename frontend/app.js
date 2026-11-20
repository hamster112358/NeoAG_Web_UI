/* NeoAG - frontend logic (vanilla JS, no build step). Milestone 9:
   bilingual (zh default), design-system rendering, real preparation
   workflow, human-readable task naming, preserved view state.
   Data comes from /api/bootstrap + per-conversation fetches + SSE.
   All writes go through the API; the browser only renders
   server-persisted state. Layout contract: nothing at page level
   scrolls - sidebar, chat log and work body each own
   their scroll; the composer stays pinned at the bottom. */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* ---------------- i18n (M9 §3) ----------------
   Centralized resources. zh is the default; the language persists in
   localStorage AND on the server (settings), so a new browser starts
   where the user left off. English literals are the static markup's
   fallback, so a missing key can never blank out a control.
   Filenames, scientific identifiers and server-generated records are
   deliberately NOT translated. */
const I18N = {
  en: {
    "brand.tag": "Agent-assisted neoantigen analysis",
    "chat.new": "New chat", "chat.stop": "Stop",
    "chat.tech": "Technical view",
    "chat.chat": "Conversation view",
    "chat.techTip": "Show the full per-call execution record",
    "chat.ph": "Message…",
    "chat.send": "Send",
    "chat.busySend": "The agent is still answering - one exchange at a time.",
    "chat.emptyTitle": "Start a conversation",
    "chat.emptySub": "Ask anything about NeoAG, or describe a sample and where its data live.",
    "chat.agentWorking": "agent working", "chat.awaiting": "awaiting confirmation",
    "chat.deleteQ": "Delete this chat? It disappears from this list; the saved messages stay on the server for audit.",
    "menu.rename": "Rename", "menu.pin": "Pin to top", "menu.unpin": "Unpin",
    "menu.archive": "Archive", "menu.unarchive": "Unarchive", "menu.delete": "Delete",
    "menu.archived": "Archived", "menu.noChats": "No chats yet.",
    "conv.msg": "msg",
    "tab.tasks": "Tasks", "tab.results": "Results", "tab.discovery": "Discovery",
    "tab.skills": "Skills", "tab.help": "Help",
    "filter.all": "All", "filter.analysis": "Analyses",
    "filter.discovery": "Discovery", "filter.diagnostic": "Diagnostics",
    "conn.live": "connected", "conn.off": "reconnecting",
    "ago.now": "just now", "ago.m": "m ago", "ago.h": "h ago", "ago.d": "d ago",
    "type.analysis": "Full neoantigen analysis",
    "type.discovery": "Partial analysis",
    "type.diagnostic": "Diagnostic check",
    "type.benchmark": "Benchmark experiment",
    "type.review": "Scientific review",
    "type.tooling": "Tool discovery",
    "type.task": "Task",
    "st.queued": "Queued", "st.running": "Running", "st.paused": "Paused",
    "st.blocked": "Waiting for approval", "st.completed": "Completed",
    "st.failed": "Failed", "st.cancelled": "Cancelled",
    "concl.decision_ready": "Scientific conclusion: review complete",
    "concl.preliminary": "Scientific conclusion: review incomplete",
    "attn.waiting": "waiting for you",
    "attn.execute": "Execute approval",
    "attn.bench": "Benchmark consent",
    "attn.decision": "Decision",
    "attn.approve": "Approve", "attn.deny": "Deny", "attn.reject": "Reject",
    "attn.limit": "Limit scope…", "attn.lit": "Literature only",
    "attn.scopePrompt": "What scope will you allow? (e.g. 'subsample 5 of 200 samples; CPU only; no GPU')",
    "attn.policyDefault": "(policy default)",
    "gate.q": "Confirm destructive operation",
    "gate.sub": "The execution gate blocked this before it ran. Approving unlocks exactly this one operation; nothing else. It is also recorded in the audit trail.",
    "gate.approve": "Approve once", "gate.deny": "Deny",
    "gate.scopeBtn": "Always allow this scope",
    "gate.scopeTitle": "Approves this operation now, and auto-allows further writes under the recorded target directories for 4 hours. Server-enforced, audited, revocable at any time.",
    "gate.queued": "Queued - an earlier approval is waiting first",
    "gate.staleGone": "This confirmation is no longer valid (the task ended or it expired)",
    "gate.effect": "Effect", "gate.reversibility": "Reversibility",
    "gate.ec.write": "write", "gate.ec.overwrite": "overwrite",
    "gate.ec.delete": "delete", "gate.ec.network": "network",
    "gate.ec.network-read": "network read",
    "gate.ec.write-or-execute": "write or execute",
    "gate.ec.undetermined": "not determinable from the command text",
    "attn.scopesH": "Always-allow scopes (revocable)",
    "attn.scopeUntil": "until", "attn.revoke": "Revoke",
    "toast.revokeOk": "Scope revoked - the execution gate no longer auto-allows it",
    "toast.revokeFail": "revoke failed: {e}",
    "act.waitappr": "Waiting for your approval",
    "act.reconnect": "Reconnecting - the last recorded state is shown below",
    "act.doing": "Working on", "act.think": "Thinking",
    "act.silent": "no new events for {m} min - the job is still running",
    "act.lastturn": "Last turn",
    "act.tools1": "Agent activity - 1 tool call",
    "act.toolsN": "Agent activity - {n} tool calls",
    "act.done": "done", "act.failed": "failed", "act.running": "running",
    "role.user": "You", "role.agent": "Assistant", "role.system": "System",
    "role.policy": "Notice", "role.tool": "Tool",
    "tool.done": "done", "tool.failed": "failed", "tool.running": "...",
    "prep.title": "Prepare an analysis",
    "prep.dirlabel": "Sequencing data directory",
    "prep.dirhint": "Enter the folder that holds this case's data; NeoAG recognizes the files inside it.",
    "prep.discover": "Discover inputs",
    "prep.discovering": "Scanning {dir}",
    "prep.elapsed": "{s}s",
    "prep.needDir": "Enter the sequencing data directory first",
    "prep.scanning": "The NeoAG detector is reading the directory (nothing is modified).",
    "prep.failTitle": "Discovery failed",
    "prep.failFix": "Nothing was proposed from this directory and nothing was substituted. Check the path and permissions, then try again.",
    "prep.timeout": "The scan took too long and was closed. The directory may be very large or on slow storage - start a fresh scan when ready.",
    "prep.caseName": "Analysis name",
    "prep.caseFrom": "derived from the directory name",
    "prep.caseEdit": "Rename",
    "prep.confirm": "Confirm these inputs",
    "prep.confirmedBtn": "Confirmed",
    "prep.rescan": "Scan a different folder",
    "prep.submit": "Start analysis",
    "prep.approval": "Starting the analysis authorizes exactly this run on this host; destructive or out-of-scope operations still require their own confirmation.",
    "prep.submitted": "Task {id} created - it proceeds in the background; watch it in Tasks.",
    "prep.submittedCtrl": "Task {id} created - when planning finishes it will wait for your Run/Abort decision before anything executes.",
    "prep.needConfirm": "First discover the inputs and confirm them.",
    "inv.title": "Detected inputs",
    "inv.files": "{n} files", "inv.dirs": "{d} directories",
    "inv.recognized": "{n} recognized analysis inputs",
    "inv.excluded": "{n} files are not analysis inputs",
    "inv.ambiguous": "{n} items need your decision",
    "inv.blocked": "{n} blocking problem(s)",
    "inv.ok": "This directory is ready for analysis",
    "inv.advisories": "Left out of the selected workflow (not blocking):",
    "kind.missing_patient_input": "Missing patient input",
    "kind.missing_server_reference": "Missing server reference",
    "kind.ambiguous": "Needs your choice",
    "kind.unsupported_format": "Unusable file",
    "kind.missing_tool": "Missing tool / runtime",
    "kind.unselected_stage_dependency": "Not needed by the selected workflow",
    "kind.invalid_config": "Invalid configuration",
    "kind.not_authorized": "Outside authorized roots",
    "kind.planner_verdict": "Planner verdict",
    "kind.environment": "Environment",
    "basis.content": "by content",
    "basis.filename": "by filename (heuristic)",
    "inv.needPick": "Choose the intended file for each highlighted item",
    "inv.tree": "Folder contents",
    "inv.notInput": "not an input",
    "inv.role": "Role", "inv.file": "File", "inv.state": "State",
    "inv.bamIndex": "alignment index {v}",
    "inv.sampleId": "Sample identifier",
    "inv.relFromVcf": "read from the data: {v}",
    "inv.noRel": "the directory name is used as the case name",
    "inv.dna": "DNA sequencing", "inv.rna": "RNA sequencing",
    "inv.ev": "Existing evidence", "inv.none": "none found",
    "blk.title": "Cannot start yet",
    "warn.title": "Needs attention",
    "info.title": "Note",
    "pick.q": "Which file should be used for {label}?",
    "pick.need": "Choose one file before confirming.",
    "sci.summary": "Scientific configuration:",
    "sci.defaults": "NeoAG defaults",
    "sci.n1": "1 change - recorded in the run",
    "sci.nn": "{n} changes - recorded in the run",
    "sci.reset": "Restore defaults",
    "sci.unavail": "The parameter inventory is unavailable right now - nothing is shown rather than guessed.",
    "sci.unchecked": "Run normally (recommended)",
    "plan.summary": "Planning details (technical)",
    "plan.loading": "The NeoAG planner is checking the confirmed inputs - this can take a minute.",
    "plan.fail": "The plan preview failed: {e}",
    "plan.failFix": "You can retry; nothing runs during planning.",
    "plan.retry": "Retry plan review",
    "plan.unavail": "Native execution is not available on this host, so a plan cannot be produced and submission is disabled. This is reported honestly instead of being substituted.",
    "plan.blocked": "The planner found blocking problems:",
    "plan.status": "Plan:",
    "ps.PASS": "will run", "ps.DRY_RUN": "will run (validated)",
    "ps.SKIPPED": "not applicable", "ps.BLOCKED": "blocked",
    "ps.FAILED": "problem",
    "ps.PLANNED_IN_PRODUCTION_DAG": "planned in the production run",
    "plan.runplan": "Generated plan (technical)",
    "run.title": "Running & recent", "run.history": "Task history",
    "run.none": "Nothing is running yet. Prepare an analysis above, or ask the agent in chat.",
    "run.histnone": "No finished tasks yet.",
    /* M11 P1-9: task management (pin/lock/trash, search/sort, bulk) */
    "tm.search": "Search tasks...",
    "tm.sortNew": "Newest", "tm.sortOld": "Oldest", "tm.sortName": "Name",
    "tm.showTrash": "Trash", "tm.backToList": "Back to tasks",
    "tm.trashEmpty": "Trash is empty.",
    "tm.trashNote": "Trash holds RECORDS only. Nothing here deletes "
      + "files; removal is always a manual, confirmed step.",
    "tm.trashAge": "moved to trash {d}",
    "tm.retentionHint": "Retention: records are kept here {d} days; "
      + "nothing is ever removed automatically - the choice stays "
      + "with the operator.",
    "tm.pin": "Pin", "tm.unpin": "Unpin",
    "tm.lock": "Lock", "tm.unlock": "Unlock",
    "tm.trashBtn": "Trash", "tm.restore": "Restore",
    "tm.lockedTag": "locked", "tm.pinnedTag": "pinned",
    "tm.trashedTag": "trashed",
    "tm.cancelFirst": "active task - cancel it before trashing",
    "tm.lockedFirst": "locked - unlock it first",
    "tm.purge": "Delete record forever", "tm.purgeBtn": "Delete record",
    "tm.purgeNote": "This deletes the task RECORD (its events, "
      + "decisions and output-manifest rows). Files under the run "
      + "directory are NEVER removed, and anything referencing them "
      + "keeps working. This cannot be undone - type the task id to "
      + "confirm.",
    "tm.bulkN": "{n} selected", "tm.bulkClear": "Clear",
    "tm.pinned": "Task pinned", "tm.unpinned": "Task unpinned",
    "tm.locked": "Task locked - it is now protected from trash and "
      + "record deletion",
    "tm.unlocked": "Task unlocked",
    "tm.trashed": "Moved to trash (record only - files untouched)",
    "tm.restored": "Task restored",
    "tm.purged": "Record deleted; files were not touched",
    "tm.trashRowClick": "Trashed record - restore it to open, or "
      + "delete the record below (files are never removed)",
    "tm.purgeMismatch": "Type the exact task id to confirm",
    "tm.bulkDone": "{ok} task(s) updated",
    "tm.bulkPart": "{ok} updated, {ref} refused (last reason: {why})",
    /* M11 P1-10: file intake */
    "att.add": "Attach file", "att.pick": "Pick on server",
    "att.uploading": "Uploading {n}...", "att.cancel": "Cancel",
    "att.cancelled": "Upload cancelled - nothing was stored",
    "att.tooBig": "{n} is {s} bytes, over the {cap}-byte cap; "
      + "nothing was uploaded",
    "att.badType": "Unsupported file type for {n} - documents, "
      + "reports, tables and images only; sequencing files belong to "
      + "the formal ingestion path",
    "att.chipTitle": "{kind} - {size} bytes - the agent receives the "
      + "PATH, never the bytes",
    "att.remove": "Remove", "att.pickedNote": "referenced in place",
    "att.egressNote": "Attachments travel as paths - the file bytes "
      + "never leave this machine; every reference is audited.",
    "att.up": ".. up", "att.roots": "Authorized input roots",
    "att.seqNote": "sequencing input - formal ingestion only",
    "att.unsup": "unsupported type",
    "att.badSend": "an upload is still in flight - cancel it or wait",
    "toast.attFail": "upload failed: {e}",
    "toast.pickFail": "could not reference that file: {e}",
    "dr.close": "Close", "dr.rename": "Rename", "dr.renameSave": "Save",
    "dr.renameCancel": "Cancel", "dr.renamed": "Title updated",
    "dr.id": "Task ID", "dr.created": "Created", "dr.finished": "Finished",
    "dr.exec": "Execution status", "dr.concl": "Scientific conclusion",
    "dr.controls": "Controls", "dr.pause": "Pause", "dr.resume": "Resume",
    "dr.cancel": "Cancel",
    "dr.noPause": "This run type has no safe pause point.",
    "dr.cancelNote": "Cancelling stops the task here; a native gateway job cannot be recalled once submitted (verified limitation).",
    "dr.tech": "Technical details (audit)", "dr.decisions": "Decision history",
    "dr.artifacts": "Output artifacts", "dr.reqcfg": "Requested configuration",
    "dr.effcfg": "Effective configuration (what this run really applied)",
    "dr.versions": "Versions",
    "dr.snapshot": "Run snapshot", "dr.events": "Live events",
    "dr.snapFull": "full recorded snapshot",
    "dr.snapNote": "Written once when the run started; never updated.",
    "dr.noDec": "No decisions recorded", "dr.defOnly": "defaults only",
    "dr.noArt": "No artifacts registered yet",
    "res.title": "Results", "res.pick": "Run", "res.none": "No runs yet.",
    "res.showMock": "Show simulation history",
    "res.inprogress": "This run has not finished yet ({state}) - its results will appear here when it ends.",
    "res.cancelledNote": "This run was cancelled; nothing was produced after cancellation.",
    "res.previewH": "Final ranking tables (as stored in the run output)",
    "res.previewNote": "Shown verbatim from the run's own output files - nothing is re-ranked or reformatted here.",
    "res.dl": "Download",
    "res.discoveryRow": "Discovery review",
    "res.discoveryNote": "This is a Discovery review, not a pipeline run - its report belongs to the Discovery workspace.",
    "res.openDiscovery": "Open the report in Discovery",
    "res.shortlist": "Shortlist (first rows of the final ranking)",
    "res.prov": "Output manifest, decisions and provenance",
    "res.bundle": "Download results bundle (.zip)",
    "res.download": "built from the persisted run state; raw FASTQ/BAM-class inputs are never included and exclusions are listed with reasons; re-download serves the same pinned bytes",
    "res.artH": "Output artifacts ({n})",
    "res.noArtReg": "No artifacts are registered for this run - nothing from it can be described as delivered.",
    "res.ranking": "Ranking", "res.empty0": "0 rows - an empty candidate table is a legitimate result; the artifact status and the run outcome above both say so.",
    "res.unavail": "Results unavailable: {e}",
    "disc.title": "Tool discovery & benchmarking",
    "disc.intro": "Searches public scientific sources and your configured NeoAG inventory to review analysis tools. It reports what it verified and marks the rest as not verified.",
    "disc.scope": "Scope", "disc.scopeT": "Targeted review",
    "disc.scopeF": "Full pipeline review",
    "disc.run": "Start review", "disc.targets": "Targets",
    "disc.targetsT": "(one or more; from the configured repository's real stage inventory)",
    "disc.targetsF": "- full pipeline review covers every inventoried stage",
    "disc.custom": "Custom target", "disc.customPh": "e.g. neoantigen clonality scoring",
    "disc.none": "No research runs yet.",
    "disc.needTarget": "Pick at least one target (or a custom one) for a targeted review",
    "disc.started": "Research {id} started",
    "disc.docsStage": "docs-level stage", "disc.uses": "uses:",
    "disc.reportFail": "report failed: {e}",
    "disc.cancelBtn": "Cancel run", "disc.cancelNote": "stops after the current query; progress is kept",
    "disc.resumeBtn": "Resume (persisted queries skipped)",
    "disc.partNote": "This run did not finish; nothing here is a complete review. Resume or accept the partial result.",
    "disc.noRep": "No research runs yet.",
    "skill.title": "Agent skills",
    "skill.intro": "Skills are advisory instructions the agent reads when a chat starts. They guide wording and style; they cannot change approvals, safety gates or scientific settings. Changes apply to new chats.",
    "skill.new": "New skill", "skill.none": "No skills yet.",
    "skill.edit": "Edit", "skill.enable": "Enable", "skill.disable": "Disable",
    "skill.enabled": "enabled", "skill.disabled": "disabled",
    "skill.loaded": "not loaded", "skill.applies": "applies to new chats",
    "skill.savedNot": "saved - not applied yet",
    "skill.adv": "Advanced (version history, raw text)",
    "sk.editTitle": "Edit skill", "sk.newTitle": "New skill",
    "sk.name": "Name", "sk.namePh": "What is this skill called?",
    "sk.body": "Instructions",
    "sk.bodyPh": "What should the agent know or do differently? Write it in plain language - the agent reads this at the start of each new chat.",
    "sk.save": "Save", "sk.cancel": "Cancel",
    "sk.note": "Changes apply to new chats.",
    "help.title": "Help", "help.unavail": "unavailable",
    "st.incomplete": "incomplete",
    "res.noCand": "No candidate rows to display.",
    "disc.dl": "Download {f}", "disc.dlTsv": "Download TSV tables",
    "disc.dlNote": "persisted per report version - re-download never re-runs retrieval",
    "disc.compat": "Check compatibility", "disc.plan": "Adoption plan",
    "disc.bench": "Propose benchmark",
    "disc.actNote": "read-only checks; a proposed benchmark is a FORMAL job that waits for your approval before anything runs",
    "disc.benchDone": "Benchmark PROPOSED: job {id} is blocked on approval {a} - it runs only if you approve the card.",
    "disc.compatT": "Compatibility check (read-only): {t}",
    "disc.planT": "Adoption plan - {t} ({tg})",
    "disc.notApplied": "not applied",
    "disc.noDirect": "No retrieved candidate passed the scientific relevance screen as a direct alternative for this module.",
    "disc.noProg": "no persisted progress yet",
    "disc.ignored": " - ignored unknown targets: {u}",
    "disc.engineL": "live", "disc.engineE": "example",
    "disc.h1": "1. Executive assessment",
    "disc.h2": "2. Module-level recommendations",
    "disc.h3": "3. Scientific comparison",
    "disc.h4": "4. Recommended actions (scientific priority)",
    "disc.h5": "5. Technical appendix (exclusions, full candidates, version chains, evidence records, coverage, gates, citations, provenance)",
    "disc.qReady": "decision-ready review", "disc.qPre": "PRELIMINARY RESEARCH",
    "disc.whatIs": "What this report is (and is not)",
    "disc.questionL": "Question:", "disc.curMethod": "Current NeoAG method:",
    "disc.exclL": "Excluded candidates - every retrieval that failed the scientific eligibility screen, with its reason",
    "disc.fullTbl": "Full candidate comparison table",
    "disc.verChains": "Version verification chains",
    "disc.coverageL": "Coverage accounting",
    "disc.gatesL": "Research quality gates",
    "disc.citesL": "Sources and citations",
    "disc.methL": "Methodology / limitations / provenance",
    "sk.hist": "Version history", "sk.raw": "Raw SKILL.md",
    "sk.restore": "restore",
    "attn.resolvedNote": "resolved in Tasks panel",
    "toast.actFail": "action failed: {e}",
    "toast.researchFail": "research failed: {e}",
    "toast.cancelFail": "cancel failed: {e}",
    "toast.resumeFail": "resume failed: {e}",
    "toast.apprFail": "approval failed: {e}",
    "toast.decFail": "resolve failed: {e}",
    "toast.togFail": "toggle failed: {e}",
    "toast.verFail": "versions failed: {e}",
    "toast.rawFail": "cannot load raw text: {e}",
    "toast.restoreFail": "restore failed: {e}",
    "toast.restoreOk": "restored v{v} as a new version - applies to new chats",
    "toast.skillLoadFail": "cannot load skill: {e}",
    "toast.saveApplyFail": "saved, but applying failed: {st} {err}",
    "toast.boot": "boot failed: {e}",
    "toast.sendFail": "send failed: {e}",
    "toast.newChatFail": "new chat failed: {e}",
    "toast.stopFail": "stop failed: {e}",
    "toast.renamed": "Title updated",
    "toast.renameFail": "rename failed: {e}",
    "toast.openFail": "cannot open task: {e}",
    "toast.confirmFail": "Could not record the confirmed inputs: {e}",
    "toast.confirmed": "Inputs confirmed - planning starts now",
    "toast.pickFirst": "Choose the intended file for: {items}",
    "toast.dupActive": "This sample already has an active run ({id}) - not submitted again. {d}",
    /* M10: execution modes, analysis options, readiness, failure
       cards, unified dialogs, central attention */
    "mode.label": "Execution",
    "mode.auto": "Autonomous",
    "mode.ctrl": "Controlled",
    "mode.help": "Autonomous: planning runs, and the planner's own pass leads straight to execution under the authorization you give below. Controlled: after planning, the plan waits for your explicit Run/Abort decision.",
    "opt.title": "Analysis options",
    "opt.note": "Native stages in the RUN direction: required steps always run and cannot be switched off. Optional branches state what runs - the DNA SV branch is OFF by default here, tick to run it; the sample-identity guard stays on unless deliberately disabled. Steps marked not applicable come from the planner's own verdict on this data.",
    "opt.req": "Always runs",
    "opt.runs": "Runs",
    "opt.off": "Skipped this run",
    "opt.offDefault": "Off by default here",
    "opt.guardAck": "I confirm the tumor/normal sample identity was verified outside this run; turning the guard off is recorded with the job.",
    "opt.na": "Not applicable this run",
    "rdy.title": "Readiness check",
    "rdy.run": "Check the whole workflow now",
    "rdy.checking": "Checking every stage of the workflow against the server - this can take a minute.",
    "rdy.ready": "Everything required is in place; the analysis can start.",
    "rdy.blockedNote": "Cannot start yet - all blocking items are listed:",
    "rdy.warnNote": "Noted, not blocking:",
    "rdy.checkedAt": "Checked",
    "rdy.native_runtime": "Native NeoAG runtime",
    "rdy.gateway_service": "Execution gateway service",
    "rdy.tools_env": "Tool environment",
    "rdy.tools_manifest": "Tool inventory",
    "rdy.reference_manifest": "Reference inventory",
    "rdy.reference_fasta": "Reference genome file",
    "rdy.genome_build": "Genome build",
    "rdy.vep": "Variant annotation (VEP)",
    "rdy.scientific_config": "Scientific configuration",
    "rdy.work_writable": "Working directory writable",
    "rdy.manifest": "Confirmed inputs",
    "rdy.inputs_present": "Input files present",
    "rdy.inputs_authorized": "Input locations authorized",
    "rdy.demo_fixtures": "Demo fixtures",
    "rdy.planner": "Planner verdict",
    "st.waitdec": "Waiting for your decision",
    "fail.head": "This run failed",
    "fail.startedYes": "The computation had already started.",
    "fail.startedNo": "No computation started - nothing scientific was executed.",
    "fail.blockersH": "Blocking issue(s)",
    "fail.completedH": "Completed before the failure",
    "fail.completedNone": "nothing",
    "fail.partialN": "{n} partial output file(s) are registered on the task for inspection.",
    "fail.partial0": "No partial outputs were produced.",
    "fail.next": "Next step",
    "fail.open": "Open task",
    "gate.target": "Operation target",
    "gate.consequence": "What approving does",
    "gate.approveEffect": "If you approve",
    "gate.denyEffect": "If you deny",
    "attn.center": "Waiting for you",
    "attn.badgeTitle": "{n} item(s) need your attention",
    "dec.nativePlan": "The native planner cleared this run - execute now, or stop with the plan kept?",
    "dlg.close": "Close", "dlg.cancel": "Cancel", "dlg.confirm": "Confirm",
  },
  zh: {
    "brand.tag": "智能体辅助新抗原分析平台",
    "chat.new": "新建对话", "chat.stop": "停止",
    "chat.tech": "技术视图",
    "chat.chat": "对话视图",
    "chat.techTip": "显示完整的逐次执行记录",
    "chat.ph": "发送消息…",
    "chat.send": "发送",
    "chat.busySend": "智能体正在回复——请等本轮回答结束后再发送下一条。",
    "chat.emptyTitle": "开始对话",
    "chat.emptySub": "可以询问任何关于 NeoAG 的问题，或描述一个样本及其数据所在的位置。",
    "chat.agentWorking": "智能体工作中", "chat.awaiting": "等待确认",
    "chat.deleteQ": "删除这个对话？它会从列表中消失；已保存的消息仍留在服务器上以备审计。",
    "menu.rename": "重命名", "menu.pin": "置顶", "menu.unpin": "取消置顶",
    "menu.archive": "归档", "menu.unarchive": "取消归档", "menu.delete": "删除",
    "menu.archived": "已归档", "menu.noChats": "暂无对话。",
    "conv.msg": "条消息",
    "tab.tasks": "任务", "tab.results": "结果", "tab.discovery": "研究",
    "tab.skills": "技能", "tab.help": "帮助",
    "filter.all": "全部", "filter.analysis": "分析",
    "filter.discovery": "研究", "filter.diagnostic": "诊断",
    "conn.live": "已连接", "conn.off": "重连中",
    "ago.now": "刚刚", "ago.m": "分钟前", "ago.h": "小时前", "ago.d": "天前",
    "type.analysis": "完整新抗原分析",
    "type.discovery": "部分分析",
    "type.diagnostic": "诊断检查",
    "type.benchmark": "基准测试实验",
    "type.review": "科学研究评估",
    "type.tooling": "工具发现",
    "type.task": "任务",
    "st.queued": "排队中", "st.running": "进行中", "st.paused": "已暂停",
    "st.blocked": "等待批准", "st.completed": "已完成",
    "st.failed": "失败", "st.cancelled": "已取消",
    "concl.decision_ready": "科学结论：评估完成",
    "concl.preliminary": "科学结论：评估未完成",
    "attn.waiting": "等待你的处理",
    "attn.execute": "执行批准",
    "attn.bench": "基准测试授权",
    "attn.decision": "决策",
    "attn.approve": "批准", "attn.deny": "拒绝", "attn.reject": "拒绝",
    "attn.limit": "限定范围…", "attn.lit": "仅文献",
    "attn.scopePrompt": "你允许的范围是什么？（例如“抽样 200 例中的 5 例；仅 CPU；不用 GPU”）",
    "attn.policyDefault": "（策略默认）",
    "gate.q": "确认危险操作",
    "gate.sub": "执行门禁已在其运行前拦截。批准只会放开这一项操作，且会记录在审计日志中。",
    "gate.approve": "批准一次", "gate.deny": "拒绝",
    "gate.scopeBtn": "始终允许此范围",
    "gate.scopeTitle": "先批准本次操作；之后 4 小时内向上述目标目录的写入将自动放行。由服务端强制执行、留有审计、可随时撤销。",
    "gate.queued": "排队中——请先处理更早的确认",
    "gate.staleGone": "该确认已失效（任务已结束或已过期）",
    "gate.effect": "影响类型", "gate.reversibility": "可逆性",
    "gate.ec.write": "写入", "gate.ec.overwrite": "覆盖",
    "gate.ec.delete": "删除", "gate.ec.network": "网络",
    "gate.ec.network-read": "网络读取",
    "gate.ec.write-or-execute": "写入或执行",
    "gate.ec.undetermined": "无法从命令文本确定",
    "attn.scopesH": "始终允许范围（可撤销）",
    "attn.scopeUntil": "有效期至", "attn.revoke": "撤销",
    "toast.revokeOk": "已撤销该范围——执行网关不再自动放行",
    "toast.revokeFail": "撤销失败：{e}",
    "act.waitappr": "等待你的批准",
    "act.reconnect": "连接中断，正在重连——以下显示最后记录的状态",
    "act.doing": "正在处理", "act.think": "思考中",
    "act.silent": "{m} 分钟内无新事件——任务仍在运行",
    "act.lastturn": "上一轮：",
    "act.tools1": "智能体活动——1 次工具调用",
    "act.toolsN": "智能体活动——{n} 次工具调用",
    "act.done": "完成", "act.failed": "失败", "act.running": "运行中",
    "role.user": "我", "role.agent": "助手", "role.system": "系统",
    "role.policy": "提示", "role.tool": "工具",
    "tool.done": "完成", "tool.failed": "失败", "tool.running": "...",
    "prep.title": "准备新分析",
    "prep.dirlabel": "测序数据目录",
    "prep.dirhint": "输入存放本病例数据的文件夹；NeoAG 会识别其中的文件。",
    "prep.discover": "开始识别",
    "prep.discovering": "正在扫描 {dir}",
    "prep.elapsed": "{s} 秒",
    "prep.needDir": "请先输入测序数据目录",
    "prep.scanning": "NeoAG 识别器正在读取目录（不会修改任何文件）。",
    "prep.failTitle": "识别失败",
    "prep.failFix": "该目录未被生成任何提案，也没有用其他数据替代。请检查路径与读取权限后重试。",
    "prep.timeout": "扫描超时已关闭。目录可能很大或存储较慢——准备好后可重新扫描。",
    "prep.caseName": "分析名称",
    "prep.caseFrom": "由目录名推导",
    "prep.caseEdit": "重命名",
    "prep.confirm": "确认这些输入",
    "prep.confirmedBtn": "已确认",
    "prep.rescan": "扫描其他文件夹",
    "prep.submit": "开始分析",
    "prep.approval": "开始分析即表示你授权在本机上仅执行这一次运行；破坏性或超出范围的操作仍会单独请求确认。",
    "prep.submitted": "任务 {id} 已创建并在后台进行——可在任务列表查看进度。",
    "prep.submittedCtrl": "任务 {id} 已创建——规划完成后会等待你明确选择「执行」或「中止」，在此之前不会运行任何计算。",
    "prep.needConfirm": "请先识别并确认输入数据。",
    "inv.title": "识别到的输入",
    "inv.files": "{n} 个文件", "inv.dirs": "{d} 个文件夹",
    "inv.recognized": "{n} 个被识别的分析输入",
    "inv.excluded": "{n} 个文件不是分析输入",
    "inv.ambiguous": "{n} 项需要你决定",
    "inv.blocked": "{n} 个阻断问题",
    "inv.ok": "该目录已满足分析条件",
    "inv.advisories": "不在所选流程内（不构成阻断）：",
    "kind.missing_patient_input": "缺少患者样本数据",
    "kind.missing_server_reference": "缺少服务器参考数据",
    "kind.ambiguous": "需要你确认",
    "kind.unsupported_format": "文件不可用（空或格式问题）",
    "kind.missing_tool": "缺少工具/运行时",
    "kind.unselected_stage_dependency": "所选流程不需要",
    "kind.invalid_config": "配置无效",
    "kind.not_authorized": "不在授权目录内",
    "kind.planner_verdict": "规划器判定",
    "kind.environment": "环境",
    "basis.content": "依据内容识别",
    "basis.filename": "依据文件名（启发式）",
    "inv.needPick": "请为每个高亮项选择要使用的文件",
    "inv.tree": "文件夹内容",
    "inv.notInput": "非输入文件",
    "inv.role": "角色", "inv.file": "文件", "inv.state": "状态",
    "inv.bamIndex": "比对索引 {v}",
    "inv.sampleId": "样本标识",
    "inv.relFromVcf": "从数据中读出：{v}",
    "inv.noRel": "以目录名作为病例名",
    "inv.dna": "DNA 测序", "inv.rna": "RNA 测序",
    "inv.ev": "既有证据", "inv.none": "未找到",
    "blk.title": "暂时无法开始",
    "warn.title": "需要注意",
    "info.title": "说明",
    "pick.q": "{label} 应使用哪个文件？",
    "pick.need": "请先选择一个文件，再确认。",
    "sci.summary": "科学配置：",
    "sci.defaults": "NeoAG 默认",
    "sci.n1": "已修改 1 项——将记录在本次运行中",
    "sci.nn": "已修改 {n} 项——将记录在本次运行中",
    "sci.reset": "恢复默认",
    "sci.unavail": "暂时取不到参数清单——宁可不显示，也不猜测选项。",
    "sci.unchecked": "正常运行（推荐）",
    "plan.summary": "规划详情（技术细节）",
    "plan.loading": "NeoAG 规划器正在核查已确认的输入——可能需要一分钟。",
    "plan.fail": "计划预览失败：{e}",
    "plan.failFix": "可以重试；规划期间不会运行任何计算。",
    "plan.retry": "重新查看计划",
    "plan.unavail": "本机当前无法进行原生执行，因此无法生成计划，提交已停用。系统如实报告，而不会用其他方式替代。",
    "plan.blocked": "规划器发现以下阻断问题：",
    "plan.status": "计划：",
    "ps.PASS": "将执行", "ps.DRY_RUN": "将执行（已校验）",
    "ps.SKIPPED": "不适用", "ps.BLOCKED": "被阻断",
    "ps.FAILED": "存在问题",
    "ps.PLANNED_IN_PRODUCTION_DAG": "将纳入正式运行流程",
    "plan.runplan": "生成的计划（技术细节）",
    "run.title": "进行中与最近", "run.history": "任务历史",
    "run.none": "目前没有进行中的任务。可在上方准备新分析，或在对话中询问智能体。",
    "run.histnone": "暂无已完成的任务。",
    /* M11 P1-9：任务管理（置顶/锁定/回收站、搜索排序、批量） */
    "tm.search": "搜索任务…",
    "tm.sortNew": "最新", "tm.sortOld": "最早", "tm.sortName": "名称",
    "tm.showTrash": "回收站", "tm.backToList": "返回任务列表",
    "tm.trashEmpty": "回收站为空。",
    "tm.trashNote": "回收站只保存记录。此处不会删除任何文件；"
      + "彻底移除始终是手动、需确认的步骤。",
    "tm.trashAge": "移入回收站 {d}",
    "tm.retentionHint": "保留期：记录在此保存 {d} 天；系统绝不会自动删除，"
      + "决定权始终在操作者手中。",
    "tm.pin": "置顶", "tm.unpin": "取消置顶",
    "tm.lock": "锁定", "tm.unlock": "解锁",
    "tm.trashBtn": "移入回收站", "tm.restore": "恢复",
    "tm.lockedTag": "已锁定", "tm.pinnedTag": "已置顶",
    "tm.trashedTag": "回收站",
    "tm.cancelFirst": "任务进行中 - 请先取消再移入回收站",
    "tm.lockedFirst": "已锁定 - 请先解锁",
    "tm.purge": "永久删除记录", "tm.purgeBtn": "删除记录",
    "tm.purgeNote": "这将删除任务记录（事件、决策与产物清单行）。"
      + "运行目录中的文件绝不会被删除，引用它们的其它记录继续可用。"
      + "此操作不可撤销 - 请输入任务 id 确认。",
    "tm.bulkN": "已选 {n} 项", "tm.bulkClear": "清除",
    "tm.pinned": "已置顶", "tm.unpinned": "已取消置顶",
    "tm.locked": "已锁定 - 该任务现在受保护，不能移入回收站或删除记录",
    "tm.unlocked": "已解锁",
    "tm.trashed": "已移入回收站（仅记录 - 文件未动）",
    "tm.restored": "已恢复",
    "tm.purged": "记录已删除；文件未动",
    "tm.trashRowClick": "回收站中的记录 - 恢复后才能打开；"
      + "或在下方删除记录（文件永远不会被删除）",
    "tm.purgeMismatch": "请输入准确的任务 id 以确认",
    "tm.bulkDone": "已更新 {ok} 个任务",
    "tm.bulkPart": "{ok} 项已更新，{ref} 项被拒绝（原因：{why}）",
    /* M11 P1-10：文件接入 */
    "att.add": "添加文件", "att.pick": "选择服务器文件",
    "att.uploading": "正在上传 {n}...", "att.cancel": "取消",
    "att.cancelled": "已取消上传 - 未存储任何内容",
    "att.tooBig": "{n} 有 {s} 字节，超过 {cap} 字节上限；未上传",
    "att.badType": "{n} 类型不支持 - 仅限文档、报告、表格与图片；"
      + "测序文件请走正式录入流程",
    "att.chipTitle": "{kind} - {size} 字节 - 代理只收到路径，绝不包含文件字节",
    "att.remove": "移除", "att.pickedNote": "原位引用",
    "att.egressNote": "附件以路径传递 - 文件字节绝不会离开本机；"
      + "每次引用均有审计记录。",
    "att.up": ".. 上级", "att.roots": "已授权输入根目录",
    "att.seqNote": "测序输入 - 请走正式录入流程",
    "att.unsup": "类型不支持",
    "att.badSend": "仍有文件在上传 - 请取消或等待完成",
    "toast.attFail": "上传失败：{e}",
    "toast.pickFail": "无法引用该文件：{e}",
    "dr.close": "关闭", "dr.rename": "重命名", "dr.renameSave": "保存",
    "dr.renameCancel": "取消", "dr.renamed": "标题已更新",
    "dr.id": "任务 ID", "dr.created": "创建于", "dr.finished": "完成于",
    "dr.exec": "执行状态", "dr.concl": "科学结论",
    "dr.controls": "操作", "dr.pause": "暂停", "dr.resume": "继续",
    "dr.cancel": "取消",
    "dr.noPause": "此类运行没有安全的暂停点。",
    "dr.cancelNote": "取消将在此处停止任务；原生网关任务一旦提交便无法召回（已验证的限制）。",
    "dr.tech": "技术详情（审计）", "dr.decisions": "决策历史",
    "dr.artifacts": "输出产物", "dr.reqcfg": "请求的配置",
    "dr.effcfg": "实际生效的配置（本次运行真正应用的）",
    "dr.versions": "版本",
    "dr.snapshot": "运行快照", "dr.events": "实时事件",
    "dr.snapFull": "完整的运行快照",
    "dr.snapNote": "运行开始时写入一次，之后不再更新。",
    "dr.noDec": "未记录决策", "dr.defOnly": "仅使用默认",
    "dr.noArt": "尚未登记输出产物",
    "res.title": "结果", "res.pick": "运行", "res.none": "暂无运行记录。",
    "res.showMock": "显示模拟/演示历史",
    "res.inprogress": "该运行尚未结束（{state}）——结束后结果将显示在这里。",
    "res.cancelledNote": "该运行已被取消；取消之后不会再产生任何结果。",
    "res.previewH": "最终排序表（与运行输出文件逐字一致）",
    "res.previewNote": "以下内容逐字来自运行自身的输出文件——此处不做重新排序或改写。",
    "res.dl": "下载",
    "res.discoveryRow": "研究报告",
    "res.discoveryNote": "这是一项研究评估，不是流程运行——其报告归属研究工作区。",
    "res.openDiscovery": "在研究页打开报告",
    "res.shortlist": "短名单（最终排序表的前几行）",
    "res.prov": "输出清单、决策与溯源",
    "res.bundle": "下载结果包（.zip）",
    "res.download": "由持久化的运行状态生成；不会包含原始 FASTQ/BAM 输入，所有排除都附有原因；重复下载得到同一份固定字节。",
    "res.artH": "输出产物（{n}）",
    "res.noArtReg": "本次运行未登记任何产物——其结果均不可描述为已交付。",
    "res.ranking": "排序", "res.empty0": "0 行——候选表为空是合法结果；上方产物状态与运行结论均已如实说明。",
    "res.unavail": "结果不可用：{e}",
    "disc.title": "工具发现与基准评估",
    "disc.intro": "检索公开科学资源和你配置的 NeoAG 清单，以评估分析工具。只报告已核实的内容，其余一律标注为未核实。",
    "disc.scope": "范围", "disc.scopeT": "定向评估",
    "disc.scopeF": "全流程评估",
    "disc.run": "开始评估", "disc.targets": "目标",
    "disc.targetsT": "（可多选；来自所配置仓库的真实阶段清单）",
    "disc.targetsF": "——全流程评估覆盖清单中的全部阶段",
    "disc.custom": "自定义目标", "disc.customPh": "例如：新抗原克隆性评分",
    "disc.none": "暂无研究运行。",
    "disc.needTarget": "定向评估请至少选择一个目标（或使用自定义目标）",
    "disc.started": "研究 {id} 已开始",
    "disc.docsStage": "文档层面的阶段", "disc.uses": "使用：",
    "disc.reportFail": "报告加载失败：{e}",
    "disc.cancelBtn": "取消运行", "disc.cancelNote": "在当前查询后停止；进度会保留",
    "disc.resumeBtn": "继续（跳过已持久化的查询）",
    "disc.partNote": "该运行未完成；这里没有任何内容可视为完整评估。请继续运行或接受部分结果。",
    "disc.noRep": "暂无研究运行。",
    "skill.title": "智能体技能",
    "skill.intro": "技能是智能体在对话开始时阅读的建议性说明，只影响措辞与风格，不能改变批准、安全门禁或科学设置。修改将在新对话中生效。",
    "skill.new": "新建技能", "skill.none": "暂无技能。",
    "skill.edit": "编辑", "skill.enable": "启用", "skill.disable": "停用",
    "skill.enabled": "已启用", "skill.disabled": "已停用",
    "skill.loaded": "未加载", "skill.applies": "新对话生效",
    "skill.savedNot": "已保存——尚未生效",
    "skill.adv": "高级（版本历史、原始文本）",
    "sk.editTitle": "编辑技能", "sk.newTitle": "新建技能",
    "sk.name": "名称", "sk.namePh": "这个技能叫什么？",
    "sk.body": "指令",
    "sk.bodyPh": "希望智能体了解或改变哪些做法？请用平实语言描述——每次新对话开始时智能体都会阅读这段内容。",
    "sk.save": "保存", "sk.cancel": "取消",
    "sk.note": "修改将在新对话中生效。",
    "help.title": "帮助", "help.unavail": "不可用",
    "st.incomplete": "未完成",
    "res.noCand": "没有可展示的候选行。",
    "disc.dl": "下载 {f}", "disc.dlTsv": "下载 TSV 表格",
    "disc.dlNote": "按报告版本固定保存——重新下载不会重新执行检索",
    "disc.compat": "检查兼容性", "disc.plan": "采用计划",
    "disc.bench": "发起基准测试",
    "disc.actNote": "只读检查；发起的基准测试是一项正式任务，在你批准之前不会运行任何内容",
    "disc.benchDone": "已提出基准测试：任务 {id} 正等待批准 {a}——只有你在批准卡片上同意后才运行。",
    "disc.compatT": "兼容性检查（只读）：{t}",
    "disc.planT": "采用计划 - {t}（{tg}）",
    "disc.notApplied": "未应用",
    "disc.noDirect": "本次检索没有任何候选通过科学相关性筛选，成为该模块的直接替代方案。",
    "disc.noProg": "暂无已保存的进度",
    "disc.ignored": "——已忽略未知目标：{u}",
    "disc.engineL": "公开来源", "disc.engineE": "示例数据",
    "disc.h1": "1. 执行评估",
    "disc.h2": "2. 模块级建议",
    "disc.h3": "3. 科学比较",
    "disc.h4": "4. 建议行动（科学优先级）",
    "disc.h5": "5. 技术附录（排除项、候选全表、版本核验链、覆盖度、门禁、引用、溯源）",
    "disc.qReady": "可支持决策的评估", "disc.qPre": "初步研究（未定论）",
    "disc.whatIs": "本报告的定位（以及它不是什么）",
    "disc.questionL": "问题：", "disc.curMethod": "当前 NeoAG 方法：",
    "disc.exclL": "被排除的候选——每次未通过科学资格筛选的检索及其原因",
    "disc.fullTbl": "候选工具完整比较表",
    "disc.verChains": "版本核验链",
    "disc.coverageL": "覆盖度统计",
    "disc.gatesL": "研究质量门禁",
    "disc.citesL": "来源与引用",
    "disc.methL": "方法 / 局限 / 溯源",
    "sk.hist": "版本历史", "sk.raw": "原始 SKILL.md",
    "sk.restore": "恢复",
    "attn.resolvedNote": "已在任务面板中处理",
    "toast.actFail": "操作失败：{e}",
    "toast.researchFail": "研究失败：{e}",
    "toast.cancelFail": "取消失败：{e}",
    "toast.resumeFail": "继续失败：{e}",
    "toast.apprFail": "批准失败：{e}",
    "toast.decFail": "提交决策失败：{e}",
    "toast.togFail": "切换失败：{e}",
    "toast.verFail": "读取版本失败：{e}",
    "toast.rawFail": "无法读取原文：{e}",
    "toast.restoreFail": "恢复失败：{e}",
    "toast.restoreOk": "已将 v{v} 作为新版本恢复——新对话生效",
    "toast.skillLoadFail": "无法加载技能：{e}",
    "toast.saveApplyFail": "已保存，但应用失败：{st} {err}",
    "toast.boot": "启动失败：{e}",
    "toast.sendFail": "发送失败：{e}",
    "toast.newChatFail": "新建对话失败：{e}",
    "toast.stopFail": "停止失败：{e}",
    "toast.renamed": "标题已更新",
    "toast.renameFail": "重命名失败：{e}",
    "toast.openFail": "无法打开任务：{e}",
    "toast.confirmFail": "无法记录确认的输入：{e}",
    "toast.confirmed": "输入已确认——开始生成计划",
    "toast.pickFirst": "请先为以下项选择文件：{items}",
    "toast.dupActive": "该样本已有进行中的运行（{id}）——未重复提交。{d}",
    "mode.label": "执行方式",
    "mode.auto": "自主（默认）",
    "mode.ctrl": "受控",
    "mode.help": "自主：完成规划后，在你下方给出的授权范围内直接进入执行。受控：规划完成后，运行计划会先等待你明确选择「执行」或「中止」。",
    "opt.title": "分析项目",
    "opt.note": "按原生阶段、以「执行方向」列出：必需项目始终执行，不能关闭。可选分支表示本次是否运行——DNA 结构变异（SV）分支在本界面默认关闭，勾选后才运行；样本身份核验（安全检查）默认开启，除非刻意关闭。「不适用」来自规划器对本批数据的实际判定。",
    "opt.req": "始终执行",
    "opt.runs": "本次执行",
    "opt.off": "本次已跳过",
    "opt.offDefault": "本界面默认关闭",
    "opt.guardAck": "我确认肿瘤/正常样本身份已在别处核实；关闭该安全检查将随任务一并记录。",
    "opt.na": "本次不适用",
    "rdy.title": "就绪检查",
    "rdy.run": "立即检查完整流程",
    "rdy.checking": "正在与服务器核对流程的每个环节——可能需要一分钟。",
    "rdy.ready": "所有必需条件均已具备，可以开始分析。",
    "rdy.blockedNote": "暂时无法开始——以下列出全部阻断项：",
    "rdy.warnNote": "提示（不阻断）：",
    "rdy.checkedAt": "检查时间",
    "rdy.native_runtime": "原生 NeoAG 运行时",
    "rdy.gateway_service": "执行网关服务",
    "rdy.tools_env": "工具环境",
    "rdy.tools_manifest": "工具清单",
    "rdy.reference_manifest": "参考数据清单",
    "rdy.reference_fasta": "参考基因组文件",
    "rdy.genome_build": "基因组版本",
    "rdy.vep": "变异注释（VEP）",
    "rdy.scientific_config": "科学配置",
    "rdy.work_writable": "工作目录可写",
    "rdy.manifest": "已确认的输入",
    "rdy.inputs_present": "输入文件存在",
    "rdy.inputs_authorized": "输入位置在授权范围内",
    "rdy.demo_fixtures": "演示样例数据",
    "rdy.planner": "规划器判定",
    "st.waitdec": "等待你的决定",
    "fail.head": "该运行失败",
    "fail.startedYes": "计算已经开始。",
    "fail.startedNo": "没有任何计算启动——未执行科学计算。",
    "fail.blockersH": "阻断问题",
    "fail.completedH": "失败前已完成",
    "fail.completedNone": "无",
    "fail.partialN": "已在任务上登记 {n} 个部分输出文件，可供核查。",
    "fail.partial0": "没有产生任何部分输出。",
    "fail.next": "下一步",
    "fail.open": "打开任务",
    "gate.target": "操作对象",
    "gate.consequence": "批准后的效果",
    "gate.approveEffect": "若批准",
    "gate.denyEffect": "若拒绝",
    "attn.center": "等待你的处理",
    "attn.badgeTitle": "有 {n} 项需要你的处理",
    "dec.nativePlan": "原生规划器已通过本次运行——现在执行，还是仅保留计划并停止？",
    "dlg.close": "关闭", "dlg.cancel": "取消", "dlg.confirm": "确认",
  },
};

let LANG = "zh";
function t(key, vars) {
  let s = (I18N[LANG] && I18N[LANG][key]) ?? I18N.en[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
  return s;
}
function applyStaticI18n() {
  document.documentElement.lang = LANG === "zh" ? "zh" : "en";
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
}
function initLang() {
  let saved = null;
  try { saved = localStorage.getItem("nj_lang"); } catch { /* private mode */ }
  LANG = (saved === "en" || saved === "zh") ? saved : "zh";
}
async function setLang(lang, persist) {
  if (!(lang === "zh" || lang === "en")) return;
  LANG = lang;
  try { localStorage.setItem("nj_lang", lang); } catch { /* ignore */ }
  const sel = $("lang-select");
  if (sel && sel.value !== lang) sel.value = lang;
  if (persist !== false) {
    try { await api("POST", "/api/settings", { language: lang }); }
    catch { /* local preference still applies */ }
  }
  applyStaticI18n();
  renderAll();
}

/* ---------------- view state (M9 §10) ----------------
   One consistent store: scroll position per workspace tab, selected
   task, filters, folded sections and the in-progress preparation
   review survive tab switches and reloads. State is re-validated on
   restore (a vanished task or finished discovery is not resurrected
   into a stale screen). */
const VS = {
  data: {},
  load() {
    try { this.data = JSON.parse(sessionStorage.getItem("nj.view.v1") || "{}"); }
    catch { this.data = {}; }
  },
  save() {
    try { sessionStorage.setItem("nj.view.v1", JSON.stringify(this.data)); }
    catch { /* storage full/unavailable: state degrades gracefully */ }
  },
  get(k, dflt) { return this.data[k] === undefined ? dflt : this.data[k]; },
  set(k, v) { this.data[k] = v; this.save(); },
  drop(k) { delete this.data[k]; this.save(); },
};

const state = {
  boot: null, params: null, view: "tasks", kindFilter: "",
  resultsJob: null, resultsStrategy: null,
  resultsMock: false,
  manifest: null, agentMode: "mock",
  convs: [], activeCid: null, convMsgs: [],
  // M11 P0-1: the conversation shows the CONVERSATION (chat rows);
  // the per-call execution record is opt-in (technical view, persisted
  // choice) - the backend stores the two classes separately.
  techView: false,
  sseDown: true, // until the live stream says otherwise (§6 status)
  // M11 P1-9: task management. trashMode swaps the sidebar list to
  // the record-level trash; bulkSel holds ids selected for bulk
  // flag actions; trashCount is refreshed on the same beats as jobs.
  taskSearch: "", taskSort: "new", trashMode: false,
  trashJobs: [], trashCount: null, bulkSel: new Set(),
  // M11 P1-10: files waiting beside the composer + in-flight uploads
  pendAtts: [], uploads: new Map(), pickDir: null,
};
try { state.techView = localStorage.getItem("nj_tech") === "1"; }
catch { /* private mode */ }

/* ---------------- API ---------------- */
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status, data });
  return data;
}

/* api with a client-side ceiling - discovery can hold the request for
   minutes, and the UI must reach a terminal state regardless. */
function apiWithTimeout(ms, method, path, body) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(
      () => reject(Object.assign(new Error("timeout"), { timeout: true })),
      ms);
    api(method, path, body).then(
      (v) => { clearTimeout(to); resolve(v); },
      (e) => { clearTimeout(to); reject(e); });
  });
}

function toast(msg, err) {
  const tEl = $("toast");
  tEl.textContent = msg;
  tEl.classList.toggle("err", !!err);
  tEl.classList.remove("hidden");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => tEl.classList.add("hidden"), 4200);
}

/* Unified app dialogs (M10 §3) - these REPLACE window.alert/confirm/
   prompt everywhere in the product. One visual language, bilingual,
   keyboard-reachable; a result (or cancel) always resolves the promise
   so no flow can be left half-decided. */
let dlgKill = null;
function uiDialog({ title, bodyHtml, actions }) {
  return new Promise((resolve) => {
    if (dlgKill) dlgKill();          // exactly one dialog lives at a time
    const root = $("dialog-root");
    $("dlg-title").textContent = title || "";
    $("dlg-body").innerHTML = bodyHtml || "";
    const bar = $("dlg-actions");
    bar.innerHTML = "";
    const cleanup = () => {
      root.classList.add("hidden");
      root.removeEventListener("click", onBack);
      document.removeEventListener("keydown", onKey, true);
      dlgKill = null;
    };
    const finish = (v) => { cleanup(); resolve(v); };
    dlgKill = cleanup;
    (actions || []).forEach((a) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn sm " + (a.cls || "");
      b.textContent = a.label;
      b.addEventListener("click", () => finish(a.value));
      bar.appendChild(b);
    });
    const onKey = (ev) => {
      if (ev.key === "Escape") { ev.stopPropagation(); finish(null); }
    };
    const onBack = (ev) => { if (ev.target === root) finish(null); };
    root.addEventListener("click", onBack);
    document.addEventListener("keydown", onKey, true);
    root.classList.remove("hidden");
    const first = bar.querySelector(".primary") || bar.querySelector("button");
    if (first) first.focus();
  });
}
/* cancel on backdrop/Esc reads as "no" for confirmations */
function uiConfirm({ title, bodyHtml, okLabel, cancelLabel, danger }) {
  return uiDialog({
    title, bodyHtml,
    actions: [
      { label: okLabel || t("dlg.confirm"), value: true,
        cls: danger ? "danger" : "primary" },
      { label: cancelLabel || t("dlg.cancel"), value: false, cls: "ghost" },
    ],
  }).then((v) => v === true);
}
function uiPrompt({ title, bodyHtml, placeholder, value }) {
  return uiDialog({
    title,
    bodyHtml: (bodyHtml || "") +
      `<textarea id="dlg-input" rows="3" style="width:100%"
        placeholder="${esc(placeholder || "")}">${esc(value || "")}</textarea>`,
    actions: [
      { label: t("dlg.confirm"), value: "ok", cls: "primary" },
      { label: t("dlg.cancel"), value: null, cls: "ghost" },
    ],
  }).then((v) => {
    if (v !== "ok") return null;
    const s = ($("dlg-input") || {}).value || "";
    return s.trim() || null;
  });
}
/* the attention/decision cards resolve through the delegated handler;
   opening an action closes any stale dialog so it never shows an
   already-resolved card */
function closeDialog() { if (dlgKill) dlgKill(); }

function ago(ts) {
  if (!ts) return "";
  const d = (Date.now() - new Date(ts).getTime()) / 1000;
  if (d < 60) return t("ago.now");
  if (d < 3600) return `${Math.floor(d / 60)}${t("ago.m")}`;
  if (d < 86400) return `${Math.floor(d / 3600)}${t("ago.h")}`;
  if (d < 7 * 86400) return `${Math.floor(d / 86400)}${t("ago.d")}`;
  return new Date(ts).toLocaleDateString();
}
function fmtDT(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d) ? String(ts)
    : d.toLocaleString([], { year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit" });
}
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}

/* ---------------- markdown (help docs) ---------------- */
function mdToHtml(md) {
  const out = [];
  let inCode = false, inList = false;
  for (const raw of md.split("\n")) {
    const line = raw;
    if (line.startsWith("```")) {
      if (inCode) { out.push("</pre>"); inCode = false; }
      else { if (inList) { out.push("</ul>"); inList = false; }
        out.push("<pre>"); inCode = true; }
      continue;
    }
    if (inCode) { out.push(esc(line)); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) {
      if (inList) { out.push("</ul>"); inList = false; }
      const lvl = Math.min(3, h[1].length + 1);
      out.push(`<h${lvl}>${mdInline(h[2])}</h${lvl}>`); continue;
    }
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (li) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${mdInline(li[1])}</li>`); continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    if (/^\s*\|/.test(line)) { out.push(`<p>${mdInline(line.replace(/\|/g, " "))}</p>`); continue; }
    if (line.trim() === "") { continue; }
    const bq = line.match(/^>\s?(.*)/);
    if (bq) { out.push(`<blockquote>${mdInline(bq[1])}</blockquote>`); continue; }
    out.push(`<p>${mdInline(line)}</p>`);
  }
  if (inList) out.push("</ul>");
  if (inCode) out.push("</pre>");
  return out.join("\n");
}
function mdInline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)");
}

/* ---------------- boot ---------------- */
async function boot() {
  try {
    state.boot = await api("GET", "/api/bootstrap");
    state.agentMode = state.boot.agent_mode || "mock";
    state.convs = state.boot.conversations || [];
    state.activeCid = state.boot.active_conv || null;
    state.params = await api("GET", "/api/params");
    // a language chosen on another device wins over the built-in
    // default only when this browser never chose one itself
    const srvLang = (state.boot.settings || {}).language;
    let local = null;
    try { local = localStorage.getItem("nj_lang"); } catch { /* ignore */ }
    if (!local && (srvLang === "zh" || srvLang === "en")) LANG = srvLang;
    $("lang-select").value = LANG;
    applyStaticI18n();
    VS.load();
    // P0-8: the selected task survives a reload of the page
    state.resultsJob = VS.get("resultsJob", null);
    // P1-9: so does the chosen list order
    state.taskSort = VS.get("taskSort", "new");
    $("tm-sort").value = state.taskSort;
    renderSidebar();
    await openConv(state.activeCid, true);
    restorePrep();
    renderAll();
    restoreView();
    initSplitter();
    connectSSE();
  } catch (e) {
    if (e.status === 401) return; // browser shows the login prompt
    toast(t("toast.boot", { e: e.message }), true);
  }
}

function renderAll() {
  renderTasks();
  renderJobs();
  renderAttention();
  renderResults();
  renderDiscoveryControls();
  renderDiscovery();
  renderSkills();
  renderHelp();
  renderChat();
  renderPrepFlow();
}

/* M11 P0-5: coalesce chat-event refetches. While a real-agent turn is
   live, chat events arrive constantly (and SSE reconnect replays a
   burst); each one used to fire its own full conversation GET and
   sidebar re-render. One fetch per short window carries the same
   state - and the streaming watchdog remains the delivery guarantee
   when events never arrive at all. */
let chatRefreshT = null;
function chatRefreshSoon(convId) {
  if (convId && convId !== state.activeCid) { refreshSidebar(); return; }
  if (chatRefreshT) return;
  chatRefreshT = setTimeout(() => {
    chatRefreshT = null;
    refreshConv(); refreshSidebar();
  }, 150);
}

let es = null;
function connectSSE() {
  if (es) es.close();
  es = new EventSource("/api/stream");
  // Reconnect catch-up: events published while the stream was down are
  // gone, so (re)opening always refetches the visible state. SSE is
  // best-effort - the streaming watchdog below is the guarantee.
  es.onopen = () => {
    state.sseDown = false;
    $("conn-dot").className = "dot on"; $("conn-text").textContent = t("conn.live");
    refreshConv(); refreshSidebar(); refreshJobs(); renderAttention();
    renderResults();
  };
  es.onerror = () => {
    state.sseDown = true;
    $("conn-dot").className = "dot off";
    $("conn-text").textContent = t("conn.off");
    if (state.activeCid) renderChat(); // status line must say "Reconnecting"
  };
  es.onmessage = (m) => {
    let ev = {};
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "chat") chatRefreshSoon(ev.conv_id);
    else if (ev.type === "conversations") refreshSidebar();
    else if (ev.type === "job" || ev.type === "approval") {
      refreshJobs(); renderAttention(); refreshConv();
      if (state.resultsJob) renderResults();
    } else if (ev.type === "research" || ev.type === "research_changed") {
      refreshDiscovery();
      refreshJobs(); // research rows are unified task rows too (§1.2)
    } else if (ev.type === "skills_changed") refreshSkills();
    else if (ev.type === "manifest") { /* the prep flow keeps its own state */ }
    else if (ev.type === "agent_mode") {
      state.agentMode = (state.boot && state.boot.agent_mode) || state.agentMode;
      api("GET", "/api/bootstrap").then((b) => {
        state.boot.agent_mode = b.agent_mode;
      }).catch(() => { /* transient */ });
    }
    else if (ev.type === "records_changed") boot();
  };
}

/* ---------------- conversation sidebar ---------------- */
/* One ellipsis menu per conversation (Rename / Pin / Archive /
   Delete). Archived chats move to a collapsed group so the action
   stays reversible. */
function renderSidebar() {
  const host = $("conv-list");
  const convs = state.convs || [];
  const live = convs.filter((c) => !c.archived);
  const arch = convs.filter((c) => c.archived);
  const row = (c) => `
    <div class="conv-item ${c.id === state.activeCid ? "active" : ""}"
         data-conv="${esc(c.id)}">
      <div class="ct">${c.pinned ? '<span class="cpin" title="pinned">&#9733;</span>' : ""}${esc(c.title || t("chat.new"))}</div>
      <div class="cm">
        ${(c.job_state === "working" || c.job_state === "blocked")
          ? '<span class="live"></span>' : ""}
        <span>${ago(c.updated_at)}</span><span>-</span>
        <span>${c.n_messages || 0} ${esc(t("conv.msg"))}</span>
      </div>
      <div class="cops">
        <button class="icon-btn" data-menu="${esc(c.id)}">&#8943;</button>
      </div>
    </div>`;
  host.innerHTML =
    (live.map(row).join("") || `<div class="empty">${esc(t("menu.noChats"))}</div>`)
    + (arch.length
       ? `<details class="conv-arch"><summary>${esc(t("menu.archived"))} (${arch.length})</summary>`
         + arch.map(row).join("") + "</details>" : "");
  host.querySelectorAll("[data-conv]").forEach((el) =>
    el.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-menu]")) return;
      openConv(el.dataset.conv);
    }));
  host.querySelectorAll("[data-menu]").forEach((el) =>
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      showConvMenu(el.dataset.menu, el);
    }));
}

let convMenuCloser = null;
function closeConvMenu() {
  document.querySelectorAll(".conv-menu").forEach((x) => x.remove());
  if (convMenuCloser) {
    document.removeEventListener("click", convMenuCloser);
    convMenuCloser = null;
  }
}
function showConvMenu(cid, anchor) {
  closeConvMenu();
  const c = (state.convs || []).find((x) => x.id === cid) || {};
  const items = c.archived
    ? [["unarchive", t("menu.unarchive")], ["delete", t("menu.delete")]]
    : [["rename", t("menu.rename")],
       [c.pinned ? "unpin" : "pin", c.pinned ? t("menu.unpin") : t("menu.pin")],
       ["archive", t("menu.archive")], ["delete", t("menu.delete")]];
  const m = document.createElement("div");
  m.className = "conv-menu";
  m.innerHTML = items.map(([k, label]) =>
    `<button data-act="${k}"${k === "delete" ? ' class="danger"' : ""}>` +
    `${label}</button>`).join("");
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  m.style.top = (r.bottom + 4) + "px";
  m.style.left = Math.max(6, Math.min(r.left, window.innerWidth - 160)) + "px";
  m.addEventListener("click", async (ev) => {
    const act = (ev.target.closest("[data-act]") || {}).dataset
      && ev.target.closest("[data-act]").dataset.act;
    closeConvMenu();
    if (!act) return;
    try {
      if (act === "rename") { startRename(cid); return; }
      if (act === "pin" || act === "unpin") {
        await api("POST", `/api/conversations/${cid}/flag`,
                  { pinned: act === "pin" });
      } else if (act === "archive" || act === "unarchive") {
        await api("POST", `/api/conversations/${cid}/flag`,
                  { archived: act === "archive" });
      } else if (act === "delete") {
        if (!await uiConfirm({ title: t("menu.delete"),
                               bodyHtml: esc(t("chat.deleteQ")),
                               danger: true })) return;
        await api("POST", `/api/conversations/${cid}/delete`, {});
      }
      if (state.activeCid === cid && ["archive", "delete"].includes(act)) {
        state.activeCid = null;
        await refreshSidebar();
        await openNextConv();
      } else await refreshSidebar();
    } catch (e) { toast(e.message, true); }
  });
  convMenuCloser = (ev2) => {
    if (!ev2.target.closest(".conv-menu")
        && !ev2.target.closest("[data-menu]")) closeConvMenu();
  };
  setTimeout(() => document.addEventListener("click", convMenuCloser), 0);
}

async function refreshSidebar() {
  try { state.convs = (await api("GET", "/api/conversations")).conversations; }
  catch { return; }
  renderSidebar();
}

async function openNextConv() {
  await openConv(state.convs.length ? state.convs[0].id : null);
}

function convFetchPath(cid) {
  return `/api/conversations/${cid}` +
    (state.techView ? "?include=audit" : "");
}

/* M11 P0-4 recoverable drafts: an un-sent message belongs to the chat
   it was written in. Switching chats parks the draft (localStorage,
   keyed by conversation) and restores the target chat's own draft, so
   text is never lost and never silently follows the user into a
   different conversation. */
const DRAFT_KEY = "nj_drafts";
function draftsAll() {
  try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || "{}"); }
  catch { return {}; }
}
function draftSave(cid) {
  if (!cid) return;
  const all = draftsAll();
  const v = $("chat-input").value;
  if (v) all[cid] = v; else delete all[cid];
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(all)); }
  catch { /* quota / private mode: composer still holds it live */ }
}
function draftLoad(cid) {
  const all = draftsAll();
  $("chat-input").value = (cid && all[cid]) || "";
}

async function openConv(cid, quiet) {
  draftSave(state.activeCid);
  state.activeCid = cid;
  draftLoad(cid);
  renderSidebar();
  if (!cid) { state.convMsgs = []; renderChat(); return; }
  try {
    const d = await api("GET", convFetchPath(cid));
    state.convMsgs = d.messages;
    state.convObj = d.conversation;
  } catch (e) {
    if (!quiet) toast(e.message, true);
    state.convMsgs = []; state.convObj = null;
  }
  renderChat();
}

async function refreshConv() {
  if (!state.activeCid) return;
  try {
    const d = await api("GET", convFetchPath(state.activeCid));
    state.convMsgs = d.messages;
    state.convObj = d.conversation;
    renderChat();
  } catch { /* transient */ }
}

/* Streaming watchdog: while the visible chat shows a streaming message,
   refetch every 3 s whether SSE is alive or not - reverse proxies can
   buffer or kill SSE, and missed events are never replayed. The server
   always stamps a terminal turn_state (completed / failed / cancelled /
   timed out / connection lost / interrupted), so this polling stops by
   itself once the turn honestly ends. */
setInterval(() => {
  if (!state.activeCid) return;
  // M11 P0-1: the live turn lives on the conversation row (one honest
  // indicator), not on message-tail bookkeeping; stop polling once the
  // turn state is terminal.
  const st = (state.convObj || {}).turn_state;
  if (st === "streaming") refreshConv();
}, 3000);

$("new-chat").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/conversations", {});
    state.activeCid = r.conversation.id;
    await refreshSidebar();
    await openConv(r.conversation.id);
    $("chat-input").focus();
  } catch (e) { toast(t("toast.newChatFail", { e: e.message }), true); }
});

function startRename(cid) {
  const item = document.querySelector(`[data-conv="${CSS.escape(cid)}"] .ct`);
  const conv = state.convs.find((c) => c.id === cid) || {};
  if (!item) return;
  const old = conv.title || t("chat.new");
  item.innerHTML = `<input class="rename" value="${esc(old)}">`;
  const inp = item.querySelector("input");
  inp.focus(); inp.select();
  const commit = async () => {
    const tt = inp.value.trim();
    if (tt && tt !== old) {
      try { await api("POST", `/api/conversations/${cid}/rename`, { title: tt }); }
      catch (e) { toast(t("toast.renameFail", { e: e.message }), true); }
    }
    await refreshSidebar();
    if (cid === state.activeCid) renderChat();
  };
  inp.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") commit();
    if (ev.key === "Escape") { renderSidebar(); if (cid === state.activeCid) renderChat(); }
  });
  inp.addEventListener("blur", commit);
}

/* ---------------- chat pane ---------------- */
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d) ? "" : d.toLocaleTimeString([],
    { hour: "2-digit", minute: "2-digit" });
}
const dayKey = (ts) => new Date(ts).toDateString();
const roleLabel = (r) => t("role." + r) !== "role." + r ? t("role." + r) : r;

/* Tool output arrives as two persisted messages (call + update) with the
   same tool_call_id; they merge into ONE collapsed entry, and the stored
   text is RAW (escaped here once) - no more leaked HTML entities. */
function toolHtml(m) {
  const meta = m.meta || {};
  const status = meta.tool_status || "running";
  const title = meta.tool_title || "tool";
  const out = meta.tool_output ?? m.text;
  return `<details class="msg tool toolmsg">
    <summary><span class="tool-status ${esc(status)}">${
      status === "completed" ? esc(t("tool.done"))
      : status === "failed" ? esc(t("tool.failed")) : esc(t("tool.running"))}</span>
      <b>${esc(title)}</b>
      <span class="ts">${esc(fmtTime(m.ts))}</span></summary>
    <pre class="tool-out">${esc(out)}</pre></details>`;
}

function msgHtml(m) {
  const meta = m.meta || {};
  if (meta.kind === "tool") return toolHtml(m);
  if (meta.kind === "job_failure") return failMsgHtml(m);
  const badges = (meta.badges || []).map((b) =>
    `<span class="badge">${esc(b)}</span>`).join("");
  return `<div class="msg ${esc(m.role)}">
    <div class="meta"><span>${esc(roleLabel(m.role))}</span>${badges}<span class="ts">${
      esc(fmtTime(m.ts))}</span></div>
    <div class="body">${esc(m.text)}</div></div>`;
}

/* the tool chatter of ONE turn collapses into a single expandable
   activity summary once it is done - while tools are still running the
   summary stays open so the work is visible. Nothing is hidden from the
   audit trail: every call remains its own stored message, rendered
   inside the fold. */
function activityHtml(rows) {
  if (rows.length === 1) return msgHtml(rows[0]);
  const done = rows.filter((r) =>
    (r.meta || {}).tool_status === "completed").length;
  const failed = rows.filter((r) =>
    (r.meta || {}).tool_status === "failed").length;
  const live = rows.length - done - failed;
  const bits = [];
  if (done) bits.push(`${done} ${t("act.done")}`);
  if (failed) bits.push(`${failed} ${t("act.failed")}`);
  if (live) bits.push(`${live} ${t("act.running")}`);
  const head = rows.length === 1 ? t("act.tools1")
    : t("act.toolsN", { n: rows.length });
  return `<details class="msg activity"${live ? " open" : ""}>
    <summary>${esc(head)} (${esc(bits.join(", "))})
      <span class="ts">${esc(fmtTime(rows[rows.length - 1].ts))}</span>
    </summary>
    <div class="act-rows">${rows.map((r) => toolHtml(r)).join("")}
    </div></details>`;
}

/* final answers deserve more visual weight than tool chatter */
function mergeToolMsgs(msgs) {
  const merged = [], idxById = {};
  for (const m of msgs) {
    const meta = m.meta || {};
    if (meta.kind === "tool" && meta.tool_call_id) {
      if (idxById[meta.tool_call_id] !== undefined) {
        merged[idxById[meta.tool_call_id]] = m;  // update wins, stays in place
        continue;
      }
      idxById[meta.tool_call_id] = merged.length;
    }
    merged.push(m);
  }
  return merged;
}

/* Every confirmation card states the SAME quality fields (M10 §10):
   task / operation / target / overwrite-risk / consequence / what
   approving does / what denying does. Values are computed server-side
   from the real command and arrive inside approval.detail. */
function gateFieldsHtml(info, a) {
  const f = [];
  if (info.job_id) f.push([t("dr.id"), info.job_id]);
  if ((info.effect_classes || []).length)
    f.push([t("gate.effect"), info.effect_classes.map((e) =>
      t("gate.ec." + e) !== "gate.ec." + e ? t("gate.ec." + e) : e)
      .join(" + ")]);
  if ((info.target_paths || []).length)
    f.push([t("gate.target"), info.target_paths.join(", ")]);
  if (info.overwrite_risk) f.push([t("warn.title"), info.overwrite_risk]);
  if (info.consequence) f.push([t("gate.consequence"), info.consequence]);
  if (info.reversibility)
    f.push([t("gate.reversibility"), info.reversibility]);
  if (info.approve_effect) f.push([t("gate.approveEffect"), info.approve_effect]);
  if (info.deny_effect) f.push([t("gate.denyEffect"), info.deny_effect]);
  return f.length ? `<div class="at-fields">${f.map(
    ([k, v]) => `<b>${esc(k)}</b><span>${esc(v)}</span>`).join("")}</div>` : "";
}

/* M11 P0-2: one actionable approval at a time - callers render the
   OLDEST card actionable and queue the rest as a count line. The
   scope button only appears where a narrow scope can exist at all
   (plain write confirmations with recorded targets; the server
   still refuses to broaden it). */
function gateCardHtml(a, actionable = true) {
  let info = {};
  try { info = JSON.parse(a.detail || "{}"); } catch { /* raw */ }
  if (!actionable) {
    return `<div class="inline-card confirm queued">
      <div class="sub">${esc(t("gate.queued"))}</div>
      <div class="detail">${esc(info.note || "")}</div></div>`;
  }
  const scopeBtn = (info.target_paths || []).length && !info.structured
    ? `<button class="btn sm" data-appr="${esc(a.id)}"
       data-dec="approve_scope"
       title="${esc(t("gate.scopeTitle"))}">${esc(t("gate.scopeBtn"))}</button>`
    : "";
  return `<div class="inline-card confirm">
    <div class="q">${esc(t("gate.q"))}</div>
    <div class="sub">${esc(t("gate.sub"))}</div>
    ${gateFieldsHtml(info, a)}
    <div class="detail">${esc(info.note || a.detail || "")}</div>
    <div class="btnrow">
      <button class="btn primary sm" data-appr="${esc(a.id)}" data-dec="approve">${esc(t("gate.approve"))}</button>
      ${scopeBtn}
      <button class="btn danger sm" data-appr="${esc(a.id)}" data-dec="reject">${esc(t("gate.deny"))}</button>
    </div></div>`;
}

/* proactive failure notice (M10 §11): name, time, whether computation
   started, the confirmed primary reason, what completed, partial
   results, next step - all fields the backend already confirmed. */
function failureBodyHtml(f, completed, partial) {
  f = f || {};
  const rows = [];
  rows.push(esc(f.started_computation ? t("fail.startedYes") : t("fail.startedNo")));
  if (f.reason) rows.push(`<div>${esc(f.reason)}</div>`);
  if ((f.blockers || []).length)
    rows.push(`<div><b>${esc(t("fail.blockersH"))}</b>: ${f.blockers.map(esc).join("<br>")}</div>`);
  rows.push(`<div class="hint">${esc(t("fail.completedH"))}: ${
    (completed || []).length ? esc(completed.join(", ")) : esc(t("fail.completedNone"))}</div>`);
  rows.push(`<div class="hint">${partial
    ? esc(t("fail.partialN", { n: partial })) : esc(t("fail.partial0"))}</div>`);
  if (f.next_step)
    rows.push(`<div><b>${esc(t("fail.next"))}</b>: ${esc(f.next_step)}</div>`);
  return `<div class="failcard">${rows.join("")}</div>`;
}

function failMsgHtml(m) {
  const meta = m.meta || {};
  return `<div class="msg system failmsg">
    <div class="meta"><span>${esc(roleLabel(m.role))}</span>
      <span class="badge danger">${esc(t("fail.head"))}</span>
      <span class="ts">${esc(fmtTime(m.ts))}</span></div>
    <div class="body"><b>${esc(t("fail.head"))}: ${esc(
      taskDisplayNameById(meta.job_id || ""))}</b>
      ${failureBodyHtml(meta.failure, meta.completed_before_failure,
                        meta.partial_artifacts)}
      <div class="btnrow" style="margin-top:6px">
        <button class="btn sm" data-openjob="${esc(meta.job_id || "")}">${esc(t("fail.open"))}</button>
      </div></div></div>`;
}

function renderChat() {
  const log = $("chat-log");
  const conv = state.convObj;
  $("chat-title").textContent =
    (conv && (conv.title || t("chat.new"))) || t("chat.new");
  const tt = $("tech-toggle");
  if (tt) {
    tt.textContent = t(state.techView ? "chat.chat" : "chat.tech");
    tt.title = t("chat.techTip");
    tt.setAttribute("aria-pressed", state.techView ? "true" : "false");
    tt.classList.toggle("active", state.techView);
  }
  const st = conv && conv.job_state;
  const chip = $("chat-state");
  if (st === "working" || st === "blocked") {
    chip.classList.remove("hidden");
    chip.className = "pill accent";
    chip.textContent = st === "working" ? t("chat.agentWorking") : t("chat.awaiting");
  } else chip.classList.add("hidden");

  const merged = mergeToolMsgs(state.convMsgs || []);
  // consecutive tool messages (one turn's work) become one block
  const blocks = [];
  for (const m of merged) {
    const last = blocks[blocks.length - 1];
    if ((m.meta || {}).kind === "tool" && last && last.tools) {
      last.tools.push(m);
    } else if ((m.meta || {}).kind === "tool") {
      blocks.push({ tools: [m] });
    } else {
      blocks.push({ msg: m });
    }
  }
  let lastDay = "";
  let html = blocks.map((b) => {
    const first = b.tools ? b.tools[0] : b.msg;
    let sep = "";
    let dk = "";
    try { dk = dayKey(first.ts); } catch { /* unparseable ts: no separator */ }
    if (dk && dk !== lastDay) {
      sep = `<div class="date-sep">${esc(
        new Date(first.ts).toLocaleDateString([],
          { year: "numeric", month: "short", day: "numeric" }))}</div>`;
      lastDay = dk;
    }
    return sep + (b.tools ? activityHtml(b.tools) : msgHtml(b.msg));
  }).join("");
  const gateCards = (state.boot?.approvals || []).filter((a) => {
    if (a.state !== "pending" || a.action !== "gate_op") return false;
    try { return JSON.parse(a.detail || "{}").conv_id === state.activeCid; }
    catch { return false; }
  }).sort((x, y) => String(x.created_at).localeCompare(String(y.created_at)));
  // one actionable card at a time; the rest stay visible but queued
  gateCards.forEach((a, i) => { html += gateCardHtml(a, i === 0); });
  updateActivity(gateCards.length > 0);
  if (!(state.convMsgs || []).length && !gateCards.length) {
    html += `<div class="emptychat">
      <div class="et">${esc(t("chat.emptyTitle"))}</div>
      <div class="es">${esc(t("chat.emptySub"))}</div>
    </div>`;
  }
  // M11 P0-5: identical content must not touch the DOM. The watchdog
  // and duplicate SSE events redraw the same conversation constantly;
  // each blind innerHTML write rebuilt every node and yanked the
  // scroll position back to the bottom.
  const sig = (state.activeCid || "") + "\u0000" + html;
  if (sig !== renderChat._sig) {
    renderChat._sig = sig;
    log.innerHTML = html;
    log.scrollTop = log.scrollHeight;
  }
}

/* activity line right above the composer - what the agent is doing
   right now, for how long, and a Stop control for the live turn. */
/* M11 P0-1: one compact live indicator, fed by the conversation row.
   The label is the agent's CURRENT WORK (the real tool title the
   gateway reported), not a call count. */
function currentTurn() {
  const conv = state.convObj || {};
  if (conv.turn_state) {
    return { state: conv.turn_state, ts: conv.turn_ts || conv.updated_at,
             activity: conv.activity || "" };
  }
  return null;
}

function updateActivity(gatePending) {
  const bar = $("chat-status"), txt = $("act-text"),
        el = $("act-elapsed"), stop = $("chat-stop");
  if (!bar) return;
  const turn = currentTurn();
  const working = !!turn && turn.state === "streaming";
  state.turnWorking = working;
  state.turnSince = turn && working ? new Date(turn.ts).getTime() : null;
  // honest at a glance: waiting for approval is never shown as
  // "thinking", a lost connection is never shown as work in progress,
  // and "thinking" only shows while no work label is available.
  const blocked = state.convObj && state.convObj.job_state === "blocked";
  let label = "";
  if (working && (gatePending || blocked)) {
    label = t("act.waitappr");
  } else if (working && state.sseDown) {
    label = t("act.reconnect");
  } else if (working && turn.activity) {
    label = t("act.doing") + ": " + turn.activity;
  } else if (working) {
    label = t("act.think");
  } else if (gatePending || blocked) {
    label = t("act.waitappr");
  } else if (turn) {
    label = t("act.lastturn") + turn.state;
  }
  // M11 P0-3: a live job that says nothing for a while is shown as
  // SILENT-BUT-RUNNING, never as finished and never as an error. The
  // gateway heartbeat is the conversation's own updated_at.
  if (working) {
    const conv = state.convObj || {};
    const silent = conv.updated_at
      ? Date.now() - new Date(conv.updated_at).getTime() : 0;
    if (silent > 45000) {
      label += " · " + t("act.silent",
                         { m: Math.max(1, Math.floor(silent / 60000)) });
    }
  }
  // one exchange at a time (M10 §12): sending is disabled while the
  // agent is still answering; the draft stays in the box for the next turn
  const send = $("chat-send");
  if (send) {
    send.disabled = !!working;
    send.title = working ? t("chat.busySend") : "";
  }
  if (!label) {
    bar.classList.add("hidden"); stop.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  bar.classList.toggle("working", working);
  txt.textContent = label;
  stop.classList.toggle("hidden", !working);
  tickElapsed();
}

function tickElapsed() {
  const el = $("act-elapsed");
  if (!el || !state.turnSince) { if (el) el.textContent = ""; return; }
  const s = Math.max(0, Math.floor((Date.now() - state.turnSince) / 1000));
  el.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}
setInterval(() => { if (state.turnWorking) tickElapsed(); }, 1000);

$("chat-stop").addEventListener("click", async () => {
  $("chat-stop").disabled = true;
  try {
    const r = await api("POST", "/api/chat/stop",
                        { conv_id: state.activeCid || "" });
    toast(r.note || "stop requested");
    await refreshConv();
  } catch (e) { toast(t("toast.stopFail", { e: e.message }), true); }
  $("chat-stop").disabled = false;
});

$("chat-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("chat-input").value.trim();
  if (!text && !state.pendAtts.length) return;
  if (state.turnWorking) { toast(t("chat.busySend"), true); return; }
  // P1-10: never send "some of" the pending set - either the uploads
  // all finished or the send waits.
  if (state.uploads.size) { toast(t("att.badSend"), true); return; }
  const atts = state.pendAtts.slice();
  $("chat-input").value = "";
  try {
    const r = await api("POST", "/api/chat",
                        { text, conv_id: state.activeCid || "",
                          attachment_ids: atts.map((a) => a.id) });
    state.activeCid = r.conv_id;
    state.pendAtts = [];
    renderAttChips();
    draftSave(state.activeCid);   // sent: its parked draft is no more
    await refreshConv();
    await refreshSidebar();
    await refreshJobs();
  } catch (e) {
    // The server now REJECTS an oversized prompt (413) instead of
    // silently cutting its tail. Put the draft back so a long message
    // is never lost on a failed send, and surface the server's own
    // explanation rather than a bare "HTTP 413".
    if (!$("chat-input").value) $("chat-input").value = text;
    draftSave(state.activeCid);
    const detail = (e.data && (e.data.detail || e.data.error)) || e.message;
    toast(t("toast.sendFail", { e: detail }), true);
  }
});
$("chat-input").addEventListener("input",
                                 () => draftSave(state.activeCid));

/* ---------------- file intake (M11 P1-10) -----------------------------
 * Two doors, one model: an attachment is a workspace file whose PATH
 * the agent may read. Uploads are stored copies; server picks stay
 * in place. File bytes never enter the chat payload, and the removal
 * rules are honest (a pick's ORIGINAL is never deleted here). */
function fmtBytes(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}
function attExt(name) {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i).toLowerCase();
}
const ATT_EXT_OK = new Set([".txt", ".md", ".pdf", ".docx", ".html",
  ".htm", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xlsx", ".png",
  ".jpg", ".jpeg", ".webp"]);
const ATT_EXT_SEQ = new Set([".bam", ".cram", ".fastq", ".fq", ".sam",
  ".vcf", ".bcf"]);
function attPreflight(f, cap) {
  const ext = attExt(f.name);
  if (ATT_EXT_SEQ.has(ext)) {
    return t("att.badType", { n: f.name });
  }
  if (!ATT_EXT_OK.has(ext)) return t("att.badType", { n: f.name });
  if (f.size > cap) return t("att.tooBig",
    { n: f.name, s: f.size, cap });
  if (!f.size) return t("att.badType", { n: f.name });
  return null;
}
function renderAttChips() {
  const box = $("att-chips");
  box.hidden = !state.pendAtts.length;
  box.innerHTML = state.pendAtts.map((a) =>
    `<span class="att-chip" title="${esc(t("att.chipTitle",
      { kind: a.kind, size: a.size }))}">`
    + `${esc(a.name)} <i>${esc(fmtBytes(a.size))}</i>`
    + `${a.source === "server_pick"
        ? ` <em>${esc(t("att.pickedNote"))}</em>` : ""}`
    + ` <button class="att-x" type="button" data-ax="${esc(a.id)}"`
    + ` title="${esc(t("att.remove"))}">×</button></span>`).join("")
    + `<div class="hint">${esc(t("att.egressNote"))}</div>`;
  box.querySelectorAll("[data-ax]").forEach((b) =>
    b.addEventListener("click", async () => {
      const id = b.dataset.ax;
      try { await api("POST", `/api/attachments/${encodeURIComponent(id)}/delete`, {}); }
      catch { /* already gone: local state still clears */ }
      state.pendAtts = state.pendAtts.filter((a) => a.id !== id);
      renderAttChips();
    }));
}
function uploadAtt(f, cap) {
  const err = attPreflight(f, cap);
  if (err) { toast(err, true); return; }
  const row = $("att-upload");
  row.hidden = false;
  row.innerHTML = `<span class="hint">${esc(t("att.uploading",
    { n: f.name }))}</span>`;
  const fr = new FileReader();
  fr.onerror = () => { row.hidden = true;
    toast(t("toast.attFail", { e: f.name }), true); };
  fr.onload = () => {
    const b64 = String(fr.result).split(",")[1] || "";
    const xhr = new XMLHttpRequest();
    state.uploads.set(f.name, xhr);
    const done = () => {
      state.uploads.delete(f.name);
      if (!state.uploads.size) { row.hidden = true; row.innerHTML = ""; }
    };
    xhr.open("POST", "/api/attachments");
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      row.innerHTML = `<span class="hint">${esc(t("att.uploading",
        { n: f.name }))} ${Math.round((e.loaded / e.total) * 100)}%</span>`
        + ` <button class="btn sm ghost" id="att-up-cancel" type="button">${esc(t("att.cancel"))}</button>`;
      const c = $("att-up-cancel");
      if (c) c.addEventListener("click", () => {
        xhr.abort();
        toast(t("att.cancelled"));
      });
    };
    xhr.onload = () => {
      done();
      let j = {};
      try { j = JSON.parse(xhr.responseText || "{}"); } catch { /* */ }
      if (xhr.status === 201 && j.attachment) {
        state.pendAtts.push(j.attachment);
        renderAttChips();
      } else {
        toast(t("toast.attFail",
          { e: (j && (j.detail || j.error)) || `HTTP ${xhr.status}` }),
          true);
      }
    };
    xhr.onerror = () => { done();
      toast(t("toast.attFail", { e: f.name }), true); };
    xhr.onabort = done;
    xhr.send(JSON.stringify({ name: f.name, data_b64: b64,
                              conv_id: state.activeCid || "" }));
  };
  fr.readAsDataURL(f);
}
$("att-btn").addEventListener("click", () => $("att-file").click());
$("att-file").addEventListener("change", async () => {
  const files = [...$("att-file").files];
  $("att-file").value = "";
  if (!files.length) return;
  let cap = 26214400;
  try {
    const r = await api("GET", "/api/attachments");
    cap = r.max_bytes || cap;
  } catch { /* server default still applies client-side */ }
  files.forEach((f) => uploadAtt(f, cap));
});

/* the server-side picker: one level at a time under the authorized
   roots; sequencing files are visible but clearly refused here -
   they belong to the formal ingestion, and the label says so */
async function openPicker(dir) {
  const box = $("att-picker");
  let r;
  try {
    r = await api("GET", "/api/files"
      + (dir ? "?dir=" + encodeURIComponent(dir) : ""));
  } catch (e) {
    box.hidden = false;
    box.innerHTML = `<div class="alert warn">${esc(e.message)}</div>`;
    return;
  }
  state.pickDir = r.dir || "";
  const up = !r.roots && r.dir
    ? `<button class="btn sm ghost" id="att-pk-up" type="button">${esc(t("att.up"))}</button>`
    : "";
  box.hidden = false;
  box.innerHTML = `<div class="pk-head"><b>${esc(r.roots
      ? t("att.roots") : r.dir)}</b> ${up}
      <button class="btn sm ghost" id="att-pk-close" type="button">×</button>
    </div><div class="pk-list">${(r.entries || []).map((en) => {
      if (en.dir) {
        return `<div class="pk-row" data-pkdir="${esc(en.path)}">`
          + `<span class="pk-name">${esc(en.name)}/</span></div>`;
      }
      const tag = en.seq ? `<span class="hint">${esc(t("att.seqNote"))}</span>`
        : en.allowed ? ""
          : `<span class="hint">${esc(t("att.unsup"))}</span>`;
      return `<div class="pk-row ${en.allowed ? "use" : "no"}"`
        + (en.allowed ? ` data-pkfile="${esc(en.path)}"` : "")
        + `><span class="pk-name">${esc(en.name)}</span>`
        + ` <span class="hint">${esc(fmtBytes(en.size))}</span> ${tag}</div>`;
    }).join("") || `<div class="empty">-</div>`}</div>`;
  const u = $("att-pk-up");
  if (u) u.addEventListener("click", () => {
    const parent = state.pickDir.replace(/\/[^/]+\/?$/, "");
    openPicker(parent || "");
  });
  $("att-pk-close").addEventListener("click",
    () => { $("att-picker").hidden = true; });
  box.querySelectorAll("[data-pkdir]").forEach((el) =>
    el.addEventListener("click", () => openPicker(el.dataset.pkdir)));
  box.querySelectorAll("[data-pkfile]").forEach((el) =>
    el.addEventListener("click", async () => {
      try {
        const j = await api("POST", "/api/attachments/pick",
          { path: el.dataset.pkfile, conv_id: state.activeCid || "" });
        state.pendAtts.push(j.attachment);
        renderAttChips();
        $("att-picker").hidden = true;
      } catch (e) {
        toast(t("toast.pickFail", { e: e.message }), true);
      }
    }));
}
$("att-pick-btn").addEventListener("click", () => {
  if (!$("att-picker").hidden) { $("att-picker").hidden = true; return; }
  openPicker("");
});
$("chat-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    $("chat-form").requestSubmit();
  }
});

/* M11 P0-1: the per-call execution record is an OPT-IN technical view
   over the same durably-stored data (server-side visibility class),
   never a hide-the-DOM toggle. */
$("tech-toggle").addEventListener("click", async () => {
  state.techView = !state.techView;
  try { localStorage.setItem("nj_tech", state.techView ? "1" : "0"); }
  catch { /* private mode */ }
  // technical view also governs the raw planner dump (M11 P0-6)
  showPlanBlock($("plan-block") &&
                $("plan-block").dataset.hasplan === "1");
  await refreshConv();
});

/* M11 P0-6: the raw native planning dump is TECHNICAL detail - gated
   behind the same technical-view toggle as the execution record, never
   a section of the normal page. Options/readiness stay visible because
   they state what WILL run in plain terms. */
function showPlanBlock(hasPlan, open) {
  const el = $("plan-block");
  if (!el) return;
  el.dataset.hasplan = hasPlan ? "1" : "";
  el.hidden = !(hasPlan && state.techView);
  if (open && state.techView) el.open = true;
}

async function refreshJobs() {
  try {
    const b = await api("GET", "/api/bootstrap");
    state.boot.jobs = b.jobs; state.boot.approvals = b.approvals;
    state.boot.decisions = b.decisions;
    state.boot.scope_grants = b.scope_grants || [];
    renderJobs(); renderAttention();
    // finished runs must appear in Results WITHOUT a page reload
    renderResults();
    // P1-9: keep the open trash view (and its count) honest too
    if (state.trashMode) {
      const tr = await api("GET", "/api/jobs?trashed=1");
      state.trashJobs = tr.jobs || [];
      state.trashCount = state.trashJobs.length;
      renderJobs();
    }
  } catch (e) { /* transient */ }
}

/* ---------------- splitter ---------------- */
function initSplitter() {
  const shell = $("shell"), sp = $("splitter");
  const saved = localStorage.getItem("neoag.chatw");
  if (saved) shell.style.setProperty("--chatw", saved);
  let drag = null;
  sp.addEventListener("pointerdown", (ev) => {
    drag = { x: ev.clientX,
             w: parseInt(getComputedStyle(shell)
                  .getPropertyValue("--chatw")) || 420 };
    sp.classList.add("drag");
    sp.setPointerCapture(ev.pointerId);
  });
  sp.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const w = Math.min(760, Math.max(300, drag.w + ev.clientX - drag.x));
    shell.style.setProperty("--chatw", w + "px");
  });
  const end = () => {
    if (!drag) return;
    drag = null; sp.classList.remove("drag");
    localStorage.setItem("neoag.chatw",
      shell.style.getPropertyValue("--chatw") || "420px");
  };
  sp.addEventListener("pointerup", end);
  sp.addEventListener("pointercancel", end);
}

/* ---------------- workspace tabs + view state (M9 §10) ---------------- */
function switchView(view) {
  const wb = $("work-body");
  VS.set("scroll." + state.view, wb.scrollTop);
  state.view = view;
  VS.set("view", view);
  document.querySelectorAll(".wtab").forEach((x) =>
    x.classList.toggle("active", x.dataset.view === view));
  document.querySelectorAll(".wview").forEach((x) =>
    x.classList.toggle("active", x.id === "view-" + view));
  requestAnimationFrame(() => {
    wb.scrollTop = VS.get("scroll." + view, 0);
  });
}
document.querySelectorAll(".wtab").forEach((tab) =>
  tab.addEventListener("click", () => switchView(tab.dataset.view)));

/* folded sections remember open/closed across tabs and reloads */
document.querySelectorAll("details[id]").forEach((d) => {
  d.addEventListener("toggle", () => VS.set("fold." + d.id, d.open));
});
function restoreFolds() {
  document.querySelectorAll("details[id]").forEach((d) => {
    const v = VS.get("fold." + d.id, null);
    if (v === true && d.hasAttribute("hidden") === false) d.open = true;
    else if (v === false) d.open = false;
  });
}
let scrollT = null;
$("work-body").addEventListener("scroll", () => {
  clearTimeout(scrollT);
  scrollT = setTimeout(() => VS.set("scroll." + state.view,
    $("work-body").scrollTop), 200);
});
window.addEventListener("beforeunload", () => {
  VS.set("scroll." + state.view, $("work-body").scrollTop);
  VS.set("kindFilter", state.kindFilter);
});

function restoreView() {
  const v = VS.get("view", "tasks");
  state.view = v;
  document.querySelectorAll(".wtab").forEach((x) =>
    x.classList.toggle("active", x.dataset.view === v));
  document.querySelectorAll(".wview").forEach((x) =>
    x.classList.toggle("active", x.id === "view-" + v));
  const kf = VS.get("kindFilter", "");
  state.kindFilter = kf;
  document.querySelectorAll("#kind-filter .chip").forEach((x) =>
    x.classList.toggle("active", x.dataset.kind === kf));
  restoreFolds();
  requestAnimationFrame(() => {
    $("work-body").scrollTop = VS.get("scroll." + v, 0);
  });
}

/* ---------------- scientific configuration (M9 §6) ----------------
   Only the curated, verified-modifiable controls render. The native
   default IS the displayed selection; an untouched control forwards
   nothing (display is not an override), and only intentional changes
   become overrides. CLI flags, code paths and provenance strings stay
   out of this view - the API keeps serving them for audit. */
let planDebounce = null;

/* M11 P0-6 run-rows: a checkbox row states a scientific BRANCH or
   GUARD in RUN direction (checked = it runs). Each row carries
   web_run_default: what THIS interface submits when untouched. The SV
   branch defaults OFF here even though native defaults to running it
   - so the payload states skip_dna_sv explicitly (see
   nativeParamsPayload), while this function returns only the user's
   DEVIATIONS from the interface defaults (what the "N changed" badge
   and Restore-defaults act on). */
function scienceOverrides() {
  const cfg = {};
  if (!state.params) return cfg;
  (state.params.science || []).forEach((c) => {
    const el = document.querySelector(`[data-param="${CSS.escape(c.id)}"]`);
    if (!el) return;
    if (c.control === "checkbox") {
      const defaultSkip = !(c.web_run_default !== false);
      const skip = !el.checked;
      if (skip !== defaultSkip) cfg[c.id] = skip;
    } else if (c.control === "number") {
      const v = parseInt(el.value, 10);
      if (!isNaN(v) && v !== Number(c.native_default)) cfg[c.id] = v;
    } else if (c.control === "radio") {
      const sel = document.querySelector(
        `input[name="sci-${CSS.escape(c.id)}"]:checked`);
      if (sel && sel.value !== String(c.native_default)) cfg[c.id] = sel.value;
    }
  });
  state.requestedConfig = cfg;
  return cfg;
}

function renderScience() {
  const host = $("cfg-science");
  const sc = state.params || {};
  const secs = sc.science_sections || {};
  const ctrl = sc.science || [];
  if (!ctrl.length) {
    host.innerHTML = `<p class="hint">${esc(t("sci.unavail"))}</p>`;
    return;
  }
  const ov = state.requestedConfig || {};
  let html = `<div class="scientific">`;
  Object.keys(secs).forEach((sid) => {
    // optional skip-switches live in Analysis options, not here (M10 §7)
    const items = ctrl.filter((c) => c.section === sid &&
                                     c.control !== "checkbox");
    if (!items.length) return;
    html += `<div class="pgroup"><h3>${esc(LANG === "zh" ? secLabelZh(sid) : secs[sid])}</h3>`;
    html += items.map((c) => scCtrlHtml(c, ov)).join("") + `</div>`;
  });
  html += `</div>`;
  host.innerHTML = html;
  host.querySelectorAll("input").forEach((el) =>
    el.addEventListener("change", () => {
      updateSciState();
      renderScience();          // repaint "changed" highlight state
      if (prep.manifest) {      // planner re-checks the consequences
        clearTimeout(planDebounce);
        planDebounce = setTimeout(loadPlan, 600);
      }
    }));
  updateSciState();
}

function secLabelZh(sid) {
  return {
    rna_fusion_splice: "RNA 表达证据",
    tool_routing: "主工具缺失时的证据来源策略",
    structural_variants: "结构变异分支",
    ranking_reporting: "排序窗口",
    reference_consistency: "参考基因组一致性",
  }[sid] || sid;
}
function scLabelZh(id) {
  return {
    rna_quant_method: "转录本定量后端",
    automatic_tool_policy: "替代工具策略",
    skip_dna_sv: "DNA 结构变异（SV）分支",
    skip_bam_matcher: "肿瘤/正常样本身份核验（安全检查）",
    event_top_n: "事件排名数量上限",
    candidate_top_n: "候选肽排名数量上限",
    genome_build: "基因组版本",
  }[id] || id;
}
function scHintZh(id) {
  return {
    rna_quant_method: "决定用哪个定量工具产生表达证据。",
    automatic_tool_policy: "主工具不可用时允许改用哪些替代证据来源。",
    skip_dna_sv: "本界面默认关闭该分支（运行记录会明确标注 DNA_SV_UNASSESSED）；勾选后才会运行 SV 识别（需 SV VCF 或肿瘤/正常 DNA BAM 配对）。关闭它不影响 RNA 侧——融合与剪接证据只来自 RNA 分支，照常执行。",
    skip_bam_matcher: "这是安全检查（污染/样本混淆核验），不是分析偏好。默认保持开启；如需关闭必须另行书面确认身份已在别处核实，该确认将随任务记录。",
    event_top_n: "最终排名保留的事件级行数（1-500）。",
    candidate_top_n: "每条分析线保留的肽-HLA 候选数（1-2000）。",
    genome_build: "参考序列与注释资产必须与之一致。",
  }[id] || "";
}

function scCtrlHtml(c, ov) {
  const id = esc(c.id);
  const name = LANG === "zh" ? scLabelZh(c.id) : c.label;
  const hint = LANG === "zh" ? scHintZh(c.id) : "";
  const changed = ov[c.id] !== undefined;
  let ctl;
  if (c.control === "radio") {
    ctl = `<div class="sc-ctl">${(c.options || []).map((o) => {
      const on = String(ov[c.id] !== undefined ? ov[c.id] : c.native_default) === String(o);
      return `<label class="radio"><input type="radio" name="sci-${id}"
        data-param="${id}" value="${esc(o)}"${on ? " checked" : ""}>
        <span>${esc(o)}</span></label>`;
    }).join("")}</div>`;
  } else if (c.control === "number") {
    const v = ov[c.id] !== undefined ? ov[c.id] : c.native_default;
    ctl = `<div class="sc-ctl"><input type="number" data-param="${id}"
      min="${c.min}" max="${c.max}" step="${c.step || 1}" value="${esc(v)}"></div>`;
  } else {
    ctl = `<div class="sc-ctl"><label class="chk"><input type="checkbox"
      data-param="${id}"${ov[c.id] ? " checked" : ""}>
      <span>${esc(LANG === "zh" ? "本次运行跳过该项" : "skip for this run")}</span></label></div>`;
  }
  return `<div class="sctrl${changed ? " changed" : ""}">
    <div><div class="sc-name">${esc(name)}</div></div>
    ${ctl}
    ${hint ? `<div class="sc-hint">${esc(hint)}</div>` : ""}</div>`;
}

function updateSciState() {
  const el = $("sci-state");
  if (!el) return;
  const n = Object.keys(scienceOverrides()).length;
  el.textContent = n === 0 ? t("sci.defaults")
    : (n === 1 ? t("sci.n1") : t("sci.nn", { n }));
  el.setAttribute("data-i18n", n ? "" : "sci.defaults");
  $("sci-block").hidden = !state.params;
}

$("sci-reset").addEventListener("click", () => {
  state.requestedConfig = {};
  renderScience();
  if (prep.manifest) loadPlan();
});

/* ---------------- preparation flow (M9 §5/§7) ----------------
   Choose data directory → discover → review the real inventory →
   confirm the manifest → the real native Planner's stage plan →
   submit. Discovery and planning are the deployed NeoAG detector and
   planner; the Web layer only presents their answers. The review
   survives tab switches and reloads (VS "prep"). */
const prep = { disc: null, manifest: null, plan: null, planState: "idle",
               caseName: "", ready: null, readyState: "idle" };
let discReq = 0;

function prepPersist() {
  VS.set("prep", {
    dir: $("prep-dir").value.trim(),
    disc: prep.disc, manifest: prep.manifest, caseName: prep.caseName,
  });
}
function restorePrep() {
  const p = VS.get("prep", null);
  if (!p) return;
  if (p.dir) $("prep-dir").value = p.dir;
  prep.disc = p.disc || null;
  prep.manifest = p.manifest || null;
  prep.caseName = p.caseName || "";
  if (prep.manifest) {
    // revalidate against the server (a vanished manifest must not leave
    // a dead confirmation card on screen)
    api("GET", `/api/manifests/${prep.manifest.id}`).then((m) => {
      prep.manifest = m;
      renderPrepFlow();
      if (m.status === "confirmed" && !prep.plan) loadPlan();
    }).catch(() => {
      prep.manifest = null;
      prepPersist();
      renderPrepFlow();
    });
  }
}

function renderPrepFlow() {
  /* #prep-error is deliberately NOT touched here. A previous version
     cleared it on every rebuild, which silently deleted discovery
     failure cards seconds after they appeared (incident 2026-11-19:
     eight clicks on an unauthorized directory, every 400 answered by
     the backend, every error erased before it could be read). The
     error lives until the next discovery attempt or an explicit reset. */
  if (!prep.disc) {
    $("prep-flow").innerHTML = "";
    $("prep-actions").hidden = true;
    $("sci-block").hidden = true;
    showPlanBlock(false);
    $("options-block").hidden = true;
    $("ready-block").hidden = true;
    $("approval-note").hidden = true;
    $("prep-entry").hidden = false;
    return;
  }
  $("prep-entry").hidden = false;
  renderInvCard();
  if (prep.manifest) renderConfirmed();
}

function discoverBtnState(busy, elapsed) {
  const btn = $("prep-discover"), pg = $("prep-progress");
  btn.disabled = busy;
  btn.classList.toggle("loading", busy);
  btn.textContent = busy ? "…" : t("prep.discover");
  pg.classList.toggle("hidden", !busy);
  if (busy) {
    $("prep-progress-text").textContent =
      t("prep.discovering", { dir: $("prep-dir").value.trim() }) +
      " - " + t("prep.scanning") + " " + t("prep.elapsed", { s: elapsed });
  }
}

$("prep-discover").addEventListener("click", async () => {
  const data_dir = $("prep-dir").value.trim();
  if (!data_dir) { toast(t("prep.needDir"), true); return; }
  const req = ++discReq;
  $("prep-error").innerHTML = "";
  const t0 = Date.now();
  discoverBtnState(true, 0);
  const tick = setInterval(() => {
    if (req === discReq) discoverBtnState(true,
      Math.round((Date.now() - t0) / 1000));
  }, 1000);
  try {
    const d = await apiWithTimeout(240000, "POST",
                                   "/api/native/discover", { data_dir });
    if (req !== discReq) return;           // a newer scan superseded this
    prep.disc = d; prep.manifest = null; prep.plan = null;
    prep.planState = "idle"; prep.ready = null; prep.readyState = "idle";
    prep.caseName = ((d.summary || {}).tumor_sample_id
      || (d.summary || {}).case_guess || "").replace(/_T$/i, "").toUpperCase();
    prepPersist();
    renderPrepFlow();
  } catch (e) {
    if (req !== discReq) return;
    const msg = e.timeout
      ? t("prep.timeout")
      : String((e.data && (e.data.error || e.data.detail)) || e.message) +
        (e.status ? ` (HTTP ${e.status})` : "");
    prep.disc = null; prep.manifest = null;
    prepPersist();
    renderPrepFlow();          // rebuild the entry area FIRST...
    /* ...and only then place the failure card, so no rebuild path can
       ever erase it again (see renderPrepFlow note, 2026-11-19). */
    $("prep-error").innerHTML = alertHtml("blocking", t("prep.failTitle"),
      esc(msg), t("prep.failFix"));
  } finally {
    clearInterval(tick);
    if (req === discReq) discoverBtnState(false);
  }
});

function alertHtml(kind, title, body, fix) {
  const ico = { blocking: "&#10006;", warn: "&#9888;", info: "&#8505;",
                good: "&#10003;" }[kind] || "";
  return `<div class="alert ${kind}"><span class="ico">${ico}</span>
    <div class="ab"><b>${esc(title)}</b>
    ${body ? `<div>${body}</div>` : ""}
    ${fix ? `<div class="fix">${esc(fix)}</div>` : ""}</div></div>`;
}

function kindLabel(k) {
  const key = "kind." + k;
  return t(key) !== key ? t(key) : (k || "");
}

function renderInvCard() {
  const d = prep.disc, s = d.summary || {};
  const amb = d.ambiguous || [], missing = d.missing || [];
  const confirmed = !!prep.manifest;
  const picks = VS.get("picks", {});
  const unresolved = amb.filter((a) => (a.candidates || []).length && !picks[a.field]);
  /* M11 P0-7: the confirm gate and the red list come from the
     workflow-typed assessment (kinds: patient data / server reference
     / choice pending / unusable file / tool gap / branch not
     selected), never from the raw "missing" pile - a reference asset
     that only the OFF-by-default DNA SV branch would need must not
     block the workflow the user is actually running. The raw native
     answer stays verbatim in the technical view. */
  const wf = d.workflow || {};
  const blocks = wf.blockers || [];
  const hardBlocks = blocks.filter((b) => b.kind !== "ambiguous");
  const blocked = hardBlocks.length + unresolved.length;
  const blockLine = (b) =>
    `<b>${esc(kindLabel(b.kind))}</b> — ${esc(b.field ? b.field + ": " : "")}${esc(b.reason || "")}`;
  const alerts = [];
  if (hardBlocks.length) alerts.push(alertHtml("blocking", t("blk.title"),
    hardBlocks.map(blockLine).join("<br>"),
    LANG === "zh" ? "补齐/更换上述数据后重新扫描即可；「缺少服务器参考数据」类问题由服务器管理员解决。"
      : "Add or fix the listed data and scan again; items marked "
        + "'Missing server reference' are fixed by the server "
        + "administrator, not by re-uploading."));
  if (unresolved.length) alerts.push(alertHtml("warn", t("warn.title"),
    esc(t("inv.needPick")), ""));
  if ((wf.advisories || []).length) alerts.push(alertHtml("info",
    t("inv.advisories"),
    wf.advisories.map(blockLine).join("<br>"), ""));
  if ((d.warnings || []).length) alerts.push(alertHtml("warn", t("warn.title"),
    d.warnings.map(esc).join("<br>"), ""));
  if (d.native_hla_note) alerts.push(alertHtml("info", t("info.title"),
    esc(d.native_hla_note), ""));

  // M11 P0-7: every role states HOW it was established - a content
  // match (VCF header, table headers, allele pattern, BAM header
  // samples) or the native classifier's filename heuristic. A
  // name-based judgement is shown as such, never dressed as proven.
  const basisChip = (i) => {
    const key = i.basis === "content" ? "basis.content"
      : i.basis === "filename token" ? "basis.filename" : "";
    if (!key) return "";
    return `<span class="rr-basis${i.basis === "content" ? "" : " hint"}"` +
      ` title="${esc(i.basis_detail || "")}">${esc(t(key))}</span>`;
  };
  const roleRow = (i) => `<div class="role-row">
    <span class="rr-name">${esc(roleLabelFor(i.flag))}</span>
    <span class="rr-path">${esc(String(i.path).split("/").slice(-2).join("/"))}</span>
    ${basisChip(i)}
    <span class="rr-size">${fmtBytes(i.size_bytes)}</span></div>`;
  const pickHtml = amb.map((a) => (a.candidates || []).length ? `
    <div class="pickbox" data-pick="${esc(a.field)}">
      <div class="pq">${esc(t("pick.q", { label: roleLabelFor(a.field) || a.label || a.field }))}</div>
      ${(a.candidates || []).map((c) => `
        <label><input type="radio" name="amb-${esc(a.field)}" value="${esc(c)}"
          ${picks[a.field] === c ? "checked" : ""}>
          <span class="pp">${esc(String(c).split("/").slice(-2).join("/"))}</span></label>`).join("")}
    </div>` : "").join("");

  $("prep-flow").innerHTML = `<div class="inv-card card-in" id="inv-card">
    <div class="card-head"><h3 class="sec" style="margin:0">${esc(t("inv.title"))}</h3>
      ${confirmed ? "" : `<button class="btn ghost sm" id="prep-rescan">${esc(t("prep.rescan"))}</button>`}
    </div>
    <div class="inv-root">${esc(d.data_dir)}</div>
    <div class="inv-sum">
      <span><b>${s.n_files || 0}</b> ${esc(t("inv.files", { n: "" }).replace(/^\d*\s*/, ""))}</span>
      <span><b>${s.n_recognized || 0}</b> ${esc(t("inv.recognized", { n: "" }).replace(/^\d*\s*/, ""))}</span>
      <span><b>${(d.unmatched || []).length}+</b> ${esc(t("inv.excluded", { n: "" }).replace(/^\d*\s*/, ""))}</span>
      ${amb.length ? `<span class="pill warn">${esc(t("inv.ambiguous", { n: amb.length }))}</span>` : ""}
      ${blocked ? `<span class="pill danger">${esc(t("inv.blocked", { n: blocked }))}</span>` : `<span class="pill ok">${esc(t("inv.ok"))}</span>`}
    </div>
    ${alerts.join("")}
    ${pickHtml}
    <div class="inv-sec">
      <h3>${esc(t("inv.recognized", { n: s.n_recognized || 0 }))}</h3>
      <div class="role-grid">${(d.inputs || []).map(roleRow).join("") || `<div class="empty">${esc(t("inv.none"))}</div>`}</div>
    </div>
    ${state.techView && missing.length ? `<details class="adv"><summary>native raw missing (pre-workflow view)</summary>
      <pre class="tool-out">${esc(missing.map((m) => `${m.field}: ${m.reason || ""}`).join("\n"))}</pre></details>` : ""}
    <div class="inv-sec">
      <h3>${esc(t("inv.tree"))}</h3>
      <div class="tree" id="inv-tree"></div>
    </div>
    <label class="lbl">${esc(t("prep.caseName"))}
      <span class="count">${esc(t("prep.caseFrom"))}</span></label>
    <div class="conf-row"><span id="case-name-txt" class="hint">${esc(prep.caseName || "-")}</span>
      ${confirmed ? "" : `<button class="btn ghost sm" id="case-name-edit">${esc(t("prep.caseEdit"))}</button>`}</div>
    ${confirmed ? "" : `<div class="btnrow">
      <button id="prep-confirm" class="btn primary" ${blocked ? "disabled" : ""}>${esc(t("prep.confirm"))}</button>
      ${blocked ? `<span class="hint">${esc(t("plan.blocked"))}</span>` : ""}
    </div>`}
  </div>`;
  mountTree();
  const rc = $("prep-rescan");
  if (rc) rc.addEventListener("click", () => {
    prep.disc = null; prep.manifest = null; prep.plan = null; prep.planState = "idle";
    prep.ready = null; prep.readyState = "idle";
    $("prep-error").innerHTML = "";   // explicit fresh start clears the old card
    VS.drop("picks"); prepPersist(); renderPrepFlow();
    $("prep-dir").focus();
  });
  const cn = $("case-name-edit");
  if (cn) cn.addEventListener("click", () => {
    const host = $("case-name-txt");
    host.innerHTML = `<input id="case-name-in" value="${esc(prep.caseName)}" style="max-width:320px;display:inline-block">`;
    const inp = $("case-name-in");
    inp.focus();
    inp.addEventListener("change", () => {
      prep.caseName = inp.value.trim().toUpperCase() || prep.caseName;
      prepPersist(); renderInvCard();
    });
  });
  document.querySelectorAll("#prep-flow input[name^='amb-']").forEach((r) =>
    r.addEventListener("change", () => {
      const p = VS.get("picks", {});
      p[r.name.replace(/^amb-/, "")] = r.value;
      VS.set("picks", p);
    }));
  const cb = $("prep-confirm");
  if (cb) cb.addEventListener("click", confirmInputs);
}

/* human-readable scientific role names (zh/en) - the ROLES themselves
   come from the native detector, this only localizes their labels */
function roleLabelFor(flag) {
  const zh = {
    tumor_dna_bam: "肿瘤 DNA 比对", normal_dna_bam: "配对正常 DNA 比对",
    tumor_dna_fastq: "肿瘤 DNA reads", normal_dna_fastq: "配对正常 DNA reads",
    tumor_rna_bam: "RNA 比对", tumor_rna_fastq: "RNA reads",
    somatic_vcf: "体细胞变异调用", sv_vcf: "结构变异调用",
    fusion_tsv: "融合基因结果表", splice_junction_tsv: "剪接连接结果表",
    hla_file: "HLA 分型结果", peptide_csv: "既有肽-HLA 表",
    expression_tsv: "基因表达表", purity_tsv: "纯度/倍体表",
    cnv_tsv: "拷贝数节段", hla_loh_tsv: "HLA LOH 证据",
    capture_bed: "捕获试剂盒 BED", tumor_sample_id: "肿瘤样本名",
  };
  const amb = { tumor_sample_id: "哪个样本是肿瘤？" };
  if (LANG === "zh") return zh[flag] || flag;
  const hit = ((prep.disc || {}).ambiguous || []).find((a) => a.field === flag);
  return hit ? (hit.label || flag) : flag;
}

/* ---------------- input inventory folder tree ----------------
   Real listing via /api/native/tree, one level per expansion (lazy,
   so huge directories never render at once). Sizes come from the real
   filesystem; the scientific role tags come ONLY from what the native
   detector recognized - nothing is inferred from filenames here. */
const treeCache = {};
function inputRoleMap() {
  const map = {};
  const base = (prep.disc || {}).data_dir || "";
  for (const i of (prep.disc || {}).inputs || []) {
    const rel = String(i.path).startsWith(base)
      ? String(i.path).slice(base.length).replace(/^\/+/, "")
      : null;
    if (rel) map[rel] = roleLabelFor(i.flag);
  }
  return map;
}
function unmatchedSet() {
  const base = (prep.disc || {}).data_dir || "";
  const rels = new Set();
  for (const u of (prep.disc || {}).unmatched || []) rels.add(u);
  return rels;
}
async function mountTree() {
  const host = $("inv-tree");
  if (!host || !prep.disc) return;
  const roles = inputRoleMap(), un = unmatchedSet();
  treeCache[""] = null;
  host.innerHTML = `<div class="tnote">${esc(t("prep.scanning"))}</div>`;
  try {
    const r = await api("GET", "/api/native/tree?data_dir=" +
      encodeURIComponent(prep.disc.data_dir));
    if (!r.ok) throw new Error(r.error || "listing failed");
    renderTreeLevel(host, r, "", roles, un);
  } catch (e) {
    host.innerHTML = `<div class="tnote">${esc(e.message)}</div>`;
  }
}
function renderTreeLevel(host, r, prefix, roles, un) {
  host.innerHTML = "";
  if (r.truncated) {
    const n = document.createElement("div");
    n.className = "tnote";
    n.textContent = `… ${r.n_total - r.entries.length} more`;
    host.appendChild(n);
  }
  for (const ent of r.entries) {
    const row = document.createElement("div");
    row.className = "trow";
    const role = roles[ent.rel];
    const excluded = !ent.dir && un.has(ent.rel);
    const tw = document.createElement("span");
    tw.className = "tw";
    tw.textContent = ent.dir ? "+" : "";
    const nm = document.createElement("span");
    nm.className = "tname";
    nm.textContent = ent.name;
    row.appendChild(tw); row.appendChild(nm);
    if (!ent.dir && role) {
      const tag = document.createElement("span");
      tag.className = "trole";
      tag.textContent = role;
      row.appendChild(tag);
    } else if (!ent.dir && excluded) {
      const tag = document.createElement("span");
      tag.className = "trole excl";
      tag.textContent = t("inv.notInput");
      row.appendChild(tag);
    }
    if (!ent.dir) {
      const sz = document.createElement("span");
      sz.className = "tsize";
      sz.textContent = fmtBytes(ent.size);
      row.appendChild(sz);
    }
    host.appendChild(row);
    if (ent.dir) {
      const kid = document.createElement("div");
      kid.style.paddingLeft = "18px";
      kid.style.display = "none";
      host.appendChild(kid);
      tw.addEventListener("click", async () => {
        if (kid.style.display === "none") {
          kid.style.display = ""; tw.textContent = "−";
          if (!kid.dataset.loaded) {
            kid.dataset.loaded = "1";
            kid.innerHTML = `<div class="tnote">…</div>`;
            try {
              const sub = await api("GET", "/api/native/tree?data_dir=" +
                encodeURIComponent(prep.disc.data_dir) +
                "&sub=" + encodeURIComponent(ent.rel));
              if (sub.ok) renderTreeLevel(kid, sub, ent.rel, roles, un);
              else kid.innerHTML = `<div class="tnote">${esc(sub.error)}</div>`;
            } catch (e) {
              kid.innerHTML = `<div class="tnote">${esc(e.message)}</div>`;
            }
          }
        } else { kid.style.display = "none"; tw.textContent = "+"; }
      });
    }
  }
}

async function confirmInputs() {
  const picks = VS.get("picks", {});
  const unresolved = (prep.disc.ambiguous || [])
    // only entries that actually offer candidate files must be picked;
    // the rest are notes resolved by fixing the directory + re-scan
    .filter((a) => (a.candidates || []).length && !picks[a.field])
    .map((a) => roleLabelFor(a.field) || a.label || a.field);
  if (unresolved.length) {
    toast(t("toast.pickFirst", { items: unresolved.join(", ") }), true);
    document.querySelectorAll(".pickbox").forEach((p) => {
      if (!unresolved.length) return;
      const box = p; const f = box.dataset.pick;
      const lbl = (prep.disc.ambiguous.find((a) => a.field === f) || {});
      if (unresolved.includes(roleLabelFor(f) || lbl.label || f)) {
        if (!box.querySelector(".perr")) {
          const e = document.createElement("div");
          e.className = "perr"; e.textContent = t("pick.need");
          box.appendChild(e);
        }
      }
    });
    return;
  }
  try {
    const m = await api("POST", "/api/native/manifest", {
      data_dir: prep.disc.data_dir,
      sample_id: prep.caseName,
      picked: picks });
    prep.manifest = m;
    VS.drop("picks");
    prepPersist();
    renderConfirmed();
    toast(t("toast.confirmed"));
    loadPlan();
  } catch (e) {
    toast(t("toast.confirmFail", { e: String((e.data && e.data.error) || e.message) }), true);
  }
}

function renderConfirmed() {
  const m = prep.manifest;
  if (!m) return;
  const p = m.proposal || {};
  let card = $("confirmed-card");
  if (card) card.remove();
  card = document.createElement("div");
  card.id = "confirmed-card";
  card.innerHTML = alertHtml("good",
    `${t("prep.confirmedBtn")} - ${esc(p.sample_id || "")}`,
    (p.inputs || []).length + " " + esc(t("inv.recognized", { n: "" }).trim()));
  $("prep-flow").appendChild(card);
  $("sci-block").hidden = false;
  showPlanBlock(true);
  $("options-block").hidden = false;
  $("ready-block").hidden = false;
  $("prep-actions").hidden = false;
  $("approval-note").hidden = false;
  $("exec-mode").value = VS.get("execMode", "autonomous");
  updateSciState();
  renderOptions();
  renderReadiness();
  renderSubmitState();
}

/* ---------------- stage plan: the REAL native Planner (M9 §7) -------- */
async function loadPlan() {
  const m = prep.manifest;
  if (!m || m.status !== "confirmed") return;
  prep.planState = "loading";
  const body = $("plan-body");
  showPlanBlock(true, true);
  body.innerHTML = `<div class="prep-progress"><span class="spin"></span>
    <span>${esc(t("plan.loading"))}</span></div>`;
  try {
    const plan = await apiWithTimeout(300000, "POST",
      "/api/jobs/native/preview", { manifest_id: m.id });
    prep.plan = plan; prep.planState = "done";
    renderPlan();
    renderOptions();
    // one coherent readiness state (M10 §9): every fresh planner verdict
    // refreshes the full-workflow check together with it
    loadReadiness();
  } catch (e) {
    prep.plan = null; prep.planState = "failed";
    body.innerHTML = alertHtml("blocking",
      t("plan.fail", { e: String((e.data && e.data.error) || e.message) }),
      "", t("plan.failFix")) +
      `<div class="btnrow"><button class="btn sm" id="plan-retry">${esc(t("plan.retry"))}</button></div>`;
    $("plan-retry").addEventListener("click", loadPlan);
    renderSubmitState();
  }
}

/* ---------------- analysis options (M10 §7) ----------------
   One list from REAL sources only: the planner's own step verdicts
   (required / not-applicable) plus the verified optional switches from
   the parameter inventory (default on = native behavior; turning one
   off records a skip override exactly as the CLI would). */
function renderOptions() {
  const block = $("options-block");
  if (!block) return;
  if (!prep.manifest || prep.manifest.status !== "confirmed") {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  state.guardAck = state.guardAck || {};
  $("options-note").textContent = t("opt.note");
  const ov = state.requestedConfig || {};
  const rows = [];
  (((state.params || {}).science) || [])
    .filter((c) => c.control === "checkbox")
    .forEach((c) => {
      const id = esc(c.id);
      // RUN-direction row: default from the interface's stated product
      // default (SV branch off, identity guard on), deviation applied
      const on = ov[c.id] !== undefined
        ? !ov[c.id] : (c.web_run_default !== false);
      const name = LANG === "zh" ? scLabelZh(c.id) : c.label;
      const hint = LANG === "zh" ? scHintZh(c.id) : "";
      const tag = on ? t("opt.runs")
        : (ov[c.id] === undefined ? t("opt.offDefault") : t("opt.off"));
      let ack = "";
      if (c.guarded && !on) {
        // skipping a safety guard takes a SEPARATE written
        // confirmation - a deliberate act, recorded with the job
        ack = `<label class="chk ack-row"><input type="checkbox"
          data-ack="${id}"${state.guardAck[c.id] ? " checked" : ""}>
          <span>${esc(t("opt.guardAck"))}</span></label>`;
      }
      rows.push(`<div class="opt-row${on ? "" : " off"}${c.guarded ? " guarded" : ""}">
        <label class="chk"><input type="checkbox" data-param="${id}"
          ${on ? "checked" : ""}><span>${esc(name)}</span></label>
        <span class="opt-tag">${esc(tag)}</span>${ack}
        ${hint ? `<div class="sc-hint">${esc(hint)}</div>` : ""}</div>`);
    });
  const plan = prep.plan;
  ((plan && plan.steps) || []).forEach((st) => {
    const label = [st.step_id, st.name].filter(Boolean).join(" ") +
      (LANG === "zh" && planStepName(st)
        ? ` ${planStepName(st)}` : "");
    if (st.status === "SKIPPED")
      rows.push(`<div class="opt-row na"><span>${esc(label)}</span>` +
        `<span class="opt-tag">${esc(t("opt.na"))}</span></div>`);
    else if (["PASS", "DRY_RUN", "PLANNED_IN_PRODUCTION_DAG"]
      .includes(st.status))
      rows.push(`<div class="opt-row req"><span>${esc(label)}</span>` +
        `<span class="opt-tag">${esc(t("opt.req"))}</span></div>`);
  });
  $("options-body").innerHTML = rows.join("") ||
    `<div class="hint">${esc(t("plan.loading"))}</div>`;
  document.querySelectorAll(
    "#options-body input[data-param], #options-body input[data-ack]")
    .forEach((el) => el.addEventListener("change", () => {
      if (el.dataset.ack) {
        state.guardAck[el.dataset.ack] = el.checked;
      } else if (el.checked && el.dataset.param) {
        // guard switched back ON: the written confirmation is moot
        delete state.guardAck[el.dataset.param];
      }
      updateSciState();
      renderOptions();
      renderScience();     // keep the fold's "changed" count in sync
      if (prep.manifest) {
        clearTimeout(planDebounce);
        planDebounce = setTimeout(loadPlan, 600);
      }
    }));
}

/* ---------------- readiness check (M10 §9) ----------------
   ONE comprehensive pre-submission check of the ENTIRE workflow,
   computed by the server (real planner included). The interface only
   renders what the server measured - readiness is never assembled,
   cached past a configuration change, or estimated locally. */
let readyReq = 0;
async function loadReadiness() {
  const m = prep.manifest;
  if (!m || m.status !== "confirmed") return;
  const req = ++readyReq;
  prep.readyState = "loading";
  renderReadiness();
  try {
    const r = await apiWithTimeout(300000, "POST",
      "/api/jobs/native/readiness",
      { manifest_id: m.id, native_params: nativeParamsPayload() });
    if (req !== readyReq) return;
    prep.ready = r; prep.readyState = "done";
  } catch (e) {
    if (req !== readyReq) return;
    // a failed check is a failed check: it BLOCKS rather than passing
    // silently, and the server's own reason is shown
    prep.ready = { status: "BLOCKED", checks: [], warnings: [],
                   blockers: [String((e.data && e.data.error) || e.message)],
                   plan: null, checked_at: "" };
    prep.readyState = "done";
  }
  renderReadiness();
  renderSubmitState();
}
$("ready-run").addEventListener("click", loadReadiness);

function renderReadiness() {
  const block = $("ready-block");
  if (!block) return;
  if (!prep.manifest || prep.manifest.status !== "confirmed") {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  $("ready-run").disabled = prep.readyState === "loading";
  const body = $("ready-body");
  if (prep.readyState === "idle") {
    body.innerHTML = "";
    return;
  }
  if (prep.readyState === "loading") {
    body.innerHTML = `<div class="prep-progress"><span class="spin"></span>
      <span>${esc(t("rdy.checking"))}</span></div>`;
    return;
  }
  const r = prep.ready || {};
  const rows = (r.checks || []).map((c) => {
    const mark = c.ok === true ? "ok" : c.ok === false ? "bad" : "note";
    const lbl = t("rdy." + c.id) !== "rdy." + c.id ? t("rdy." + c.id) : c.id;
    return `<div class="rdy-row ${mark}"><span class="rdy-mark">${
      c.ok === true ? "&#10003;" : c.ok === false ? "&#10006;" : "&#8505;"
    }</span><span class="rdy-name">${esc(lbl)}</span>` +
    `${c.kind ? `<span class="rk" title="${esc(c.kind)}">${
      esc(kindLabel(c.kind))}</span>` : ""}` +
    `<span class="rdy-detail">${esc(String(c.detail || ""))}</span></div>`;
  }).join("");
  const head = r.status === "READY"
    ? alertHtml("good", t("rdy.ready"), "")
    : alertHtml("blocking", t("rdy.blockedNote"),
        (r.blockers || []).map(esc).join("<br>"));
  const warn = (r.warnings || []).length
    ? alertHtml("info", t("rdy.warnNote"),
        (r.warnings || []).map(esc).join("<br>")) : "";
  body.innerHTML = head + warn +
    (rows ? `<div class="rdy-list">${rows}</div>` : "") +
    (r.checked_at ? `<div class="hint">${esc(t("rdy.checkedAt"))}: ${
      esc(r.checked_at)}</div>` : "");
}

function renderPlan() {
  const plan = prep.plan, body = $("plan-body");
  if (!plan) return;
  if (plan.status === "NATIVE_UNAVAILABLE" || plan.ok === false && !(plan.steps || []).length) {
    body.innerHTML = alertHtml("blocking", t("plan.unavail"),
      esc(plan.detail || ""));
    renderSubmitState();
    return;
  }
  const steps = (plan.steps || []).map((st) => {
    const cls = st.status === "PASS" ? "pass" : st.status === "DRY_RUN" ? "dry"
      : st.status === "SKIPPED" ? "skip"
      : (st.status === "BLOCKED" || st.status === "FAILED") ? "blocked" : "dry";
    return `<div class="pstep ${cls}">
      <span class="ps-name">${esc([st.step_id, st.name].filter(Boolean).join(" "))}${
        LANG === "zh" && planStepName(st)
          ? ` <i class="ps-zh">${esc(planStepName(st))}</i>` : ""}</span>
      <span class="ps-detail">${esc(String(st.detail || "").slice(0, 160))}</span>
      <span class="ps-tag">${esc(t("ps." + st.status) !== "ps." + st.status ? t("ps." + st.status) : st.status)}</span>
    </div>`;
  }).join("");
  const blocking = (plan.blocking_issues || []).length
    ? alertHtml("blocking", t("plan.blocked"),
        (plan.blocking_issues || []).map(esc).join("<br>")) : "";
  const missingEv = (plan.missing_evidence || []).length
    ? alertHtml("warn", t("warn.title"),
        (plan.missing_evidence || []).map(esc).join("<br>")) : "";
  body.innerHTML = `
    <div class="hint">${esc(t("plan.status"))} <b>${esc(plan.status || plan.status_code || "?")}</b></div>
    ${blocking}${missingEv}
    <div class="plan-steps">${steps || `<div class="empty">${esc(plan.detail || "")}</div>`}</div>
    ${plan.run_plan_md ? `<details class="adv"><summary>${esc(t("plan.runplan"))}</summary>
      <pre class="tool-out">${esc(plan.run_plan_md)}</pre></details>` : ""}`;
  renderSubmitState();
}

/* The planner's step names are its own identifiers and stay verbatim;
   the line below only adds a plain-language gloss when Chinese is
   active. Keys are the real skill_result step names (verified 2026-11-
   18 against the deployed planner; unknown names render unexplained,
   never mislabelled). */
function planStepName(st) {
  const zh = {
    "input-detection-and-routing": "识别输入并确定分析路线",
    "capability-aware-production-plan": "按本机可用工具生成生产计划",
    "doctor-preflight": "环境体检（预检）",
    "rna-expression-and-allele-evidence": "RNA 表达与等位基因证据",
    "pipeline-plan": "生成执行计划",
    "pipeline-full-dry-run": "全流程空跑校验",
    "variant-calling": "变异识别",
    "hla-typing": "HLA 分型",
    "expression-quantification": "表达定量",
    "candidate-ranking": "候选排序",
    "report-generation": "报告生成",
  };
  if (LANG === "zh") return zh[st.name] || "";
  return "";
}

function nativeParamsPayload() {
  const sci = scienceOverrides();
  const fwd = {};
  ["event_top_n", "candidate_top_n", "genome_build",
   "rna_quant_method", "automatic_tool_policy"].forEach((k) => {
    if (sci[k] !== undefined && sci[k] !== "") fwd[k] = sci[k];
  });
  // run-rows are ALWAYS explicit (never absent): this interface's
  // default differs from native's on the SV branch, so silence would
  // mean the opposite of what the UI shows. A guarded skip (identity
  // check off) is only submitted together with its separate written
  // confirmation - the server rejects the pair incomplete.
  ((state.params || {}).science || []).forEach((c) => {
    if (c.control !== "checkbox") return;
    const el = document.querySelector(`[data-param="${CSS.escape(c.id)}"]`);
    if (!el) return;
    if (c.guarded && !el.checked) {
      const ack = document.querySelector(
        `[data-ack="${CSS.escape(c.id)}"]`);
      if (!(ack && ack.checked)) return;   // unconfirmed: guard stays on
      fwd[c.id] = true;
      fwd.identity_verified_elsewhere = true;  // web-layer ack, audited
      return;
    }
    fwd[c.id] = !el.checked;
  });
  return fwd;
}

function renderSubmitState() {
  const btn = $("nj-submit"), hint = $("prep-hint");
  const plan = prep.plan;
  let can = !!prep.manifest && prep.manifest.status === "confirmed";
  let why = "";
  if (!can) { why = t("prep.needConfirm"); can = false; }
  else if (prep.planState === "loading") { why = t("plan.loading"); can = false; }
  else if (plan && (plan.status === "NATIVE_UNAVAILABLE")) {
    why = t("plan.unavail"); can = false;
  } else if (plan && (plan.blocking_issues || []).length) {
    why = t("plan.blocked"); can = false;
  } else if (prep.readyState === "loading") {
    why = t("rdy.checking"); can = false;
  } else if (prep.readyState === "done" &&
             (prep.ready || {}).status !== "READY") {
    // one coherent state: the same readiness the panel shows also
    // gates the button (the server re-validates this at submit)
    why = t("rdy.blockedNote"); can = false;
  }
  btn.disabled = !can;
  hint.textContent = why;
}

$("exec-mode").addEventListener("change", () => {
  VS.set("execMode", $("exec-mode").value);
});

$("nj-submit").addEventListener("click", async () => {
  const m = prep.manifest;
  if (!(m && m.status === "confirmed")) {
    toast(t("prep.needConfirm"), true); return;
  }
  const exMode = $("exec-mode").value === "controlled"
    ? "controlled" : "autonomous";
  const body = { engine: "native", manifest_id: m.id,
                 execution_mode: exMode,
                 // M10 §10: this click IS the explicit, scoped start
                 // authorization for exactly this run (the backend
                 // records it as such and keeps the approval trail)
                 authorized: "start_analysis" };
  const fwd = nativeParamsPayload();
  if (Object.keys(fwd).length) body.native_params = fwd;
  $("nj-submit").disabled = true;
  try {
    const res = await api("POST", "/api/jobs", body);
    toast(t(exMode === "controlled"
      ? "prep.submittedCtrl" : "prep.submitted", { id: res.job_id }));
    prep.disc = null; prep.manifest = null; prep.plan = null;
    prep.planState = "idle"; state.requestedConfig = {};
    prep.ready = null; prep.readyState = "idle";
    $("prep-error").innerHTML = "";   // submitted: the old failure card is moot
    VS.drop("prep");
    renderPrepFlow(); renderScience();
    switchView("tasks");
    await refreshJobs();
  } catch (e) {
    $("nj-submit").disabled = false;
    const d = (e.data && e.data) || {};
    if (d.error === "readiness_blocked") {
      // the server's re-validation outranks the local view: show its
      // real blockers and re-render the same readiness state
      prep.ready = { status: "BLOCKED", checks: (d.checks || []),
                     blockers: d.blockers || [], warnings: d.warnings || [],
                     plan: null, checked_at: "" };
      prep.readyState = "done";
      renderReadiness(); renderSubmitState();
      toast(String(d.detail || e.message), true);
    } else if (d.error === "duplicate_active") {
      toast(t("toast.dupActive", { id: d.job_id || "",
                                   d: d.detail || "" }), true);
    } else {
      toast(String(d.error && d.error.length < 60 ? d.error
                  : (d.detail || d.error || e.message)), true);
    }
  }
});

/* attention (M10 §10): EVERY item blocked on a human decision is
   counted and reachable from anywhere via the header badge; the
   central dialog carries the same cards with their full quality
   fields. "Waiting for approval" is never shown as running. */
function attentionRowsHtml(includeGate) {
  const rows = [];
  let gateSeen = 0;
  const gates = (state.boot.approvals || [])
    .filter((a) => a.state === "pending" && a.action === "gate_op")
    .sort((x, y) => String(x.created_at).localeCompare(String(y.created_at)));
  const gateById = new Map(gates.map((a) => [a.id, a]));
  for (const a of (state.boot.approvals || [])) {
    if (a.state !== "pending") continue;
    let info = {};
    try { info = JSON.parse(a.detail || "{}"); } catch { /* plain note */ }
    if (a.action === "gate_op") {
      // the gate card lives inline in its chat; the dialog also
      // surfaces it so nothing waits unseen across tabs - oldest
      // first, one actionable, the rest visibly queued (P0-2)
      if (includeGate) {
        rows.push(`<div class="at-col">${
          gateCardHtml(gateById.get(a.id), gateSeen === 0)}</div>`);
        gateSeen += 1;
      }
      continue;
    }
    if (info.kind === "benchmark_consent") {
      // the consent card states the whole experiment and offers the
      // four answers the user is entitled to give.
      const fields = (info.fields || [])
        .map((f) => `<b>${esc(f.label)}</b><span>${esc(f.value)}</span>`)
        .join("");
      rows.push(`<div class="at-col">
        <span><b>${esc(t("attn.bench"))}</b> - ${esc(a.risk)}</span>
        <div class="at-fields">${fields}</div>
        <span class="hint">${esc(info.note || "")}</span>
        <span class="btnrow" style="margin:0">
          <button class="btn primary sm" data-appr="${esc(a.id)}" data-dec="approve">${esc(t("attn.approve"))}</button>
          <button class="btn sm" data-appr="${esc(a.id)}" data-dec="limit_scope">${esc(t("attn.limit"))}</button>
          <button class="btn sm" data-appr="${esc(a.id)}" data-dec="literature_only">${esc(t("attn.lit"))}</button>
          <button class="btn danger sm" data-appr="${esc(a.id)}" data-dec="reject">${esc(t("attn.reject"))}</button>
        </span></div>`);
      continue;
    }
    rows.push(`<div class="at-row">
      <span><b>${esc(t("attn.execute"))}</b> - ${esc(taskDisplayNameById(a.job_id))} (${esc(a.risk)})</span>
      <span class="btnrow" style="margin:0">
        <button class="btn primary sm" data-appr="${esc(a.id)}" data-ok="1">${esc(t("attn.approve"))}</button>
        <button class="btn danger sm" data-appr="${esc(a.id)}">${esc(t("attn.deny"))}</button>
      </span></div>`);
  }
  for (const d of (state.boot.decisions || [])) {
    if (d.status !== "pending") continue;
    // the fixed plan-review question gets the product gloss; the raw
    // server record stays in the tooltip (server text is not translated)
    const q = d.key === "native.plan_review"
      ? t("dec.nativePlan") : d.question;
    rows.push(`<div class="at-row">
      <span><b>${esc(t("attn.decision"))}</b> - ${esc(q)}</span>
      <span class="btnrow" style="margin:0">${(d.options || []).map((o) =>
        `<button class="btn sm" data-dec="${esc(d.id)}" data-choice="${esc(o.id)}"
          title="${esc(o.validation_status || "")}">${esc(o.label)}${o.policy_default ? " " + esc(t("attn.policyDefault")) : ""}</button>`).join("")}</span></div>`);
  }
  return rows;
}

function renderAttention() {
  const host = $("attention");
  const all = attentionRowsHtml(true);
  const badge = $("attn-badge");
  if (badge) {
    badge.classList.toggle("hidden", !all.length);
    badge.textContent = String(all.length);
    badge.title = t("attn.badgeTitle", { n: all.length });
  }
  const rows = attentionRowsHtml(false);
  if (!rows.length) { host.classList.add("hidden"); host.innerHTML = ""; return; }
  host.classList.remove("hidden");
  host.innerHTML = `<div class="at-head">${all.length} ${esc(t("attn.waiting"))}</div>` +
    rows.join("");
}

$("attn-badge").addEventListener("click", () => {
  const rows = attentionRowsHtml(true);
  // live scoped grants (P0-2): listed, dated, each revocable - the
  // management surface the "always allow within this scope" option
  // is required to have
  const gr = (state.boot.scope_grants || []).map((g) =>
    `<div class="at-row"><span><b>${esc((g.roots || []).join(", "))}</b>`
    + ` · ${esc(t("attn.scopeUntil"))} ${esc(fmtDT(new Date(
      g.expires * 1000).toISOString()))}</span>`
    + `<button class="btn sm" data-revoke="${esc(g.id)}">${esc(
      t("attn.revoke"))}</button></div>`).join("");
  const sec = gr ? `<div class="at-scopes"><b>${esc(
    t("attn.scopesH"))}</b>${gr}</div>` : "";
  if (!rows.length && !sec) return;
  uiDialog({ title: t("attn.center"),
             bodyHtml: `<div class="at-dialog">${rows.join("")}${sec}</div>`,
             actions: [{ label: t("dlg.close"), value: null, cls: "ghost" }] });
});

/* ---------------- tasks (M9 §8/§9) ---------------- */
const ACTIVE_STATES = ["queued", "running", "paused", "blocked"];
function statusPill(st, j) {
  const cls = { completed: "ok", running: "accent", queued: "accent",
                blocked: "warn", paused: "warn", failed: "danger",
                cancelled: "muted" }[st] || "muted";
  let lbl = t("st." + st) !== "st." + st ? t("st." + st) : st;
  // "waiting for approval" and "waiting for your decision" are kept
  // apart from "running" - at a glance, never the same thing (M10 §10)
  if (st === "blocked" && j && (state.boot?.decisions || [])
    .some((d) => d.status === "pending" && d.job_id === j.id))
    lbl = t("st.waitdec");
  return `<span class="pill ${cls}">${esc(lbl)}</span>`;
}
/* Display name: an explicit user title always wins; otherwise pieces
   derived from stored metadata only. The internal id stays beside it,
   smaller - never the headline. */
function displayName(j) {
  if (j.display_title) return j.display_title;
  const tp = t("type." + (j.name_type || "task"));
  return j.name_subject ? `${j.name_subject} · ${tp}` : tp;
}
function taskDisplayNameById(id) {
  const j = (state.boot.jobs || []).find((x) => x.id === id);
  return j ? displayName(j) : id;
}
function jrFlags(j) {
  const f = [];
  if (j.pinned) f.push(`<span class="jr-flag" title="${esc(t("tm.pinnedTag"))}">pin</span>`);
  if (j.locked) f.push(`<span class="jr-flag lock" title="${esc(t("tm.lockedTag"))}">lock</span>`);
  if (j.trashed) f.push(`<span class="jr-flag trash" title="${esc(t("tm.trashedTag"))}">trash</span>`);
  return f.join("");
}
function jrCheck(j) {
  // research rows are not job records (P1-9): no checkbox, no flags
  if (/^res-/.test(j.id)) return "";
  return `<input type="checkbox" class="jr-check" data-ck="${esc(j.id)}"`
    + `${state.bulkSel.has(j.id) ? " checked" : ""}>`;
}
function jobRowHtml(j) {
  const running = ACTIVE_STATES.includes(j.state);
  const bar = running && j.progress > 0
    ? `<span class="jr-bar"><i style="width:${Math.min(95, j.progress)}%"></i></span>`
    : "";
  const concl = j.kind === "discovery" && j.outcome
    ? t("concl." + j.outcome) !== "concl." + j.outcome
      ? t("concl." + j.outcome) : "" : "";
  const sub = [j.id, fmtDT(j.created_at), j.stage || "", concl]
    .filter(Boolean).join(" · ");
  // human headline on terminal rows; the raw code stays in the tech record
  const tail = j.state === "failed" ? " - " + esc(t("fail.head"))
    : j.error ? " - " + esc(j.error) : "";
  return `${jrCheck(j)}<div class="jr-main">
      <div class="jr-name">${esc(displayName(j))} ${jrFlags(j)}</div>
      <div class="jr-sub">${esc(sub)}${tail}</div>
    </div>
    <div class="jr-side">${bar}${statusPill(j.state, j)}</div>`;
}

/* P1-9 helpers: search filter, sort order (pinned first in every
   order), the bulk bar and the record-level trash view. Every
   destructive word here means RECORDS: no UI path deletes files. */
function tmMatch(j) {
  const q = (state.taskSearch || "").toLowerCase();
  if (!q) return true;
  return `${displayName(j)} ${j.id} ${j.sample_id || ""}`
    .toLowerCase().includes(q);
}
function tmSort(rows) {
  const s = state.taskSort;
  return rows.slice().sort((a, b) => {
    if ((b.pinned || 0) !== (a.pinned || 0)) return (b.pinned || 0) - (a.pinned || 0);
    if (s === "old") return (a.created_at || "").localeCompare(b.created_at || "");
    if (s === "name") return displayName(a).localeCompare(displayName(b));
    return (b.created_at || "").localeCompare(a.created_at || "");
  });
}
function renderBulk() {
  const bar = $("tm-bulk");
  const n = state.bulkSel.size;
  bar.hidden = !n;
  if (!n) { bar.innerHTML = ""; return; }
  const ids = [...state.bulkSel];
  bar.innerHTML = `<span class="hint">${esc(t("tm.bulkN", { n }))}</span>`
    + (state.trashMode
      ? `<button class="btn sm" id="tm-b-restore">${esc(t("tm.restore"))}</button>`
      : `<button class="btn sm" id="tm-b-pin">${esc(t("tm.pin"))}</button>`
        + `<button class="btn sm" id="tm-b-lock">${esc(t("tm.lock"))}</button>`
        + `<button class="btn sm danger" id="tm-b-trash">${esc(t("tm.trashBtn"))}</button>`)
    + `<button class="btn sm ghost" id="tm-b-clear">${esc(t("tm.bulkClear"))}</button>`;
  const bulk = async (flags) => {
    try {
      const res = await api("POST", "/api/jobs/flags/bulk",
        { ids, ...flags });
      const rr = res.results || [];
      const bad = rr.filter((x) => !x.ok);
      if (bad.length) {
        toast(t("tm.bulkPart",
          { ok: rr.length - bad.length, ref: bad.length,
            why: bad[bad.length - 1].error || "" }), true);
      } else {
        toast(t("tm.bulkDone", { ok: rr.length }));
      }
      state.bulkSel.clear();
      await refreshJobs();
      if (state.trashMode) await refreshTrash();
    } catch (e) { toast(e.message, true); }
  };
  const wire = (id, fn) => { const b = $(id); if (b) b.addEventListener("click", fn); };
  wire("tm-b-pin", () => bulk({ pinned: true }));
  wire("tm-b-lock", () => bulk({ locked: true }));
  wire("tm-b-trash", () => bulk({ trashed: true }));
  wire("tm-b-restore", () => bulk({ trashed: false }));
  wire("tm-b-clear", () => { state.bulkSel.clear(); renderJobs(); });
}

function mountPurgeConfirm(container, id) {
  // explicit confirmation: the task id must be TYPED back; the call
  // repeats it server-side, where the rule is enforced again. Files
  // are never part of this - the label says so in both languages.
  container.innerHTML = `<div class="conf-row tm-purge-box">
    <div class="hint">${esc(t("tm.purgeNote"))}</div>
    <input class="tm-purge-in" placeholder="${esc(id)}" style="max-width:260px">
    <button class="btn sm danger tm-purge-go">${esc(t("tm.purgeBtn"))}</button>
  </div>`;
  const inEl = container.querySelector(".tm-purge-in");
  const goEl = container.querySelector(".tm-purge-go");
  inEl.focus();
  goEl.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const val = inEl.value.trim();
    if (val !== id) { toast(t("tm.purgeMismatch"), true); return; }
    try {
      await api("POST", `/api/jobs/${encodeURIComponent(id)}/purge`,
        { confirm: id });
      toast(t("tm.purged"));
      state.bulkSel.delete(id);
      if (state.resultsJob === id) state.resultsJob = null;
      await refreshTrash();
      await refreshJobs();
      renderResults();
    } catch (e) { toast(e.message, true); }
  });
  container.addEventListener("click", (ev) => ev.stopPropagation());
}

async function refreshTrash() {
  try {
    const r = await api("GET", "/api/jobs?trashed=1");
    state.trashJobs = r.jobs || [];
    state.trashCount = state.trashJobs.length;
  } catch { /* transient */ }
  renderJobs();
}

function renderJobs() {
  const host = $("job-list");
  const tt = $("tm-trash-toggle");
  tt.textContent = state.trashMode ? t("tm.backToList") : t("tm.showTrash");
  tt.classList.toggle("active", state.trashMode);
  $("tm-trash-count").textContent =
    state.trashCount ? `(${state.trashCount})` : "";
  if (state.trashMode) {
    // P1-9 trash: records only, restore always available, and the
    // one destructive action (record purge) behind a typed
    // confirmation. Rows do NOT open Results - a trashed record is
    // restored first, which is also the honest signal of intent.
    $("history-block").hidden = true;
    const rows = tmSort((state.trashJobs || []).filter(tmMatch));
    host.innerHTML = `<div class="alert info tm-note">${esc(t("tm.trashNote"))}
      ${esc(t("tm.retentionHint", { d: ((state.boot || {}).settings || {})
        .trash_retention_days || 30 }))}</div>`
      + (rows.length ? rows.map((j) => `<div class="job-row trashed"
          data-trash="${esc(j.id)}">${jobRowHtml(j)}
          ${j.trashed_at ? `<div class="jr-sub hint">${esc(t("tm.trashAge", { d: j.trashed_at }))}</div>` : ""}
          <div class="btnrow tm-row-acts">
            <button class="btn sm" data-restore="${esc(j.id)}">${esc(t("tm.restore"))}</button>
            <button class="btn sm danger" data-purge="${esc(j.id)}">${esc(t("tm.purge"))}</button>
          </div></div>`).join("")
        : `<div class="empty">${esc(t("tm.trashEmpty"))}</div>`);
    document.querySelectorAll("[data-trash]").forEach((el) =>
      el.addEventListener("click", (ev) => {
        if (ev.target.closest(".tm-row-acts,.tm-purge-box,.jr-check")) return;
        toast(t("tm.trashRowClick"));
      }));
    document.querySelectorAll("[data-restore]").forEach((b) =>
      b.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        try {
          await api("POST",
            `/api/jobs/${encodeURIComponent(b.dataset.restore)}/flags`,
            { trashed: false });
          toast(t("tm.restored"));
          await refreshTrash();
          await refreshJobs();
        } catch (e) { toast(e.message, true); }
      }));
    document.querySelectorAll("[data-purge]").forEach((b) =>
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const row = b.closest("[data-trash]");
        const old = row.querySelector(".tm-purge-wrap");
        if (old) { old.remove(); return; }
        const wrap = document.createElement("div");
        wrap.className = "tm-purge-wrap";
        row.appendChild(wrap);
        mountPurgeConfirm(wrap, b.dataset.purge);
      }));
    renderBulk();
    wireChecks();
    return;
  }
  $("history-block").hidden = false;
  const kind = state.kindFilter;
  const all = (state.boot.jobs || []).filter((j) => {
    if (!kind) return true;
    if (kind === "analysis") return j.kind === "analysis" || j.kind === "neoag-native-run";
    return j.kind === kind;
  }).filter(tmMatch);
  const active = tmSort(all.filter((j) => ACTIVE_STATES.includes(j.state)));
  const past = tmSort(all.filter((j) => !ACTIVE_STATES.includes(j.state)));
  const rowCls = (j) => state.resultsJob === j.id ? "selected" : "";
  host.innerHTML = active.length ? active.map((j) =>
    `<div class="job-row ${rowCls(j)}"
      data-job="${esc(j.id)}">${jobRowHtml(j)}</div>`).join("")
    : `<div class="empty">${esc(t("run.none"))}</div>`;
  $("history-count").textContent = past.length ? `(${past.length})` : "";
  $("history-list").innerHTML = past.length ? past.map((j) =>
    `<div class="job-row ${rowCls(j)}" data-job="${esc(j.id)}">${jobRowHtml(j)}</div>`).join("")
    : `<div class="empty">${esc(t("run.histnone"))}</div>`;
  // P0-8: every task row - job or research - opens the ONE place for
  // that task: Results with the task selected. Research rows get a
  // pointer card there with an explicit "open in Discovery" action.
  document.querySelectorAll("[data-job]").forEach((el) =>
    el.addEventListener("click", () => openJob(el.dataset.job)));
  renderBulk();
  wireChecks();
}

function wireChecks() {
  document.querySelectorAll(".jr-check").forEach((cb) => {
    cb.addEventListener("click", (ev) => ev.stopPropagation());
    cb.addEventListener("change", () => {
      if (cb.checked) state.bulkSel.add(cb.dataset.ck);
      else state.bulkSel.delete(cb.dataset.ck);
      renderBulk();
    });
  });
}

document.querySelectorAll("#kind-filter .chip").forEach((c) =>
  c.addEventListener("click", () => {
    document.querySelectorAll("#kind-filter .chip").forEach((x) =>
      x.classList.remove("active"));
    c.classList.add("active");
    state.kindFilter = c.dataset.kind;
    VS.set("kindFilter", c.dataset.kind);
    renderJobs();
  }));

/* P1-9 toolbar: search + sort apply to whatever list is shown; the
   trash toggle swaps the same list to the record-level trash. */
$("tm-search").addEventListener("input", () => {
  state.taskSearch = $("tm-search").value.trim();
  renderJobs();
});
$("tm-sort").addEventListener("change", () => {
  state.taskSort = $("tm-sort").value;
  VS.set("taskSort", state.taskSort);
  renderJobs();
});
$("tm-trash-toggle").addEventListener("click", () => {
  state.trashMode = !state.trashMode;
  state.bulkSel.clear();
  if (state.trashMode) refreshTrash();
  else renderJobs();
});

/* M11 P0-8: ONE place per task. The former drawer is gone - clicking a
   task in Tasks opens Results with that task selected, and Results
   renders EVERY state: running (progress + controls), blocked,
   cancelled, failed (confirmed reason + partial outputs) and
   completed. The task head below is the drawer's identity, state
   pair and controls, merged into the Results view. */
function openJob(id) {
  const job = (state.boot.jobs || []).find((j) => j.id === id);
  if (!job) { toast(t("toast.openFail", { e: id }), true); return; }
  state.resultsJob = id;
  state.resultsStrategy = null;
  VS.set("resultsJob", id);
  switchView("results");
  renderJobs();
  renderResults();
}

/* task head: name + rename, identity, execution/conclusion pair,
   progress and the control row - visible for EVERY state, because
   every state deserves controls (cancel a running one, rename a
   finished one). */
function taskHeadHtml(job, task) {
  const running = ACTIVE_STATES.includes(job.state);
  const bar = running && job.progress > 0
    ? `<div class="jr-bar wide"><i style="width:${
        Math.min(95, job.progress)}%"></i></div>` : "";
  const conclHtml = job.kind === "discovery" && job.outcome
    ? `<span class="pill ${job.outcome === "decision_ready" ? "ok" : "warn"}">${esc(t("concl." + job.outcome) !== "concl." + job.outcome ? t("concl." + job.outcome) : job.outcome)}</span>`
    : job.outcome ? `<span class="pill ${job.outcome === "delivered" ? "ok" : "warn"}">${esc(job.outcome.replace(/_/g, " "))}</span>` : "";
  return `<div class="task-head">
    <div class="dr-title-row">
      <h2 class="th-title">${esc(displayName(job))}</h2>
      <button class="btn sm ghost" id="th-rename">${esc(t("dr.rename"))}</button>
    </div>
    <div class="dr-sub">${esc(t("dr.created"))}: ${esc(fmtDT(job.created_at))}
      &nbsp;·&nbsp; ${esc(t("dr.id"))}: ${esc(job.id)}</div>
    <div class="state-pair"><span>${esc(t("dr.exec"))}:</span>
      ${statusPill(job.state, job)} ${conclHtml}</div>
    ${bar}
    <div class="btnrow" style="margin-top:8px">
      <button class="btn sm" id="th-pause"
        ${running && !(task && task.native) ? "" : "disabled"}
        title="${task && task.native ? esc(t("dr.noPause")) : ""}">${esc(t("dr.pause"))}</button>
      <button class="btn sm" id="th-resume" ${job.state === "paused" ? "" : "disabled"}>${esc(t("dr.resume"))}</button>
      <button class="btn sm danger" id="th-cancel"
        ${!["completed", "failed", "cancelled"].includes(job.state) ? "" : "disabled"}
        title="${task && task.native && task.native.profile === "gateway" ? esc(t("dr.cancelNote")) : ""}">${esc(t("dr.cancel"))}</button>
    </div>
    ${taskFlagsRow(job)}
    <div id="th-rename-box"></div>
    <div id="th-purge-box"></div>
  </div>`;
}

/* P1-9: the record-management row. Buttons state WHY they are
   disabled (cancel-first, locked) instead of silently graying out;
   the trash action says "record", and the purge confirmation repeats
   that files are never part of it. */
function taskFlagsRow(job) {
  const active = ACTIVE_STATES.includes(job.state);
  const trashDisabled = job.locked || active || job.trashed;
  const trashWhy = job.trashed ? ""
    : job.locked ? t("tm.lockedFirst")
      : active ? t("tm.cancelFirst") : "";
  return `<div class="btnrow tm-flags">
    <button class="btn sm ghost" id="th-pin">${esc(t(job.pinned ? "tm.unpin" : "tm.pin"))}</button>
    <button class="btn sm ghost" id="th-lock">${esc(t(job.locked ? "tm.unlock" : "tm.lock"))}</button>
    ${job.trashed
      ? `<span class="pill warn">${esc(t("tm.trashedTag"))}</span>
         <button class="btn sm" id="th-restore">${esc(t("tm.restore"))}</button>`
      : `<button class="btn sm danger" id="th-trash"
          ${trashDisabled ? "disabled" : ""}
          title="${esc(trashWhy)}">${esc(t("tm.trashBtn"))}</button>`}
    ${job.trashed ? `<button class="btn sm danger" id="th-purge"
        ${job.locked ? "disabled" : ""}
        title="${job.locked ? esc(t("tm.lockedFirst")) : ""}">${esc(t("tm.purge"))}</button>` : ""}
  </div>`;
}

function wireTaskHead(job) {
  const id = job.id;
  $("th-rename").addEventListener("click", () => {
    const box = $("th-rename-box");
    box.innerHTML = `<div class="conf-row">
      <input id="th-title-in" value="${esc(displayName(job))}" style="max-width:340px">
      <button class="btn primary sm" id="th-title-save">${esc(t("dr.renameSave"))}</button>
      <button class="btn ghost sm" id="th-title-cancel">${esc(t("dr.renameCancel"))}</button></div>`;
    $("th-title-in").focus();
    $("th-title-cancel").addEventListener("click", () => { box.innerHTML = ""; });
    $("th-title-save").addEventListener("click", async () => {
      const title = $("th-title-in").value.trim();
      if (!title) return;
      try {
        await api("POST", `/api/jobs/${encodeURIComponent(id)}/title`, { title });
        box.innerHTML = "";
        toast(t("toast.renamed"));
        await refreshJobs();
        renderResults();
      } catch (e) { toast(e.message, true); }
    });
  });
  $("th-pause").addEventListener("click", () => api("POST", `/api/jobs/${id}/pause`, {}).then(refreshJobs));
  $("th-resume").addEventListener("click", () => api("POST", `/api/jobs/${id}/resume`, {}).then(refreshJobs));
  $("th-cancel").addEventListener("click", () => api("POST", `/api/jobs/${id}/cancel`, {}).then(refreshJobs));
  // P1-9: record flags through the same single-rule endpoint the
  // bulk path uses; the server enforces cancel-first/locked again.
  const flag = (bid, body, okMsg) => {
    const b = $(bid);
    if (!b || b.disabled) return;
    b.addEventListener("click", async () => {
      try {
        await api("POST", `/api/jobs/${encodeURIComponent(id)}/flags`,
          body);
        toast(okMsg);
        await refreshJobs();
        renderResults();
      } catch (e) { toast(e.message, true); }
    });
  };
  flag("th-pin", { pinned: !job.pinned },
    t(job.pinned ? "tm.unpinned" : "tm.pinned"));
  flag("th-lock", { locked: !job.locked },
    t(job.locked ? "tm.unlocked" : "tm.locked"));
  flag("th-trash", { trashed: true }, t("tm.trashed"));
  flag("th-restore", { trashed: false }, t("tm.restored"));
  const pb = $("th-purge");
  if (pb && !pb.disabled) pb.addEventListener("click", () => {
    const box = $("th-purge-box");
    if (box.innerHTML) { box.innerHTML = ""; return; }
    mountPurgeConfirm(box, id);
  });
}

/* the former drawer's technical record, kept verbatim in content:
   requested config, native execution, decisions, snapshot, events */
function taskTechHtml(job, task, detail) {
  const decisions = (task && task.decisions) || [];
  return `<details class="adv" id="res-tech-fold">
    <summary>${esc(t("dr.tech"))}</summary>
    <details class="adv"><summary>${esc(t("dr.reqcfg"))}</summary>
      <div class="kv">${Object.entries((job.spec &&
        job.spec.requested_config) || {})
        .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("") ||
        `<div class="hint">${esc(t("dr.defOnly"))}</div>`}</div></details>
    ${task && task.native ? `<details class="adv"><summary>native execution</summary>
      <div class="kv">
        <div class="k">engine</div><div>native ${esc(task.native.engine_version || "")} (${esc(task.native.profile || "")})</div>
        <div class="k">inputs</div><div><code>${esc(JSON.stringify(task.native.native_params || {}))}</code></div>
        <div class="k">outdir</div><div><code>${esc(task.native.outdir || "-")}</code></div>
        ${task.native.failure_code ? `<div class="k">failure</div><div>${esc(task.native.failure_code)}</div>` : ""}
      </div>
      ${task.native.plan && (task.native.plan.steps || []).length
        ? `<details open><summary class="hint">${esc(t("plan.runplan"))}</summary><ul>${
          (task.native.plan.steps || []).map((s) => `<li><b>${esc(s.step_id || "")} ${esc(s.name || "")}</b>: ${esc(s.status || "")}${s.detail ? " - " + esc(String(s.detail).slice(0, 140)) : ""}</li>`).join("")
        }</ul></details>` : ""}
    </details>` : ""}
    <details class="adv"><summary>${esc(t("dr.decisions"))}</summary><div class="timeline">${decisions.map((d) => `
      <div class="tl-row"><span class="tl-ts">${esc(d.ts || "")}</span>
        <span class="tl-kind">${esc(d.key || d.decision_type || "")}</span>
        <span>${esc(d.question || "")} -> <b>${esc(d.chosen || "pending")}</b>
        <span class="hint">(${esc(d.decided_by || "")}, ${esc(d.policy_id || "")}${
          d.policy_version ? " v" + esc(d.policy_version) : ""})</span>
        ${d.downstream && d.downstream.applied_effect
          ? `<div class="hint">effect applied: ${esc(
              JSON.stringify(d.downstream.applied_effect))}</div>` : ""}</span>
      </div>`).join("") || `<div class="hint">${esc(t("dr.noDec"))}</div>`}</div></details>
    <details class="adv"><summary>${esc(t("dr.snapshot"))}</summary>
      <div id="th-snapshot"><div class="hint">…</div></div></details>
    <details class="adv"><summary>${esc(t("dr.events"))}</summary>
      <div class="logview">${(detail || []).slice(0, 120)
        .map((e) => `${esc(e.ts || "")} ${esc(e.kind || "")} ${esc(e.text || "")}`)
        .join("\n")}</div></details>
  </details>`;
}

function fillTaskSnapshot(jid, snap) {
  const el = $("th-snapshot");
  if (!el || state.resultsJob !== jid) return;
  const s = snap.snapshot || {};
  el.innerHTML = `
    <div class="kv">${Object.entries({ job: s.job_id, engine: s.engine_version,
      policy: s.policy_version, started: snap.created_at,
      execution_mode_at_start: s.execution_mode_at_start,
      dataset: s.dataset_version,
      seed: s.seed,
      stage_plan: (s.stage_plan || []).join(", ") })
      .filter(([, v]) => v !== undefined && v !== "")
      .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(
        typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("")}</div>
    <details class="adv"><summary>${esc(t("dr.snapFull"))}</summary>
      <pre class="tool-out">${esc(JSON.stringify(s, null, 1))}</pre></details>
    <div class="hint">${esc(t("dr.snapNote"))}</div>`;
}

function renderTasks() {
  renderScience();
  renderOptions();
  renderReadiness();
  renderSubmitState();
}

/* ---------------- results view ---------------- */
const OUTCOME_CLS = { delivered: "ok", zero_candidates: "warn",
  partial: "warn", generation_failed: "danger", no_artifacts: "danger" };

function candTable(rows) {
  if (!rows || !rows.length) {
    return `<div class="alert info">${esc(t("res.empty0"))}</div>`;
  }
  return `<div class="tablewrap"><table class="data">
      <thead><tr><th>Rank</th><th>Peptide</th><th>Gene</th><th>HLA</th>
      <th>Branch</th><th>Score</th><th>Normal hits</th></tr></thead>
      <tbody>${rows.map((r) => `
      <tr><td>${esc(r.rank)}</td><td><code>${esc(r.peptide)}</code></td>
      <td>${esc(r.gene)}</td><td>${esc(r.hla)}</td><td>${esc(r.branch)}</td>
      <td>${esc(r.mock_combined_score ?? r.combined_score ?? "")}</td>
      <td>${esc(r.safety_normal_hits ?? "")}</td></tr>`).join("")}</tbody>
    </table></div>`;
}

/* the single authoritative results registry (M10 §14): EVERY formal
   run appears with its real outcome - running, waiting, failed,
   cancelled, zero-candidates or delivered - plus the Discovery
   reviews, whose reports stay owned by that workspace. Legacy
   simulation history is hidden by default but never deleted. */
function clearResultsPanes() {
  ["res-task", "res-tech", "res-findings", "res-warnings",
   "res-artifacts", "res-strategy", "res-table", "res-meta",
   "res-manifest"].forEach((id) => { $(id).innerHTML = ""; });
}

function artifactRows(arts, jid) {
  if (!arts.length) return "";
  return `<div class="tablewrap"><table class="data">
    <thead><tr><th>Artifact</th><th>Type</th><th>Status</th>
    <th>Rows</th><th></th></tr></thead><tbody>${arts.map((a) => `
      <tr><td>${esc(a.name)}</td><td>${esc(a.type || a.strategy || "")}</td>
      <td><span class="pill ${a.status === "available" ? "ok"
        : a.status === "empty" ? "warn" : "danger"}">${esc(a.status)}</span></td>
      <td>${esc(a.rows ?? "")}</td>
      <td>${a.status === "available"
        ? `<a class="btn sm ghost" href="/api/results/${encodeURIComponent(
            jid)}/artifact/${encodeURIComponent(a.id ?? a.name)}"
            download>${esc(t("res.dl"))}</a>` : ""}</td></tr>`).join("")
    }</tbody></table></div>`;
}

let resultsSeq = 0;
async function renderResults() {
  const seq = ++resultsSeq; // late async fills must not overwrite a newer selection
  const jobs = (state.boot?.jobs || []).filter((j) => !/^res-/.test(j.id));
  const research = state.boot?.research || [];
  const isMock = (j) => !j.spec || j.spec.engine === "mock";
  const hasMock = jobs.some(isMock);
  $("res-mock-wrap").hidden = !hasMock;
  $("res-mock").checked = !!state.resultsMock;
  const runs = jobs.filter((j) => state.resultsMock || !isMock(j));
  const opts = runs.map((j) => {
    const st = t("st." + j.state) !== "st." + j.state ? t("st." + j.state) : j.state;
    return `<option value="${esc(j.id)}">${esc(displayName(j))} · ${st}` +
      `${j.outcome ? " - " + esc(j.outcome.replace(/_/g, " ")) : ""}` +
      ` · ${esc(j.id)}</option>`;
  }).join("") + research.map((r) =>
    `<option value="${esc(r.id)}">${esc(researchName(r))} · ${
      esc(t("res.discoveryRow"))}</option>`).join("");
  $("res-job").innerHTML = opts;
  if (!opts) {
    $("res-outcome").innerHTML = `<div class="empty">${esc(t("res.none"))}</div>`;
    clearResultsPanes();
    return;
  }
  if (!state.resultsJob ||
      !opts.includes(`value="${state.resultsJob}"`)) {
    state.resultsJob = $("res-job").options[0].value;
    VS.set("resultsJob", state.resultsJob);
  }
  $("res-job").value = state.resultsJob;
  const jid = state.resultsJob;
  if (/^res-/.test(jid)) {
    const r = research.find((x) => x.id === jid) || {};
    $("res-outcome").innerHTML = `<div class="alert info">
      <b>${esc(researchName(r))}</b> ${resPill(r.status || "completed")}
      <div>${esc(t("res.discoveryNote"))}</div>
      <div class="btnrow" style="margin-top:6px"><button class="btn sm"
        id="res-open-disc">${esc(t("res.openDiscovery"))}</button></div></div>`;
    $("res-open-disc").addEventListener("click", () => {
      const tab = document.querySelector('.wtab[data-view="discovery"]');
      if (tab) tab.click();
      showReport(jid);
    });
    clearResultsPanes();
    return;
  }
  const job = jobs.find((j) => j.id === jid);
  clearResultsPanes();
  if (job) {
    // P0-8: the task head (identity, state pair, progress, controls)
    // and the technical record render for EVERY state - the former
    // drawer content, now inline where the results live.
    let task = null;
    let detail = [];
    try { detail = await api("GET", `/api/jobs/${jid}/events`); } catch { /* older row */ }
    if (seq !== resultsSeq) return;
    try { task = await api("GET", `/api/tasks/${jid}`); } catch { /* older row */ }
    if (seq !== resultsSeq) return;
    $("res-task").innerHTML = taskHeadHtml(job, task);
    wireTaskHead(job);
    $("res-tech").innerHTML = taskTechHtml(job, task, detail);
    api("GET", `/api/tasks/${jid}/snapshot`)
      .then((s) => fillTaskSnapshot(jid, s)).catch(() => {});
  }
  if (job && ["queued", "running", "paused", "blocked"].includes(job.state)) {
    const st = t("st." + job.state) !== "st." + job.state ? t("st." + job.state) : job.state;
    $("res-outcome").innerHTML = alertHtml("info", displayName(job),
      esc(t("res.inprogress", { state: st })));
    return;
  }
  if (job && job.state === "cancelled") {
    $("res-outcome").innerHTML =
      `<div class="outcome muted"><b>${esc(t("st.cancelled"))}</b></div>` +
      alertHtml("info", displayName(job), esc(t("res.cancelledNote")));
    return;
  }
  if (job && job.state === "failed") {
    $("res-outcome").innerHTML = `<div class="prep-progress">
      <span class="spin"></span></div>`;
    api("GET", `/api/results/${jid}`).then((res) => {
      if (seq !== resultsSeq) return;
      const arts = res.artifacts || [];
      $("res-outcome").innerHTML =
        `<div class="outcome danger"><b>${esc(
          (res.outcome || "generation_failed").replace(/_/g, " "))}</b>
         <div class="hint">${esc(res.note || "")}</div></div>` +
        `<div class="failcard-inline"><b>${esc(t("fail.head"))}</b>` +
        failureBodyHtml(res.failure, res.completed_before_failure,
                        arts.length) +
        `<details class="adv"><summary>technical</summary><code>${esc(
          res.failure_detail || "")}</code></details></div>`;
      $("res-artifacts").innerHTML =
        `<h3 class="sec">${esc(t("res.artH", { n: arts.length }))}</h3>` +
        (artifactRows(arts, jid) ||
         `<div class="hint">${esc(t("fail.partial0"))}</div>`);
    }).catch((e) => {
      $("res-outcome").innerHTML = alertHtml("warn", t("fail.head"),
        esc(t("res.unavail", { e: e.message })));
    });
    return;
  }
  api("GET", `/api/results/${jid}`).then((res) => {
    if (seq !== resultsSeq) return;
    const outcome = res.outcome || "no_artifacts";
    $("res-outcome").innerHTML = `
      <div class="outcome ${esc(OUTCOME_CLS[outcome] || "muted")}">
        <b>${esc(outcome.replace(/_/g, " "))}</b>
        <div>${esc(res.outcome_note || "")}</div>
        <div class="hint">${esc(res.delivery_claim || "")}</div>
      </div>
      ${res.outcome_drift ? `<div class="alert warn">${esc(res.outcome_drift)}</div>` : ""}
      ${res.production_release === false
        ? `<div class="alert info">${esc(res.release_note || "")}</div>` : ""}
      <div class="btnrow"><a class="btn sm"
        href="/api/results/${esc(state.resultsJob)}/bundle"
        download>${esc(t("res.bundle"))}</a>
        <span class="hint">${esc(t("res.download"))}</span>
      </div>`;
    $("res-meta").innerHTML = `
      <div class="hint">${esc(res.disclaimer || "")}</div>
      <div class="kv">${Object.entries(res.provenance || {})
        .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>`;
    $("res-warnings").innerHTML = (res.warnings || []).length
      ? `<div class="warnlist">${res.warnings.map((w) =>
          `<div class="alert warn">${esc(w)}</div>`).join("")}</div>` : "";
    if (res.engine === "benchmark") {
      // the stated metrics are shown COMPUTED or honestly marked
      // unanswered - never silently omitted
      const m = res.metrics || {};
      const ck = (res.arms_summary || {}).candidate || {};
      $("res-meta").insertAdjacentHTML("afterbegin",
        `<div class="card"><b>Benchmark metrics</b>` +
        `<div class="hint">candidate arm: ${esc(ck.kind || "")}` +
        `${ck.tool ? " (" + esc(ck.tool) + ")" : ""}</div>` +
        (m.computed ? `<pre class="json">${esc(JSON.stringify(m.computed, null, 1))}</pre>` : "") +
        `<div class="alert${m.computed ? " info" : " warn"}">${esc(m.note || "")}</div>` +
        `<div class="hint">seed ${esc((res.reproducibility || {}).seed || "")}` +
        ` - the arm files under this run's directory are the measured` +
        ` artefacts (see the results bundle)</div></div>`);
    }
    const arts = res.artifacts || (res.manifest || {}).artifacts || [];
    // findings first: the shortlist before any tables or provenance
    const byS0 = res.candidates_by_strategy || {};
    const cands0 = byS0[state.resultsStrategy] ||
      byS0[Object.keys(byS0)[0]] || res.candidates || [];
    let findings = "";
    if (cands0.length) {
      findings = `<h3 class="sec">${esc(t("res.shortlist"))}</h3>` +
        cands0.slice(0, 3).map((r) => `<div class="find-row">
          <b>${esc(r.peptide)}</b> · ${esc(r.gene)} · HLA ${esc(r.hla)} ·
          ${esc(r.branch)} · ${esc(r.mock_combined_score ?? r.combined_score ?? "")}</div>`).join("");
    } else if ((res.preview_tables || []).length) {
      const pt = res.preview_tables[0];
      findings = `<h3 class="sec">${esc(t("res.shortlist"))}</h3>` +
        (pt.rows || []).slice(0, 3).map((row) =>
          `<div class="find-row">${(row.slice(0, 4) || []).map(esc).join(" · ")}</div>`).join("") +
        `<div class="hint">${esc(t("res.previewNote"))}</div>`;
    } else if (outcome === "zero_candidates") {
      findings = alertHtml("info", t("res.empty0"), "");
    }
    $("res-findings").innerHTML = findings;
    $("res-artifacts").innerHTML = `<h3 class="sec">${esc(t("res.artH", { n: arts.length }))}</h3>` +
      (artifactRows(arts, jid) ||
       `<div class="alert warn">${esc(t("res.noArtReg"))}</div>`);
    // every strategy the run produced gets its own table, not one
    const byS = res.candidates_by_strategy || {};
    const strategies = Object.keys(byS).length ? Object.keys(byS)
      : (res.candidates_strategy ? [res.candidates_strategy] : []);
    if (!state.resultsStrategy || !strategies.includes(state.resultsStrategy))
      state.resultsStrategy = strategies[0] || null;
    if (strategies.length) {
      $("res-strategy").innerHTML = `<div class="row"><label class="lbl">${esc(t("res.ranking"))}
        <select id="res-strategy-sel">${strategies.map((s) => {
          const a = arts.find((x) => x.strategy === s);
          return `<option value="${esc(s)}">${esc(s)}${
            a ? ` (${a.rows} rows, ${a.status})` : ""}</option>`;
        }).join("")}</select></label></div>`;
      $("res-strategy-sel").value = state.resultsStrategy;
      $("res-strategy-sel").addEventListener("change", () => {
        state.resultsStrategy = $("res-strategy-sel").value;
        renderResults();
      });
      $("res-table").innerHTML = candTable(
        byS[state.resultsStrategy] || res.candidates || []);
    } else if ((res.preview_tables || []).length) {
      // native runs: the pipeline's OWN ranked tables, verbatim
      $("res-strategy").innerHTML = "";
      $("res-table").innerHTML =
        `<h3 class="sec">${esc(t("res.previewH"))}</h3>
         <div class="hint">${esc(t("res.previewNote"))}</div>` +
        res.preview_tables.map((pt) => `
          <div class="tablewrap"><table class="data">
          <thead><tr>${(pt.header || []).map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
          <tbody>${(pt.rows || []).map((r) =>
            `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody>
          </table><div class="hint">${esc(pt.name)} (${esc(pt.kind || "")})</div></div>`).join("");
    } else {
      $("res-strategy").innerHTML = "";
      $("res-table").innerHTML = arts.length
        ? `<div class="hint">${esc(t("res.noCand"))}</div>` : "";
    }
    const man = res.manifest || {};
    const kv = (o) => Object.entries(o || {}).map(([k, v]) =>
      `<div class="k">${esc(k)}</div><div>${esc(
        typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("");
    $("res-manifest").innerHTML = `
      <div class="kv">${kv({ manifest_ref: man.manifest_ref,
        execution_mode: man.execution_mode,
        requested_vs_effective_note: man.requested_vs_effective_note,
        generated_at: man.generated_at, snapshot: man.snapshot })}</div>
      <h3 class="sec">${esc(t("dr.reqcfg"))}</h3>
      <div class="kv">${kv(man.requested_config) || `<div class="hint">${esc(t("dr.defOnly"))}</div>`}</div>
      <h3 class="sec">${esc(t("dr.effcfg"))}</h3>
      <div class="kv">${kv(man.effective_config)}</div>
      <h3 class="sec">${esc(t("dr.versions"))}</h3>
      <div class="kv">${kv(man.versions)}</div>
      <h3 class="sec">${esc(t("dr.decisions"))}</h3>
      <div class="timeline">${(man.decisions || []).map((d) => `
        <div class="tl-row"><span class="tl-kind">${esc(d.key || d.decision_type)}</span>
        <span>${esc(d.question)} -> <b>${esc(d.chosen || "pending")}</b> (${esc(d.decided_by)}, ${esc(d.policy_id || "")})</span></div>`).join("")
        || `<div class="hint">${esc(t("dr.noDec"))}</div>`}</div>`;
  }).catch((e) => {
    $("res-outcome").innerHTML = `<div class="alert warn">${esc(t("res.unavail", { e: e.message }))}</div>`;
    $("res-table").innerHTML = "";
  });
}
$("res-job").addEventListener("change", () => {
  state.resultsJob = $("res-job").value;
  state.resultsStrategy = null;
  VS.set("resultsJob", state.resultsJob);
  renderResults();
});
$("res-mock").addEventListener("change", () => {
  state.resultsMock = $("res-mock").checked;
  renderResults();
});

/* ---------------- discovery (real retrieval + example fixture) -------- */
function discoveryTargets() {
  const d = (state.boot || {}).discovery || {};
  return d.live_targets || [];
}

function renderDiscoveryControls() {
  if (!$("res-scope")) return;
  const scope = $("res-scope").value;
  const targeted = scope === "targeted";
  $("res-target-hint").textContent = targeted
    ? t("disc.targetsT") : t("disc.targetsF");
  const box = $("res-targets");
  box.classList.toggle("off", !targeted);
  const sel = state.discSel || {};
  box.innerHTML = discoveryTargets().map((x) => `
    <label class="stage-row"><input type="checkbox" data-dtarget="${esc(x.id)}"
      ${sel[x.id] ? "checked" : ""} ${targeted ? "" : "disabled"}>
      <span>${esc(x.label)}</span>
      ${x.code_verified ? "" : `<span class="dep">${esc(t("disc.docsStage"))}</span>`}
      ${(x.tools || []).length
        ? `<span class="dep">${esc(t("disc.uses"))} ${esc(x.tools.join(", "))}</span>` : ""}
    </label>`).join("");
  box.querySelectorAll("[data-dtarget]").forEach((c) =>
    c.addEventListener("change", () => {
      state.discSel = state.discSel || {};
      state.discSel[c.dataset.dtarget] = c.checked;
    }));
  $("res-custom").disabled = !targeted;
}

function researchName(r) {
  if (r.display_title) return r.display_title;
  return r.name_subject
    ? `${r.name_subject} · ${t("type." + (r.name_type || "review"))}`
    : t("type.review");
}
function resPill(st) {
  const cls = st === "completed" ? "ok"
    : ["running", "queued"].includes(st) ? "accent" : "warn";
  const lbl = t("st." + st) !== "st." + st ? t("st." + st) : st;
  return `<span class="pill ${cls}">${esc(lbl)}</span>`;
}

function renderDiscovery() {
  const list = ((state.boot || {}).research || []);
  const ACTIVE = ["running", "queued"];
  $("res-list").innerHTML = list.length ? list.map((r) => `
    <div class="job-row" data-res="${esc(r.id)}">
      <div class="jr-name">${esc(researchName(r))}</div>
      <div class="jr-sub">${esc(r.id)} · ${esc(r.engine === "live" ? t("disc.engineL") : t("disc.engineE"))} · ${esc(r.scope)} · ${resPill(r.status)}</div>
      ${(r.progress && r.progress.note)
        ? `<div class="jr-sub hint">${esc(r.progress.note)}</div>` : ""}
    </div>`).join("")
    : `<div class="empty">${esc(t("disc.none"))}</div>`;
  document.querySelectorAll("[data-res]").forEach((el) =>
    el.addEventListener("click", () => showReport(el.dataset.res)));
  if (list.length && !list.some((r) => r.id === state.discOpen))
    showReport(list[0].id);
  // keep the view honest while something runs (SSE may be buffered)
  clearTimeout(renderDiscovery._t);
  if (list.some((r) => ACTIVE.includes(r.status)))
    renderDiscovery._t = setTimeout(refreshDiscovery, 2500);
}

function exportBar(rid) {
  // files are rendered ONCE server-side, pinned to the stored report
  // content, and re-downloads serve those exact bytes (the view never
  // re-runs retrieval to serve a download).
  const b = (fmt, label) =>
    `<a class="btn sm" href="/api/research/${encodeURIComponent(rid)}` +
    `/export/${fmt}" download>${label}</a>`;
  return `<div class="btnrow">${b("html", esc(t("disc.dl", { f: "HTML" })))}` +
    `${b("json", esc(t("disc.dl", { f: "JSON" })))}` +
    `${b("tsv", esc(t("disc.dlTsv")))}${b("pdf", esc(t("disc.dl", { f: "PDF" })))}` +
    `<span class="hint">${esc(t("disc.dlNote"))}</span></div>`;
}

async function discoveryAction(btn) {
  const d = btn.dataset;
  const out = document.getElementById("dact-" + d.tid);
  const show = (html) => { if (out) out.innerHTML = html; };
  const qs = `candidate=${encodeURIComponent(d.tool)}` +
    `&target=${encodeURIComponent(d.tid)}`;
  try {
    if (d.dact === "bench") {
      const r = await api("POST", "/api/jobs", {
        kind: "benchmark",
        benchmark: {
          hypothesis: `${d.tool} versus ${d.base || "the configured tool"} ` +
            "changes the ranking metrics on this deployment's own " +
            "validation inputs",
          tools: { candidate: d.tool,
            baseline: d.base || "configured tool" },
          candidate_strategy: "evidence-consensus",
          metrics: ["top_k_overlap", "rank_correlation_spearman",
            "candidate_set_fraction"],
          dataset: { source: "demo_fixture" },
          compute_estimate: "single host, CPU minutes (fixture harness)",
          risks: ["fixture inputs are synthetic - results do not " +
            "establish real-world performance"] } });
      show(`<div class="alert info">${esc(t("disc.benchDone",
        { id: r.job_id, a: r.approval_id }))}</div>`);
      refreshJobs();
    } else if (d.dact === "compat") {
      const j = await api("GET", `/api/research/${encodeURIComponent(d.rid)}` +
        `/compatibility?${qs}`);
      show(`<div class="card"><b>${esc(t("disc.compatT", { t: j.candidate }))}</b>` +
        `<div class="hint">${esc(j.interface)}</div><pre class="json">${
          esc(JSON.stringify(j, null, 1))}</pre></div>`);
    } else {
      const j = await api("GET", `/api/research/${encodeURIComponent(d.rid)}` +
        `/adoption-plan?${qs}`);
      show(`<div class="card"><b>${esc(t("disc.planT",
          { t: j.candidate, tg: j.target || "" }))}</b>` +
        `<span class="pill danger">${esc(t("disc.notApplied"))}</span>` +
        `<div class="alert info">${esc(j.statement)}</div>` +
        `<ol>${(j.steps || []).map((s) => `<li>${esc(s)}</li>`).join("")}` +
        `</ol><div class="hint">${esc(j.verification_gate)}</div></div>`);
    }
  } catch (e) { toast(t("toast.actFail", { e: e.message }), true); }
}

function reportHtmlLive(rep, rid) {
  // conclusion-first view mirroring the HTML export - assessment,
  // module table, scientific comparison, actions; all technical detail
  // behind collapsed <details>. The report tables mirror the persisted
  // (English) export document; interface chrome here is translated.
  const kv = (o) => `<div class="kv">${Object.entries(o || {}).map(([k, v]) =>
    `<div class="k">${esc(k)}</div><div>${esc(
      typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("")}</div>`;
  const ul = (xs) => `<ul>${(xs || []).map((x) =>
    `<li>${esc(typeof x === "object" ? JSON.stringify(x) : x)}</li>`).join("")}</ul>`;
  const pill = (txt, kind) => `<span class="pill ${kind || ""}">${esc(txt)}</span>`;
  const a = rep.assessment || {};
  const ready = !!rep.decision_ready;
  const mtRows = (rep.module_table || []).map((m) => `<tr>
    <td><b>${esc(m.module)}</b></td>
    <td>${esc(m["current NeoAG method"])}</td>
    <td>${esc(m["best relevant alternative"])}</td>
    <td class="hint">${esc(m.evidence)}</td>
    <td>${esc(m.recommendation)}</td></tr>`).join("");
  const scards = (rep.scientific_comparison || []).map((x) => {
    const firstAlt = (x.rows || [])[0];
    const bl = x.baseline || {};
    const curName = (bl.tools || []).map((o) => o.name).join(", ") ||
      bl.note || "NOT identified";
    const dec = x.decision || {};
    const dKind = dec.recommendation === "Retain current tool" ? "ok"
      : ["Research incomplete", "Insufficient Evidence"].includes(dec.recommendation) ? "danger" : "warn";
    const actions = firstAlt ? `
      <div class="btnrow">
        <button class="btn sm" data-dact="compat" data-rid="${esc(rid || "")}"
          data-tid="${esc(x.target_id)}" data-tool="${esc(firstAlt.tool)}">${esc(t("disc.compat"))}</button>
        <button class="btn sm" data-dact="plan" data-rid="${esc(rid || "")}"
          data-tid="${esc(x.target_id)}" data-tool="${esc(firstAlt.tool)}">${esc(t("disc.plan"))}</button>
        <button class="btn sm" data-dact="bench" data-tid="${esc(x.target_id)}"
          data-tool="${esc(firstAlt.tool)}" data-base="${esc((bl.tools || [])[0] ? (bl.tools[0].name || "") : "")}">${esc(t("disc.bench"))}</button>
        <span class="hint">${esc(t("disc.actNote"))}</span></div>
      <div id="dact-${esc(x.target_id)}"></div>` : "";
    const groupLine = (label, names) => (names || []).length
      ? `<div class="hint"><b>${esc(label)}:</b> ${esc((names || []).join(", "))}</div>` : "";
    return `
    <div class="card"><b>${esc(x.target)}</b>
    ${x.question ? `<div class="alert info"><b>${esc(t("disc.questionL"))}</b> ${esc(x.question)}</div>` : ""}
    <div class="hint"><b>${esc(t("disc.curMethod"))}</b> ${esc(curName)}
      ${bl.consensus ? pill("multi-tool consensus", "ok") : ""}</div>
    ${(x.rows || []).length ? `<div class="tablewrap"><table class="data">
      <thead><tr><th>Alternative (direct)</th><th>Version</th>
      <th>Published performance (context-established)</th>
      <th>License · maintenance</th></tr></thead>
      <tbody>${(x.rows || []).map((r) => `<tr>
        <td><b>${esc(r.tool)}</b><div class="hint">${esc(r.why_comparable || "")}</div></td>
        <td>${esc(r.version)}</td>
        <td>${esc(r.evidence)}</td>
        <td>${esc(r.license)} · ${esc(r.maintenance)}</td></tr>`).join("")}</tbody>
    </table></div>` : `<div class="hint">${esc(t("disc.noDirect"))}</div>`}
    ${groupLine("Complementary methods", x.complementary)}
    ${groupLine("Wrappers / integrations", x.wrappers_integrations)}
    ${groupLine("Related but not comparable", x.related_not_comparable)}
    ${(x.excluded_irrelevant || []).length ? `<div class="hint"><b>Screened out as irrelevant:</b> ${esc((x.excluded_irrelevant || []).slice(0, 6).join(", "))} - reasons in the appendix</div>` : ""}
    ${dec.recommendation ? `<div>${pill(dec.recommendation, dKind)}
      <span class="hint">${esc(dec.rationale || "")}</span></div>` : ""}
    ${dec.action ? `<div class="alert info"><b>Action:</b> ${esc(dec.action)}</div>` : ""}
    <div class="hint">${esc(x.note || "")}</div>${actions}</div>`;
  }).join("");
  const cov = rep.coverage_accounting || {};
  const prov = rep.provenance || {};
  const rows = (rep.comparison || []).map((c) => `
    <tr><td><b>${esc(c.tool)}</b></td><td>${esc(c.target || "")}</td>
    <td>${esc(c.eligibility || "")}</td>
    <td>${esc(c.artifact_class || "unclassified")}</td>
    <td>${esc(c.verified_version || "Not verified")}</td>
    <td>${esc(c.license || "Not verified")}</td>
    <td>${esc(c.maintenance || "Not verified")}</td>
    <td class="hint">${esc(c.performance_summary || "")}</td></tr>`).join("");
  const exRows = (rep.excluded_candidates || []).map((c) => `<tr>
    <td><b>${esc(c.name)}</b></td><td>${esc(c.module || "")}</td>
    <td>${esc(c.eligibility || "")}</td>
    <td class="hint">${esc((c.reasons || []).join("; "))}</td>
    <td class="hint">${esc((c.origin || "").slice(0, 90))}</td></tr>`).join("");
  return `<div class="card">
    <div>${pill(ready ? t("disc.qReady") : t("disc.qPre"),
                ready ? "ok" : "warn")}</div>
    <h3 class="sec">${esc(t("disc.h1"))}</h3>
    <p class="alert info"><b>${esc(a.headline || "")}</b></p>
    <div class="hint">${esc(a.scope || "")}</div>
    ${(a.top_findings || []).length ? `<b>${esc(t("disc.h1"))}</b>${ul(a.top_findings)}` : ""}
    ${(a.evidence_gaps || []).length ? `<b>Evidence gaps</b>${ul(a.evidence_gaps)}` : ""}
    ${a.next_action ? `<div class="alert good"><b>Next recommended action:</b> ${esc(a.next_action)}</div>` : ""}
    <details class="adv"><summary>${esc(t("disc.whatIs"))}</summary>
      <div class="alert info">${esc(rep.disclaimer)}</div></details>
    ${(rep.module_table || []).length ? `<h3 class="sec">${esc(t("disc.h2"))}</h3><div class="tablewrap"><table class="data">
      <thead><tr><th>Module</th><th>Current NeoAG method</th>
      <th>Best relevant alternative</th><th>Evidence</th>
      <th>Recommendation</th></tr></thead>
      <tbody>${mtRows}</tbody></table></div>` : ""}
    ${(rep.scientific_comparison || []).length ? `<h3 class="sec">${esc(t("disc.h3"))}</h3>${scards}` : ""}
    ${(rep.recommended_next_actions || []).length ? `<h3 class="sec">${esc(t("disc.h4"))}</h3>${ul(rep.recommended_next_actions)}` : ""}
    <details class="adv"><summary>${esc(t("disc.h5"))}</summary>
      <b class="hint">${esc(t("disc.exclL"))}</b>
      <div class="tablewrap"><table class="data"><thead><tr><th>Name</th>
      <th>Module</th><th>Class</th><th>Why excluded</th>
      <th>Origin</th></tr></thead><tbody>${exRows ||
        '<tr><td colspan="5" class="hint">none</td></tr>'}</tbody></table></div>
      <b class="hint">${esc(t("disc.fullTbl"))}</b>
      <div class="tablewrap"><table class="data"><thead><tr><th>Tool</th>
      <th>Module</th><th>Eligibility</th><th>Kind</th><th>Version</th>
      <th>License</th><th>Maintenance</th><th>Performance (context gate)</th>
      </tr></thead><tbody>${rows}</tbody></table></div>
      <b class="hint">${esc(t("disc.verChains"))}</b>
      ${(rep.candidates || []).map((c) => `<div class="hint"><b>${esc(c.name)}</b>
        → ${esc(c.verified_version)} · installed: ${esc(c.installed_version || "n/a")}
        <ul>${((c.version_chain || {}).chain || []).map((s) =>
          `<li>${esc(s.step)}: ${esc(s.outcome)}${s.value ? " = " + esc(JSON.stringify(s.value)) : ""}${s.reason ? " (" + esc(s.reason) + ")" : ""} [${esc(String(s.checked_at || "").slice(0, 10))}]</li>`).join("")}</ul>
      </div>`).join("")}
      <b class="hint">${esc(t("disc.coverageL"))}</b>
      ${kv({ completeness: cov.completeness,
        identified_modules: cov.identified_modules,
        researched_modules: cov.researched_modules,
        sufficient_evidence: cov.sufficient_evidence,
        insufficient_evidence: cov.insufficient_evidence,
        not_researched: cov.not_researched, note: cov.note })}
      <b class="hint">${esc(t("disc.gatesL"))}</b>
      <ul>${(rep.quality_gates || []).map((g) => `<li>${g.passed ? "PASS" : "FAIL"} ${esc(g.check)} - ${esc(g.detail)}</li>`).join("")}</ul>
      <b class="hint">${esc(t("disc.citesL"))}</b>
      <ul>${(rep.literature_citations || []).slice(0, 40).map((c) => `
        <li><b>${esc(c.title || "")}</b> ${esc(c.year || "")} -
        ${esc(c.source || "")} ${esc(c.url || c.id || "")}</li>`).join("")}</ul>
      <b class="hint">${esc(t("disc.methL"))}</b>
      <p class="hint">${esc(rep.methodology || "")}</p>
      ${ul(rep.limitations)}
      ${kv({ sources_consulted: prov.sources_consulted,
        query_counts: prov.query_counts, dedup_removed: prov.dedup_removed,
        github_budget: prov.github_budget_at_start,
        failures: prov.failures })}
      <p class="hint">queries persisted: ${(prov.queries || []).length}</p>
    </details>
  </div>`;
}

function reportHtmlExample(rep) {
  const rows = (rep.comparison || []).map((c) => `
    <tr><td><b>${esc(c.tool)}</b> ${esc(c.version || "")}</td>
    <td>${esc(c.availability_class)}</td><td>${esc(c.license)}</td>
    <td>${esc(c.evidence_quality)}</td>
    <td>${esc(c.accuracy_example || "")}</td>
    <td>${c.independently_benchmarked ? "YES" : "no"}</td>
    <td>${esc(c.notes || "")}</td></tr>`).join("");
  return `<div class="card">
    <div class="alert warn">${esc(rep.disclaimer)}</div>
    <div class="tablewrap"><table class="data">
      <thead><tr><th>Tool</th><th>Availability</th><th>License</th>
      <th>Evidence</th><th>Example figures</th><th>Independently benchmarked</th>
      <th>Notes</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="hint">${esc(rep.recommendation.verdict)}</p>
    <p class="hint">Pre-adoption experiments: ${esc(rep.recommendation.experiments_before_adoption.join(" | "))}</p>
    </div>`;
}

async function showReport(id) {
  state.discOpen = id;
  try {
    const r = await api("GET", `/api/research/${id}`);
    if (!r.report) {
      $("res-report").innerHTML = `<div class="card">
        <div class="jr-name">${esc(researchName(r))}</div>
        <div class="jr-sub">${esc(r.id)} · ${resPill(r.status)}</div>
        ${r.progress && r.progress.steps && r.progress.steps.length
          ? `<ul>${r.progress.steps.map((s) =>
            `<li class="hint">${esc(s)}</li>`).join("")}</ul>`
          : `<p class="hint">${esc(t("disc.noProg"))}</p>`}
        ${["running", "queued"].includes(r.status)
          ? `<div class="btnrow"><button class="btn danger sm" id="rep-cancel">${esc(t("disc.cancelBtn"))}</button>
             <span class="hint">${esc(t("disc.cancelNote"))}</span></div>` : ""}
        ${["incomplete", "failed", "cancelled"].includes(r.status)
          ? `<div class="btnrow"><button class="btn sm" id="rep-resume">${esc(t("disc.resumeBtn"))}</button></div>` : ""}
        ${["incomplete", "failed"].includes(r.status)
          ? `<div class="alert warn">${esc(t("disc.partNote"))}</div>` : ""}
      </div>`;
      const cb = $("rep-cancel");
      if (cb) cb.addEventListener("click", async () => {
        try { await api("POST", `/api/research/${id}/cancel`, {}); refreshDiscovery(); }
        catch (e) { toast(t("toast.cancelFail", { e: e.message }), true); } });
      const rb = $("rep-resume");
      if (rb) rb.addEventListener("click", async () => {
        try { await api("POST", `/api/research/${id}/resume`, {}); refreshDiscovery(); }
        catch (e) { toast(t("toast.resumeFail", { e: e.message }), true); } });
      return;
    }
    const rep = r.report;
    const isLive = (rep.engine || "").includes("live") || !!rep.inventory;
    $("res-report").innerHTML =
      `<div class="btnrow" style="justify-content:flex-end"><button class="btn sm ghost" id="rep-rename">${esc(t("dr.rename"))}</button></div>` +
      exportBar(r.id) +
      (isLive ? reportHtmlLive(rep, r.id) : reportHtmlExample(rep));
    document.querySelectorAll("[data-dact]").forEach((b) =>
      b.addEventListener("click", () => discoveryAction(b)));
    $("rep-rename").addEventListener("click", () => {
      const box = $("rep-rename").closest(".btnrow");
      box.innerHTML = `<input id="rep-title" style="max-width:340px"
          value="${esc(r.display_title || researchName(r))}">
        <button class="btn primary sm" id="rep-title-save">${esc(t("dr.renameSave"))}</button>
        <button class="btn ghost sm" id="rep-title-cancel">${esc(t("dr.renameCancel"))}</button>`;
      $("rep-title").focus();
      $("rep-title-cancel").addEventListener("click", () => showReport(id));
      $("rep-title-save").addEventListener("click", async () => {
        const title = $("rep-title").value.trim();
        if (!title) return;
        try {
          await api("POST", `/api/research/${id}/title`, { title });
          toast(t("dr.renamed"));
          await refreshDiscovery();
        } catch (e) { toast(t("toast.renameFail", { e: e.message }), true); }
      });
    });
  } catch (e) { toast(t("disc.reportFail", { e: e.message }), true); }
}

$("res-run").addEventListener("click", async () => {
  const scope = $("res-scope").value;
  const engine = "live";
  const targets = [...document.querySelectorAll("[data-dtarget]:checked")]
    .map((c) => c.dataset.dtarget);
  const custom = $("res-custom").value.trim()
    ? $("res-custom").value.trim().split(/\s*,\s*/) : [];
  if (scope === "targeted" && !targets.length && !custom.length) {
    toast(t("disc.needTarget"), true);
    return;
  }
  try {
    const r = await api("POST", "/api/research",
                        { scope, engine, targets, custom });
    toast(t("disc.started", { id: r.research_id }) +
      (r.unknown_targets && r.unknown_targets.length
        ? t("disc.ignored", { u: r.unknown_targets.join(", ") }) : ""));
    state.discOpen = r.research_id;
    $("res-custom").value = "";
    await refreshDiscovery();
  } catch (e) {
    toast(t("toast.researchFail", { e: (e.data && e.data.error) || e.message }), true);
  }
});
$("res-scope").addEventListener("change", renderDiscoveryControls);

async function refreshDiscovery() {
  try {
    const b = await api("GET", "/api/bootstrap");
    state.boot.research = b.research;
    renderDiscovery();
    if (state.discOpen) showReport(state.discOpen);
  } catch (e) { /* transient */ }
}

/* ---------------- skills (advisory instructions) -----------------------
 * Normal UI = name + instructions only. One Save validates, records a
 * version and applies the skill for new chats; slug/category/frontmatter
 * and deploy are generated or pinned by the backend. Version history and
 * raw text live in a collapsed "Advanced" fold; rollback is there too. */
function stripFrontmatter(txt) {
  const m = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/.exec(txt || "");
  return m ? txt.slice(m[0].length).replace(/^\s+/, "") : (txt || "");
}

function renderSkills() {
  const skills = (state.boot || {}).skills || [];
  $("skill-list").innerHTML = skills.map((s) => `
    <div class="job-row skill-row" data-skill="${esc(s.slug)}">
      <div class="jr-name">${esc(s.title || s.slug)}
        <span class="pill ${s.enabled ? "ok" : "muted"}">${esc(s.enabled ? t("skill.enabled") : t("skill.disabled"))}</span>
        <span class="pill ${s.active_in_new_sessions ? "accent" : "warn"}">${esc(
          !s.enabled ? t("skill.loaded")
          : s.active_in_new_sessions ? t("skill.applies")
          : t("skill.savedNot"))}</span>
      </div>
      <div class="jr-sub hint">${esc(s.description || "")}</div>
      <div class="btnrow">
        <button class="btn sm" data-sedit="${esc(s.slug)}">${esc(t("skill.edit"))}</button>
        <button class="btn sm ${s.enabled ? "danger" : ""}" data-stog="${esc(s.slug)}"
          data-on="${s.enabled ? "0" : "1"}">${esc(s.enabled ? t("skill.disable") : t("skill.enable"))}</button>
      </div>
      <details class="skill-adv">
        <summary>${esc(t("skill.adv"))}</summary>
        <div class="btnrow">
          <button class="btn sm ghost" data-svers="${esc(s.slug)}">${esc(t("sk.hist"))}</button>
          <button class="btn sm ghost" data-sraw="${esc(s.slug)}">${esc(t("sk.raw"))}</button>
        </div>
        <div class="skill-versions hidden" id="sv-${esc(s.slug)}"></div>
        <pre class="skill-raw hidden" id="raw-${esc(s.slug)}"></pre>
      </details>
    </div>`).join("") ||
    `<div class="empty">${esc(t("skill.none"))}</div>`;
  const on = (attr, fn) => document.querySelectorAll(`[data-${attr}]`)
    .forEach((el) => el.addEventListener("click", (ev) => {
      ev.stopPropagation(); fn(el); }));
  on("sedit", (el) => openSkillEditor(el.dataset.sedit));
  on("stog", async (el) => {
    try {
      const on1 = el.dataset.on === "1";
      const r = await api("POST", `/api/skills/${el.dataset.stog}/` +
        (on1 ? "enable" : "disable"), {});
      const st = (r.deploy || {}).state || "";
      toast(`${el.dataset.stog}: ${t(on1 ? "skill.enabled" : "skill.disabled")}` +
        ` - ${st || t(on1 ? "skill.applies" : "skill.loaded")}`);
      await refreshSkills();
    } catch (e) { toast(t("toast.togFail", { e: e.message }), true); }
  });
  on("svers", async (el) => {
    const host = $(`sv-${el.dataset.svers}`);
    if (!host.classList.contains("hidden")) { host.classList.add("hidden"); return; }
    try {
      const d = await api("GET", `/api/skills/${el.dataset.svers}/versions`);
      host.innerHTML = (d.versions || []).map((v) => `
        <div class="tl-row"><span class="tl-ts">${esc(v.created_at || "")}</span>
        <span>v${esc(v.version)} - ${esc(v.source || "")}</span>
        <button class="btn sm ghost" data-srestore="${esc(el.dataset.svers)}"
          data-ver="${esc(v.version)}">${esc(t("sk.restore"))}</button></div>`).join("");
      host.classList.remove("hidden");
      host.querySelectorAll("[data-srestore]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            await api("POST", `/api/skills/${b.dataset.srestore}/restore`,
                      { version: parseInt(b.dataset.ver, 10) });
            toast(t("toast.restoreOk", { v: b.dataset.ver }));
            await refreshSkills();
          } catch (e) { toast(t("toast.restoreFail", { e: e.message }), true); }
        }));
    } catch (e) { toast(t("toast.verFail", { e: e.message }), true); }
  });
  on("sraw", async (el) => {
    const host = $(`raw-${el.dataset.sraw}`);
    if (!host.classList.contains("hidden")) { host.classList.add("hidden"); return; }
    try {
      const d = await api("GET", `/api/skills/${el.dataset.sraw}`);
      host.textContent = d.content;
      host.classList.remove("hidden");
    } catch (e) { toast(t("toast.rawFail", { e: e.message }), true); }
  });
}

async function refreshSkills() {
  try {
    const d = await api("GET", "/api/skills");
    state.boot.skills = d.skills;
    renderSkills();
  } catch (e) { /* transient */ }
}

function openSkillEditor(slug) {
  const host = $("skill-editor");
  const fill = (s) => {
    // inline editing inside the Skills card itself (M10 §15) - no
    // separate page, no modal detour
    host.innerHTML = `<div class="inline-edit">
      <div class="card-head"><h3>${esc(t(s ? "sk.editTitle" : "sk.newTitle"))}</h3></div>
      <label class="lbl">${esc(t("sk.name"))}</label>
      <input id="sk-name" value="${esc(s ? s.title : "")}"
        placeholder="${esc(t("sk.namePh"))}">
      <label class="lbl">${esc(t("sk.body"))}</label>
      <textarea id="sk-body" rows="14" spellcheck="false"
        placeholder="${esc(t("sk.bodyPh"))}">${esc(s ? stripFrontmatter(s.content) : "")}</textarea>
      <div class="btnrow">
        <button class="btn primary sm" id="sk-save">${esc(t("sk.save"))}</button>
        <button class="btn ghost sm" id="sk-cancel">${esc(t("sk.cancel"))}</button>
        <span class="hint">${esc(t("sk.note"))}</span>
      </div>
      <div id="sk-err" class="hint"></div></div>`;
    host.scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("sk-name").focus();
    $("sk-cancel").addEventListener("click", () => { host.innerHTML = ""; });
    $("sk-save").addEventListener("click", async () => {
      const body = { name: $("sk-name").value.trim(),
                     instructions: $("sk-body").value };
      try {
        const r = await api("POST",
                            s ? `/api/skills/${s.slug}/update` : "/api/skills",
                            body);
        const st = (r.deploy || {}).state || "";
        if (r.applied) {
          toast(`${t("sk.note")} (v${r.skill.version})`);
        } else if (st.indexOf("disabled") === 0) {
          toast("saved - but the skill is disabled, so new chats will not " +
                "load it. Press Enable to apply.", true);
        } else {
          toast(t("toast.saveApplyFail",
                  { st, err: (r.deploy || {}).error || "" }), true);
        }
        host.innerHTML = "";
        await refreshSkills();
      } catch (e) {
        $("sk-err").textContent =
          ((e.data && e.data.errors) || [(e.data && e.data.error) || e.message]).join("; ");
      }
    });
  };
  if (!slug) return fill(null);
  api("GET", `/api/skills/${slug}`).then(fill)
    .catch((e) => toast(t("toast.skillLoadFail", { e: e.message }), true));
}
$("skill-new").addEventListener("click", () => openSkillEditor(null));

/* ---------------- help ---------------- */
function renderHelp() {
  const docs = (state.boot || {}).help || [];
  $("help-tabs").innerHTML = docs.map((d, i) =>
    `<button class="chip ${i === 0 ? "active" : ""}" data-doc="${esc(d.name)}">${esc(d.title)}</button>`).join("");
  document.querySelectorAll("[data-doc]").forEach((el) =>
    el.addEventListener("click", () => {
      document.querySelectorAll("[data-doc]").forEach((x) =>
        x.classList.remove("active"));
      el.classList.add("active");
      loadHelp(el.dataset.doc);
    }));
  if (docs.length) loadHelp(docs[0].name);
}
async function loadHelp(name) {
  try {
    const d = await api("GET", `/api/help/${name}`);
    $("help-doc").innerHTML = `<div class="doc-src">${esc(d.source)}</div>`
      + mdToHtml(d.markdown);
  } catch (e) { $("help-doc").innerHTML = `<p class="hint">${esc(t("help.unavail"))}</p>`; }
}

/* ---------------- language switch ---------------- */
$("lang-select").addEventListener("change", function () {
  setLang(this.value);
});

/* ---------------- approvals + decisions (delegated) ---------------- */
document.addEventListener("click", async (ev) => {
  const appr = ev.target.closest("[data-appr]");
  if (appr) {
    try {
      const dec = appr.dataset.dec;
      const body = dec
        ? { decision: dec }
        : { approve: appr.dataset.ok === "1" };
      if (dec === "limit_scope") {
        const s = await uiPrompt({ title: t("attn.limit"),
                                   bodyHtml: esc(t("attn.scopePrompt")) });
        if (!s) return;           // user backed out - card stays pending
        body.scope = s;
      }
      closeDialog();
      await api("POST", `/api/approvals/${appr.dataset.appr}`, body);
    } catch (e) {
      // a card that is no longer pending (cancelled / expired /
      // already decided elsewhere) says so in plain words, and the
      // next queued card takes its place
      if (e.status === 409) toast(t("gate.staleGone"), true);
      else toast(t("toast.apprFail", { e: e.message }), true);
    } finally {
      await refreshJobs(); refreshConv();
    }
    return;
  }
  const rv = ev.target.closest("[data-revoke]");
  if (rv) {
    // scoped grants are revocable, and the revocation is immediate
    // (the file the execution gate reads is rewritten server-side)
    try {
      closeDialog();
      await api("POST", `/api/scopegrants/${rv.dataset.revoke}/revoke`);
      toast(t("toast.revokeOk"));
      await refreshJobs();
    } catch (e) { toast(t("toast.revokeFail", { e: e.message }), true); }
    return;
  }
  const dec = ev.target.closest("[data-dec]");
  if (dec) {
    try {
      closeDialog();
      await api("POST", `/api/decisions/${dec.dataset.dec}/resolve`,
        { choice: dec.dataset.choice, note: t("attn.resolvedNote") });
      await refreshJobs();
    } catch (e) { toast(t("toast.decFail", { e: e.message }), true); }
    return;
  }
  const oj = ev.target.closest("[data-openjob]");
  if (oj) { openJob(oj.dataset.openjob); }
});

boot();
