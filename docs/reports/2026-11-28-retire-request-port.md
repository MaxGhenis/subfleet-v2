# Retire requests on the chain tip (PR #290)

Ported C-8.6 from PR head `e1a6524a94dd187ec5e1a52b0c0ff9aedef2b1d6` onto `f2fa1a6d67d39adf07d3579a265c579467b6bbca`, the requested chain tip through #316. The first command at 04:56:30 UTC verified both refs and an empty tracked-file status. Git denied the shared worktree index lock when staging the first test step (`Operation not permitted`). HEAD stays at the base and the files listed below are available for salvage.

## PR history mapped to the port

| PR commit | Where it landed |
| --- | --- |
| `55f913f47` | Event binding and selection in `subfleet/retention.py:91–93,394–430,641–813`, request and answer in `subfleet/daemon.py:6078–6193`, wire/CLI/survey paths and the two new test modules. C-8.6 and both evidence tools are carried. |
| `fd99baac8` | `retire_requests` in `retention.py:394–423` has separate kind and job-ID queries. `Daemon._retire` uses job-ID queries for standing requests and retired jobs at `daemon.py:6099–6114`. The tests check SQLite's query plans. |
| `16c487176` | `Daemon._retire_annotate` and `_retire_pins` at `daemon.py:6143–6193` retain deferrals and the 32-job pin-read threshold. `tests/fake/test_retire_request_daemon.py:252–287` covers both pin-read forms and error scope. |
| `27f5193e2` | `Daemon._retire` makes retention due before reading the answer at `daemon.py:6134–6141`. `_rearm_retention` and the cancelled, scan-failure and deadline branches are at `daemon.py:6195–6207,6329–6364`. `Client.call_settled` preserves unknown outcomes at `client.py:875–929`. `one_pass` excludes `added_bytes` in `tools/retire_requests_differential.py:78–100`. |
| `69bb43726` | Named mutations for the review findings at `tools/retire_request_mutants.py:145–190`. The batch/deadline selection tests observe check 1, and the archive assertions run check 2. |
| `e1a6524a9` | Review-aligned contract and change log at `docs/acceptance-contract.md:9,598–600,695`. The original PR report is carried byte for byte after the new provenance line at `docs/reports/2026-11-02-retire-requests.md:1`. |

## Files and rulings

- **1–4 and 13 — `subfleet/retention.py`.** Module docstring at 19–23 and event constants at 91–93. After `_pins`, new `retire_requests` at 394–423 folds events in event-ID order and new `requested` at 426–430 matches job ID and `created_at`. Invalid data and withdrawals with another binding leave a request standing. `maintenance` has a docstring change at 502–509. `_Pass.__init__` adds the request sets at 606–607. New `_Pass._rank` at 641–643 orders orphans, requested jobs and other jobs. In `_Pass.run`, the read after listing jobs is at 654, the sort and budget gate are at 709–713, the request-only choice is recorded at 725–726 before decrementing its pool, and `requested` is reported at 810–813. The C-8.7 protection still joins the protected set at 659–664. In `_Pass._start` at 1005–1011, a request-only selection checks its binding again through the `retention.selected` transaction. A withdrawn selection adds the job to `protected` and takes no lease, journal, selection audit, deferral or progress. The existing batch, deadline, holder, recovery and sizing paths provide the surrounding behavior.
- **5–7 — `subfleet/daemon.py`.** Imports at 83–86, 32-job threshold at 260–262, reply JSON helper at 692–698, harness default at 885, and request lock/counter at 1014–1017. Dispatch at 4428. The per-job event transaction and due-now update are in `_retire` at 6078–6141. `_retire_annotate` at 6143–6160 uses retention's own lease for `retiring`. `_retire_pins` at 6162–6193 uses the notice policy and overlays C-8.7 reasons within the guarded read. The rearm at 6195–6207 marks before updating the clock and clears the delete debt only for the hour. `_retention` snapshots the counter at 6282, uses the shared rearm for cancellation, scan failure without check-2 waiters, deadline without progress, and final completion at 6329–6391. Awaiting check 2 already uses five seconds. The requested retirement count is in both completion log paths. The hour wording checked by `test_native_maintenance_startup.py:223` is preserved.
- **6 — Protocol and client.** `subfleet/protocol.py:64,410–425` registers `retention.retire` beside delete and carries `RetireArgs` with its 500-job and 500-character bounds. `subfleet/refusals.py:86–88` registers the three refusal reasons. `subfleet/client.py:875–929` updates the settled-call contract and treats a lost reply followed by a gone daemon as unknown. All five `protocol.OPS` consumers use membership, subset, or dynamic enumeration.
- **8 — `subfleet/retention_cli.py:1–32,85–100,301–367`.** Adds help, parser and dispatch after delete. The command uses the shared client, restart fix for an older daemon, and protocol refusal handling. It preserves exit 0, 7, 2, 69 and 1 with the unknown-outcome JSON shape.
- **9 — `subfleet/retention_survey.py:1–10,161–175,189–214,262–273`.** Reads requests while the read-only store is open, annotates rows, and evaluates requested jobs first within the existing kept-for-good gate. Reports `would_retire.requested`. Checked existing tests against the neighboring `awaiting_check2`, `with_worktree`, and `pin_reasons` keys. Their assertions do not enumerate any affected key set.
- **10 — Docs.** `docs/acceptance-contract.md:9,598–600,695` adds the dated change log, C-8.6 after C-8.7 and the C-17.1 offline exceptions. `docs/desktop/retention-archive.md:970–972` carries the operator instructions. The historical report labels its runs and line references as PR evidence from `18aa2a695`, through `e1a6524a9`.
- **11 — Tools.** `tools/retire_request_mutants.py:57–205,222–244,249–319` carries 38 mutations, adds M1–M5 and uses one exact source match per mutation. The tests use `pytest-mNN`, the real-holder marker exclusion and the 540 s timeout. Each run logs disk and load and removes its basetemp. The tool runs the most relevant module first for daemon/client mutations so fail-fast can avoid unrelated property worlds. `tools/retire_requests_differential.py:78–110,113–170` compares the current pass with the base using the same synthetic holder stamps and ignores the PR's documented accounting noise.
- **12 — Review.** Read comments `6590332005`, `6590399561`, `6590519642`, and `6594574762` by GET. The scan-failure P2 and cancelled-path P3 use the shared rearm, and the contract names the applicable branches and debt. The due-before-answer and lost-then-gone fixes are carried, and the differential sets aside `added_bytes` at both levels. The review's four missing mutations cover deadline, holder ownership, survey ordering and withdrawal of an unfinished job. The withdrawn-start and binding issues are closed by rulings 3–4. The probe-holder and survey limits are retained. Argument refusals and unknown-outcome JSON remain covered by tests. A lease-read failure can still fail the reply after the event and wake. Pin or mark read failures give `pins_unread`.
- **Test harnesses.** `tests/unit/test_retention_retire_requests.py:88–98,193–222,292–329,367–423` adapts the pass oracle and covers withdrawal provenance and timing. `tests/fake/test_retire_request_daemon.py:185–226,306–363,430–461,470–545` covers answer parity, delete debt, two-pass retirement, and wire and exit behavior. `tests/unit/test_retention_delete_daemon.py:68–85` adds the request lock to the partial daemon fixture that skips `__init__`.

## Limits and integration

- Requests affect scheduling and eligibility within one pool. Terminal-state checks, pins, deferrals, C-8.7 marks, the batch and deadline, holder checks, and archive verification still apply. The daemon can report a C-8.7 keep, an unread pin result, or an open deferral after recording a request. Dormancy keeps the hourly clock. The answer's `retiring` describes its lease read after the transaction.
- An over-budget requested newer job ranks before that pool's older jobs. The budget was enough to select it, so a withdrawal before `_start` leaves that job retiring in the older job's place. The over-budget race case at `tests/unit/test_retention_retire_requests.py:193–222` covers this. A within-budget selection abandoned at the start uses its original place in the current selection, so another eligible job may wait for a later pass. The preexisting owned-worktree and cwd preflight can independently reject a job before the transaction. A withdrawal after the lease commits leaves the in-flight retirement in place.
- The survey lacks orphan-lease rank, daemon deferrals and a batch limit. The answer leaves process holders and the nested-host filesystem census to the pass. The review's synthetic cross-job `probe:` holder can split global and per-job pin reads. A real probe holder belongs to a single job.
- The base's early interrupted-pass results keep their existing shape. A completed pass returns `requested` for pruned IDs whose requests stood in its selection snapshot. The synthetic differential compares one pass and primarily observes selection through `awaiting_check2` and `in_flight`. It has no orphan leases, preexisting in-flight jobs, holders, errors or worktrees.
- For #288, use `self._rank(job_id)` for the ticket order and retain the decision about whether the selected job needed its request through `_start`. The hub owns that integration. No new lock-order stress run was added.

## Test evidence

The shared archive-offload venv imported `subfleet` from this worktree. It lacked `pytest-xdist`, so the requested `-n 4` command failed argument parsing and all completed runs used serial pytest. Tests used the installed timeout plugin. No packages were installed.

The two ported modules were added before production code. Against the base, their joint run stopped in collection because `subfleet.protocol.RETIRE_MAX_JOBS` was absent. That blocked individual fake-daemon failures and the unit run in that invocation. An additional unit-only base run was interrupted at 175 MiB after 25 failures while Hypothesis explored the missing feature. A focused rerun with the base package exported to scratch showed that all seven new binding and selection-race cases fail on missing `retention.RETIRE_REQUESTED` (21 deselected). On the port, all 28 unit cases and all 53 fake daemon/CLI cases passed.

| Port regression | Pass evidence |
| --- | --- |
| Missing, malformed or differently bound withdrawals | Five unit examples and the binding-aware Hypothesis oracle. |
| Withdrawal just before the selection transaction | Within-budget and over-budget cases. The first checks protection, no progress or errors, and absence of the lease, journal and selection audit. The second checks that only the newer job is retired. |
| Five-second rearm and delete debt | Four catch-up cases retain the debt; four hourly cases clear it. Both arriving-request and no-request scan-failure cases with an awaiting job use five seconds and retain the debt. |
| Notice cutoff and C-8.7 marks | Expired unread notice versus fresh notice; mark reason in the reply and pass; a mark-read exception yields `pins_unread` in both pin-read modes. |
| CLI wire name and older daemon | Dispatch of `retention.retire`, exit 69 with restart fix for an older daemon, plus the carried lost-acknowledgement, unknown-JSON and refusal cases. |

The chain tip can archive a new selection in one pass and finish it in the next. The deterministic unit tests use a follow-up pass with only the first batch eligible. The property checks the selection order before check 2 and the set of jobs pruned after it because recovery reads journals in job-ID order. The daemon integration checks `retiring` after the first pass and retirement after the second.

### Five temporary source edits

Each mutation was applied to its actual source file in isolation, ran one focused pytest node, and was restored byte for byte. Each run returned exit 1 with exactly one failure.

| Mutation | Failing test | Observed failure |
| --- | --- | --- |
| M1 `withdrawal-unbound` | `test_an_unbound_or_differently_bound_withdrawal_leaves_the_request_standing[data4]` | A different binding removed the standing request. |
| M2 `no-start-read` | `test_a_withdrawal_before_start_only_stops_a_request_only_retirement[within-budget]` | The withdrawn job was missing from `protected`. |
| M3 `catch-up-clears-debt` | `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up[completed]` | The five-second rearm cleared the debt. |
| M4 `answer-no-notice-keep` | `test_an_expired_unread_notice_does_not_keep_a_requested_job_in_the_answer` | An expired notice still gave a keep reason. |
| M5 `answer-skips-marks` | `test_a_delete_mark_keeps_a_requested_job_in_the_answer_and_the_pass` | The reply omitted `kept`. |

### Standalone mutation runner

Both baselines passed all 81 tests. The first invocation recorded 24 kills, then its `du` scan returned nonzero as another run removed a directory. The affected basetemp was cleaned. I made `du` diagnostic so a disappearing scratch directory cannot discard a result. A resumed run covered the remaining 19 mutants and killed all 19. Combined result is **43 killed, 0 survived**. The per-mutant tests and footprint measurements appear below.

### Differential

```text
200 worlds agree with f2fa1a6d6 (110 no request, 90 requests only on kept jobs; 0 jobs retired in all; 23 deferrals of requested jobs set aside)
control: 134 of 200 worlds differ from f2fa1a6d6 (200 requests on retirable jobs; 0 jobs retired in all)
```

The first run flagged different wall-clock holder-listing stamps for identical selections in world 1. The paired deterministic stamp sequence corrected that measurement. I interrupted the preliminary control before rerunning it with the same sequence.

### Commands

```sh
R="$PWD/build/rr-port-r1"
export TMPDIR="$R/runtime" HYPOTHESIS_STORAGE_DIRECTORY="$R/hypothesis"
export PYTHONDONTWRITEBYTECODE=1
export UV_PROJECT_ENVIRONMENT=/Users/maxghenis/subfleet-v2-lanes/archive-offload/.venv
export PYTHONPATH="$PWD"
uv --no-cache run --no-sync python -c 'import subfleet; print(subfleet.__file__)'

uv --no-cache run --no-sync python -m pytest -p no:cacheprovider --timeout=540 \
  --basetemp "$TMPDIR/pytest-<label>" -q -m "not real_lsof and not real_cwd" <targets>

uv --no-cache run --no-sync python tools/retire_requests_differential.py \
  --base f2fa1a6d6 --tmp "$TMPDIR/diff-worlds" --seed 4217 --examples 200
uv --no-cache run --no-sync python tools/retire_requests_differential.py \
  --base f2fa1a6d6 --tmp "$TMPDIR/diff-worlds" --seed 4217 --examples 200 --control
uv --no-cache run --no-sync python tools/retire_request_mutants.py \
  --logs "$R/mutants" --basetemp "$TMPDIR"
uv --no-cache run --no-sync python tools/retire_request_mutants.py \
  --logs "$R/mutants-resume" --basetemp "$TMPDIR" --only \
  unread-spreads unread-hides-deferral cli-retired-refused lifts-deadline retiring-any-holder \
  survey-unordered unfinished-withdraw-says-withdrawn already-records deadline-asked-at-end \
  result-requested-all scan-failed-hour cancelled-hour due-after-answer lost-then-gone-69 \
  withdrawal-unbound no-start-read catch-up-clears-debt answer-no-notice-keep answer-skips-marks
```

The base attempts used both new modules, then the unit module alone. The triage run selected `without_a_request or withdrawal_before_start or deadline` with `--maxfail=2 --tb=short`. The feature rerun used both new modules with `--tb=short`. The five hand edits used the exact test nodes above with `--maxfail=1 --tb=short`. End-of-job runs used one named module at a time with `--tb=short -ra`.

For the focused base rerun, `git archive --format=tar f2fa1a6d6 subfleet | tar -xf - -C "$R/base-src"` supplied the package. A scratch Python wrapper put `$R/base-src` first on `sys.path`, verified `subfleet.__file__`, and invoked `pytest.main` with the same flags, `--tb=short tests/unit/test_retention_retire_requests.py -k 'unbound_or_differently_bound_withdrawal or withdrawal_before_start'`.

For subsequent runs, a scratch-only pytest plugin was loaded with `PYTEST_PLUGINS=rr_port_scratch` and `PYTHONPATH="$PWD:$R/pytest-support"`. It directed the repository's legacy `/tmp/sf-failed` hook and short socket fixture roots into each basetemp. For long Unix socket paths inside the basetemp it used the relative spelling at bind/connect. The pre-plugin runs used the repository's default failed-state hook and produced no prune log. Later runs exported `GIT_CEILING_DIRECTORIES="$TMPDIR"` to keep Git discovery in the scratch world. The daemon-refusal CLI rerun used `PATH="$R/git-bin:$PATH"` so the real Git subprocess kept that ceiling even when the daemon sanitized its environment. The plugin, Git wrapper and shell runners were temporary evidence plumbing.

## Run deviations and cleanup

- The initial draft's catch-up helper could start the next batch while finishing the first. The first triage runs exposed it. The final helper limits check 2 to the selected batch. The next draft also exposed the base's job-ID recovery order, so the property now checks selection order and the completed set separately.
- One preflight rejected the initial dormant-run label because its basetemp contained `retention`. Numeric labels avoided that component.
- The first daemon-refusal CLI attempt admitted a synthetic job when its supposed non-repository directory discovered the assigned checkout. I interrupted it. Passing only the environment variable still let the sanitized Git subprocess discover the checkout, so I interrupted that attempt as well. The scratch Git wrapper restored the intended outside-Git fixture and all eight cases passed. When zsh refused the wrapper's status-variable assignment, I recorded the result from the pytest summary. The assigned checkout's HEAD stayed at the base.
- An edit to the running shell wrapper caused the first outcomes run to use the tool-managed temporary directory. I removed that basetemp (4,694,754 logical bytes), its zero-byte uv lock and 96 generated bytecode files (5,650,826 logical bytes). The wrapper now verifies the basetemp's resolved parent before launching pytest. The clean outcomes rerun passed all 22 cases. A wrapper edit also disturbed the settlement run's after-test shell exit. Its clean rerun passed all 68 cases.
- The first CLI module run passed 255 cases and failed `test_ping_open_pipe_is_bounded[]` at 0.787 s against a 0.750 s bound. The port rerun passed all 256. The initial package-only base replay had 117 passes and 139 guarded path refusals because its synthetic roots were outside the imported package's own source tree. The corrected replay used `TMPDIR="$R/base-src/run"` and `--basetemp "$TMPDIR/pytest-16-base-cli"` with the same exported package and CLI-module flags. That run retained the live-path guard and passed all 256. Neither replay reproduced the timing failure.
- The first client-outcome run passed 56 cases and hit two one-second socket timeouts in `test_c16_3_an_undecodable_answer_is_a_lost_response` and `test_c16_3_runs_finds_a_job_by_request_id_whoever_submitted_it`. Both isolated module replays passed all 58. The base replay used the same isolated source export setup with `--basetemp "$TMPDIR/pytest-17-base-outcome"` and `tests/unit/test_outcome_unknown.py`.
- The archive module's read-only fixture directory blocked its first teardown deletion. I restored owner permissions only inside that basetemp and deleted it. Every remaining temporary path is accounted for under the job scratch root.
- The test, differential and mutation invocations used `uv --no-cache run --no-sync python`. Some scratch-only path checks and helper setup used the same venv's interpreter directly.

## End-of-job modules and footprint

Counts are passed/failed/skipped/deselected. Times are UTC on 2026-11-28. Free disk and the 1-, 5- and 15-minute loads are from pre-run `df -H /` and `uptime`. Scratch is MiB for all of `$R` at run end, so an overlap may include another run's files. Every launch passed the 60 GB floor and `WRITE_STOP` check.

| Module under `tests/` | P/F/S/D | UTC | Free | Load | MiB |
| --- | --- | --- | --- | --- | --- |
| `fake/test_retire_request_daemon.py` | 53/0/0/0 | 05:15:08 | 74G | 38.50 27.13 27.70 | 27 |
| `fake/test_state_contract.py` | 37/0/0/0 | 05:17:22 | 74G | 57.27 37.90 31.88 | 23 |
| `fake/test_native_maintenance_startup.py` | 44/0/0/0 | 05:19:53 | 74G | 19.95 30.37 29.87 | 25 |
| `fake/test_retention_dormant.py` | 7/0/0/0 | 05:22:09 | 74G | 34.61 29.79 29.53 | 7 |
| `fake/test_review130_ab.py` | 12/0/0/0 | 05:22:43 | 73G | 41.64 32.11 30.39 | 19 |
| `fake/test_unrun_worktrees.py` | 105/0/0/0 | 05:23:45 | 73G | 36.22 32.56 30.69 | 93 |
| `fake/test_admission_ramp_retention.py` | 11/0/0/0 | 05:34:16 | 73G | 38.83 32.09 31.30 | 13 |
| `fake/test_attempt_tmpdir_daemon.py` | 24/0/0/0 | 05:34:29 | 73G | 37.82 32.18 31.35 | 22 |
| `fake/test_attempt_tmpdir_leaks_daemon.py` | 8/0/0/0 | 05:35:40 | 73G | 21.74 28.30 29.94 | 32 |
| `fake/test_lost_acknowledgement.py` | 0/0/3/0 | 05:36:09 | 73G | 22.37 27.74 29.67 | 10 |
| `unit/test_retention_retire_requests.py` | 28/0/0/0 | 05:36:21 | 73G | 25.15 28.14 29.79 | 14 |
| `unit/test_invariants_index.py` | 25/0/0/0 | 05:37:32 | 73G | 35.46 30.63 30.60 | 11 |
| `unit/test_test_module_names.py` | 1/0/0/0 | 05:37:50 | 73G | 28.22 29.28 30.11 | 20 |
| `unit/test_handler_refusals.py` | 394/0/0/0 | 05:37:56 | 73G | 26.36 28.88 29.97 | 9 |
| `unit/test_cli.py` | 256/0/0/0 | 05:57:28 | 75G | 26.08 35.39 43.05 | 12 |
| `unit/test_retention_delete_daemon.py` | 39/0/0/0 | 05:41:03 | 73G | 77.95 43.88 35.46 | 15 |
| `unit/test_timers_retention.py` | 20/0/0/0 | 05:41:44 | 73G | 81.54 49.43 37.92 | 19 |
| `unit/test_fake_daemon_borrowed_methods.py` | 6/0/0/0 | 05:43:22 | 73G | 96.53 63.47 44.60 | 17 |
| `unit/test_daemon_settle.py` | 68/0/0/0 | 05:53:22 | 75G | 25.00 45.31 48.28 | 74 |
| `unit/test_retention_archive.py` | 108/0/0/1 | 05:45:14 | 73G | 82.82 70.81 50.01 | 84 |
| `unit/test_retention_archive_properties.py` | 2/0/0/0 | 05:57:41 | 75G | 27.23 35.33 42.94 | 7 |
| `unit/test_review130_pass.py` | 21/0/0/0 | 06:00:57 | 74G | 36.11 36.54 42.00 | 13 |
| `unit/test_retention_listings.py` | 12/0/0/0 | 06:01:40 | 74G | 38.48 37.13 41.94 | 13 |
| `unit/test_retention_listings_properties.py` | 1/0/0/0 | 06:02:22 | 74G | 34.34 36.17 41.36 | 15 |
| `unit/test_notice_keep.py` | 49/0/0/0 | 06:03:21 | 73G | 33.02 35.36 40.69 | 23 |
| `unit/test_retention_81_notes.py` | 38/0/0/0 | 06:04:46 | 73G | 30.61 33.85 39.60 | 32 |
| `unit/test_retention_regenerable.py` | 26/0/0/0 | 06:17:35 | 72G | 45.28 40.27 39.09 | 24 |
| `unit/test_outcome_unknown.py` | 58/0/0/0 | 06:21:24 | 72G | 27.02 35.81 37.79 | 7 |
| `unit/test_settle_properties.py` | 129/0/0/0 | 06:11:03 | 73G | 21.93 30.91 36.41 | 23 |
| `unit/test_stopped_daemon.py` | 72/0/0/0 | 05:34:20 | 73G | 38.12 32.05 31.30 | 20 |
| `unit/test_protocol_registry.py` | 4/0/0/0 | 05:34:58 | 73G | 27.68 30.33 30.71 | 28 |
| `unit/test_protocol_secrecy.py` | 334/0/0/0 | 05:35:10 | 73G | 24.51 29.56 30.43 | 19 |
| `unit/test_retention_delete_request.py` | 67/0/0/0 | 05:35:52 | 74G | 21.26 28.00 29.81 | 11 |
| `unit/test_daemon_refusals_cli.py` | 8/0/0/0 | 05:50:23 | 73G | 27.89 60.40 53.30 | 27 |
| `unit/test_retention_outcomes.py` | 22/0/0/0 | 05:53:04 | 75G | 28.13 46.93 48.89 | 62 |
| `unit/test_retention_delete_apply.py` | 13/0/0/0 | 05:44:49 | 73G | 91.05 71.06 49.47 | 76 |
| `unit/test_daemon_connections.py` | 31/0/0/0 | 05:31:44 | 73G | 18.82 30.20 30.81 | 72 |
| `unit/test_rte_walk_bundle.py` | 16/0/0/0 | 05:32:55 | 73G | 25.17 29.04 30.28 | 23 |

**Total across 38 modules:** 2149 passed, 0 failed, 3 skipped, 1 deselected.

Skip details

- [1] tests/fake/test_lost_acknowledgement.py:35: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- [1] tests/fake/test_lost_acknowledgement.py:54: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- [1] tests/fake/test_lost_acknowledgement.py:71: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable

### Other run footprints

| Run | Result | UTC | Free | Load | MiB |
| --- | --- | --- | --- | --- | --- |
| `00-base-red` | xdist argument refused | 04:58:55 | 75G | 17.78 27.05 32.44 | 1 |
| `01-base-red` | 1 collection error | 04:59:08 | 75G | 18.62 26.79 32.25 | 1 |
| `02-base-unit-red` | interrupted after 25 failures | 04:59:25 | 75G | 20.44 26.79 32.16 | 175 |
| `03-pass-triage` | 3 passed, 1 failed, 24 deselected | 05:03:20 | 75G | 24.62 26.25 30.64 | 2 |
| `04-unit-green` | 26 passed, 2 failed | 05:04:46 | 75G | 21.55 24.67 29.60 | 7 |
| `05-feature` | interrupted during the property | 05:05:35 | 75G | 17.64 23.24 28.80 | 8 |
| `06-differential` | first comparison differed on wall-clock stamps | 05:09:02 | 74G | 16.18 22.99 27.70 | 8 |
| `07-differential-control` | preliminary control interrupted | 05:09:09 | 74G | 15.53 22.74 27.59 | 8 |
| `08-feature-rerun` | 81 passed | 05:10:01 | 74G | 16.28 21.75 26.94 | 32 |
| `09-differential-final` | 200 worlds agree | 05:11:31 | 74G | 33.57 25.61 27.86 | 1 |
| `11-manual-mutants` | five source edits | 05:12:53 | 74G | 21.24 24.42 27.25 | 2 |
| `m1-withdrawal-unbound` | 1 intended failure | 05:12:59 | 74G | 20.26 24.17 27.14 | 2 |
| `m2-no-start-read` | 1 intended failure | 05:13:13 | 74G | 16.95 23.24 26.76 | 2 |
| `m3-catch-up-clears-debt` | 1 intended failure | 05:13:22 | 74G | 15.41 22.71 26.53 | 3 |
| `m4-answer-no-notice-keep` | 1 intended failure | 05:13:33 | 74G | 13.72 22.11 26.27 | 2 |
| `m5-answer-skips-marks` | 1 intended failure | 05:13:41 | 74G | 14.15 22.06 26.22 | 2 |
| `10-differential-final-control` | 134 worlds differ | 05:14:11 | 74G | 20.20 22.75 26.33 | 16 |
| `12-tool-mutants` | 24 reported kills, then diagnostic interruption | 05:22:08 | 74G | 34.61 29.79 29.53 | 8 |
| `13-base-races` | 7 expected failures, 21 deselected | 05:27:24 | 73G | 36.71 32.11 30.82 | 36 |
| `end-34` | interrupted after one pass | 05:36:23 | 73G | 25.77 28.22 29.81 | — |
| `end-15` | 255 passed, 1 timing failure | 05:38:51 | 73G | 28.02 28.73 29.83 | 10 |
| `14-tool-mutants-resume` | 19 kills | 05:40:08 | 73G | 47.40 34.11 31.74 | 61 |
| `end-35` | 22 passed outside the prescribed basetemp | 05:42:45 | 73G | 91.28 58.60 42.17 | — |
| `end-19` | 68 passed, wrapper cleanup error | 05:43:57 | 73G | 97.89 67.60 46.87 | 53 |
| `end-34-rerun` | interrupted after one pass | 05:47:04 | 72G | 84.61 77.77 55.44 | — |
| `15-base-cli` | 117 passed, 139 guarded path refusals | 05:54:38 | 74G | 21.16 39.28 45.71 | 46 |
| `16-base-cli-isolated` | 256 passed | 05:59:26 | 75G | 35.77 37.03 42.78 | 8 |
| `end-28-early` | 56 passed, 2 socket-response timeouts | 06:12:25 | 72G | 40.96 33.11 36.62 | 26 |
| `17-base-outcome` | baseline replay 58/0/0/0 | 06:21:58 | 72G | 32.54 36.29 37.89 | 7 |
| `mutants` baseline | 81 passed | 05:22:10 | 74G | 34.61 29.79 29.53 | 36 |
| original `unread-spreads` | expected failure, diagnostic interrupted | 05:38:23 | 73G | 25.73 28.47 29.78 | 22 |
| `mutants-resume` baseline | 81 passed | 05:40:09 | 73G | 47.40 34.11 31.74 | 40 |

### Standalone mutation footprints

Every row is one killed mutant. The two baselines and the diagnostic interruption are in the preceding table.

| Mutant | Failing pytest node | UTC | Free | Load | MiB |
| --- | --- | --- | --- | --- | --- |
| `no-lift` | `test_a_request_retires_a_job_its_pools_budget_keeps` | 05:24:18 | 73G | 37.03 33.18 30.99 | 10 |
| `lifts-pins` | `test_a_pinned_request_takes_no_place_in_the_batch` | 05:24:30 | 73G | 35.87 33.06 30.97 | 13 |
| `lifts-deferral` | `test_a_deferred_request_waits_out_its_deferral` | 05:24:40 | 73G | 38.42 33.68 31.22 | 15 |
| `lifts-batch` | `test_requested_jobs_go_after_leftovers_and_before_the_rest` | 05:24:49 | 73G | 36.58 33.45 31.16 | 20 |
| `ahead-of-leftovers` | `test_requested_jobs_go_after_leftovers_and_before_the_rest` | 05:24:59 | 74G | 35.80 33.38 31.17 | 22 |
| `no-priority` | `test_requested_jobs_go_after_leftovers_and_before_the_rest` | 05:25:07 | 74G | 35.17 33.29 31.15 | 23 |
| `withdrawal-ignored` | `test_a_withdrawn_request_retires_nothing` | 05:25:24 | 74G | 30.32 32.35 30.86 | 25 |
| `unbound-withdraws` | `test_a_request_that_names_no_created_at_is_none[{}]` | 05:25:41 | 74G | 25.61 31.21 30.48 | 28 |
| `not-bound` | `test_each_jobs_latest_request_or_withdrawal_decides` | 05:26:06 | 73G | 21.15 29.75 29.98 | 39 |
| `survey-blind` | `test_the_survey_counts_a_request_as_a_pass_does` | 05:27:42 | 73G | 38.75 32.80 31.09 | 57 |
| `answer-not-bound` | `test_a_request_an_earlier_job_with_the_id_left_does_not_stand` | 05:29:38 | 73G | 43.59 35.96 32.53 | 57 |
| `pruned-by-kind` | `test_the_answer_reads_each_job_by_its_own_events` | 05:30:06 | 73G | 43.41 36.50 32.82 | 58 |
| `takes-unfinished` | `test_what_a_request_cannot_take_is_answered_and_nothing_recorded` | 05:30:41 | 73G | 32.04 34.58 32.28 | 60 |
| `dry-run-records` | `test_a_dry_run_answers_the_same_and_records_nothing` | 05:31:14 | 73G | 23.77 32.21 31.51 | 65 |
| `not-due` | `test_a_request_makes_retention_due_now_and_nothing_else_does` | 05:32:04 | 73G | 17.11 29.07 30.38 | 85 |
| `withdrawal-due` | `test_a_request_makes_retention_due_now_and_nothing_else_does` | 05:32:48 | 73G | 21.87 28.47 30.08 | 95 |
| `no-catch-up` | `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up[completed]` | 05:33:24 | 73G | 29.05 29.66 30.46 | 107 |
| `asked-at-end` | `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up[completed]` | 05:33:56 | 73G | 32.43 30.50 30.74 | 27 |
| `clock-first` | `test_a_pass_whose_mark_fails_stays_due[completed]` | 05:34:26 | 73G | 37.63 32.05 31.30 | 33 |
| `mark-unlocked` | `test_a_request_while_a_pass_marks_its_end_is_not_lost` | 05:35:16 | 73G | 23.19 29.20 30.30 | 31 |
| `hides-pin` | `test_the_answer_names_a_pin_a_deferral_and_a_retirement_in_flight` | 05:36:13 | 74G | 22.98 27.77 29.67 | 21 |
| `pins-each-always` | `test_above_the_limit_the_answer_reads_every_jobs_pins_once[2]` | 05:36:51 | 73G | 31.18 29.36 30.18 | 28 |
| `pins-once-always` | `test_above_the_limit_the_answer_reads_every_jobs_pins_once[3]` | 05:37:22 | 73G | 34.89 30.44 30.53 | 21 |
| `unread-silent` | `test_a_failed_delete_mark_read_marks_pins_unread_without_losing_the_reply[2]` | 05:37:59 | 73G | 26.81 28.93 29.98 | 19 |
| `unread-spreads` | `test_pins_that_could_not_be_read_are_named_and_nothing_else_is_lost` | 05:45:26 | 73G | 82.75 71.19 50.39 | 26 |
| `unread-hides-deferral` | `test_pins_that_could_not_be_read_are_named_and_nothing_else_is_lost` | 05:47:19 | 72G | 81.61 77.43 55.71 | 31 |
| `cli-retired-refused` | `test_the_cli_counts_a_retired_job_as_settled_and_a_dry_run_as_unrecorded` | 05:48:55 | 72G | 70.13 74.97 57.07 | 46 |
| `lifts-deadline` | `test_the_deadline_binds_requested_jobs` | 05:49:58 | 72G | 36.53 64.67 54.55 | 27 |
| `retiring-any-holder` | `test_only_retentions_own_retire_lease_says_retiring` | 05:50:28 | 73G | 26.22 59.51 53.03 | 34 |
| `survey-unordered` | `test_the_survey_takes_a_request_before_the_oldest` | 05:50:48 | 73G | 20.66 56.08 51.95 | 32 |
| `unfinished-withdraw-says-withdrawn` | `test_a_withdrawal_of_an_unfinished_job_records_nothing` | 05:51:37 | 72G | 29.90 53.27 51.11 | 38 |
| `already-records` | `test_a_request_records_one_event_that_names_the_job` | 05:51:47 | 72G | 30.03 52.53 50.87 | 29 |
| `deadline-asked-at-end` | `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up[deadline]` | 05:51:53 | 72G | 30.81 51.97 50.69 | 48 |
| `result-requested-all` | `test_a_withdrawal_before_start_only_stops_a_request_only_retirement[within-budget]` | 05:52:04 | 72G | 30.16 51.13 50.41 | 35 |
| `scan-failed-hour` | `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up[holder-scan-failed]` | 05:52:11 | 72G | 30.39 50.83 50.31 | 51 |
| `cancelled-hour` | `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up[cancelled]` | 05:52:18 | 72G | 29.24 50.25 50.10 | 53 |
| `due-after-answer` | `test_a_request_whose_answer_fails_to_read_still_makes_retention_due` | 05:52:29 | 73G | 30.04 49.37 49.79 | 63 |
| `lost-then-gone-69` | `test_the_cli_after_a_lost_answer_and_a_gone_daemon_says_the_outcome_is_unknown[False]` | 05:52:46 | 74G | 31.80 48.82 49.59 | 65 |
| `withdrawal-unbound` | `test_an_unbound_or_differently_bound_withdrawal_leaves_the_request_standing[None]` | 05:52:58 | 74G | 29.97 47.60 49.14 | 53 |
| `no-start-read` | `test_a_withdrawal_before_start_only_stops_a_request_only_retirement[within-budget]` | 05:53:08 | 75G | 27.23 46.43 48.70 | 57 |
| `catch-up-clears-debt` | `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up[completed]` | 05:53:17 | 75G | 25.69 45.79 48.47 | 72 |
| `answer-no-notice-keep` | `test_an_expired_unread_notice_does_not_keep_a_requested_job_in_the_answer` | 05:53:27 | 75G | 24.36 44.84 48.10 | 84 |
| `answer-skips-marks` | `test_a_delete_mark_keeps_a_requested_job_in_the_answer_and_the_pass` | 05:53:33 | 75G | 23.69 44.36 47.91 | 71 |

## Salvage and scratch

Peak observed scratch was 175 MiB during the interrupted base run. The last pre-cleanup measurement was 7 MiB. Scratch held the runtime and per-run basetemps, Hypothesis data, source export, helper scripts and test logs. Cumulative byte churn was not separately instrumented. All of `$R` was removed with a realpath guard, leaving zero scratch bytes from this task.

The lock refusal left these files uncommitted for salvage.

- `docs/acceptance-contract.md`
- `docs/desktop/retention-archive.md`
- `docs/reports/2026-11-02-retire-requests.md`
- `docs/reports/2026-11-28-retire-request-port.md`
- `subfleet/client.py`
- `subfleet/daemon.py`
- `subfleet/protocol.py`
- `subfleet/refusals.py`
- `subfleet/retention.py`
- `subfleet/retention_cli.py`
- `subfleet/retention_survey.py`
- `tests/fake/test_retire_request_daemon.py`
- `tests/unit/test_retention_delete_daemon.py`
- `tests/unit/test_retention_retire_requests.py`
- `tools/retire_request_mutants.py`
- `tools/retire_requests_differential.py`
