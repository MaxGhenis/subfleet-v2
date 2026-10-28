# Retention dependency pins: continuation 3 in progress, 2026-10-28

**Not ready to land.** Continuation 3 has all 37 imported folded-pin cases passing and closes the observed legacy-spelled-folder mutation gap with a deterministic red/green test. 15 original mutants have run: 14 are killed and one is an intended equivalent. 18 original mutant holds remain, with measurement and contract audit still unstarted. The current property hold caps and remaining correctness work are explicit below. Earlier continuation sections preserve their historical results.

Original build (2026-10-27): the lane started at approximately 14:51Z; new holds/steps stop at 17:49:00Z, conservatively before its three-hour start cutoff. Lock waits count toward that wall. The final update is 2026-10-27T18:08:30Z.

## Continuation 3 (in progress), 2026-10-28

Started at approximately **19:43Z**; **23:42Z** is the conservative cutoff for new holds and steps. Shared metadata rejected `index.lock`, so commits use private scratch metadata on `fix/retention-dependency-pins-217`; the assigned worktree's `.git` file stays unchanged. A verified `rdp217c3.bundle` will deliver the ordered commits. No fetch, push, real provider, live store or production CLI is used.

The three imported firmlink assertions now check recorded canonical identity and the fixture's canonical folded identity. **TURN, READER and writer all pass their behavioral assertions (3 passed).** The other **34 cases pass**, with **500 passing / 217 invalid** string examples and **150 passing / 17 invalid** real-volume examples. **WIP `454ed9528` is subsumed; its production change is not ported.** The imported file has no other change.

The mutation table below is observed evidence, with unrun rows still explicit. The first four mutation holds used normal Hypothesis settings; later mutation holds retain the same strategies, seed 217 and configured example counts but use explicit/generate phases with no shrinking and stop at the first counterexample. Head-only correctness keeps normal settings. `live-ignores-spelled-folder` survived its planned property. The new `test_d2_a_legacy_admission_folder_alone_pins_until_the_job_ends` isolates `job.folder_spelled`: **red 1 failed** against that mutant, then **green 1 passed** at head. It also checks transactional dependents and release after the job ends. Its actual killing node replaces the planned selector in the table. `pin-counts-own-places` is an intended equivalent on a consistent pin census: own live evidence is already `active`, and own pending-export evidence is already `job-lease`; `add()` keeps that earlier reason. The transactional checks are unchanged.

Head-only holds completed so far include **17 dependency units**, **44 dependency/spelling cases**, and the **37 imported folded-pin cases**. A combined property hold reached its **1,140 s cap**: selection safety completed with passing teardown; the independent-reference case was incomplete. A standalone reference hold and a monotonicity hold also reached the cap without a test outcome. Those two current properties remain open. The generated fence property passed **24 generated examples / 0 invalid**, plus its explicit case. Its completed safety result is preserved; no generated-example count is claimed for that capped hold because its terminal statistics were not retained. The properties and neighbouring batches are split into separate holds; detached and shared basic cases are split further by `-k`.

The supplied environment sync ran once. Holds use that locked Python 3.14.7 environment, private TMPDIR/Hypothesis roots and the supplied suite lock. A green-waiter preflight hit a concurrent JSON rewrite before starting any hold; atomic ledger writes and tolerant reads fixed that orchestration race, and its restarted green hold passed. No test result is credited to the aborted preflight. Early holds invoked the installed environment's Python directly; later holds use `uv run --no-sync` with the same explicit environment/cache. This changes no dependencies. At most four pytest workers execute concurrently. Independent head batches use three workers; all mutant test executions, including the survivor replay in its own copy, serialize on one scratch file lock. The green replay waits for the guard worker to end before it starts. Completed results and failures are saved as compact JSON; passing logs are not committed. Scratch was sampled at **200 MiB**. The two pin-query timing runs and contract audit remain unstarted at this checkpoint.

## Continuation 2 correctness, 2026-10-28

The lane began at approximately **17:02Z**. A conservative **21:01Z** cutoff stops new holds and steps before four hours. This is a serial build using Python 3.14.7, the locked dev environment, the supplied test-lock script and fixture-owned roots. No fetch, push, real provider, live store, production CLI, mutation sweep or performance measurement was started. Test runtime and host load below describe correctness holds, not query measurements. The contract is unchanged.

Shared metadata rejected `index.lock` with `Operation not permitted`. Ordered commits use private scratch metadata on `fix/retention-dependency-pins-217`; the worktree's `.git` file remains unchanged. Delivery is the verified **`rdp217c2.bundle`**, requiring the already-present `be9109a8ed18` history. Final bundle verification and guarded scratch cleanup follow the report commit.

### Folded-pin verdict

`tests/unit/test_retention_folded_pin.py` was imported byte for byte from `454ed9528`, with no production WIP change. The complete hold produced **34 passed, 3 failed**. Hypothesis observed **500 passing / 197 invalid** string examples and **150 passing / 18 invalid** real-volume examples, with zero failing generated examples. The latter count is observed statistics, not the configured maximum.

All three failures stop at the old assertion that a submitted job's `workdir` starts with `/System/Volumes/Data/` (TURN, READER and writer). Release's submit now explicitly records `folders.canonical(workdir)` at `subfleet/daemon.py`'s `recorded_workdir` assignment. A separate three-case rerun under a `/Users` workspace root produced the same three failures, so the result is not an assumption about the temporary root's spelling. These cases never reach the behavioral assertions after that obsolete precondition. The manually recorded firmlink case and every other imported case pass.

Changing production to retain the prefix would undo release behavior and cannot fit the dependency-read/fold design. The brief permits import changes only, so the imported test remains unchanged; a narrow request to replace only the obsolete prefix assertion with canonical identity has no approval yet. **Full WIP subsumption is unproven**. Its `retention.py` implementation was not ported, and no pin defect requiring a second mechanism was observed. These three fixture-oracle failures remain explicit follow-up work.

### Spelling gap: real red, then green

The pending sketch was revalidated before use. Its first trial was not accepted as red evidence: eight output cases failed submit's existing parent-directory validation, and exact Git-storage evidence masked review-root aliases. The corrected fixture quarantines immediately after output-parent validation and before dependency recording; its output file and then its aliased parent are absent. Review-root replay respells its Git-storage evidence too. A separate premature launch collected no tests and contributes no coverage.

The corrected **16-case red hold failed every case at `assert not _live(...)`**: case, ﬁ, ſ and K/Kelvin aliases, output and review root, before workspace preparation and in the reservation transaction. Production had not changed for this hold. After the fix, **124 tests passed**: all 16 gap cases, 17 dependency units, 28 dependency fakes and 63 guards. The gap cases assert the dependency role and original path, no premature workspace call, continued waiting on the next look, and admission after restoring the tree and releasing the fence.

`dependencies.fences` now reads the indexed `worktree:` lease range once and compares normalized `folders.fold` keys, as retention's pins do. The reserving transaction calls the same query for all needs, preserving its own-key exclusion. Only retention holders count, nearest fences come first, and existing needs roles identify the hold. No new mechanism, intended xfail or contract exception was added.

### Generated properties

Each job set mixes live and terminal states, row and recorded folders, worktrees, review roots, output paths, boundary non-matches and linked-checkout storage read from a generated gitfile. Case and Unicode spellings include ﬁ, ſ, K/Kelvin and NFC/NFD. The independent oracle implements canonical caseless normalization and path boundaries directly, without production folding or dependency enumeration.

| Property | Observed generated passing examples | Invalid | Progress assertion |
|---|---:|---:|---|
| Selection safety | 100 | 2 | Untargeted `free` retires in every case; live dependents' files remain intact. |
| Differential | 100 | 2 | Pins and transactional dependents equal the brute-force reference; `free` actually retires. |
| Monotonicity | 100 | 2 | Adding a live dependent preserves pins; ending the last dependent makes every owner retire. |
| Fence | 24, plus 1 explicit example | 0 | Generated needs match the reference in one indexed query; `free` retires; actual submit waits and is admitted after release. |

The first fence attempt failed in its explicit example before generation: a helper import inside the running property imported another `@given` declaration. Imports now run during collection; the health check was not suppressed. The successful run's **24 generated examples** and **one explicit example** are counted separately. No intended correctness violation is hidden by an xfail. Conservative folding on case-sensitive volumes is intentional.

### Holds and coverage

All 12 requested files were collected in the final guard hold before its keyword deselection. The independent evidence join finds **356 required nodes: 353 passed, 3 failed, 0 missing or incomplete**. The failures are exactly the three imported obsolete firmlink-prefix assertions above. Every spelling, new property, dependency, release-integration and guard node has a later passing outcome. Red trials and repeated guards are not summed as unique coverage.

Start times below are the pytest process's recorded start inside each hold, on 2026-10-28 UTC; loads are `os.getloadavg()`'s 1-minute values captured then. The wrapper's `sysctl` display was sandbox-denied, so its blank load is not used. Runtime is the collector's elapsed time; startup/cleanup account for small differences from pytest's terminal duration. Every completed test hold stayed below 20 minutes. The empty launch and invalid sketch are listed for accounting, not accepted as red evidence.

| Hold | Start UTC | Result | Lock | 1-min load | Recorded runtime |
|---|---|---|---|---:|---:|
| [c2-folded](2026-10-27-retention-dependency-pins-data/c2-folded.json) | 17:08:20 | 34 passed, 3 failed | locked | 95.2 | 562.5 s |
| [c2-folded-firmlink](2026-10-27-retention-dependency-pins-data/c2-folded-firmlink.json) | 17:18:55 | 3 failed | locked | 124.0 | 151.6 s |
| [c2-spelling-not-ready](2026-10-27-retention-dependency-pins-data/c2-spelling-not-ready.json) | 17:24:13 | 0 tests (exit 4) | locked | 52.0 | 17.0 s |
| [c2-spelling-sketch](2026-10-27-retention-dependency-pins-data/c2-spelling-sketch.json) | 17:25:21 | 4 passed, 12 failed | locked | 48.7 | 110.0 s |
| [c2-spelling-red](2026-10-27-retention-dependency-pins-data/c2-spelling-red.json) | 17:29:07 | 16 failed | locked | 91.4 | 108.3 s |
| [c2-spelling-green](2026-10-27-retention-dependency-pins-data/c2-spelling-green.json) | 17:31:36 | 124 passed | locked | 54.6 | 646.3 s |
| [c2-properties-selection](2026-10-27-retention-dependency-pins-data/c2-properties-selection.json) | 17:48:33 | 3 passed | locked | 108.9 | 714.6 s |
| [c2-properties-fence-import](2026-10-27-retention-dependency-pins-data/c2-properties-fence-import.json) | 18:01:23 | 1 failed | locked | 88.1 | 41.7 s |
| [c2-properties-fence](2026-10-27-retention-dependency-pins-data/c2-properties-fence.json) | 18:03:26 | 1 passed | locked | 95.4 | 136.7 s |
| [c2-writer-rediscovery](2026-10-27-retention-dependency-pins-data/c2-writer-rediscovery.json) | 18:07:36 | 40 passed | locked | 56.4 | 215.6 s |
| [c2-detached-basic](2026-10-27-retention-dependency-pins-data/c2-detached-basic.json) | 18:11:48 | 87 passed | locked | 56.4 | 432.3 s |
| [c2-detached-generated](2026-10-27-retention-dependency-pins-data/c2-detached-generated.json) | 18:23:11 | 2 passed | locked | 48.7 | 281.3 s |
| [c2-shared-basic](2026-10-27-retention-dependency-pins-data/c2-shared-basic.json) | 18:30:30 | 59 passed | locked | 92.0 | 707.1 s |
| [c2-shared-generated](2026-10-27-retention-dependency-pins-data/c2-shared-generated.json) | 18:42:45 | 2 passed | locked | 21.4 | 326.3 s |
| [c2-shared-reservation](2026-10-27-retention-dependency-pins-data/c2-shared-reservation.json) | 18:48:26 | 1 passed | locked | 58.5 | 564.9 s |
| [c2-guards-plan](2026-10-27-retention-dependency-pins-data/c2-guards-plan.json) | 18:58:01 | 63 passed | locked | 63.3 | 112.8 s |

The compact `c2-*.json` files retain node lists, outcomes and observed statistics. Earlier normal-exit holds retain calls and every non-passing phase; later holds retain all three phases. `c2-check-results.py` requires normal completion or passing teardown before crediting a node and independently joins the final collection to its latest outcome. Full passing logs, runner PIDs, claims and admission markers are not committed.

The largest captured pytest RSS was **154.7 MiB**; the largest captured child RSS was **9.0 MiB**. Child RSS was not recorded in the first holds. Scratch was sampled at about 114 MiB during the run and **293.7 MiB at close**, plus the **2.4 MiB** owned firmlink fixture. The largest sampled combined footprint was about **296.1 MiB**, below 2 GB. Sixteen tracked owned process IDs were checked after the final hold; none remained present. No timing, mutation or contract-audit run was started. After the report commit, verify the ordered bundle, confirm the tracked drivers have ended and remove only this run's scratch and owned fixture root with the lane rules' guarded cleanup.


## Continuation 1 recovery, 2026-10-28 (coverage complete; later steps unbuilt)

This process started at **07:47Z** and stops starting holds and steps at **11:46Z**. Accounting snapshot: **2026-10-28T12:32:22+00:00**. It has completed **732 of 732** starting nodes with complete full-phase outcomes (including four fixture skips); **0 remain** in `c1c-current-remaining.txt`. Incomplete nodes count as unfinished even when earlier nodes in their hold passed. Release-comparison results never count toward branch coverage. The JSONL ledgers and per-hold collection/results/log files preserve the exact evidence. `c1c-verify-coverage.py` independently joins every planned node to its saved evidence in `c1c-planned-status.json`; it agrees with the residual list.

The lost job's 27 salvaged files were verified byte for byte (its original list is preserved separately). Its `c1-reconstruct.py` correctly reproduces **761** unfinished nodes from **1,104** unique planned nodes and **343** completed planned nodes. `c1b-reconstruct.py` independently verifies every saved plan against pytest's saved collection and keyword matcher. Two additional complete salvaged results leave **759** nodes in `remaining-nodes.txt`; `c1-original-remaining-nodes.txt` preserves the original 761. Lost batches 01 and 03 contain only setup results and count as incomplete. Four inherited `c1b-remaining-02` through `-05` runner-PID records are retained as historical artifacts; they grant no additional result credit. The preserved replacement attempt completed another **27** nodes (20 + 6 + 1); this process reuses their complete results and starts with **732**, saved in `c1c-start-remaining.txt`.

At unchanged production checkpoint `6ca8f4b0bc36`, the salvage also proves **80 passed, 3 skipped** for dependency units, all three guards and provider shell environment, and **37 passed** for dependency fakes and daemon launch. These results are reused: 17 dependency units, 28 dependency fakes, 9 daemon-launch cases and the 63 guards (37 state-contract, 1 module-name, 25 invariants-index). All three provider-shell cases were collected and skipped with real provider executables absent from PATH; fake daemon-launch cases executed. Tests used the tree's fakes and their own roots. No live process/store, real provider or production CLI was used; no fetch, push or timing run was started for this continuation. Each hold has one serial pytest, at most four workers, a separate lock request and a cap below 20 minutes. Queued holds use the authorized direct fallback only after 30 minutes without admission. The 11:46Z cutoff prevents creating a new hold; a hold already queued before it retains that wait and execution cap, so its admission may occur later.

Shared metadata rejects `index.lock` with `Operation not permitted`. The preserved bundle's recovery checkpoint **`4319e7b0c`** remains an unchanged ancestor. Ordered commits use private scratch Git metadata on `fix/retention-dependency-pins-217`; the workspace's `.git` file is unchanged. The verified delivery artifact is **`rdp217c1.bundle`**. Only this process's `sf-rdp217c1/recovery-0747` scratch subtree is owned here; its initial footprint was approximately 23 MB and its largest sampled footprint was approximately 182 MiB. The largest recorded Python RSS was 261,308,416 bytes (249.2 MiB); subprocess RSS was not separately sampled. The release comparison checkout was removed after use. Guarded removal of the remaining private metadata and environment follows final bundle verification.

### Integration corrections

- The detached-fence oracle omitted recorded dependency evidence, including a linked checkout's external Git storage. Its red hold had 49 passed and 1 failed, outside the lock at load 93.1. Its corrected independent comparison retains both counterexamples. The first rerun encountered stale fixture readings and `no-slot`; the original test at detached release **`46ab44d72` passed (1 passed, 81.28 s, load 23.5)**, so that failure is **not classified as pre-existing**. Fixture readings now refresh per example, retaining the normal 120-second freshness bound. The corrected oracle passed in `04-resume-4` (47.31 s call; its at-least-80-example assertion completed).
- The writer oracle omitted the same Git-storage evidence. Its corrected independent comparison and both explicit counterexamples passed in isolated hold `47`: **1 passed in 106.30 s**, outside the lock at load **110.0**. Its at-least-60-example assertion completed. These corrections change reference expectations, not production admission.
- Three old `_fence_hold` stubs now accept the `needs` keyword. The rediscovery comparison passed in `11-resume-2`. The completed early/reservation differential is reported below; its configured maximum is separate from observed example counts.
- The transaction interleaving previously injected its fence after the first early query. The new dependency query correctly caught it before preparation. Injection now happens after the entire early look so the test exercises the reserving transaction; the focused regression passed in `11-resume-2`.
- `11-resume-2` completed the admission state machine and salvage property (9 passed overall, load 94.8). `13-resume-2` completed the sweep property (**40 observed generated passing examples**, plus explicit examples) and all seven combined admission cases (8 passed, load 80.0). These are existing properties, separate from the unbuilt new dependency properties.

The early/reservation differential in isolated hold `58` passed: **1 passed in 915.95 s**, outside the lock at load **57.5**. Its configured maximum is 400 examples; its nested generator supplies no separate observed-example statistic, so no exact example count is claimed here. All observed integration failures now have later complete passing results.

No production source or contract was changed in this continuation. Step 3's folded-pin test is **unassessed**, with **0 observed real-volume examples**; its WIP implementation was not ported. The spelling gap has **no red or green run** yet. The new safety, differential, monotonicity and fence properties each have **0 observed examples**. Their unexecuted preparation is preserved under `pending/c1-*`; it requires validation before use.

### Continuation holds

Loads are observed 1-minute admission loads. Counts below describe each hold, including completed prefixes of interrupted holds; they must not be summed as unique coverage. Failed nodes are resolved only by a later complete passing result. The exact current unresolved failure list is `c1c-accounting.json`.

| Hold | Result | Lock | Admission load | Child runtime / cap |
|---|---|---|---:|---:|
| c1 core-units | 80 passed, 3 skipped | locked | 115.3 | salvaged log |
| c1 core-fake | 37 passed | locked | 110.9 | salvaged log |
| c1 remaining-02 | 1 passed | locked | 62.7 | salvaged log |
| c1 remaining-04 | 1 passed | outside after 30 min | 115.1 | salvaged log |
| c1b remaining-01 | 20 passed | locked | 50.3 | 158.1 s wall |
| c1b remaining-02 | 6 passed | locked | 122.7 | 107.5 s wall |
| c1b remaining-03 | 1 passed | locked | 136.9 | 274.2 s wall |
| [c1c-remaining-02](2026-10-27-retention-dependency-pins-data/c1c-remaining-02.log) | 50 passed | locked | 104.6 | 289.50 s |
| [c1c-remaining-03](2026-10-27-retention-dependency-pins-data/c1c-remaining-03.log) | 50 passed | locked | 93.4 | 552.95 s |
| [c1c-remaining-01](2026-10-27-retention-dependency-pins-data/c1c-remaining-01.log) | 7 passed, 1 incomplete | locked | 106.3 | interrupted before 20 min |
| [c1c-remaining-05](2026-10-27-retention-dependency-pins-data/c1c-remaining-05.log) | 50 passed | locked | 86.8 | 537.80 s |
| [c1c-remaining-07](2026-10-27-retention-dependency-pins-data/c1c-remaining-07.log) | 50 passed | locked | 74.6 | 795.48 s |
| [c1c-remaining-04](2026-10-27-retention-dependency-pins-data/c1c-remaining-04.log) | 49 passed, 1 failed | outside after 30 min | 93.1 | 780.95 s |
| [c1c-remaining-09](2026-10-27-retention-dependency-pins-data/c1c-remaining-09.log) | 52 passed, 4 skipped, 1 incomplete | locked | 77.5 | interrupted before 20 min |
| [c1c-remaining-04-resume-2](2026-10-27-retention-dependency-pins-data/c1c-remaining-04-resume-2.log) | 1 failed | locked | 18.3 | 472.81 s |
| [c1c-remaining-14](2026-10-27-retention-dependency-pins-data/c1c-remaining-14.log) | 49 passed, 1 incomplete | locked | 79.6 | interrupted before 20 min |
| [c1c-release-oracle](2026-10-27-retention-dependency-pins-data/c1c-release-oracle.log) | 1 passed | locked | 23.5 | 106.11 s |
| [c1c-remaining-11](2026-10-27-retention-dependency-pins-data/c1c-remaining-11.log) | 51 passed, 2 failed | outside after 30 min | 177.6 | 800.02 s |
| [c1c-remaining-14-resume-2](2026-10-27-retention-dependency-pins-data/c1c-remaining-14-resume-2.log) | 59 passed, 1 failed | locked | 84.1 | 1022.28 s |
| [c1c-remaining-13](2026-10-27-retention-dependency-pins-data/c1c-remaining-13.log) | 43 passed, 1 incomplete | outside after 30 min | 130.1 | interrupted before 20 min |
| [c1c-remaining-11-resume-2](2026-10-27-retention-dependency-pins-data/c1c-remaining-11-resume-2.log) | 9 passed | locked | 94.8 | 797.99 s |
| [c1c-remaining-13-resume-2](2026-10-27-retention-dependency-pins-data/c1c-remaining-13-resume-2.log) | 8 passed | locked | 80.0 | 880.46 s |
| [c1c-remaining-04-resume-4](2026-10-27-retention-dependency-pins-data/c1c-remaining-04-resume-4.log) | 53 passed, 1 failed, 1 incomplete | outside after 30 min | 93.0 | 1128.66 s |
| [c1c-remaining-14-resume-3](2026-10-27-retention-dependency-pins-data/c1c-remaining-14-resume-3.log) | 21 passed, 1 incomplete | locked | 94.3 | interrupted before 20 min |
| [c1c-remaining-47](2026-10-27-retention-dependency-pins-data/c1c-remaining-47.log) | 1 passed | outside after 30 min | 110.0 | 136.88 s |
| [c1c-remaining-48](2026-10-27-retention-dependency-pins-data/c1c-remaining-48.log) | 53 passed | outside after 30 min | 54.9 | 702.59 s |
| [c1c-remaining-49](2026-10-27-retention-dependency-pins-data/c1c-remaining-49.log) | 53 passed | outside after 30 min | 54.9 | 961.32 s |
| [c1c-remaining-06](2026-10-27-retention-dependency-pins-data/c1c-remaining-06.log) | 9 passed, 1 incomplete | outside after 30 min | 23.9 | interrupted before 20 min |
| [c1c-remaining-58](2026-10-27-retention-dependency-pins-data/c1c-remaining-58.log) | 1 passed | outside after 30 min | 57.5 | 936.37 s |
| [c1c-remaining-51](2026-10-27-retention-dependency-pins-data/c1c-remaining-51.log) | 3 passed | outside after 30 min | 98.6 | 354.19 s |
| [c1c-remaining-32](2026-10-27-retention-dependency-pins-data/c1c-remaining-32.log) | 2 passed, 1 incomplete | locked | 69.0 | interrupted before 20 min |
| [c1c-remaining-50](2026-10-27-retention-dependency-pins-data/c1c-remaining-50.log) | 2 passed, 1 incomplete | outside after 30 min | 64.4 | 1123.44 s |
| [c1c-remaining-60](2026-10-27-retention-dependency-pins-data/c1c-remaining-60.log) | 1 passed | locked | 39.4 | 520.37 s |
| [c1c-remaining-62](2026-10-27-retention-dependency-pins-data/c1c-remaining-62.log) | 1 passed | outside after 30 min | 64.1 | 459.57 s |
| [c1c-remaining-61](2026-10-27-retention-dependency-pins-data/c1c-remaining-61.log) | 1 passed | outside after 30 min | 64.3 | 900.22 s |

All foreground test drivers have ended. The latest hold was queued at **2026-10-28T11:44:52.916289+00:00**, before the 11:46Z cutoff; its later admission does not represent a newly created hold. Queued-only cancellations ran no tests. Final unique outcomes for this process: **728 passed, 4 skipped, 0 unfinished**. `c1c-completion-summary.json` and the independent per-node evidence join agree.

The four skips are the clone/hardlink × resume combinations of `test_an_ordinary_tree_retires_without_an_unarchived_path`, skipped by its offline virtualenv fixture before its assertions. Its exact skip-reason text was not retained. These are coverage exceptions, not passing assertions. The old turn-folder pin property in hold 51 observed 150 generated passing examples; that is separate from the **unrun real-volume folded-pin property**, which observed zero.

The resumed shared-folder interleaving in hold 60 observed **40 generated passing examples** (1 passed in 513.97 s, locked at load 39.4). No production source or contract changed. No new hold or feature step was created after 11:46Z; only the three already-queued holds finished afterward. The final bundle is verified before guarded removal of this process's scratch subtree; unrelated pre-existing scratch remains untouched.

## Original build checkpoints and recovery (2026-10-27)

Shared Git metadata rejected `ORIG_HEAD.lock` with `Operation not permitted`. That build's workspace metadata remained at `8f106c64595c`; checkpoints were committed in scratch Git metadata on `fix/retention-dependency-pins-217`. That build's delivery bundle required the already-present `8f106c645` and `c9430763f` histories. The current continuation bundle requires `6ca8f4b0bc36`, as described above. No shared branch update was possible.

1. `c1c1f6b0d`: merge release/217, retaining the Python dependency pin and every release fence behavior.
2. `6997e2127`: canonical legacy folder discovery outside transactions, output-lease identity assertions, the closed Unicode pin expectation, and the unique unit module basename.

Every checkpoint has the requested co-author footer. [The complete file manifest](2026-10-27-retention-dependency-pins-data/commit-order.txt) lists changes in commit order. The unit file is now `tests/unit/test_retention_dependency_units.py`; its old basename duplicated the fake module and failed the required module-name guard.

## Verified behavior and red runs

On release alone (`c9430763f`, private detached scratch worktree), the dependency fake file produced **25 failures and 3 passes in 348.13 s**. The unit file produced **one collection error** because release has no `subfleet.dependencies`; no unit cases executed. This unit red run used the authorized outside-lock fallback after 30 minutes without admission.

The merged core hold produced **58 passes and 2 failures in 1,150.39 s**. Both failures were obsolete expectations: raw output lease keys versus release's canonical identity, and a legacy Unicode alias expected to miss the pin. After their correction, the follow-up produced **85 passes and one failure in 959.48 s**: the newly added legacy firmlink regression. The three contract guards (63 cases) and desktop ledger checks passed in that follow-up.

Release no longer caches `job.folder_spelled` across admission looks. A legacy job in a plain folder through the Data-volume firmlink therefore needed its canonical workdir added to the pass's dependency cache. That filesystem work runs before the transaction. The regression failed with `legacy={'legacy': []}` before the fix; the three legacy unit checks passed afterward (**3 passed, 14 deselected, 18.59 s**, outside the lock after its 30-minute wait).

## Original build holds (2026-10-27)

Loads below are the observed 1-minute load at admission, except the original fake red hold, whose lock script's `sysctl` read was blocked. Its queue/end loads are given instead. Every pytest was serial; no xdist was installed. Interrupted holds are incomplete coverage, not green suite results. The original build's highest recorded Python RSS was 184,696,832 bytes (176.1 MiB); subprocess RSS was not separately sampled.

| Hold | Result | Lock | Load | Admission-to-exit wall |
|---|---|---|---:|---:|
| [red-fake](2026-10-27-retention-dependency-pins-data/red-fake.log) | 25 failed, 3 passed in 348.13s (0:05:48) | locked | unavailable (queue 24.4; end 125.5) | not recorded |
| [merge-core](2026-10-27-retention-dependency-pins-data/merge-core.log) | 2 failed, 58 passed in 1150.39s (0:19:10) | locked | 84.3 | 1190.4 s |
| [retention-archive-a](2026-10-27-retention-dependency-pins-data/retention-archive-a.log) | 15 passed, 42 deselected in 339.58s (0:05:39) | locked | 124.6 | not recorded |
| [red-unit](2026-10-27-retention-dependency-pins-data/red-unit.log) | 1 error in 34.25s | outside | 95.2 | 57.1 s |
| [merge-fixes](2026-10-27-retention-dependency-pins-data/merge-fixes.log) | 1 failed, 85 passed in 959.48s (0:15:59) | locked | 96.5 | 1010.4 s |
| [admission-a](2026-10-27-retention-dependency-pins-data/admission-a.log) | interrupted: 162 passing markers, 1 intended xfail; final 2 nodes scheduled separately | locked | 93.2 | not recorded |
| [retention-light](2026-10-27-retention-dependency-pins-data/retention-light.log) | 37 passed in 1077.07s (0:17:57) | locked | 106.3 | 1100.6 s |
| [retention-archive-1](2026-10-27-retention-dependency-pins-data/retention-archive-1.log) | 22 passed, 87 deselected in 300.91s (0:05:00) | locked | 51.6 | 315.7 s |
| [legacy-fold-green](2026-10-27-retention-dependency-pins-data/legacy-fold-green.log) | 3 passed, 14 deselected in 18.59s | outside | 69.7 | 29.1 s |
| [retention-archive-3](2026-10-27-retention-dependency-pins-data/retention-archive-3.log) | 21 passed, 88 deselected in 212.49s (0:03:32) | locked | 61.8 | 221.6 s |
| [admission-a-tail](2026-10-27-retention-dependency-pins-data/admission-a-tail.log) | 2 passed in 156.30s (0:02:36) | outside | 61.7 | 171.7 s |
| [retention-rest-1](2026-10-27-retention-dependency-pins-data/retention-rest-1.log) | 12 passed in 185.78s (0:03:05) | outside | 217.4 | 769.7 s |
| [admission-b-1](2026-10-27-retention-dependency-pins-data/admission-b-1.log) | 21 passed, 276 deselected in 767.14s (0:12:47) | locked | 174.9 | 1120.4 s |
| [retention-archive_properties](2026-10-27-retention-dependency-pins-data/retention-archive_properties.log) | 2 passed in 332.69s (0:05:32) | outside | 64.4 | 341.1 s |
| [salvage-small-a](2026-10-27-retention-dependency-pins-data/salvage-small-a.log) | 62 passed, 54 deselected in 899.31s (0:14:59) | outside | 13.6 | 1024.9 s |

The admission slice's expected failure is intentional and pre-existing: `test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again` documents C-6.10's wait-kind reset gap. It is not closed by retention pins. Queued wrappers that were cancelled or resized before admission did not run tests and are omitted above.

## Conflict resolutions

The required contract helper refused C-6.5, C-6.11 and C-8.4. Its non-overlapping edits were retained; only those named clauses were hand-integrated. Clause IDs were checked with `^- \*\*(C-\d+\.\d+)\*\*`: 255 unique IDs.

- Contract changelog hunk: required helper combined independent entries against the merge base, without duplicating clauses.
- Contract C-6.5 hunk: keep release refusal and resource-identity rules; add dependency recording once (the old branch duplicated that sentence).
- Contract C-6.11 hunk: keep release hold taxonomy and output identities; add the dependency needs role.
- Contract C-8.4 hunk: keep release cwd, rediscovery, job-directory, nested-lease and retention scheduling guarantees; replace the obsolete ASCII pin limit with folded dependency rules.
- retention-archive hunk 1: dependency places and pending exports supersede ASCII-only pin text.
- service hunk 1: keep release job-directory/worktree wording and folded folder comparison, add git-storage explanation.
- daemon hunk 1: union dependency and release imports.
- daemon hunk 2: retain depends and release unspelled metadata.
- daemon hunk 3: retain release early-look signature and semantics; add keyword needs.
- daemon hunk 4: retain release actual cwd/absent/source fence reads; append dependency fence reads.
- daemon hunk 5: combine early fence results and name first dependency role when applicable.
- daemon hunk 6: retain release rediscovery/current-folder flow and compute needs before it.
- daemon hunk 7: retain all release reservation blockers and cwd reader rows; append dependency blockers, excluding own key.
- daemon hunk 8: retain release source-folder attribution; otherwise name folded dependency and needs role.
- folders hunk 1: keep release threading import.
- folders hunk 2: keep release under_git and folded retiring API.
- retention hunk 1: union dependency and release process/TMPDIR imports.
- retention hunk 2: use Python dependency pin instead of both SQL self-joins.
- retention hunk 3: keep release nested worktree leases and jobs-directory turn pins; omit superseded SQL unrecorded pin.
- retention hunk 4: retain both legacy dependencies and orphan-retention lease state.
- retention_archive hunk 1: retain both canonical dependency recheck and release nested exclusive-lease recheck.

## Unbuilt and unrun: continuation 3

- Mutation work remains in progress: the table records completed holds and the resolved legacy-folder survivor; unrun rows are still open.
- The requested measurement and contract audit have not started.
- The firmlink fixture and all three guards are resolved (37 imported cases and 63 guard cases pass). Remaining correctness holds and the capped reference/monotonicity properties are still open in this continuation.

The full suite and Python 3.12 were not run on this lane. `tests/frontend/test_core_live.py` remains CI-only under the lane rules because its person-only probe cannot run here; these are CI scope, not extra local continuation 3 holds.

| Scale | Old pin-query time | New pin-query time | Measurement load |
|---|---:|---:|---:|
| 3,000 jobs / 100 live | not measured | not measured | not measured |
| 6,000 jobs / 600 live | not measured | not measured | not measured |

The pending property and spelling sketches are superseded by continuation 2's committed tests. Remaining mutation and measurement preparation still references old scratch helpers and requires revalidation and adaptation; it is not executed evidence.

## Mutation results: continuation 3 in progress

“Planned test” names the first selection. Observed results below come from separate serial holds; only selections from the first test file run together. Definitions and exact failures are retained in `c3-mutant-definitions.json`, `c3-mutants.json` and the per-hold JSON files in the data folder.

| Mutant | Planned test | Observed result / killing test |
|---|---|---|
| `submit-records-nothing` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` | killed by `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` |
| `live-ignores-depends` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` |
| `live-ignores-row-places` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` |
| `live-ignores-recorded-folder` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` |
| `live-ignores-spelled-folder` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_a_legacy_admission_folder_alone_pins_until_the_job_ends` (added deterministic test; red/green complete) |
| `live-ignores-exports` | `tests/unit/test_retention_dependency_units.py::test_d2_a_pending_export_pins_and_a_written_one_does_not` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_a_pending_export_pins_and_a_written_one_does_not` |
| `exports-only-recorded-path` | `tests/unit/test_retention_dependency_units.py::test_d2_a_pending_export_pins_and_a_written_one_does_not` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_a_pending_export_pins_and_a_written_one_does_not` |
| `no-fold` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` |
| `within-not-prefix-boundary` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` |
| `pin-ignores-unrecorded-tree` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | killed by `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` |
| `pin-counts-own-places` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | intended equivalent: earlier `active` / `job-lease` protection wins (consistent census) |
| `selection-no-dependents` | `tests/unit/test_retention_dependency_units.py::test_the_selecting_transaction_reads_dependents_on_the_fences_spelling` | killed by `tests/unit/test_retention_dependency_units.py::test_the_selecting_transaction_reads_dependents_on_the_fences_spelling` |
| `commit-no-dependents` | `tests/unit/test_retention_dependency_units.py::test_the_commit_reads_dependents_on_the_journals_spelling` | killed by `tests/unit/test_retention_dependency_units.py::test_the_commit_reads_dependents_on_the_journals_spelling` |
| `no-legacy-evidence` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_job_queued_before_dependencies_were_recorded_keeps_the_tree[linked]` | killed by `tests/fake/test_retention_dependencies.py::test_c8_4_a_job_queued_before_dependencies_were_recorded_keeps_the_tree[linked]` |
| `legacy-admission-reads-nothing` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_legacy_job_submitted_under_the_fence_waits_on_its_git_storage` | killed by `tests/fake/test_retention_dependencies.py::test_c8_4_a_legacy_job_submitted_under_the_fence_waits_on_its_git_storage` |
| `early-look-folder-only` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_job_submitted_under_the_fence_waits_while_a_place_it_needs_is_retired[linked-read-only]` | not run / none |
| `transaction-no-needs-blockers` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_fence_on_a_needed_place_taken_after_the_early_look_holds_the_job_in_its_transaction[linked-worktree]` | not run / none |
| `transaction-own-not-filtered` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_fence_on_an_in_place_writers_own_folder_that_its_output_is_in_is_named_once` | not run / none |
| `transaction-hold-names-no-place` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_fence_on_a_needed_place_taken_after_the_early_look_holds_the_job_in_its_transaction[output-read-only]` | not run / none |
| `git-storage-no-commondir` | `tests/unit/test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in` | not run / none |
| `git-storage-no-worktrees-fallback` | `tests/unit/test_retention_dependency_units.py::test_d1_storage_a_gitfile_names_counts_when_it_is_gone` | not run / none |
| `git-storage-no-alternates` | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | not run / none |
| `git-storage-alternates-one-level` | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | not run / none |
| `git-storage-gitfile-relative-to-cwd` | `tests/unit/test_retention_dependency_units.py::test_d1_a_separate_git_dir_and_a_relative_gitfile` | not run / none |
| `git-storage-nearest-ignores-gitfiles` | `tests/unit/test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in` | not run / none |
| `record-raw-spelling` | `tests/unit/test_retention_dependency_units.py::test_record_spells_each_place_once` | not run / none |
| `fences-not-retention-only` | `tests/unit/test_retention_dependency_units.py::test_d5_fences_reads_what_retiring_reads_place_by_place` | not run / none |
| `render-ignores-needs` | `tests/fake/test_retention_dependencies.py::test_c6_11_why_names_the_place_a_job_needs_in_the_tree_being_retired` | not run / none |
| `turn-reason-ignores-needs` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_turn_in_a_checkout_whose_repository_is_in_the_tree_waits_for_the_fence[READER]` | not run / none |
| `collate-nocase-restored` | `tests/unit/test_retention_folded_pin.py::test_a_job_id_typed_with_letters_the_volume_folds_keeps_the_tree` | not run / none |
| `record-no-output` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[output]` | not run / none |
| `record-no-git` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` | not run / none |
| `admission-no-needs-role` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_job_submitted_under_the_fence_waits_while_a_place_it_needs_is_retired[output-read-only]` | not run / none |
