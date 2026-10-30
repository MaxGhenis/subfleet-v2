# D-WALK-FD1: fix round r4 for P2-6

Base: `9b1a9d462c26e0cb4bd6376aed7d973de5a1c9f4` (review r4's test and
reports on fix r3). The first recorded task operation was before 18:48:21 UTC
on 2026-10-30; the job workspace was created at 18:47:32 UTC. The first hold
was admitted at 18:49:18 UTC. Fix commit: `4a1b56ddc`; test/mutant commit:
`439dd394d`. The final delivery head is printed with bundle verification.

## Decisions built

1. P2-6: every descriptor acquisition formerly followed by `_cleanup` now
   follows an entered owner with a preallocated `[None]` slot. The acquisition
   result goes directly into `owner.resources[0]`; only then is a local alias
   bound. No append or resource container allocation occurs while filling a
   slot. `_close_all` skips `None`. One small class and one spelling serve all
   five modules; no generator context manager was added.
2. Two-descriptor sites retain two nested one-slot owners. For example,
   `_marker` prepares and enters the directory slot before `open_dir`, then
   prepares and enters the file slot before `os.open`. File cleanup still
   precedes directory cleanup. Separate scopes also preserve existing exception
   precedence when an inner close fails. The borrowed root is never put in a slot.
3. No L1–L5 repair was attempted. In particular, native open-return-to-slot-store
   remains L5; this code does not make native acquisition and Python storage
   atomic. Precedence rules and handoff no-retry behavior are unchanged.
4. Only the strict xfail decorator (four expanded cases) was removed from
   review r4's file; an AST comparison verifies no other change. The new
   `test_retention_descriptor_prepared.py` has 140 cases (all 35 sites ×
   construction/entry × MemoryError/KeyboardInterrupt), the AST argument guard,
   an empty-slot/one-close test and a real file-before-directory close test.
   Ancestor acquisitions must close exactly once and the identical injected
   exception must propagate. Every converted site is behaviorally injected.
5. Production mutants MOC and MES respectively restore acquisition before
   read_regular owner setup and remove the empty-slot skip. All seventeen
   existing mutants are rerun against their named tests. MSR is retargeted to
   locate the new slot owner and restore just size's plain root close. MD now
   retains the empty-slot skip while still aborting the drain. The other
   seventeen mutant definitions, including MOC's equivalent source reversion,
   retain their intended behavior; no configuration error counts as a kill.
6. The main/r1/r2/r3 reports retain historical counts and now distinguish
   executed ownership guarantees from review r4's listed differences. Current
   results, exact inputs, sources, logs and hashes accompany this report.

No hub decision required changing. The scratch location and isolated Git
metadata are sandbox accommodations, detailed below.

## Converted acquisition sites

All rows use the same entered `[None]` owner and direct slot store. Each row
is one owner; nested pairs provide two preallocated slots. Line numbers refer
to this fixed source, while acquisition text names the original expression.

| Module | Function | Resource | Acquisition |
| --- | --- | --- | --- |
| `retention_fs:122` | `still_archived` | `fd` | `os.open(name, O_FILE, dir_fd=parent_fd)` |
| `retention_fs:286` | `tree_bytes` | `fd` | `os.open(path, O_DIR)` |
| `retention_fs:362` | `clone_or_copy` | `out` | `os.open(name, os.O_WRONLY \| os.O_CREAT \| os.O_EXCL \| os.O_NOFOLLOW \| os.O_CLOEXEC, 384, dir_fd=dst_dir_fd)` |
| `retention_fs:527` | `_package_or_scope` | `fd` | `open_dir(name, dir_fd=parent_fd, expect=st)` |
| `retention_fs:539` | `_package_or_scope` | `inner` | `open_dir(child, dir_fd=fd, expect=cst)` |
| `retention_fs:613` | `_layout` | `fd` | `open_dir(name, dir_fd=parent_fd, expect=st)` |
| `retention_fs:654` | `bytecode` | `fd` | `os.open(name, O_FILE, dir_fd=parent_fd)` |
| `retention_fs:684` | `installed_files` | `sp` | `_open_path(venv_fd, base)` |
| `retention_fs:693` | `installed_files` | `fd` | `open_dir(dist, dir_fd=sp)` |
| `retention_fs:740` | `record_complete` | `fd` | `os.open(name, O_FILE, dir_fd=parent)` |
| `retention_fs:760` | `_site_packages` | `fd` | `open_dir(lib, dir_fd=venv_fd)` |
| `retention_fs:771` | `_site_packages` | `inner` | `open_dir(name, dir_fd=fd)` |
| `retention_fs:968` | `RegenerableWalk._rule_for` | `fd` | `open_dir(name, dir_fd=parent, expect=st)` |
| `retention_fs:982` | `RegenerableWalk._start` | `fd` | `open_dir(name, dir_fd=parent, expect=st)` |
| `retention_fs:1046` | `RegenerableWalk._whole` | `fd` | `_open_path(self.root_fd, key)` |
| `retention_fs:1108` | `_small` | `fd` | `os.open(name, O_FILE, dir_fd=dir_fd)` |
| `retention_fs:1132` | `_marker` | `fd` | `open_dir(directory, dir_fd=parent_fd, expect=st)` |
| `retention_fs:1135` | `_marker` | `handle` | `os.open(name, O_FILE, dir_fd=fd)` |
| `retention_fs:1157` | `sync_path` | `fd` | `os.open(path, os.O_RDONLY \| os.O_NOFOLLOW \| os.O_CLOEXEC \| (os.O_DIRECTORY if path.is_dir() else 0))` |
| `retention_fs:1170` | `write_atomic` | `fd` | `os.open(temporary, os.O_WRONLY \| os.O_CREAT \| os.O_EXCL \| os.O_NOFOLLOW \| os.O_CLOEXEC, mode)` |
| `retention_fs:1182` | `read_regular` | `fd` | `os.open(path, O_FILE)` |
| `retention_fs:1201` | `remove_own_tree` | `fd` | `os.open(path, O_DIR)` |
| `retention_fs:1216` | `_remove_children` | `child` | `open_dir(name, dir_fd=fd, expect=st)` |
| `retention_fs:1261` | `Reclaim.run` | `fd` | `open_dir(root, expect=st)` |
| `retention_fs:1299` | `Reclaim._one` | `child` | `open_dir(name, dir_fd=fd, expect=st)` |
| `retention_fs:1366` | `Reclaim._set_aside_fd` | `target` | `self._conflict_dir(parent)` |
| `retention_fs:1387` | `Reclaim._set_aside` | `fd` | `open_dir(parent)` |
| `attempt_tmp:291` | `AttemptTemp.size` | `fd` | `rfs.open_dir(where)` |
| `retention_git:683` | `admin_objects` | `fd` | `os.open(admin, rfs.O_DIR)` |
| `retention_survey:73` | `_walk_sizes` | `fd` | `rfs.open_dir(path)` |
| `retention_survey:500` | `_sample_walk_once` | `fd` | `rfs.open_dir(path)` |
| `retention_archive:831` | `Retirement._final_check` | `fd` | `rfs.open_dir(path)` |
| `retention_archive:1130` | `Retirement._late_admin` | `fd` | `rfs.open_dir(admin)` |
| `retention_archive:1149` | `Retirement._late_admin` | `fd` | `rfs.open_dir(admin)` |
| `retention_archive:1683` | `_Builder._walk` | `fd` | `rfs.open_dir(path)` |

There are 27 owners in retention_fs, one in attempt_tmp, one in retention_git,
two in retention_survey and four in retention_archive: **35**. The three
non-open_dir descriptor-helper calls are `_open_path` in `installed_files`,
`_open_path` in `RegenerableWalk._whole`, and `_conflict_dir` in
`Reclaim._set_aside_fd`; these use slots too. Both `_late_admin` root acquisitions
are converted. All direct opens and all open_dir calls followed by these
owners are converted.

## Sites deliberately left alone

- `retention_fs.open_dir`: its validation handler and ownership-return contract
  are retained; it never constructs `_cleanup`. Its native-open binding window
  is L5. `walk`'s child acquisition/stack registration and pop handoff,
  `RegenerableWalk._in_repository`'s registration, `_open_path`'s descent, and
  `Reclaim._conflict_dir`'s descent retain their existing handlers and no-retry
  behavior; none builds a new cleanup owner after acquisition.
- `walk`'s lazy stack owner and `_in_repository`'s mutable-list owner are entered
  when the collections are empty, before any owned child is acquired. They are
  the two named AST collection exceptions, with reasons in the test.
- The ten iterator owners remain over unstarted generators: tree_bytes,
  record_complete, size, admin_objects, _final_check, both _late_admin walks,
  _Builder._walk, _walk_sizes and _sample_walk_once. No descriptor is acquired
  by constructing those generators. Their named AST exception explains that
  iteration starts only inside the owner; the separate root already has a slot.
- Raw legacy acquisitions/closes in attempt_tmp (`_intend`, `_marker` twice,
  `_remove` twice, `_own`, module `_empty` twice); retention_git
  (`admin_objects` handle and `create_bundle`); retention_survey (`_throughput`,
  `_sample_walk_once`'s two handles); and retention_archive (`_read_back`,
  `_Copies.__enter__`, `_Copies.holds`, `Retirement.lock`, `_Builder._flush`,
  `_Builder._run`, `_Builder._digest`, `_Builder._nested_gitfile`,
  `_Builder._file`, `_Builder._verify_omissions` root and handle,
  `_Builder._git`, `_Builder._verify_store`, `_Builder._drop_unreferenced`,
  `_open_parent`, `check_archive` root and handle, `_restore_tree` src/parent
  and output) were not converted by this PR and remain outside P2-6. Their
  raw-handle precedence behavior is explicitly out of scope under L3.
- Moving the root owners also places existing RegenerableWalk construction in
  `_Builder._walk` and `_sample_walk_once` inside root ownership. This follows
  the required owner-before-acquisition order; those separate constructor
  boundaries were read but not independently injected.

## Listed differences retained from review r4

These are the review's executed inputs/results on head and release, not new
runs claimed for this fix. The reviewed sources and raw evidence remain in
`2026-10-30-walk-descriptors-r4-evidence.json`. Offsets refer to that reviewed
source and interpreter. L1–L5 still stand; the claim extends only as far as the
tests. Native-close completion is unproved.

| ID | Review r4 executed input | Observed result |
| --- | --- | --- |
| L1 | Pop marker-to-call injection: 3.14 offsets `340,350,370,372,374`; 3.12 `294,304,324,326`. Release pop-return injection on both Pythons. | Popped child leaks; other child closes once; cancellation survives; no retry. |
| L2 | Normal `_cleanup.__exit__`, instruction 2; corresponding release finally-close setup, both Pythons. | One descriptor leaks with no close attempt. |
| L3 | Scratch `admin_objects`: read raises KeyboardInterrupt; raw handle close releases then raises EIO. | Head and release propagate EIO, keeping cancellation only as context. Similar raw survey/archive handles remain. |
| L4 | Body cancellation, then another cancellation between drain attempts at FOR_ITER `54/50` (3.14/3.12). Release walk cleanup likewise interrupted after its first close. | Original replaced; two descriptors remain open at head; release also aborts its drain. |
| L5 | Interrupt STORE_FAST immediately after native open returns, on both Pythons. | Head and release leak one descriptor with no close. The new direct STORE_SUBSCR still has a native-return-to-store window. |

Review r4 also used two synthetic native-close models: release then EINTR and
retain then EINTR. Production makes one attempt in both; the test releases any
synthetically retained number afterward. Actual native-close signal delivery
was not exercised. The bounded pop tests continue to cover their named
pre-marker and post-call offsets, with L1 outside that guarantee.

## Red, green, core and parity runs

Times below are **2026-10-30 UTC**, loads are 1/5/15-minute averages captured
inside admission using os.getloadavg (the wrapper's sysctl read is sandbox
denied). P/F mean passed/failed. Every invocation is one serial pytest under
one hold. Red uses --runxfail on unchanged starting source; green uses the fix
without the marks. The prepared matrix uses real scratch descriptors.

| Run / Python | Start UTC | Load 1/5/15 | P/F | Seconds |
| --- | --- | --- | --- | ---: |
| red314 / 3.14.7 | 18:49:18 | 46.71/49.79/50.48 | 4F | 8.73 |
| red312 / 3.12.14 | 18:49:36 | 49.11/50.05/50.55 | 4F | 11.01 |
| green314 / 3.14.7 | 18:51:16 | 41.96/47.00/49.29 | 4P | 11.02 |
| review314 / 3.14.7 | 18:51:33 | 42.74/46.93/49.22 | 47P | 13.28 |
| review_r2314 / 3.14.7 | 18:51:53 | 36.42/45.22/48.55 | 8P | 7.20 |
| prepared314 / 3.14.7 | 18:55:17 | 81.40/56.90/52.28 | 143P | 42.71 |
| prepared312 / 3.12.14 | 18:56:06 | 78.66/60.14/53.76 | 143P | 31.76 |
| review_r3314 / 3.14.7 | 19:06:57 | 48.19/58.98/57.25 | 10P | 8.44 |
| cleanup314 / 3.14.7 | 19:07:10 | 44.69/57.66/56.80 | 48P | 9.63 |
| ownership314 / 3.14.7 | 19:07:22 | 42.48/56.75/56.49 | 16P | 11.35 |
| walk314-1 / 3.14.7 | 19:07:39 | 46.79/56.95/56.56 | 20P | 12.91 |
| walk314-2 / 3.14.7 | 19:08:30 | 44.82/54.73/55.76 | 20P | 18.40 |
| walk314-3 / 3.14.7 | 19:08:51 | 44.32/54.10/55.52 | 20P | 12.85 |
| walk314-4 / 3.14.7 | 19:09:07 | 46.93/54.19/55.52 | 20P | 9.89 |
| walk314-5 / 3.14.7 | 19:09:19 | 46.24/53.82/55.38 | 20P | 10.20 |
| parity314 / 3.14.7 | 19:09:33 | 41.45/52.41/54.84 | 4P | 29.93 |
| green312 / 3.12.14 | 19:10:07 | 44.32/51.92/54.57 | 4P | 11.72 |
| review312 / 3.12.14 | 19:26:30 | 42.33/40.78/45.39 | 47P | 16.11 |
| review_r2312 / 3.12.14 | 19:26:50 | 47.55/42.08/45.75 | 8P | 8.09 |
| review_r3312 / 3.12.14 | 19:27:02 | 47.88/42.33/45.79 | 10P | 9.00 |
| cleanup312 / 3.12.14 | 19:30:01 | 70.48/51.96/48.97 | 48P | 11.63 |
| ownership312 / 3.12.14 | 19:30:16 | 73.60/53.55/49.59 | 15P | 10.58 |
| walk312-1 / 3.12.14 | 19:30:31 | 70.88/53.93/49.79 | 20P | 10.65 |
| parity312 / 3.12.14 | 19:33:52 | 43.68/51.12/49.70 | 4P | 39.70 |

Core run names expand under `tests/unit/`: review=`test_retention_descriptor_review.py`,
review_r2/review_r3=its numbered files, green/red=its r4 file,
cleanup=`test_retention_descriptor_cleanup.py`, ownership=`test_retention_descriptor_ownership.py`,
walk=`test_retention_walk_descriptors.py`, prepared=`test_retention_descriptor_prepared.py`.
The ownership count differs by Python because the tested instruction count differs.

Parity uses review r2's unchanged **33 walk plans, 33 repository plans, 400 walk
differentials and 400 repository differentials per Python**. Sources came
byte-for-byte from review r4's embedded copies of that harness; only its worktree
import path was relocated. Plans/expectations were not changed. The inherited
GeneratorExit/close-OSError accounting remains. P2-6 adds owner-setup-failure
behavior outside those parity inputs: setup now fails before acquisition.

## Regression files and guards

All use Python 3.14. Readback is split at source line 176 into two holds to
keep each invocation comfortably below ten minutes. Archive partitions at source line 857 retain all 108
non-live cases and deselect `test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`.
Sweep separates its 12 examples, all 15 explicit property examples and all 40
generated examples without changing assertions or bounds. The property is one
distinct maintained case despite its two phase holds. All fixture trees are
created in task-owned scratch; failed_keep is redirected there before pruning.

| Run / Python | Start UTC | Load 1/5/15 | P/F | Seconds |
| --- | --- | --- | --- | ---: |
| archive1 / 3.14.7 | 18:55:59 | 81.08/59.99/53.63 | 38P | 276.75 |
| archive2 / 3.14.7 | 19:00:51 | 53.94/58.11/54.83 | 70P | 321.07 |
| turns / 3.14.7 | 19:06:26 | 50.11/60.34/57.64 | 7P | 28.59 |
| sweep-examples / 3.14.7 | 19:06:58 | 48.19/58.98/57.25 | 12P | 82.18 |
| sweep-explicit / 3.14.7 | 19:10:24 | 44.03/51.48/54.36 | 1P | 143.59 |
| sweep-generate / 3.14.7 | 19:12:51 | 45.10/50.79/53.75 | 1P | 313.39 |
| retention_readback / 3.14.7 | 19:18:08 | 35.69/41.62/48.56 | 24P | 165.17 |
| retention_regenerable / 3.14.7 | 19:20:59 | 48.50/44.16/48.32 | 26P | 121.02 |
| retention_regenerable_properties / 3.14.7 | 19:23:05 | 41.26/44.38/47.91 | 1P | 113.39 |
| timers_retention / 3.14.7 | 19:25:01 | 32.29/39.75/45.61 | 20P | 11.63 |
| attempt_tmpdir_guardian / 3.14.7 | 19:25:15 | 33.34/39.60/45.45 | 2P | 5.75 |
| attempt_tmpdir_leaks / 3.14.7 | 19:27:15 | 48.13/42.67/45.85 | 197P | 118.38 |
| retention_readback2 / 3.14.7 | 19:31:03 | 70.96/55.55/50.52 | 13P | 120.79 |
| attempt_tmpdir_lifecycle / 3.14.7 | 19:33:38 | 46.77/52.06/50.00 | 35P | 10.18 |
| attempt_tmpdir_mutations / 3.14.7 | 19:35:45 | 63.36/56.14/51.87 | 85P | 19.04 |
| attempt_tmpdir_retention / 3.14.7 | 19:36:07 | 68.71/57.66/52.50 | 6P | 7.95 |
| attempt_tmpdir_sandboxes / 3.14.7 | 19:36:19 | 65.91/57.44/52.48 | 12P | 11.50 |
| test_module_names / 3.14.7 | 19:36:39 | 58.72/56.37/52.21 | 1P | 10.33 |
| invariants_index / 3.14.7 | 19:36:54 | 53.38/55.30/51.91 | 25P | 7.09 |
| attempt_tmpdir_daemon / 3.14.7 | 19:37:05 | 48.30/54.08/51.53 | 24P | 50.63 |
| attempt_tmpdir_leaks_daemon / 3.14.7 | 19:38:04 | 38.70/50.37/50.30 | 8P | 18.18 |
| state_contract / 3.14.7 | 19:38:28 | 43.53/50.60/50.39 | 37P | 51.83 |

Regression labels expand to `tests/unit/test_<label>.py` except archive/turns/sweep
and the three explicit fake files: attempt_tmpdir_daemon, attempt_tmpdir_leaks_daemon,
and state_contract are under `tests/fake/`. Both retention_readback labels
select `test_retention_readback.py`. No real provider is started. The
lifecycle model is unchanged; its previously tracked defect remains with the hub.

## Mutants and named killing tests

Each mutation runs only its named selection on 3.14. A kill requires a failed
call-phase report in that named test. M1/M2 now use only the low-soft-limit
selection (four cases); prior broad-file counts are historical, not repeated.
MSR and MD adaptations are described above. MOC is review r4's removal of the
repair applied to production read_regular; MES removes only the None skip.

| Mutant | Start UTC | Load 1/5/15 | P/F | Named killing test selection |
| --- | --- | --- | --- | --- |
| M1 | 19:23:44 | 35.26/42.55/47.10 | 2P, 2F | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction` |
| M2 | 19:23:52 | 34.65/42.17/46.91 | 2P, 2F | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction` |
| MO | 19:23:59 | 33.72/41.85/46.77 | 2F | `tests/unit/test_retention_walk_descriptors.py::test_open_dir_failed_validation_closes_descriptor` |
| ML | 19:24:07 | 33.01/41.43/46.56 | 1F | `tests/unit/test_retention_descriptor_review.py::test_layout_allocation_before_open` |
| MI | 19:24:13 | 32.93/41.27/46.48 | 1F | `tests/unit/test_retention_descriptor_review.py::test_in_repository_registration_failure_closes_descriptor` |
| MP | 19:24:21 | 33.32/41.08/46.35 | 2F | `tests/unit/test_retention_walk_descriptors.py::test_open_path_failed_descent_closes_intermediate_descriptors` |
| MC | 19:24:36 | 32.05/40.41/46.02 | 1P, 3F | `tests/unit/test_retention_walk_descriptors.py::test_conflict_dir_failed_descent_closes_descriptor` |
| MH | 19:24:54 | 31.34/39.82/45.71 | 6F | `tests/unit/test_retention_descriptor_review.py::test_descent_close_failure_owns_new_child` |
| MD | 19:25:03 | 32.29/39.75/45.61 | 2F | `tests/unit/test_retention_descriptor_review.py::test_walk_cleanup_interruption_closes_remaining_children` |
| MR | 19:25:10 | 32.42/39.53/45.46 | 4P, 2F | `tests/unit/test_retention_descriptor_review_r2.py::test_failed_handoff_close_never_closes_a_reused_number` |
| MA | 19:25:19 | 33.34/39.60/45.45 | 2F | `tests/unit/test_retention_descriptor_review_r2.py::test_interrupt_after_registration_closes_child_once` |
| ME | 19:25:26 | 34.58/39.65/45.40 | 1F | `tests/unit/test_retention_descriptor_cleanup.py::test_open_dir_swapped_identity_survives_close_error` |
| MX | 19:25:35 | 33.14/39.17/45.16 | 1F | `tests/unit/test_retention_descriptor_cleanup.py::test_set_aside_cleanup_cancellation_overrides_handoff_oserror` |
| MAH | 19:25:45 | 35.02/39.39/45.17 | 1P, 3F | `tests/unit/test_retention_descriptor_review_r3.py::test_normal_read_close_failure_is_not_suppressed_by_handled_caller_exception` |
| MSR | 19:25:58 | 36.01/39.47/45.13 | 2F | `tests/unit/test_retention_descriptor_review_r3.py::test_attempt_temp_size_root_close_preserves_walk_cancellation` |
| MPO | 19:26:14 | 41.55/40.51/45.41 | 1F | `tests/unit/test_retention_descriptor_review_r3.py::test_interrupt_after_stack_pop_closes_popped_child` |
| MGF | 19:30:47 | 67.99/54.21/49.97 | 3F | `tests/unit/test_retention_descriptor_review_r3.py::test_record_complete_early_return_propagates_cleanup_cancellation` |
| MOC | 19:31:00 | 70.96/55.55/50.52 | 4F | `tests/unit/test_retention_descriptor_review_r4.py::test_owner_setup_failure_closes_acquired_file` |
| MES | 19:33:20 | 51.26/53.14/50.33 | 1F | `tests/unit/test_retention_descriptor_prepared.py::test_empty_slots_are_never_closed_and_live_slots_close_once` |

## PR-body replacements for the hub

- The exercised listing and registration failures close a child once;
  post-registration cleanup leaves it to the stack. The documented open-return
  and asynchronous instruction boundaries remain outside this guarantee.
- The exercised validation, container-allocation, registration and failed-descent
  cases are protected. Fix r4 enters all 35 descriptor owners with preallocated
  slots before acquisition, including the three other descriptor-helper calls.
  The native open-return-to-slot-store window remains L5. Descent handoffs do
  not retry a close or reuse a released descriptor number.
- Once `_close_all` is entered and its iteration is not asynchronously interrupted,
  it attempts each remaining owned close once. `walk` preserves the borrowed
  root. The named single-failure differentials retain release exception types
  and TreeError codes for their executed inputs.
- The `_cleanup`/`_close_all` paths receive the operation's explicit exit exception,
  including AttemptTemp.size's root close. In those paths, handled caller
  exceptions no longer suppress cleanup failures or cancellation.
- All ten walk consumption sites explicitly close their iterators after owner
  setup succeeds, subject to the documented exit-instruction boundary.

**Invariant paragraph's last sentence:** This rule is verified for entered
cleanup owners and the exercised inputs; fix r4 prepares descriptor owners
before acquisition, while the listed pop, exit-dispatch, raw-handle precedence,
repeated-interruption and native open-return-to-store differences L1–L5 remain
qualified, and claims extend only as far as these tests.

**Fix round r4:** Review r4 requested changes at b698146d0 for P2-6. At starting
head 9b1a9d462, all four owner construction/entry cases fail on Python 3.14.7
and 3.12.14 with marks bypassed. Fix r4 enters preallocated-slot owners before
all 35 converted descriptor acquisitions and skips empty slots during cleanup.
The original four cases pass without marks, and all 140 setup-injection cases
across those sites pass on both Pythons. The AST guard rejects acquired-descriptor
cleanup arguments; inherited parity remains unchanged. All nineteen named
production mutants are killed. L1–L5 remain listed differences.

## Environment, delivery and exclusions

The sandbox denied `$(getconf DARWIN_USER_TEMP_DIR)sf-fix279r4`. Environment,
logs and isolated Git metadata use `/private/tmp/sf-fix279r4`. Walked fixtures
use the task's writable Darwin temporary directory, in its `sf-fix279r4` child,
to avoid the literal tmp-component classification in archive fixtures. Both
Python dev environments were installed under uv_sync_hold.sh from the locked
dev group and reused with uv run --no-sync. Network was used only for sync.
No sub-agents were used, in accordance with the lane rules.

Shared Git staging failed creating index.lock. Coherent fix, tests/mutants and
text commits therefore use isolated metadata on `fix/walk-fd-leak-217`, reading
base objects without modifying shared metadata. Only named files are staged;
every commit ends with the required Claude coauthor trailer. No history was
rewritten and no caller checkout was written, fetched, merged or pushed.
The incremental bundle `2026-10-30-walk-descriptors-fix-r4.bundle` contains the
branch from 9b1a9d462 and is verified before guarded scratch cleanup. The shared
detached HEAD remains 9b1a9d462. Final delivery details accompany verification.

No live store, real CLI, provider, host process census or CI/network snapshot
was accessed. All test-started daemons/providers use fixture stand-ins. No
run was detached; every tool session is polled to completion and every started
process ends before delivery.

Not run: archive's live lsof case, the full suite, live frontend tests left to
CI, broader regressions on Python 3.12, actual native-close signal delivery,
independent RegenerableWalk constructor injections, or CI status/job logs.

Distinct maintained passes (excluding parity, repeats, reds and mutants): 3.12.14: 295, 3.14.7: 940.

Pending at evidence collection: none.

Recorded runs: 65; longest pytest session: 321.07s; peak runner RSS: 118.34 MiB.

All recorded runs used the shared wrapper; none used the direct fallback.
All batch drivers and every admitted hold started within the first hour; the last admission was 19:38:28 UTC. No hold crossed ten minutes.
The two scratch roots are removed after bundle verification with the lane rules’ guarded cleanup; all task-started processes end before the final answer.
The largest recorded logical scratch sample is 37.53 MiB; the earlier allocated-size du sample was approximately 60 MiB combined.
