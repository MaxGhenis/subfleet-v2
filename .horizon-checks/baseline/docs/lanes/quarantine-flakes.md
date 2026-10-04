# Lane brief: quarantine-flakes (two load-dependent false quarantines)

You are diagnosing a defect in subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/quarantine-flakes`). Two daemon tests quarantine an attempt when the full suite runs and pass when run alone:

- `tests/fake/test_daemon_contract.py::test_c5_6_c13_1_writable_kill_salvages_dirty_workspace_after_verified_containment`
- the Claude workspace-write case of `tests/e2e/test_guard_and_isolation.py::test_provider_environment_isolated_in_each_sandbox`

Both were seen on 2026-09-05 after the state-root marker fix (C-5.1, C-5.5: `SUBFLEET_ROOT` is the second marker), so the cause is not the cross-root marker collision that fix removed. The kept state roots showed a live pid outside the recorded process group at kill time. A quarantine that a rerun does not reproduce is exactly the class of defect the seven-day soak exists to catch, so the release gate wants a root cause, not a retry.

## What to read first

- `docs/acceptance-contract.md` C-5.1 to C-5.7 (containment, quarantine, the three census sources) and C-13.1 (salvage after a writable kill).
- `subfleet/procs.py` `containment()` (group, parent chain, markers; any failed inspection prevents release) and `subfleet/daemon.py` `_quarantine`, the kill path around line 1599 to 1626, and the start-grace path around line 1543.
- `subfleet/guardian.py` (start and exit receipts) and `tests/bin/{codex,claude,fakeprov}` (the fake providers; `SUBFLEET_FAKE_SCENARIO`, `SUBFLEET_FAKE_DELAY_S`, `SUBFLEET_PROBE`).
- `tests/fake/conftest.py`: on failure the `daemon` fixture copies the state root to `/tmp/sf-failed/<test name>/`; read `daemon.log`, the `events` table (`attempt.quarantined` carries the containment census as data), and the receipts under the attempt directory.

## Steps

1. Reproduce. Run the full suite (`export PATH=/usr/sbin:/sbin:$PATH; uv run pytest -q`) up to three times, or run the two tests while a second `uv run pytest -q tests/fake` runs in another process to create load. Record how often each quarantines.
2. From a kept root, identify the pid the census found outside the group: its ppid, command, and which of the three sources saw it (`containment.groups`, `descendants`, `markers` in the event data). Decide whether it was a real escapee of this attempt, a process of another test, a pid reused after exit, a `ps` snapshot race between the group and the parent-chain reads, or a zombie/exiting state the filter does not cover.
3. Fix the cause at its source. The census must stay conservative (C-5.5: never release on inferred authority; any failed inspection prevents release). A fix that makes the census more precise, for example one `ps -axo pid,ppid,pgid,stat,command` snapshot read once and interpreted for all three sources, or excluding the daemon's own probe or guardian helpers that legitimately live outside the group, is in scope. A fix that widens what counts as contained is not, unless you can show from the contract that the pid was never this attempt's to begin with; say so in the commit message.
4. Add a regression test that reproduces the race deterministically (a fake provider scenario or a harness that injects the offending process shape), so the fix is not "passes when run alone" again.
5. Run the full suite three consecutive times green (`3425 passed, 5 skipped` on `main` at the time of writing, plus your new test). Commit in coherent pieces, one behaviour per commit.

## Out of scope

Anything not on the containment, kill, or salvage path. Do not edit the contract; if a clause must change, say exactly what and why. Do not weaken or skip a quarantine to make a test pass.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Never commit to `main`, never force-push, never write under `~/.claude`. Commit on `lane/quarantine-flakes`; you have no network, so do not push: the integrator pushes. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Headings: Root cause (the pid shape, with evidence from the census data); Fix (files, one line each); Regression test (name and what it injects); Tests (command, count, time, how many consecutive runs); Open questions for the integrator. No preamble.
