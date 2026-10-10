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
The saved ruling's numbers and source also drive the first pass's latch comparison, so a
ruling that expired while the daemon was stopped cannot leave an open latch
below the restored policy floor, even with no queued jobs.
Source transitions also re-evaluate the latch, covering a file edited with the
same expiry and ruling before an offline expiry: its last source event can
contain older numbers, since number-only edits do not emit source events.
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

### Timed ruling changes and validation

These results apply to the timed-ruling extension; the earlier report records
the original #161 implementation. The tested implementation head is
`206a7450f7d88efe160bdfdeb3a1e3378ae86039`, stacked on
`e884a773ac933c87ca7caf2a67f4e853831b9a83`. The final delivery adds this
validation report in a separate commit on `feat/disk-admission-overrides` in
the workspace's `.git-local`; shared Git metadata is outside the writable
workspace. The final response names that delivery head.

| File:line | Change |
| --- | --- |
| `subfleet/policy.py:206`, `:419`, `:429` | Four defaults, finite range checks, optional absolute ruling paths, exact-key errors |
| `subfleet/default_policy.json:181` | Null paths, 20 GB minimum and 16-hour lowering cap |
| `subfleet/disk.py:28`, `:56`, `:190` | Bounded descriptor reader, agent-equivalent floor selection, per-pass refresh and latch evaluation |
| `subfleet/daemon.py:686`, `:2922`, `:2981`, `:4608` | Restore source and numbers, transition-only floor events, `why` visibility, detached-pass integration outside the store lock |
| `subfleet/render.py:239`, `:249`, `:389` | Source, optional explanation and ignored reasons in status and disk holds |
| `tests/disk_floor_model.py:40` | Independent agent oracle and fake files/clock helpers |
| `tests/unit/test_disk_floor.py:36`, `:53`, `:71`, `:85`, `:207` | F1, live-pair F1, F2, F3 and frozen-#161 F5 properties; unsafe readers and mutation witnesses |
| `tests/unit/test_disk_admission.py:33`, `:176` | Original and timed-floor I1–I5 sequence variants and I6 disabled comparison |
| `tests/unit/test_policy_disk.py:12`, `:36` | New defaults and path/range loader validation |
| `tests/fake/test_admission_disk.py:229`, `:275`, `:309` | Expiry, lock assertion, `why`/status, events, precedence and restart cases, including edits with unchanged source |
| `docs/acceptance-contract.md:219` | Extended C-6.17 |
| `docs/reports/2026-10-09-disk-admission.md:177`, `:255` | Built semantics, agent differences and invariant mapping |

All four mutations were applied to the actual production source, one at a
time, and restored after each run. Controls passed before and after them.

| Mutation | Witness | Observed result |
| --- | --- | --- |
| Drop the ruling requirement | `test_mutation_floor_witnesses[ruling]` | **Caught**, 1 expected failure |
| Drop the mtime cap, using now alone | `test_mutation_floor_witnesses[mtime]` | **Caught**, 1 expected failure |
| Allow a lone raise to lower the policy floor | `test_mutation_floor_witnesses[raise]` | **Caught**, 1 expected failure |
| Omit latch evaluation on changed floor/source | `test_latch_recomputed_at_start_and_expiry_even_without_jobs` | **Caught**, 1 expected failure |

| Run | Result |
| --- | --- |
| Final feature suite: `test_disk_floor.py`, `test_disk_admission.py`, `test_policy_disk.py`, fake `test_admission_disk.py` | **102 passed** (26 + 21 + 40 + 15), 10.74 s |
| Final mutation controls before / after source restoration | **4 passed / 4 passed** |
| Final actual mutations | **4/4 caught**, one expected failure each |
| Full targeted run including `tests/unit/test_policy.py` and all four requested gate/legacy files | **366 passed, 17 failed, 1 skipped**, 1,644.14 s; ten test-helper wiring failures subsequently fixed and all feature cases rerun successfully |
| First corrected feature suite plus fixture-child retry | **100 passed, 1 failed**, 172.28 s; final restart cases subsequently expanded from two to four |
| Python syntax, default-policy JSON and `git diff --check` | Passed |

The full targeted run included `tests/unit/test_gate_admission.py`,
`tests/unit/test_gate_service.py`, `tests/unit/test_legacy_hold.py`, and
`tests/fake/test_gate_end_to_end.py`, including their Daemon-without-`__init__`
paths. Seven unrelated regression cases remain unresolved in this environment:

- Six legacy tests fail before their assertions because the sandbox refuses
  `/bin/ps` with `PermissionError: Operation not permitted`: the two
  `test_a_live_claude_process_outside_subfleet_is_a_dispatch_wait` variants
  and four
  `test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait`
  variants.
- `test_gate_fixture_child_publishes_only_its_explicit_synthetic_attestation`
  hits its unchanged three-second child-process timeout, both in the full run
  and on retry with inherited `PYTHONPATH` cleared. The fixture reaches and
  completes daemon admission before timing out in the external fixture child.
  This is not a passing regression result.

The initial ten feature failures came from importing a decorated test module
inside a Hypothesis example. Moving the oracle into the plain support module
fixed that harness error without suppressing Hypothesis's nested-given check.
All final F1–F5 and I1–I6 cases pass.

Every pytest process ran serially under
`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`. Each run used
a task-specific `TMPDIR` under `$(getconf DARWIN_USER_TEMP_DIR)` and removed
it afterward. Final runs used Python 3.13.15, pytest 9.1.1 and Hypothesis
6.168.5, with inherited `PYTHONPATH` cleared. No live policy, external agent
or caller checkout was modified. No logs, JSON evidence or bundles were
committed; no agents were delegated and nothing was pushed.

## Review fix round 1: #164 (2026-10-10)

Base: `76eac97ed869d8ca32521cc1f27eb936637420c5`. Tested implementation:
`520f99cc808291993140db13d0b98f4df0bb1cf2`, on `feat/disk-admission-overrides`
in this workspace's `.git-local`. Shared Git metadata is outside the writable
workspace. The final response names the delivery head containing this report.

| File:line | Fix |
| --- | --- |
| `subfleet/disk.py:122`, `:269`; `subfleet/daemon.py:2978`; `subfleet/render.py:255` | Report non-finite values as strings, retain original floor/margin text, and replace lone surrogates recursively, including metadata keys. Snapshots, holds, notices and floor audit events encode as strict UTF-8 JSON. Admission retains the original numbers and strings. |
| `subfleet/daemon.py:723`; `subfleet/disk.py:239` | Recheck the recovered latch against the first pass's current floor, margin, measurement and reservations, even with no candidates. Stop recovering numbers from transition-only source events. Later unchanged-policy passes retain existing behavior. |
| `subfleet/policy.py:430` | Reject oversized integer GB settings before float conversion can overflow, naming each of the four GB keys. |

The three review tests were ported and run before production edits:

| Ported test | At the base | After fixes |
| --- | --- | --- |
| `tests/fake/test_admission_disk.py:353` — `test_review_live_ruling_status_is_valid_wire_json` | 6 failed | 6 passed; also checks why, floor events and notice signatures |
| `tests/fake/test_admission_disk.py:414` — `test_review_restart_after_same_source_offline_edit` | 1 failed | 1 passed |
| `tests/unit/test_policy_disk.py:83` — `test_review_large_size_rejected_with_key` | 4 failed, 8 passed | 12 passed |

The base run had **11 failed, 8 passed**. All **19** ported cases pass after
the fixes. The 26 review fidelity cases were also ported to
`tests/unit/test_disk_floor.py:314`; all admission numbers and hold decisions
match their original expectations. Generated agent parity and admission
invariant checks pass. Additional coverage checks nested metadata, both ruling
paths, raw numeric preservation, recovery thresholds and safe source events
after restart.

The real app's `DaemonClient.decodeResponse<JSONValue>` and menu `Snapshot`
decoder both accept reported evidence in **6 Swift cases**, using
`tests/frontend/DiskRulingProbe.swift:7` and
`tests/frontend/test_disk_ruling.py:33`. No app source changes were needed.

| Verification | Result |
| --- | --- |
| Disk policy suite | 57 passed |
| Focused JSON evidence, agent differential and Swift checks | 18 passed |
| Required files plus Swift ruling tests | **275 passed, 6 failed, 1 skipped**, 282 total, 25.85 s |
| Python syntax and `git diff --check` | Passed |

The complete run selected `tests/unit/test_disk_*.py`,
`tests/unit/test_policy_disk.py`, `tests/fake/test_admission_disk.py`,
`tests/unit/test_gate_admission.py`, `tests/unit/test_gate_service.py`,
`tests/unit/test_legacy_hold.py`, `tests/fake/test_gate_end_to_end.py`, and
`tests/frontend/test_disk_ruling.py`.

The six failures occur in legacy fixture setup because the sandbox denies
`/bin/ps`; these need host execution, rather than a passing claim:

- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_outside_subfleet_is_a_dispatch_wait[False]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_outside_subfleet_is_a_dispatch_wait[True]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait[same-False]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait[same-True]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait[upper-stored-False]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait[lower-stored-False]`

`tests/fake/test_gate_end_to_end.py::test_plan_peer_process_finalizes_through_real_daemon`
was skipped because permitted sysctl/ps inspection is needed for macOS boot
identity. The remaining gate cases pass, including the previously reported
fixture-child timeout case.

Python **3.14.7**, pytest **9.1.1**, Hypothesis **6.168.5**. Every pytest process
ran serially under `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`.
TMPDIR and caches were under a task directory within
`$(getconf DARWIN_USER_TEMP_DIR)`, removed after verification. No caller
checkout or host policy/state was modified, no sub-agents were used, and
nothing was pushed. Only source, tests and this report were committed.

Fix commits: `d29e57869` (policy), `5124e5234` (reported evidence),
`520f99cc8` (restart recovery). Each ends with the requested Claude Opus 5.5
co-author trailer.
