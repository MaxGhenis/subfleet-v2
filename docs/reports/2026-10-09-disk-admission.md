# Disk admission: paced recovery

Max's d636 ruling (2026-09-29) holds new Subfleet job launches below 40 GB
until retention works. The external chief-of-staff `bin/subfleet-disk-hold`
runs every 120 seconds and renews each lane's 20-minute hold. It never
releases those holds explicitly. After the last low reading, all lanes become
available together.

On 2026-10-09 the holds lapsed at **16:33Z**. About **70** waiting jobs were
placed within **30 seconds**: **38 read-only**, **20 writable in new worktrees**,
and **12 writable in place**. Free space fell from **42 to 28 GB in two
minutes** as checkouts, environments and installs started; the holds returned.

## Rule and policy

`admission.disk` defaults to disabled in both code and the shipped policy.
The hub enables it in its live policy. No live policy, daemon, external agent,
or launchd configuration is changed by this work.

| Setting | Default | Validation |
| --- | --- | --- |
| `enabled` | `false` | boolean |
| `floor_gb` | `40` | finite, nonnegative |
| `resume_margin_gb` | `5` | finite, nonnegative |
| `placement_reserve_gb` | `1.5` | finite, positive |
| `reserve_ttl_s` | `600` | finite, positive |
| `path` | `null` | optional nonempty string; omitted/null uses the state root |

Unknown keys are refused with their dotted policy key. GB is decimal, **1e9
bytes**. The injectable disk reader calls `os.statvfs(path)` once per detached
admission pass and uses `f_bavail * f_frsize`, matching the external agent.

Effective free is measured free minus outstanding reservations. A detached
placement requires:

```text
effective_free - placement_reserve_gb >= floor_gb
```

Once a disk refusal occurs, the gate remains latched until effective free
reaches `floor_gb + resume_margin_gb`. The reserve check still applies after
unlatching. A 46 GB reading permits four 1.5 GB placements above a 40 GB floor,
then holds the rest. Another pass cannot spend that same 6 GB again. A later
pass resumes when attempt ends, reservation expiry, or increased free space
leave enough room, subject to the margin.

## Reservations, exemptions and visibility

Every committed detached placement, including retries and read-only jobs,
records its budget and TTL in existing attempt evidence. Reservations begin
at **`reserved_at`**, since checkout and setup can precede provider `started_at`.
The in-memory map is rebuilt from live attempts before each pass and on
startup. Execution end (`finished_at`, including an attempt still finalizing)
or TTL releases the reservation, whichever is first. Recorded amounts and
TTLs survive a policy change; recent attempts from before disk admission use
the configured defaults. Status reconciles ends and expiry without rereading
the disk or changing the admission worker's map.

Hysteresis transitions are recorded as `admission.disk_latch` events, so a
restart also preserves a low-space refusal. This adds no schema or separate
reservation table.

Attended conversation turns are exempt: their separate admission pass neither
reads nor reserves disk. Probes remain exempt and existing timer probes can
keep refreshing capacity. C-6.16 priority callers are subject to the same disk
rule as all other detached work.

The gate follows C-6.13's machine guard: it records a `disk` hold at the door,
before workspace preparation, without leasing a lane, adding a FIFO waiter,
or creating a capacity backoff. Approval, uncertainty and a workspace retry
whose clock is still running retain their own reasons. Disk holds are checked
again on every detached pass. `why` reports free, reserved, floor, margin and
placement budget; status has one disk line showing free, reserved, floor and
holding/open/disabled. An unreadable enabled path holds detached placements
and shows the reading error. Disabled/absent policy does not read disk or
record reservations.

The external agent **can be retired once this is live**. Retiring it is the
**Disk manager's step, not this PR's**. Retention remains necessary: these are
provisional launch budgets, not filesystem quotas or a guarantee about disk
consumption by running jobs, attended sessions or unrelated processes.

Clarifications beyond the brief: budgets start at reservation rather than
provider start, to cover setup; unreadable measurements fail closed; latch
transitions are durable to preserve hysteresis across restart. The waiting
reason uses the machine guard's existing in-memory hold record rather than
adding a new persisted job state or backoff.

## Invariants and tests

The floor and pacing statements use the pass's one measured-free snapshot.
A later external decrease can put existing work below the floor; the next
pass refuses more detached placements. Generated sequences include free-space
readings, submissions across classes, attempt ends, clock steps and restarts,
with a fake disk and an independent GB/expiry oracle.

| Invariant | Property/test |
| --- | --- |
| **I1 floor:** no detached placement leaves measured free minus outstanding reservations below the floor | `test_invariants_over_generated_sequences[I1-floor]` checks after each placement |
| **I2 progress:** effective free at least floor + margin + reserve and a placeable detached waiter imply at least one placement that pass | `[I2-progress]`, including latched recovery |
| **I3 pacing:** placements per pass are at most `max(0, floor((effective_free - floor) / reserve))` | `[I3-pacing]`, with attempts ending and budgets expiring |
| **I4 attended exempt:** attended turns are never refused for disk | `[I4-attended]`; real turn pass also checks zero disk reads and reservations |
| **I5 reservations:** nonnegative; released at attempt end or TTL; identical after restart | `[I5-reservations]`; real restart, finalization, TTL boundary and retry cases |
| **I6 off means off:** disabled and absent disk policy give existing admission decisions | `test_I6_disabled_matches_absent_over_generated_sequences` compares the production scheduler pass model for the same generated scenario; real daemon tests cover both forms and forbid disk reads |

I3 uses zero placements when effective free is below the floor. The brief's
literal formula has a negative upper bound there, which no nonnegative
placement count can satisfy; clamping it to zero states the intended rule.

The fake-daemon stampede regression submits 70 detached jobs, holds at 39 GB,
then places exactly four at 46 GB. Repeated passes place zero while budgets
remain; one end alone cannot bypass hysteresis; ending the four permits four
more. The 599-second pass still waits; at 600 seconds another four are placed.
Other daemon cases verify priority callers, before-workspace gating, `why`,
both status renderers, the custom path, read errors and retry reservations.

## Mutation checks

| Mutation | Failing test | Observed result |
| --- | --- | --- |
| Drop reservation subtraction | `test_reservation_subtraction_limits_a_burst` | Caught: 1 failed |
| Drop hysteresis | `test_hysteresis_refuses_between_floor_and_resume` | Caught: 1 failed |
| Exempt priority callers | `test_exemptions_at_zero_free[priority-True]` | Caught: 1 failed |
| Hold attended turns | `test_exemptions_at_zero_free[attended-False]` | Caught: 1 failed |

## Changed files

| File:line | Change |
| --- | --- |
| `subfleet/policy.py:206`, `:406`; `subfleet/default_policy.json:174` | Disabled defaults, nested settings and exact-key loader validation |
| `subfleet/disk.py:22`, `:32`, `:48`, `:85`, `:110` | Available-byte reader, reservation reconstruction, one-read pass and hysteresis rule |
| `subfleet/daemon.py:662`, `:2869`, `:2881`, `:4571`, `:4746`, `:5243`, `:5305` | Startup recovery, status reconciliation, before-workspace guard and durable/in-memory placement budgets |
| `subfleet/render.py:209`, `:239`; `subfleet/cli.py:413` | Disk wait explanation and shared single-line status formatting |
| `tests/unit/test_disk_admission.py:35`, `:164` | I1–I6 sequence properties and deterministic mutation witnesses |
| `tests/unit/test_policy_disk.py:10`; `tests/unit/test_policy.py:442` | Policy validation and updated nested-default contract |
| `tests/fake/test_admission_disk.py:50`, `:58` | Fake clock and real-daemon regression, exemption, recovery and visibility checks |
| `docs/acceptance-contract.md:213`, `:219` | Disk wait reason in C-6.11 and new C-6.17 |

## Verification and handoff

Standard Python **3.13.15**, pytest **9.1.1**, Hypothesis **6.168.1**. The six
sequence properties are each configured for 120 examples of up to 65 events.
There are **48 new test cases** (13 disk unit/property, 26 policy, 9 daemon).

| Run | Result |
| --- | --- |
| All `tests/unit/test_scheduler*.py`, `tests/unit/test_policy*.py`, `tests/fake/test_admission_*.py`, plus `tests/unit/test_disk_admission.py` | **626 passed, 1 failed**, 420.89 s; the sole failure was restart hysteresis |
| All nine `tests/fake/test_admission_disk.py` cases after fixing latch persistence | **9 passed**, 8.39 s |
| Four actual source mutations, one pytest process each | **4/4 caught**, each with exactly one expected failure; source restored after each |
| Static compilation and `git diff --check` | Passed |

All **627 selected cases** have passing coverage across the broad run and the
focused follow-up; the broad run was not repeated after the narrowly scoped
latch writer fix. `Store.transaction` suppresses audits for transactions that
change no rows, which the real restart test caught. The fix explicitly inserts
the latch record with a distinct audit kind, avoiding both an empty transaction
and an audit that would shadow the saved boolean. The follow-up rechecks the
stampede, restart, retry, exemption, status, path-error and disabled cases.

Every pytest process ran serially under
`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`; the mutation
runner and its sequential regression child shared one held lock. No `-n` was
used. TMPDIR was a fresh `disk-admission.*` directory under
`getconf DARWIN_USER_TEMP_DIR`, removed after verification. An earlier
free-threaded Python run was interrupted and is not counted as a successful
verification run.

Shared Git metadata rejected writes, so commits are on `feat/disk-admission`
in `.git-local`. The implementation commit is recorded below; the bundle also
includes this completed report. The standalone bundle's prerequisite is
**`59f684ffd8c673c994afebfdbfa435d739b56261`**. Its advertised head is named in the
packaging record and final handoff. Every commit ends with the requested
Claude Opus 5.5 co-author trailer. No logs or JSON run evidence are committed.

Implementation head: `2ddf1ebb392c224ff448fbda43fce348b4bb0813`.
