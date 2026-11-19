# Milestone 11 - root-cause audit and implementation matrix (2026-11-19)

Every "confirmed" line below was observed in the running deployment or
read from the files named (file:line). "Hypothesis" lines are not
treated as facts anywhere in this milestone. Status column: landed /
in-progress / planned; classification column: FIXED (underlying
behavior), MITIGATED (web layer only, root stays), BLOCKED (needs
someone else).

## Part 1 - Phase-0 audit findings

### Environment at audit time
- Repo main = 2833da8, clean tree. Live: web :8712 (watchdog),
  cloudflared -> :8712, CodeBuddy gateway :7890, native task gateways
  :8760 (prod) / :8779 (dev). No test suite occupied 879x.
- Baseline battery launched BEFORE any change (log
  /tmp/m11_battery_baseline.log) as the before-reference.
- Baseline measurements (live, authenticated):
  /api/bootstrap 52.9 ms / 666.0 KiB; the 323-message conversation
  fetches as 294.0 KiB / 7.0 ms; the streaming watchdog refetches it
  whole every 3 s (app.js:1087-1094) and renderChat rebuilds the full
  innerHTML every time (app.js:1330).
- Deployment trap found: start.sh deploys agent_workspace_assets/
  .codebuddy/ into the live workspace, but the repo had NO .codebuddy
  copy - gate.py existed only in the live workspace, so a fresh clone
  would deploy no gate. The live files were vendored back into the
  repo at the start of this milestone (byte-identical, __pycache__
  excluded) before any edit.

### P0-1 chat pollution - CONFIRMED
- Every gateway tool_call / tool_call_update becomes its own persisted
  chat message: agent.py:612-642 (role "tool", text "[running] ..."/
  "[tool failed] ..."). Live DB: 984 of 1136 messages are tool rows;
  the worst conversation is 323 rows = 303 tool + 20 real
  (conv-20261119-115441-05fb29).
- "Real agent started/continued (job ...)" notice on EVERY dispatch
  and resume: agent.py:461-471. Session-restore system notice:
  agent.py:451-460.
- Raw Python dicts surface as chat text: agent.py:900-906 renders
  f"...could not be reached to act on it: {e}" where e is the reply
  result dict; observed verbatim in the saved conversation ("(Approval
  recorded, but ... {'delivered': False, 'saved': True, ...})").
- Frontend grouping (app.js:1169-1210, 1277-1332) is exactly the
  "fold the DOM rows" approach the brief rejects: all rows are still
  stored and still fetched (294 KiB payload).

### P0-2 approvals - CONFIRMED
- Card creation: one approval per distinct op hash, but dedup is the
  in-memory st["ops"] set per turn (agent.py:727-740); after a turn or
  server restart the same blocked command re-presents a fresh card.
- Decision delivery: gate decision replies to the agent session; if the
  session died, the raw dict leaks into chat and the grant is left
  stranded in APPROVALS_DIR until TTL (agent.py:880-906). Observed
  twice in the saved conversation.
- No "always allow within this scope" option exists (approve / reject /
  limit_scope / literature_only only, agent.py:857-879). The only scope
  grant is the dataset-download 72 h scope (agent.py:807-856).
- Expiry is one-way: grant_valid unlinks the grant after TTL
  (vendored gate.py grant_valid), but the card has no expiry state and
  a decision on an expired grant is not reported honestly.
- False positive CONFIRMED: WRITE_BASH_RE ends in >>?\s*["'~/.]
  (vendored gate.py:87-89) with no guard for fd redirects, so
  "ls ... 2>/dev/null" matches "redirect write"; REDIR_RE (gate.py:229)
  then yields /dev/null as a write target, outside ALLOWED_ROOTS, so a
  pure read asks for destructive confirmation (observed as
  "[tool failed] ... NEOAG_CONFIRM_REQUIRED" in the saved transcript).
- Waiting-approval IS a real persisted job state for formal jobs
  (jobs.py:250-253, 372-376, 586-590 "blocked: waiting for approval")
  - that part already meets the brief and must not regress.

### P0-3 long jobs / the 900 s mismatch - CONFIRMED
- Chat turns run as an in-process daemon thread with a wall-clock cap
  TURN_MAX_SECONDS = 900 (agent.py:476, loop 552); at expiry the relay
  posts "No terminal job state seen within 900s; the gateway still
  holds the job. Send another message..." (agent.py:690-698) and
  abandons. The gateway keeps the job; nothing ever reattaches. The
  saved POLYSOLVER research turn ended exactly this way.
- Every 2.5 s tick refetches the ENTIRE transcript (agent.py:556) -
  O(all events) per tick, and a Web restart loses the relay thread and
  its st["seen"] set entirely.
- The 4 states the brief wants separated DO exist but only chat knows
  about two of them: UI request state, gateway agent job
  (conversations.job_id), formal task state (jobs table), scientific
  outcome (jobs.outcome / research).

### P0-4 message integrity - PARTIALLY FIXED already, one gap confirmed
- Loud 413 over MAX_CHAT_CHARS = 100_000 (server.py:62, 974-992); the
  CodeBuddy client raises instead of shortening
  (codebuddy_client.py:84-98). Both prior fixes are present.
- Remaining silent shortening CONFIRMED: store.conv_recent_text clips
  every history line to 200 chars (store.py:407) and that is what the
  session-restore context is built from (agent.py:416-422) - a long
  spec loses most of its text on the first restart, silently.
- No composer draft persistence (only splitter width is saved,
  app.js:1451-1477).

### P0-5 performance - measured above
- Full-conv fetch every 3 s while streaming + full innerHTML rebuild;
  bootstrap payload already 666 KiB and grows with the tables.

### P0-6 scientific selection - CONFIRMED
- The 7 curated science controls are grounded (neoag_params.py:345-404,
  each with a flag + gateway-route citation) and revalidated
  server-side - the taxonomy work keeps these, not invents new ones.
- Planner internals leak into the normal page as plan-step rows inside
  Analysis Options (app.js:2136-2148) and the plan fold; internal ids
  even have zh labels (app.js:2279-2283 "input-detection-and-routing",
  "doctor-preflight", "pipeline-plan").
- DNA-SV is the forbidden negative control "Disable the DNA SV calling
  branch" (neoag_params.py:364-369); identity skip is one plain
  checkbox (370-376).

### P0-7 discovery / readiness - CONFIRMED where tested
- readiness.assess() is honest and live (readiness.py:79-275) but
  merges planner blockers verbatim with no classification: a missing
  server reference, a missing patient input and a dependency of a
  stage the user did not select all land in the same blockers list
  (readiness.py:241-273, _finish 278-296).
- sv_wgs reference requirements are imposed through the planner params
  regardless of the DNA-SV selection (params flow native.py ->
  run_plan); inversion is part of the P0-6 change and must be verified
  here.
- Content-based role detection + structured ambiguity (2833da8) is in
  place and pinned by tests/verify_input_discovery.py; CXLMINI rerun
  is an acceptance step (read-only), and input_inventory.tsv must stay
  unclassified-as-HLA.

### P0-8 navigation / naming - CONFIRMED
- No default "Analysis - YYYY-MM-DD HH:mm" / "Discovery - ..." names
  exist anywhere (grep: zero matches); displayName falls back to
  "<subject> . <type>" (app.js:2480-2484).
- Tasks/Discovery/Results tabs exist (index.html:71-77); the job
  drawer (openJob) still splits state/controls away from Results.

### P1-9 task management - CONFIRMED missing
- jobs has no pinned/locked/trashed columns and no batch endpoints;
  rename exists only for conversations + display_title.

### P1-10 file interaction - CONFIRMED missing
- Zero upload references in backend/server.py and frontend/app.js.
  No attachment model at all.

### P1-11 Discovery science - CONFIRMED with the saved record
- res-20261119-153059-9e95ab (live DB): the Chinese question
  "POLYSOLVER和sovalign的可靠替代" became the label and was embedded
  VERBATIM in all 5 literature queries + the GitHub query; 5x0 records;
  0 candidates; headline "NO tool replacement is justified for any
  researched module" while decision_ready=false and the module verdict
  itself says "Review incomplete - baseline missing".
- Root cause: custom targets fall back to GENERIC_PROFILE
  (science.py:222-226) and literature_queries uses the raw label as
  query term d1 when the profile has no domain vocabulary
  (science.py:948-973); the report headline composes a verdict sentence
  at research.py:1871/1880 regardless of decision_ready.
- GitHub budget was 0 at start (recorded in progress) - unauthenticated
  60/h budget must gate planning, not silently zero the tool phase.

### Hypotheses (NOT treated as facts)
- WHO reaps the gateway worker mid-conversation (idle reclaim vs crash)
  is not established - the saved transcript proves restarts happened,
  not the cause. The durable-reattach design must therefore not assume
  it can prevent session death, only survive it.
- JS main-thread cost is unmeasurable here (no browser automation on
  this host, M9 precedent): frontend performance evidence is served
  markup + payload sizes + row counts + the operator's visual pass.

## Part 2 - requirement -> implementation matrix

Legend: A# = audit finding above. "Test" = automated evidence named.

| req | change (short) | files | test / evidence | class | status |
|-----|----------------|-------|-----------------|-------|--------|
| P0-1 | conversation vs execution split in the DATA model: messages gain visibility ('chat'/'audit'); tool events, kickoffs and integration notices persist as audit rows only; chat GET returns chat rows, ?include=audit for the technical view; migration converts existing tool/kickoff rows; one compact activity line fed by a new /activity endpoint (latest tool title as meaningful label, no call counts) | backend/store.py backend/agent.py backend/server.py frontend/app.js tests/verify_battery.py | battery section "M11 P0-1": polluted conv renders 20 rows, audit view has 323; migration converts 984 rows idempotently | FIXED | planned |
| P0-2 | (a) gate: strip fd redirects (2>/dev/null, 2>&1, &>/dev/null) before write classification - real redirects to real files still confirm; (b) dedup approvals server-side by pending op hash per conversation, one actionable card, rest queued; (c) "always allow within this scope": server-enforced narrow scope grants (class + path root, TTL, revocable, listed + managed in UI, audited), high-risk classes excluded; (d) decisions to a dead session are persisted as pending-delivery and replayed when the session resumes instead of leaking a dict; expired grant says so; (e) keep blocked-waiting-approval job state | agent_workspace_assets/.codebuddy/gate.py backend/agent.py backend/server.py backend/store.py frontend/app.js tests/verify_battery.py | battery section "M11 P0-2": read-only commands with 2>/dev/null pass the gate (unit over gate.decide); duplicate card suppression; scope grant consumed once by identical op + revocable; decision queued when session dead, delivered on resume; expired-grant decision text | FIXED | planned |
| P0-3 | durable relay: no 900 s wall cap - turn threads poll with backoff while the gateway reports the job alive; startup reconciliation reattaches relays for every conversation whose job is alive (seen-keys rebuilt from persisted audit rows); transcript fetch incremental by event cursor; no "send another message to discover completion" anywhere; Stop/cancel stays honest-terminal | backend/agent.py backend/store.py backend/server.py tests/verify_battery.py tests/verify_real_agent.py | battery: a >TURN budget mock job still delivers its terminal reply into chat after restart of the relay (simulated by direct reconciliation call); real-agent suite covers reattach on :8799 | FIXED | planned |
| P0-4 | honest context reconstruction: full-text recent turns up to a byte budget with an explicit omission marker + file-backed full transcript written into the agent workspace (.neoag/chat-history/<conv>.md, path given to the agent, retrieval via taskquery.py); per-conversation composer drafts (localStorage) | backend/store.py backend/agent.py frontend/app.js tests/verify_battery.py | long_message suite (8797) extended: multi-section spec survives dispatch, delivery, simulated restart, recovery byte-exact; battery checks omission marker present, never a silent 200-char clip | FIXED | planned |
| P0-5 | chat fetch returns chat rows only (small); conversation fetch supports after-id pagination + If-None-Match style hash skip when unchanged; watchdog refetch backs off; renderChat paints the newest window and prepends older on demand; bootstrap trims to summary fields; SSE fanout debounced (single in-flight guard per refresher) | backend/server.py backend/store.py frontend/app.js | before/after table in this doc: same 323-row conv, refetch payload + rows rendered + bootstrap size; measured with the same curl harness as Part 1 | FIXED (server) / MITIGATED (browser side bounded by no-automation constraint) | planned |
| P0-6 | run-direction rows replacing negative toggles: skip_dna_sv is "DNA structural-variant branch" OFF by default HERE (web_run_default False; the payload states the bool explicitly because native's default is the OPPOSITE - silence would flip what the UI shows); skip_bam_matcher is a guarded run row that RUNS by default and is accepted by the BACKEND only with a separate identity_verified_elsewhere confirmation (kept with the job, never forwarded to a native command); affects-text states DNA_SV_UNASSESSED and the RNA independence in both languages; the raw planner dump (plan-block) is gated behind the technical view. Native tree untouched. | backend/neoag_params.py backend/native.py frontend/app.js tests/verify_m11_p06.py | verify_m11_p06 19/19 on :8788: flag semantics via build_plan_argv; deployed-generator structural pins (skip flag gates ONLY the dna_sv stage; fusion stage gated on fusion inputs alone); REAL native planner A/B on read-only CXLMINI (SV-off plan PASSes, fusion+splice stay READY, identical to SV-on); live API 400-without-ack / 202-with-ack | FIXED | LANDED faed919 |
| P0-7 | discovery states HOW each role was established (role_basis): VCF by header content (SVTYPE/ALT), tables by header groups, HLA by allele pattern, BAM cross-checked against its OWN @RG samples read from the BGZF header (stdlib) or honestly labelled "filename token" (garbage-named BAMs stay UNCLASSIFIED, never guessed); readiness/discovery blockers carry one of six kinds (missing_patient_input / missing_server_reference / ambiguous / unsupported_format / missing_tool / unselected_stage_dependency) evaluated under the EFFECTIVE workflow: with DNA-SV off the sv_wgs reference_fasta/gencode_gtf become advisories, ticking the branch makes them real blockers again; human sample picks validated against the somatic VCF genotype columns land in the confirmed manifest (previously silently dropped); raw native view preserved verbatim under the technical view; kind + basis chips bilingual | backend/native.py backend/readiness.py frontend/app.js frontend/styles.css tests/verify_m11_p07.py | verify_m11_p07 21/21 on :8786: real read-only CXLMINI discovery (fusion/splice READY, sv server-refs = advisories, ambiguous tumor_sample_id over the real genotype columns); garbage-name copy proof of content classification; @RG content evidence surfaced; pick round-trip + bogus-pick 400; nohla => missing_patient_input, zero-byte VCF => unsupported_format; readiness checks kind-tagged | FIXED | LANDED 5b354bf |
| P0-8 | default names set AT CREATION in the store: "Analysis - YYYY-MM-DD HH:MM" for jobs (kind-prefixed: Discovery/Diagnostic), "Discovery - <same>" for research rows (server clock = display tz; ids, sample_id, spec and audit untouched; renames overwrite display_title only); clicking ANY task row - job or research - opens Results with that task selected (openJob is a synchronous select-and-jump); the job drawer is DELETED: identity, rename, state pair, progress, pause/resume/cancel and the tech record (requested config, native execution + run plan, decisions, snapshot, events) render inline in Results for EVERY state, incl. failed with the confirmed reason + partial outputs (P0-4 failure card) - research rows get a pointer card with an explicit open-in-Discovery action; selection survives reload (session storage) and a render-sequence guard kills stale fills; Task history was already a collapsed <details> | backend/store.py frontend/app.js frontend/index.html frontend/styles.css tests/verify_m11_p08.py | verify_m11_p08 28/28 on :8787: name regex at API + store level (all four kind prefixes), rename spec_json byte-identical + audited (direct DB read), empty 400 / unknown 404, full mock lifecycle keeps the name, events/tasks/snapshot answer per state, research naming through the ordinary submit+rename path; frontend contracts (zero jd-*, zero state.jobDetail, no drawer CSS, both i18n dicts); adjacent re-runs green: p07 21/21, m7 ALL PASS, input_discovery 17/0, m10 ALL PASS | FIXED | LANDED e169148 |
| P1-9 | jobs gain pinned/locked/trashed/trashed_at (migration-safe, exposed on every task row); ONE rule shared by single and bulk endpoints (bulk is never the laxer door): trash refused while ACTIVE (cancel-first validated) and while LOCKED, bulk answers one honest line per id; trash is RECORD-level (leaves listings, GET /api/jobs?trashed=1 with stamp, restore intact); purge deletes RECORDS only behind a TYPED-id confirmation and never touches files - the shared-artifact checksum probe proves bytes survive and the other job's reference keeps working; purged record honestly 404s; research ids refused by the flag endpoint; every change audited, purge audited with snapshot + files_touched=false, audit/approval rows never purged; retention is a display (trash_retention_days in the settings whitelist) - removal always manual; UI: search + sort (pinned first in every order), trash view, checkbox bulk bar, Results-head record controls with the WHY in the disabled title | backend/store.py backend/server.py frontend/app.js frontend/index.html frontend/styles.css tests/verify_m11_p09.py | verify_m11_p09 26/26 on :8785: lock blocks individual AND bulk delete; shared-artifact job delete keeps the file (sha256 identical); restore works; results 404 after purge; active/locked purge refused; typed-confirm required; retention persists; direct-DB audit assertions; UI + bilingual contracts | FIXED | LANDED c429ba5 |
| P1-10 | two intake paths: (1) browser upload of document/report/table/image types with size caps, progress, cancel, stable attachment ids, metadata, conversation association; (2) picker over authorized server roots for existing files. Model: attachments are workspace files whose PATH the agent may read; egress audit records exactly what leaves (no raw upload forwarding); BAM/FASTQ/WGS stay on formal ingestion | backend/server.py backend/store.py frontend/app.js tests/verify_battery.py | battery "M11 P1-10": upload round-trip byte-exact; oversize rejected loudly; disallowed type rejected; chat with attachment shows path, gateway payload text contains path not file bytes | FIXED (web feature) | planned |
| P1-11 | intent layer before retrieval: mixed-language question => parsed into modality, candidate baseline names (fuzzy vs verified lexicon, e.g. sovalign handled as an UNVERIFIED name that must be disambiguated, never silently conflated), English query terms + synonyms; per-tool separation grounded in the native inventory; verbatim-NL-as-query removed; zero-result => bounded broadening then honest "Research could not be completed; no comparison was possible" headline; data-integrity gates separated from scientific-adequacy gates (no vacuous PASS rows - they report N/A); licensing vocabulary ladder enforced; GitHub budget checked BEFORE planning and recorded | backend/science.py backend/research.py backend/identity.py new backend/intent.py tests/verify_discovery.py tests/verify_battery.py | discovery suite on :8797 replays the exact regression question and asserts: queries contain resolved English terms, target split per tool, zero-result outcome = research-incomplete, headline never claims "no replacement justified"; generalization cases (HLA, CNV, fusion, splicing, presentation) with no POLYSOLVER keyword in code (grep-enforced) | FIXED | planned |
| P1-12 | humanize every integration/terminal notice (no raw dicts anywhere - battery greps stored agent rows), execution vs scientific status separated in one shared phrasing helper used by Chat/Tasks/Discovery/Results; no resume promises not implemented | backend/agent.py backend/jobs.py backend/research.py frontend/app.js frontend/index.html | battery greps stored messages for {, dict patterns, "gateway detail" leaking internals; zh/en key equality stays enforced | FIXED | planned |
| P1-13 | config audit pass: every control keeps its verified meaning citation; genome build choice validated against installed reference assets; auto-derived settings (expression backend defaults etc.) get NO duplicate knob; dev-only flags never rendered outside dev mode | backend/neoag_params.py backend/readiness.py frontend/app.js | battery: params payload contains only curated controls; every control row carries evidence string; dev flags absent from /api/params | FIXED | planned |
| P2-14 | visual pass after P0: alert/approval-card overlap fix, zh typography, focus states, consistent modals, internal badges removed from default view (consequence of P0-1), responsive checks at narrow widths | frontend/styles.css frontend/index.html frontend/app.js | served markup probes + operator visual pass (stated method - no browser automation on this host) | MITIGATED (visual is by nature the surface) | planned |

### Sequencing (each step lands as its own commit, battery green before
### the next; suites on shared test ports never run concurrently):
1. gate vendoring (done) + gate false-positive fix + gate unit test.
2. P0-1 data model + migration + rendering + activity line.
3. P0-3 durable relay + reconciliation.
4. P0-2 approvals (dedup/queue, scope grants, delivery queue, expiry).
5. P0-4 context reconstruction + drafts.
6. P0-6 selection inversion + taxonomy display + internals removal.
7. P0-7 readiness classification + CXLMINI verification.
8. P0-8 navigation/naming/results merge.
9. P0-5 performance (server + render windowing) with measured table.
10. P1-9, P1-10, P1-11, P1-12+13, P2-14.
11. Scenarios A-K + all suites + this doc's status/class columns
    finalized + push + SHA verified + live restart.
