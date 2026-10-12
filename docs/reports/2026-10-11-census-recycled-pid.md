# Containment census and recycled PIDs — 2026-10-11

Fix round 1 starts at `cb23ab373f1da830f276661f1390b11beb3ddfa1`, with integration
base `be7afd599` (`release/217`). Work stays in the assigned checkout, on
`fix/census-recycled-pid` in `.git-local`; shared Git metadata is outside the
writable workspace. Each coherent step is committed without rewriting history.
No remote Git operation or live-state change, and no sub-agents.

The original incidents were qyp16-r1c (child 4203), pb-kit-u4-docs (leader 243),
and pe-uk-2192-r2/a2 (child 16379). The previous census counted strangers under
recycled root/group PIDs, causing false quarantines and successful runs reported
lost. Start-identity filtering fixes those incidents, but the reviewed head
introduced unsafe disappearance of genuine survivors.

## Causes and fixes

**C-6.5 writer refusals (five fanout tests and one policy test):** the branch added
`procs.liveness = lambda *args: "dead"` to the borrowed `state_daemon` fixture.
Tests supplied live or unreadable identities through `procs.identity`, but the
stub bypassed comparison and told the writer precheck the holder had died.
Production `_instance_alive` and `_writable_precheck` already implement the
contract's cautious three-way rule. Keep the real liveness function, default
identity reads to the synthetic table, and let behavioral tests override either
seam. New fixture regressions pin live, absent and unreadable overrides.

**C-10.3 desktop signal (five tests):** the same fixture also added an always-empty
snapshot. Registry validation therefore removed the simulated desktop caller.
Its default synthetic table now includes the fixture's own caller; unlisted
attempt guardians remain absent. A regression pins both registry validation and
liveness for that row without host process inspection. These two CI causes are
fixture defects, separate from P1; no writer or desktop production refusal was
weakened, and all existing behavioral assertions are preserved.

**Module collection:** unit and fake modules shared `test_census_recycled_pid.py`.
The fake module is now `test_census_recycled_pid_daemon.py`. The review's unit
fixtures use `test_census_survivors_review.py`, preserving the specifically
requested fake name `test_review_r185_adversarial.py` without another collision.

**P1:** only live guardian/child PIDs seeded the walk. Filtering a recycled,
absent or zombie guardian could hide its live descendants, and a fully
reparented recorded writer outside those roots was ignored. Seed every recorded
identity independently unless positive comparison proves reuse. Retain absent
and zombie PIDs as parent-link anchors for non-atomic scan rows. Matching and
incomparable recorded survivors retain protection even in their own sessions.
The adopted unit fixtures cover absent, zombie and recycled guardians, plus the
owned survivor's individual kill loop; the writable fake fixture proves
`--confirm-dead` keeps both job- and attempt-held leases while PID 43 survives.

**P2 and identity uncertainty:** attempt receipts and process births are separate
wall-clock samples. A clock step can put a genuine birth before start or after
exit. Remove both receipt bounds as proof of reuse; preserve the existing API
arguments for callers. An incomplete recorded identity, shifted legacy boot
timestamp, failed comparison or live row without a start makes the census
unverifiable. Do not repair a missing snapshot start with a later PID read that
could observe a different incarnation. `identity` raises for a live PID with an
unavailable start, and `liveness` answers unknown for an observed live process with an incomplete
record; observed absence or zombie state still proves death independently.
Added regressions cover each seam and a clock step forward as well as the
reviewer's rollback case.

An additional regression excludes a positively recycled zombie leader and its
foreign group while retaining a separately recorded orphan. A zombie whose recorded start still matches, with no live group, needs no boot read. The matching-identity property pins the
complete recorded live walk and its identity map, including orphans; the old
walk-equality expectation would contradict the newly required recorded source.

The old unit assertion that an incomplete live identity was dead, and the five
receipt-bound expectations, contradicted C-5.3's unknown-never-establishes-death
rule. Their replacements require retained protection and unknown/unverifiable
results for every input, including births outside either bound. No test was
deleted or weakened to accept unsafe release.

**P3:** verified the raw tagged source directly. Apple's
[XNU 12377.1.9 `forkproc`, lines 970–972](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/bsd/kern/kern_fork.c#L970-L972)
rejects candidates present in the process, group or session hashes. Corrected
the code comment, C-5.5 and this report. A proven different leader excludes the
old group; an absent leader retains surviving group members. Reuse of the
leader says nothing about recorded writers that already escaped that group.

C-5.3/C-5.5 and a dated Changes entry state these rules. Markers and the signal
ownership checks are unchanged; census identities never confer authority to
signal. Recycled evidence keeps only recorded/current identity and fixed reasons,
never command or environment data. All deciding callers retain `Daemon._contain`.

## Validation (in progress)

Baseline comparisons freeze the starting head's `identity`, `liveness`,
`containment` and state fixture in a job-local pytest plugin. Original test copies
retain the starting assertions and generators; copies outside `tests/` explicitly
register its shared fixtures, including network/desktop isolation and cache resets. These files and all logs stay in
ignored local metadata; they are not committed. This lets implementation proceed
while the shared lock queues without changing the measured baseline.

Every pytest invocation runs one file under its own prescribed Darwin TMPDIR and
`lockf` hold, with cleanup by `shutil.rmtree`. A timeout inside the hold caps each
pytest at 1,100 seconds; time waiting for the lock is outside that cap. Dependencies
were installed from the unchanged lockfile.

| File | Starting head | Fixed head |
|---|---:|---:|
| unit/test_test_module_names.py | 1 failed | pending |
| fake/test_writable_fanout.py | 20 passed, 5 failed; 3 pending after cap | pending |
| fake/test_policy_sandbox.py | 23 passed, 1 failed | pending |
| fake/test_admission_priority.py | pending | pending |
| unit/test_census_survivors_review.py | pending | pending |
| fake/test_review_r185_adversarial.py | pending | pending |
| unit/test_census_identity_uncertainty.py | 8 failed | pending |
| fake/test_state_fixture_processes.py | 1 passed, 3 failed | pending |
| unit/test_census_recycled_pid.py | 23 passed | pending |
| fake/test_census_recycled_pid_daemon.py (previous name before) | 8 passed | pending |
| fake/test_state_contract.py | pending | pending |
| unit/test_procs.py | pending | pending |

The Hypothesis generator now gives each root live, absent, zombie and recycled
states, and always includes a genuinely recorded escaped survivor reparented to
init in its own session. Seven properties run 200 examples apiece. Invariants:

- A census is never verified empty while any matching recorded identity is alive.
- Excluding a recycled PID never removes a different matching live process.
- Incomparable identity produces unverifiable, never death or verified empty.
- The set of PIDs that may be signalled does not grow: the generated kill property
  keeps the old identity and refuses the recycled incarnation; the review's kill
  fixture still signals its previously recorded escaped writer.
- Marker discovery is unchanged, and matching identities never shrink any existing
  source. Independent recorded survivors may extend the descendant source.

The first full fanout batch reached its 1,100-second cap after 20 passes and
five failures. Its progress order matches the five CI failures; a short rerun
will collect their full assertions and cover the remaining three cases. Future
runs record each completed case in ignored local evidence. No interrupted
pytest process remains alive after its hold. Syntax compilation and named-file
Git diff checks passed.

Unrun while validation is pending: remaining table rows, the full repository
suite, real-process/live-provider suites, live incident replay and unmerged
PR #131 integration. Final measured counts will replace this draft.

## Updated PR description for the hub

Prevent recycled guardian and child PIDs from keeping completed attempts
quarantined while retaining genuine escaped writers after their ancestor dies,
becomes a zombie or is recycled. The census walks from all recorded survivors,
keeps uncertain identity checks unverifiable and uses comparable identities
rather than wall-clock receipt bounds to prove reuse. Signal ownership remains
subject to the existing recorded-identity and fresh-read checks. Correct the
XNU citation to the tagged source's lines 970–972.

Repair the shared fake process fixture that bypassed writer refusals and hid
simulated desktop callers, give the fake recycled-PID module a unique basename,
and add the independent review fixtures and departing-root generated cases.
Validation counts are recorded above. No remote PR edit was attempted.
