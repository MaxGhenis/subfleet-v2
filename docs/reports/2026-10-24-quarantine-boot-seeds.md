# A recorded pid from an earlier boot never keeps an attempt quarantined (D-QUAR-REUSE1), and a gone worktree has nothing to salvage (D-SALV-MISSING1)

Branch `fix/quarantine-reuse-217`, base `release/217` at `15bd8c172`. Built in two runs: job
`20261024-112128-quar-reuse-build` (commits `623c42437`..`993a28ef3`, ended by a lost network) and
its continuation `20261024-193043-quar-reuse-buildb` (commits from `c9e0af2fc`). Not pushed.

## The incident

On 2026-10-24 the dead attempt `20261014-071003-trackb-d1408-review5/a1` stayed quarantined after
`subfleet kill --confirm-dead` (15:10:56Z).

- Its guardian is recorded on boot `421449f2…` (`start.json`: guardian_pid 76526, pgid 76526,
  proc_start "Wed Oct 14 12:05:49 2026").
- Its exit receipt (`exit.json`, finished 2026-10-14T12:28:32Z) records `child_pid 83026` with no
  identity.
- On the current boot `93c98812…`, pid 83026 was an unrelated `tail -40`, started 15:07:47Z.
- The census: `descendant_pids [83026]`, `live_pids [83026]`, `errors ["recorded identity
  unavailable for pid 83026"]`, `unverifiable true`.

(These values are the hub's, from the brief. Neither run read the live store or daemon.)

## The mechanism, at 15bd8c172

- `Daemon._contain` (`subfleet/daemon.py:8154`) passes `a.get("child_pid") or
  receipt.get("child_pid")` to `procs.containment` (`daemon.py:8197`), with the guardian's
  identity and every stored owned, responsible and counted identity as seeds.
- `containment` (`subfleet/procs.py:703`) seeds the walk with every recorded pid that
  `is_recycled` cannot prove recycled (`procs.py:841-842`). `is_recycled` (`procs.py:756`) finds
  no recorded birth for a live 83026, appends `recorded identity unavailable for pid 83026`
  (`procs.py:771`) and returns False, so 83026 stays a root, is counted, and the census is
  unverifiable.
- The group source is kept whenever its leader is not proven recycled (`procs.py:833`), so a later
  boot's process group numbered like the old pgid, its leader gone, is counted too.
- `_contain` then keeps every member a census counted in `counted_identities` (D-KILL2,
  `sighted` at `daemon.py:8211`, written under event `attempt.counted_recorded` at
  `daemon.py:8219-8225`) with no boot check, so the stranger's current-boot identity was kept and
  seeded every later census as a recorded survivor whose identity matches.
- A reboot ends every process. The child a receipt names was born on the attempt's boot, the boot
  its guardian identity records; on a later boot no process can be it or descend from it.

The continuation found one more path of the same kind (below, "The responsibility root"). A
guardian recorded as a responsibility root without a kernel unique id
(`Daemon._spawn_guardian` records `{"pid", "unique_id": None}` when the launch's metadata read
fails) is rejected while its pid is absent: `responsibility root identity unavailable for pid
<guardian>` (`procs.py:904`). On a later boot that held the attempt for as long as the old
guardian pid stayed free, at 15bd8c172 and at the first run's `993a28ef3` alike (executed: probe
and the regression `test_the_attempts_responsibility_root_never_holds_it_on_a_later_boot`). A
known-id root's final holder read failing did the same, for that pass.

## Each decision as built

**1. Boot-scoped seeds** (`procs.containment`, new keyword `attempt_boot`, `procs.py:717`).
`_contain` passes the guardian's recorded boot, the row's `boot_id` else `start.json`'s
(`daemon.py:8220`). Only a boot-session UUID names the attempt's boot (`procs.py:770`); legacy
`kern.boottime` seconds can shift within a boot (C-5.3) and decide nothing. `attempt_boot_over()`
(`procs.py:868`) compares it with the table's boot by `boot_identity.matches`, and re-reads a
mismatch after `forget_boot_id()` (C-5.12), once per census and only when a seed, a parent link to
one, the recorded group or a failed root check depends on it. When it is over (`procs.py:920-931`)
no recorded pid seeds the walk and the recorded group is cleared. Each seed the table shows, or
whose pid parents a row, and the group's leader, go under `recycled` through `exclude()`
(`procs.py:885`). A birth the ordinary comparison can decide keeps its reason ("different start
identity", "different boot identity"). Any other, an identity-less seed included, is recorded for
`different boot identity (attempt's boot)`, and the comparison's errors are dropped. An unknown or
unreadable boot (none recorded, legacy seconds, a current boot that cannot be read or is seconds)
takes the 15bd8c172 path unchanged. The first run applied this to every seed and to the group,
not only to identity-less seeds: invariant 1 requires it, since an old pgid's group or an old
pid's children on the new boot are the same stranger by another route
(`test_an_orphaned_group_and_an_absent_seeds_children_on_a_later_boot_are_not_the_attempts`).

**The responsibility root** (continuation, `procs.py:986-1060`). The root is the guardian, recorded
on the attempt's boot (`_contain` builds `ResponsibleRoot(guardian, boot, …)` only when the rooted
pid is the guardian). `root_ended()` is true when the root's recorded boot is the attempt's UUID
and `attempt_boot_over()`. It is asked only where a root check fails: the early check of a root
without a unique id (`procs.py:1015`) and the final holder check (`procs.py:1060`). When it is
true neither adds `responsibility root identity unavailable`. Members were already excluded by
`ours()` for a root on a proven other boot. On the attempt's own boot or with an unknown boot,
both checks fail exactly as before (guards below).

**2. No adoption across boots** (`daemon.py:8222-8236`). `_contain`'s `attempts(pid)` drops from
`fresh` (responsible identities) and `sighted` (counted identities) any census identity whose
boot-session UUID differs from the attempt's recorded boot (`boot_identity.differ`,
`boot_identity.py:26`: only two different UUIDs differ; seconds, a missing boot or seconds beside
a UUID never do). A member counted on the attempt's own boot, or with the attempt's boot unknown,
is kept exactly as D-KILL2 and D-CEN1 require
(`test_decision_2_on_the_attempts_own_boot_every_member_is_still_kept`, and the property
`test_decision_2_property_kept_identities_are_exactly_the_attempts_boot`).

**3. Repair** (`procs.py:772-783`). Before anything else, a census with a known attempt boot
filters the stored identities (owned, responsible, counted, and their alternatives at one pid):
one whose UUID differs from the attempt's is not the attempt's. It is never a seed and never
counted, and the census names it under `other_boot` (`Containment.other_boot`, `procs.py:348`;
`to_dict` writes the key only when there is one, `procs.py:373`, so other censuses read as they
always did). Nothing is rewritten or deleted: trackb's stored `counted_identities` keeps the
stranger's entry (`test_repair_an_adopted_later_boot_identity_is_not_the_attempts`,
`test_trackb_as_release_217_left_it_is_repaired`). The filter also covers owned identities. An
owned identity on another boot than the guardian's cannot be the attempt's either, and
`signal_process` still checks a fresh identity (`same_process`) before any signal
(`procs.py:1142`).

**4. D-SALV-MISSING1** (`daemon.py:272`, `:294`, `:8686-8753`). `_gone(path)` is true only for an
absence the file system reports (`FileNotFoundError`, `NotADirectoryError` from `os.lstat`). A
worktree gone before salvage runs no git. One that vanished during a failed salvage that wrote no
snapshot is treated the same, without a retry. Either way the receipt is `{"result": null,
"checkpoint": null, "error": null, "skipped": [], "gone": true}`, the attempt's evidence and a
release's audit event carry `salvage_gone: "worktree already gone: nothing to salvage"`, and the
notice gives that line. Nothing says "the worktree is kept", and no `salvage_error` reaches
admission, which holds a start snapshot for a retry only after one (`daemon.py:5155`). A worktree that exists but cannot be salvaged fails and is kept
as before (`test_d_salv_missing1_a_worktree_that_is_there_but_broken_still_fails_and_is_kept`).

**5. Contract.** C-5.5 states the boot scope of seeds, the repair, `other_boot`, the root's checks
and the extra boot read, and keeps the limits: "The boot scope above covers only an attempt whose
recorded boot is known to be over: same-second reuse within one boot and missing successor
ordering remain limits." C-13.1 states the gone worktree. Change-log entries: C-5.5 and C-13.1
(2026-10-24).

## Invariants, and the test that checks each

1. **Cross-boot.** For an attempt whose recorded boot is known and is not the current boot, no
   process on the current boot is counted through a recorded pid (a seed, the walk, the recorded
   group, the root's members), no seed comparison or root check makes the census unverifiable,
   and `_contain` keeps none of their identities.
   - Property: `test_invariant_1_cross_boot_no_current_process_is_counted_seeded_or_kept`.
   - Regressions: the trackb shape (receipt and row child), the adoption, the orphaned group, the
     two root shapes, and the fake daemon's `kill --confirm-dead` tests.
   - **Stated exception:** the marker source matches the attempt's environment strings, not a pid,
     and is unchanged (the hub scoped this change to seeds). A process of a later boot that
     carries the attempt's exact `SUBFLEET_ATTEMPT`/`SUBFLEET_ROOT` pair is still counted, which
     only holds the attempt. It is never kept (`test_repair_still_holds_for_whatever_else_is_live`,
     fake `test_invariant_4_…`).
2. **Same boot unchanged.** On the attempt's own boot, or with its boot unknown, and with no
   stored identity from another boot, the census equals 15bd8c172's whole `to_dict()`: sources,
   identities, errors (an identity-less live seed stays unverifiable), shapes and `recycled`.
   - Property: `test_invariant_2_on_the_attempts_own_boot_every_source_counts_as_at_15bd8c172`.
   - Guards: `test_same_boot_an_identity_less_live_seed_is_still_unverifiable`,
     `test_unknown_boot_the_result_is_unchanged` (unrecorded, legacy seconds, unreadable, current
     legacy), `test_the_roots_identity_checks_are_unchanged_without_a_boot_known_over`, and fake
     `test_trackb_on_its_own_boot_is_still_held`.
3. **Monotone safety.** A differential test against a verbatim copy of 15bd8c172's `containment`
   (`tests/reference_census_15bd8c172.py`, rebound to the live module's globals so the same
   fixtures reach it; its body was re-checked identical to `git show 15bd8c172:subfleet/procs.py`
   lines 703-1001 in the continuation). Over generated tables, the census differs from the base
   only in a cross-boot world (case 1) or one storing an identity from another boot (case 3). It
   never moves from unverifiable to verified-empty otherwise, and in a cross-boot world it never
   gains a pid or an error, and its marker pids are the base's.
   - Property: `test_invariant_3_any_difference_from_15bd8c172_is_a_cross_boot_case`.
4. **No signal authority** from counted or responsible identities.
   - Property: `test_invariant_4_contain_never_writes_signal_authority` (`_contain` never writes
     `owned_identities`).
   - Fake daemon: `test_invariant_4_no_signal_reaches_a_counted_or_responsible_identity`. The real
     kill protocol on a cross-boot attempt with an adopted counted identity, a stored responsible
     identity and a live marker process sends no `kill`/`killpg`, owns nothing new, and stays
     quarantined while the marker process lives.

The generated worlds (`worlds()` in `tests/unit/test_census_boot_seeds.py`) draw up to 8 rows over
pids 100-109 with random parents, groups, states (including zombies) and starts; the attempt's
boot (UUID A, none, legacy seconds); the current boot (A, B, unreadable); guardian, child, owned,
responsible and counted identities on any of six recorded boots; marker processes; and, since the
continuation, a responsibility root with a unique id, without one, or none, and a failing kernel
read of the guardian's pid. Settings: `max_examples=150`, `derandomize=True`, `database=None`,
no deadline. That is bounded and reproducible like the repository's `ci` profile (40 examples),
with more examples.

## Mutations

Each mutant was applied to the committed code, its named tests were run (one pytest at a time, inside
a suite hold), and the file was restored. A mutant is killed when a named test fails. The brief's four
are M1 (drop the boot check; M9 is the same for the root), M2a and M2b (use the current boot instead
of the attempt's, in the census and in `_contain`'s keep check), M3 (keep writing cross-boot counted
identities) and M4 (treat an unknown boot as different). The others cover the rest of each decision.
All 14 are killed, and every run ended with the sources restored (`git diff --quiet -- subfleet`).

| Mutant | Mutation | File | Killed | Named tests that failed | Run |
|---|---|---|---|---|---|
| M1 | drop the boot check: the attempt's-boot rule fires for any recorded boot | `subfleet/procs.py` | yes (5 failed, 1 passed in 5.69s) | `test_trackb_on_its_own_boot_is_still_held`<br>`test_same_boot_an_identity_less_live_seed_is_still_unverifiable`<br>`test_unknown_boot_the_result_is_unchanged[current-legacy]`<br>`test_unknown_boot_the_result_is_unchanged[legacy-seconds]`<br>`test_unknown_boot_the_result_is_unchanged[unreadable]` | H1 |
| M1x | drop the boot rule entirely: a cross-boot attempt's seeds are seeded as at the base | `subfleet/procs.py` | yes (3 failed in 7.52s) | `test_trackb_confirm_dead_releases_the_attempt`<br>`test_trackb_a_child_pid_reused_on_a_later_boot_is_recycled_not_counted[receipt-child]`<br>`test_trackb_a_child_pid_reused_on_a_later_boot_is_recycled_not_counted[row-child]` | H1 |
| M2a | use the current boot instead of the attempt's (the census's comparison) | `subfleet/procs.py` | yes (3 failed in 1.76s) | `test_trackb_confirm_dead_releases_the_attempt`<br>`test_trackb_a_child_pid_reused_on_a_later_boot_is_recycled_not_counted[receipt-child]`<br>`test_trackb_a_child_pid_reused_on_a_later_boot_is_recycled_not_counted[row-child]` | H1 |
| M2b | use the member's own (current) boot instead of the attempt's (`_contain`'s keep check) | `subfleet/daemon.py` | yes (2 failed in 7.45s) | `test_invariant_4_no_signal_reaches_a_counted_or_responsible_identity`<br>`test_decision_2_an_identity_from_another_boot_than_the_attempts_is_never_kept` | H1 |
| M3 | keep writing cross-boot counted identities | `subfleet/daemon.py` | yes (3 failed in 1.67s) | `test_invariant_4_no_signal_reaches_a_counted_or_responsible_identity`<br>`test_decision_2_an_identity_from_another_boot_than_the_attempts_is_never_kept`<br>`test_repair_still_holds_for_whatever_else_is_live` | H1 |
| M4 | treat an unknown boot as different | `subfleet/procs.py` | yes (3 failed, 7 passed in 7.07s) | `test_unknown_boot_the_result_is_unchanged[current-legacy]`<br>`test_unknown_boot_the_result_is_unchanged[legacy-seconds]`<br>`test_unknown_boot_the_result_is_unchanged[unreadable]` | H1 |
| M5 | keep stored identities from another boot as seeds (no repair) | `subfleet/procs.py` | yes (3 failed in 4.70s) | `test_trackb_as_release_217_left_it_is_repaired`<br>`test_repair_an_adopted_later_boot_identity_is_not_the_attempts[also-the-receipt-child]`<br>`test_repair_an_adopted_later_boot_identity_is_not_the_attempts[counted-only]` | H1 |
| M6 | salvage a gone worktree as before (D-SALV-MISSING1) | `subfleet/daemon.py` | yes (1 failed, 2 passed in 1.41s) | `test_d_salv_missing1_salvage_asks_git_nothing_about_a_worktree_already_gone` | H2 |
| M7 | a cross-boot root recorded without a unique id still fails its identity check | `subfleet/procs.py` | yes (2 failed, 1 passed in 2.80s) | `test_trackb_with_a_root_recorded_without_a_unique_id_is_released`<br>`test_the_attempts_responsibility_root_never_holds_it_on_a_later_boot[unique-id-unrecorded]` | H1 |
| M8 | a cross-boot root still takes the final holder read's failure | `subfleet/procs.py` | yes (1 failed, 1 passed in 0.82s) | `test_the_attempts_responsibility_root_never_holds_it_on_a_later_boot[final-holder-unreadable]` | H1 |
| M9 | drop the boot check for the root: its checks are skipped whenever it has a UUID boot | `subfleet/procs.py` | yes (2 failed, 4 passed in 1.62s) | `test_the_roots_identity_checks_are_unchanged_without_a_boot_known_over[final-holder-unreadable-own-boot]`<br>`test_the_roots_identity_checks_are_unchanged_without_a_boot_known_over[unique-id-unrecorded-own-boot]` | H1 |
| M6b | a worktree removed while salvage runs is a failure as before (D-SALV-MISSING1) | `subfleet/daemon.py` | yes (1 failed in 0.68s) | `test_d_salv_missing1_a_worktree_removed_while_salvage_runs_is_gone_not_failed` | H2 |
| M6c | the notice leaves out the gone line (D-SALV-MISSING1) | `subfleet/daemon.py` | yes (2 failed in 1.26s) | `test_d_salv_missing1_a_finalization_whose_worktree_is_gone_is_no_failure`<br>`test_d_salv_missing1_a_release_whose_worktree_is_gone_reports_nothing_to_salvage` | H2 |

M6 first survived (H1, `2 passed`): the check after a failed salvage reports the same receipt when
the worktree is missing, so the release and finalization tests could not tell the pre-check from it.
The continuation added `test_d_salv_missing1_salvage_asks_git_nothing_about_a_worktree_already_gone`
(`dc4361826`: neither salvage nor its HEAD read runs on a missing path), which kills it (H2, above),
and split out M6b (the check after a failure) and M6c (the notice line).

The first run's M2b compared a member's identity with itself, and its runner was cut by the network
loss with that mutant still in `daemon.py` (the uncommitted patch, below). The continuation kept M2b
as written: in a cross-boot census every member's identity is the current boot's, so comparing with
the member's own boot is comparing with the current boot.

## Test runs

Every pytest ran under `host-runs/suite_hold.sh`, one at a time, in a scratch folder, with a venv
built for this worktree. The first run's venv was installed from the first run's worktree, so it was
not reused. No test touched the live store or daemon. The shared lock gave this job two holds;
waits ran up to 72 minutes, and slot B was mostly closed for low disk. Each hold
ran its steps in priority order under one 19-minute cut:

- **H1** (slot A, 04:08-04:26Z): the new tests at the fix, the mutations, the base swap (cut at its
  240 s cap, its output lost with it; it included the Hypothesis properties, which H2 leaves out),
  then the census, salvage and quarantine files until the cut.
- **H2** (slot B, 04:31-04:49Z): the escapers tests that failed in H1, alone, at the fix and at the
  base; both salvage fake files at the fix; the salvage mutants; the swaps; then other files.
- **H3** (queued 04:49:52Z, cancelled unstarted at 05:50:58Z, the brief's 4 h 30 m mark): it would
  have rerun the escapers file in its H1 order at the fix and at the base, then more files.

**Regressions fail at the base, guards pass there** (H2, the new tests run against the base's
`procs.py`, `daemon.py` and `boot_identity.py`; the Hypothesis properties are left out there: two
through four compare the base with itself, and H1's run with them did not finish in 240 s):
22 failed, 14 passed. The 14 that pass are exactly the guards: the own-boot and unknown-boot
census guards (5), the root guards (6), `test_decision_2_on_the_attempts_own_boot_every_member_is_still_kept`,
fake `test_trackb_on_its_own_boot_is_still_held`, and
`test_d_salv_missing1_a_worktree_that_is_there_but_broken_still_fails_and_is_kept`. Against the
first run's `993a28ef3` (its `procs.py`), the three new root regressions fail and the six root guards
pass.

**The escapers flake.** In H1, `tests/fake/test_census_escapers_daemon.py` gave 7 passed, 2 failed,
1 error: `test_d_kill2_a_lockf_whose_markers_the_census_cannot_read_holds_the_attempt_on_any_macos[rooted]`
failed and its teardown errored, and `test_d_kill2_the_daemon_s_stand_in_hides_markers_the_host_shows`
failed. Both start a real daemon with `--hide-environment-of` and a real `/usr/bin/lockf` or node
escaper. The cut swallowed their tracebacks. Rerun alone in H2, all three cases passed at the fix
(3 passed in 12.29 s) and at the base (3 passed in 10.17 s). The rerun of the whole file in its H1 order (after
`test_salvage_finalization.py` and `test_census_recycled_pid_daemon.py`), at the fix and at the base,
was H3's first step and never ran. So the H1 failure is not explained. It did not reproduce alone at
either source, and nothing here shows whether it depends on order, host timing or this change. What
bears on it: these tests run on one boot, where the census equals the base's on every generated
world (invariant 2, which passed in H1), and the only new reads on one boot are one boot-identity
comparison when a seed shows or a root check fails. That is evidence, not proof: the property runs
on fake process tables, and these tests read the real kernel.

**What changed in an existing test.** `test_removed_workspace_during_finalization_records_failure_and_replays`
in `tests/fake/test_salvage_adversarial.py` asserted release/217's
`salvage failed: git rev-parse failed (exit 128)` for a detached job's removed workspace, the receipt
decision 4 replaces. Its detached case now expects the gone receipt and its replay; its turn case
(the end snapshot, which this change does not cover) is unchanged. It is renamed
`…_is_recorded_once_and_replays` (`10f2d3a12`). It failed in H1 under the old expectation and passed
in H2 (8 passed in the file).

### Per-directory and per-file counts at the fix

| Directory | Test files | Complete | Partial | Unrun | Passed | Failed | Errors | Skipped | Xfailed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| tests/unit | 348 | 17 | 0 | 331 | 414 | 0 | 0 | 0 | 0 |
| tests/fake | 115 | 33 | 1 | 81 | 994 | 2 | 1 | 0 | 2 |

| File | Hold | Passed | Failed | Errors | Skipped | Xfailed | Status | Other runs |
|---|---|---:|---:|---:|---:|---:|---|---|
| `tests/unit/test_boot_identity.py` | H1 | 10 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_boot_seeds.py` | H1 | 30 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_counted.py` | H1 | 5 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_identity_uncertainty.py` | H1 | 8 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_procargs.py` | H1 | 52 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_recycled_pid.py` | H1 | 25 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_responsibility_failures.py` | H1 | 20 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_responsibility_identity.py` | H1 | 17 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_responsibility_races.py` | H1 | 34 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_census_survivors_review.py` | H1 | 14 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_host_push_quarantine_restart.py` | H1 | 8 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_procs.py` | H1 | 48 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_retention_salvage_properties.py` | H1 | 2 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_salvage.py` | H1 | 31 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_salvage_optional_transient.py` | H1 | 15 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_salvage_ref_race.py` | H1 | 9 | 0 | 0 | 0 | 0 | complete |  |
| `tests/unit/test_salvage_unindexable.py` | H1 | 86 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_admission_held_waiters.py` | H2 | 1 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_admission_latency.py` | H2 | 14 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_admission_liveness.py` | H2 | 32 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_admission_priority.py` | H2 | 33 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_admission_properties.py` | H2 | 27 | 0 | 0 | 0 | 1 | complete |  |
| `tests/fake/test_admission_route_isolation.py` | H2 | 93 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_admission_stateful.py` | H2 | 17 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_canonical_identity_fix2.py` | H2 | 21 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_canonical_identity_fix3.py` | H2 | 17 | 0 | 0 | 0 | 1 | complete |  |
| `tests/fake/test_capacity_steer_daemon.py` | H2 | 72 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_census_boot_seeds_daemon.py` | H1 | 5 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_census_escapers_daemon.py` | H1 | 7 | 2 | 1 | 0 | 0 | complete | H2 selected tests: 3 passed |
| `tests/fake/test_census_recycled_pid_daemon.py` | H1 | 29 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_codex_turn_completed_fake.py` | H2 | 109 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_daemon_contract.py` | H2 | 21 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_daemon_descriptors.py` | H2 | 13 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_daemon_launch.py` | H2 | 9 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_daemon_stacks.py` | H2 | 6 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_detached_cwd_pins.py` | H2 | 18 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_detached_retention_fence.py` | H2 | 89 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_frozen_stream_nonregular.py` | H2 | 140 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_identity_rollback.py` | H2 | 5 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_idle_cost.py` | H2 | 2 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_interactive_steer.py` | H2 | 13 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_lane_fault.py` | H2 | 20 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_lanes_enroll.py` | H2 | 15 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_lanes_reenroll.py` | H2 | 19 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_launch_env_daemon.py` | H2 | 49 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_main_refusal_write_dir.py` | H2 | 5 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_native_maintenance_startup.py` | H2 | 22 | 0 | 0 | 0 | 0 | partial (cut) |  |
| `tests/fake/test_salvage_adversarial.py` | H2 | 8 | 0 | 0 | 0 | 0 | complete | H1 partial (cut): 7 passed, 1 failed |
| `tests/fake/test_salvage_finalization.py` | H1 | 47 | 0 | 0 | 0 | 0 | complete |  |
| `tests/fake/test_salvage_worktree_gone.py` | H2 | 6 | 0 | 0 | 0 | 0 | complete | H1 complete: 5 passed |
| `tests/fake/test_turn_retention_quarantine.py` | H2 | 10 | 0 | 0 | 0 | 0 | complete |  |

Unrun: every file in the UNRUN lists below never started; the brief's 4 h 30 m mark for starting
holds came first. The partial file stopped at the cut, with the counts shown.

- **tests/unit, 331 files unrun:** `test_admission_uncapped.py`, `test_agent_secret_credentials.py`, `test_app_cutover_daemon.py`, `test_approval_commit.py`, `test_approval_resolution.py`, `test_capacity_desktop.py`, `test_capacity_steer_mutations.py`, `test_capacity_steer_route.py`, `test_capacity_view.py`, `test_ci_retry_known_flakes.py`, `test_ci_shard_plugin.py`, `test_claude_adapter.py`, `test_claude_attest.py`, `test_claude_cards.py`, `test_claude_cards_wiring.py`, `test_claude_conversation_mcp_scope.py`, `test_claude_identity.py`, `test_claude_large_stream.py`, `test_claude_mcp.py`, `test_claude_mcp_launch.py`, `test_claude_stream.py`, `test_claude_synthetic_error.py`, `test_claude_turn.py`, `test_claude_usage.py`, `test_claude_v1_parity.py`, `test_cli.py`, `test_cli_batch.py`, `test_cli_turns.py`, `test_client_availability.py`, `test_codex_adapter.py`, `test_codex_ca_bundle.py`, `test_codex_ca_bundle_provider.py`, `test_codex_probe.py`, `test_codex_shim.py`, `test_codex_turn.py`, `test_codex_turn_completed.py`, `test_compat.py`, `test_continuation_r2.py`, `test_conversation_catalog.py`, `test_conversation_catalog_lifecycle.py`, `test_conversation_connections.py`, `test_conversation_diff.py`, `test_conversation_fifo_reads.py`, `test_conversation_handoff.py`, `test_conversation_handoff_briefs.py`, `test_conversation_handoff_dispatch_races.py`, `test_conversation_history.py`, `test_conversation_peers_attachments.py`, `test_conversation_reconcile.py`, `test_conversation_redact.py`, `test_conversation_refresh.py`, `test_conversation_service.py`, `test_conversation_state_reads.py`, `test_conversation_status.py`, `test_conversation_steer_service.py`, `test_conversation_store.py`, `test_conversation_stream_assertion.py`, `test_conversation_titles.py`, `test_conversation_waits.py`, `test_conversation_wakes.py`, `test_cpu_scaling.py`, `test_daemon_accept.py`, `test_daemon_connections.py`, `test_daemon_fifo_reads.py`, `test_daemon_init_release.py`, `test_daemon_inspection_load.py`, `test_daemon_inspection_properties.py`, `test_daemon_integration.py`, `test_daemon_log_cut_windows.py`, `test_daemon_log_first_pass.py`, `test_daemon_log_handler.py`, `test_daemon_log_mutation_tool.py`, `test_daemon_log_reminders.py`, `test_daemon_log_scrubbing.py`, `test_daemon_log_sites.py`, `test_daemon_log_tail.py`, `test_daemon_qos_tools.py`, `test_daemon_refusals_cli.py`, `test_daemon_settle.py`, `test_daemon_verbs.py`, `test_decision_horizon.py`, `test_descriptor_review_fixes.py`, `test_descriptors.py`, `test_descriptors_desktop.py`, `test_desktop_account_cache.py`, `test_desktop_in_use.py`, `test_desktop_ledger.py`, `test_disk_admission.py`, `test_disk_floor.py`, `test_doctor.py`, `test_doctor_ca_bundle.py`, `test_doctor_identity.py`, `test_empty_provider_results.py`, `test_enrollment_runner.py`, `test_failed_keep.py`, `test_failed_keep_layout.py`, `test_failed_keep_prune.py`, `test_failed_keep_teardown.py`, `test_fake_codex_http.py`, `test_filesystem_identity_agreement.py`, `test_folders.py`, `test_forget_paced.py`, `test_fresh_until.py`, `test_frontend_probe_cache.py`, `test_frozen_provider_streams.py`, `test_gate_admission.py`, `test_gate_merge.py`, `test_gate_replay.py`, `test_gate_revision.py`, `test_gate_service.py`, `test_gate_verdict.py`, `test_guard_overlay.py`, `test_guard_trust.py`, `test_guardian.py`, `test_guardian_qos.py`, `test_handler_refusals.py`, `test_held_waiter_holdback.py`, `test_hidden_environment.py`, `test_hook_cost.py`, `test_hooks.py`, `test_hooks_native_guard.py`, `test_host_push.py`, `test_host_push_admission.py`, `test_host_push_build.py`, `test_host_push_build_budget.py`, `test_host_push_cli.py`, `test_host_push_config_cost.py`, `test_host_push_copy.py`, `test_host_push_export.py`, `test_host_push_intake.py`, `test_host_push_metadata.py`, `test_host_push_mutations.py`, `test_host_push_names.py`, `test_host_push_restart.py`, `test_host_push_retention.py`, `test_host_push_storage_recovery.py`, `test_host_push_storage_retry_budget.py`, `test_host_push_store_completion_pr183.py`, `test_host_push_store_interleavings.py`, `test_host_push_trees.py`, `test_host_push_validated_top.py`, `test_importer.py`, `test_inspection_pacing_properties.py`, `test_invariants_index.py`, `test_keychain_exec_guard_properties.py`, `test_lane_answers.py`, `test_lanes_transfer.py`, `test_launch_env_allowlist.py`, `test_launch_env_helpers.py`, `test_launch_env_mutations.py`, `test_launch_env_policy.py`, `test_launch_env_round2.py`, `test_launch_env_round3.py`, `test_launch_env_round4.py`, `test_launch_env_round5.py`, `test_lease_waiter_selection.py`, `test_legacy_hold.py`, `test_limit_from_model_text.py`, `test_live_store_guard.py`, `test_live_store_guard_round10.py`, `test_live_store_guard_round10_mutations.py`, `test_live_store_guard_round3.py`, `test_live_store_guard_round4.py`, `test_live_store_guard_round5.py`, `test_live_store_guard_round6.py`, `test_live_store_guard_round7.py`, `test_live_store_guard_round7_mutations.py`, `test_live_store_guard_round8.py`, `test_live_store_guard_round8_mutations.py`, `test_live_store_guard_round9.py`, `test_live_store_guard_round9_mutations.py`, `test_live_store_guard_state_root.py`, `test_lockwatch.py`, `test_machine_signals.py`, `test_machine_topology.py`, `test_mcp_cli_contract.py`, `test_mcp_scheduler_contract.py`, `test_mirror_flags_faults.py`, `test_mirror_flags_model.py`, `test_mirror_flags_stateful.py`, `test_native_operations.py`, `test_notice_rendering.py`, `test_notify_push.py`, `test_offline.py`, `test_operator_hold_closures.py`, `test_operator_notices.py`, `test_outcome_unknown.py`, `test_person_boundary.py`, `test_person_boundary_identity.py`, `test_picker.py`, `test_placement_ramp.py`, `test_policy.py`, `test_policy_disk.py`, `test_policy_ramp.py`, `test_policy_support.py`, `test_policy_tier_preferences.py`, `test_pr124_fixes.py`, `test_pr124_review_r2_fixes.py`, `test_pr192_review_r1.py`, `test_pr192_review_r2.py`, `test_protocol_registry.py`, `test_protocol_secrecy.py`, `test_provider_active_cap.py`, `test_prune_217.py`, `test_prune_decision_backup_verification.py`, `test_prune_decision_fingerprints.py`, `test_prune_decisions.py`, `test_prune_restore.py`, `test_quota_projection.py`, `test_rank_review_report.py`, `test_reap_liveness.py`, `test_rediscovery_git_trace.py`, `test_relay.py`, `test_render.py`, `test_render_quota_projection.py`, `test_reserve_authorization_plumbing.py`, `test_reset_credit_claim_timing.py`, `test_reset_credit_demand.py`, `test_reset_credit_properties.py`, `test_reset_credit_review_r1.py`, `test_reset_credit_review_r2.py`, `test_reset_credit_review_r3.py`, `test_reset_credit_review_r3_mutants.py`, `test_resource_identity.py`, `test_retention_81_notes.py`, `test_retention_archive.py`, `test_retention_archive_properties.py`, `test_retention_case_alias.py`, `test_retention_nested_leases.py`, `test_retention_ordinary_trees.py`, `test_retention_qos.py`, `test_retention_readback.py`, `test_retention_rediscovery.py`, `test_retention_regenerable.py`, `test_retention_regenerable_properties.py`, `test_retention_shared_folders.py`, `test_retention_sweep_race.py`, `test_retention_turn_contract.py`, `test_retention_turn_workdirs.py`, `test_retention_turns.py`, `test_retention_worktrees.py`, `test_review130_pass.py`, `test_review_pr127_fixes.py`, `test_review_pr127_history.py`, `test_review_pr127_pr_wakes.py`, `test_review_pr127_report.py`, `test_review_pr127_requests.py`, `test_review_pr127_service.py`, `test_review_r223_conv_probes.py`, `test_review_r2_repro.py`, `test_review_r3_measure.py`, `test_review_r3_repro.py`, `test_review_r4_repro.py`, `test_review_r5_repro.py`, `test_review_r6_probes.py`, `test_review_r6_restart.py`, `test_round10_refusal_context.py`, `test_route_check.py`, `test_scheduler.py`, `test_scheduler_machine_signals.py`, `test_scheduler_priority_callers.py`, `test_scheduler_split.py`, `test_scheduler_tier_preferences.py`, `test_scheduler_turns.py`, `test_scheduler_weekly.py`, `test_scoped_stream_capacity.py`, `test_scrub_key_order.py`, `test_session_credential_env.py`, `test_sessions_cli.py`, `test_sessions_handoff.py`, `test_sessions_handoff_excerpts.py`, `test_sessions_headless_properties.py`, `test_sessions_mirror.py`, `test_sessions_mirror_cache_coherence.py`, `test_sessions_mirror_fifo.py`, `test_sessions_mirror_hot_health.py`, `test_sessions_mirror_hot_progress.py`, `test_sessions_mirror_hot_regressions.py`, `test_sessions_mirror_inventory.py`, `test_sessions_mirror_load_gap.py`, `test_sessions_mirror_safety.py`, `test_sessions_mirror_write_churn.py`, `test_sessions_nudge.py`, `test_sessions_registry.py`, `test_sessions_revive.py`, `test_sessions_revive_reads.py`, `test_sessions_transcripts.py`, `test_settle_properties.py`, `test_soak_tools.py`, `test_status_json.py`, `test_steer_invariants.py`, `test_stop_alarm.py`, `test_stop_watchdog.py`, `test_stopped_daemon.py`, `test_store_contention_queries.py`, `test_store_ids.py`, `test_store_migration_2.py`, `test_store_properties.py`, `test_store_readers.py`, `test_store_snapshot_plans.py`, `test_suggest_cli.py`, `test_suggest_hint.py`, `test_suggestion_error_boundaries.py`, `test_suggestion_properties.py`, `test_suggestion_secret_properties.py`, `test_suggestion_structural_errors.py`, `test_suggestions.py`, `test_suggestions_r1b_adversarial.py`, `test_suggestions_r3_channels.py`, `test_suggestions_review_boundaries.py`, `test_suggestions_review_multiline.py`, `test_suggestions_review_response.py`, `test_test_module_names.py`, `test_tick_prefilter.py`, `test_timers_alerts.py`, `test_timers_busy_read.py`, `test_timers_daemon.py`, `test_timers_keepalive.py`, `test_timers_probe.py`, `test_timers_reset_credits.py`, `test_timers_retention.py`, `test_title_review_regressions.py`, `test_turn_attachment_replay.py`, `test_turn_overlap.py`, `test_turn_replay.py`, `test_turn_runner.py`, `test_ultracode_effort.py`, `test_unadmittable_pin.py`, `test_wait_hub.py`, `test_wake_cli.py`, `test_weekly_rank_vs_base.py`
- **tests/fake, 81 files unrun:** `test_admission_disk.py`, `test_admission_lease_waiters.py`, `test_admission_ramp.py`, `test_admission_ramp_retention.py`, `test_admission_visibility.py`, `test_batch_label.py`, `test_canonical_identity.py`, `test_canonical_identity_fix4.py`, `test_canonical_identity_fix5.py`, `test_capacity_wait_backoff.py`, `test_codex_ca_bundle_fake.py`, `test_conversation_pools.py`, `test_conversation_wakes_fake.py`, `test_crash_matrix.py`, `test_descriptor_exhaustion.py`, `test_enrollment_guardian.py`, `test_fake_provider.py`, `test_finalization_replay.py`, `test_folder_spelling.py`, `test_gate_end_to_end.py`, `test_interactive_claude_titles.py`, `test_lane_pilot.py`, `test_lanes_reenroll_keychain.py`, `test_launch_env_providers.py`, `test_launch_gates_above_stdio.py`, `test_lost_acknowledgement.py`, `test_machine_guard_daemon.py`, `test_mcp_job_contract.py`, `test_notice_delivery.py`, `test_notice_header.py`, `test_picker_native.py`, `test_policy_sandbox.py`, `test_probe_cancel.py`, `test_probe_recovery.py`, `test_probe_turn.py`, `test_prompt_seam.py`, `test_provider_active_cap_fake.py`, `test_provider_cap_review_evidence.py`, `test_provider_qos.py`, `test_record_secrecy.py`, `test_request_routing.py`, `test_reserve_authorization.py`, `test_reserve_authorization_validation.py`, `test_reset_credit_incident.py`, `test_resume_cli.py`, `test_resume_contract.py`, `test_retention_dormant.py`, `test_retry_for_good_refusals.py`, `test_review130_ab.py`, `test_review_r185_adversarial.py`, `test_review_r223_wall_probes.py`, `test_route_view.py`, `test_routing_end_to_end.py`, `test_routing_tier_preferences.py`, `test_sessions_end_to_end.py`, `test_state_contract.py`, `test_state_fixture_processes.py`, `test_status_payload.py`, `test_suggestions_fake.py`, `test_tier_hold_by_demand.py`, `test_timers_busy_admission.py`, `test_timers_end_to_end.py`, `test_turn_listing.py`, `test_turn_retention_cwd.py`, `test_turn_retention_cwd_properties.py`, `test_turn_retention_job_directory.py`, `test_turn_retention_job_properties.py`, `test_turn_trees.py`, `test_turn_wait_reasons.py`, `test_unadmittable_pin_admission.py`, `test_unadmittable_pin_rereview.py`, `test_unadmittable_pin_review.py`, `test_wall_cancel_races.py`, `test_wall_inspection_order.py`, `test_wall_running_time.py`, `test_wall_stop_deliveries.py`, `test_workspace_contract.py`, `test_workspace_enospc.py`, `test_workspace_transient.py`, `test_writable_fanout.py`, `test_writer_retention_fence.py`

## Continuation notes

- **Kept from the first run, as committed** (no history rewritten): `623c42437` (tests), `397df5da7`
  (census), `f05c0d2ab` (`_contain` and salvage) and `993a28ef3` (contract). Their code was re-read
  against the brief, and the checks the deliverables rest on were re-run here rather than taken from
  the first run's notes: the new tests at the fix, the regressions against the base (the first run
  had reported "26 failed, 5 guards passed" there; on today's set, with the properties left out,
  22 failed and 14 guards passed), the reference copy's identity with 15bd8c172, every mutant, and
  the census, salvage and quarantine files. Later commits extend two of their test files (the root
  shapes, the generator's draws, one salvage test).
- **Dropped:** the first run's uncommitted patch
  (`wall-salvage-2026-10-24/quar-reuse-build/uncommitted.patch`). It is not a change to build on.
  It is the first run's mutant M2b exactly (`differ(census.identities[pid].boot_id,
  census.identities[pid].boot_id)` in `_contain`'s keep check), left in the file when the network
  loss ended its mutation runner before its `finally` could restore the file. It was applied,
  compared with the runner's mutation list, and reverted.
- **Added in the continuation:**
  - the responsibility root's two checks: `c9e0af2fc` (tests), `644297910` (fix), `a41aa45bc`
    (contract), with the generator's root and holder-read draws and mutants M7-M9;
  - `dc4361826`, the test that kills M6 (and mutants M6b, M6c);
  - `10f2d3a12`, the salvage-adversarial test brought to decision 4;
  - this report.
- **Not covered, for the hub:** a turn's end snapshot (`trees.json`) of a removed workspace still
  fails as git says. Decision 4 names salvage only, so it was left alone.
