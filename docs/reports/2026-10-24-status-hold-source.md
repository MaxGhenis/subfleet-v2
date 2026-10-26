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

## Round r2 (review r2 of #263, 2026-10-26)

Review r2 asked for changes at `09ce84d32`: every r1 finding was fixed, and it
found five new P3s, each executed. The hub's six decisions are built on
`fix/status-hold-source-217` from `09ce84d32`, red tests first. Every
credential in a test or a run is a dummy `SECRET7…` value.

### The decisions, as built

1. **P3-1: I5 holds for the JSON forms.** `contracts.hold_note` now refuses a
   note, with `HOLD_NOTE_CREDENTIAL_RULE`, when `ids.holds_credential` flags it
   as written or as `json.dumps` writes it (`json.dumps(note)[1:-1]`).
   `holds_credential` is unchanged; it stays the request-id predicate.
   - **Why:** the scrubber's URL rule opens with `\b`, and there is no word
     boundary between `ü` and `h`. So `ühttps://u:SECRET7…@host` passes the
     scrubber as written. `ühttps://…`, the form `status --json` and
     `status.json` carry, is masked (its scheme reads as `u00fchttps`).
   - **Tests:**
     - `test_r2_p3_1_a_url_password_after_a_non_ascii_letter_is_refused` covers
       `ü`, `é` and `中`. Its premise asserts the raw note is clean and the
       JSON form flagged. It then checks `hold_note`'s rule, the daemon's
       refusal (exit 2, `hold-note`, the credential rule's wire text) and the
       store: unchanged, and the dummy in no row.
     - `test_r2_p3_1_lanes_hold_refuses_it_before_asking_the_daemon` checks
       the CLI: exit 2, the rule on stderr alone, and the daemon never asked.
     - A stored `operator: ühttps://…` is withheld: the `url after ü` case of
       decision 3's test.
   - **I5:**
     - Two URL shapes join the credential shapes: `https://u:{}@host` and
       `ftp://operator:{}@example.invalid/x`.
     - A prefix of nothing, `ü`, `é` or `中` goes just before the credential.
     - Three URL examples are pinned.
     - An accepted note's surfaces are now also checked as JSON writes them
       (`scrubbed_alike(json.dumps(text)[1:-1])`).
     - Two whole JSON documents are checked too: `status --json` (the
       `daemon.status` answer as the CLI's `emit` writes it) and `status.json`
       as `Timers.publish_status` writes it after `alerts.evaluate`.
   - **The whole-document check:** the whole `status --json` text is not left
     alone by the scrubber even with a clean note, because `daemon.status`
     carries `"lane_credentials": []` and the scrubber masks that. So I5
     checks that the note's JSON form is in each document and that every copy
     of it comes through the scrubber over the whole document unchanged
     (`survives_scrubbing`).
   - **D-SCR-URL1, outside this PR:** `1https://u:SECRET7…@host`, a URL
     password just after a digit, is masked by no scrubber mode, as written or
     as JSON. So the rule accepts it.
     `test_d_scr_url1_a_url_password_after_a_digit_is_accepted_today` pins
     that: both forms, `whole` false and true, are unchanged and
     `hold_note` returns the note. When D-SCR-URL1 changes the URL rule, the
     note is refused and the test is to be turned round.
2. **P3-2: every reader withholds a stored credential note.** The `lanes` list
   answer, the `readings` answer and the `lanes release --limits` answer now
   go through `closure_rows`. `operations.error_report` (`errors`, its JSON and
   its text lines alike) goes through `closure_row`. Their rows gain `kind` and
   `note`, as `daemon.status`'s did.
   - **The grep found four more readers, all route decisions:**
     - `why --task/--tier`;
     - `why <job>`, recorded or evaluated now;
     - the `run --dry-run` answer;
     - the offline `runs show`.

     The scheduler copies the closure rows it read into each evaluation's
     `closures` and `stranding_closures`. They are stored in `decisions` and
     shown as they were.
   - **How they are routed: deviation.** These four go through
     `status_json.decision_rows`. It applies `closure_row`'s withholding step
     (`withheld_source`) to each hold row and adds nothing else.
     - **Why not `closure_row`:** `why` answers with the recorded walk
       (C-11.5), and
       `tests/fake/test_routing_end_to_end.py::test_c11_6_…` asserts that
       answer equals the stored `decision_json`.
     - **So:** a decision whose holds `lanes hold` wrote reads exactly as
       recorded. A hold row any other writer left shows
       `operator: [note withheld: …]`.
   - **Test:** `test_r2_p3_2_every_reader_withholds_a_stored_note`, for a
     stored `operator: api_key=SECRET7…` row and for a stored 161-character
     clean note. It checks each of the eight readers:
     - each hold row's `(note, source_event)`, or the source alone in a
       decision;
     - neither the note nor its JSON form is in the answer as JSON or as
       text.

     The decision readers are reached through a recorded decision (a job
     row and `Store.add_decision`). The test also checks the premise that
     the scheduler's decision carries the stored row as stored.
   - **The reader list** is below. With it, the change-log sentence "every
     reader withholds one stored outside `lanes hold`" is true as written, and
     the change log is unchanged.
3. **P3-3: the backstop parses strictly. Deviation in placement.** The strict
   parse is in `contracts.hold_note_of`, which `closure_row` reads through
   `withheld_source` and `hold_source_shown`. It is not only in `closure_row`.
   - **The rule:**
     - exactly `operator` carries no note;
     - exactly `operator: <note>` carries the note when `hold_note` accepts it
       as it stands, which means length, printable, credential and JSON form,
       and no surrounding whitespace;
     - anything else reads as `HOLD_NOTE_WITHHELD`.
   - **Why there:** `hold_note_of` is the one parser behind every reader:
     `closure_row` (both `note` and `source_event`), the status text
     (`render._held_text`) and the fleet-empty body (`alerts.closure_note`).
     So the text and the body show the withheld text too. The decision's test
     asks for it there.
   - **Literal reading:**
     - A hold whose source is None, or not a string, is "not exactly
       `operator`", so it reads as withheld.
     - `hold_note_of("wham")` now reads as withheld too. Every caller passes a
       hold's row only.
   - **No comparison with the word:** the parse is a round trip,
     `hold_source(hold_note(rest)) == source_event`, so it compares nothing
     with `operator`, and C-9.6's guard has nothing to except.
   - **Cost:** a note over 160 characters is refused on its length before the
     scrubber reads it.
   - **Test:** `test_r2_p3_3_a_hold_source_lanes_hold_would_not_write_is_withheld`.
     - **Inputs:** r2's three (`operator:api_key=…`, `Operator: api_key=…`,
       `sk-…`), plus a stored `operator: ühttps://…`.
     - **Where it checks:** `daemon.status` at both levels, the offline
       reader, the status text and the fleet-empty body each show the
       withheld text, and the notice is written.
     - **What must be absent:** the dummy, from both JSON documents (the
       daemon's and the offline reader's), both texts, the body and the
       notice.
   - **Strict-parse property:** `test_hold_note_accepts_exactly_the_spec_…`
     now also checks `hold_note_of` against a spec mirror (`stored_note`) for
     each drawn text, alone and after `operator: `. It also checks ten fixed
     malformed sources.
4. **P3-4: the source guard catches what syntax shows, and C-9.6 says it is
   syntactic.**
   - **What `_comparisons` catches:**
     - **A comparison:** `==`, `!=`, `in`, `not in`, `is` or `is not`, with
       one of these on either side, whatever the other side is:
       - the literal `"operator"`;
       - a name or attribute `HOLD_SOURCE`;
       - a set, tuple or list holding one, or a `frozenset(…)`, `set(…)`,
         `tuple(…)` or `list(…)` of one.
     - **A `match` case** whose value pattern is one, `|` alternatives
       included.
     - **SQL** in a string constant or an f-string. The f-string is read with
       each formatted `operator` or `HOLD_SOURCE` written in. It must compare
       `source_event` (`=`, `==`, `!=`, `<>`, `IN`, `IS`, `LIKE`, an optional
       `(`) with the word in either quote, or with a `?` that its call passes
       the word for among its other arguments, keywords included.
   - **Reading "whatever wraps the other side":** the guard needs nothing of
     the other side. So it also catches a helper's `value == "operator"` and
     an alias of the column. As a cost, any comparison with the word in
     `subfleet/` or `tools/` fails it, even one on another field. No such
     comparison exists today; a case pins the behaviour.
   - **r2's cases:** r2 kept no scratch modules, so its 16 are rebuilt from
     its list. Its getattr and its alias are each read both ways:
     - on the column, `getattr(closure, "source_event") == "operator"` and
       `source = row["source_event"]`: caught;
     - on the constant, `== getattr(contracts, "HOLD_SOURCE")` and
       `MARK = "operator"`: the known misses.
   - **Tests:**
     - `test_r2_p3_4_the_source_guard_catches_each_comparison_it_can_see` has
       22 cases, each with the line the guard must name.
     - `test_r2_p3_4_the_source_guard_known_misses_stay_listed` has 5 cases:
       `operator.eq`, a dict lookup, getattr for the constant, the constant
       aliased through a variable, and the SQL aliased through a variable
       (that last is one more than the hub listed).
     - `test_r2_p3_4_the_source_guard_passes_what_is_no_comparison_with_the_word`
       covers `hold_note_of`'s round trip, a `?` whose parameter is not the
       word, the word in other columns' SQL, and `"operator-hold"`.
     - r1's seven cases still pass.
     - The real tree is clean under the new guard.
   - **The contract:** C-9.6 now states that the guard is syntactic only,
     what it catches, and what it does not see.
5. **P3-5: the >160 rule has a test.**
   `test_r2_p3_5_a_stored_note_over_160_characters_is_withheld_unread` stores
   `LONGEST + "!"`. That note is 161 characters, has no credential, and has no
   surrounding whitespace, so nothing else could withhold it.
   - It expects the withheld text in `daemon.status` (both levels), the status
     text, the fleet-empty body and the notice.
   - Neither the note nor its first 160 characters may appear in `status
     --json`, the text, the body or the notice.
   - A spy on `ids.holds_credential` shows the note is never read.
   - r2's R1 is killed, both at `09ce84d32` (red, below) and as it applies at
     the head.
6. **The contract.** Only C-9.6 changed, in three places:
   - the note rule now reads the JSON form too;
   - the stored-note sentence is now the strict read, with every reader
     named;
   - the guard sentence says it is syntactic and names what it misses.

   The change-log entry is true as written and is unchanged. So are C-17.1
   and C-18.4.

### Every reader of closure rows (decision 2)

`git grep` for `closures`, `list_closures`, `source_event`, `decision_json` and
`FROM decisions` under `subfleet/` and `tools/`, at the head. "Reader" here
means code that shows a closure row, or a copy of one, to anyone: a daemon
answer, CLI output, an alert, a notice or a published file.

**Through `closure_row`, with `kind` and `note`, and a hold's source withheld:**

| Reader | Where |
| --- | --- |
| `daemon.status` (`status --json`, and the status text drawn from it) | `daemon.py:3729` (`closure_rows`) |
| the `lanes hold` and `lanes release` answer | `daemon.py:1398` |
| the `lanes release --limits` answer (new) | `daemon.py:1433` |
| the `lanes` list answer (`lanes list --json`; `status` falls back to it for an older daemon) (new) | `daemon.py:3620` |
| the `readings` answer's closures, and the `status` text it renders from them (new) | `daemon.py:3623` |
| `operations.error_report`: `errors` JSON, and its text lines (`json.dumps` per row), from the daemon and offline (new) | `operations.py:37` |
| the offline `status` | `offline.py:359` |

**Through `decision_rows` (each hold's `source_event` withheld, nothing added):**

| Reader | Where |
| --- | --- |
| `why --task/--tier` (its `decision` and its `text`) | `daemon.py:3630` |
| `why <job>`, recorded or evaluated now | `daemon.py:3890` |
| `run --dry-run` (`--why` prints the decision) | `daemon.py:2598` |
| offline `runs show` (`job.decision`) | `offline.py:294` |

**Reading a hold's note through the strict `hold_note_of`, as text:**
- `render.closure_line` and `render.closure_text` (`_held_text`). These serve
  the `status` text, `operations.brief`, the `readings` answer's `status` text,
  and the `why` answer's `text` (`render.why`).
- `alerts.fleet_empty_body` (`closure_note`). This serves the alert body, the
  session notice, the alert latch's kept words, and `status.json`'s `alerts`.

**Shown not to render `source_event` or a note:**
- `alerts.evaluate_conditions`'s per-lane limit alert: it reads scope, reason
  and end, and skips holds.
- `scheduler.judge_lane`, `evaluate`, `pin_unadmittable` (its `closures` are
  scope, end and reason only, so `job.pin_unadmittable`, `job.pin_refused` and
  the `why` hold record carry no source), `_earliest_reset` and
  `standing_refusals`.
- `render.pin_refusals`, `pin_notice` and `pin_failure`: reason, scope and end.
- `picker.rank` (the `pick` answer): reasons `closed:<scope>`.
- `capacity.build_view`, `store_rows` and the horizon. `route_check` is the
  internal C-6.3 check; its evaluations become the decisions above.
- `actions.store_holds`, `lane_hold` and `_lanes_now` (the reset-credit pass
  and its answers): lane ids and reasons.
- `timers.py`'s held-lane, auth-dead and released queries: lane ids or
  existence.
- `status_json.build_status` (`status.json`): verdicts and alert bodies, with
  no closure rows.
- `daemon._open_closures` and `_pin_view`: internal route and pin views.
- `tools/compare_decisions.py`: the chosen model and lane, and the reason.
- `cli._format_decision`, the text `why` and `run --dry-run` print: each
  evaluation's reason, candidates and rejections, and no closure row.

**Writers and copies, not readers:**
- the re-enrollment copy (`daemon.py:1275`), which copies a stored source as
  it is;
- `importer.py`'s v1 cooldowns (`v1-cooldown`);
- the store API;
- the `decisions` table and the retention archive's copy of it
  (`retention_archive.py:312`), which keep the decision as recorded. Its
  readers are the four above.

Round r1's "Other readers that pass a closure's `source_event` raw" are all
routed now.

### Invariants, round r2

- **I1 to I4:** unchanged. I4's spec oracle (`note_rule`) now reads the JSON
  form too, as `hold_note` does.
- **I5:**
  - **Statement:** no text the C-23.14 scrubber would change reaches the
    store, status, the alerts or a notice through a hold. That holds as
    written and as JSON carries it (`status --json`, `status.json`).
  - **The notes:** 150 generated examples plus six pinned (three URL notes
    among them).
  - **The shapes:** seven credential shapes, two of them URLs, alone or after
    a word and a joiner, with an optional letter outside ASCII just before
    the credential.
  - **Outside it:** D-SCR-URL1's digit prefix, pinned as accepted today.
- **The strict hold source (not a numbered invariant):** for every text the
  rule's property draws (2,000), `hold_note_of` of the text, and of
  `operator: ` and the text, equals the spec mirror. The mirror gives None for
  exactly `operator`, the note for exactly `operator: <note>` with an
  accepted note, and the withheld text otherwise.

### Mutants, round r2

`tools/status_hold_source_mutations.py` gained R1 and M15 to M17. M9 and M10
now follow the code they mutate, and IDs given on the command line select
mutants. Per the Budget hold, only the four named below ran, in one hold, from a
`git archive` export of `2fee10928`. The driver ran its own baseline first:
the 10 named tests, all passed. "Killed" means pytest failed and a named test
has a `FAILED` line.

| Mutant | What it takes back | Named tests that failed | Result |
| --- | --- | --- | --- |
| R1 (r2's) | drops `len(note) > HOLD_NOTE_MAX or`. It was at `contracts.py:440` in `hold_note_of`; that check now lives only in `hold_note` | `test_r2_p3_5_…`, `test_r2_p3_2_…[161 characters]`, I4, `test_hold_note_accepts_exactly_the_spec_…`, `test_lanes_hold_refuses_an_invalid_note_…[x×161]` | killed |
| M15 | `hold_note` without the JSON form | `test_r2_p3_1_a_url_password_…` ×3, `test_r2_p3_1_lanes_hold_refuses_it_…` ×3, I5, `test_r2_p3_3_…[url after ü]` | killed |
| M16 | `closure_row` (`withheld_source`) shows any source opening with `operator` as stored | `test_r2_p3_3_…[no space]`, `[url after ü]`, `test_r1_p2_a_stored_credential_note_is_withheld_by_every_reader` | killed |
| M17 | the `lanes` list answer left unrouted | `test_r2_p3_2_…[161 characters]`, `[credential]` | killed |

**R1 at `09ce84d32`.** r2's R1 was applied to its own line in a `git archive`
export of `349b81601` (the base code with the red tests), and
`test_r2_p3_5_…` failed: the 161-character note showed. Without R1, the same
test passes on that code (the red run). That is P3-5's red.

### Runs, round r2

Every pytest went through `suite_hold.sh`, one at a time; each hold was
admitted at once.
- **Environment:** `TMPDIR` and `HYPOTHESIS_STORAGE_DIRECTORY` were in the
  scratch folder, `PYTHONPATH` and `SUBFLEET_RETENTION_DORMANT` were unset,
  and `PYTHONDONTWRITEBYTECODE=1` was set.
- **Python:** 3.14.7 (`uv sync --locked --group dev`), serial (no xdist). No
  network.
- **Times and loads:** UTC on 2026-10-26; loads are `vm.loadavg` (1, 5 and
  15 minutes).

| Hold | Tree | What ran | Result | Load at start → end |
| --- | --- | --- | --- | --- |
| red, 10:25 | `349b81601`: `09ce84d32`'s code with the red tests | the round's new and changed tests in `test_status_hold_source.py` (`-k`) | **33 failed, 12 passed** in 11 s, as intended. The 12 passes are the tests whose behaviour already held: the guard cases the old guard caught (literal `==`, SQL `LIKE`, the f-string with the literal), the five pinned misses, the pass case, the D-SCR-URL1 pin, I4, and P3-5 (the >160 rule exists at the base) | 25.7 → 29.0 |
| R1 red, 10:30 | the same tree, R1 applied | `test_r2_p3_5_…` and three others | `test_r2_p3_5_…` **failed** with R1, against passing without it. The other three failed for their own decisions: `test_hold_note_accepts_…` on the strict read, I5 on `éhttps://…`, and P3-2 on the `lanes` list | not recorded (29.0 two minutes before) |
| 1, 10:35 | the head's code before it was committed (one docstring changed after) | `test_status_hold_source.py` | **115 passed** in 82 s | 15.6 → 23.0 |
| mutants, 10:40 | export of `2fee10928` | R1, M15, M16, M17 | baseline passed (10 tests); **4 of 4 killed** | 36.9 → 30.7 |
| A, 10:42 | `2e1f8c702` | `test_status_hold_source.py`, `frontend/test_status_model.py`, the three contract guards | **204 passed** in 199 s: 115, 26, `fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25 | 35.1 → 35.9 |
| B, 10:46 | `2e1f8c702` | `test_cli.py` | **256 passed** in 45 s; `test_ping_continuous_input_has_a_total_deadline` passed at load 33 | 33.5 → 23.0 |
| C, 10:47 | `2e1f8c702` | beyond the brief's list: 11 existing tests that drive the readers this round routed (`why` against the recorded decision, the `why` walk over the socket, `run --dry-run`, `error_report`, `release --limits`, the `lanes` list with a transaction open, the hold/limit interleaving property) | **20 passed** in 27 s | 23.6 → 33.3 |

**The app:** no Swift changed. **The CLI timing tests:** all passed in hold B,
so there were no reruns.

### Not run, round r2

- The rest of the suite, as the brief asked; CI runs it. The files nearest
  this round's changes are listed below. Hold C ran only the named tests
  from `fake/test_routing_end_to_end.py`, `test_native_operations.py`,
  `fake/test_lanes_enroll.py`, `test_store_readers.py` and
  `test_operator_hold_closures.py`.
  - `test_render`, `test_status_json` and `test_offline`;
  - the rest of those five files;
  - `fake/test_status_payload` and `test_operator_notices`.
- **Mutants:** M1 to M14b were not rerun (the Budget hold), and neither was
  any mutant beyond the four.
- `tests/frontend/test_core_live.py` cannot run on a lane, and the old-release
  Swift decoder check was not run.

## Round r3 (review r3 of #263, 2026-10-26)

Review r3 asked for changes at `b11661169` for one P3. C-9.6's sentence on the
source guard named SQL forms that the guard, executed, does not catch, and its
list of misses was incomplete. Every r2 finding was fixed, and review r3 found
no P1 or P2. The hub's five decisions are built on `fix/status-hold-source-217`
from `b11661169`, red tests first. No code under `subfleet/` changed. The guard
is a test, so this round changes only `tests/unit/test_status_hold_source.py`,
`tools/status_hold_source_mutations.py`, the contract and this report. No test
this round carries a credential.

### The decisions, as built

1. **The extended SQL check. Deviation: one `\s*` kept.**
   `SQL_COMPARISON` (`tests/unit/test_status_hold_source.py:746`) is review
   r3's regex with one change.
   - **The counterexample:** review r3's regex drops the `\s*` that
     `b11661169`'s had after the optional `(`. So, as written, it misses four
     forms that `b11661169`'s regex catches:
     - `source_event IN ( 'operator')`;
     - `source_event = ( 'operator')`;
     - `source_event LIKE ( "operator")`;
     - `source_event IN ( ?)`, with the word as its parameter.
   - **The smallest change:** `\(?\s*` where review r3 has `\(?`. It keeps the
     decision's intent: every form review r3's regex catches is caught, and
     nothing `b11661169`'s caught is lost.
   - **The evidence (a plain-Python probe, not pytest):** every guard test
     function was called directly under each regex, 55 checks in all.
     - `b11661169`'s regex passes 48, and fails the 7 new forms.
     - Review r3's, as written, passes 54, and fails only the space case
       below.
     - The head's passes all 55.
   - **Newly caught.** Each form is a case of `GUARD_CASES_R3` (`:1206`), with
     its own id, in `test_r3_p3_1_the_source_guard_catches_each_sql_form_it_reads`:
     - `IN ('wham','operator')` (`IN, the word a later item`);
     - `NOT IN`, `IS NOT`, `NOT LIKE` and `GLOB`;
     - `'operator' = source_event` (`the word first`);
     - `lower(source_event)='operator'`.

     An eighth case, `source_event IN ( 'operator')` (`a space after the (,
     caught at b11661169`), pins the deviation. It passes at `b11661169` and at
     the head, and fails under review r3's regex as written.
   - **Still holds:**
     - The tree is `[]` over `subfleet/` and `tools/`, M18's two regex strings
       in the mutation driver included.
     - r1's 7 cases and r2's 22 name the same lines.
     - r2's 5 misses stay missed.
     - Both pass cases pass.
2. **Every remaining miss is pinned.** `GUARD_MISSES` (`:1136`) gains ten
   cases, one each, which `test_r2_p3_4_the_source_guard_known_misses_stay_listed`
   runs:
   - **SQL:** `COALESCE(source_event,'')='operator'`,
     `source_event BETWEEN 'operator' AND 'operator'`,
     `CASE source_event WHEN 'operator' THEN 1 END`, and `source_event=:s`
     with `{"s": "operator"}`.
   - **Python:** `== f"{HOLD_SOURCE}"`, `== "oper" + "ator"`,
     `.__eq__("operator")`, `== hold_source(None)`,
     `.lower() == "OPERATOR".lower()` and `in {"operator": 1}`.

   With r2's five, 15 misses are pinned. `_comparisons`' docstring (`:792`)
   names what is caught and each of the 15.
3. **C-9.6 states the guard exactly** (`docs/acceptance-contract.md:441`).
   - **The SQL clause** names what `SQL_COMPARISON` reads, in any letter case.
     One form is the column first:
     - `source_event`, with an optional `)` after it (`lower(source_event)`);
     - then `=`, `==`, `!=`, `<>`, `IN`, `IS`, `IS NOT`, `LIKE` or `GLOB`,
       each optionally after `NOT`;
     - then an optional `(`, and any text without parentheses that ends in a
       comma (a list's earlier items);
     - then the word in either quote, or a `?` whose call has the word or
       `HOLD_SOURCE` among its other arguments.

     The other is the word first: then `=`, `==`, `!=`, `<>` or `IS`, then
     `source_event` after nothing but letters, digits, `_` and `(`.
   - **The Python clause, corrected too:** review r3 found that the guard also
     catches `set(…)`, `tuple(…)` and `list(…)` of one, while the sentence
     named only `frozenset(…)`. It now names all four, each of one argument
     (as `_holds_operator` requires), nesting included.
   - **"Among what it does not see, each pinned by a test case"** names all 15
     pinned misses, r2's five included. r2's "a comparison made through
     `getattr`" now reads "the word reached through `getattr`", because
     `getattr` on the column is caught (r2's case 10).
   - **Why "among":** the list is the pinned misses, not every miss; the catch
     clause bounds the guard.
     - **Outside the clause:** the probe found these misses, which no case
       pins:
       - `? = source_event`;
       - the word first with `IN` or `LIKE`;
       - `'operator' = lower( source_event)` (a space in the call);
       - `'operator' = c.source_event` (a qualified column after the word).
     - **The wording excludes each:** the `?` and the list skip come only
       after the column, the word-first operators are five, and only
       letters, digits, `_` and `(` may stand before the column.
     - **The column qualified and first is caught:**
       `c.source_event = 'operator'`.
4. **The change log.** The D-STATUS-SRC1 entry
   (`docs/acceptance-contract.md:12`) gains a "Review r2 of #263 (2026-10-26)"
   clause. It covers r2's three C-9.6 changes and this round's sentence:
   - **The JSON form:** `hold_note` refuses a note that the scrubber would
     change as `json.dumps` writes it (round r2's decision 1).
   - **The strict read:** `hold_note_of`, through `closure_row` and
     `decision_rows`, withholds any hold source but `operator`, or
     `operator: <note>` with an accepted note. That covers every reader of
     closure rows, and the route decisions: `why` (`daemon.py:3630`, `:3890`),
     `run --dry-run` (`:2598`) and `runs show` (`offline.py:294`).
   - **The guard:** syntactic only and, since review r3, stated exactly.
5. **The report.** This section, and the line numbers in round r2's reader
   table. The merge with release/217 moved its five `daemon.py` lines by +9:
   they now read 3620, 3623, 3630, 3729 and 3890. Each was rechecked at the
   head:
   - `:3620` is the `lanes` list's `closure_rows`;
   - `:3623` is `readings`' `closure_rows`;
   - `:3630` is `why --task/--tier`'s `decision_rows`;
   - `:3729` is `daemon.status`'s `closure_rows`;
   - `:3890` is `why <job>`'s `decision_rows`.

   The table's other references did not move, and were rechecked too:
   `daemon.py:1398`, `:1433` and `:2598`, `operations.py:37`, `offline.py:294`
   and `:359`, and the writers' `daemon.py:1275` and
   `retention_archive.py:312`.

### Mutants, round r3

`tools/status_hold_source_mutations.py` gains M18: `SQL_COMPARISON` as review
r2 left it (at `b11661169`). Per the Budget hold, only M18 ran, in one hold,
from a `git archive` export of `90099a0d3`. The driver ran its baseline first:
the 1 named test, all 8 cases, passed.

| Mutant | What it takes back | Named tests that failed | Result |
| --- | --- | --- | --- |
| M18 | review r3's SQL check, back to `b11661169`'s regex | `test_r3_p3_1_…` ×7: `IN, the word a later item`, `NOT IN`, `IS NOT`, `NOT LIKE`, `GLOB`, `the word first`, `lower(source_event)` | killed |

The space case passes under M18, as it should, since `b11661169`'s regex
catches it. Instead, it guards the deviation: under review r3's regex as
written it fails (the probe above). That regex was not run as a pytest mutant,
because the brief named M18 alone.

### Runs, round r3

Every pytest went through `suite_hold.sh`, one at a time.
- **Environment:** as in round r2. `TMPDIR` and
  `HYPOTHESIS_STORAGE_DIRECTORY` were in the scratch folder, `PYTHONPATH` and
  `SUBFLEET_RETENTION_DORMANT` were unset, and `PYTHONDONTWRITEBYTECODE=1` was
  set.
- **Python:** 3.14 (`uv sync --locked --group dev`), serial. No network.
- **Times and loads:** UTC on 2026-10-26; loads are the 1-minute
  `vm.loadavg`.

| Hold | Tree | What ran | Result | Load at start → end |
| --- | --- | --- | --- | --- |
| red, queued 12:35, ran 12:40 | `f354b6195`: `b11661169`'s code with the red tests | the guard's tests in `test_status_hold_source.py` (`-k`: `test_r3_p3_1`, `test_r2_p3_4`, `test_r1_p3_3`, `test_nothing_reads`) | **7 failed, 38 passed** in 11 s, as intended. The 7 are the new forms. The 38 are the space case, r1's 7 cases and pass case, r2's 22 cases, 5 misses and pass case, and the tree | not recorded → 45.4 |
| guard check, queued 12:43, ran 12:51 | the step-2 change before it was committed as `2caefa4ad` (unchanged since) | the same `-k` | **55 passed** in 14 s | 44.3 → 41.1 |
| 1, 12:54 | `90099a0d3` | `test_status_hold_source.py` | **133 passed** in 162 s: r2's 115, the 8 r3 cases and the 10 new misses | 59.4 → 38.9 |
| 2, 12:57 | `90099a0d3` | the three contract guards | **63 passed** in 51 s: `fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25 | 27.6 → 18.2 |
| mutant, 12:59 | export of `90099a0d3` | M18 | baseline passed (1 test); **1 of 1 killed** | 15.1 → 15.8 |

The head adds only this report to `90099a0d3`, and no test reads it.

**Plain-Python probes (not pytest):**
- 26 forms under `b11661169`'s regex and review r3's, and then with the `\s*`
  kept;
- every guard test function under the three regexes;
- the five forms outside C-9.6's catch clause, and two inside it (the column
  qualified and first, and another letter case);
- M18's rule applies once and gives `b11661169`'s regex exactly.

### Not run, round r3

- **The rest of the suite**, as the brief asked; CI runs it. No code under
  `subfleet/` changed this round. So the files that test it, such as
  `frontend/test_status_model`, `test_cli`, `test_render`, `test_status_json`,
  `test_offline` and `test_operator_notices`, were not rerun.
- **Mutants:** M1 to M17 were not rerun (the Budget hold). Review r3's regex as
  written was not run as a mutant; the probe covers it.
- **Off the lane:** `tests/frontend/test_core_live.py` can't run on a lane, and
  the old-release Swift decoder check was not run.
