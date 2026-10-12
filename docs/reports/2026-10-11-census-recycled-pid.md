# Containment census and recycled PIDs — 2026-10-11

Base: `4c6a8a9c989e`. Branch: `fix/census-recycled-pid` in `.git-local`
(shared Git metadata is read-only). No history rewrite, remote Git operation,
or live-state change. Implementation and a draft report were committed before
long test waits; each coherent step has its own commit.

The census counted whichever process currently occupied the guardian or child
PID, even after the attempt's recorded start identity had died. A reused leader
PID also supplied an unrelated group. The measured incidents were qyp16-r1c
(child 4203), pb-kit-u4-docs (leader 243), and pe-uk-2192-r2/a2 (child 16379).
Successful work could become `lost`, rc 125, with indefinitely held leases.
Signalling already checked identity separately and retains those checks.

Apple's [XNU 12377.1.9 `forkproc`, lines 895–949](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/bsd/kern/kern_fork.c#L895-L949)
allocates PIDs under `proc_list_lock` and rejects a candidate present in the
process hash, process-group hash, or session hash. A different process at the
recorded leader PID therefore proves that the original group no longer exists.
An absent leader does not establish group death: surviving members remain.
The same citation appears in the code comment and C-5.5.

## Design and changes

- `subfleet/procs.py:324,389`: census evidence includes `recycled`; recorded
  identities filter roots and the group leader in the fresh process snapshot.
  Different start times or freshly confirmed different boot UUIDs establish
  death. Each root/leader is compared once per snapshot. Empty groups and zombie
  roots need no boot read. Absent leaders preserve surviving groups.
- `subfleet/daemon.py:6450`: `_contain` supplies guardian identity from the row
  or start receipt, child identity from `owned_identities`, and receipt bounds.
  The exit receipt supplies the child PID before the row is refreshed and its
  actual `finished_at`, rather than a later quarantine timestamp. Proven reuse
  persists in attempt evidence and an event, including verified-empty success,
  so `runs show --json` explains it. Existing evidence is preserved.
- `subfleet/daemon.py:4137`: probe censuses delegate to `_contain`. Finalization,
  kill, start grace, quarantine and `--confirm-dead` already use that path.
  Production edits stay within these two files for the PR #131 interaction.
- `docs/acceptance-contract.md:9,205`: dated incident entry and C-5.5 specify
  identity filtering, bounds, unknown handling, kernel rule and durable evidence.
  C-5.3 already specifies reused starts as death and is unchanged.
- `tests/unit/test_census_recycled_pid.py:58`: five generated properties and
  conservative examples cover identity, bounds, legacy timestamps, failed reads,
  fresh UUID confirmation, absent leaders and zombie-only empty groups.
- `tests/fake/test_census_recycled_pid.py:51`: three incident regressions,
  rc-0 success/export, verified-empty quarantine release with Git salvage,
  probe delegation and receipt-supplied child identities.
- `tests/fake/test_state_contract.py:40`: state-only fixture now stubs the shared
  process table, liveness and group-member inspection. Its old `same_process`
  stub did not cover the C-5.12 running-inspection path.

Unknown legacy boot matches or failed reads needed to decide identity retain
containment and make it unverifiable. A different start alone proves reuse even
when the optional boot read for evidence fails. Without a complete recorded
identity, readable birth times before attempt start or after the exit receipt
exclude a root. The lower bound
is rounded down to whole seconds to match `ps lstart` precision; missing or
unparseable bounds retain it. Matching recorded identities override bounds.
Marker enumeration is unchanged. Evidence records only identity/bounds and a
fixed reason, never commands or environments. The kill ownership and signal
checks are unchanged.

## Regressions: before and after

The before run used a frozen copy of the unfixed source inside this workspace,
checked to contain neither new identity argument. Existing fake process-table
fixtures supply all incident OS reads. All four tests failed at behavior
assertions: **4 failed in 41.19 s**.

| Regression | Before | After |
|---|---|---|
| qyp16-r1c child 4203 | Walk `{4203, 55000}` | Verified empty; recorded/current starts retained |
| pb-kit-u4-docs leader 243 | Group and walk `{243, 55000}` | Both empty; reuse explains exclusion |
| pe-uk-2192-r2/a2 child 16379 | Walk `{16379, 55000}` | Verified empty; recorded/current starts retained |
| Exit rc 0, then recycled child | `lost`, rc 125 | `succeeded`, rc 0; acceptance, export and lease release |

PID 55000 is a synthetic child of the stranger. The incident PID/start pairs and
qyp16 exit time are measured values from the task; guardian PIDs 4000/16000 and
the other two exit bounds are synthetic fixture data. The salvage regression
recreates qyp16's quarantine in a temporary writable Git repository, verifies
empty without override, releases leases and preserves the deliverable in a
salvage ref. It does not modify the live qyp16 job.

## Invariants and validation

The final run completed **1,000 valid Hypothesis cases**, **200 per property**,
with **0 failures** and 104 rejected generation attempts (not failing cases).
Property (b) also varies whether the other root is recycled, preserving a
matching child even under a recycled guardian. The earlier property run passed
1,000 valid cases too, with 108 rejected generation attempts.

| Invariant | Valid passing cases |
|---|---:|
| (a) Recycled roots/groups cannot contribute the stranger or its walk | 200 |
| (b) Matching roots remain; a matching leader retains live group members | 200 |
| (c) Marker source is unaffected | 200 |
| (d) All matching identities preserve all source sets and captured identities | 200 |
| (e) Kill ownership retains the old identity and never adds/signals the stranger | 200 |

All pytest runs used one process at a time, the prescribed `lockf` lock and an
isolated Darwin TMPDIR removed on exit. Own lock holds stayed below 20 minutes.
Dependencies were installed from the unchanged lockfile.

- First fixed-code run: **118 passed, 1 failed in 83.95 s** across new unit/fake
  regressions, existing process units and daemon-settle units. The failure was
  the new writable salvage fixture lacking measured lane capacity; it failed
  before containment. Added the existing workspace fixture's measured reading.
- Related fake-daemon run: **106 passed, 1 failed in 802.14 s** across new fake
  regressions, state contract, notice headers, probe recovery, probe cancellation
  and daemon launch. Salvage and all incident checks passed. The old missing-exit
  state test exposed incomplete process stubs; corrected the fixture as above.
- Final validation at `4085e06dcd77b365994243736de82cfb500de6cb`:
  **115 passed, 0 failed in 455.72 s (7 min 35 s)**. Files:
  `tests/unit/test_census_recycled_pid.py`,
  `tests/fake/test_census_recycled_pid.py`, `tests/unit/test_procs.py`,
  `tests/fake/test_state_contract.py`, with `--hypothesis-show-statistics`.
  Both previously failing fixtures passed, as did all three incident cases,
  rc-0 success/export, verified-empty salvage and the strengthened properties.

Across the fixed-code runs, **220 distinct selected pytest items passed**,
including **31 new items** (23 unit, 8 fake). The final runner waited about two
hours for the shared lock; its actual test duration remained below 20 minutes.
The runner exited 0 and its TMPDIR cleanup completed. The frozen before copy
and local Hypothesis artifacts were removed; no background runner remains.

Unrun: the full repository suite, real-process/live-provider suites, a live hub
replay, and integration with unmerged PR #131's timed quarantine rechecks.
Incident reproduction used only temporary stores and fake process tables; no
incident process was signalled. No logs or JSON evidence are committed.

## Fix round 1 — work in progress

Starting head: `cb23ab373f1da830f276661f1390b11beb3ddfa1`; current integration
base: `be7afd599`. Read the original brief, CI log and all review-r1 findings.
Adopted the independent unit and writable-lease fixtures unchanged, naming the
unit module `test_census_survivors_review.py` to preserve unique module names.
CI reproduction and individual review-fixture runs are queued, one file per
lock hold with an 1,100-second pytest cap. This draft will be replaced with
measured before/after counts and the final identity rules.
