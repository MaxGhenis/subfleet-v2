# Containment and reused process identities (2026-09-28)

## The defect

`procs.containment()` seeded its descendant walk with the recorded guardian
and child pids and kept every root the snapshot showed live, without
comparing the process at that pid with the one recorded at launch. When
macOS gave a recorded pid to an unrelated process, the census counted that
process as a surviving writer, and `_finalize` quarantined the attempt after
the exit-settle window ("writers remain after exit receipt"). An uncancelled
job then ended `lost` (rc 125) with no salvage.

Evidence from the live store (read-only, `mode=ro`), each quarantine's
`quarantine_reason`:

| Attempt | Recorded pid (role) | Quarantined on | Job |
| --- | --- | --- | --- |
| `20260927-151250-r218-conv-opus/a2` | child 80817, started Sun Sep 27 23:26:33 | pid 80817 started Mon Sep 28 22:11:29, pgid/ppid 76443 | cancelled, rc 130 (a cancel was pending) |
| `20260925-074431-inv-eggnest/a1` | child 45548, started 12:08:04 | pid 45548 started 12:35:39, its own group, ppid 1 | lost, rc 125 |
| `20260925-110251-review-chronicle-institute-4/a1` | guardian 28242, started 15:09:11 | pid 28242 started 15:15:02, leading its own group 28242, ppid 1 | lost, rc 125 |

In all three, the recorded identity was in the attempt's own
`evidence_json.owned_identities` (the guardian's also in the attempt row).
Across the store, 5 of 1,556 attempts with a `child_pid` have no recorded
identity for it (children that lived less than one inspection interval).

## The rule as implemented

Identity decides only what a recorded pid would otherwise decide on its own
number (`subfleet/procs.py`, `containment`; contract C-5.3, C-5.5):

- **Roots.** The guardian and the child seed the parent walk only when the
  snapshot's row at their pid, a zombie's included, holds the recorded
  identity: the same `lstart` and the same boot. A different start alone
  proves another process with no boot read. A start that matches on a boot
  that differs is confirmed by a fresh boot read first (C-5.12, as
  `liveness` does). A root another process holds is reported in
  `reused_pids` and seeds nothing. A root in the snapshot with no complete
  recorded identity, or with a comparison that cannot be made, makes the
  census unverifiable.
- **Descendants.** From verified roots the walk follows the kernel's parent
  links through any number of generations, with no identity filter: a live
  child of a verified process is the attempt's, even at a pid an earlier
  process of the attempt once held.
- **Group.** With members present, the recorded group is the attempt's when
  the leader pid (the recorded `pgid`, the guardian) holds the recorded
  leader, live or a zombie. Another process at the leader pid proves that the
  old group emptied and a new one took the id: no member is attributed by the
  group, and the leader pid is reported in `reused_pids`. With the leader pid
  free, the members count if the recorded leader's boot is this boot (POSIX
  keeps a group's id while it has members), and do not count on another boot.
  A leaderless group with members and no recorded leader identity is
  unverifiable.
- **Markers.** A process whose environment carries both `SUBFLEET_ATTEMPT`
  and `SUBFLEET_ROOT` is always counted, whatever its identity (unchanged).
- **Inspection.** A failed snapshot, marker read or boot read, an unknown
  legacy boot comparison, and a snapshot row with no start all make the
  census unverifiable (fail closed). Pid lists, identities, shapes and
  `reused_pids` serialize sorted, and errors keep their first-seen order,
  so a census is byte-identical for the same inputs in any order.

The daemon passes the recorded identities to every census
(`Daemon._containment_identities`: the probe record's or the attempt's
`owned_identities`, plus the guardian's launch identity from the row). The
two call sites are `_probe_census` (probe containment) and `_contain`, which
serves start grace, the dead-guardian path, the kill protocol, `_finalize`
and `_resolve_quarantine`.

### Why the recorded identity does not filter descendants and members

The brief's I1 says a process whose identity differs from the one recorded
for its pid is never counted unless it is marked. Its I3 says every live
descendant of a verified root is counted. Taken literally, they conflict
when an attempt's own later process lands on a pid the attempt recorded
earlier: `owned_identities` records every observed group member, and pids
wrap under heavy churn. The salvaged WIP chose the literal I1: it excluded
such pids from the walk and the group, and pruned their subtrees. That could
prove "empty" while a genuine writer still ran, which is a false release.
A parent link from a verified process cannot reach an unrelated process
(orphans are reparented to launchd, never to a later holder of the parent's
pid), and only processes in the guardian's session can join its group. So
this change reads I1 as *never counted by its pid number*: the rule decides
roots and the leader, and I3 governs everything reached from them. That
precedence is intended and tested
(`test_i3_recycled_pid_inside_a_verified_chain_still_counts`).

### Limits (both err toward holding on)

- **A leaderless group whose id was taken again.** If the attempt's group
  emptied, and a later holder of the pid made a group of it and exited while
  members lived (a double-forking daemon does exactly this), the snapshot
  cannot tell those members from survivors of the attempt's own group. They
  count, and C-5.9 quarantines. Pinned by
  `test_absent_leader_group_is_attributed_even_if_its_id_was_taken_again`.
  Narrowing it would need evidence the snapshot lacks: a recorded session,
  or the daemon keeping its guardian unreaped until the final census so a
  zombie holds the pid.
- **An unrecorded child pid, since reused.** A child that lived less than
  one inspection interval has no recorded identity (5 of 1,556), so a later
  process at its pid makes the census unverifiable rather than empty. This is
  the base behaviour's outcome, and it needs pid reuse before the census.
  `exit.json` names the child only after `child.wait()` reaped it, except
  when the relay failed after spawn (the receipt then carries `spawn_error`).
  A follow-up could have the guardian record the child's `lstart` at spawn.
- Live v1 imports (`importer.py`) record a `child_pid` with no identity and
  fail closed the same way.

## Invariants and the tests that execute them

| Invariant | Tests |
| --- | --- |
| I1: a process whose identity differs from the one recorded for its pid is never counted by that pid (not a root, does not validate the group) unless marked. | `test_i1_r218_a2_reused_child_in_foreign_group_is_exited`, `test_i1_confirmed_historical_incidents_reused_root_is_exited` (eggnest, chronicle), `test_i1_reused_guardian_cannot_attribute_its_forked_children`, `test_i1_reused_group_leader_rejects_entire_new_group`, `test_i1_group_from_another_boot_is_not_owned`, `test_i1_mixed_genuine_and_reused_pids_keeps_only_genuine_writers`, `test_i1_reused_child_that_joined_a_verified_tree_counts_through_the_tree`; Hypothesis: `test_i1_generated_reused_pids_alone_are_verified_empty` (250 reused-only tables) and the general property below. |
| I2: a live process with both markers is always counted. | `test_i2_attempt_markers_override_reused_identity_and_group`, `test_i2_i4_incomplete_snapshot_identity_keeps_writer_without_fresh_read`; both properties. |
| I3: every live descendant of a verified root is counted, through several generations. | `test_i3_verified_root_counts_generations_in_escaped_groups`, `test_i3_verified_zombie_root_still_attributes_its_live_descendants`, `test_i3_recycled_pid_inside_a_verified_chain_still_counts`; property oracle (each pid's own ancestry path, independent of the traversal). |
| I4: every inspection failure leaves the census unverified. | `test_i4_failed_inspection_never_proves_release` (snapshot, markers, boot), `test_i4_missing_live_root_or_group_leader_identity_prevents_release`, `test_legacy_boot_identity_matches_or_remains_unverified`, `test_i4_uuid_record_against_legacy_boot_fallback_cannot_prove_exit`, `test_i4_snapshot_without_root_or_leader_start_cannot_prove_exit`, `test_i4_late_marked_process_with_failed_single_pid_inspection_prevents_release`, `test_c5_12_only_a_fresh_boot_read_calls_a_start_matching_root_another_boot`; Hypothesis `test_i4_i5_generated_inspection_failures_stay_unverified_and_deterministic` (100 examples). |
| I5: deterministic for a given snapshot. | Both general properties reverse the table, marker and recorded-map order and compare the serialized evidence byte for byte. |
| I6: an attempt whose only live pids are reused finalizes normally through `_finalize`. | `tests/unit/test_daemon_pid_reuse.py::test_i6_only_reused_pids_finalize_normally_and_reach_salvage` (r218 child in a foreign group; reused guardian with a forked child), with the settle window already expired: `succeeded`, rc 0, salvage reached, no quarantine. Also probe containment and quarantine resolution with recorded identities. |

The general property (`test_i1_i2_i3_i5_generated_census_obeys_identity_ancestry_and_markers`,
250 examples) also checks that recorded identities for pids that are
neither root nor leader decide nothing: the census is identical when they
are dropped.

Every rule is pinned by a test. Each of these mutants turned the identity
tests red: roots seeded without the identity check; the group always owned;
the WIP's identity filter on the walk; no fresh boot read; re-reading a row
with no start; the daemon passing no recorded identities (I6 fails).
The four daemon-level tests in `test_daemon_pid_reuse.py` all fail when run
against a clean c79aed84.

## Signalling

Unchanged, and it cannot reach a reused pid. `signal_group` returns without
signalling unless `same_process(pgid, boot_id, proc_start)` holds for the
recorded leader (a fresh `ps` start read and a boot match, with a fresh boot
read before a mismatch counts) and `os.getpgid(pgid) == pgid`. Only then does it
call `killpg`. `signal_process` requires `same_process` on the recorded
identity before `kill`. The kill protocol adds census identities to its
signal targets only for group members while the recorded leader is live and
verified, which by the group rule are the attempt's own members. The r218,
eggnest and chronicle attempts were quarantined in `_finalize`, which sends
no signal.

## Why r218 a2 sat in `finalizing` for 20 hours

Store events (read-only): `attempt.finalizing` at 2026-09-28T01:42:09Z,
`job.cancel_requested` at 14:56:37Z, `attempt.quarantined` at 22:11:51Z.
`daemon.log` has 49 lines for the attempt. All are `worker
20260927-151250-r218-conv-opus/a2 failed: SalvageError (n in a row, next try
in … s)`, climbing to 512 in a row at the 60 s ceiling. The count reset
several times; it is held in memory and clears on any pass that returns
normally and on a daemon restart.

- `_process_attempt` sends a `finalizing` attempt to `_finalize` before it
  looks at a cancel request. `_finalize` runs `_salvage` before it reads
  cancellation, and a `SalvageError` propagates. The worker pool retries it
  with capped backoff (C-5.10) forever, and logs only the exception type, so
  the git failure itself is not recoverable from the permitted evidence. The
  baseline commit and tree exist, and no salvage ref was written.
- The false quarantine is what ended the loop. With this fix, the same stall
  would have no end.
- The cause is outside containment and the fix is not small: finalization
  needs a bound on salvage retries and a terminal state that keeps the
  workspace. The unmerged branch `fix/salvage-unindexable` (c1f95838, with
  follow-ups on `fix/salvage-unindexable-r2`) does exactly this: an
  unindexable path is skipped, transient failures are retried three times,
  any other failure is recorded and the attempt ends. It describes the same
  symptom for other r218 attempts on 2026-09-27. No pull request for it
  exists.

## Validation

- **Focused tests.** `test_containment_identity.py` (44),
  `test_daemon_pid_reuse.py` (4), `test_procs.py`, `test_daemon_settle.py`,
  `tests/process/test_guardian_process.py` and
  `tests/fake/test_daemon_contract.py` all pass on the final tree.
- **Full suite.** Run on the final tree (CPython 3.14.7 free-threaded,
  `SUBFLEET_LIVE=0`, a private `SUBFLEET_HOME`, basetemp under `$TMPDIR`)
  in capped foreground batches with JUnit accounting of every collected node
  id. Of 7,077 collected: **7,054 passed, 6 skipped, 16 failed, 1 error**.
  The machine's load average was 15 to 150 throughout.
  - **9 fail in this environment by design, identically on c79aed84.** Seven
    `tests/e2e/test_conversations.py` tests and
    `tests/frontend/test_core_live.py::test_the_app_core_drives_a_development_daemon`
    fail with `person-only: … the caller carries Subfleet's attempt markers`.
    The peer check (`conversations/peers.py`, `judge`) refuses a caller
    whose ancestry runs under a Subfleet guardian, and this validation ran
    inside a Subfleet attempt. A clean c79aed84 clone fails the same 8 with
    identical normalized messages. The one error
    (`test_a_claude_conversation_hands_off_to_codex_with_its_pending_messages`,
    a 3 s daemon-start timeout) passed on rerun on both trees.
  - **8 are load-sensitive timing tests that fail intermittently on both
    trees.** None calls `containment()`. In the reruns (9 on the fix, 7 on
    c79aed84, concurrent):

    | Test | Fix | c79aed84 |
    | --- | --- | --- |
    | `test_daemon_stacks_prints_every_thread_of_the_live_daemon` | 1/9 | 1/7 |
    | `test_c6_8_git_past_its_cap_requeues_and_the_next_pass_admits[read-only-False]` | 0/9 | 0/7 |
    | `test_shim_forwards_model_and_original_argv[args0-gpt-6-astra]` (bash 5 s timeout) | 3/9 | 0/7 |
    | `test_wait_rechecks_on_its_own_clock_without_a_commit` | 4/9 | 3/7 |
    | `test_version_timeout_is_reported_as_a_timeout` | 2/9 | 1/7 |
    | `test_c16_3_run_minted_id_refused_on_re_send_finds_its_job` (client's 1 s reply wait) | 2/9 | 0/7 |
    | `test_no_wake_up_is_lost[0]` | 5/9 | 3/7 |
    | `test_no_wake_up_is_lost[3]` | 2/9 | 3/7 |

    Six of the eight failed on c79aed84 as well. The two that did not are a
    bash script's 5 s timeout and a CLI's 1 s reply wait against an
    in-process fake. Neither can reach the changed code, and the difference
    is not significant (Fisher p ≈ 0.22 and 0.47).
- **Review.** An independent Opus review found no blocking issue. It agreed
  with the I3-over-literal-I1 reading. It found the leaderless-group
  overclaim, which is now corrected, and it is the source of the two limits
  above.
