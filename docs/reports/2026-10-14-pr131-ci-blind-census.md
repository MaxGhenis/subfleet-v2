# PR 131 CI: a finished run reported lost, two census causes — 2026-10-14

CI run 38458495060 at `5de28be9e` (Python 3.12) failed one test of 13,350:
`tests/e2e/test_review_r3_strand_timing_e2e.py::test_strand_timing[automatic]`,
with `waiter_rc=125` (`Exit.JOB_LOST`) in the same second the run finished. The
work was done in a detached job worktree at `5de28be9e`. No remote Git operation
and no live state was touched, and no sub-agents were used. Every pytest ran
alone, under its own hold of `/private/tmp/claude-501/subfleet-suites.lock`.

## Summary

`subfleet wait` exits 125 only for a job whose state is `lost`
(`subfleet/cli.py:198`). The daemon's long poll answers from one committed
snapshot (`Daemon._wait_answer`), so the 125 is no transient misread: the run
really ended `lost`. In both failures reproduced here, the post-receipt census
(`Daemon._finalize`, C-5.9) did not verify empty within `exit_settle_s`. So
`Daemon._quarantine` quarantined the finished attempt, which also writes the
job `lost` with rc 125. Two different defects made that census fail. Both come
from #131's census change (`350d116bd`, 2026-10-06: the cwd scan and retained
lineage), and `release/217` has neither:

1. **A blind census.** The cwd `lsof` ran out its 10 s cap twice in a row. The
   census saw no process, but one census outlasts the whole 3 s window, so the
   second one quarantined the run.
2. **The daemon's own process group, retained as the attempt's lineage group.**
   The guardian left the daemon's group only when its Python reached
   `os.setsid()`. A table read in between recorded the daemon's group as a root
   that never empties.

Each failure was reproduced at `5de28be9e`. The CI log carries no quarantine
reason, and the run kept no artifacts, so which of the two struck CI cannot be
read. Both give its exact symptom: a finished run, `wait` returning 125 in the
same second, and the turn finishing later. Each is fixed below, with a
deterministic regression. Neither is the path in
`docs/reports/pr131-lost-run.md` (a late background waiter retained as a cwd
writer), which is fixed in this head.

## Cause 1: two blind censuses in a row

The run's attempt was `quarantined` with:

```text
"reason": "writers remain after exit receipt",
"errors": ["cwd enumeration unavailable"], "unverifiable": true,
"live_pids": [], "identities": {}, "ambiguous_identities": [], "lineage_roots": []
```

Its events are `attempt.finalizing` at 07:49:22 and `attempt.quarantined` at
07:50:02. In those 40 s the census saw no process at all. It was only
unreadable.

- #131 added C-5.5's cwd source to every deciding census: one
  `lsof -nP -d cwd -F0pn`, capped at 10 s (`procs.cwd_pids`, `procs._read`).
  When `lsof` fails or is slow, the census is unverifiable
  (`cwd enumeration unavailable`; `unverifiable = bool(errors)`).
- On this host, under the load other sessions put on it, `lsof` mostly takes
  0.4–0.7 s but sometimes takes more than 10 s. Two of 69 back-to-back calls ran
  out the cap, and spikes of 3–12 s came in clusters. Inside the test, one
  finalization census took 1.7–12.6 s. In a traced run, one census took 12.6 s
  and failed on an `lsof` timeout at 10.02 s; the retry read.
- C-5.9 allows `exit_settle_s` (3 s), measured from the end of the first census
  that is not verified empty. Every census takes longer than that, so the window
  holds exactly one retry. Two `lsof` timeouts in a row quarantine the run.

**Fix (`06cf67d5b`, C-4.2 and C-5.9 amended).** A *blind* census is one that is
unverifiable only because C-5.5 source reads failed: its only errors are
`group`, `descendant`, `marker` or `cwd enumeration unavailable`, and it shows
no live process and no ambiguous identity (`procs.Containment.blind`,
`procs.ENUMERATION_ERRORS`). It saw nothing, so it decides nothing, as a
guardian that cannot be inspected decides nothing (C-4.2):

- The attempt stays `finalizing` with every lease.
- Its census is taken again once the time the window has run has passed again
  (at least `inspect_interval_s`). At most
  `2 + ceil(log2(exit_inspect_s / inspect_interval_s))` censuses are taken.
- The wait lasts until `exit_inspect_s` (60 s, `contracts.EXIT_INSPECT_S`) from
  the window's first census. A blind census past that quarantines as before.

A census that sees a writer, or holds for any other reason (identity, lineage
overflow, publication), keeps `exit_settle_s`. Only a verified-empty census
releases, so nothing is released that was not before.

**Regression (`tests/unit/test_daemon_settle.py`).**

- `test_c5_9_two_timed_out_cwd_scans_in_a_row_do_not_lose_a_finished_run` runs
  the interleaving on a held monotonic clock: the run exits 0, two censuses take
  12 s each and fail their cwd read, and the third reads. At `5de28be9e` it
  fails: the `wait` answer maps to `Exit.JOB_LOST` and the job is `lost`.
- The bound, the pace, and a writer seen after a blind census are separate
  tests.
- A Hypothesis property (150 derandomized examples) covers every sequence of
  censuses, durations and pass spacing. Finalization ends and accepts only on a
  verified-empty census. Every census before the last ended inside its own
  window. A quarantine follows only a census past its window. The pace bound
  holds, and `wait` maps the outcome to 0 or 125.
- `tests/unit/test_procs.py` checks that an `lsof` timeout or failure, through
  the real `_read` and `cwd_pids`, gives a blind census. A property shows `blind`
  is exactly "failed source reads only, nothing seen", and never verified empty.

## Cause 2: the daemon's own group, retained as the attempt's lineage group

In the second failure, the run's attempt was quarantined with the same reason
and `cwd enumeration unavailable`, but with nine live descendants:

```text
"descendant_pids": [13593, 20198, 29493, 29496, 70410, 70480, 97650, 97670, 97672]
evidence.lineage_roots[0] = {"pid": 25531 (the guardian), "proc_start": "...09:47:31", "pgid": 97650}
start.json: {"guardian_pid": 25531, "pgid": 25531, ...}
```

Group 97650 is the daemon's (pid 20198, per `daemon.log`). In the test it also
holds pytest, `lockf`, the repeat harness and the shell that started them; 29493
and 29496 are the daemon's own `ps` and `lsof`. The guardian led its own group
(`start.json`), yet its recorded lineage root carries the daemon's group. Only a
table read before the guardian's `setsid()` shows that.

- `Daemon._launch` (and the probe and re-enrolment launches) spawned
  `python -m subfleet.guardian` with a plain `Popen`, so the guardian started in
  the daemon's process group. It left it only when its own code reached
  `os.setsid()` (`guardian.py`), after interpreter start-up and imports, which
  can take seconds on a loaded host.
- C-5.12 gives a new attempt its first table one interval after the launch
  returns. A table read in that window shows the guardian in the daemon's group.
  `_record_owned` records every descendant's `census_root`, the guardian's
  included, with its table group as a lineage root.
- C-5.5 keeps recorded groups as census roots until they are verified empty.
  The daemon is in that group, so it never empties while the daemon lives.
  Every later census lists the daemon and its helpers as the attempt's
  descendants: the finished run is quarantined and recorded `lost`, and
  self-resolution can never release it.

**Fix (`0ed9c029a`, C-5.1 amended).** All three launch paths pass
`start_new_session=True`. The forked child calls `setsid` before it execs, and
`Popen` returns only after the exec. So every table C-5.12 can give the attempt
(read after its launch returned), and every fresh census, shows the guardian
leading its own group. The guardian calls `os.setsid()` itself only when it is
not already a session leader, so a guardian started any other way still leads a
new one. Two alternatives were set aside:

- Dropping roots in the daemon's group at recording time would also guard
  against this, but nothing else can put an attempt process in the daemon's
  group. The provider runs in the guardian's session, and `setpgid` cannot leave
  it.
- The daemon's `git` helpers use `git -C`, so their cwd is never the workdir.

**Regression (`tests/fake/test_launch_gates_above_stdio.py`).**
`test_c5_1_every_guardian_leads_its_own_group_before_any_code_of_its_own_runs`
runs each launch path (attempt, C-11.4 probe, re-enrolment) with a real
guardian. A `sitecustomize` on the guardian's `PYTHONPATH` holds its interpreter
before any guardian code runs; the harness reads its process group as the
launch returns, then releases it. Before the fix, nothing has put the guardian
in a group of its own yet. After it, the guardian leads its own group, and each
provider still runs. Results are under "Tests run".

## Reproduction counts

These are runs of the unmodified test, `test_strand_timing[automatic]`, with one
pytest per lock hold. Lock queueing from other sessions took most of the wall
time. The commit message of `0ed9c029a` says "1 of 9 plain runs"; the correct
count is the one below.

| Tree | Runs | Failed (rc 125) |
| --- | --- | --- |
| `5de28be9e` (#131 head) | 21 | 2: one of each cause |
| `7b877d299` (#195 merged) | 2 | 0 |
| `ba5b2d098` (#131 before #195) | 4 | 0 |
| `origin/release/217` (`5a711114e`) | 2 | 0 |
| `06cf67d5b` (cause 1 fixed; one run in the e2e file hold) | 4 | 0 |

Four of the 21 head runs carried logging-only instrumentation of `procs._read`
and the finalization census. At these rates, a plain bisect by counts is not
possible within a six-hour job. Both causes depend on code that is identical in
the three #131 commits (`procs.py` is byte-identical from `ba5b2d098` to
`5de28be9e`, as are `_finalize`'s window, `_quarantine`, `_record_owned` and the
guardian launch) and absent from `release/217`. The #195 and #191/#194 merges are
not implicated.

A stress A/B makes cause 1 deterministic at the e2e level. It uses uncommitted
copies of each tree whose `procs.cwd_pids` fails, as a timed-out `lsof` does,
for the first 30 s after the daemon's first cwd scan:

| Tree (with the 30 s cwd outage) | Runs | Failed (rc 125) |
| --- | --- | --- |
| before the fix, `5de28be9e` | 2 | 2: run `lost`, its turn `lost` |
| after the fix, `06cf67d5b` | 2 | 0: run and turn `succeeded`, the run about 30 s after its receipt |

## Out of scope

- The kill protocol's `kill_settle_s` loop (C-5.6) has cause 1's shape: one
  slow census can outlast its 3 s, so a blind census quarantines a killed
  attempt. The job is `cancelled` (or `lost`, for a dead guardian) either way,
  so no finished result is lost; the quarantine holds the workspace until the
  next pace.
- The start-grace census (C-4.2 `starting`) still quarantines a blind census.
  It is reached only by a guardian that wrote no receipt in 10 s.

## Tests run

These are listed in the job's final answer, with counts per file.
