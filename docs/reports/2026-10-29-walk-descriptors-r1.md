# D-WALK-FD1: review r1 fixes and execution evidence

Base: `b1996dcf6bed875d7f781c6527c81f2abcc87fab` (the build plus
the hub's byte-for-byte review tests). Work began 2026-10-29 at 14:43 UTC;
the first hold was admitted at 14:45:18 UTC.

## Decisions built in fix r1 (historical)

- P2-1, commit `63dcfc10b`: each new child in `_open_path` and
  `_conflict_dir` enters its error cleanup scope before closing the previous
  descriptor. At that commit the child receives cleanup on a failed handoff,
  and the previous descriptor's close is attempted a second time (review r2
  P2-3, now fixed in fix r2). Secondary cleanup errors were suppressed while the original
  `BaseException` propagates unchanged. `_set_aside_fd` now propagates the
  original interrupt in the case that previously returned after catching
  replacement `EBADF`.

- P2-2, commit `63e3158d7`: one `_close_all` helper drains every
  remaining descriptor in the `walk` stack and `_in_repository` list,
  continuing after any `BaseException`, then raises the first close error.
  The borrowed root is excluded from cleanup.

- At `7e267a13e` the review test file differed from `b1996dcf6` only by its
  three removed decorators (six P2-1 cases, three P2-2 cases). After this report,
  the hub's `1c8e00e16` (baseline read from the fixture) and `a98ee557a`
  (`raise_at` through `sys.monitoring`) changed it. Fix r2 revises only the
  three `closed_first=False` handoff expectations and corrects the mechanism
  docstring. The opt-in mutant plugin and its r1 evidence were committed
  separately in `c00804112`.

- The original report's ownership statements have been corrected for the
  additional r1 evidence. The requested PR-body replacements appear below.
  No hub decision required changing.

## Runs

Locked dev environment: Python 3.14.7, `uv sync --locked --group dev`,
then `uv run --no-sync`. Every pytest ran serially in one shared-lock hold
with `SUBFLEET_RETENTION_DORMANT` and `PYTHONPATH` removed, bytecode and
the pytest cache disabled, and isolated TMPDIR/Hypothesis storage.
The scratch evidence entry point calls `pytest.main` and redirects
failed-state retention to task-owned scratch before conftest's pruning.
The wrapper's sysctl load read was sandbox-denied; the evidence plugin
recorded the actual loads using `os.getloadavg()`.

Times are 2026-10-29 UTC; load means 1/5/15-minute averages. Each row
is one pytest hold; file paths are relative to `tests/`. Start/load are
recorded at pytest session start. P/F mean passed/failed. Seconds measure
session start to session finish; terminal totals also include configuration.

| Hold | File / selection | Start | Load | Result | Seconds |
| --- | --- | --- | --- | --- | ---: |
| R | `unit/test_retention_descriptor_review.py --runxfail at base` | 14:45:26 | 15.97/23.05/30.08 | 38P, 9F | 9.52 |
| P1 | `unit/test_retention_descriptor_review.py, six P2-1 cases` | 14:46:09 | 19.15/23.00/29.74 | 6P | 6.33 |
| G | `unit/test_retention_descriptor_review.py, all marks removed` | 14:47:02 | 22.66/23.33/29.44 | 47P | 7.59 |
| W1 | `unit/test_retention_walk_descriptors.py` | 14:47:34 | 20.62/22.78/29.03 | 20P | 4.72 |
| W2 | `unit/test_retention_walk_descriptors.py` | 14:48:50 | 17.22/21.21/27.90 | 20P | 10.88 |
| W3 | `unit/test_retention_walk_descriptors.py` | 14:49:03 | 16.60/20.87/27.66 | 20P | 5.93 |
| W4 | `unit/test_retention_walk_descriptors.py` | 14:49:16 | 16.28/20.66/27.51 | 20P | 6.63 |
| W5 | `unit/test_retention_walk_descriptors.py` | 14:49:26 | 18.69/21.01/27.55 | 20P | 6.87 |
| A1 | `unit/test_retention_archive.py (A1 selection; live lsof excluded)` | 14:50:27 | 20.00/20.81/26.98 | 38P | 169.09 |
| A2 | `unit/test_retention_archive.py (A2 selection; live lsof excluded)` | 14:53:21 | 17.48/20.23/25.68 | 70P | 244.07 |
| T | `unit/test_retention_turns.py` | 14:58:14 | 22.05/20.84/24.34 | 7P | 5.48 |
| S1 | `unit/test_retention_sweep_race.py (12 example cases)` | 14:58:29 | 26.92/22.04/24.71 | 12P | 42.53 |
| RDB | `unit/test_retention_readback.py` | 15:09:43 | 26.87/29.53/28.34 | 37P | 228.21 |
| RG | `unit/test_retention_regenerable.py` | 15:15:38 | 47.36/37.10/31.73 | 26P | 197.78 |
| RGP | `unit/test_retention_regenerable_properties.py` | 15:19:18 | 26.42/34.12/31.90 | 1P | 81.05 |
| TI | `unit/test_timers_retention.py` | 15:20:50 | 19.00/29.82/30.48 | 20P | 10.82 |
| FD | `fake/test_attempt_tmpdir_daemon.py` | 15:21:42 | 29.18/30.78/30.79 | 24P | 50.35 |
| FL | `fake/test_attempt_tmpdir_leaks_daemon.py` | 15:23:01 | 37.56/33.10/31.66 | 8P | 22.09 |
| UG | `unit/test_attempt_tmpdir_guardian.py` | 15:23:55 | 36.26/33.50/31.90 | 2P | 7.40 |
| UL | `unit/test_attempt_tmpdir_leaks.py` | 15:24:28 | 37.92/34.04/32.15 | 197P | 74.24 |
| UC | `unit/test_attempt_tmpdir_lifecycle.py` | 15:26:15 | 26.44/31.68/31.49 | 35P | 5.73 |
| UM | `unit/test_attempt_tmpdir_mutations.py` | 15:27:06 | 24.11/30.23/30.96 | 85P | 13.25 |
| UR | `unit/test_attempt_tmpdir_retention.py` | 15:27:51 | 22.73/28.94/30.44 | 6P | 5.67 |
| US | `unit/test_attempt_tmpdir_sandboxes.py` | 15:28:27 | 23.17/28.30/30.14 | 12P | 6.60 |
| C1 | `fake/test_state_contract.py` | 15:29:10 | 23.09/27.65/29.82 | 37P | 40.64 |
| C2 | `unit/test_test_module_names.py` | 15:30:13 | 25.43/27.55/29.63 | 1P | 2.26 |
| C3 | `unit/test_invariants_index.py` | 15:30:23 | 27.27/27.88/29.72 | 25P | 2.49 |
| S2E | `unit/test_retention_sweep_race.py (15 explicit schedule examples)` | 15:30:41 | 29.30/28.29/29.83 | 1P | 199.01 |
| S2G | `unit/test_retention_sweep_race.py (40 generated schedule examples)` | 15:34:52 | 27.83/30.24/30.48 | 1P | 363.96 |

The initial combined schedule-property hold S2 started at 14:59:23
UTC with load 21.20/21.35/24.29. It approached the ten-minute
cap and was interrupted using the native tool at about 15:09 UTC (driver
exit 130). No valid pytest result or session-finish record was produced.
A cross-call SIGINT was sandbox-denied; after native interruption, the
test PID was confirmed gone with ProcessLookupError. The property was
rerun in two holds: its 15 explicit examples, then all 40 generated
examples with the usual remaining Hypothesis phases. Scratch-only
profiles selected phases before collection; collection asserted that
max_examples stayed 40. Test source and assertions were unchanged.
S2E selected `phases=(Phase.explicit,)`; S2G selected every usual phase
except `Phase.explicit`, using a profile loaded before collection. Each
phase hold reported two `PytestAssertRewriteWarning` messages because the
scratch runner imported Hypothesis before pytest initialized its plugins.
This runner adjustment preserves the requested coverage and caps each
completed hold below ten minutes. No hub decision changed.

**Red:** 38 passed and nine failed at the base with `--runxfail`.
The failure node IDs match the six handoff and three interrupted-cleanup
cases. **Green:** all 47 review cases passed without expected-failure
marks. The original descriptor file then passed 20/20 five times.

**Regression coverage:** 664 distinct passes in the narrower review set,
745 including descriptor repetitions and the split schedule-property phases.
Adding the 47 review cases gives
**711 distinct passes, 792 including repetitions**. The intermediate
six-case P2-1 check is additional. Every requested non-live case passed.
All holds were admitted under the shared lock; none ran outside it.
Every completed pytest stayed below ten minutes, and all holds started within
four hours of job start. No test bound or example count was changed.

Archive selections partition the named functions before line 858 and the
remaining functions, with `-m 'not real_lsof'` in both. The sweep examples
and schedule property phases ran in separate holds. The
[machine-readable record](2026-10-29-walk-descriptors-r1-evidence.json) retains
each invocation's exact test arguments, per-file first-test start and load,
counts, and failing node IDs.

## Mutants

All nine were killed. The [mutant table](2026-10-29-walk-descriptors-r1-mutants.md)
gives each mutant, start/load, counts, and named killing test. The two new
mutants remove both handoff guards (six failures) and stop the shared drain
at the first close error (three failures). The seven inherited mutants
reproduced their review results. No production file was modified by a
mutant; the source hash at the end of fix r1 matched its second fix commit:

`0a54853fb58923320a4ea066a23acd041a921683699b8507e7d7d98f958d3128`.

## PR-body replacements supplied by fix r1 (superseded)

**Line 9:** The audit protects validation, allocation, and registration
failures. Failed-open/mkdir descent is protected; the new child in
`_open_path` and `_conflict_dir` is owned before closing the previous
descriptor, so a failed close cleans up the child and preserves the original
exception, including cancellation. The other 21 opens have immediate
cleanup scopes.

**Line 11:** `walk` preserves `root_fd` and passes the listed completion,
injected-failure, and early-close tests. If closing a stack entry is
interrupted, cleanup closes every remaining owned descriptor before
propagating the first closing exception.

## Delivery, constraints, and exclusions

Shared Git metadata denied index.lock creation. Each coherent step was
committed on `fix/walk-fd-leak-217` using isolated scratch metadata with
read-only base-object access. Delivery is the incremental bundle
`docs/reports/2026-10-29-walk-descriptors-r1.bundle`, from `b1996dcf6bed`.
It is verified after the report commit, and its head is supplied in the
final report. The shared worktree HEAD remains at the starting commit.
Only named files were staged; every commit has the requested coauthor
trailer. No history was rewritten. No caller repository was fetched, merged,
or pushed; existing tests use only fixture repositories and local Git traffic.

The requested Darwin scratch root was sandbox-denied, so the permitted
job temp directory's `sf-fix279r1` child was used. Successful hold roots
were removed between later holds. Scratch was kept below 500 MB and is
removed with the lane's guarded cleanup after bundle verification. No
task-started process is retained.

No completed combined schedule-property result is claimed; its two phase
holds supply the coverage. Not run: the archive's real-lsof case (explicitly
deselected), the full suite, and the live frontend test (left to CI). No CI/network snapshot was
requested or taken. No live store, real CLI, real provider, or unrelated
host process was accessed; every walked fixture tree was made in scratch.

## Review r2 and fix round r2

Review r2 requested changes at `a98ee557a` for P2-3 and P3-1–P3-4. Fix r2
removes the parent-close retry, checks registration ownership, and applies the
[main report's cleanup invariant](2026-10-29-walk-descriptors.md#invariant)
to explicit error and finally cleanup in `retention_fs.py`. Normal walk
completion and successful descent handoffs still called `os.close` directly.
Review r3 then found P2-4, P2-5, P3-5 and P3-6; the broader invariant still had
those gaps at the r2 head. Its precedence rule requires no expectation changes
in the existing review or walk file. The three revised synthetic pre-release
handoff cases belong to P2-3: they now assert one parent-close attempt, child
closure, and test-owned release of the stranded parent. The [r2 report](2026-10-29-walk-descriptors-r2.md)
contains current run and mutant results, plus replacement PR-body lines.


## Fix round r3 correction

The [r3 fixes and evidence](2026-10-29-walk-descriptors-r3.md) close the four
executed gaps: explicit operation exit state, `size` root cleanup, pop ownership,
and explicit ownership of all ten walk consumers. The main invariant is achieved
again in the exercised scenarios. Coverage of every possible cleanup interruption
extends only as far as the tests, including the reported marker-to-call boundary.
All r1 run and mutant counts above remain historical evidence.


## Review r4 and fix round r4

Review r4 reran all ten r3 cases successfully on both Pythons at `b698146d0`,
then found P2-6: constructing or entering `_cleanup` after acquisition can leak
one descriptor with no close attempt. At the r4 starting head `9b1a9d462`, all
four cases fail with marks bypassed on Python 3.14.7 and 3.12.14. Fix r4 enters
35 preallocated descriptor-slot owners before acquisition; the original four
cases and the 140-case matrix across every converted site pass on both Pythons.
Only the r4 expected-failure decorator was removed from the review file.

The `_cleanup`/`_close_all` paths receive the operation's explicit exit exception,
including `AttemptTemp.size`'s root close. In those paths, handled caller
exceptions no longer suppress cleanup failures or cancellation. Once
`_close_all` is entered and its iteration is not asynchronously interrupted,
it attempts each remaining owned close once. The ten walk consumers close their
iterators after owner setup succeeds, subject to the listed exit boundary.

The [fix r4 report](2026-10-30-walk-descriptors-fix-r4.md) records the full site
list, red/green runs, current mutant results and PR-body replacements. L1–L5
remain listed differences with review r4's executed inputs: pop marker-to-call,
normal exit dispatch, legacy raw-handle precedence, repeated interruption of
the drain, and native open-return-to-store. None is fixed by this round. In
particular, preallocation does not make native acquisition and slot storage
atomic. The invariant is verified for entered owners and exercised inputs;
claims extend only as far as these tests. Historical run and mutant counts
above are unchanged.
