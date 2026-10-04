# v2 plan, acceptance contract, and coordination docs

Base: worktree `/Users/maxghenis/subfleet-v2-lanes/desktop-workspace`, branch `feat/desktop-workspace` at `3f155e5`. `git status` is clean. All line numbers below refer to this worktree unless another path is given.

## 1. Clause numbering and how tests cite clauses

**Scheme**
- Every clause is a bullet that starts `- **C-<section>.<n>**`.
- The header says tests cite clauses by number, for example `C-5.4` (`docs/acceptance-contract.md:3`).
- Sections are `## N. Title`, numbered 1 to 23 with no gaps. The headings are at lines 22, 32, 39, 47, 65, 78, 93, 100, 107, 118, 129, 138, 151, 158, 165, 172, 179, 189, 194, 198, 206, 214 and 218.
- **The highest section is 23**, "Carried-forward invariants" (line 218). The highest clause is **C-23.55** (line 276). There are 170 clause markers in total. Sections 21 and 22 contain no bullets: 21 is a milestone table and 22 is prose.

**Numbering quirks (checked with grep)**
- C-5.9 appears before C-5.8.
- C-9.9 sits under the `## 10. Lanes and credentials` heading.
- C-11.7 and C-11.7a sit under `## 12. Adapters` (line 138).
- So a clause's position in the file does not tell you its section.
- Inserted clauses use a letter suffix. **C-11.7a** (2026-09-20) is the only one.
- Caveat: `tests/unit/test_invariants_index.py:96` parses clause ids with `\*\*(C-\d+\.\d+)\*\*`, and `CLAUSE_RE` at line 69 is `^C-\d+\.\d+$`. Neither matches a suffixed id. This only matters if `invariants.json` ever cites one.

**Change rules**
- Line 3: "A clause changes only by editing this file in the same commit as the code and tests that depend on it. Where this file and `plan.md` disagree, this file wins."
- Line 7, `## Changes in version 2`, works as a running changelog. Each bullet names clause ids, a date and time, and the incident or report. Examples: the C-11.7a bullet dated 2026-09-20 and the C-6.12/C-11.2 bullet dated 2026-09-22 (line 20).
- The header line was not updated as clauses were added. It still says "Version 2, 2026-09-05" and "binds milestones 1 to 3 of `plan.md`" (line 3), even though later clauses bind milestones 4 to 8.
- Section 23 allows clauses to land before their code: "A clause here binds like any other; the milestone names when it is first tested" (line 220).
- C-6.5 (line 84): "This list is the index of refusals: a new refusal is added here, not in a clause of its own."
- Section 21 (lines 206–212) is a milestone table that names acceptance tests. Plan amendment 9 (`plan.md:17`) requires every milestone row to carry an acceptance test name.

**How tests cite clauses (C-20.5, line 204)**
The rule is "Every test names the clause it proves in its docstring." Observed patterns:
- Single clause: `tests/unit/test_salvage.py:33` `"""C-13.1 temporary-index salvage leaves HEAD, real index and files untouched."""`
- Section level in a module docstring: `tests/unit/test_salvage.py:1` `"""C-13 salvage snapshots are private git objects..."""`
- Several clauses, with a colon: `tests/unit/test_timers_alerts.py:81` `"""C-18.1, C-23.27: imported v1 alert-latch events..."""`
- Suffixed clause: `tests/fake/test_admission_route_isolation.py:482` `"""C-11.7a an authorization names the lane id..."""`
- Swift frontend: `tests/frontend/test_status_model.py:165` `"""C-17.7, C-18.2 the Swift model keeps a batch together..."""`
- A module header line, `Every test names the clause it proves (C-20.5).`, at `tests/unit/test_daemon_verbs.py:3`, `test_hooks.py:3`, `test_sessions_revive.py:3` and others.
- Inline comments: `tests/unit/conftest.py:20` `# C-2.4`, `tests/conftest.py:123` `# C-10.6`.

**Enforcement.** I found no test that enforces C-20.5. `grep -rn C-20.5 tests` returns only module docstrings and `test_invariants_index.py:207`, which checks `invariants.json` citations. Sixteen `test_*.py` files contain no `C-` citation at all, including:
- `tests/frontend/test_menu_view.py`
- `tests/unit/test_native_operations.py`
- `tests/unit/test_boot_identity.py`
- `tests/fake/test_reserve_authorization.py`
- `tests/e2e/test_http_isolation.py`

**Citation counts** (grep across `tests/`): C-17.1 is cited 85 times, C-17.3 84, C-10.6 66, C-9.1 58.

**Commit subjects also cite clauses.** Examples: `b841d0d Admission: one unroutable job never stops the pass; pins are lane ids (C-6.12, C-11.2)`, `2928247 Menu bar app: show jobs, grouped by batch, from a jobs section in status.json (C-18.2)`.

## 2. Existing clauses on sessions, notices, the app, status.json and interactive use

No clause covers a conversation, a message submission, an approval response, attachments, an events cursor or a desktop main window. I grepped the contract for cockpit, conversation, interactive, sidebar and Traycer; the only hits are the related clauses below.

| Clause (line) | What it says | Consequence for the desktop workspace |
|---|---|---|
| C-2.2, C-2.3 (35–36) | Lists what the state root contains. Job files are 0600 and directories 0700. | New conversation or attachment stores must be added to C-2.2 and follow C-2.3's permissions. |
| C-3.2 (42) | Every state change is one transaction that also inserts an `events` row. | Applies to message and approval rows. |
| **C-3.4** (44) | "Readers other than the daemon (the CLI in offline mode, the menu bar app, Logpile) open the database read-only. Only the daemon and the guardian receipt path write." | The app stays a client. Every mutation goes through a daemon op. |
| C-1.5, C-6.2 (28, 81) | A request id is at most 128 characters. The payload digest is SHA-256 over canonical JSON. The same id with a different digest is exit 2. | Direct precedent for a client message UUID plus a payload digest. |
| **C-4.1** (49) | `wait_reason` already includes **`approval`**. C-6.11 (90) treats `approval` as "a person's or a retry's to end" and leaves it out of `admission.pending`. | An approval-needed state already has a slot in the job model. I did not check whether any code sets it (UNVERIFIED). |
| C-4.6 (63) | An attempt never changes lane or model. | Changing the model in a follow-up means a new attempt. |
| C-5.1 (67), C-12.1 (142), C-12.4 (145) | One guardian per attempt. The adapter interface is one-shot: `build_launch`, `classify`, `deliverable`, `resume_launch`. The Claude resume launch is `claude -p --resume`. | There is no persistent-transport or per-turn receipt method. A warm worker needs a new adapter surface. |
| **C-6.5** (84) | Refuses a second live *instance* of a session. A revive is always another instance. This clause is the index of refusals. | New refusals (a second writer for a native session, a stale approval reply) must be added to this list. |
| **C-10.3** (123), C-11.1 (131) | The desktop login's lane is never a candidate without `allow_desktop`. Policy sets `desktop_login: "never"`. `plan.md:33` open decision 2 records this as a standing order. | Continuing a native session that lives under the desktop login's credential conflicts with this unless the new section rules on it explicitly. |
| C-15.1–C-15.4 (167–170) | Terminal jobs write a notice for the caller session. Notice states are `pending`, `offered`, `acknowledged`, `surfaced`. `wait` is a server-side long poll of at most 60 s and "never targets a lane session". | The long-poll pattern fits a bounded events cursor. The notice states are not message delivery states. |
| **C-16.1–C-16.4** (174–177) | Protocol v1. C-16.2 lists 12 ops, and "unknown fields are ignored". | **C-16.2 no longer matches the code.** `subfleet/protocol.py:18-23` defines 19 ops; the extra ones are `notice.mark`, `gate.start`, `gate.poll`, `gate.continue`, `sessions`, `pick` and `operations`. `protocol.py:289-290` rejects any `v` other than 1. Because unknown fields are ignored (also `protocol.py:59`), an older daemon would **silently drop** a new field such as a Fast setting. |
| **C-17.1** (181) | `sessions [list\|continue\|revive\|mirror]` and `handoff` are permanent verbs. | `docs/lanes/sessions.md:14` maps v1 tickle, muster and revive onto `sessions continue --scope interrupted\|idle\|cold`, which is interruption recovery. The legacy cockpit used `sessions continue claude:SESSION_UUID --stdin --queue --message-id` (legacy `subfleet/docs/interactive-delivery.md:67`). Same spelling, incompatible meaning. |
| C-17.6 (186) | `run` inside a Claude Code session defaults to detached. | — |
| **C-18.1, C-18.2** (191–192) | `status.json` is published "for the menu bar app every probe cycle", with a `jobs` section. The app decodes a snapshot with no `jobs` key, "so an app newer than its daemon still works". The snapshot is one probe cycle old; `subfleet runs` is the live ledger. | Precedent for forward compatibility. Snapshot freshness is not a live stream. |
| C-20.1 (200) | Test layout is unit, fake, process and live. | `tests/frontend/` exists but is not in C-20.1. It compiles `app/SubfleetApp.swift` with `-D SUBFLEET_MODEL_TEST` (`tests/frontend/test_status_model.py:21-29`) and is skipped outside macOS. |
| C-23.14, C-23.36 (235, 257) | A handoff is scrubbed, bounded per section, and records the absolute path of the source transcript. | Applies to cross-provider handoff provenance. |
| C-23.30, C-23.31, C-23.33–C-23.35, C-23.39, C-23.55 (251–276) | Session registry ranking. A headless lane run is not a session. Nudge rules. Revive admits only `bypassPermissions` sessions. The revive lease is `session:<id>:revive`. | This is session maintenance, not conversation. |
| C-23.42, C-23.49, C-23.50 | Notice envelope rules. | — |
| **C-23.54** (275) | "Every provider launch is a `subfleet run` submission, including subfleet's own"; the session hook blocks direct provider CLIs. | A warm conversation worker is a provider launch outside submit. The new section must either route every turn through submit or explicitly amend C-23.54. |

**Plan-level "interactive" references.** Amendment 11 says "No interactive allowance is needed for one user" (`plan.md:19`), and "An interactive-capacity allowance (one user)" appears under "What is not adopted" (`plan.md:29`). Interactive turns therefore get no priority lane under the current plan. Any latency target (the transition plan suggests cached acknowledgement p95 ≤ 250 ms) has to be met without an admission bypass, or this amendment has to be revisited explicitly.

**The v2 app today.** `app/Info.plist:13` sets `LSUIElement` to true, so it is menu-bar only with no Dock icon. `app/SubfleetApp.swift:618` uses `MenuBarExtra`. `app/build.sh:24-25,32-33` refuses to build into `/Applications`.

## 3. The cockpit exclusion: where it is and what an amendment should reference

**Plan text.** I verified this is identical in the main checkout: `diff` of lines 380–405 matches `/Users/maxghenis/subfleet-v2/docs/plan-b-rev4.md`.
- `docs/plan-b-rev4.md:382`: `## Traycer, Logpile, the public repo, and the app`
- `docs/plan-b-rev4.md:384`: the Traycer rationale. Traycer's Host "is a signed binary talking to Traycer's production cloud … contradicts local, honest, subscription-only operation." This objection is about Traycer only.
- **`docs/plan-b-rev4.md:387`**: "- **Menu bar app**: keep the current single-file Swift app, pointed at a `status.json` the daemon writes every probe cycle; the cockpit branch is not carried."
- **`docs/plan-b-rev4.md:403`**: "| The Traycer bridge and cockpit branch | see above; the events spool survives |" (in "What gets dropped", which starts at line 389).
- `docs/plan-b-rev4.md:465`: open decision 5, "Traycer. Recommendation: events spool only." This is unaffected.

**Migration and importer (the exclusion is also in code).**
- `docs/migration.md:48`: "| `S/cockpit-client/pending-messages.json` | Cockpit branch client state | drop | the cockpit branch is not carried |"
- `subfleet/importer.py:150`: `ManifestRow("cockpit", "drop", 0, "the cockpit branch is not carried", ("cockpit-client",))`
- `docs/migration.md:49` drops `S/composer-attachments/`, which was empty at the 2026-09-05 listing.
- `docs/migration.md:50` drops `S/broker.lock` and `S/broker.sock`.

**A related classification that needs a ruling.**
- `docs/migration.md:33` classifies `S/outbox.sqlite3` as "The notice outbox for socket pushes" and imports it as `notices` rows (non-delivered rows become `offered`).
- `subfleet/importer.py:1530-1545` implements this. Its docstring says "The live outbox holds session continuations, which name a session and no run."
- The legacy cockpit doc (`…/subfleet-traycer-port/subfleet/docs/interactive-delivery.md:56-59`) says the broker's pending conversation deliveries live in `outbox.sqlite3` under that same state root.
- So the existing importer maps what appear to be cockpit message deliveries onto notices. Stage 4 of the transition plan instead requires each message to be classified as terminal, not dispatched, or ambiguous. Whether the importer ran against live cockpit rows at cutover is UNVERIFIED.

**The contract and plan.md.** No contract clause mentions the cockpit. The menu-only scope shows up only indirectly, through C-3.4 ("the menu bar app" is a read-only reader) and C-18.1/C-18.2. `plan.md` does not mention the cockpit. `plan.md:42` (the 2026-09-19 execution decision) says to verify "the retained native menu bar app", and `docs/release-gates.md:22` describes "The retained Swift menu app".

**What the superseding amendment should do**
- **Where it goes:** a new dated section in `docs/plan.md`, in the same style as `## Execution decision, 2026-09-19` (line 35) through `## A stalled queue that said nothing, 2026-09-20` (line 98). Each of those sections quotes Max, names the clauses and sections it amends, and gives the reason. Since 2026-09-06 all amendments have used dated prose sections rather than new rows in the amendments table (rows 1–17). Following the transition plan (`subfleet-desktop-transition-20260924.md:68`), do not edit `plan-b-rev4.md` itself; `plan.md:3` already says the plan of record is plan B "as amended below".
- **What it supersedes:** only the cockpit half of `plan-b-rev4.md:387` and the words "and cockpit branch" in `:403`. The Traycer exclusion at `:384` and `:403` and open decision 5 stay.
- **Milestone:** define a new milestone. Plan B's build sequence ends at milestone 8 (`plan-b-rev4.md:430-442`), so the desktop workspace would be milestone 9, possibly with the transition plan's stages as sub-steps.
- **Migration:** re-open `docs/migration.md:48–50` and `importer.py:150`. Change `cockpit-client` from drop to inventory/retain, and re-examine the outbox classification at `docs/migration.md:33` and `importer.py:1530`.
- **Contract clauses it touches:** C-3.4, C-10.3/C-11.1, C-16.2, C-17.1 (the meaning of `sessions continue`), C-23.54, C-6.5, C-20.1, the C-21 table, and the header at line 3 ("binds milestones 1 to 3").
- **Decision record:** optionally `docs/decisions/2026-09-24-desktop-workspace.md`. The only existing decision file, `docs/decisions/2026-09-05-cutover-prerequisites.md`, uses a dated title with a revision number, one numbered `## N.` per decision with "Facts." and "Decision." paragraphs, and peer-round sections.

## 4. Multi-agent coordination conventions

**`docs/coordination.md`: the shadow-week ledger, started 2026-09-06**
- The integrator is "the Fable session named `maxghenis-02`" (line 3). It merges v2 lanes and ports v1 changes. Whether that session is still live today is UNVERIFIED and probably stale.
- Rules (lines 7–12):
  - v2 changes go through `main` with a green full suite (`uv run pytest -q`).
  - Lanes branch from `main` under `~/subfleet-v2-lanes/<lane>/`, and the integrator merges them.
  - v2 never edits v1 files except through `lanes transfer`.
  - Line 9 lists "machine state nobody else touches": `~/.subfleet/`, the relocated canary home, launchd `com.subfleet.daemon` and `com.subfleet.soak-report`, and the `transferred_to_v2` roster entry.
  - A v1 change to any file the importer reads needs a row in the table *before* it merges, naming the exact keys. The table columns are `When | Session (branch) | What changes | Files | v2 impact | v2 action | Status` (lines 16–19).
- Incidents are appended as `## Incident <date> <time>` sections. They cover what happened, the manual unblock, the fix branch and clauses, and an "Other sessions:" guidance bullet (lines 62–67, 2026-09-22).
- **The main checkout has uncommitted changes to this file.** `/Users/maxghenis/subfleet-v2/docs/coordination.md` has 8 extra lines ("2026-09-08 — Fable reserve ported into v1") that are not on origin/main (`git diff --stat`). That is another session's work; do not overwrite it.

**`docs/lanes/README.md`**
- One brief per lane covers the assignment, the clauses it binds, its worktree and branch, and the shape of its final message. Reports go under `docs/lanes/reports/`. "Lanes never commit to `main`" (line 3).
- Codex/Astra lanes cannot run the `fake`, `e2e` or `process` suites (the sandbox denies `ps`, `sysctl` and socket binds) and cannot push (lines 7–9).
- Lanes are launched only through `subfleet run` (line 10).
- Worktrees are `~/subfleet-v2-lanes/<lane>/` on branch `lane/<lane>`, each with its own `.venv`. The physical suites need `/usr/sbin` on `PATH` (lines 14–21).

**Lane brief template** (`docs/lanes/sessions.md`)
- Title: `# Lane brief: <name> (milestone N: …)`.
- Sections: "Read first, in this order" (names the clauses); "Scope"; "Out of scope", which includes "Do not change existing clause meanings; if one must change, say exactly what and why in your final message"; "Acceptance for this lane"; "Tooling and git".
- "Final message" headings: `Built; Tests (command, count, time); Clauses covered; Compat cases added; Seam changes; Contract questions; Open questions for the integrator` (sessions.md:45–47).

**How work is claimed.** The docs describe no lock or claim mechanism. A lane is claimed when the integrator writes its brief and creates its worktree and branch; a v1-side change is claimed by adding a row to the coordination table. Since the cutover, the practice has moved to PR branches: the head commit is "Merge pull request #31 from MaxGhenis/fix/route-errors-coverage", and there are branches `feat/lanes-touch`, `fix/daemon-store-lock-wedge` and `fix/reset-credit-demand-only`. The transition plan adds "One integrator owns shared schemas, admission changes and installation. Do not have parallel agents edit the same store/daemon files independently" (transition plan line 148).

**`docs/gates.md`**
- The gate verbs keep v1's exit codes 0 to 5 (lines 14–21).
- "Only the daemon writes database rows." Gate state lives in `events` and is projected atomically to `gates/<id>/gate.json` (line 25). This is a model for daemon-owned conversation state with file projections.
- Line 43 shows the house style for noting a "contract consequence to resolve before changing that behavior" without editing a clause.

## 5. Invariants format and whether new invariants need registering

- **Format.** `docs/invariants.json` is a JSON array of 220 objects. Every row has exactly these fields: `id`, `invariant`, `v1_location`, `class`, `disposition`, `replacement`, `contract_clause`, `acceptance_owner`, `v2_module`, `test_name`, `notes` (`test_invariants_index.py:40-52`). Example: id 72 → `C-23.30`, owner `unit`, module `subfleet/notices.py`, test `test_duplicate_registry_rows_ranked_by_live_pid_then_socket_then_start`.
- **Allowed values:**
  - disposition: `keep`, `replace` or `drop` (line 54)
  - owner: `unit`, `fake`, `process` or `live` (line 55)
  - class: one of 10 values (lines 56–67)
  - `contract_clause`: `C-x.y` or `P-23.n`
  - dropped rows put `n/a` in the clause, owner, module and test fields
- `docs/invariants.md` is the same content as a Markdown table. A test asserts the two agree (lines 188–201 of the test section).
- **The set is closed at 220 rows.** `LEDGER_ROWS = 220` (line 34). The ids must be exactly 1 to 220 in order (tests at lines 127–143), and the ledger `docs/reports/A-invariants.md` must still have 220 rows (lines 204–208). `invariants.md:10` says: "Nothing here is a new invariant."
- **Recommendation: do not register new desktop invariants in `invariants.json`.** Appending row 221 would fail four existing tests, and the file is pinned to v1's adjudicated ledger. New rules should go in as contract clauses (C-24.x and onward), each proved by tests that cite it per C-20.5.
- None of the 220 ledger rows mention cockpit, broker, outbox or conversation; I grepped every row.
- For the transition plan's "acceptance ledger" (each requirement marked recovered, implemented, verified or deferred), use a separate file such as `docs/desktop-ledger.json` with its own index test modelled on `test_invariants_index.py`.
- If that ledger records owner layers, it would need `frontend`, which is not in `OWNERS` or C-20.1.

## 6. Recommended structure for the new contract content

**Placement and numbering**
- Add new sections after 23 rather than suffixed clauses spread across existing sections, so the work can be cited and reviewed as one block.
- Sections 1–22 each cover one topic with a flat list of bullets, so use several short sections. Give each clause the section 23 shape: a parenthetical title, then the clause text, then a bracketed trailer, e.g. `- **C-24.3** (a lost acknowledgement returns the same receipt) … [milestone 9; owner fake; module subfleet/conversations/store.py; legacy provenance …/subfleet/outbox.py]`.
- The trailer's `milestone N` follows the line 220 precedent that "the milestone names when it is first tested", so clauses can land in the contract PR ahead of code. Each still needs its fixture or contract test in the same commit as the code (line 3).

**Proposed sections, grounded in the transition plan's Stage 1 (lines 74–82)**
- **24. Conversation identity and message states.** Conversation id; message UUID (at most 128 characters, following C-1.5) plus a digest (following C-6.2); a repeated UUID returns the same receipt, and changed content is exit 2. States: `saved`, `queued`, `waiting` (reusing the C-4.1 reasons), `starting`, `running`, `approval-needed`, `complete`, `failed`, `cancelled`, `delivery-unknown`. Local acknowledgement is not provider receipt. No exactly-once claim; an ambiguous send is never replayed automatically. Turns are serialized per native session.
- **25. Protocol additions and capability discovery.** Add a `capabilities` op under `v: 1`, because `protocol.py:289` rejects any other version. The client must confirm a capability before sending any field whose silent omission would change the outcome; C-16.2's "unknown fields are ignored" is exactly that hazard. Ops for conversation list/open/create, message submit/status, events after a cursor (a long poll of at most 60 s following C-15.4, with no claim of streaming), approvals pending/respond, and cancel. Also **bring C-16.2 back in line with the code**: it lists 12 ops and `protocol.py:18-23` has 19.
- **26. Turns under admission.** Every turn is an attempt under C-6.3 admission, with lane, model and effort recorded (C-4.6). A warm worker keeps no privileges beyond its reservation. Receipts per turn for completion and interruption, since process exit cannot stand in for them (C-5.1, C-12.1). An explicit ruling that either extends or amends **C-23.54**. An explicit ruling on the **C-10.3** desktop login for native sessions that live under it.
- **27. Approvals.** Scoped to session, turn, request and effective policy. A stale or duplicate reply is refused; add this to C-6.5's index. Never auto-granted. Reuses the `approval` wait reason (C-4.1, C-6.11).
- **28. Attachments and drafts.** Stored under the state root (update C-2.2), 0600/0700 (C-2.3), published per C-8.1. Size limits, symlink and path rules, ownership checks, and retention exceptions (extend C-8.4).
- **29. The desktop client.** The app is a client only (extend C-3.4). An explicit endpoint, with no fallback to v1 or `subfleet-local`. An unavailable daemon produces a status in the app and keeps drafts. Forward-compatible decoding (C-18.2 precedent). The status snapshot is distinct from requesting a new observation. `sessions continue` keeps its C-17.1 recovery meaning, and a new verb name is used for sending a message.
- **30. Legacy continuity import.** Per-message classification as terminal, proven-not-dispatched, or running/ambiguous. Idempotent id mapping. Reclassify `migration.md:33,48-50` and `importer.py:150,1530`.

**Housekeeping in the same commit**
- A `Changes` bullet naming C-24 to C-30 with the date and the transition plan.
- Update the line 3 header (versions and milestones bound).
- A C-21 row for milestone 9 that names acceptance tests (amendment 9).
- Add `tests/frontend/` to C-20.1.
- Match the suffix rule: either avoid letter-suffixed ids or extend the regex in `test_invariants_index.py:96`.

Unverified: whether the integrator `maxghenis-02` is live; whether any code sets `wait_reason=approval`; whether the importer processed live cockpit outbox rows at cutover.