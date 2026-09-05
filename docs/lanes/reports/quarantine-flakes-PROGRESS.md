# quarantine-flakes progress

## State

Blocked on incident evidence and a process-capable test environment. Neither
reported false quarantine has been reproduced. No runtime, test, or contract
behavior has changed. Containment must remain conservative under C-5.1–C-5.7;
salvage follows C-13.1.

## Done

- Confirmed the worktree and branch; initial checkout was clean.
- Read the lane brief, containment contract, process census, and guardian receipts.
- Final report path: `docs/lanes/reports/quarantine-flakes-OUTPUT.md`.
- Read kill, finalization, start-grace, probe, fake-provider, and harness paths;
  obtained an independent read-only review of candidate races.
- `/tmp/sf-failed` is absent. Existing top-level `/tmp/sf-*` state databases
  examined read-only contained no quarantined attempt rows. Requested the
  original kept-root paths from the user; no answer received yet.
- `/bin/ps` and `sysctl kern.boottime` are denied by the sandbox. Both target
  tests therefore skipped (2 skipped in 0.04 s); quarantine frequency is unknown.
- Installed pytest 9.1.1 and its dependencies offline from local cache with
  `uv sync --offline --group dev --no-install-project`. Full editable-project
  installation remains incomplete: normal sync failed on DNS, offline sync
  lacked hatchling metadata, and a subsequent cache copy hit a denied write
  through a preserved cache symlink. Did not bypass that denial.
- Baseline `uv run --offline --no-sync pytest -q`: 3232 passed, 66 skipped,
  131 failed, 1 error in 72.56 s. Log:
  `/tmp/quarantine-flakes-baseline-pytest.log`. Socket/process restrictions are
  visible among the failures; this is not a green acceptance run.
- Existing process-unit tests: 22 passed in 0.05 s.
- Existing combined process-unit and deterministic daemon-state tests:
  57 passed in 0.97 s.
- A PreToolUse hook rejected an unscoped `rg --files /tmp` command. Did not
  retry or bypass that blocked search. All refusals will be recorded in the report.
- Final report written to `docs/lanes/reports/quarantine-flakes-OUTPUT.md`;
  the lane remains unresolved, with no fix or new regression claimed.

## Findings (unconfirmed incident causes)

- Separate group/parent/marker snapshots can classify a newly born group member
  as descendant-only. One snapshot would improve precision but does not establish
  why an unrelated process remained after Claude's completed exit receipt.
- Parent walking starts at numeric guardian/child PIDs without comparing those
  roots against recorded start identities; PID reuse can introduce unrelated
  descendants. No incident PID, PPID, command, or source sets are available here.
- Start-grace handling reads `start.json` before the census and does not reread
  it before quarantine. A guardian can publish its receipt during inspection.
- The fake Claude spawns no children, and the guardian reaps it before publishing
  `exit.json`. Helper environments are sanitized and probes have distinct markers;
  no evidence supports excluding helpers or weakening quarantine.

## Next

- Three consecutive green full-suite runs remain outstanding (zero green).
- Integrator: supply original kept roots or rerun in an environment permitting
  C-5.3/C-5.5 process inspection and local socket binding. Capture the outside-group
  PID's PPID, PGID, state, executable, start identity, source sets, and receipts.
- Once evidence distinguishes the candidates, fix that cause and add the matching
  deterministic regression, then run the full suite three consecutive times green.
