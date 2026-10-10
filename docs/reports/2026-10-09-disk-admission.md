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

## Timed floor rulings (stacked on #161)

The native gate now follows the two external ruling files on every enabled
**detached** admission pass, without restarting or reloading policy. Configure
`admission.disk.lower_path` and `raise_path` with absolute paths to
`state/subfleet-disk-hold-lower.json` and `state/subfleet-disk-hold-override.json`.
Both default to null. `min_floor_gb` defaults to **20** (finite nonnegative);
`max_lower_h` defaults to **16** (finite positive). Unknown settings and invalid
values report the exact dotted policy key. No live configuration is changed.

A valid lowering has a future `until`, a floor at least `min_floor_gb`, a ruling
that stringifies and trims to a nonempty value, and an expiry at most
`max_lower_h` after **min(now, file mtime)**. For a writing time at or before now, the cap means an
initially overlong file cannot become valid just because time passed. A future
mtime cannot extend the cap. Its floor replaces the policy floor and its
`release_margin_gb` defaults to zero, with negative values clamped to zero.

A raise without a lowering uses `max(policy floor, raise)` with the policy
margin. When both are live, a raw raise strictly above the lowered floor wins
with the policy margin; at equality or below, the lowering wins. Expired,
refused and unreadable files retain their stable reason codes:
`lower_error`, `lower_expired`, `lower_refused`, `override_error`,
`override_expired`. A missing file is an absent ruling. Each ignored file has
no effect; a valid other file can still supply the floor.

The implementation was compared directly with the read-only external
`bin/subfleet-disk-hold` methods `floor_gb`, `lower_override` and `pass_once`.
The source wins over these differences in the brief:

- Numeric values use `float` coercion, including numeric strings and booleans.
  NaN raises are labelled expired; NaN lowerings are refused. Positive infinity
  is accepted by the agent and safely holds native admission without integer
  overflow. Policy settings themselves remain finite.
- `ruling` need not originally be a string: the agent stringifies a truthy
  value and trims it. Negative release margins clamp to zero.
- When both are live, a raise above a lowered floor can still be below the
  policy floor (policy 60, lower 30, raise 35 gives **35 with policy margin**).
  Without a live lowering, that same raise leaves the floor at 60.
- The agent validates `drop_gb` and `drop_window_min` while parsing a
  lowering. That validation is retained: malformed values reject the whole
  lowering, even though their drop-trigger effect is ignored. The brief's
  instruction that the code wins resolves this difference.
- Naive ISO timestamps use the machine's local timezone, as in the agent.
- JSON is decoded as UTF-8 text before parsing, matching the agent: UTF-16,
  UTF-32 and UTF-8 BOM files are ignored rather than silently accepted.

Deliberate differences required for native admission:

- The reader rejects FIFOs, symlinks, directories, files over **16 KiB**, and
  files changed during the read. It opens nonblocking and no-follow, obtains
  the bytes and mtime from one descriptor, and does no filesystem work under
  the store lock. The agent uses unbounded text reads and a separate stat.
  A failed mtime read is an error here, rather than the agent's fallback to now.
- Non-object JSON and deeply nested malformed JSON are ignored as errors;
  the agent can raise on some non-object values. This satisfies F2.
- Drop values never trigger a native hold; placement reservations already
  pace launches. The numeric validation still matches the agent.

Floor and margin changes re-evaluate the hysteresis latch against the current
measurement and outstanding reservations, including passes with no queued
jobs. A held 34 GB reading therefore opens at a 30 GB floor with zero margin;
at expiry it returns to 40 GB with the policy margin and holds again. With
both paths null the original #161 decision and reservation behavior is
preserved, including when a gate is reconstructed after restart.

Status and disk holds in `why` show the floor, its source (`policy`,
`lowered until <t> by <ruling>`, `raised until <t>`), the ruling's optional
`why` text, and ignored-file reasons. A source transition writes exactly one
`admission.disk_floor` event; the same source on later passes writes none.
The saved source is recovered at daemon startup to avoid duplicate events.
The saved ruling's numbers also drive the first pass's latch comparison, so a
ruling that expired while the daemon was stopped cannot leave an open latch
below the restored policy floor, even with no queued jobs.
Attended passes read neither disk nor ruling files, and probes remain exempt.

### Timed ruling invariants

| Invariant | Test |
| --- | --- |
| **F1 agent parity** | `test_F1_agent_differential` and `test_F1_both_live_rulings_differential`: independent small oracle translated from the three agent methods; generated files, mtimes, clock, policy floor, margin, lower limits and fake disk; 400 examples each, including guaranteed live pairs |
| **F2 invalid files** | `test_F2_invalid_and_unreadable_never_change_policy`: generated bad bytes, bad shapes and read failures; 400 examples; real FIFO/symlink/directory/oversize rejection also runs through a pass |
| **F3 lower limit/lifetime** | `test_F3_lower_bound_and_lifetime`: generated floor, minimum, duration, mtime and clock; 400 examples |
| **F4 pacing I1–I5** | `test_invariants_over_generated_sequences[timed-floor-*]`: independent agent floor substituted into all five original generated sequence invariants, with timed lower/raise actions, expiry, restarts, reservations, ends and disk changes; 120 examples per invariant; original policy-only variants rerun too |
| **F4 I6 off means off** | `test_I6_disabled_matches_absent_over_generated_sequences` rerun; `test_disabled_rule_reads_no_ruling_files` forbids both readers even with configured paths |
| **F5 null paths** | `test_F5_null_paths_match_161_decisions`: independent frozen #161 decision/expiry oracle over generated sequences, floors, margins and reserves; 120 examples, ruling reads forbidden |
| **Expiry/latch regression** | `test_latch_recomputed_at_start_and_expiry_even_without_jobs` and real-daemon `test_timed_floor_expiry_visibility_events_and_no_store_lock` check starts and exact expiry without restart, with source events and visibility |

The independent oracle lives in `tests/disk_floor_model.py`, a plain test
support module with no test decorators or production imports. Both the floor
properties and pacing properties use it. This avoids importing a test module
inside a running Hypothesis example, which would create nested `@given` tests
when pytest and Python load the module under different names.

The F1 oracle retains the agent's drop-field parsing, and generated drop
fields include both valid and malformed values. Explicit tests also check that
malformed fields reject the lowering while valid drop values never trigger a
native hold. The oracle totalizes the agent's non-object
crash into an error so invalid inputs are safe while valid-input rules remain
independent of production code.
