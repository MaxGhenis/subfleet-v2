# PR 131 CI: a finished run reported lost after two blind censuses — 2026-10-14

CI run 38458495060 at `5de28be9e` (Python 3.12) failed one test of 13,350:
`tests/e2e/test_review_r3_strand_timing_e2e.py::test_strand_timing[automatic]`,
with `waiter_rc=125` (`Exit.JOB_LOST`) in the same second the run finished. The
work was done in a detached job worktree at `5de28be9e`. No remote Git operation
and no live state was touched, and no sub-agents were used. Every pytest ran
alone, under its own hold of `/private/tmp/claude-501/subfleet-suites.lock`.

## Cause

`subfleet wait` exits 125 only for a job whose state is `lost`
(`subfleet/cli.py:198`). The daemon's long poll answers from one committed
snapshot (`Daemon._wait_answer`), so the 125 is no transient misread: the run
really ended `lost`. In the reproduction below the run's final row is
`state=lost`, and its attempt is `quarantined` with this reason:

```text
"reason": "writers remain after exit receipt",
"errors": ["cwd enumeration unavailable"], "unverifiable": true,
"live_pids": [], "identities": {}, "ambiguous_identities": []
```

The events are `attempt.finalizing` at 07:49:22 and `attempt.quarantined` at
07:50:02. In the 40 s between them, the post-receipt census saw no process at
all. It was only unreadable.

1. #131 (`350d116bd`, 2026-10-06) added C-5.5's cwd source to every deciding
   census: one `lsof -nP -d cwd -F0pn`, capped at 10 s
   (`procs.cwd_pids`, `procs._read`). A failed or slow `lsof` makes the census
   unverifiable (`procs.containment` appends `cwd enumeration unavailable`, and
   `unverifiable = bool(errors)`). `release/217` has no cwd source.
2. On this host, under the load other sessions put on it, `lsof` mostly takes
   0.4–0.7 s but sometimes takes more than 10 s. Two of 69 back-to-back calls
   ran out the cap, and spikes of 3–12 s came in clusters. Inside the test, one
   finalization census took 1.7–12.6 s. A traced run (r002) shows one census at
   12.6 s that failed on an `lsof` timeout at 10.02 s; the retry, 5.3 s later,
   read.
3. C-5.9 (`Daemon._finalize`) allows `exit_settle_s` (3 s), measured from the
   end of the first census that is not verified empty. Every census takes longer
   than that, so the window holds exactly one retry. When `lsof` runs out its
   cap in two censuses in a row, the second census is past the window and
   quarantines the attempt.
4. `Daemon._quarantine` writes the job `lost`, rc 125, at once
   (`subfleet/daemon.py`, `state, rc = ... ("lost", 125)`). `wait` returns
   `JOB_LOST`. Self-resolution 600 s later releases the leases but cannot revive
   the job: the finished deliverable is never accepted.

The race, then, is between finalization's settle window and the census's own
latency. The window is a 3 s clock started by a census that can itself take 12
s, and nothing in it tells "a writer is still there" apart from "this census
could not look".

This is not the path in `docs/reports/pr131-lost-run.md`, which is a background
waiter retained as a cwd writer. That one shows a live pid and is fixed in this
head (`tests/fake/test_finished_run_containment.py`). Here the census showed no
pid. The CI log carries no quarantine reason and the run kept no artifacts, so
CI's reason cannot be read. The reproduction at the same head has the same shape
(a run finished, `wait` returned 125 in the same second, the turn finished
later), and the only failure seen locally was blind.

## Fix

A **blind** census is unverifiable only because C-5.5 source reads failed: its
only errors are `group`, `descendant`, `marker` or `cwd enumeration
unavailable`, and it shows no live process and no ambiguous identity
(`procs.Containment.blind`, `procs.ENUMERATION_ERRORS`). It saw nothing, so it
decides nothing, as a guardian that cannot be inspected decides nothing (C-4.2):

- The attempt stays `finalizing` with every lease.
- Its census is taken again once the time the window has run has passed again
  (at least `inspect_interval_s`), so the window's censuses at least double in
  age. At most `2 + ceil(log2(exit_inspect_s / inspect_interval_s))` are taken.
- The wait lasts until `exit_inspect_s` (60 s, `contracts.EXIT_INSPECT_S`) from
  the window's first census. A blind census past that quarantines exactly as
  before.

A census that sees a writer, or holds for any other reason (identity, lineage
overflow, publication), keeps `exit_settle_s`. Only a verified-empty census
releases, so nothing is released that was not released before; a finished run is
just no longer declared lost because the host was slow to answer. C-4.2's
`finalizing` row and C-5.9 are amended in the same commit.

Out of scope, and unchanged: the kill protocol's `kill_settle_s` loop (C-5.6)
has the same shape. One slow census there can outlast its 3 s, so a blind
census quarantines a killed attempt. The job is `cancelled` (or `lost` for a
dead guardian) either way, so no finished result is lost there; the quarantine
holds the workspace until the next pace.

## Regression and properties

`tests/unit/test_daemon_settle.py`:

- `test_c5_9_two_timed_out_cwd_scans_in_a_row_do_not_lose_a_finished_run` runs
  the CI interleaving deterministically on a held monotonic clock. A run exits
  0, two censuses each take 12 s and fail their cwd read, and the third reads.
  On `5de28be9e` it fails: the second census quarantines the run, the job is
  `lost`, and the `wait` answer maps to `Exit.JOB_LOST`. With the fix the run
  is `succeeded` and `wait` exits 0.
- `test_c5_9_a_census_blind_for_exit_inspect_s_still_quarantines` checks the
  bound and the pace.
- `test_c5_9_a_writer_seen_after_a_blind_census_ends_the_window_at_exit_settle_s`
  checks that a seen writer still quarantines at `exit_settle_s`.
- `test_c5_9_property_...` is a Hypothesis property (150 derandomized examples)
  over census sequences (empty, blind, survivor, guardian, seen-while-blind,
  overflow), census durations and pass spacing. It checks that finalization
  ends, and accepts only on a verified-empty census. Every census before the
  last ended inside its window: before `exit_inspect_s` if blind, before
  `exit_settle_s` otherwise. A quarantine follows only a census past its window.
  Blind censuses keep the pace bound, and `wait` maps the outcome to 0 or 125.
- The existing C-5.9 parametrized test keeps its 3 s window for the survivor,
  guardian, seen-while-the-cwd-read-failed and lineage-overflow censuses. Its
  former `unverifiable` case was a blind table failure and is now under the new
  rule.

`tests/unit/test_procs.py`: a cwd `lsof` that times out (or exits 1) through the
real `_read` and `cwd_pids`, with nothing else seen, gives a census whose only
error is `cwd enumeration unavailable` and which is blind and not verified
empty. A property over errors, live pids and ambiguity shows `blind` is exactly
"failed source reads only, nothing seen", and that a blind census is never
verified empty.

## Reproduction counts

These runs use the unmodified test, `test_strand_timing[automatic]`, one pytest
per lock hold. Lock queueing from other sessions took most of the wall time.

| Tree | Runs | Failed (rc 125) |
| --- | --- | --- |
| `5de28be9e` (#131 head) | 17 | 1 |
| `7b877d299` (#195 merged) | 1 | 0 |
| `ba5b2d098` (#131 before #195) | 1 | 0 |
| `origin/release/217` (`5a711114e`) | 1 | 0 |

At about 1 in 17 the plain failure is too rare to bisect by counts in a
six-hour job. The cwd source the failure depends on is the same in all three
#131 commits and absent from `release/217`, and neither merge touched
`procs.cwd_pids` or `_finalize`'s window. The #195 and #191/#194 merges are not
implicated.

A stress A/B makes the CI shape deterministic at the e2e level. It uses
uncommitted copies of each tree whose `procs.cwd_pids` fails, as a timed-out
`lsof` does, for the first 30 s after the daemon's first cwd scan:

| Tree (with the 30 s cwd outage) | Runs | Failed (rc 125) |
| --- | --- | --- |
| before the fix, `5de28be9e` | 2 | 2: run `lost`, its turn `lost` |
| after the fix, `06cf67d5b` | 2 | 0: run and turn `succeeded`, the run about 30 s after its receipt |

## Tests run

Listed in the job's final answer, with counts per file.
