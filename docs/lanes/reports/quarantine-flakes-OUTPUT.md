# Root cause

**Unresolved; release gate remains unmet.** The sandbox denies `/bin/ps` and
`sysctl kern.boottime`, so both named tests skipped before exercising their
daemon paths. Neither incident was reproduced; quarantine frequency is unknown
for both tests, not zero.

`/tmp/sf-failed` is absent. Read-only inspection of existing top-level
`/tmp/sf-*` state databases found no quarantined attempt rows. The original
kept-root paths were requested but were not supplied during this run. There is
therefore no incident PID, PPID, command, or census source set to report.

Source review identified distinct candidates, not an established incident cause:

- `subfleet/procs.py:160–209` reads separate group, parent, and marker snapshots.
  A new group member can appear only in later sources. A unified snapshot would
  improve group attribution but would not alone explain a live PID after the
  Claude provider was reaped.
- `subfleet/procs.py:176–183` roots the parent walk at numeric guardian/child
  PIDs without comparing their recorded start identities. PID reuse could
  introduce an unrelated process and its descendants.
- `subfleet/daemon.py:1509–1543` does not reread `start.json` after a slow
  start-grace census; a receipt published during that census can be missed.

The Claude test's fake provider spawns no children; the guardian waits for it
before publishing `exit.json`. Inspection helpers receive sanitized environments
and probes use distinct attempt markers. No evidence supports blanket helper
exclusions, treating an exiting process as a zombie, or weakening quarantine.
The fake salvage test's final standalone containment assertion also omits the
root, but that cannot explain an already-quarantined attempt.

# Fix

- `PROGRESS.md`: committed investigation state, findings, blockers, and next steps
  from the start of the lane.
- `docs/lanes/reports/quarantine-flakes-OUTPUT.md`: committed this blocked handoff.

No runtime, test, or contract changes. A speculative census change would not
satisfy the requested evidence-based root cause.

# Regression test

None added: the offending process shape remains unidentified. Once the incident
is recovered, inject its actual shape: a child born between census reads, a reused
root PID with a different start identity, or a receipt published during start-grace
inspection. Existing deterministic process/state tests pass, but do not prove
either reported flake is fixed.

# Tests

Commands used `PATH=/usr/sbin:/sbin:$PATH`,
`UV_CACHE_DIR="$PWD/.uv-cache"`, and `UV_PROJECT_ENVIRONMENT="$PWD/.venv"`.

| Command | Result | Time | Consecutive green runs |
| --- | --- | --- | --- |
| `uv run --offline --no-sync pytest -q` | 3232 passed, 66 skipped, 131 failed, 1 error | 72.56 s | 0 full-suite |
| `uv run --offline --no-sync pytest -q tests/unit/test_procs.py tests/fake/test_state_contract.py` | 57 passed | 0.97 s | 1 targeted |
| `uv run --offline --no-sync pytest -q tests/unit/test_procs.py` | 22 passed | 0.05 s | 1 standalone |
| Target command below | 2 skipped: denied process inspection | 0.04 s | 0 exercised runs |

```sh
uv run --offline --no-sync pytest -q \
  'tests/fake/test_daemon_contract.py::test_c5_6_c13_1_writable_kill_salvages_dirty_workspace_after_verified_containment' \
  'tests/e2e/test_guard_and_isolation.py::test_provider_environment_isolated_in_each_sandbox[workspace-write-haiku-success-allowed-claude]' -rs
```

Full-suite log: `/tmp/quarantine-flakes-baseline-pytest.log`. Failures include
denied local socket binding and process inspection; the run cannot establish
acceptance. No test was altered or skipped by this lane. Three consecutive green
full-suite runs remain outstanding; repeating the blocked run would not exercise
the target paths.

Normal `uv sync --group dev` failed on DNS while fetching pytest. Offline
`uv sync --offline --group dev --no-install-project` installed pytest 9.1.1 and
its dependencies from existing local cache. Editable-project installation remains
incomplete: offline sync initially lacked hatchling metadata, and a later cache
copy encountered a denied write through a preserved symlink into the external
cache. Tests consequently used `--offline --no-sync` against this checkout.

# Open questions for the integrator

- Where are the September 5 kept roots, including `daemon.log`, quarantine event
  data, and start/exit receipts? Recover the PID's PPID, PGID, state, executable,
  and start identity alongside its `group_pids`, `descendant_pids`, and
  `marker_pids` membership. Do not persist raw environments or secrets.
- Continue reproduction in a lane permitting process inspection and local socket
  binding. Then implement the evidenced cause, add its deterministic regression,
  and complete three consecutive green full-suite runs. No contract amendment is
  proposed. All commits are on `lane/quarantine-flakes`; nothing was pushed.

Refusals respected: the sandbox denied `/bin/ps`, `sysctl kern.boottime`, and the
cache-copy write. A PreToolUse hook rejected `rg --files /tmp` because an unscoped
broad-root search can create excessive load. Those denials were not bypassed.
