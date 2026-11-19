# Message-integrity and gate-classification defects — 2026-11-19

Two independent defects found while verifying whether a long
specification prompt reached the agent. Both are recorded with the
evidence that established them.

## D1 — silent truncation of user prompts (FIXED)

**Symptom.** An 18-section specification prompt was reported as sent,
was stored, and the agent worked from it — but the last several sections
were never delivered. The stored text ended mid-word
(`...grouped by spacing rather than arbitrary bor`).

**Root cause chain (three independent silent cuts).**

| # | Site | Code | Effect |
|---|------|------|--------|
| 1 | `backend/server.py` `/api/chat` | `text = str(body.get("text",""))[:4000]` | Any prompt over 4 000 characters was cut before persistence, before the audit entry and before dispatch. The client still received HTTP 200. The true submitted length became unrecoverable, because the cut precedes the first persistence point. |
| 2 | `backend/codebuddy_client.py` `dispatch()` | `{"prompt": prompt[:16000]}` | The dispatch prompt is `CHAT_PREAMBLE + rolling transcript + user message`, i.e. the user's message is LAST. Once the combined string passed 16 000, the slice removed the **tail of the user's own message** — a second, larger-cap version of the same failure, latent at the 4 000 cap but guaranteed to fire once #1 was raised. |
| 3 | `backend/codebuddy_client.py` `reply()` | `{"text": text[:16000]}` | Same class of cut on the multi-turn path. |

**Fix.**
- `MAX_CHAT_CHARS = 100_000` in `backend/server.py`, counted in Unicode
  **characters, not bytes**, so Chinese prompts get the same allowance.
  Over-limit is rejected with **HTTP 413** carrying `error`, `limit`,
  `actual` and a human-readable `detail`, plus a
  `chat.rejected_too_long` audit record. Nothing is persisted or
  dispatched on that path.
- `MAX_PROMPT_CHARS = 200_000` plus `within_limit()` in
  `backend/codebuddy_client.py`: returns the identical string or raises
  `CodeBuddyError`. It never returns a shortened copy. It is kept
  strictly above `MAX_CHAT_CHARS` + preamble + transcript, so the only
  limit a user can reach is the one that reports itself properly.
- `frontend/app.js` chat submit: a failed send now restores the draft
  (previously the textarea was cleared before the request, so a rejected
  long message was lost from the UI too) and shows the server's `detail`
  instead of a bare `HTTP 413`.

**Known remaining limit (unchanged, by design).**
`store.conv_recent_text()` builds the rolling transcript with
`text[:200]` per line. A fresh agent session therefore sees only the
first 200 characters of each earlier turn. This is what produced the
cut transcript line in the 2026-11-19 conversation. It bounds history
summarisation, not the current message — the current message is now
delivered whole — but any long instruction that must survive a session
re-dispatch should be written to a file and referenced by path.
Left as-is deliberately: raising it re-opens unbounded prompt growth.
Needs an explicit Milestone decision.

## D2 — gate false positive on read-only commands (OPEN, not worked around)

**Symptom.** A read-only command was classified as
`writes, overwrites or moves files into a location outside the
authorized job workspace`, and the reported approval did not unlock
`op=a14e5e41ba34bc151f84440d`.

**Reproducer (deterministic, minimal).**

```
ls <any-directory> 2>/dev/null | head -5      -> NEOAG_CONFIRM_REQUIRED
ls <any-directory> 2>&1 | head -5             -> passes
```

`ls`, `grep`, `head` and `tail` are irrelevant to the verdict. The only
variable is the stderr redirect target: a **file descriptor** (`2>&1`)
passes, a **filesystem path** (`2>/dev/null`) is flagged as an
out-of-workspace write. `/dev/null` is not on an allow-list, so any
command that suppresses stderr — an extremely common read-only idiom —
is misclassified as destructive.

**Consequence.** Approval cards get raised for operations that change
nothing. Because a card is the only path past the gate, benign
diagnostics become blocked or burn a human approval.

**On the unlock failure.** Op ids are per command string: a trivially
different command produced a different op hash
(`8c5f50b9c0ae5bc639ae05c9`), which is the documented hash-matched
single-use grant behaviour. Retrying the byte-identical command
re-reported the *same* op hash and was still denied, so the grant was
absent rather than mismatched. `codebuddy_client.py` documents the
protocol as "approving writes a single-use, hash-matched grant that
unlocks exactly one identical retry"; the observed outcome is that no
such grant existed for the approved operation, which points at the card
that was approved not being bound to that op id.

**Investigation limits.** `.codebuddy/gate.py`, the approval store and
session logs are self-protected: the gate denies reading its own
approval mechanism and states there is no approval path. This diagnosis
is therefore **black-box only** — reproducer plus documented behaviour.
No gate rule was loosened, no approval was self-issued, and the flagged
commands were not re-attempted through alternatives.

**Suggested remedy (for the owner, not applied here).** Allow-list
redirect targets that cannot persist data (`/dev/null`, `/dev/stdout`,
`/dev/stderr`), or exclude stderr-only redirects (`2>`) from the
write-detection rule while keeping stdout redirects (`>`, `>>`)
classified. Additionally: make the card display the `op=` hash it
authorises, so a mismatch between approved card and retried operation
is visible instead of silent.

## Verification

`tests/verify_long_message.py` — isolated server on an OS-allocated free
port, fresh database, no gateway contact: **28 passed, 0 failed,
1 skipped** (the skip is the real Milestone 10 specification round-trip,
which needs the specification re-supplied via
`NEOAG_LONGMSG_FIXTURE=/path/to/spec.txt`; the battery never
reconstructs prompt text).

`tests/verify_battery.py` — **143 passed, 0 failed** after the change.

The battery also exposed a harness hazard: `verify_battery.py` pins port
8798 and an orphaned pre-fix server on the new battery's first fixed
port caused it to test stale code and report false failures.
`verify_long_message.py` now allocates a free port and refuses to run if
the answering server is not the one it spawned. Other fixed-port
batteries share this hazard.

## Milestone 11 backlog (unresolved — recorded, not fixed here)

### M11-1  Gate approval misclassification (OPEN)
The `2>/dev/null` false positive in D2 is unfixed: any read-only command
that suppresses stderr raises a confirmation card. In addition, an
approved card did **not** unlock the operation it named
(`op=a14e5e41ba34bc151f84440d`), and the grant was **absent** rather
than mismatched, since retrying the byte-identical command re-reported
the same op id and was still denied. Neither the gate source
(`.codebuddy/gate.py`) nor its approval store can be read — they are
self-protected with no approval path — so this needs an owner-side fix
with visibility into the grant store. Two concrete asks: allow-list
non-persistent redirect targets, and print the authorised `op=` hash on
the card so a card/operation mismatch is visible instead of silent.

### M11-2  200-character history truncation (OPEN)
`store.conv_recent_text()` caps every transcript line at `text[:200]`.
A fresh agent session therefore sees only the first 200 characters of
each earlier turn, so a long instruction silently disappears on session
re-dispatch even though it is stored in full. D1 fixed delivery of the
**current** message; this is the remaining path by which the agent loses
instructions the user already sent. Options to weigh: raise the cap, or
make it proportional and explicit, or persist long prompts to a file and
hand the session a path. Any change re-opens prompt growth, so it needs
a deliberate budget rather than a bigger constant.

### M11-3  Documented restart "reattach" does not exist (NEW, doc-vs-code)
`help_docs.py:144-149` states: *"While the job is alive a backend
restart reattaches (same session, no new dispatch)."* No such code path
exists. `reattach` appears nowhere in the backend except that help text.
The relay is `threading.Thread(target=self._turn, ...)` started only at
`agent.py:441` when a message is dispatched, and `main()`
(`server.py:1515-1539`) does the opposite of reattaching at startup:
`store.msg_sweep_stale_streaming()` (`:1519`) actively marks surviving
`streaming` messages **interrupted**. A restart during an in-flight
turn therefore abandons that turn's delivery: the gateway job keeps
running and its transcript is durable, but nothing relays the remainder
into the chat until the next user message takes the session-restore
path. Either implement the documented reattach (scan conversations whose
job is still alive and re-arm `_turn`) or correct the help text.

