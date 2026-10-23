# Lane briefs

One file per lane: the assignment, the contract clauses it binds, its worktree and branch, and the shape of its final message. Reports the lanes wrote back live under `reports/`. The integrator (the Fable session that owns `main`) merges lanes; lanes never commit to `main`.

## Which provider can do what

- **Astra (Codex) lanes** run inside the Codex sandbox with no network. They can build and run the unit suites, but the sandbox denies `/bin/ps`, `sysctl`, and unix-socket binds, so every test that starts a real daemon or guardian skips or fails there: `tests/fake`, `tests/e2e`, `tests/process`, and any test marked as needing process inspection. A Codex lane cannot reproduce or verify a containment, kill, quarantine, or recovery defect. Give those to a Claude lane or to the integrator, and say so in the brief (learned 2026-09-05, `quarantine-flakes`: the lane spent its run confirming it could not observe the process table).
- **Claude lanes** run unsandboxed under `--dangerously-skip-permissions` and can run the whole suite. They push their own branches. They die on the account's five-hour or weekly limit with rc 4 and an empty output file; v1 has no reading for most Claude accounts, so pick one whose last limit is more than five hours old, or check the lane's transcript for the `resets` time before retrying.
- **Codex lanes cannot push.** Briefs for them say "commit; do not push"; the integrator pushes after review.
- Both kinds must be launched with `subfleet run` from the integrator's session, never with the provider CLI directly; a directly launched lane dies with the session.

## Worktrees

Lane worktrees live under `~/subfleet-v2-lanes/<lane>/` on branch `lane/<lane>`, created from `main` (`git worktree add -b lane/<lane> ~/subfleet-v2-lanes/<lane> main`). Never under `/tmp`. Each worktree has its own `.venv` (`UV_PROJECT_ENVIRONMENT=$PWD/.venv`); after a Codex lane, run `uv sync --group dev` there before running the daemon suites, because the sandbox's offline sync leaves out the project's console scripts (`subfleetd`), which the end-to-end harness needs.

## Running the physical suites

```
export PATH=/usr/sbin:/sbin:$PATH
uv run pytest -q
```

A sandboxed shell without `/usr/sbin` makes every process-inspection test skip. Failed daemon tests keep their state root under `/tmp/sf-failed/<pid>/<test name>/`, one folder per pytest process, so two suite holds never overwrite each other's evidence (the fake-daemon and end-to-end harnesses, and the app core's live test, `tests/frontend/test_core_live.py`): `daemon.log`, the store, receipts, and any quarantine census live there. A failed test's root is kept even when the harness's teardown raises, and so is the root of a teardown that raised. The live test adds the probe's record under `probe/` (`live-checks.json`, `live-state.json`, `exchanges.jsonl`, and `app/`, the app's outbox and staged attachments). CI runs the suite in shards (`.github/workflows/tests.yml`, `tools/ci_shard_plugin.py`). A shard that fails or is cancelled uploads `/tmp/sf-failed` as the artifact `sf-failed-python-<version>-shard-<i>-of-<n>-attempt-<k>`. Every shard also uploads `pytest-outcomes-python-<version>-shard-<i>-of-<n>-attempt-<k>`: each test phase's outcome and duration, which `tools/ci_shard_durations.py` turns into the shard map (`.github/test-shards.json`).

Kept roots are pruned at the start of every pytest session, before any test runs (`prune` in `tests/failed_keep.py`, called from `tests/conftest.py`). A folder `/tmp/sf-failed/<pid>/` is removed when its process is gone (`os.kill(pid, 0)` finds no such process) and nothing has been kept in it for more than 24 hours (the folder's own mtime, which changes whenever a test's root is added to it). The pruner never removes the folder of a live process (another user's, which answers EPERM, counts as live) or of the session itself, a folder 24 hours old or younger, or anything whose name is not a pid: files, symlinks and other names are left alone. A folder it claims but cannot fully remove keeps its pid name, and a later session tries again once that folder's current mtime is over a day old (a partial removal can refresh it, or leave it as it was); a claimed `.pruning-*` folder left behind (by an interrupted pruner, a put-back that failed, or a sweep that could not finish) goes once its pruner and its pid are both gone and it is over a day old. Copy a root elsewhere to keep it longer. Pruning changes nothing on CI: each job starts on a fresh runner with no `/tmp/sf-failed`, and a known flake's retry (`tools/ci_retry_known_flakes.py`) is a second pytest process started as soon as the first ends, well inside a day, so the first attempt's folder survives to be uploaded beside the retry's.
