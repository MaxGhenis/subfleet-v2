# Review r5 of #222: refused lanes.json rows, held lanes and the lane-variable checks (D-ENV2)

Base: `origin/release/217` at `473fe2941`, which has #222 (the launch-environment
allowlist, merged as `f99da54b0`). Review r5
(`~/reviews/subfleet-hub/launch-env-2026-10-14/review-r5.md`) approved #222 with
three P3s, and the hub ruled on each. The hub also asked for C-1.3's wording and an
install check. This report gives each cause as executed at the base, then the fix,
then the tests. No credential store, secret loader, live store, live daemon or live
process was read or changed. Every test value is a dummy, and nothing here quotes an
environment value.

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
  (`test_no_transfer_writes_a_refused_row_into_lanes_json`).
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

STATUS-TESTS

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

STATUS-VALIDATION

### Not run

STATUS-NOTRUN

## Commits

STATUS-COMMITS

## Cleanup

STATUS-CLEANUP
