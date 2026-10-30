VERDICT #279: REQUEST_CHANGES

Reviewed `b698146d045764d8906abb6917a0a5a9c48f423b` against `release/217` at `a093fb4b6`. All four review-r3 inputs now pass on both Pythons. One introduced ownership regression remains: the new `_cleanup` object is allocated and entered after acquiring its descriptor.

**New finding: P2-6 — prepare the owner before opening the descriptor.** `subfleet/retention_fs.py:1159`, with the same pattern at the other single-descriptor owners; helper construction/entry is at `:178–183`.

Input: make a scratch file, call `read_regular`, and inject `MemoryError` or `KeyboardInterrupt` at instruction 2 of `_cleanup.__init__` or `__enter__`. On Python 3.14.7 and 3.12.14, all four inputs propagate the identical injected object but leave the acquired descriptor open with zero close attempts. These are four actual failing assertions with marks bypassed on each Python. A separate ordinary constructor-failure probe, with no instruction tracing, also leaks at head and closes normally on release. On release, an injected first-body `fstat` failure closes the file for both exception types. Owner construction is a new fallible operation placed before protection; it is not the waived pop-marker window.

Fix: construct the owner and preallocate its resource slot before acquisition, enter it before opening, and bind the returned descriptor into that existing slot. Apply this ordering to every new owner following acquisition. A focused `read_regular` control using a preallocated slot passes all four cases on each Python. MOC removes that repair and fails all four again. This control validates the reproducers; it does not constitute a complete product patch or eliminate the separately documented native-return gap.

`tests/unit/test_retention_descriptor_review_r4.py` preserves the four cases as strict expected failures naming P2-6. No product source changed. The review restriction prohibits commits and the supplied shared Git metadata is read-only, so the test and review evidence remain uncommitted; no staging or commit was attempted.

**Review r3 findings rerun**

| Finding | Executed input | Status at head |
| --- | --- | --- |
| P2-4 | `read_regular` in handled RuntimeError/KeyboardInterrupt; release-and-raise close with EIO/new KeyboardInterrupt: four combinations | Fixed for these inputs: cleanup error/cancellation reaches the caller with identical object and ambient context. |
| P2-5 | `AttemptTemp.size` on `a/b/leaf`; third `_names` raises original KeyboardInterrupt; root close releases then raises EIO or a second KeyboardInterrupt | Fixed: one root owner/attempt and the original walk cancellation object survives both cases. |
| P3-5 | Four yields on `a/b/leaf`, then interrupt immediately after pop; also every committed bounded ownership offset on both Pythons | Fixed within the named bounds: each child closes once, borrowed root stays open, no retry. The remaining marker-to-call gap is L1 below. |
| P3-6 | `record_complete(root_fd, {}, "x/RECORD")` early False; walk-owned close releases then raises KeyboardInterrupt/SystemExit/custom BaseException | Fixed for all three cases: cancellation propagates; no unraisable-hook event. |

The r3 file passes 10/10 on each Python. Its AST differs from `0a433cfe1` only by removal of the strict expected-failure marks. Existing cleanup tests change only the two required explicit pending arguments; no expectation was weakened.

**Checks 1–9**

| Check | Verdict and executed evidence |
| --- | --- |
| 1. Explicit operation exit | Revised helper paths pass: all seven direct `_close_all` calls supply pending state. No ambient exception getter, imported getter, getter alias, or dynamic getter lookup remains in any of the five named modules. R3's four handled-caller cases and the precedence property pass on both Pythons. At full-module scope, legacy raw handle finally blocks still lack this rule; L3 executes one. |
| 2. `size` root | Pass for the requested two root-close failures: original cancellation identity survives, with one root acquisition/close attempt. P2-6 remains a separate owner-setup boundary. |
| 3. Pop bounds | Pass: 3.14 offsets 320,322,324,326,330,334,336,338,382; 3.12 offsets 278,280,282,284,288,290,292,334. Child closure is exactly once, root borrowed, original interruption retained. |
| 4. Consumers | Ten is correct: tree_bytes, record_complete, AttemptTemp.size, admin_objects, Retirement._final_check, both _late_admin walks, _Builder._walk, _walk_sizes, _sample_walk_once. All have explicit iterator closure inside root ownership once setup succeeds. A 20-case test on each Python forces an Exception or cancellation at iteration entry, then cancellation from iterator close and EIO from root close: all pass, closing each iterator once. R3 additionally covers real early-return cleanup. |
| 5. Uncovered windows | Executed all nine marker-to-call offsets, helper construction/entry/normal exit, repeated interruption, open-return binding, and release/nonrelease close models. P2-6 is new. The other executed counterexamples reproduce analogous release gaps (L1–L5). No duplicate production close was found. Native syscall completion and every untested Python instruction remain unproved. The r3 report truthfully identifies these bounds and possible unowned resources; its exclusions are not proof of closure. |
| 6. Every open/close | Read all 30 direct `os.open`/`open_dir` acquisition sites plus the three descriptor-returning helper calls. Read the seven `_close_all` call sites, callback close, and three direct normal/handoff closes. P2-6 leaves an acquired descriptor unowned. L1/L2/L4/L5 expose existing asynchronous boundaries; L3 exposes legacy cancellation replacement outside retention_fs. Named reused-number tests pass; no new duplicate-close path was found in the executed domain. |
| 7. Tests/mutants | All requested core runs pass on both Pythons; walk passes five times on 3.14 and once on 3.12. The Hypothesis property implements the stated first-error/pending/cancellation rule, with identity, context, cause and one attempt per descriptor; its 150 generated plus eight explicit examples pass on each Python. ME, MX and MAH each make that property fail. All seventeen named production mutants fail call-phase assertions in their named tests; MOC kills the new focused control. |
| 8. Text | Historical counts are retained and the r3 report's coverage exclusions are truthful. Broad ownership wording still needs the corrections below. The unchanged 33 walk plans, 33 repository plans and 400+400 differentials pass on each Python. |
| 9. CI | The single permitted snapshot contains ten pending shards, five per Python. Pass 0, fail 0, pending 10, skipping 0, cancelled 0. Neither known D-QT-FLAKE1 failure nor any other failure is identifiable from this snapshot. No job logs or second snapshot were read. |

**Listed differences — P3, predating this PR; they do not independently hold the merge**

| ID / site | Executed input and result | Fix / scope |
| --- | --- | --- |
| L1 / P3-5 remainder, retention_fs.py:235 | Head: inject at 340,350,370,372,374 on 3.14; 294,304,324,326 on 3.12. Each leaves the popped child open, closes the other child once, preserves the injected cancellation, and never retries. Equivalent release pop-return injection also leaks on both Pythons. | Native ownership transfer or deferred/masked asynchronous cancellation across mark-and-close; retain the documented bound until then. The hub expressly permits a declared pure-Python window. |
| L2 / retention_fs.py:186 | Interrupt instruction 2 at normal `_cleanup.__exit__` entry: no close, one leaked file descriptor. Interrupt the release normal-finally close setup: same leak on each Python. | Protect cleanup dispatch with native ownership or deferred cancellation; this is an existing exit-setup boundary. |
| L3 / retention_git.py:698 | `admin_objects` on a scratch ref: read raises original KeyboardInterrupt, handle close releases then raises EIO. Head and release both propagate the EIO with KeyboardInterrupt only in context. | Replace the plain handle finally with the explicit-exit precedence mechanism, prepared safely before acquisition. Similar retained raw handle closes exist in survey/archive; no universal full-module precedence claim is justified. |
| L4 / retention_fs.py:197 | Body raises KeyboardInterrupt; inject a second cancellation between drain attempts (FOR_ITER 54/50). Head replaces the original and leaves two of three descriptors open. Release walk cleanup likewise aborts after its first owned close and leaves another child open. | Defer repeated cancellation during cleanup, or move ownership/drain into a native boundary; preserve the report's repeated-interruption qualification. |
| L5 / retention_fs.py:160 | Interrupt the STORE_FAST fd immediately after native open returns. Head and release both leave one descriptor open with no close on each Python. | Native acquire-and-register ownership or deferred cancellation across acquisition. This differs from avoidable owner allocation after an already-bound descriptor in P2-6. |

Synthetic native-close models release or retain the descriptor before raising EINTR. Production makes exactly one attempt in both models. The test releases the deliberately retained number afterward. No signal was delivered to a real native close, so syscall completion remains explicitly unproved. The pre-existing RegenerableWalk constructor-before-root-owner boundaries in builder/survey were read but not separately injected.

**Text and PR-body corrections**

- Main report, decision 1 and the corresponding r2 PR-body replacement: replace the universal before-registration claim with: “The exercised listing and registration failures close a child once; post-registration cleanup leaves it to the stack. The documented open-return and asynchronous instruction boundaries remain outside this guarantee.”
- Main descriptor-audit table and live PR body line 9: replace “Validation, allocation, registration ... protected” / “other 21 opens have immediate cleanup scopes” with: “The exercised validation, container-allocation, registration and failed-descent cases are protected. The other 21 direct-open sites have cleanup contexts after acquisition; review r4 found that constructing or entering those owners can fail before ownership begins (P2-6).” Keep the correct handoff and no-retry description.
- Live PR body line 10: “Once `_close_all` is entered and its iteration is not asynchronously interrupted, it attempts each remaining owned close once. `walk` preserves the borrowed root. The named single-failure differentials retain release exception types and TreeError codes for their executed inputs.”
- Live PR body line 11: “The `_cleanup`/`_close_all` paths receive the operation’s explicit exit exception, including the `AttemptTemp.size` root close. In those paths, handled caller exceptions no longer suppress cleanup failures or cancellation.” Line 12 can retain the ten-consumer count, qualified by successful owner setup and the documented exit-instruction boundary.
- Live PR body line 14, retain the precedence rule but replace its final sentence with: “This rule is verified for entered cleanup owners and the exercised inputs. Review r4 additionally found P2-6: owner construction/entry after acquisition can leak the descriptor. The documented pop, exit, repeated-interruption and native boundaries remain qualified.”
- Add to the r3 report and r1/r2 follow-ups: “Review r4 reran all ten r3 cases successfully on both Pythons, and executed the declared instruction windows. It requested changes for the new P2-6 owner-setup leak. Historical result counts remain unchanged.”
- The r1 mutant report's historical named-mutant claims reproduce; no result-count correction is needed. Delivery/bundle statements describe their original rounds, not files newly present at this checkout.

**Holds and results**

Times below are 2026-10-30 UTC. Loads are 1/5/15-minute averages recorded after shared-lock admission with `os.getloadavg` (wrapper sysctl access is denied). P/F/X mean passed/failed/strict expected failure. One serial pytest per hold; holds used the shared wrapper or the explicit thirty-minute fallback identified below, with at most two of this task's pytests active. The unadmitted MR batch was stopped through its native tool session; its seven remaining mutants were requeued individually. These eight admissions missed the first-hour target; their actual starts are recorded below. A task-scoped process lookup failed before returning any process data; native tool interruption ended both cancelled sessions, with no PID-targeted signals sent. Core labels have a 314/312 Python suffix: remove it to get `tests/unit/test_retention_descriptor_<name>.py`. Walk labels name `test_retention_walk_descriptors.py`; the trailing number identifies the repetition.

| Hold / file | Python | Start UTC | Load 1/5/15 | Result |
| --- | --- | --- | --- | --- |
| review314 | 3.14.7 | 16:49:48 | 21.09/19.91/21.21 | 47P |
| review_r2314 | 3.14.7 | 16:49:51 | 21.56/20.03/21.24 | 8P |
| review_r3314 | 3.14.7 | 16:49:54 | 21.28/19.99/21.22 | 10P |
| cleanup314 | 3.14.7 | 16:49:57 | 21.28/19.99/21.22 | 48P |
| ownership314 | 3.14.7 | 16:50:00 | 21.18/19.99/21.22 | 16P |
| walk314-1 | 3.14.7 | 16:50:03 | 20.68/19.91/21.18 | 20P |
| walk314-2 | 3.14.7 | 16:50:07 | 20.68/19.91/21.18 | 20P |
| walk314-3 | 3.14.7 | 16:50:12 | 20.95/19.98/21.20 | 20P |
| walk314-4 | 3.14.7 | 16:58:44 | 23.81/19.41/19.81 | 20P |
| walk314-5 | 3.14.7 | 16:58:48 | 23.81/19.41/19.81 | 20P |
| review312 | 3.12.14 | 16:58:54 | 23.08/19.39/19.80 | 47P |
| review_r2312 | 3.12.14 | 16:58:57 | 23.08/19.39/19.80 | 8P |
| review_r3312 | 3.12.14 | 16:59:00 | 22.75/19.39/19.79 | 10P |
| cleanup312 | 3.12.14 | 16:59:03 | 22.75/19.39/19.79 | 48P |
| ownership312 | 3.12.14 | 16:59:06 | 23.17/19.53/19.84 | 15P |
| walk312-1 | 3.12.14 | 16:59:10 | 22.84/19.52/19.84 | 20P |

Archive labels name `tests/unit/test_retention_archive.py`; sweep labels name `tests/unit/test_retention_sweep_race.py`. Other regression/guard labels expand to `tests/unit/test_<label>.py`; remove `fake-` for files under `tests/fake/`. Archive A1/A2 partition functions before line 858 and the remainder; both deselect `real_lsof`. Sweep examples, all 15 explicit examples and all 40 generated examples run separately.

| Hold / file | Python | Start UTC | Load 1/5/15 | Result |
| --- | --- | --- | --- | --- |
| archive1 | 3.14.7 | 16:39:34 | 17.02/21.30/23.46 | 33P, 5F |
| archive2 | 3.14.7 | 16:41:51 | 19.87/20.60/22.83 | 70P |
| sweep-examples | 3.14.7 | 16:44:32 | 14.28/18.33/21.56 | 12P |
| sweep-explicit | 3.14.7 | 16:45:15 | 17.50/18.69/21.53 | 1P |
| sweep-generate | 3.14.7 | 16:46:11 | 15.76/17.95/21.07 | 1P |
| retention_turns | 3.14.7 | 16:49:42 | 21.88/20.04/21.26 | 7P |
| retention_readback | 3.14.7 | 16:59:33 | 19.33/19.00/19.64 | 37P |
| retention_regenerable | 3.14.7 | 17:02:00 | 20.22/19.00/19.51 | 26P |
| retention_regenerable_properties | 3.14.7 | 17:03:55 | 25.36/22.33/20.81 | 1P |
| timers_retention | 3.14.7 | 17:11:21 | 44.75/23.78/21.11 | 20P |
| attempt_tmpdir_guardian | 3.14.7 | 17:12:04 | 48.97/27.64/22.65 | 2P |
| attempt_tmpdir_leaks | 3.14.7 | 17:12:39 | 48.53/30.21/23.81 | 197P |
| attempt_tmpdir_mutations | 3.14.7 | 17:17:06 | 69.33/50.74/34.43 | 85P |
| attempt_tmpdir_retention | 3.14.7 | 17:17:52 | 61.54/51.14/35.40 | 6P |
| attempt_tmpdir_sandboxes | 3.14.7 | 17:18:17 | 56.88/50.85/35.75 | 12P |
| test_module_names | 3.14.7 | 17:18:41 | 51.24/50.00/35.88 | 1P |
| invariants_index | 3.14.7 | 17:19:19 | 78.66/57.11/39.13 | 25P |
| fake-attempt_tmpdir_daemon | 3.14.7 | 17:19:45 | 81.24/59.51/40.52 | 24P |
| fake-attempt_tmpdir_leaks_daemon | 3.14.7 | 17:21:13 | 74.87/63.53/43.91 | 8P |
| fake-state_contract | 3.14.7 | 17:21:51 | 81.33/66.28/45.80 | 37P |
| archive1-darwin | 3.14.7 | 16:55:26 | 17.60/17.74/19.63 | 38P |

The first A1 admission preceded the runner path correction and produced the five known scratch-path classification failures. The exact same 38-case partition passes as archive1-darwin. No product or test assertion was changed to obtain that result.

Additional diagnostic and control holds: windows/owner/extra-window/boundary labels use scratch `test_r4_windows.py`; consumers use `test_r4_consumers.py`; parity uses `test_parity.py`. R4-marked names the worktree regression file `tests/unit/test_retention_descriptor_review_r4.py` (left uncommitted here). Property-mutant rows select the ownership property. Scratch sources are embedded in the JSON.

| Hold / file | Python | Start UTC | Load 1/5/15 | Result |
| --- | --- | --- | --- | --- |
| windows314 | 3.14.7 | 16:44:56 | 17.48/18.78/21.63 | 2P, 14X |
| windows312 | 3.12.14 | 16:45:02 | 17.60/18.78/21.61 | 2P, 13X |
| owner-control314 | 3.14.7 | 16:45:06 | 17.15/18.67/21.55 | 4P |
| owner-control312 | 3.12.14 | 16:51:29 | 14.66/18.16/20.39 | 4P |
| property-ME | 3.14.7 | 16:51:35 | 14.37/18.04/20.33 | 1F |
| property-MX | 3.14.7 | 16:59:58 | 18.99/18.96/19.61 | 1F |
| property-MAH | 3.14.7 | 17:00:01 | 19.23/19.01/19.63 | 1F |
| consumers314 | 3.14.7 | 16:57:36 | 17.70/17.12/19.08 | 20P |
| consumers312 | 3.12.14 | 16:57:42 | 17.64/17.12/19.07 | 20P |
| owner-red314 | 3.14.7 | 16:49:04 | 23.00/19.90/21.26 | 4F |
| extra-windows314 | 3.14.7 | 16:49:10 | 23.80/20.11/21.33 | 2P, 1X |
| owner-red312 | 3.12.14 | 16:49:17 | 24.46/20.31/21.39 | 4F |
| extra-windows312 | 3.12.14 | 16:49:38 | 22.57/20.15/21.31 | 2P, 1X |
| r4-marked314 | 3.14.7 | 16:58:27 | 25.08/19.36/19.80 | 4X |
| r4-marked312 | 3.12.14 | 16:58:39 | 23.62/19.30/19.77 | 4X |
| boundary-baselines314 | 3.14.7 | 17:10:43 | 24.04/17.85/19.00 | 1P, 4X |
| boundary-baselines312 | 3.12.14 | 17:11:02 | 29.89/19.60/19.60 | 1P, 4X |
| parity314 | 3.14.7 | 16:58:00 | 26.79/19.12/19.73 | 4P |
| parity312 | 3.12.14 | 16:59:16 | 23.17/19.64/19.88 | 4P |

**Mutant table**

All mutant and property-mutant holds use Python 3.14.7.

Outside-lock runs after the thirty-minute fallback: MX, MR.

| Mutant | Start UTC | Load 1/5/15 | P/F | Named killing test |
| --- | --- | --- | --- | --- |
| M1 | 16:40:36 | 16.86/20.50/23.01 | 16/4 | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction[1]` |
| M2 | 16:40:44 | 15.98/20.19/22.87 | 9/11 | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction[1]` |
| MO | 16:40:51 | 17.74/20.49/22.96 | 0/2 | `tests/unit/test_retention_walk_descriptors.py::test_open_dir_failed_validation_closes_descriptor[error0]` |
| ML | 16:54:28 | 18.29/17.84/19.81 | 0/1 | `tests/unit/test_retention_descriptor_review.py::test_layout_allocation_before_open` |
| MI | 17:15:29 | 60.79/40.75/29.23 | 0/1 | `tests/unit/test_retention_descriptor_review.py::test_in_repository_registration_failure_closes_descriptor` |
| MP | 17:16:34 | 77.40/50.09/33.62 | 0/2 | `tests/unit/test_retention_walk_descriptors.py::test_open_path_failed_descent_closes_intermediate_descriptors[a/missing]` |
| MC | 17:17:14 | 67.03/50.88/34.67 | 1/3 | `tests/unit/test_retention_walk_descriptors.py::test_conflict_dir_failed_descent_closes_descriptor[error0-mkdir]` |
| MH | 17:17:39 | 60.52/50.61/35.03 | 0/6 | `tests/unit/test_retention_descriptor_review.py::test_descent_close_failure_owns_new_child[True-set-aside]` |
| MD | 17:18:17 | 56.88/50.85/35.75 | 0/3 | `tests/unit/test_retention_descriptor_review.py::test_walk_cleanup_interruption_closes_remaining_children[close]` |
| MR | 17:53:00 | 26.21/52.01/70.59 | 0/2 | `tests/unit/test_retention_descriptor_review_r2.py::test_failed_handoff_close_never_closes_a_reused_number[keyboard-path]` |
| MA | 17:59:33 | 31.07/40.77/58.38 | 0/2 | `tests/unit/test_retention_descriptor_review_r2.py::test_interrupt_after_registration_closes_child_once[walk]` |
| ME | 17:59:05 | 37.78/42.81/59.61 | 0/1 | `tests/unit/test_retention_descriptor_cleanup.py::test_open_dir_swapped_identity_survives_close_error` |
| MX | 18:24:59 | 38.02/39.98/44.42 | 0/1 | `tests/unit/test_retention_descriptor_cleanup.py::test_set_aside_cleanup_cancellation_overrides_handoff_oserror` |
| MAH | 18:04:35 | 53.21/48.40/56.64 | 1/3 | `tests/unit/test_retention_descriptor_review_r3.py::test_normal_read_close_failure_is_not_suppressed_by_handled_caller_exception[keyboard-keyboard]` |
| MSR | 18:15:08 | 45.78/43.13/49.55 | 0/2 | `tests/unit/test_retention_descriptor_review_r3.py::test_attempt_temp_size_root_close_preserves_walk_cancellation[os]` |
| MPO | 18:17:12 | 39.45/41.56/48.04 | 0/1 | `tests/unit/test_retention_descriptor_review_r3.py::test_interrupt_after_stack_pop_closes_popped_child` |
| MGF | 18:04:01 | 52.52/47.67/56.73 | 0/3 | `tests/unit/test_retention_descriptor_review_r3.py::test_record_complete_early_return_propagates_cleanup_cancellation[keyboard]` |
| MOC | 16:51:32 | 14.66/18.16/20.39 | 0/4 | `test_r4_windows.py::test_owner_setup_failure_closes_acquired_file[memory-construction]` |

M1/MI/MA retain their intended mutations with explicit-argument/indentation adaptations; MD accepts pending and the close callback while still aborting the drain. MOC removes only the prepared-owner control and restores head read_regular. ME/MX/MAH additionally kill the ownership property, as recorded above. Mutation failures are intentional call-phase assertions, not collection or plugin failures.

Completed **74 holds**. Longest pytest invocation: **352.28s**; peak runner RSS: **119.62 MiB**. The initial nine batch drivers started by 16:53:16 UTC, within 18 minutes. The final eight mutant admissions were late after shared-lock waits. Outside-lock runs are identified above, and cancelled waiter/queue events are retained in the JSON. Latest hold start: **18:24:59 UTC**; 8 admissions followed the first hour because of shared-lock waits; those starts are shown explicitly above. Largest recorded combined scratch sample: **72.90 MiB**, below 500 MB.

The requested maintained set contains 758 distinct passing cases on Python 3.14 and 148 on Python 3.12, excluding repetitions, diagnostics and new strict expected failures. The earlier 793-case fix-r3 total also included the 35 lifecycle cases outside this review’s run list.

Not run: the full suite; archive `test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`; live frontend tests (the lane person-only check leaves them to CI); broader regression files on Python 3.12; the separately tracked lifecycle model; real native-close signal delivery; or CI job logs. Every requested non-live file/selection completed.

Complete sources, arguments, per-case outcomes, logs, snapshot, and hashes are in `2026-10-30-walk-descriptors-r4-evidence.json`. Both scratch roots were removed with the rules file’s guarded cleanup after restoring write permission on two task-owned read-only fixture directories. All task-started background processes have ended. No caller checkout, live store, real CLI or real provider was accessed. No review change was committed, pushed or published; the reviewed repository was not fetched or merged. Fixture Git operations stayed in scratch repositories.

DONE
