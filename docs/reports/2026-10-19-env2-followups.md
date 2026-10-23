# Review r5 of #222: refused lanes.json rows, held lanes and the lane-variable checks (D-ENV2)

Base: `origin/release/217` at `473fe2941`, which has #222 (the launch-environment
allowlist, merged as `f99da54b0`). Release at `fd1e4b65c` is merged in since
(`38afc8110`, "The release merge" below), and every count in Validation is from
that merged head. Review r5
(`~/reviews/subfleet-hub/launch-env-2026-10-14/review-r5.md`) approved #222 with
three P3s, and the hub ruled on each. The hub also asked for C-1.3's wording and an
install check. This report gives each cause as executed at the base, then the fix,
then the tests. No credential store, secret loader, live store, live daemon or live
process was read or changed. Every test value is a dummy, and nothing here quotes an
environment value.

**History.** Four jobs wrote this.
- **The first build** (`20261020-073952-env2-build`) was lost to the
  2026-10-20 power cut, at about 15:25Z, with no final answer. Before that it
  made the seven commits from `251625c37` to `290b7bb5a` and wrote this report
  up to its test sections. The hub committed that draft unchanged as `757bb71d9`.
- **The follow-on job** (`20261020-191828-env2-build-b`) was pinned to one lane
  that stayed closed for two days, and never started.
- **The third job** (`20261022-125125-env2-build-c`) checked each of the
  brief's items against the commits and found them all done. It merged
  release/217 and re-ran every test count in the Validation section at the
  merged head. Then it finished this report and wrote the PR body,
  `docs/reports/2026-10-19-env2-followups-pr-body.md`.

- **The fourth job** (`20261023-043149-pr248-fix1`) fixed review r1 of #248's
  three P3s at `364fefdbf`. Its work is the "Fix round 1" section at the end.

The causes, the fix, the choices, the control at the base and the mutations
below are the first build's work and wording. The Validation, Not run, Commits
and Cleanup sections are the third job's.

## The causes, as executed at the base

A probe script ran review r5's inputs against an export of `473fe2941` and against
the fix, on scratch state roots with dummy values. It used `cli.daemon_env`, the
check `daemon start` runs before it spawns; daemon startup runs the same
`check_lane_credentials` on the same `startup_lanes` list. The pytest regressions
below rerun each input through both callers. At the base:

| Input | Base | Fix |
| --- | --- | --- |
| P3-1, input 1: claude-9 stored on `HOME` (round 4's store), its `lanes.json` row unchanged | exit 2, `credential_ref` | starts; one warning: held, and the row ignored |
| P3-1, input 2: claude-9 stored and healthy on `SUBFLEET_TEST_OAUTH`, its row changed to `HOME` | exit 2 | starts; one warning: the row ignored; claude-9 launches on its own token |
| A refused row for a lane the store lacks | exit 2 | exit 2 (unchanged) |
| P3-1, input 3: `lanes_transfer.transfer` of a held claude-9 missing from `lanes.json` | `applied: true`; `lanes.json` gains `claude-9: HOME`; the next start exits 2 | refused, `transfer-held`, exit 7; no `lanes.json` written |
| P3-2: doctor's lane-variable row with claude-9 held | `pass`: "the daemon holds no env-kind lane variable" | `warn`, naming claude-9 and `HOME`, with its `lanes.json` edit |
| P3-3: a `lanes.json` row naming `PYTEST_CURRENT_TEST` | accepted, and `daemon start` hands the daemon that variable by name | refused, exit 2 |
| Install check | none | `daemon start --check` |

**Why P3-1 happened.** `credentials.startup_lanes` returned every env-kind roster
row with source `lanes.json`, whether or not the store already held that lane id.
`policy.check_lane_credentials` then refused any `lanes.json`-sourced row whose
variable is refused. But `Daemon._seed_lanes` skips a row whose lane id is stored,
apart from C-1.3's repair, so the refused row would never have been stored. The
refusal guarded nothing, and it stopped a daemon that round 5 had ruled must start
with only that lane held. Under launchd's `KeepAlive` the daemon came straight back
to exit 2, and every lane was down until someone edited `lanes.json`.

**Why the transfer wrote a refused row.** `lanes_transfer._v2_roster_edit` appends
the store's row to `lanes.json` when the file does not name the lane, so that a
rebuilt store re-seeds with the transfer's owner. For a held lane that row carries
the refused variable, and at the next start it was a refused `lanes.json` row for
a new lane: exit 2.

**Why P3-2 happened.** `doctor.check_daemon_lane_credentials` read only
`daemon.status.environment.lane_credentials`. `credentials.env_references` leaves
a held lane's variable out of that list, because it is no lane's credential. So
the row saw nothing to report.

**Why P3-3 happened.** `launch_env.refused_reference` was `inherited_name`: listed,
harness or session names. `PYTEST_CURRENT_TEST` is none of those, but it is one of
`SESSION_PROBE_NAMES`, whose values `_selected` reads to decide whether the parent
is in a live test session. A lane on it was accepted (`env_references` named it, so
`daemon start` passed it to the daemon by name). Resolution used `inherited_name`
too, so it read the value. C-12.3's "A withheld name is rejected before anything
reads its value" failed for that one name.

## The fix

**P3-1: a refused row for a stored lane is ignored** (`1a92f1a5b`).
- `startup_lanes(roster, stored)` tags a row `launch_env.IGNORED` ("lanes.json,
  ignored") when its variable is refused and the store already holds its lane id,
  of any credential kind. Every other row is `lanes.json` as before.
- `check_lane_credentials` refuses only a refused `lanes.json`-sourced row, so
  only a refused row for a new lane stops startup. It now returns both held
  stored lanes and ignored rows as (lane id, variable, source).
- `policy.startup_notes` turns them into one warning per lane:
  - **a held lane**, naming its variable and the fix, and adding "its lanes.json
    row names HOME too, so the row is ignored" when it has a refused row;
  - **a stored lane that is not held**, with only an ignored row: "lanes.json
    gives lane claude-9 HOME, a variable every launch inherits, which cannot hold
    one lane's credential; the store already holds lane claude-9 with its own
    credential, so the row is ignored; fix: make the row match the stored lane,
    or remove it; the store keeps the lane either way".
- Both callers print those warnings: `Daemon._open_lanes` to `daemon.log`, and
  `cli.daemon_env` as `daemon start`'s stderr notes. Seeding is unchanged: it
  never stores an ignored row, and a fixed row still repairs a held lane in
  place (C-1.3).
- C-10.5 now holds as written: a stored lane with a refused variable never stops
  the daemon, whatever `lanes.json` says about it.

**`lanes transfer` refuses a held lane** (`1a92f1a5b`). `plan_transfer` raises
`TransferError` (`transfer-held`, exit 7) right after the unknown-lane check, so
a dry run refuses too and nothing is planned or written. The public text names
the lane and, for a listed, session or probe name, the variable
("lanes transfer: lane claude-9 is held (credential-refused=HOME): it holds no
credential to move"). A harness hook takes the next form, which names no
variable, because `refusals` renders only values in a closed list. The fix is to
repair the lane first, restart, then transfer.

**P3-3: every session probe name is a refused lane variable** (`1a92f1a5b`).
`refused_reference(name)` is now `valid_name(name) and (inherited_name(name) or
name in SESSION_PROBE_NAMES)`. Every consumer of that rule follows: the start
checks, `env_references` (so the daemon is not handed it by name), the
scheduler's `credential-refused`, both status renderers and doctor. Resolution
now checks `refused_reference` before reading any value. The three markers were
already session names; only `PYTEST_CURRENT_TEST` changes. `launch_env.refused_phrase`
gives each refusal a reason that fits its name: "a variable every launch inherits",
"a test session's variable, which every launch in that session inherits", or "a
variable launch selection reads to find a test session".

**C-1.3's wording.** C-1.3 now says the fixed row "names a variable no launch
inherits, and none the session probe reads (C-10.5)". The fix texts that said
"off the launch allowlist" now say "no launch inherits": `policy.LANE_REFERENCE_FIX`
and the `credential-refused` pin notice in `render`.

**P3-2: doctor names each held lane** (`7a3791883`). The row applies the
scheduler's own rule (env kind and `refused_reference`) to `daemon.status`'s lane
rows. With any lane held it warns, naming each held lane and its variable. Its
fix is each lane's `lanes.json` row, for example
`{"lane_id": "claude-9", "provider": "claude", "account_key": "claude:lane-9",
"credential_kind": "env", "credential_ref": "<VARIABLE>"}` ("edit its row if it
has one, keeping the other fields"), then a restart. Those are the four fields
C-1.3's repair matches, plus the kind. The daemon's own lane variables are still
listed. A warning never decides doctor's exit status.

**The install check: `daemon start --check`** (`751432ce2`).
- **What it lists:** each env-kind `lanes.json` row whose variable is refused, as
  `<lane> <variable>` on stdout (`--json`: `{"lanes_json": ..., "refused":
  [{"lane_id", "variable"}]}`).
- **Exit status:** 2 when it lists any row, as startup would for a new lane; 2
  when the file is not a lane list, naming only the exception class; 0 otherwise.
- **What it reads:** `lanes.json` alone, through `read_regular`, so a FIFO at its
  name is refused, never waited on. It does not read the store, the policy, the
  daemon's socket or any variable's value, and it spawns nothing. It runs before
  `daemon start` builds a `Client`.
- **What it cannot say:** without the store it cannot tell which rows startup
  would refuse (new lanes) and which it would ignore (stored lanes). Its note
  says both, and gives the fix.

## Choices

- **Refuse the transfer rather than drop the row.** The hub left this open.
  - A held lane holds no credential, so it can serve neither owner. In v1 it
    would be enrolled under the refused variable: `_claude_roster_edit` falls
    back to the lane's `credential_ref`.
  - Leaving its row out of `lanes.json` would break the module's own rule that
    `lanes.json` carries the store's owner, so that a rebuilt store re-seeds
    with it.
  - Refusing changes nothing and names the repair. Once the lane is repaired,
    the transfer goes through unchanged.
- **`daemon start --check` rather than a doctor row.** The hub runs the check
  against the live install before installing release/217, and it must read
  nothing but `lanes.json` and the allowlist. A doctor row could meet that by
  itself, but `subfleet doctor` cannot run one row alone. A whole run asks the
  live daemon for its status, reads the store, and runs the provider binaries
  and the guard preflight. A `--check` path on the verb that would refuse makes
  the promise checkable: a test runs it with the store, the policy, the client,
  the spawn and every variable's value made unreachable.
- **One warning per lane.** A held lane with its own refused row is one lane
  with one fix, so it gets one warning (the regression asserts exactly one line
  naming it, at both callers and on the KeepAlive restart).
- **Ignored, whatever the stored lane's kind.** A refused row is ignored for a
  stored home-kind or keychain lane too. Seeding skips that row as well, so
  refusing it would wedge the daemon just as before.
- **No new doctor read.** Doctor reuses the `daemon.status` call it already
  makes and the scheduler's predicate, rather than a new status field. A daemon
  old enough to lack `lane_credentials` still reads `unknown`, before any lane
  is judged.

## Invariants

Each is stated for every input and checked by a property or differential test
(Hypothesis, derandomized and bounded):

- **Only a refused row for a new lane refuses startup.** For every `lanes.json`
  and store pair (up to 4 rows and 3 stored lanes, five refused and two good
  variables, home-kind stored lanes, and `env.pass`): startup is refused exactly
  when `env.pass` names a lane credential, or a refused row's lane id is not
  stored. Every other refused row is ignored. Each held lane and each lane with
  an ignored row gets exactly one warning, which says "held" only for a held
  lane, and no warning names any other lane
  (`test_bounded_property_only_a_refused_row_for_a_new_lane_refuses_startup`,
  300 examples, pure). Round 5's daemon-level property runs the same rule
  through `cli.daemon_env` and real `Daemon` constructions
  (`test_bounded_property_stored_lanes_never_refuse_startup`, 25 examples).
  Its model now encodes this ruling, and nothing else changed in it.
- **The install check agrees with startup.** For every roster of env, home and
  keychain rows (including a variable that is no exact name): with an empty
  store, startup refuses exactly the lanes the check lists, naming each. With
  every lane stored, it refuses none of them and warns once per listed lane
  (`test_bounded_property_the_install_check_lists_what_startup_refuses_or_ignores`,
  200 examples).
- **No transfer writes a refused row.** After every transfer of every stored
  lane to either owner, no env-kind `lanes.json` row names a refused variable
  (`test_no_transfer_writes_a_refused_row_into_lanes_json`). Review r1 of #248
  found this held only when `lanes.json` started without one: a transfer kept
  the lane's existing refused row. Fix round 1, below, repairs that row, and
  states the invariant exactly.
- **No refused variable's value is read by resolution or by the install check**
  (a recording environment, for each probe name and for each listed row).

## Contract

Amended in the same change: C-1.3 (the wording), C-10.4 (a held lane does not
transfer), C-10.5 (ignored rows, the probe names, doctor, the transfer and the
install check), C-11.1 (only a new lane's refused row refuses startup), C-12.3
(why the probe's own reads hold no credential), C-17.1 (`daemon start
[--check] [--json]`) and C-17.8 (the lane-variable row warns). The change log has
one entry for them, dated 2026-10-20.

## Tests

### Per file

These are the brief's files, run by the third job at the merged head `38afc8110`,
plus `tests/fake/test_launch_env_daemon.py`, which this change edits. Each hold
is one pytest process (no workers) on GIL CPython 3.14.7, in a venv built once
with `uv sync --locked --group dev`. No `tests/unit/test_credentials*.py` exists.

| Hold | File | Passed | Failed |
| --- | --- | ---: | ---: |
| 1 | `tests/unit/test_launch_env_allowlist.py` | 27 | 0 |
| 1 | `tests/unit/test_launch_env_helpers.py` | 20 | 0 |
| 1 | `tests/unit/test_launch_env_mutations.py` | 44 | 0 |
| 1 | `tests/unit/test_launch_env_policy.py` | 26 | 0 |
| 1 | `tests/unit/test_launch_env_round2.py` | 67 | 0 |
| 1 | `tests/unit/test_launch_env_round3.py` | 69 | 0 |
| 1 | `tests/unit/test_launch_env_round4.py` | 40 | 0 |
| 1 | `tests/unit/test_launch_env_round5.py` | 39 | 0 |
| 1 | `tests/unit/test_launch_env_round6.py` (new) | 43 | 0 |
| 1 | `tests/unit/test_lanes_transfer.py` | 79 | 0 |
| 2 | `tests/unit/test_doctor.py` | 44 | 0 |
| 2 | `tests/unit/test_cli.py` | 248 | 0 |
| 2 | `tests/unit/test_daemon_accept.py` | 5 | 0 |
| 3 | `tests/unit/test_live_store_guard.py` | 324 | 0 |
| 3 | `tests/unit/test_live_store_guard_round3.py` | 22 | 0 |
| 3 | `tests/unit/test_live_store_guard_round4.py` | 8 | 0 |
| 3 | `tests/unit/test_live_store_guard_round5.py` | 11 | 0 |
| 3 | `tests/unit/test_live_store_guard_round6.py` | 112 | 0 |
| 3 | `tests/unit/test_live_store_guard_round7.py` | 50 | 0 |
| 3 | `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 0 |
| 3 | `tests/unit/test_live_store_guard_round8.py` | 100 | 0 |
| 3 | `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 0 |
| 3 | `tests/unit/test_live_store_guard_round9.py` | 89 | 0 |
| 3 | `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 0 |
| 3 | `tests/unit/test_live_store_guard_round10.py` | 60 | 0 |
| 3 | `tests/unit/test_live_store_guard_round10_mutations.py` | 13 | 0 |
| 3 | `tests/unit/test_live_store_guard_state_root.py` | 22 | 0 |
| 3 | `tests/fake/test_state_contract.py` | 37 | 0 |
| 3 | `tests/unit/test_test_module_names.py` | 1 | 0 |
| 3 | `tests/fake/test_launch_env_daemon.py` (changed) | 52 | 0 |
| | **Total** | **1,691** | **0** |

The brief's files alone are 1,639 passed; the changed fake file adds 52.
The named mutations pass, which means each mutant is killed by its test:
`test_named_ignored_row_regression_kills_refusing_a_stored_lanes_row` (both
callers), `test_named_install_check_regression_kills_reading_a_value` and the
amended `test_named_first_start_regression_kills_seed_before_check`, all in
`test_launch_env_mutations.py`.

### Control: the regressions at the base

The control ran the new and changed test files against an export of
`473fe2941`, the base: its `subfleet/`, `tests/`, `bin/` and `pyproject.toml`,
with only `tests/unit/test_launch_env_round6.py` and
`tests/fake/test_launch_env_daemon.py` copied in from `5bb81a3aa`. The job's
`.venv` ran it from the export, and `pythonpath = ["."]` made it import the
base. The round 6 file reads its one new name (`IGNORED`) with a fallback, as
round 5's tests did, so every test there runs at the base and fails by its own
assertion. The result was 40 failed and 55 passed in 80 s.

| Test (cases) | At the base |
| --- | --- |
| `test_a_refused_row_for_a_stored_lane_is_ignored_and_daemon_start_goes_on` (5) | `PolicyError` (exit 2) |
| `test_a_refused_row_for_a_new_lane_still_refuses_beside_an_ignored_one` | "the stored lane's row is ignored, not refused" |
| `test_each_held_lane_and_ignored_row_gets_one_warning` | `PolicyError` |
| `test_bounded_property_only_a_refused_row_for_a_new_lane_refuses_startup` | 6 distinct failures |
| `test_lanes_transfer_refuses_a_held_lane` (8) | "DID NOT RAISE TransferError" |
| `test_no_transfer_writes_a_refused_row_into_lanes_json` | `lanes.json` names claude-9 and claude-6 on refused variables |
| `test_doctor_names_each_held_lane_with_its_fix` | `pass`, not `warn` |
| `test_every_session_probe_name_is_a_refused_lane_variable[PYTEST_CURRENT_TEST]` | not refused |
| `test_daemon_start_check_lists_each_inheriting_env_row_and_reads_only_lanes_json` (5) | "the check reached Client": no `--check`, so it went on to start |
| the other `daemon start --check` tests (9) and the parser test | argparse refuses `--check` (exit 2, usage) |
| `test_bounded_property_the_install_check_lists_what_startup_refuses_or_ignores`, `test_a_probe_name_says_why_it_is_refused` | the new functions do not exist |
| fake `test_review_r5_a_refused_row_for_a_stored_lane_is_ignored` (2) | `daemon start` exits 2 |
| fake `test_review_r5_a_refused_row_never_displaces_a_healthy_stored_lane` | `daemon start` exits 2 |
| fake `test_bounded_property_stored_lanes_never_refuse_startup` | "daemon start refused a startup the rule allows" (its model now follows the ruling) |

Seven round 6 cases passed at the base, as they should. Three are the marker
probe names, which were already refused. The others are the healthy-lane
transfer control, doctor's row with no lane held, a plain `daemon start`, and
the source tags. The fake file's 48 older tests passed.

### Mutations

`tests/unit/test_launch_env_mutations.py` runs each mutant in memory against its
named regression:

| Mutant | Killed by |
| --- | --- |
| an ignored row checked as a new lane's row again, at `daemon start` (`cli.startup_lanes`) | `test_review_r5_a_refused_row_for_a_stored_lane_is_ignored`: `daemon start` exits 2 |
| the same at daemon startup (`daemon.startup_lanes`) | the same test: launchd's start exits 2 |
| the install check reading each listed variable's value (`os.environ.get`) | `test_daemon_start_check_lists_each_inheriting_env_row_and_reads_only_lanes_json`: "the check read a lane variable's value" |

One existing mutation test changed. `test_named_first_start_regression_kills_seed_before_check`
seeds `lanes.json` before the check. Its oracle used to fail on "the check runs
before lanes.json is seeded", because the check refused the seeded row. Now the
check finds that lane stored and ignores its row, so the mutant's daemon serves
(holding a lane it should never have stored), and the oracle fails on "a refused
roster served". The mutant is still killed. The test accepts either failure of
its oracle and says why.

### Validation

All counts are from the third job's runs at the merged head `38afc8110`. Each hold
is one pytest process under the hub's `suite_hold.sh`, which queues for a slot of
the shared suite lock. The whole-suite holds ran with `--session-timeout`, which
stops pytest between tests, to keep each hold under 20 minutes. Files run to
completion are counted; a file a timeout cut short was run again whole in the next
hold, when one was left.

| Hold | Files | Waited | Ran | Result |
| --- | --- | ---: | ---: | --- |
| 1 | brief: `test_launch_env*`, `test_lanes*` | 3 min 22 s | 3 min 44 s | 454 passed |
| 2 | brief: `test_doctor`, `test_cli`, `test_daemon_accept` | 18 min 34 s | 1 min 51 s | 297 passed |
| 3 | brief: `test_live_store_guard*`, state contract, module names; the fake daemon file | 4 min 36 s | 9 min 44 s | 940 passed |
| 4 | suite: 70 files whole, then `test_record_secrecy` (cut) | 4 min 40 s | 17 min 15 s | 2,043 passed, 2 failed |
| 5 | suite: 22 files whole, then `test_compat` (cut) | under 1 s | 16 min 50 s | 2,603 passed, 9 failed |
| 6 | suite: `test_compat` whole, then `test_unadmittable_pin` (cut) | 59 min 31 s | 18 min 33 s | 1,616 passed |
| 7 | the three failing fake files again, then 31 suite files whole and `test_reset_credit_incident` (cut) | 86 min 17 s | 15 min 1 s | 312 passed, 1 failed |

**The whole-suite rule, per directory** (files run to completion, holds 1 to 7):

| Directory | Files run | Passed | Failed | Files unrun |
| --- | ---: | ---: | ---: | ---: |
| `tests/unit` | 97 of 335 | 5,811 | 0 | 238 |
| `tests/fake` | 57 of 112 | 795 | 12 | 55 |

In all, the seven holds executed 8,277 tests: 7,385 in `tests/unit` and 892 in
`tests/fake`. Besides the table, that total includes:
- 31 reruns;
- 1,628 tests from the four cut runs: 43 in `test_record_secrecy`, 1,573 in
  `test_compat`, 1 in `test_unadmittable_pin` and 11 in
  `test_reset_credit_incident`, all passed.

**The 12 failures are one failure mode, the fake daemon harness's 5 s start
bound.** Daemon startup does run this change's check (`Daemon._open_lanes`). The
daemons that logged their reset-credit line had passed it, since that line comes
after it in `Daemon.__init__`. None of them logged a refusal or an error:
- **Which tests:**
  - hold 4: `test_resume_cli.py::test_resume_cli_decodes_raw_show_row_and_continues_native_session`
    and `test_census_escapers_daemon.py::test_d_cen1_a_retitled_child_alive_after_the_exit_receipt_is_not_released[retitled-node]`;
  - hold 5: nine consecutive tests of `test_daemon_contract.py`, from
    `test_c4_2_starting_recovers_late_or_missing_receipt[late-receipt]` to
    `test_c8_3_unwritable_export_keeps_success_and_records_error`;
  - hold 7: `test_lost_acknowledgement.py::test_c16_3_a_kill_whose_answer_is_lost_reports_what_the_job_became`.
- **How each failed:** `Harness.start` raised "condition timed out after 5s" (`tests/fake/conftest.py:145`).
  The kept state roots held a `daemon.log` of one or two startup lines (the
  open-file limit, then the reset-credit line), or none at all. No log had an
  error, a refusal, a traceback or a credential line. The daemons were still
  starting when the bound ran out.
- **The host:** the 1-minute load was 41 to 49 when these holds began and
  reached 90 during the job, with about 47 `suite_hold.sh` processes queued or
  holding host-wide. The other daemon-harness tests in the same holds started
  and passed. Among them were 12 more in `test_daemon_contract.py`, 8 more in
  `test_census_escapers_daemon.py` and 2 more in `test_lost_acknowledgement.py`.
- **The reruns:** hold 7 ran the three files of the first eleven failures
  again, whole, first: `test_resume_cli.py` 1 of 1, `test_census_escapers_daemon.py`
  9 of 9 and `test_daemon_contract.py` 21 of 21 passed. The twelfth,
  `test_lost_acknowledgement.py`, failed in hold 7, after the 4 h 30 m cutoff
  had closed reruns; it has not been run again.

### Not run

- **293 of 447 files in `tests/unit` and `tests/fake`:** 238 in `tests/unit` and
  55 in `tests/fake`, unreached at the 4 h 30 m cutoff. Runs took 2.6 to 20 times their CI time from `.github/test-shards.json`
  (hold 2's three files: 111 s here, 37 s in CI), and slot waits reached 86
  minutes. `pytest-xdist` is not in the lock, and the job's rules forbade
  fetching it, so every hold used one process. The unrun files are below.
- **A rerun of `test_lost_acknowledgement.py`** after its hold 7 start timeout.
- **The control at the merged release.** The table above comes from the first
  build's run against `473fe2941`. The third job exported `fd1e4b65c` with the
  two new and changed test files, to run them again against today's release,
  but no hold was left before the cutoff.
- **Outside the brief:** `tests/e2e`, `tests/process`, `tests/frontend` and
  `tests/live`; Python 3.12 and free-threaded 3.14t; CI on GitHub, since the job
  had no network.

<details><summary>The 293 unrun files</summary>

`tests/fake/` (55): `test_admission_held_waiters`, `test_admission_latency`, `test_admission_lease_waiters`, `test_admission_liveness`, `test_admission_priority`, `test_admission_properties`, `test_admission_ramp`, `test_admission_stateful`, `test_admission_visibility`, `test_canonical_identity`, `test_canonical_identity_fix2`, `test_canonical_identity_fix3`, `test_canonical_identity_fix5`, `test_capacity_wait_backoff`, `test_codex_ca_bundle_fake`, `test_codex_turn_completed_fake`, `test_crash_matrix`, `test_descriptor_exhaustion`, `test_detached_cwd_pins`, `test_detached_retention_fence`, `test_frozen_stream_nonregular`, `test_gate_end_to_end`, `test_idle_cost`, `test_interactive_claude_titles`, `test_lane_fault`, `test_lane_pilot`, `test_launch_gates_above_stdio`, `test_native_maintenance_startup`, `test_notice_delivery`, `test_probe_cancel`, `test_probe_turn`, `test_provider_active_cap_fake`, `test_provider_qos`, `test_reset_credit_incident` (11 of its tests passed before the cut), `test_review130_ab`, `test_review_r223_wall_probes`, `test_routing_end_to_end`, `test_salvage_adversarial`, `test_salvage_finalization`, `test_sessions_end_to_end`, `test_suggestions_fake`, `test_timers_end_to_end`, `test_turn_retention_cwd`, `test_turn_retention_cwd_properties`, `test_turn_retention_job_directory`, `test_turn_retention_job_properties`, `test_turn_retention_quarantine`, `test_turn_trees`, `test_wall_cancel_races`, `test_wall_inspection_order`, `test_wall_running_time`, `test_wall_stop_deliveries`, `test_workspace_transient`, `test_writable_fanout`, `test_writer_retention_fence`.

`tests/unit/` (238): `test_admission_uncapped`, `test_approval_commit`, `test_approval_resolution`, `test_boot_identity`, `test_capacity_desktop`, `test_capacity_steer_mutations`, `test_capacity_steer_route`, `test_capacity_view`, `test_census_counted`, `test_census_identity_uncertainty`, `test_census_procargs`, `test_census_recycled_pid`, `test_census_responsibility_failures`, `test_census_responsibility_identity`, `test_census_responsibility_races`, `test_census_survivors_review`, `test_ci_retry_known_flakes`, `test_ci_shard_plugin`, `test_claude_attest`, `test_claude_cards`, `test_claude_cards_wiring`, `test_claude_conversation_mcp_scope`, `test_claude_identity`, `test_claude_large_stream`, `test_claude_mcp`, `test_claude_mcp_launch`, `test_claude_stream`, `test_claude_synthetic_error`, `test_claude_turn`, `test_claude_usage`, `test_claude_v1_parity`, `test_codex_ca_bundle`, `test_codex_ca_bundle_provider`, `test_codex_probe`, `test_codex_shim`, `test_codex_turn`, `test_codex_turn_completed`, `test_continuation_r2`, `test_conversation_catalog`, `test_conversation_catalog_lifecycle`, `test_conversation_connections`, `test_conversation_diff`, `test_conversation_fifo_reads`, `test_conversation_handoff`, `test_conversation_handoff_briefs`, `test_conversation_handoff_dispatch_races`, `test_conversation_history`, `test_conversation_peers_attachments`, `test_conversation_reconcile`, `test_conversation_redact`, `test_conversation_service`, `test_conversation_state_reads`, `test_conversation_steer_service`, `test_conversation_stream_assertion`, `test_conversation_titles`, `test_conversation_waits`, `test_conversation_wakes`, `test_cpu_scaling`, `test_decision_horizon`, `test_descriptor_review_fixes`, `test_descriptors`, `test_descriptors_desktop`, `test_desktop_account_cache`, `test_desktop_in_use`, `test_desktop_ledger`, `test_disk_admission`, `test_disk_floor`, `test_empty_provider_results`, `test_failed_keep`, `test_failed_keep_layout`, `test_failed_keep_prune`, `test_failed_keep_teardown`, `test_fake_codex_http`, `test_filesystem_identity_agreement`, `test_folders`, `test_forget_paced`, `test_fresh_until`, `test_frontend_probe_cache`, `test_frozen_provider_streams`, `test_gate_admission`, `test_gate_merge`, `test_gate_replay`, `test_gate_revision`, `test_gate_service`, `test_gate_verdict`, `test_held_waiter_holdback`, `test_hook_cost`, `test_hooks`, `test_host_push`, `test_host_push_admission`, `test_host_push_build`, `test_host_push_build_budget`, `test_host_push_config_cost`, `test_host_push_copy`, `test_host_push_export`, `test_host_push_intake`, `test_host_push_metadata`, `test_host_push_mutations`, `test_host_push_names`, `test_host_push_quarantine_restart`, `test_host_push_restart`, `test_host_push_retention`, `test_host_push_storage_recovery`, `test_host_push_storage_retry_budget`, `test_host_push_trees`, `test_host_push_validated_top`, `test_importer`, `test_inspection_pacing_properties`, `test_invariants_index`, `test_lane_answers`, `test_lease_waiter_selection`, `test_legacy_hold`, `test_limit_from_model_text`, `test_lockwatch`, `test_mirror_flags_faults`, `test_mirror_flags_model`, `test_mirror_flags_stateful`, `test_native_operations`, `test_notify_push`, `test_operator_hold_closures`, `test_operator_notices`, `test_outcome_unknown`, `test_person_boundary`, `test_person_boundary_identity`, `test_picker`, `test_placement_ramp`, `test_pr124_fixes`, `test_pr124_review_r2_fixes`, `test_pr192_review_r1`, `test_pr192_review_r2`, `test_procs`, `test_protocol_secrecy`, `test_provider_active_cap`, `test_prune_217`, `test_prune_decision_backup_verification`, `test_prune_decision_fingerprints`, `test_prune_decisions`, `test_quota_projection`, `test_rank_review_report`, `test_reap_liveness`, `test_rediscovery_git_trace`, `test_relay`, `test_reserve_authorization_plumbing`, `test_reset_credit_claim_timing`, `test_reset_credit_demand`, `test_reset_credit_properties`, `test_reset_credit_review_r1`, `test_reset_credit_review_r2`, `test_reset_credit_review_r3`, `test_reset_credit_review_r3_mutants`, `test_resource_identity`, `test_retention_81_notes`, `test_retention_archive`, `test_retention_archive_properties`, `test_retention_case_alias`, `test_retention_nested_leases`, `test_retention_ordinary_trees`, `test_retention_qos`, `test_retention_readback`, `test_retention_rediscovery`, `test_retention_regenerable`, `test_retention_regenerable_properties`, `test_retention_salvage_properties`, `test_retention_shared_folders`, `test_retention_sweep_race`, `test_retention_turn_workdirs`, `test_retention_turns`, `test_retention_worktrees`, `test_review130_pass`, `test_review_pr127_fixes`, `test_review_pr127_history`, `test_review_pr127_pr_wakes`, `test_review_pr127_report`, `test_review_pr127_requests`, `test_review_pr127_service`, `test_review_r223_conv_probes`, `test_review_r2_repro`, `test_review_r3_measure`, `test_review_r3_repro`, `test_review_r4_repro`, `test_review_r5_repro`, `test_review_r6_probes`, `test_review_r6_restart`, `test_route_check`, `test_salvage`, `test_salvage_optional_transient`, `test_salvage_ref_race`, `test_salvage_unindexable`, `test_scoped_stream_capacity`, `test_sessions_handoff`, `test_sessions_handoff_excerpts`, `test_sessions_headless_properties`, `test_sessions_mirror`, `test_sessions_mirror_cache_coherence`, `test_sessions_mirror_fifo`, `test_sessions_mirror_hot_health`, `test_sessions_mirror_hot_progress`, `test_sessions_mirror_hot_regressions`, `test_sessions_mirror_inventory`, `test_sessions_mirror_load_gap`, `test_sessions_mirror_safety`, `test_sessions_mirror_write_churn`, `test_sessions_nudge`, `test_sessions_registry`, `test_sessions_revive`, `test_sessions_revive_reads`, `test_sessions_transcripts`, `test_settle_properties`, `test_soak_tools`, `test_steer_invariants`, `test_stop_alarm`, `test_stop_watchdog`, `test_suggest_hint`, `test_suggestion_error_boundaries`, `test_suggestion_properties`, `test_suggestion_secret_properties`, `test_suggestion_structural_errors`, `test_suggestions`, `test_suggestions_r1b_adversarial`, `test_suggestions_r3_channels`, `test_suggestions_review_boundaries`, `test_suggestions_review_multiline`, `test_suggestions_review_response`, `test_tick_prefilter`, `test_timers_alerts`, `test_timers_busy_read`, `test_timers_keepalive`, `test_timers_probe`, `test_timers_reset_credits`, `test_timers_retention`, `test_title_review_regressions`, `test_turn_attachment_replay`, `test_turn_overlap`, `test_turn_replay`, `test_turn_runner`, `test_ultracode_effort`, `test_wait_hub`, `test_weekly_rank_vs_base`.

</details>

The files run first were picked by name for the code this change and the merge
touch: launch environment, credentials, policy, scheduler, render, refusals,
doctor, daemon, lanes, store, offline, compat, the adapters, isolation, the
stopped daemon, CLI and status. The rest of `tests/fake` came next, then the
rest of `tests/unit`. Within each group, files ran in rising order of their CI
time. The unrun files are the remainder: retention, sessions, conversations,
suggestions, host push, gates, the turn runner, timers and the slowest fake
files.

## Commits

On `fix/env2-followups-217`, first-parent from `473fe2941`:

| Commit | What |
| --- | --- |
| `251625c37` | test: the regressions for review r5's P3s, and the named mutations |
| `1a92f1a5b` | fix: a refused `lanes.json` row for a stored lane is ignored; `lanes transfer` refuses a held lane; the probe names are refused; "no launch inherits" |
| `7a3791883` | fix: doctor names each held lane with the `lanes.json` edit that repairs it |
| `751432ce2` | feat: `daemon start --check`, the install check |
| `5bb81a3aa` | fix: judge a transferred lane by `lane_reference`; doctor's held-lane wording |
| `95d1d54b8` | docs: amend C-1.3, C-10.4, C-10.5, C-11.1, C-12.3, C-17.1 and C-17.8 |
| `290b7bb5a` | test: `subfleet`'s entry point passes `daemon start --check` through |
| `757bb71d9` | wip: this report's draft, as the first build left it (committed by the hub) |
| `38afc8110` | merge: `origin/release/217` at `fd1e4b65c` (see "The release merge") |
| this commit | docs: this report finished, and the PR body |

Nothing is pushed. The third job committed in its own worktree, which shares
the repository's Git metadata.

## The release merge

`origin/release/217` was at `4f5983f4c` when the brief was written. By the
time the third job ran, the hub had fetched it to `fd1e4b65c`, which is
`4f5983f4c` plus #243 and #247. The job merged `fd1e4b65c`, as the hub last
fetched it; it ran no fetch. The merge is `38afc8110`, and it brings
#237, #212, #238, #232, #223, #243 and #247 into the branch.

Only `docs/acceptance-contract.md` conflicted, in two places:
- **The change log.** D-ENV2's entry comes first, followed by every entry
  release added, unchanged and in release's order.
- **C-17.1.** Release added `kill <id> [--wait] [--reason TEXT] ...`, and
  D-ENV2 added `daemon [start [--check] [--json]|stop|status|logs|install]`
  with its parenthesis. The merged line is release's line with D-ENV2's
  `daemon` part. A check confirmed that the two lines differed in nothing else.

A three-way line check of the merged contract found every line either side
added present, and every line either side removed gone. The only line in
neither parent is the combined C-17.1.

The code merged cleanly, and its parts don't interact:
- Release added no caller of `check_lane_credentials`, `startup_lanes` or
  `held_lane_message`, whose shapes D-ENV2 changed.
- Release gave `daemon start` a stopped-holder preflight (#232, C-5.13). That
  preflight runs after `Client(root)`, and `daemon start --check` returns
  before it.
- Release's change to `tests/fake/test_launch_env_daemon.py` (the
  `_daemon_alive` stand-ins take `timeout`, and a stopped-holder stand-in) sits
  beside D-ENV2's new tests there.

## Cleanup

The third job's cleanup:
- **Scratch:** everything of its own lived in one folder,
  `$(getconf DARWIN_USER_TEMP_DIR)sf-env2c`. That held the venv (10 MB, built
  once through the hub's `uv_sync_hold.sh`), each hold's `TMPDIR`, the
  Hypothesis store, the export of `fd1e4b65c` for the control, and the logs and
  junit files. The folder is removed right after this commit. The default uv
  cache was used.
- **pytest-xdist:** the attempt to install it, `uv pip install --offline`
  through the same wrapper, found nothing in the cache and changed nothing.
- **Kept state roots:** the fake daemon harness had kept three, under
  `/tmp/sf-failed/2916`, `32269` and `72788` (3.7 MB). Each is named for a
  pytest of this job's, and all three are removed. `/tmp/sf-failed` itself is
  the harness's shared folder, so it stays.
- **The worktree:** the 13 bytecode folders the runs wrote are removed, and no
  `.hypothesis` folder was written. Apart from this commit, the worktree
  matches `38afc8110`.
- **Processes:** none of the job's processes is running at report time, and
  none holds or waits on either lock.

## Fix round 1 (review r1 of #248)

Review r1 of #248 (`~/reviews/subfleet-hub/env2-2026-10-19/review-r1.md`)
approved with three P3s. All three come from a refused `lanes.json` row that this
PR lets the daemon start with, where the base exited 2. The hub ruled that all
three are fixed before merging, since P3-1 loses a secrecy property the base had.
The fourth job (`20261023-043149-pr248-fix1`) did this round at `364fefdbf`; the
causes below are read from that code and executed against it (hold A).

### P3-1: an ignored row's harness hook reached every launch

**Cause.** `credentials.withheld_references` was fed the store's lanes alone, at
both of the daemon's withheld sets: `Daemon._withheld_references`, behind the
daemon's `ProviderEnvironment` (every lane launch, probe and relaunch), and
`Timers._provider_environment`, the timers' own (the card sensor's reads).
`startup_lanes` tags a refused row for a stored lane `IGNORED`, and nothing
remembered the tag past the startup warning. So a harness hook that only such a
row named was withheld from no launch, that lane's own included. Before this PR,
startup refused such a row (exit 2: the table at the top, P3-1's input 2), so no
launch ever ran beside one.

**Fix** (`ba1814366`).
- `credentials.ignored_hooks(found)` gives the harness hooks of the `IGNORED`
  rows in `check_lane_credentials`'s result.
- `Daemon._open_lanes` stores them as `_ignored_hooks`. Its class default is the
  empty set, for daemons the unit tests build without `__init__`.
- `_withheld_references` returns the stored lanes' names plus those hooks.
- `Timers` takes a `references` callable. The daemon hands it
  `_withheld_references`, so the two sets are one set.
- A listed or session name that an ignored row gives (HOME) is left to the
  selection, as a held lane's is.
- The hooks are read once, at startup, because the daemon checks and seeds
  `lanes.json` only then (`_open_lanes`). A row added to `lanes.json` later is
  not checked until the next start, as before.

### P3-2: `daemon start --check` printed lane ids unscrubbed

**Cause.** `_daemon_start_check` printed the (lane id, variable) pairs from
`lanes_json_refusals` as read, in text and in `--json`. `daemon start` prints its
refusal of the same row through `daemon_log.scrubbed` (C-25.7, #233).

**Fix** (`5f2241c98`).
- Each row's lane id (its text) and variable go through `daemon_log.scrubbed`
  once, and both modes print the result.
- One change in `--json`: a lane id that is not a string is now printed as its
  text, where it used to be the raw JSON value.
- Doctor's held-lane row had the same gap. That row, which this PR added, printed
  the store's lane ids and accounts as read; the store took them from
  `lanes.json`. I found this while testing invariant 2, which names doctor.
- Doctor now scrubs each held lane's words separately. Scrubbing the joined text
  would cut it at the 2,000-character line bound (`refusals.LOG_LINE_MAX`) once
  about five lanes are held.

**A limit of the scrubber, seen and not changed.** The scrubber's token patterns
stop at `(?<![A-Za-z0-9_-])` and `(?![A-Za-z0-9_-])` (`sessions/handoff.py`). It
left `ghp_…-9` as it was (hold B), and by the same boundary it would leave
`lane-ghp_…`, which I read but did not run. `daemon_log.scrubbed`, which
`daemon start`'s refusal and daemon.log go through, prints such an id unchanged.
`--check` now matches them exactly, so it does too. The bounded property found
this case, and lists its shape (`JOINED`) apart. Whether the scrubber
should read through a hyphen is a question for C-25.7's owner. A harness-hook
name with a token-shaped tail (`SUBFLEET_FAKE_ghp_…`) is also read as one word,
and printed as `daemon start` prints it.

### P3-3: a transfer left the lane's refused row behind

**Cause.** `_v2_roster_edit` set only `owner` (and, for a Codex lane, the home) on
the first row naming the lane. Take `claude-9`, stored on `SUBFLEET_TEST_OAUTH`,
with a row naming `HOME`. A transfer planned
`{"credential_ref": "HOME", "owner": "v1"}`, and a store rebuilt from that file
refused startup.

**Fix** (`b47f49624`): the hub's rule.
- `_repair_refused_row` runs on every row for the lane whose credential, as
  startup reads it (`lane_reference`), names a refused variable.
- That row takes the store's own credential, in the row's own shape: a
  `credential` object, or the flat `credential_kind`, `credential_ref` and
  `credential_epoch`. "The store's own" means its kind, its variable, or its home
  after the transfer, plus its epoch.
- Owner and home are set on the first row, as before.
- A lane whose stored variable is refused is still refused first
  (`transfer-held`), so the store's credential is always an accepted one.

**One choice.** A transfer changes no other lane's row, so the PR body's
invariant 3 holds as written whenever the file's other rows are clean. Another
lane's refused row is not this transfer's to repair. If that lane is stored,
startup ignores the row and names it in its warning. If not, the row refuses
startup and `--check` lists it. Invariant 3 below says this exactly.

### Invariants

1. **Withholding.** Every variable named as a harness hook by any lane, stored or
   by an ignored row, is withheld from every launch except that lane's own. An
   ignored row's hook is withheld from all of them, since it is no lane's
   credential.
   - `test_bounded_property_every_lanes_hook_is_withheld_from_every_launch`
     checks this without a daemon: 300 examples of stores and rosters over three
     hooks, four other refused names and two good variables. The withheld names
     equal a model written from the rule, not from the code (seeding, repair,
     ignored rows), and a launch selected from a parent holding every name
     carries exactly the hooks no lane names.
   - `test_bounded_property_the_daemon_withholds_every_lanes_hook` (fake, 25
     examples) checks the same model against real `Daemon` constructions. Both
     withheld sets equal it, and an assembled launch carries no named hook.
   - Scope: "launch" means what the daemon's `ProviderEnvironment` and the
     timers' assemble. Seen and not changed: Codex's isolated-review inspection
     selects its own environment (`CodexAdapter._launch`,
     `build_environment(credential_env)`, then `isolation.inspect_codex`), which
     withholds no lane's names. A held lane's hook reaches that one spawn at the
     base too. I read this; I did not execute it.
2. **No value printed.** No output path prints a dummy value, or a token-shaped
   id the scrubber reads as standing alone. The paths are `daemon start` (stderr
   and its refusal), `--check` (text and JSON), doctor's lane-variable row,
   `lanes transfer` (its refusal, and its dry-run and applied output) and
   daemon.log.
   - `test_daemon_start_check_scrubs_a_token_shaped_lane_id`.
   - `test_bounded_property_the_check_prints_each_row_as_daemon_start_would`
     (150 examples, differential between text, JSON and `daemon start`'s
     scrubbing).
   - `test_doctor_names_a_held_lane_by_its_id_scrubbed`.
   - `test_a_held_lanes_transfer_refusal_names_no_token_shaped_id`.
   - The transfer tests check that each result names variables only. No dummy
     was set while they ran, so for `lanes transfer` the no-value half rests on
     its code handling names only, which I read.
   - The fake regression asserts no dummy in daemon.log or either start's stderr.
   - A transfer's applied output echoes the lane id the operator typed, and the
     `lanes.json` diff, as before this PR.
3. **Transfers.** After any transfer of lane L, dry run or not, no `lanes.json`
   row for L names a refused variable as startup reads it, and no other lane's
   row changes. So a file whose other rows were clean ends with no refused row.
   A transfer whose stored lane is held is refused (`transfer-held`) and writes
   nothing.
   - `test_bounded_property_no_transfer_leaves_a_refused_row` (60 examples of up
     to four transfers each, flat and object rows, env- and home-kind rows and
     stored lanes, unknown lanes).
   - The regressions below.

### Tests

**Regressions.** Each fails at `364fefdbf` (hold A ran the new files against an
export of `096a1fcd7`, which is `364fefdbf`'s code plus only these tests) and
passes at the fix:

| Finding | Test (cases) | At `364fefdbf` |
| --- | --- | --- |
| P3-1 | fake `test_review_r1_an_ignored_rows_harness_hook_reaches_no_launch[ignored-row]`, review r1's input | "a withheld set misses the hook" |
| P3-1 | `test_an_ignored_rows_harness_hook_is_withheld_as_a_held_lanes_is` (2: a hook row beside an env-kind and a home-kind stored lane) | the hook is not withheld |
| P3-1 | `test_bounded_property_every_lanes_hook_is_withheld_from_every_launch`, and fake `test_bounded_property_the_daemon_withholds_every_lanes_hook` | withheld `frozenset()` where the model has `{SUBFLEET_FAKE_LANE_EIGHT}`; minimal inputs: a home-kind `claude-7` whose row names the hook (pure), and `claude-7` held on `HOME` with the same row (daemon) |
| P3-2 | `test_daemon_start_check_scrubs_a_token_shaped_lane_id` (6: text and `--json`, each with `HOME`, `SUBFLEET_FAKE_LANE_EIGHT` and `PYTEST_CURRENT_TEST`) | "the check printed the lane id unscrubbed" |
| P3-2 | `test_bounded_property_the_check_prints_each_row_as_daemon_start_would` | the raw id where `daemon start` prints `[REDACTED]` |
| P3-2 | `test_doctor_names_a_held_lane_by_its_id_scrubbed` | the token-shaped id in doctor's row |
| P3-3 | `test_review_r1_a_transfer_repairs_the_lanes_refused_row` (4: dry run and applied, to v1 and to v2), review r1's input | planned `("env", "HOME", <owner>)`, not the store's `SUBFLEET_TEST_OAUTH` |
| P3-3 | `test_a_transfer_repairs_every_refused_row_of_its_lane_in_its_own_shape` (6: a `credential` object, duplicate rows, a home-kind stored lane; dry run and applied) | the refused rows stay |
| P3-3 | `test_a_codex_transfer_repairs_its_refused_row_to_the_relocated_home` (2) | the row keeps kind `env` beside the home path |
| P3-3 | `test_bounded_property_no_transfer_leaves_a_refused_row` | a stored lane's refused row survives its transfer |

Hold A's whole result was 26 failed and 5 passed. The five that passed are the
controls, which pass at both heads: the held lane on the hook (fake and unit),
an ignored row naming `HOME` (left to the selection), the held lane's transfer
refusal naming no token-shaped id, and a held lane's transfer still refused.

**Mutations,** in `tests/unit/test_launch_env_mutations.py`, each run in memory
against its named regression. Each passed in hold B, so each mutant was killed:

| Mutant | Killed by |
| --- | --- |
| the ignored hooks dropped from both withheld sets (`daemon.ignored_hooks` returns none) | `test_review_r1_an_ignored_rows_harness_hook_reaches_no_launch[ignored-row]`: "a withheld set misses the hook" |
| the timers' set built from the store alone again | the same test, on the second set |
| `--check --json` listing the rows unscrubbed (an AST rewrite of the `emit` comprehension) | `test_daemon_start_check_scrubs_a_token_shaped_lane_id[json-*]`: "printed the lane id unscrubbed" |
| a transfer writing only `owner` again (`_repair_refused_row` a no-op) | `test_review_r1_a_transfer_repairs_the_lanes_refused_row`, dry run and applied |

**Holds.** Each hold is one pytest process under the hub's `suite_hold.sh`,
with the job's venv (GIL CPython 3.14.7, built once through `uv_sync_hold.sh`).
Credential-shaped names were removed from each child, and each hold wrote only
to the scratch folder.

| Hold | Where | Files | Waited | Ran | Result |
| --- | --- | --- | ---: | ---: | --- |
| 0 | worktree, before the commits | the new files, round 6, transfer, doctor | 81 min, then stopped by me unrun | none | none (see below) |
| B | worktree, uncommitted then: the code of `ba1814366` to `b47f49624`, the contract edits, and `096a1fcd7`'s tests before the property's fix | every briefed file: review r1's hold 1 and hold 2 files, the new files, the state contract and module names | 8 min | 7 min 27 s | 901 passed, 11 failed |
| A | export of `096a1fcd7` (`364fefdbf`'s code, the new tests) | the two new test files | 32 min | 1 min 46 s | 26 failed, 5 passed, as above |
| C | worktree, with the contract commit `6e5abecce` | the new files and mutations again, the state contract, module names, the four contract-reading files, and hold B's two failing fake files whole | 64 min | 4 min 2 s | 245 passed, 1 failed |

- **Hold 0** queued at 20:29Z while the host's 1-minute load stood over 120, so only
  slot A was open. A slot is taken by whichever waiter polls first, not in queue
  order, so a broader hold waits no longer than a narrow one. I stopped hold 0's
  wrapper (my own two processes, which were waiting and held no lock) and queued
  hold B, which covers everything hold 0 would have run.
- **Hold B's 11 failures:**
  - **One was mine:** `test_bounded_property_the_check_prints_each_row_as_daemon_start_would`
    drew `ghp_…-9`. That is the scrubber limit described under P3-2. `--check`
    printed the id exactly as `daemon start` does, so the property's
    differential held, but its claim that no token-shaped id is shown is true
    only for a token standing alone. The property now lists the hyphen-joined
    shape apart (`JOINED`) and makes that claim for standalone shapes only.
  - **The other 10 are the fake daemon harness's 5 s bound**
    (`tests/fake/conftest.py:145`, "condition timed out after 5s"):
    - 9 in `Harness.start`, 1 in `attempt_state`;
    - 2 in `test_daemon_contract.py`, 8 of the 9 in
      `test_census_escapers_daemon.py`.
  - **The kept state roots** (`/tmp/sf-failed/17652`): their `daemon.log`s held
    0 to 3 startup lines each. None had an error, a refusal, a traceback or a
    credential line, so those daemons were still starting when the bound ran
    out.
  - **Comparison:** this is the failure mode the third job and review r1 both
    recorded, in the same files. At review r1's load of 30 to 36 all of them
    passed. Hold B began at a 1-minute load of 61; the host stood near 95 to
    120 for the rest of the job.
  - **The startup change in this round** is one set comprehension over the
    startup check's result.

**Per file:**

| File | Hold B (fix) | Hold A (`364fefdbf`) |
| --- | ---: | ---: |
| `tests/unit/test_launch_env_round7.py` (new) | 27 passed, 1 failed (the property above; fixed since) | 4 passed, 24 failed |
| `tests/fake/test_launch_env_ignored_hook.py` (new) | 3 passed | 1 passed, 2 failed |
| `tests/unit/test_launch_env_mutations.py` (6 new) | 50 passed | not run |
| `tests/unit/test_launch_env_round6.py` | 43 passed | |
| `tests/unit/test_lanes_transfer.py` | 79 passed | |
| `tests/unit/test_doctor.py` | 44 passed | |
| `tests/unit/test_cli.py` | 248 passed | |
| `tests/unit/test_daemon_accept.py` | 5 passed | |
| `tests/unit/test_launch_env_allowlist.py` | 27 passed | |
| `tests/unit/test_launch_env_helpers.py` | 20 passed | |
| `tests/unit/test_launch_env_policy.py` | 26 passed | |
| `tests/unit/test_launch_env_round2.py` | 67 passed | |
| `tests/unit/test_launch_env_round3.py` | 69 passed | |
| `tests/unit/test_launch_env_round4.py` | 40 passed | |
| `tests/unit/test_launch_env_round5.py` | 39 passed | |
| `tests/fake/test_launch_env_daemon.py` | 52 passed | |
| `tests/fake/test_state_contract.py` | 37 passed | |
| `tests/unit/test_test_module_names.py` | 1 passed | |
| `tests/fake/test_resume_cli.py` | 1 passed | |
| `tests/fake/test_daemon_contract.py` | 19 passed, 2 failed (5 s start bound) | |
| `tests/fake/test_lost_acknowledgement.py` | 3 passed | |
| `tests/fake/test_census_escapers_daemon.py` | 1 passed, 8 failed (5 s start bound) | |
| **Total** | **901 passed, 11 failed** | **5 passed, 26 failed** |

Hold C, at the fix with the contract amended (begun at a 1-minute load of 29):

| File | Passed | Failed |
| --- | ---: | ---: |
| `tests/unit/test_launch_env_round7.py` (the property fixed) | 28 | 0 |
| `tests/fake/test_launch_env_ignored_hook.py` | 3 | 0 |
| `tests/unit/test_launch_env_mutations.py` | 50 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |
| `tests/unit/test_invariants_index.py` (reads the contract) | 25 | 0 |
| `tests/unit/test_desktop_ledger.py` (reads the contract) | 4 | 0 |
| `tests/unit/test_retention_turn_contract.py` (reads the contract) | 2 | 0 |
| `tests/unit/test_live_store_guard_round10.py` (reads the contract) | 66 | 0 |
| `tests/fake/test_daemon_contract.py` | 20 | 1 |
| `tests/fake/test_census_escapers_daemon.py` | 9 | 0 |
| **Total** | **245** | **1** |

Hold C's one failure, `test_daemon_contract.py::test_c6_2_request_id_is_idempotent_and_digest_conflicts_are_code_2`,
is the same 5 s start bound. Its kept `daemon.log` was one startup line with
no error. That test passed in hold B, and both of hold B's failing tests in that
file passed here. So every test in the two fake files passed in at least one of
the two holds, and all 9 census tests passed in hold C.

**Not run:**
- The rest of `tests/unit` and `tests/fake`, `tests/e2e`, `tests/process`,
  `tests/frontend` and `tests/live`, Python 3.12, free-threaded 3.14t, and CI.
  The job had no network.
- A third hold of `tests/fake/test_daemon_contract.py` whole, to see all 21 pass
  in one run. Hold C ended at 23:54Z, a quarter hour before the 4 h 30 m cutoff,
  and the slot waits had run from 8 to 81 minutes.
- The mutations at `364fefdbf`: three of them patch names the fix added, so they
  mean nothing there.
- Outside the holds: two sub-second scrubber probes (`daemon_log.scrubbed` on
  dummy strings) and one listing of this shell's environment-variable names (no
  values) ran directly with the job's venv, before I applied the rule that probes
  go through the wrapper too.

### Contract

C-10.4, C-10.5, C-12.3, C-17.8 and C-25.7 are amended, with one change-log entry
dated 2026-10-23:
- **C-10.4:** a transfer repairs its lane's refused rows.
- **C-10.5:** an ignored row's hook is withheld, and `--check` prints rows
  scrubbed.
- **C-12.3:** an ignored row's variable is withheld as a held lane's is.
- **C-17.8:** the held-lane row is scrubbed.
- **C-25.7:** `--check` joins `daemon start`'s scrubbed output.

### Commits

On top of `364fefdbf`, in this job's detached worktree:

| Commit | What |
| --- | --- |
| `096a1fcd7` | test: the regressions for review r1 of #248's three P3s, and their mutations |
| `ba1814366` | fix: withhold an ignored `lanes.json` row's harness hook from every launch |
| `5f2241c98` | fix: `daemon start --check` and doctor's held-lane row scrub lane ids |
| `b47f49624` | fix: a transfer repairs its lane's refused `lanes.json` rows |
| `6e5abecce` | docs: amend C-10.4, C-10.5, C-12.3, C-17.8 and C-25.7 |
| this commit | docs: this section, the earlier sections' pointers to it, and the PR body |

Nothing is pushed.

### Cleanup

- **Scratch:** everything of this job's lived in
  `$(getconf DARWIN_USER_TEMP_DIR)sf-env2f1`: the venv (built once through
  `uv_sync_hold.sh`), each hold's `TMPDIR`, the Hypothesis store, the export of
  `096a1fcd7`, and the logs and junit files. The folder is removed right after
  this commit, with the brief's `shutil.rmtree` command. One `rm -rf` of a
  scratch path ran before the export was made. The path did not exist yet, so
  it removed nothing. The default uv cache was used.
- **Kept state roots:** the fake harness kept 10 roots under
  `/tmp/sf-failed/17652` (hold B's pytest) and one under `/tmp/sf-failed/4822`
  (hold C's). Both folders are removed. `/tmp/sf-failed` itself is shared, so it
  stays.
- **The worktree:** the holds ran with `PYTHONDONTWRITEBYTECODE=1`. The three
  `__pycache__` folders under `subfleet/` came from the probes outside the
  holds, and are removed. No `.hypothesis` folder was written. Apart from this
  commit, the worktree matches `6e5abecce`.
- **Processes:** hold 0's wrapper was stopped while it was waiting, holding no
  lock. None of this job's processes is running at report time.
