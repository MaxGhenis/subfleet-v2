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
