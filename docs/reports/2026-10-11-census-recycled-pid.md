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
unavailable start, and `liveness` answers unknown for an observed live process
with an incomplete record; observed absence or zombie state still proves death independently.
Added regressions cover each seam and a clock step forward as well as the
reviewer's rollback case.

An additional regression excludes a positively recycled zombie leader and its
foreign group while retaining a separately recorded orphan. A zombie whose
recorded start still matches, with no live group, needs no boot read. The
matching-identity property pins the
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

## Validation

Validated implementation/test head: `8aa9ae7cb5f05cf46b836b748b5d7606ed336b06`. The final report commit changes
only documentation. Runtime: CPython 3.14.7 with free threading, dependencies
installed from the unchanged lockfile. Python 3.12 was not rerun locally.

Baseline comparisons freeze `cb23ab373`'s `identity`, `liveness`, `containment`
and state fixture in a job-local pytest plugin. Original test copies retain the
starting assertions and generators. Copies outside `tests/` register its shared
network/desktop isolation and cache fixtures. Plugins and all logs/evidence remain
ignored local artifacts; no logs, JSON evidence or bundles are committed.

Every pytest invocation selected one file under its own prescribed Darwin
TMPDIR and `lockf` hold. Each wrapper cleaned its TMPDIR with `shutil.rmtree`.
A watchdog inside the hold capped pytest at 1,100 seconds and terminated its own
process group on timeout. All runners exited; no detached runner remains.

Counts below are distinct pytest items, combining short selections and excluding
repeated probes. The four CI files total **85 passed after**, and all **12 CI
failures reproduced before**. Final coverage is **229 passed, zero failed, zero
skipped**, across every CI-named file and every touched/new test file.

| File under tests/ | Starting head | Fixed head |
|---|---:|---:|
| unit/test_test_module_names.py | 1 failed | 1 passed |
| fake/test_writable_fanout.py | 23 passed, 5 failed | 28 passed |
| fake/test_policy_sandbox.py | 23 passed, 1 failed | 24 passed |
| fake/test_admission_priority.py | 27 passed, 5 failed | 32 passed |
| unit/test_census_survivors_review.py | 9 passed, 5 failed | 14 passed |
| fake/test_review_r185_adversarial.py | 1 failed | 1 passed |
| unit/test_census_identity_uncertainty.py | 8 failed | 8 passed |
| fake/test_state_fixture_processes.py | 1 passed, 3 failed | 4 passed |
| unit/test_census_recycled_pid.py | 23 passed | 25 passed |
| fake/test_census_recycled_pid_daemon.py (old basename before) | 8 passed | 8 passed |
| fake/test_state_contract.py | 37 passed | 37 passed |
| unit/test_procs.py | 47 passed | 47 passed |

The adopted review regressions each failed at the starting code and passed at
head: absent guardian, zombie guardian, fully reparented survivor after guardian
reuse, clock rollback, the owned survivor's individual kill loop, and the writable
confirm-dead lease fixture. The last failed specifically with “released while PID
43 is still writing”; after the fix it retains quarantine and its leases. These
are the review's four original unit failures, the recycled-guardian unit extension
and its one fake failure, with all nine passing unit controls preserved.

The first full fanout run reached the 1,100-second cap after 20 passing and five
failing call reports. Their order matches the five CI failures. A short selection
then completed with four passes and those five failures, including the remaining
three cases and every DID NOT RAISE assertion. Fixed fanout validation split the
file into 27 cases and its 12-repository case: both passed. Subsequent batches
were short; none of our lock holds exceeded 20 minutes. Lock waits were outside
the holds. The final consistent missing-record identity fixture separately
reproduced all three failures before passing at head.

The first fixed-head selection found four stale test premises: **225 passed,
four failed**. These were resolved against C-5.3/C-5.5, without changing production
behavior or relaxing writer/lease checks:

- The two-ps performance fixture now supplies a complete matching root identity;
  it still requires exactly two ps reads and the same full group/identity result.
- The failed-boot example also requires the new missing-recorded-identity error.
- The vanished-marker fixture now reports live during marker enumeration and
  absent at identity capture. A blank start with a live stat is unknown, as the
  new missing-start regression requires; the old fixture did not model absence.
- The receipt-supplied child case without an owned identity must retain both the
  child and its walk as unverifiable. The recorded case still proves reuse and
  empties. Receipt bounds alone cannot prove death after clock correction.

A fixture edit initially matched a neighboring unit test; it was moved to the
intended performance test, the unrelated change was reverted, and the complete
47-item process file passed. Final reruns of process units and fake census checks
passed **47 and eight** respectively. Syntax compilation and Git diff checks
passed. No implementation changed after the passing property/CI/review checks.

The generator now samples live, absent, zombie and recycled roots and includes
an escaped recorded survivor reparented to init in its own session. Hypothesis
completed **1,400 passing examples**, **200 per property**, **zero failures** and
**103 rejected generation attempts**. Statistics exercised every root-state pair
in the survivor property, including absent/absent and recycled/recycled.

| Property / invariant | Passing examples |
|---|---:|
| Recycled roots and their groups/walks contribute no stranger | 200 |
| Matching roots and legitimate group members remain | 200 |
| Marker discovery is unchanged | 200 |
| Matching identities never shrink a source; the complete recorded walk and identities remain | 200 |
| Signal permission does not grow; recycled incarnation excluded, recorded survivor signalled | 200 |
| Never verified empty with any matching recorded identity alive; excluding a recycled PID preserves other matching live processes | 200 |
| An incomparable survivor identity yields unverifiable, never death or verified empty | 200 |

Unrun: the full repository suite, real-process/live-provider suites, local Python
3.12, live incident replay, and unmerged PR #131 integration. No live incident
process was signalled and no caller workspace or live state was modified.

## Updated PR description for the hub

Prevent recycled guardian and child PIDs from keeping completed attempts
quarantined while retaining genuine escaped writers after their ancestor dies,
becomes a zombie or is recycled. The census walks from all recorded survivors,
keeps uncertain identity checks unverifiable and uses comparable identities
rather than wall-clock receipt bounds to prove reuse. Signal ownership remains
subject to the existing recorded-identity and fresh-read checks. Group exclusion
follows [XNU 12377.1.9, lines 970–972](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/bsd/kern/kern_fork.c#L970-L972).

Repair the shared fake process fixture that bypassed writer refusals and hid
simulated desktop callers, give the fake recycled-PID module a unique basename,
and add the independent review fixtures and departing-root generated cases.
Validation: 229 tests passed; seven Hypothesis properties passed 1,400 examples.
Every CI-named file and all adopted review fixtures passed, with before/after
evidence recorded above. No remote PR edit was attempted.
