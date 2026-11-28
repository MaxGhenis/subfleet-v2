# #356 · D-REMOTE-JOBS1 build report

The design note is complete. The record-and-show slice is blocked before its
required red test run. `uv sync --group dev --offline` exited 2, so work stopped
at the dependency gate in the brief. Git also refused to create the worktree
index lock. HEAD remains at `f2fa1a6d67d39adf07d3579a265c579467b6bbca`.

## Files and rulings

| File | Work left in the checkout |
| --- | --- |
| `docs/design/remote-jobs.md` | Five-part design. Lines 20–25 cover the separate table, migration, row, and provisional state (rulings 1–3). Lines 9–18 and 24–31 cover adoption, forgetting, uniqueness, account labels, privacy, and events (4, 5, 7). Lines 82–89 cover listing and conditional online and offline status (5, 6). Lines 33–38, 40–78, and 88–113 cover local resource isolation, wait, and later work (8). |
| `tests/fake/test_remote_jobs_admission.py` | Unverified drafts for ruling 8. Lines 52–88 compare one-lane admission and disk reserves before and after adoption and check the contents of the local `jobs` table. Lines 91–110 compare retention, and lines 113–124 compare `wait --mine`. The tests use synthetic roots and an in-process CLI transport. |
| `docs/reports/2026-11-28-remote-jobs.md` | This report. |

The proposed Claude URL is `https://claude.ai/code/session_<id>` with a
nonempty ASCII alphanumeric suffix. The hub checked that shape against actual
session URLs. The proposed Codex URL is
`https://chatgpt.com/codex/tasks/task_<id>` with a nonempty suffix of ASCII
letters, digits, or underscores. The Codex shape is the hub's unverified
assumption. These are proposed parser inputs.

The migration, parser, record commands, status integration, and unit tests
remain unwritten. The next free contract clause is C-17.10. The clause,
changelog, and any required invariants index update remain pending. The
existing notice-wake migration test assumes its step is the latest and will
need to assert the version and event sequence by named constants when the next
migration is added.

The design defers polling, launching, steering, and usage accounting. Polling
needs each provider's verified read-only status interface, structured status
evidence, and account and workspace selection. An inaccessible workspace keeps
the last observation. Open questions include the Codex URL, stale-state
display, later remote waits, result links, and how to confirm steering and
attribute usage.

## Commands and evidence

```sh
date -u +%FT%TZ
git rev-parse HEAD
git status --porcelain --untracked-files=no
gh issue view 356 --repo MaxGhenis/subfleet-v2
getconf DARWIN_USER_TEMP_DIR
uv run --no-sync python -c 'import subfleet; print(subfleet.__file__)'
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
uv sync --group dev --offline
```

The first command block started at 2026-11-28T02:33:56Z. HEAD matched the
brief and tracked status was empty. The GitHub issue read succeeded. Both uv
commands reported the same cache initialization error.

```text
error: Failed to initialize cache at `/Users/maxghenis/.cache/uv`
  Caused by: failed to open file `/Users/maxghenis/.cache/uv/sdists-v9/.git`: Operation not permitted (os error 1)
```

The named-file commit attempts were:

```sh
git add docs/design/remote-jobs.md && git commit -m 'Design remote job tracking'
git add docs/reports/2026-11-28-remote-jobs.md && git commit -m 'Report remote jobs build results'
```

Git refused staging with `Operation not permitted` at
`/Users/maxghenis/subfleet-v2/.git/worktrees/20261127-213017-rj-build-r1/index.lock`.
The files are available in the worktree for salvage.

No pytest run started. Each drafted fake test has zero observed base failures
and zero verified passes. The parser host, local `jobs` write, and
unconditional JSON key mutants were never applied. Each required module has
zero recorded passes, failures, and skips because it did not run.

## Footprint

The requested Darwin temporary directory denied `mkdir` for
`remotejobs-r1`. I created the in-worktree fallback
`build/remotejobs-r1`, verified that the prospective
`build/remotejobs-r1/pytest-01-red` real path met the pytest path rule, and
removed the empty fallback using a resolved-path guard before removing its
empty `build` parent. No folder was created outside the workspace.
No virtual environment or pytest base directory was created.

No pre-test checks were due. A diagnostic snapshot at 02:41:43Z from
`date -u +%FT%TZ`, `df -H /`, and `uptime` showed 76 GB available and
load averages of 84.67, 73.61, and 57.84. The fallback held 0 bytes at that
check. Cleanup completed at 02:42:39Z.

The design note (7,515 bytes), fake tests (5,130 bytes), and this report
(4,840 bytes) remain uncommitted in the worktree. The design directory
holds only the note. No live state, cloud provider, or licensed data was used.
