Review r5 of #222 approved the launch-environment allowlist (D-ENV1) with three P3s, and the hub ruled on each. This PR is those follow-ups (D-ENV2): a refused `lanes.json` row for a lane the store already holds no longer stops the daemon; `lanes transfer` and doctor handle held lanes; every session probe name is a refused lane variable; C-1.3 says "a variable no launch inherits"; and `daemon start --check` is the install check the hub runs before installing release/217. The full report is `docs/reports/2026-10-19-env2-followups.md`. Every test value is a dummy (`dummy-…`), and no message names a value.

**Causes, as executed at the base** (`473fe2941`, which has #222):

- **P3-1, a refused row for a stored lane stopped the daemon.** `credentials.startup_lanes` gave every env-kind `lanes.json` row the source `lanes.json`, whether or not the store held that lane id, and `policy.check_lane_credentials` refused any such row whose variable is refused. But `Daemon._seed_lanes` skips a row whose lane id is stored (apart from C-1.3's repair), so that row would never be stored: the refusal guarded nothing. Review r5's input 1 (the store round 4 left, `lanes.json` unchanged) and input 2 (claude-9 healthy on its own variable, its row changed to `HOME`) both exited 2 at `daemon start` and at launchd's start, so under `KeepAlive` every lane stayed down until someone edited `lanes.json`.
- **P3-1, input 3: `lanes transfer` of a held lane wrote a refused row.** `lanes_transfer._v2_roster_edit` appends the store's row to `lanes.json` when the file does not name the lane. For a held lane that row carries the refused variable (`credential_ref: HOME`), and the next start exited 2.
- **P3-2, doctor never showed a held lane.** `doctor.check_daemon_lane_credentials` read only `daemon.status.environment.lane_credentials`, which leaves a held lane's variable out, so the row passed with "the daemon holds no env-kind lane variable".
- **P3-3, `PYTEST_CURRENT_TEST` was accepted as a lane variable.** `launch_env.refused_reference` was `inherited_name` (listed, harness or session names). `PYTEST_CURRENT_TEST` is none of those but is a `SESSION_PROBE_NAMES` name, whose value selection reads before any name is withheld. A lane on it was accepted, `daemon start` handed it to the daemon by name, and resolution read its value, against C-12.3's "a withheld name is rejected before anything reads its value".

**Fix:**

- **A refused row for a stored lane is ignored, with one warning per lane.** `startup_lanes(roster, stored)` tags such a row `launch_env.IGNORED`, whatever the stored lane's kind. `check_lane_credentials` refuses only a refused row for a lane the store lacks, and returns held lanes and ignored rows as (lane, variable, source). `policy.startup_notes` gives each lane one warning, at `daemon start` (stderr) and in `daemon.log`: a held lane's says it is held and that its row is ignored; a healthy stored lane's says the row is ignored and the store keeps the lane. Seeding is unchanged, so a fixed row still repairs a held lane in place (C-1.3). C-10.5 now holds as written.
- **`lanes transfer` refuses a held lane** (`transfer-held`, exit 7), dry run or not, before anything is planned or written. The refusal names the lane and, for a listed, session or probe name, the variable, with the fix (repair the row, restart, then transfer).
- **Doctor's lane-variable row warns for each held lane,** applying the scheduler's own rule (env kind and `refused_reference`) to `daemon.status`'s lane rows. Its fix is each lane's `lanes.json` row (lane id, provider, account, `credential_kind: env`, `credential_ref: <VARIABLE>`). A warning never decides doctor's exit status.
- **Every session probe name is a refused lane variable:** `refused_reference(name)` is `valid_name(name) and (inherited_name(name) or name in SESSION_PROBE_NAMES)`. Every consumer follows: the start checks, `env_references`, the scheduler's `credential-refused`, both status renderers and doctor. Resolution checks it before reading any value. Only `PYTEST_CURRENT_TEST` changes; the three markers were already session names.
- **C-1.3's wording,** and the fix texts that said "off the launch allowlist", now say "a variable no launch inherits".
- **`daemon start --check`** lists each env-kind `lanes.json` row whose variable is refused, as `<lane> <variable>` (`--json`: `lane_id` and `variable`), and starts nothing. It exits 2 when it lists any row or cannot read the file as a lane list (naming only the exception class), and 0 otherwise. It reads `lanes.json` alone, as a regular file (a FIFO is refused, not waited on): no store, policy, socket, spawn or variable value. Without the store it cannot tell which rows startup would refuse (new lanes) and which it would ignore (stored lanes), and its note says both.

**Choices:** refusing the transfer, rather than dropping the held lane's row, keeps `lanes_transfer`'s rule that `lanes.json` carries the store's owner, and a held lane holds no credential either owner could use. `daemon start --check`, rather than a doctor row, because `subfleet doctor` cannot run one row alone: a whole run asks the live daemon, reads the store and runs the provider binaries.

**Invariants** (each checked by a bounded, derandomized Hypothesis property or a differential test):

1. Only a refused row for a new lane refuses startup. For every `lanes.json` and store pair (up to 4 rows, 3 stored lanes, five refused and two good variables, home-kind stored lanes and `env.pass`), startup is refused exactly when `env.pass` names a lane credential or a refused row's lane id is not stored. Each held lane and each lane with an ignored row gets exactly one warning, "held" only when held, and no other lane is named (300 examples; round 5's daemon-level property runs the same rule through `cli.daemon_env` and real `Daemon` constructions, 25 examples).
2. The install check agrees with startup. With an empty store, startup refuses exactly the lanes the check lists; with every lane stored, it refuses none and warns once per listed lane (200 examples, including a variable that is no exact name).
3. No transfer writes or leaves a refused row: after any transfer of lane L, dry run or not, no `lanes.json` row for L names a refused variable as startup reads it, and no other lane's row changes; so a file whose other rows were clean ends with none (fix round 1, P3-3).
4. No refused variable's value is read, by resolution or by the install check (a recording environment, for each probe name and each listed row).
5. Every variable named as a harness hook by any lane, stored or by an ignored row, is withheld from every launch but that lane's own; an ignored row's from all (fix round 1, P3-1).
6. No output path prints a dummy value, or a token-shaped id the scrubber reads as standing alone: `daemon start`, `--check` (text and JSON), doctor, `lanes transfer` and daemon.log (fix round 1, P3-2).

**Contract:** C-1.3, C-10.4, C-10.5, C-11.1, C-12.3, C-17.1 and C-17.8 are amended, with one change-log entry (2026-10-20).

**Release merge:** `origin/release/217` at `fd1e4b65c`, as the hub last fetched it, is merged as `38afc8110`. It contains `4f5983f4c`, the head the brief named, plus #243 and #247. Only `docs/acceptance-contract.md` conflicted: the change log keeps both sides' entries, and C-17.1 keeps release's `kill … [--reason TEXT]` with D-ENV2's `daemon [start [--check] [--json]|…]`. A three-way check confirms that every line either side added is present and every line either side removed is gone.

**Tests** (at the merged head `38afc8110`, GIL CPython 3.14.7, one pytest process per hold of the shared suite lock):

- **The brief's files** (`test_launch_env*` 375, `test_lanes_transfer` 79, `test_doctor` 44, `test_cli` 248, `test_daemon_accept` 5, `test_live_store_guard*` 850, fake `test_state_contract` 37, `test_test_module_names` 1) all passed: 1,639. So did the changed `tests/fake/test_launch_env_daemon.py`, 52. Among them, the new `test_launch_env_round6.py` passed 43, and `test_launch_env_mutations.py` passed 44, which kills each named mutant: the ignored row refused again (at `daemon start` and at daemon startup) and the install check reading a value.
- **Regressions at the base:** the first build ran the new and changed test files against an export of `473fe2941`. 40 failed, each on its own assertion, and 55 passed: the controls, and the older fake tests. The report has the table.
- **The whole-suite rule,** counting files run to completion:

  | Directory | Files run | Passed | Failed | Files unrun |
  |---|---:|---:|---:|---:|
  | `tests/unit` | 97 of 335 | 5,811 | 0 | 238 |
  | `tests/fake` | 57 of 112 | 795 | 12 | 55 |

  The seven holds ran 8,277 tests in all, reruns and cut-short files included.
- **All 12 failures are the fake daemon harness's 5 s start bound** (`tests/fake/conftest.py:145`), under host load, in `test_resume_cli` (1), `test_census_escapers_daemon` (1), `test_daemon_contract` (9) and `test_lost_acknowledgement` (1). The kept daemon logs showed only startup lines, with no error or refusal. Rerun whole, the first three files passed: 1, 9 and 21 tests. `test_lost_acknowledgement` failed after the cutoff for reruns and was not run again.
- **Not run:** 293 files, listed in the report. Locally the suite ran 2.6 to 20 times slower than CI, slot waits reached 86 minutes, and xdist is not in the lock. Also not run: the control again at `fd1e4b65c`, `tests/e2e`, `tests/process`, `tests/frontend` and `tests/live`, Python 3.12, and CI.

**Fix round 1** (review r1 of #248, three P3s, each from a refused `lanes.json` row this PR lets the daemon start with, where the base exited 2):

- **P3-1, an ignored row's harness hook reached every launch.** The daemon withheld only the stored lanes' names, at both of its withheld sets. It now remembers the ignored rows' hooks at startup (`credentials.ignored_hooks`), and both sets add them, exactly as for a held lane's hook (`ba1814366`).
- **P3-2, `daemon start --check` printed lane ids unscrubbed.** Each listed row's lane id and variable are now printed as `daemon start` prints them, through `daemon_log.scrubbed`, in text and `--json`; doctor's held-lane row scrubs each lane's words too (`5f2241c98`).
- **P3-3, a transfer kept the lane's refused row.** Each of the lane's rows naming a refused variable is repaired to the store's own credential, in the row's own shape, dry run or not; a held lane is still refused (`b47f49624`).
- Tests first (`096a1fcd7`): the reviewer's three inputs as regressions, failing at `364fefdbf`; bounded properties for the three invariants; a named mutation for each fix. Contract: C-10.4, C-10.5, C-12.3, C-17.8 and C-25.7, one change-log entry (2026-10-23). The report's "Fix round 1" section has the counts.

**History:** the first build (`20261020-073952-env2-build`) was lost to the 2026-10-20 power cut after its seven commits, with its report unwritten past the test sections. The follow-on job (`20261020-191828-env2-build-b`) was pinned to a lane that stayed closed for two days and never started. This job merged the release, re-ran every test count reported here, and finished the report and this body.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
