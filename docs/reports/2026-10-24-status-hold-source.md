# Status and alerts name the source of every lane closure (D-STATUS-SRC1)

Branch `fix/status-hold-source-217`, from release/217 at `1c3bfc4f2`. Contract
clauses C-9.6, C-17.1 and C-18.4, with a change-log entry under "Changes in
version 3".

## Why

On 2026-10-24 the Claude lanes started no jobs for nearly 8 hours, and the Codex
lanes were held from 21:51Z. Every closure was a hub controller's `operator-hold`
(load, running caps, disk), not a provider limit. `subfleet status` and the
notices still said the accounts were out, and fleet ops and a lane both told Max
so. What the code did, read at `1c3bfc4f2`:

- `status_json.lane_verdict` returned `limited` for any open account-scope
  closure, holds included, whenever the lane carried no cycle verdict (after a
  restart, say). Otherwise it kept the last cycle's verdict, from before the
  hold, since a held lane is never read (C-18.1): `ok`, or a stale `limited`.
- `alerts.evaluate_conditions` raised `"<provider>: <home> <scope> limited"` for
  every closure but `auth-dead`. A held Codex lane's alert was
  `codex-scoped-limit:<email>:account`, a held Claude lane's `claude-limit:<until>`.
- The fleet-empty alert said "All enrolled lanes are exhausted, unavailable, or
  unknown", whatever the cause.
- `subfleet status` printed closures as `lane scope until … (reason, clock)`.
- `Daemon._hold_lane` took no note and wrote `source_event="operator"`.
- The menu (`StatusModel.swift`) showed `limited` as "Limited".

## The decisions, as built

1. **A `held` verdict** (`status_json.lane_verdict`). The order is:
   - a credential fault the cycle found (its verdict, one of
     `status_json.CREDENTIAL_VERDICTS`, the set the alerts already treated as
     credential conditions);
   - `auth-dead` from a closure, or `disabled`, each keeping the cycle's verdict
     when it has one (as before);
   - `limited`: an open account-scope closure whose reason is not a hold
     (`provider-limit`, `credits`, `cooldown`);
   - `held`: an open `operator-hold` (of any scope) and no such limit;
   - the cycle's verdict, else the readings' (as before).

   A cycle's `limited` (or `held`) is read from the closures and never kept.
   `dispatchable()` judges by `_admission_verdict`, which is the old function with
   the account-closure branch left out: `dispatchable` refuses a lane with any
   account closure anyway, so it answers exactly as before (I1). A held lane
   stays non-dispatchable.

   *Interpretation.* The hub's order was `auth-dead` > `disabled` > `limited` >
   `held` > the readings-based verdicts. The lane rows the timer publishes also
   carry the cycle's own verdict, which the hub's list does not place. A cycle's
   credential fault (`auth-revoked`, `no-auth`, `free-plan` and the rest) is
   ranked with `auth-dead`, above `limited` and `held`. Otherwise a revoked lane
   that also has a limit would lose its credential alert, which names the login
   to run, and the menu's "Sign-in required". Any other cycle verdict ranks
   below `held`, with the readings.
2. **A hold note, with no schema change.** `subfleet lanes hold <lane> --until
   <iso> [--note <text>]`.
   - The note is stored in the closure's `source_event`: `operator` without a
     note, as before, and `operator: <note>` with one (`contracts.hold_source`).
   - It also goes into the `lane.held` event's data, as `note` beside `until`.
     Without a note the data is `{"until": …}`, as before.
   - The rule (`contracts.hold_note`): after stripping, the note must be one line
     of 1 to 160 printable characters (`str.isprintable`: no newline, tab,
     control or separator character other than the space). The CLI refuses
     anything else with exit 2 before it asks the daemon. The daemon refuses it
     with exit 2 (reason `hold-note`) before its transaction, so no closure and
     no event is written. A blank note counts as invalid.
   - `Daemon._hold_lane` is the only code that writes an operator hold. The CLI
     has no store-direct hold path: with the daemon down, `lanes hold` exits 69.
     The importer writes no `operator-hold`. Re-enrolment (`Daemon._enroll_lane_locked`)
     copies the old binding's open holds to its successor with their
     `source_event`, so a note already validated follows the lane unchanged.
   - `source_event` and `"operator"`: the only code that ever produced that
     literal was the writer. No reader compares a `source_event` with it, and
     `test_nothing_reads_a_closure_source_for_equality_with_operator` keeps it so.
     It scans `subfleet/` and `tools/` for an equality test, Python or SQL, and
     for a quoted `operator` on any line that names `source_event`.
   - The daemon's hold answer now carries `note`. When a daemon older than this
     one records the hold without its note, the CLI exits 69 ("recorded the hold
     without its note"), in text and with `--json`.
   - Holds on one lane and scope share one row (C-9.6, `Store.put_closure`). A
     later hold that extends the row rewrites it with its own note; one that ends
     no later leaves the row's end and note as they were. Its own note is in its
     `lane.held` event.
3. **Alerts.**
   - An `operator-hold` closure raises no per-lane alert. Provider limits keep
     today's alert, key and wording.
   - The fleet-empty alert keeps its key (`codex-fleet-empty`,
     `claude-lanes-empty`) and its subject (`<provider>: no dispatchable lanes`).
     Its body names the causes with counts, in this order and only when a cause
     has a lane: held by operator (each distinct note, then the earliest end, a
     lane's end being its last hold's), provider-limited (the earliest reset),
     credential-blocked, disabled, otherwise unavailable. For example: "No codex
     lane is dispatchable: 6 held by operator (codex-open: disk brake; earliest
     end 2026-10-25T03:09:22Z). Run: subfleet status".
   - The condition also carries `causes`, the count for each cause.
   - Severity is `warn` when every one of those lanes is held by an operator.
     Otherwise it is today's: critical for Codex, warn for Claude.
   - Once, at upgrade: a latch an operator hold raised before (a
     `codex-scoped-limit:…:account` or `claude-limit:<until>` key) sees its
     condition gone on the first cycle and clears. Where no other condition is
     in force on that home, C-23.52 sends one `recovered: <home>` notice, and
     only when `alerts.operator_session` is set. The hold itself stays in force
     and `status` shows it.

   *Interpretation.* The brief's example ended "…, 0 provider-limited". I3 says
   "limited" appears only when a lane is provider-limited, so a cause with no
   lane is left out. Times are full ISO instants, as in every closure line,
   because an `HH:MMZ` can be on another day. "Exhausted" is never written.
4. **Status text and JSON.**
   - Closure lines say what each one is (`render.closure_line`):
     `codex-1 held by operator until 2026-10-25T03:51:22Z: codex-open: disk brake`,
     `claude-10 provider limit on account until 2026-10-27T04:00:00Z (reported)`,
     and `credits limit`, `cooldown` and `auth-dead` in the same form.
     `render.closure_text`, which `why`, the daemon's own status text and
     `operations` use, also reads `held by operator until …: <note>` for a hold.
   - Lane rows: `status.json`'s rows carry `held` where they carried `limited`.
     `subfleet status`'s text rows never showed a verdict; they now flag `held`
     or `limited` from the closures in force. With the daemon down, the store's
     unreleased rows include ended ones, so the flag checks each row's end.
   - Closure rows in `status --json` gain `kind` (`hold`, `limit` or `auth`) and
     `note` (null when there is none). This covers the daemon's `closures` and
     each lane's `closures`, the offline reader's, and the `lanes hold` answer.
     Every existing field is kept, unchanged.
   - Unchanged: the `verdict` key on `status --json` lane rows is the timer's
     cycle verdict (C-23.27), as before. No in-repo consumer reads it.
5. **The app.** `StatusModel.swift` maps `held` to "Held" with the neutral tone,
   distinct from "Limited" (warning). A held lane is never read, so its usage
   goes stale. "Held" therefore comes before "Stale usage", and the stale note
   stays in the detail line. `tests/frontend/test_status_model.py` compiles the
   real model and checks Held, Limited and stale-but-held for both providers.
6. **Contract.**
   - C-9.6 says what a hold's note is, where it is stored and when it is refused.
   - C-17.1's verb list has `hold <lane> --until [--note]`.
   - C-18.4 says what status and the alerts call a closed lane.
   - "Changes in version 3" has an entry naming D-STATUS-SRC1.
   - The README has a paragraph on the note and the wording.

Out of scope, as the brief says: the hub's controllers passing `--note`.

## Consumer audit: the verdict

Found with `git grep -n "lane_verdict\|\"limited\"\|'limited'"` and a grep for
`dispatchable(`, `status_json` and `.verdict` in `subfleet/`, `tools/` and
`app/Sources/`. The other `"limited"` hits are attempt outcome classes
(`OutcomeClass.LIMITED`, conversation turn reasons, `canary_check`,
`replay_weekly_ranking`) and probe statuses (`codex.py:442`,
`timers.py:736/815/913`, `actions.py:567`). None of them reads a lane's verdict,
and all are unchanged.

| Site | What it does with the verdict | Effect of the change | Covered by |
| --- | --- | --- | --- |
| `status_json.build_status` (`status.json`) | `verdict` on each Codex home and Claude account, Claude `probe.status`, `live.source`, `oauth_status`, Codex `windows.source`; `auth-dead` shown as `auth-revoked` | A hold reads `held`; a hold plus a limit reads `limited`; a stale cycle `limited` no longer outlives its closure | `test_all_six_codex_lanes_held_…`, `test_a_mix_…`, `test_claude_lanes_all_held_…`, `test_auth_dead_outranks_…`, I2 property, `tests/unit/test_status_json.py` |
| `status_json.dispatchable` | `dispatchable_now`, `best_home`, `claude.lanes.dispatchable_now`, alert fleet counts | None: judges by `_admission_verdict`, equal to release/217's for every lane | I1 differential property (3,000 examples), `test_i1_a_held_lane_is_not_dispatchable_…`, M2 |
| `alerts.evaluate_conditions`, credential conditions | `verdict in CREDENTIAL_VERDICTS` raises `codex-revoked`/`noauth`/`suspect`/`free-plan` or `claude-lane-auth` | None: the set is the same ten verdicts, and a cycle's credential verdict outranks any closure | `tests/unit/test_timers_alerts.py`, `test_operator_notices.py`, `test_auth_dead_outranks_…` |
| `alerts.evaluate_conditions`, per-closure loop | (reads closures, not the verdict) | Skips `operator-hold`; limits unchanged | `test_all_six_…`, `test_a_mix_…` (exact key, subject, body), `test_claude_lanes_all_held_…`, M6 |
| `alerts.fleet_causes`, `fleet_empty_body` (new) | Each lane under one cause by verdict; severity | Counts per cause; `warn` when all held | I3 property (1,500 fleets), the examples, M3, M4, M7 |
| `cli.format_status` lane flags (new) | `held` / `limited` flag from the closures in force | Lane rows say `held` | `test_all_six_held_status_text_…`, `test_a_hold_and_a_limit_on_one_lane_…`, `test_offline_status_text_flags_only_…`, I4 |
| `app/Sources/StatusModel.swift` `laneDisplay` | `verdict` → status and tone | `held` → "Held", neutral, ahead of stale usage | `tests/frontend/test_status_model.py::test_d_status_src1_frontend_says_held_…` (compiled Swift) |
| `status --json` lane rows' `verdict` | The timer's cycle verdict (metadata), not `lane_verdict` | Unchanged | (no consumer in the repository) |

## Invariants

- **I1.** `dispatchable(lane)` equals release/217's function for every lane.
  `test_i1_dispatchable_equals_the_frozen_release_217_function` checks it with
  Hypothesis over closures (all five reasons, three scopes, four ends, sources
  with and without notes), `enabled` (true, false, None, 0, 1), owner,
  `canonical`, `duplicate_of`, the desktop flags, probe state, an explicit
  `dispatchable`, the cycle's `verdict`/`outcome` (strings, None, integers) and
  the readings. It runs 3,000 derandomized examples plus four pinned ones,
  against a frozen copy of the old function in the test file.
- **I2.** The verdict is `limited` iff an open account-scope closure that is not
  a hold exists, the lane is enabled, and it has no credential fault (an
  `auth-dead` closure, or a cycle verdict that names one). It is `held` iff an
  open `operator-hold` exists, no such limit does, and the lane is enabled with
  no credential fault. A credential verdict is kept, and with no other cycle
  verdict an `auth-dead` closure reads `auth-dead` and a disabled lane reads
  `disabled`. Checked over the same lanes, 3,000 examples.
- **I3.** The fleet-empty body's per-cause counts sum to the provider's
  non-dispatchable enrolled lanes. The `limited` and `held` counts equal the
  lanes with those verdicts. Outside the operators' own notes (each replaced by a
  separator before the check), the body says "limited" or "exhausted" only when
  a lane is provider-limited, and never names a cause with 0 lanes. The severity
  is `warn` exactly when every lane is held, or always for Claude. Checked over
  1,500 fleets of 1 to 7 lanes, whose notes include "rate limited by hub" and
  "exhausted? no".
- **I4.** A valid note round-trips exactly into:
  - the closure row (`source_event` = `operator: <note>`);
  - the `lane.held` event (`{"until", "note"}`);
  - `daemon.status` (`kind` `hold`, `note`) on the fleet's and the lane's closure rows;
  - the offline reader's closure rows;
  - `subfleet status`'s closure line;
  - the fleet-empty body (from `Timers.snapshot`).

  An invalid note is refused with exit 2 and reason `hold-note`, and the count
  of closures and events is unchanged. Checked through the daemon's own
  `dispatch`, over generated text: arbitrary Unicode, 150 to 175 characters,
  each of `\n \r \t \x00 \x07 \x1b \x7f \x85   ​ \xa0` embedded, and
  blanks. That is 60 examples plus five pinned ones (a newline, a BEL, 160 and
  161 characters, the plain note). `test_hold_note_accepts_exactly_the_spec_…`
  checks the rule itself over 2,000 examples.

## Mutants

`tools/status_hold_source_mutations.py` runs every named test on the unmutated
tree, which must pass, and then each mutant against its own tests. A mutant
counts as killed only when one of its named tests fails with a `FAILED` line;
an import or collection error does not count.

| Mutant | What it takes back | Tests that must catch it | Result |
| --- | --- | --- | --- |
| M1 | `lane_verdict` returns `limited` for a hold | `test_all_six_codex_lanes_held_…` (all three cycle verdicts), I2, `test_i1_a_held_lane_is_not_dispatchable_…` | killed: all five failed |
| M2 | `dispatchable` also accepts a `held` lane | I1 (the differential), `test_i1_a_held_lane_is_not_dispatchable_…` | killed: both failed |
| M3 | The fleet-empty body loses its per-cause counts | `test_all_six_…`, I3, `test_a_mix_of_holds_and_provider_limits_…` | killed: all five failed |
| M4 | The body calls held lanes exhausted | `test_all_six_…`, I3 | killed: all four failed |
| M5 | The daemon stores the note without `hold_note` | I4 | killed |
| M6 | A hold raises its per-lane limit alert again | `test_all_six_…`, `test_claude_lanes_all_held_…` | killed: all four failed |
| M7 | An all-held Codex fleet stays `critical` | `test_all_six_…`, I3 | killed: all four failed |

The baseline run of the 8 named tests (10 cases) passed on the unmutated tree.
All 7 mutants were then killed, each by its own named tests (`mutants rc 0`,
07:46 to 07:48Z).

## Runs

All runs went through `suite_hold.sh`, in the foreground, one at a time, with
`TMPDIR` and `HYPOTHESIS_STORAGE_DIRECTORY` in the job's scratch directory, and
with `PYTHONPATH`, `SUBFLEET_HOME`, `SUBFLEET_ROOT` and
`SUBFLEET_RETENTION_DORMANT` unset. The unit and fake files also ran with the
lane markers (`SUBFLEET_JOB`, `SUBFLEET_ATTEMPT`) unset, which
`test_operator_notices.py` needs. The e2e files ran with the markers set, which
the person-only tests in `test_conversations.py` need. No network was used.

Times are UTC on 2026-10-25. Each pytest ran in-process under a `SIGALRM` bound
(`run.py`), so a run that reached the hold's 20-minute limit stopped cleanly,
with teardown. The files it had not reached were then run in a later hold.

| Hold | Tree | What ran | Result |
| --- | --- | --- | --- |
| 1, red (07:41) | `git archive 1c3bfc4f2` with the red commit's two test files | `tests/unit/test_status_hold_source.py`, `tests/frontend/test_status_model.py` | **32 failed, 28 passed**, as intended. The 28 passes are the 3 tests of unchanged behaviour (the I1 differential, the source-event scan, the CLI's no-note path) and the 25 existing frontend tests. The new frontend test failed as `('Limited', 'warning') != ('Held', 'neutral')`. |
| 1, green (07:43) | `aa1d1bfe7` plus the uncommitted Swift change | The two files above, plus `test_timers_alerts`, `test_status_json`, `test_render`, `fake/test_lanes_enroll`, `fake/test_status_payload`, `test_operator_notices`, `test_operator_hold_closures`, `test_cli` and `test_handler_refusals` | **834 passed** in 185 s |
| 1, mutants (07:46) | `aa1d1bfe7` | `tools/status_hold_source_mutations.py` | Baseline passed; **7 of 7 killed** |
| 2 (06:24) | `aa1d1bfe7` | Keep-green set A: 71 unit and fake files | **2,153 passed, 1 xfailed**, 0 failed. It stopped at its bound inside `test_operator_notices.py`. |
| 4 (07:33) | `aa1d1bfe7` | The rest of set A: `test_operator_notices.py` and the 12 files after it | **552 passed** in 158 s |
| 4, e2e (07:36) | `aa1d1bfe7` | `e2e/test_conversations.py`, `test_http_isolation.py`, `test_milestone2_claude.py`, `test_reserve.py`, with the lane markers set | **41 passed, 15 skipped**. The skips are the person-only tests, which skip inside a lane. |
| 3 (07:48) | `aa1d1bfe7` | Keep-green set B: 70 fake, frontend and unit files | **395 passed**, 0 failed, over the first 18 files. It stopped at its bound in `frontend/test_core_queue_tray.py`, after `fake/test_admission_stateful.py` (17 stateful examples, each a daemon) had taken most of the hold. |
| 5 (08:08) | `aa1d1bfe7` | The rest of set B: 53 files, the unit files first, then the frontend ones | **914 passed, 3 failed** over the first 7 files (`test_claude_adapter`, `test_claude_cards`, `test_claude_large_stream`, `test_cli`, `test_cli_turns`, `test_codex_probe`, `test_compat`), at load 146 to 165, before it stopped at its bound. The 3 failures are wall-clock bounds in `test_cli.py`: `test_wait_backs_off_when_the_long_poll_returns_at_once` (pause order), `test_ping_open_pipe_is_bounded[partial message]` (0.751 s against 0.75 s) and `test_ping_continuous_input_has_a_total_deadline` (2.39 s against 0.75 s). Those paths are `wait` and `ping`, which this change does not touch, and all three passed in hold 1, at load 22, in the same `test_cli.py`. An A/B rerun of the three, on this tree and on `1c3bfc4f2`, was queued at 08:31. It had not got the lock by 08:44 and was withdrawn at the job's time limit. |

The keep-green set is every test file that
`git grep -lE "limited\|no dispatchable"` finds, every one that names
`lane_verdict`, `dispatchable`, `alerts`, `evaluate_conditions`, `build_status`,
`format_status` or `status_json`, and every one that calls the status op
(`cmd_status`, `daemon.status`, `subfleet status`). The CLI status tests in
`test_cli.py` are included. That is 145 files. `tests/frontend/test_core_live.py`
is left out: inside a lane it always fails, refused for the lane's own markers,
and only after a 900 s wait. Every test in the brief's grep
(`git grep -l "limited\|no dispatchable" tests`) is in the set.
`.hypothesis` was kept in the scratch directory, and the one a stray
`strategy.example()` call left in the worktree was removed.


## Not run

Hold 5 stopped at its bound with 46 of set B's files unreached. Six of them ran
green in hold 1: `test_handler_refusals`, `test_operator_hold_closures`,
`test_status_hold_source`, `test_status_json`, `test_timers_alerts` and
`frontend/test_status_model`. The other 40 did not run in this job. The host
was at load 146 to 165, and holds waited up to 131 minutes for the suite lock (hold 1 from 05:29 to 07:41).
The files: `unit/test_conversation_connections.py`,
`test_conversation_handoff.py`, `test_conversation_handoff_dispatch_races.py`,
`test_conversation_reconcile.py`, `test_conversation_titles.py`,
`test_daemon_settle.py`, `test_daemon_verbs.py`, `test_gate_merge.py`,
`test_importer.py`, `test_invariants_index.py`, `test_launch_env_round2.py`,
`test_launch_env_round4.py`, `test_launch_env_round7.py`, `test_legacy_hold.py`,
`test_limit_from_model_text.py`, `test_mirror_flags_faults.py`, `test_policy.py`,
`test_relay.py`, `test_reset_credit_claim_timing.py`,
`test_reset_credit_demand.py`, `test_reset_credit_review_r1.py`,
`test_reset_credit_review_r2.py`, `test_scheduler.py`, `test_sessions_mirror.py`,
`test_sessions_mirror_load_gap.py`, `test_soak_tools.py`,
`test_steer_invariants.py`, `test_suggestions_r1b_adversarial.py`,
`test_timers_busy_read.py`, `test_timers_daemon.py`, `test_timers_keepalive.py`,
`test_timers_probe.py`, `test_timers_reset_credits.py`,
`test_title_review_regressions.py`, and `frontend/test_core_queue_tray.py`
(partly), `test_core_search.py`, `test_core_store.py`, `test_menu_view.py`,
`test_queue_tray_view.py` and `test_visual_progress.py`.

None of them imports `lane_verdict`, `fleet_causes`, `closure_line` or the hold
note. They are in the set because they match "limited", "dispatchable",
"alerts" or "status". CI on the branch's head is the full check.


## Round r1 (review r1 of #263, 2026-10-25)

Review r1 (GPT-6.1 Sol, REQUEST_CHANGES at `0ced5dde3`) found one P2 and four
P3s. This round builds the hub's six decisions on `8b27040a2` (`0ced5dde3`
with release/217 `4ddb47c6f` merged in), in steps: red tests, one commit per
decision, the mutants, then the contract and this section.

### The decisions, as built

1. **P2: a note that carries a credential is refused, not masked.**
   - `contracts.hold_note` keeps the line rule and then raises ValueError with
     `HOLD_NOTE_CREDENTIAL_RULE`, `--note must not carry a credential (the
     C-23.14 scrubber would change it)`, when `ids.holds_credential` flags the
     stripped note. That is the test C-1.5 applies to a request id and C-25.7
     to a correlation id: the scrubber, read whole and from just after each
     `-` or `_`. The import is lazy, because `ids` imports `contracts`.
   - The CLI (`cmd_lanes`) and the daemon (`_hold_lane`) print the exception's
     message, so each refusal names its own rule. The reason stays
     `hold-note` and the exit stays 2. The wire builds its text from the
     refusal table, never from an exception (C-25.7), so the template is now
     `lanes hold: {hold_rule}`, and `hold_rule` is a checked value whose only
     choices are the two contract rules (`refusals._choices`). Its
     less-specific form is the line rule.
   - No refusal, log line, event or error detail holds the note. The
     ValueError carries only the rule, the daemon raises `from None`, and its
     details are `{"hold_rule": <rule>}`. The tests check `str`, `args`,
     `details` and `fix` of the error, the encoded wire reply, the daemon.log
     line (`refusals.detail_line`), every row of every table in the store, and
     the bytes of every file under the state root, for five dummy shapes.
   - Readers, as a backstop: `hold_note_of` returns `HOLD_NOTE_WITHHELD`,
     `[note withheld: it would carry a credential]`, for a stored note that
     `holds_credential` flags. `closure_row` then shows that hold's
     `source_event` as `operator: [note withheld: …]` too
     (`contracts.hold_source_shown`), because `status --json` carries
     `source_event` beside `note`. Status text (`render`), `status --json`
     (daemon and offline), the fleet-empty body (`closure_note`) and
     `_timer_notice` (the body) all read the note through `hold_note_of`.
   - **One addition:** a stored note longer than `HOLD_NOTE_MAX` is withheld
     too, without being read. `holds_credential` scrubs from each joiner, so
     its work grows with the square of the length. Measured here at load
     about 130: 42.5 s for a 10,000-character note with 5,000 hyphens, and
     4.35 s for 2,000 characters. No writer here stores such a note.
2. **P3-1: the account-closure short circuit comes back, in release's
   order.** *Deviation from the decision's placement.* The decision put the
   account test in `dispatchable`, before `_admission_verdict`. A test proved
   that order wrong for one malformed shape. With closures `[<account hold>,
   None]` and any readings, release/217 raises `AttributeError`: its
   `auth-dead` scan reaches the `None` before its account test. The
   decision's order answers `False`. The smallest change that keeps the
   decision's intent is release's own order: `_admission_verdict` tests for
   an account closure after `auth-dead` and `disabled` and before the
   readings, returning `limited` as release's `lane_verdict` did.
   `dispatchable` is unchanged. A closed lane is refused without its
   evidence being read, and every lane answers or raises exactly as
   release's function did (216 hand-built malformed combinations, then the
   property below).
3. **P3-2: the hold answer enriches nested closure rows.**
   `status_json.closure_rows(view)` converts a capacity view's rows, the
   fleet's and each lane's, with `closure_row`. `daemon.status` and
   `_hold_lane` both call it, so the two cannot drift.
4. **P3-3: the source guard reads syntax trees.**
   `operator_comparisons(*roots)` parses each `*.py` under `subfleet/` and
   `tools/`. It flags an `ast.Compare` using `==`, `!=`, `in` or `not in` with
   the constant `"operator"` (or a tuple, list or set holding it) on one side
   and a `source_event` subscript, attribute or `.get("source_event")` call on
   the other, in either order. It also flags SQL in a string constant that
   compares the column with `'operator'`, which was the old guard's SQL rule.
   `startswith("operator")` and `hold_note_of`'s prefix test pass. The fault
   cases (one line, two lines, `.get(...) !=`, `in ("operator",)`, an
   attribute against a set, the constant first, and SQL) are modules under
   pytest's temporary folder. A clean module passes.
5. **P3-4: I3 checks every rendered count.** `check_fleet_empty` parses the
   body, with the operators' notes masked first. Every cause it names
   carries the condition's count for that cause, in C-18.4's order, and a
   cause with no lane is not named. The review's `1 disabled` → `disabled`
   mutation is now a test and mutant M8.
6. **Invariants, amended.** I4 now covers the accepted text, the session
   notice and the credential refusal. I5 is new. See below.
7. **Contract.** C-9.6 states the credential rule, the refusal's two texts,
   that nothing echoes the note, the only hold writer, and the withheld
   note. C-18.4 states that the hold answer's nested rows carry `kind` and
   `note` too, and that a withheld note's `source_event` is the one field
   that changes. The D-STATUS-SRC1 change-log entry is extended. C-17.1
   names only the verb and is unchanged. The README sentence on the note
   names the credential rule.

**The ordinary-note property, bounded.** *A counterexample to the decision's
wording.* "An ordinary note (letters, digits, spaces, `:;,./()-`) is always
accepted" is false over that whole alphabet. These notes use only those
characters, and the scrubber masks each one:
- `x` × 160 and `abc/` × 40 (a 160-character base64 run);
- `sk-` and 16 letters;
- `AKIA` and 16 capitals;
- the JWT shape `eyJabcdefgh.abcdefgh.abcdefgh`;
- `token: x`, `passwd:x` and `--token abc` (a key word and its value);
- `cookie: x` (a header);
- `Bearer abcdefgh`.

So `hold_note` refuses them all, which is decision 1 working.
`test_i5_the_plain_alphabet_can_spell_a_credential_shape_and_that_is_refused`
pins them. The smallest change that keeps the property's intent (the
credential check never refuses what an operator writes) is to draw ordinary
notes as words:
- of 1 to 18 characters of that alphabet, which is under the shortest token
  shape the scrubber reads (`sk-` and 16 characters);
- none of which contains a key word the scrubber reads (`key`, `token`,
  `secret`, `pass`, `cookie`, `authoriz`, `credential`, `bearer`, `base64`);
- joined by spaces, up to 160 characters.

Every word of the hub's own notes (`codex-open:`, `disk`, `brake`, `load`,
`140`, `running`, `cap`) is such a word. The old I4 example `x` × 160, a
160-character base64 run, is now refused, so the longest-accepted example is a
160-character note of ordinary words (`LONGEST`).

### Every writer of a hold (decision 1)

`git grep` for `add_closure(`, `put_closure(`, `OPERATOR_HOLD`,
`INTO closures` and `UPDATE closures` under `subfleet/` and `tools/`:

| Site | What it writes | The note |
| --- | --- | --- |
| `daemon.py` `_hold_lane` (`lanes hold`) | the `operator-hold` closure and the `lane.held` event | through `hold_note`, before the transaction |
| `daemon.py` re-enrollment (`lanes enroll` of a new binding) | copies each open closure but `auth-dead` of the old binding to the new lane | copies the stored `source_event` as it is; it makes no new text |
| `daemon.py` attempt settlement (two sites), `timers.py` probe cycle | an adapter's or the probe's limit closure (`provider-limit`, `credits`, `cooldown`) | not a hold; the source is a fixed event name (`wham`, `result-text`, the rate-limit event) |
| `importer.py` v1 cooldowns | `cooldown` closures, `source_event` `v1-cooldown` | not a hold |
| `store.py` `put_closure`/`add_closure` | the store API every site above calls | — |
| `daemon.py` `lanes release`, `release --limits`; `actions.py`, `timers.py` releases | `released_at` only | none |
| `tools/measure_idle_cost.py` | a `provider-limit` closure in a measuring tool's own store | not a hold |

The CLI has no store-direct path for a hold: with the daemon down, `lanes hold`
exits with the daemon-unavailable error and writes nothing.

### The `lanes release --limits --note` finding (out of scope, reported)

That note is stripped and must be nonblank. Nothing else is checked: no length
or printable rule and no credential check. It is stored only in the
`lane.limits_released` event's data. No code in `subfleet/`, `app/` or
`tools/` reads that event kind back: the readers of `lane.held` and
`lane.released` (`timers.py`, `actions.py`) read only `until`, and
`Store.list_events` has no caller. The daemon's answer carries the released
closure ids, not the note, and the CLI prints counts only. So that note
reaches neither status, its JSON, the alerts nor a session notice. It does sit
in the store as written. Whether it should share the hold note's rule is the
hub's call.

### Other readers that pass a closure's `source_event` raw (out of scope, reported)

These answers carry closure rows as the store holds them. With an accepted
note that is the note `lanes hold` accepted. Only a row from a writer outside
`lanes hold` could carry a flagged one, and these readers would then show it:
- the `lanes` list op (`lanes[*].closures`, `subfleet lanes list --json`);
- the `readings` op's `closures`;
- the `lanes release --limits` answer;
- `subfleet errors`, which prints each closure row created in its window as
  JSON;
- the stuck-pin and route records `why` shows.

None is status, `status --json`, an alert or a notice. Routing them through
`hold_source_shown` is a one-line change at each site, if the hub wants it.

### Invariants, round r1

- **I1.** `dispatchable(lane)` equals release/217's function for every lane.
  The well-formed differential is unchanged (3,000 examples).
  `test_i1_with_malformed_evidence_dispatchable_answers_or_raises_as_release_did`
  adds 3,000 more over malformed evidence, plus four pinned cases:
  - readings: None, `[None]`, non-mapping entries, and mappings missing their
    keys;
  - closure rows missing `scope` or `reason`, and non-mapping closure rows.

  The two functions must give the same answer, or raise the same exception
  class.
- **I2.** Unchanged (3,000 examples).
- **I3.** As before, and now every count the fleet-empty body renders equals
  the condition's count for that cause, in C-18.4's order, and a cause with
  no lane is not named (1,500 fleets).
- **I4.** A note `hold_note` accepts round-trips exactly into each of these:
  - the closure row;
  - the `lane.held` event;
  - `daemon.status` at both levels;
  - the offline reader;
  - the status text;
  - the fleet-empty body;
  - the session notice that body becomes (`_timer_notice` with
    `alerts.operator_session` set).

  A note it refuses is refused with exit 2, reason `hold-note` and the wire
  text of the rule it broke. The store is left unchanged: no closure and no
  event. Its pinned examples now include a 160-character ordinary note
  (`LONGEST`) and two credential shapes, beside the 60 generated ones, and the
  oracle is the spec: the line rule, then `holds_credential`.
- **I5 (new).** No text the C-23.14 scrubber would change reaches the store,
  status, the alerts or a notice through a hold.
  - Notes are printable text with a dummy credential spliced in:
    `api_key=…`, `sk-…`, `glpat-…`, `xoxb-…` or `Bearer …`, alone or after a
    word and a `-` or `_` joiner. That is 150 examples plus three pinned.
  - A refused note leaves the store's closure and event counts unchanged,
    and the dummy value is in no row.
  - An accepted note has `holds_credential` false. Every surface it reaches
    is left alone by the scrubber, read whole and from each joiner: the
    event's note, the status line, the fleet-empty body, the notice text,
    and each hold row's `note` and `source_event` at both levels.
  - Paired with the ordinary-note property: every such note is accepted,
    and round-trips through `source_event` (400 examples).

### Mutants, round r1

`tools/status_hold_source_mutations.py` ran from a `git archive` export of
`5bf49779f`'s tree in hold 1, never in the worktree. Its baseline was hold 1's
own run of the same tree minutes before (`.mutants-baseline.xml`, that run's
JUnit file): all 21 named tests passed there, every case. Each mutant then ran
its named tests. "Killed" means pytest failed and at least one named test has a
`FAILED` line.

| Mutant | What it takes back | Named tests that failed | Result |
| --- | --- | --- | --- |
| M1 | `held` reads `limited` | `test_all_six_…` ×3, I2, `test_i1_a_held_lane_…` | killed |
| M2 | `held` counts as dispatchable | I1, `test_i1_a_held_lane_…` | killed |
| M3 | the per-cause counts dropped | `test_all_six_…` ×3, I3, `test_a_mix_…` | killed |
| M4 | holds said to be exhausted | `test_all_six_…` ×3, I3 | killed |
| M5 | the note stored without validation | I4 | killed |
| M6 | a per-lane limit alert for a hold | `test_all_six_…` ×3, `test_claude_lanes_all_held_…` | killed |
| M7 | an all-held fleet left critical | `test_all_six_…` ×3, I3 | killed |
| M8 | I3: `1 disabled` rendered `disabled` | I3, `test_r1_p3_4_…`, `test_a_disabled_lane_…` | killed |
| M9 | `hold_note` without the credential check | (the bound stopped it mid-run) | **not finished** |
| M10 | the readers' backstop dropped | — | **not run** |
| M11 | `_admission_verdict` reads the readings before the account test | — | **not run** |
| M12 | the hold answer's nested rows left raw | — | **not run** |
| M13 | the old line-filtering source guard | — | **not run** |
| M14a, M14b | the daemon's, then the CLI's, refusal echoes the note | — | **not run** |

The mutant step had a 25-minute bound. At load 105 to 124 each mutant took
about 2.6 minutes, almost all of it pytest collecting the module under load.
The bound stopped the driver inside M9 at 00:46Z, and the driver restored every
file it had mutated (checked byte for byte against the commit afterwards). That
was past the 4-hour mark, so no further run was started.

**What stands in for M9 to M13: the red run.** For each of these five, the
mutated code is exactly what `8b27040a2` had for that decision:
- M9: no credential check;
- M10: no backstop;
- M11: no account test before the readings;
- M12: nested rows raw;
- M13: the line-filtering guard.

The red run of the round's tests on that code failed each named assertion:
- **M9:** I4, `test_hold_note_accepts_…`, I5, the constants test, the five
  daemon-refusal cases, the five CLI-refusal cases and the P2 reproduction;
- **M10:** `test_r1_p2_a_stored_credential_note_is_withheld_by_every_reader`;
- **M11:** `test_r1_p3_1_…[None]` and `[readings1]`;
- **M12:** `test_r1_p3_2_…`;
- **M13:** `test_r1_p3_3_…[two lines]`.

The same tests passed on the fixed tree in hold 1. This pairs each decision's
code with its tests, but the rest of that tree is the unfixed one, so it is not
the driver's one-change-at-a-time pairing. M14a and M14b have no stand-in and
did not run.

### Runs, round r1

All pytest runs went through `suite_hold.sh`, one at a time, in-process under a
`SIGALRM` bound (`run.py`), so a run that reached its bound stopped with
teardown. `TMPDIR` and `HYPOTHESIS_STORAGE_DIRECTORY` were in the job's scratch
folder, and `PYTHONPATH`, `SUBFLEET_HOME`, `SUBFLEET_ROOT` and
`SUBFLEET_RETENTION_DORMANT` were unset. The lane markers were unset for the
unit and fake files. Python 3.12.14 (`uv sync --group dev`), no network. Times
are UTC, 2026-10-25 into 10-26. Loads are `vm.loadavg` (1, 5 and 15 minutes).

| Hold | Tree | What ran | Result | Load at start → end |
| --- | --- | --- | --- | --- |
| red, 22:03 (queued 21:39) | `5a91bc9f8`: `8b27040a2`'s code with the red tests | `test_status_hold_source.py` | **32 failed, 39 passed** in 861 s, as intended. Every test of a behaviour this round changes failed. The 39 passes are the existing tests, plus the new tests whose behaviour already held at `8b27040a2`: the old guard caught five of the six comparison cases (all but the one split over two lines), though it also flagged the clean module's `source_event != "wham"`, and the ordinary-note property passed | 120 → 114 |
| 2a, 22:44 | `3b62103f1` (the head's code) | the three contract guards | **63 passed** in 531 s: `fake/test_state_contract.py` 37, `test_test_module_names.py` 1, `test_invariants_index.py` 25 | 97 → 26 |
| 2b, 22:53 | same | review r1's two unfinished tests in `test_unadmittable_pin.py` | **2 passed** in 159 s | 26 → 65 |
| 2c, 22:56 | same | 19 neighbouring unit and fake files | **628 passed**, 0 failed, in 1,559 s, stopped at the bound in or just after `test_importer.py`. Passed: `test_offline` 33, `test_capacity_view` 20, `test_daemon_refusals_cli` 8, `test_native_operations` 28, `test_legacy_hold` 56, `test_route_check` 24, `test_timers_probe` 35, `test_timers_busy_read` 115, `test_timers_reset_credits` 47, `test_reset_credit_demand` 68, `test_reset_credit_properties` 11, `test_decision_horizon` 7, `test_lease_waiter_selection` 6, `test_limit_from_model_text` 95, `test_importer` 75. Not reached: `test_scheduler_weekly`, `test_capacity_steer_route`, `fake/test_admission_disk`, `fake/test_reset_credit_incident` | 65 → 110 |
| 1a, 23:55 | `5bf49779f` (the head) | the builder's targets and the full CLI file, in that order | **240 passed**, 0 failed, in 1,524 s, stopped at the bound: `test_status_hold_source.py` **73 passed** (all of it), `test_handler_refusals.py` 167 passed (stopped after `test_a_checked_value_has_its_shape_and_no_free_text`). Not reached: `test_render`, `test_status_json`, `test_timers_alerts`, `test_operator_hold_closures`, `fake/test_lanes_enroll`, `fake/test_status_payload`, `test_operator_notices`, `test_cli`, `frontend/test_status_model` | 115 → 124 |
| 1b, 00:21 | same | the mutants | M1 to M8 killed; M9 stopped by the bound; M10 to M14b not run | 124 → 105 |
| 3, 00:46 | — | the rest of hold 1's targets, then the review's remaining files | not started: past the 4-hour mark | 108 |

Before the holds, a plain Python smoke (not pytest) called the round's
daemon-core tests and the rule tests directly on the fixed tree, with
`tests/conftest.py`'s home-path isolation applied by hand: 36 checks passed. It
found one test bug, now fixed (`edada4bdb`). The P2 reproduction, once the hold
was refused, had no fleet-empty alert to read, so it now holds the lane with a
plain note first.

**The three CLI timing tests.** `test_cli.py` did not run in this round: hold
1's bound stopped before it, and hold 3 could not start. So there were no
failures to rerun, and no load-paired reruns exist. Their bounds are unchanged.

**The app.** No Swift changed, so the app model tests (review r1's check 6)
were not rerun.

### Not run, round r1

- **Mutants:** M9 to M14b, run as the driver pairs them; see above for the red
  run's stand-in for M9 to M13.
- **From the builder's targets** (review r1's hold 01):
  - `test_render`, `test_status_json`, `test_timers_alerts`,
    `test_operator_hold_closures`, `test_operator_notices` and `test_cli` (the
    full CLI tests, the three timing tests among them);
  - `fake/test_lanes_enroll`, `fake/test_status_payload`;
  - `frontend/test_status_model`;
  - the rest of `test_handler_refusals`.
- **From review r1's holds table** (holds 02 to 05 and 09 to 12):
  - `test_scheduler_weekly`, `test_capacity_steer_route`, `test_scheduler`,
    `test_steer_invariants`;
  - `test_conversation_handoff`, `test_conversation_handoff_dispatch_races`,
    `test_conversation_steer_service`, `test_conversation_store`,
    `test_conversation_wakes`;
  - `test_claude_adapter`, `test_codex_adapter`, `test_daemon_verbs`,
    `test_daemon_settle`, `test_policy`;
  - `test_reset_credit_claim_timing`, `test_reset_credit_review_r1`,
    `test_reset_credit_review_r2`, `test_timers_daemon`, `test_timers_keepalive`;
  - `fake/test_admission_disk`, `fake/test_reset_credit_incident`;
  - `e2e/test_http_isolation`, `e2e/test_milestone2_claude`;
  - frontend `test_core_approval_reach`, `test_core_queue_tray`,
    `test_pr124_fixes_ui`, `test_review_r219_probes`, `test_visual_progress`;
  - the old-release Swift decoder check.

The round's code changes touch `contracts.py` (`hold_note`, `hold_note_of`,
`hold_source_shown`), `status_json.py` (`_admission_verdict`, `closure_row`,
`closure_rows`), `daemon.py` (`_hold_lane`, `daemon.status`), `cli.py`
(`cmd_lanes`'s refusal line) and `refusals.py` (the `hold-note` template).
`test_status_hold_source.py` covers each of them and passed in full. Of the
unrun files, `test_cli`, `test_status_json`, `test_render`,
`test_operator_hold_closures`, `fake/test_lanes_enroll` and
`fake/test_status_payload` import or drive these functions, so they are the
first to run. CI on the head is the full check.
