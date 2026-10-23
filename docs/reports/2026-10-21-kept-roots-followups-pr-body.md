#238's review r1 left four findings outside that PR's scope (2, 3, 5 and 6). This fixes all four (D-KR1). The KR1 changes touch tests and docs; the release merge also imports release's existing production changes. `.github/workflows/tests.yml` is unchanged. The full report is `docs/reports/2026-10-21-kept-roots-followups.md`.

**Changes:**

- **A failed test's evidence survives a failing teardown (finding 2).**
  - **Cause:** in `tests/fake/conftest.py`'s `daemon` and `tests/e2e/conftest.py`'s `e2e`, the keep code ran after `harness.close()` in the same `finally`. When `close()` raised (r1 saw `TimeoutExpired` stopping `tests.fake.run_daemon`), the copy never ran and `TemporaryDirectory` deleted the root.
  - **Fix:** both now keep in a nested `finally`, as `test_core_live.py`'s `dev_daemon` does, and `close()`'s error is raised after the copy.
  - **Behaviour:** the root is kept when the call failed or `close()` raised. A copy that fails is printed to stderr and never replaces the teardown's error.

- **The layout is pinned (finding 5).** `tests/unit/test_failed_keep_layout.py` checks three things:
  - no string or bytes literal under `tests/` names the kept-roots folder outside `tests/failed_keep.py` (docstrings and comments may), and no non-Python file there names it at all;
  - every harness that keeps roots imports and calls `failed_keep_dir`;
  - the workflow's one `sf-failed-*` upload has `path:` equal to `failed_keep.ROOT`.

  The base's one stray literal, `Path("/tmp/sf-failed")` in `tests/unit/test_failed_keep.py`, is gone.

- **The frontend probe cache can't hand out a truncated binary (finding 3).** `tests/frontend/swift.py` copies the probe to a `mkstemp` name in the cache folder (`.<digest>.XXXX.partial`), then `os.replace`s it into place, and removes the temporary file if the copy fails.
  - Readers check neither size nor digest. They don't need to: a digest name is only ever created by an atomic rename of a whole file.
  - The one gap is a file a pre-fix writer left truncated in a long-lived checkout's `build/`. Fresh worktrees and CI have no `build/`.

- **Kept roots are pruned (finding 6).** `prune` in `tests/failed_keep.py` runs from `tests/conftest.py`'s `pytest_sessionstart`, before any test. A folder `/tmp/sf-failed/<pid>/` is removed only when all of these hold:
  - it is a real directory whose name is a pid;
  - `os.kill(pid, 0)` raises `ProcessLookupError` (EPERM and other errors count as alive), and the pid is not the session's own;
  - its mtime is more than 24 hours old.

  Each folder is claimed by a rename first. So of several pruners (xdist workers), exactly one removes it, and a folder written or a pid revived in between is put back. The rule is in `docs/lanes/README.md`. It changes nothing on CI: runners start fresh, and a retry's first attempt is minutes old.

**Invariants** (each tested; each test fails against a mutant that breaks it):

1. A failed test's root is kept whether or not teardown raises.

2. Only `tests/failed_keep.py` builds the path, and CI uploads its `ROOT`.

3. The probe cache never exposes a partial file under its final name.

4. Pruning never removes a live process's folder, nor one younger than 24 hours.

**Decision audit:** all four decisions are implemented. No further implementation changes were needed
in this continuation. The cached `origin/release/217` at `43f42d83d` was merged as `df751af86`, retaining
both parents' contract clauses and change logs. Commits use workspace-local `.git-local` metadata on
`fix/kept-roots-followups-217`; nothing was pushed.

**Tests:** 25 focused cases passed: helper 2, layout 4, teardown 4, probe cache 4, pruning 11.
State contract: 37 passed; module names: 1 passed. Selected fake resume file: 1 skipped;
selected e2e guard/isolation file: 5 skipped (process inspection unavailable).

| Directory | Selected | Passed | Skipped | Failed | Unrun |
|---|---:|---:|---:|---:|---:|
| `tests/e2e` | 5 | 0 | 5 | 0 | 0 |
| `tests/fake` | 1972 | 37 | 1 | 0 | 1934 |
| `tests/unit` | 13333 | 6136 | 9 | 2 | 7186 |

The positive run completed **6,173 passes, 15 skips and two environment failures** (`/bin/ps` denied,
and a denied sysctl kernel-ceiling read). **9,120 collected cases remain unrun**, plus the baseline rerun
that never acquired a slot before the cutoff. The report certifies every selected file individually.
The first slot exceeded 20 minutes because its timer excluded startup; subsequent holds used a parent
supervisor with a 17 min 30 s deadline that includes startup.

**Mutations:** all four required mutants were caught in a scratch archive of the merged source:
**10 expected failures, 15 passes**, in 34.18 seconds (48.9 seconds for the whole slot command).
They cover copy-before-close, the stray literal, direct copying to a final cache name, and pruning a live pid.
The named failing tests and earlier development notes are in the report.
