VERDICT #279: APPROVE

Reviewed `a37ddd1694c6` for descriptor ownership across the branch, including
`9b1a9d462..a37ddd169` for fix round r4. No new P1/P2 or r4-introduced P3 was
found in the executed domain. CI completion remains a separate merge gate.

## Review r4 finding

**P2-6 is fixed.** Rerun the four original `read_regular` cases: MemoryError
and KeyboardInterrupt at the first traced instruction of owner construction
and entry, on Python 3.14.7 and 3.12.14. All pass. Independent ordinary Python
exceptions replacing `__init__`/`__enter__` also propagate with identity before
any descriptor opens. All 35 site injections reach their intended owner;
140 matrix cases plus the three prepared checks pass on each Python.

## Checks 1–8

1. **Pass: owner setup.** All 35 converted owners are entered before acquisition.
   Both versions of the setup injection pass on both Pythons. There is no
   converted acquisition followed by owner construction or entry.
2. **Pass: hand-offs.** Read every converted descriptor's uses and the walk
   stack, open_dir, _open_path, _conflict_dir, _set_aside, _set_aside_fd and
   Reclaim paths. No slot transfers its owned descriptor to a closing owner;
   calls using an already-filled slot borrow it. Descriptor-returning helpers
   transfer into the caller's slot only after their own successful return.
   There is no slot-clear operation and no transfer between two live slots.
   Executed EIO/KeyboardInterrupt closes that release then reuse the same
   number for a different scratch file through walk-stack, helper-return,
   nested second-slot, set-aside, set-aside-fd and Reclaim inputs. Each acquired
   descriptor receives one close, the borrowed root remains live, and the
   replacement survives. R2 additionally executes path/conflict/set-aside
   reuse and walk/repository post-registration interrupts.
3. **Pass: inventory and storage.** The count is 35: retention_fs 27,
   attempt_tmp 1, retention_git 1, retention_survey 2, retention_archive 4.
   Independently enumerated every os.open/open_dir/_open_path/_conflict_dir
   call and the two built-in stream opens in retention_git. The latter already
   use stream context managers. Existing subprocess pipe owners in run,
   ObjectReader and the archive blob reader are not numeric-descriptor
   transfers into these slots and remain unchanged. The report's raw legacy sites are unchanged;
   helper validation/descent handlers own their results or return them and
   do not construct post-acquisition owners. Walk/repository collection owners
   enter empty; iterator owners receive unstarted generators.
   All slots use `[None]` plus direct item assignment, with no append. On 3.12
   every open-return-to-store sequence is LOAD_FAST, LOAD_ATTR, LOAD_CONST,
   STORE_SUBSCR; 3.14 uses LOAD_FAST[_BORROW], LOAD_ATTR, LOAD_SMALL_INT,
   STORE_SUBSCR. There is no intervening call or container construction. These
   instructions remain L5. Marker, package/scope, installed_files and
   site-packages retain their nested close order against 9b1a9d462.
4. **Pass, with added coverage.** Empty slots are skipped; live descriptors
   close once in order under the same first-error and precedence rules.
   The inherited property never generates an empty slot: each generated
   outcome opens one descriptor. The independent property always includes
   empty and live slots and checks all eight pending/handled categories,
   first error, cancellation, identity, context and cause over 150 generated
   examples on each Python. It passes. MEC exposes the inherited test-domain gap
   and is killed by this added property. Its minimized failing input is an
   in-flight RuntimeError and one EIO close with `[None, fd, None]`; production
   preserves the RuntimeError, while the mutant replaces it with EIO.
5. **Pass: tests and mutants.** The only r4-file diff is its one xfail
   decorator, covering four cases; the remaining AST is identical. Setup
   injections assert they fired at each exact owner line and compare real
   acquisitions/closes. Five manual regressions acquire inside the owner
   argument at still_archived, package/scope's inner owner, installed_files,
   _set_aside_fd and _Builder._walk; each is killed by its four site cases.
   The AST guard rejects local, attribute and call-result arguments in tuple,
   list and bare forms. Its exceptions are the empty walk stack view, empty
   repository list, and nine named iterator functions containing ten owners
   (two in _late_admin). All nineteen production mutants fail named call-phase
   tests; there are no configuration kills. MSR restores size's plain root
   finally while retaining iterator cleanup; MD skips empty slots while
   removing the drain. Both retargets preserve the intended mutation.
6. **Pass: behavior and measured cost.** The unchanged 33 walk plans, 33
   repository plans and 400+400 differential inputs pass on both Pythons.
   Those inherited differentials compare d3a48269a. Additional 400+400 runs
   compare the original release baseline directly, also on both Pythons.
   No exception class, TreeError code, errno, message, yield or repository
   result differs in those single-failure inputs. The inherited intentional
   GeneratorExit plus close-EIO precedence difference remains accounted for.
   The serial benchmark creates 100 top directories, 9,900 child directories
   and 90,000 empty files: 100,000 descendants, 100,001 yields including root.
   Initial whole-tree timing was noisy (head/release 2.303; head/pre-r4
   1.432). Three balanced rounds then interleave the modules within 100
   top-directory blocks and rotate their order; each round visits all
   100,000 descendants per module, including those top directories.
   Median wall seconds: release 30.303, pre-r4 30.508, head 32.998. Head/release = **1.089**, head/pre-r4 = **1.082**, below 1.5. Per-run CPU/wall times and loads are in the evidence.
7. **Pass: text.** Read the main, r1, r2, r3 and fix-r4 reports, their r4
   changes, evidence hashes, and the one permitted live PR-body read. Historical
   results are labelled as historical; ownership claims retain L1–L5 and
   exercised-input qualifications. Current production hashes match fix r4's
   evidence. No false r4 sentence or required PR-body correction was found.
8. **CI pending.** The single allowed `gh pr checks 279` read reports all
   five shards on each Python pending: **0 pass, 0 fail, 10 pending, 0 skip,
   0 cancelled**. Neither D-QT-FLAKE1 nor another failed test can be identified
   from the 20:28:46 UTC snapshot. No job logs or second snapshot were read.
   Run: https://github.com/MaxGhenis/subfleet-v2/actions/runs/41122943957.

## New findings

No P1/P2 or r4-introduced P3. L6 records the inherited property's test-domain
limitation; no head defect or false r4 claim was reproduced, and head passes
the added mixed-slot property.

## Listed differences

L1–L5 are unchanged and do not hold this verdict. Reexecuted inputs on both
Pythons and release controls remain strict expected failures; detailed events
are in the evidence JSON. Native-close interruption remains modelled only.

| ID | Executed input / result | One line for the doc |
| --- | --- | --- |
| L1 | Interrupt each documented marker-to-call offset after four yields of `a/b/leaf`; one popped child leaks and is released by the test; release pop-return also leaks. | Pop marker-to-call remains outside the ownership guarantee. |
| L2 | Interrupt the first instruction of normal _cleanup.__exit__; one descriptor leaks without a close; release finally-setup control also leaks. | Normal cleanup dispatch is not protected against asynchronous entry interruption. |
| L3 | admin_objects scratch read raises KeyboardInterrupt; raw handle close releases and raises EIO; head and release propagate EIO with cancellation as context. | Legacy raw handle cleanup can replace an in-flight cancellation. |
| L4 | Body cancellation followed by a second cancellation at the drain FOR_ITER after one close; original is replaced and two descriptors remain; release walk drain also aborts. | Repeated asynchronous interruption can abort the drain. |
| L5 | Interrupt open_dir's STORE_FAST immediately after open returns; head and release each leak one descriptor with zero close attempts. | Native open-return-to-store remains unprotected, including the new direct slot store. |
| L6 / coverage only | `tests/unit/test_retention_descriptor_ownership.py:53` generates only live descriptors. MEC clears pending only on None: the inherited property and simple empty-slot example pass (2P); the added mixed-slot property kills it. Both Pythons pass that property at head. | The maintained precedence property covers live slots; review r5 supplies mixed empty/live coverage. |

## Holds and exclusions

Times below are 2026-10-30 UTC; loads are 1/5/15-minute averages captured
inside admission with os.getloadavg(). P/F/X denote passed, failed, and strict
expected failure. Each hold contains one serial pytest; the shared wrapper
admits at most two concurrently. Every started test process belongs to this task. The initial batch drivers
were queued within the first hour. Lock waits pushed individual admissions
beyond it; the final hand-off probe follow-up was queued at 21:28:05 UTC
(after the first hour). All pytest start times are listed rather than claimed
on time. No hold was started after the four-hour cutoff.
Distinct maintained passes, including the added review file: **970 on Python 3.14.7 and 325 on Python 3.12.14**.

Recorded pytest invocations: 84. Longest: 380.06s. Peak runner RSS: 117.62 MiB. All completed pytests used the shared wrapper. Archive1 waited more than thirty minutes; the sandbox blocked the waiter-stop helper from discovering its PID. No duplicate direct run was started. It was subsequently admitted under the lock. Queue-log creation and wrapper admission timestamps are retained.

Not run: archive's `test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` (deselected explicitly), the full suite, live frontend tests (lane person-only check; CI owns them), broader regressions on 3.12, actual native-close signal delivery, or CI job logs.

### Core and parity

| Hold / Python | File or selection | Start UTC | Load 1/5/15 | Result | Seconds |
| --- | --- | --- | --- | --- | --- |
| review_r4314 / 3.14.7 | `tests/unit/test_retention_descriptor_review_r4.py` | 20:35:50 | 44.42/37.53/39.81 | 4P | 13.86 |
| prepared314 / 3.14.7 | `tests/unit/test_retention_descriptor_prepared.py` | 20:36:08 | 41.93/37.41/39.71 | 143P | 15.79 |
| review314 / 3.14.7 | `tests/unit/test_retention_descriptor_review.py` | 20:36:31 | 44.09/38.28/39.96 | 47P | 11.63 |
| review_r2314 / 3.14.7 | `tests/unit/test_retention_descriptor_review_r2.py` | 20:36:46 | 38.21/37.28/39.57 | 8P | 8.78 |
| review_r3314 / 3.14.7 | `tests/unit/test_retention_descriptor_review_r3.py` | 20:37:10 | 31.88/35.89/39.02 | 10P | 5.73 |
| cleanup314 / 3.14.7 | `tests/unit/test_retention_descriptor_cleanup.py` | 20:37:16 | 28.60/35.05/38.69 | 48P | 4.22 |
| ownership314 / 3.14.7 | `tests/unit/test_retention_descriptor_ownership.py` | 20:37:21 | 27.51/34.72/38.55 | 16P | 4.10 |
| walk314-1 / 3.14.7 | `tests/unit/test_retention_walk_descriptors.py` | 20:37:27 | 27.07/34.51/38.45 | 20P | 5.69 |
| walk314-2 / 3.14.7 | `tests/unit/test_retention_walk_descriptors.py` | 21:03:07 | 48.66/51.46/45.80 | 20P | 13.95 |
| walk314-3 / 3.14.7 | `tests/unit/test_retention_walk_descriptors.py` | 21:03:25 | 48.24/51.23/45.81 | 20P | 10.07 |
| walk314-4 / 3.14.7 | `tests/unit/test_retention_walk_descriptors.py` | 21:03:39 | 57.30/53.05/46.56 | 20P | 7.44 |
| walk314-5 / 3.14.7 | `tests/unit/test_retention_walk_descriptors.py` | 21:17:02 | 20.48/33.40/39.51 | 20P | 13.93 |
| parity314 / 3.14.7 | `test_parity.py` | 21:17:22 | 21.41/32.79/39.15 | 4P | 21.87 |
| review_r4312 / 3.12.14 | `tests/unit/test_retention_descriptor_review_r4.py` | 21:18:01 | 21.01/31.19/38.27 | 4P | 17.10 |
| prepared312 / 3.12.14 | `tests/unit/test_retention_descriptor_prepared.py` | 21:18:25 | 28.56/32.29/38.50 | 143P | 24.91 |
| review312 / 3.12.14 | `tests/unit/test_retention_descriptor_review.py` | 21:18:58 | 19.31/29.60/37.27 | 47P | 11.04 |
| review_r2312 / 3.12.14 | `tests/unit/test_retention_descriptor_review_r2.py` | 21:29:59 | 25.10/28.87/32.81 | 8P | 4.48 |
| review_r3312 / 3.12.14 | `tests/unit/test_retention_descriptor_review_r3.py` | 21:30:06 | 24.93/28.77/32.76 | 10P | 3.27 |
| cleanup312 / 3.12.14 | `tests/unit/test_retention_descriptor_cleanup.py` | 21:30:12 | 27.03/29.09/32.82 | 48P | 4.07 |
| ownership312 / 3.12.14 | `tests/unit/test_retention_descriptor_ownership.py` | 21:33:42 | 32.55/29.70/32.11 | 15P | 49.87 |
| walk312-1 / 3.12.14 | `tests/unit/test_retention_walk_descriptors.py` | 21:34:47 | 42.10/33.34/33.31 | 20P | 22.06 |
| parity312 / 3.12.14 | `test_parity.py` | 21:35:18 | 32.07/31.83/32.77 | 4P | 32.75 |

### Regression set and guards (3.14)

| Hold / Python | File or selection | Start UTC | Load 1/5/15 | Result | Seconds |
| --- | --- | --- | --- | --- | --- |
| archive1 / 3.14.7 | `tests/unit/test_retention_archive.py` | 21:04:30 | 37.17/48.31/45.20 | 38P | 250.01 |
| archive2 / 3.14.7 | `tests/unit/test_retention_archive.py` | 21:08:45 | 36.73/40.02/42.08 | 70P | 380.06 |
| turns / 3.14.7 | `tests/unit/test_retention_turns.py` | 21:15:56 | 22.18/36.82/41.12 | 7P | 11.33 |
| sweep-examples / 3.14.7 | `tests/unit/test_retention_sweep_race.py` | 21:16:09 | 22.31/36.38/40.91 | 12P | 35.44 |
| sweep-explicit / 3.14.7 | `tests/unit/test_retention_sweep_race.py` | 21:19:14 | 20.88/29.39/37.06 | 1P | 162.30 |
| sweep-generate / 3.14.7 | `tests/unit/test_retention_sweep_race.py` | 21:22:25 | 31.90/28.89/35.14 | 1P | 314.57 |
| readback1 / 3.14.7 | `tests/unit/test_retention_readback.py` | 21:27:44 | 29.68/29.97/33.77 | 24P | 123.18 |
| readback2 / 3.14.7 | `tests/unit/test_retention_readback.py` | 21:30:05 | 24.93/28.77/32.76 | 13P | 110.76 |
| retention_regenerable / 3.14.7 | `tests/unit/test_retention_regenerable.py` | 21:33:08 | 29.47/28.75/31.89 | 26P | 252.90 |
| retention_regenerable_properties / 3.14.7 | `tests/unit/test_retention_regenerable_properties.py` | 21:38:17 | 24.39/26.73/30.31 | 1P | 309.91 |
| timers_retention / 3.14.7 | `tests/unit/test_timers_retention.py` | 21:43:37 | 17.49/25.51/29.16 | 20P | 91.43 |
| attempt_tmpdir_guardian / 3.14.7 | `tests/unit/test_attempt_tmpdir_guardian.py` | 21:45:22 | 13.11/21.51/27.16 | 2P | 28.50 |
| attempt_tmpdir_leaks / 3.14.7 | `tests/unit/test_attempt_tmpdir_leaks.py` | 21:46:04 | 12.21/20.26/26.45 | 197P | 122.39 |
| attempt_tmpdir_lifecycle / 3.14.7 | `tests/unit/test_attempt_tmpdir_lifecycle.py` | 21:48:23 | 13.21/17.72/24.47 | 35P | 38.30 |
| attempt_tmpdir_mutations / 3.14.7 | `tests/unit/test_attempt_tmpdir_mutations.py` | 21:55:53 | 23.61/22.38/24.46 | 85P | 12.13 |
| attempt_tmpdir_retention / 3.14.7 | `tests/unit/test_attempt_tmpdir_retention.py` | 21:56:08 | 20.87/21.84/24.23 | 6P | 6.47 |
| attempt_tmpdir_sandboxes / 3.14.7 | `tests/unit/test_attempt_tmpdir_sandboxes.py` | 21:56:19 | 19.05/21.41/24.06 | 12P | 10.19 |
| test_module_names / 3.14.7 | `tests/unit/test_test_module_names.py` | 21:56:31 | 19.42/21.40/24.02 | 1P | 7.19 |
| invariants_index / 3.14.7 | `tests/unit/test_invariants_index.py` | 21:56:44 | 18.41/21.10/23.87 | 25P | 13.60 |
| attempt_tmpdir_daemon / 3.14.7 | `tests/fake/test_attempt_tmpdir_daemon.py` | 21:57:04 | 16.86/20.57/23.61 | 24P | 31.18 |
| attempt_tmpdir_leaks_daemon / 3.14.7 | `tests/fake/test_attempt_tmpdir_leaks_daemon.py` | 21:57:41 | 14.65/19.52/23.09 | 8P | 19.60 |
| state_contract / 3.14.7 | `tests/fake/test_state_contract.py` | 21:58:07 | 15.34/19.26/22.89 | 37P | 54.25 |

### Independent checks and listed-difference controls

| Hold / Python | File or selection | Start UTC | Load 1/5/15 | Result | Seconds |
| --- | --- | --- | --- | --- | --- |
| own314 / 3.14.7 | `test_r5.py` | 20:44:02 | 56.62/36.02/36.67 | 32P, 1F | 13.72 |
| release-parity314 / 3.14.7 | `test_release_parity.py` | 20:50:39 | 42.97/38.16/37.39 | 2P | 16.58 |
| listed314 / 3.14.7 | `test_r5_listed.py` | 20:51:00 | 37.59/37.22/37.06 | 14X | 11.04 |
| release-parity312 / 3.12.14 | `test_release_parity.py` | 20:51:19 | 39.69/37.70/37.24 | 2P | 26.47 |
| listed312 / 3.12.14 | `test_r5_listed.py` | 20:51:57 | 52.53/41.03/38.46 | 13X | 18.11 |
| own312 / 3.12.14 | `test_r5.py` | 21:03:51 | 51.93/52.03/46.30 | 33P | 7.85 |
| S1 / 3.14.7 | `tests/unit/test_retention_descriptor_prepared.py` | 21:04:01 | 48.88/51.37/46.13 | 4F | 8.91 |
| own314-corrected / 3.14.7 | `test_r5.py` | 21:11:47 | 43.07/46.22/44.58 | 33P | 12.36 |
| own312-corrected / 3.12.14 | `test_r5.py` | 21:12:04 | 41.93/45.82/44.46 | 33P | 9.59 |
| artifact314 / 3.14.7 | `tests/unit/test_retention_descriptor_review_r5.py` | 21:13:19 | 41.92/44.90/44.22 | 30P | 11.99 |
| artifact312 / 3.12.14 | `tests/unit/test_retention_descriptor_review_r5.py` | 21:13:42 | 40.67/44.32/44.03 | 30P | 10.90 |
| MEC-added / 3.14.7 | `tests/unit/test_retention_descriptor_review_r5.py` | 21:14:00 | 35.81/42.93/43.53 | 1F | 12.73 |
| S2 / 3.14.7 | `tests/unit/test_retention_descriptor_prepared.py` | 21:22:03 | 29.36/28.23/35.06 | 4F | 17.71 |
| S3 / 3.14.7 | `tests/unit/test_retention_descriptor_prepared.py` | 21:26:50 | 30.33/30.19/34.10 | 4F | 8.10 |
| S4 / 3.14.7 | `tests/unit/test_retention_descriptor_prepared.py` | 21:27:00 | 27.66/29.63/33.85 | 4F | 4.04 |
| S5 / 3.14.7 | `tests/unit/test_retention_descriptor_prepared.py` | 21:30:20 | 25.50/28.74/32.68 | 4F | 11.23 |
| MEC-control / 3.14.7 | `tests/unit/test_retention_descriptor_prepared.py` | 21:30:35 | 25.26/28.54/32.54 | 1P | 7.72 |
| MEC-control / 3.14.7 | `tests/unit/test_retention_descriptor_ownership.py` | 21:30:35 | 25.26/28.54/32.54 | 1P | 7.72 |
| handoff314-final / 3.14.7 | `tests/unit/test_retention_descriptor_review_r5.py` | 21:30:48 | 22.35/27.73/32.18 | 12P | 7.63 |
| handoff312-final / 3.12.14 | `tests/unit/test_retention_descriptor_review_r5.py` | 21:31:05 | 20.96/27.17/31.90 | 12P | 10.89 |
| MDR / 3.14.7 | `tests/unit/test_retention_descriptor_review_r5.py` | 21:31:19 | 23.61/27.47/31.92 | 1F | 10.50 |
| MEC / 3.14.7 | `test_r5.py` | 21:32:54 | 32.15/29.20/32.10 | 1F | 22.85 |

## Mutant table

| Mutant | Start UTC | Load 1/5/15 | P/F | Named killing test |
| --- | --- | --- | --- | --- |
| M1 | 20:35:21 | 42.28/36.49/39.52 | 2P, 2F | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction[1]` |
| M2 | 20:35:38 | 46.72/37.72/39.90 | 2P, 2F | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction[1]` |
| MO | 20:37:35 | 27.06/34.38/38.38 | 2F | `tests/unit/test_retention_walk_descriptors.py::test_open_dir_failed_validation_closes_descriptor[error0]` |
| ML | 20:37:41 | 29.71/34.70/38.45 | 1F | `tests/unit/test_retention_descriptor_review.py::test_layout_allocation_before_open` |
| MI | 20:37:53 | 27.66/34.10/38.19 | 1F | `tests/unit/test_retention_descriptor_review.py::test_in_repository_registration_failure_closes_descriptor` |
| MP | 20:38:03 | 24.71/33.25/37.84 | 2F | `tests/unit/test_retention_walk_descriptors.py::test_open_path_failed_descent_closes_intermediate_descriptors[a/missing]` |
| MC | 20:38:10 | 23.29/32.67/37.58 | 1P, 3F | `tests/unit/test_retention_walk_descriptors.py::test_conflict_dir_failed_descent_closes_descriptor[error0-mkdir]` |
| MH | 20:38:12 | 23.29/32.67/37.58 | 6F | `tests/unit/test_retention_descriptor_review.py::test_descent_close_failure_owns_new_child[False-path]` |
| MD | 20:38:17 | 29.27/33.75/37.93 | 2F | `tests/unit/test_retention_descriptor_review.py::test_walk_cleanup_interruption_closes_remaining_children[close]` |
| MR | 20:43:04 | 28.14/28.62/34.35 | 4P, 2F | `tests/unit/test_retention_descriptor_review_r2.py::test_failed_handoff_close_never_closes_a_reused_number[keyboard-path]` |
| MA | 20:43:21 | 31.14/29.14/34.39 | 2F | `tests/unit/test_retention_descriptor_review_r2.py::test_interrupt_after_registration_closes_child_once[walk]` |
| ME | 20:43:30 | 36.01/30.18/34.73 | 1F | `tests/unit/test_retention_descriptor_cleanup.py::test_open_dir_swapped_identity_survives_close_error` |
| MX | 20:43:38 | 44.11/32.11/35.36 | 1F | `tests/unit/test_retention_descriptor_cleanup.py::test_set_aside_cleanup_cancellation_overrides_handoff_oserror` |
| MAH | 20:43:49 | 51.73/34.16/36.05 | 1P, 3F | `tests/unit/test_retention_descriptor_review_r3.py::test_normal_read_close_failure_is_not_suppressed_by_handled_caller_exception[runtime-os]` |
| MSR | 20:50:10 | 40.95/37.35/37.08 | 2F | `tests/unit/test_retention_descriptor_review_r3.py::test_attempt_temp_size_root_close_preserves_walk_cancellation[os]` |
| MPO | 21:06:00 | 31.12/43.04/43.44 | 1F | `tests/unit/test_retention_descriptor_review_r3.py::test_interrupt_after_stack_pop_closes_popped_child` |
| MGF | 21:06:05 | 31.13/42.66/43.29 | 3F | `tests/unit/test_retention_descriptor_review_r3.py::test_record_complete_early_return_propagates_cleanup_cancellation[keyboard]` |
| MOC | 21:06:12 | 31.20/42.48/43.23 | 4F | `tests/unit/test_retention_descriptor_review_r4.py::test_owner_setup_failure_closes_acquired_file[memory-construction]` |
| MES | 21:06:17 | 31.34/42.32/43.17 | 1F | `tests/unit/test_retention_descriptor_prepared.py::test_empty_slots_are_never_closed_and_live_slots_close_once` |


| Own mutation / control | Start UTC | Load 1/5/15 | Result | Named test |
| --- | --- | --- | --- | --- |
| S1 | 21:04:01 | 48.88/51.37/46.13 | 4F | `tests/unit/test_retention_descriptor_prepared.py::test_owner_setup_failure_at_every_converted_site[memory-construction-retention_fs.still_archived.0]` |
| MEC-added | 21:14:00 | 35.81/42.93/43.53 | 1F | `tests/unit/test_retention_descriptor_review_r5.py::test_empty_and_live_slot_precedence` |
| S2 | 21:22:03 | 29.36/28.23/35.06 | 4F | `tests/unit/test_retention_descriptor_prepared.py::test_owner_setup_failure_at_every_converted_site[memory-construction-retention_fs._package_or_scope.1]` |
| S3 | 21:26:50 | 30.33/30.19/34.10 | 4F | `tests/unit/test_retention_descriptor_prepared.py::test_owner_setup_failure_at_every_converted_site[memory-construction-retention_fs.installed_files.0]` |
| S4 | 21:27:00 | 27.66/29.63/33.85 | 4F | `tests/unit/test_retention_descriptor_prepared.py::test_owner_setup_failure_at_every_converted_site[memory-construction-retention_fs.Reclaim._set_aside_fd.0]` |
| S5 | 21:30:20 | 25.50/28.74/32.68 | 4F | `tests/unit/test_retention_descriptor_prepared.py::test_owner_setup_failure_at_every_converted_site[memory-construction-retention_archive._Builder._walk.0]` |
| MEC-control | 21:30:35 | 25.26/28.54/32.54 | 2P | `tests/unit/test_retention_descriptor_prepared.py::test_empty_slots_are_never_closed_and_live_slots_close_once; tests/unit/test_retention_descriptor_ownership.py::test_cleanup_property_attempts_each_owned_close_once_and_preserves_precedence` |
| MDR | 21:31:19 | 23.61/27.47/31.92 | 1F | `tests/unit/test_retention_descriptor_review_r5.py::test_slot_handoffs_close_once_with_reused_number[keyboard-second-slot]` |
| MEC | 21:32:54 | 32.15/29.20/32.10 | 1F | `test_r5.py::test_empty_and_live_slot_precedence` |

Every kill is a named test's call-phase failure. S1–S5 preserve source line
numbers while moving acquisition into the context argument. MEC resets
pending only when it sees an empty slot: the inherited live-slot property and
simple empty-slot test survive it, whereas the mixed-slot property fails.

## PR-body lines to change

No required correction. After the hub records this verdict, line 31 can read:
“Review r5 approves a37ddd169 for the executed descriptor-ownership inputs;
completed CI remains the merge gate.” The generated precedence property in
this PR covers live slots; r5 supplies mixed empty/live coverage.

## Review artifacts and process

New portable test: `tests/unit/test_retention_descriptor_review_r5.py` (30
cases pass on each Python; the twelve hand-off cases are rerun after the final
probe hardening). The report and evidence are
`docs/reports/2026-10-30-walk-descriptors-r5.md` and
`docs/reports/2026-10-30-walk-descriptors-r5-evidence.json`. All remain
uncommitted under the review restriction and read-only shared Git metadata.

The initial independent 3.14 hand-off probe recorded 32P/1F because its
assertion compared descriptor numbers across distinct acquisitions. Reclaim
legitimately reuses numbers after successful closes. The failed-close
replacement itself survived. Corrected accounting tracks each acquisition
generation; the original run, source and failure remain in the evidence.
A later probe hardening records every close attempt before testing ownership,
so cleanup cannot hide an assertion by suppressing it. The twelve affected
hand-off cases pass again on both Pythons, and MDR (retry a failed slot close)
is killed by the reused-number case. No production behavior or expected
ownership/exception rule was changed.

The sandbox denied the requested Darwin scratch parent. Environments/logs use
/private/tmp/sf-r279r5; walked fixtures use this job's writable temporary
sf-r279r5 child, avoiding archive's literal tmp-component classification.
Both environments use the locked dev group and uv run --no-sync. Only the
requested scratch fixture daemons and stand-ins ran. No live store, caller
CLI/daemon, real provider, caller-repository fetch/merge/push or GitHub write
was used.
No sub-agents were used, following the lane rule. No source file was mutated
by a mutant. No commit was made, following the opening review prohibition;
new tests and reports are left in this worktree. Shared Git metadata was not
written. Both owned scratch roots were removed with the lane's guarded cleanup and
verified absent at 2026-10-30T22:25:54.316560+00:00. One owned read-only directory
was made writable first. The largest sampled allocated scratch size was
36.64 MiB, below 500 MB. All task-started test/benchmark processes
completed before cleanup; no process is kept running.

DONE
