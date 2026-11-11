# D-STORE-FREE1: offline store compaction

Built from release/217, `266c3ab8b0be2548fbe0abc77273fa1ccd7fa9a9`, in the assigned isolated worktree. Work began 2026-11-11 at 17:06 UTC; the deadline is 20:06 UTC. No live state or daemon was opened. All database probes used disposable synthetic stores.

The offline pass and daemon maintenance are implemented; all 47 offline tests and all 12 incremental tests pass, and all six mutants were killed. Six existing targeted regression tests still fail on timing/timeouts after a focused retry. The 1 and 2 GB measurements could not be completed inside the time cap. Git staging/committing is blocked by sandbox access to the parent repository's `index.lock`.

## Design

The operator entry point is `python -m subfleet.compact_store --state-root PATH`. It defaults to a dry run; `--apply` performs the pass. A separate module follows `prune_decisions`, requires no daemon, and requires an explicit state root. It constructs no writable `Store`, runs no migration, and deletes or changes no row, including `events`. This preserves C-3.2.

Both dry run and apply take the daemon's nonblocking `flock` on `daemon.lock` and reject any process holding the database, WAL or SHM open. An idle descriptor counts even without a SQLite transaction. Inspection failure is a refusal. The lock's contents are never written; a lock created by this pass is removed on exit. Existing files and lock contents survive refusals byte for byte.

A normal read-only SQLite connection can change SHM. Preflight therefore reads the main-file header and checksum-valid committed WAL frames directly, creates a private sparse schema-only snapshot in scratch, and opens that snapshot with `immutable=1`. It copies schema/version btree and overflow pages, rather than a second whole database. Schema checks cover the current version, columns, foreign keys, named indexes and their SQL expressions/predicates, CHECK constraints, and table flags. Column ordinals are ignored because additive migrations append columns in a different order from fresh DDL.

After admission, the pass takes SQLite's exclusive locking mode, folds and truncates the WAL, changes the old database to DELETE journal mode while retaining its exclusive lock, and fsyncs the old database. It checks the source's integrity so a corrupt index is refused rather than silently rebuilt with different answers. `VACUUM INTO` writes one same-directory hidden temporary output. The old connection stays exclusively locked through publication. Before publication the output must pass `integrity_check`, the build's schema check, every table's row count and SHA-256 digest of all typed values in primary-key order, and `auto_vacuum=2`. Tables with no declared key use all columns for deterministic ordering; internal tables are included. Source and target each have a bounded 256 MiB SQLite page cache for the offline verification reads.

The output's owner, group and permissions are preserved, then it is fsynced before atomic `os.replace`; its directory is fsynced afterwards. Old sidecars are removed only after checkpointing and leaving WAL mode, before replacement. No old WAL can be applied to the new file. Normal completion and Python exceptions remove temporary outputs and their sidecars. An abruptly killed pass can leave an untrusted hidden output; a subsequent apply removes it only after acquiring exclusive SQLite ownership and rebuilds rather than resuming it. The temporary filename prefix is reserved and cannot be selected as the retained original's name.

`--keep-original PATH` preserves the checkpointed old database by hard link, using an unused name on the same filesystem. It adds no full-size copy to peak use. That retained inode occupies the old database's space until the install kit removes it, so keeping it postpones reclamation of those bytes. By default only the compacted database remains.

## Crash table

The tests inject Python failures and abrupt child-process exit after the following boundaries. The old and new databases have identical logical content; mode/file size distinguish publication.

| Boundary | Authoritative path after interruption | Other possible files |
| --- | --- | --- |
| `checkpointed` | Old database, fully folded, DELETE mode | Existing `daemon.lock` |
| `built` | Old database | Unverified hidden compact output after abrupt exit |
| `verified` | Old database | Verified but not yet durably synced output |
| `file_synced` | Old database | Durable hidden output |
| `sidecars_removed` | Old database; WAL/SHM absent | Durable hidden output |
| `original_kept` | Old database | Named hard link and durable hidden output |
| `replaced` | Verified new database | Optional named old hard link |
| `directory_synced` | Verified new database, rename durably recorded | Optional named old hard link |

Before replacement the old path is never renamed away. Replacement is one atomic rename over an existing durable path. A process crash therefore leaves old or new, with no absent/mixed intermediate state. File and directory fsync ordering provides the durability protocol; process-failure injection is not a physical power-cut experiment. Every abrupt-crash case is followed by a successful retry and leftover cleanup.

## Space and auto-vacuum

Admission reports, in order, page count, freelist count, live bytes, reclaimable freelist bytes, needed bytes, and available bytes. The formula is:

```
live = (page_count - freelist_count) * page_size
margin = max(512 MiB, ceil(live / 50))
need = live + margin + existing_WAL_bytes
```

The output needs approximately live bytes plus pointer-map overhead. Margin also covers the sparse schema snapshot and filesystem rounding. `temp_store=MEMORY` avoids a disk sort file; its index-sort memory is additional to the two page-cache limits. There is one compact output and no output-sized WAL. Reclaimable freelist bytes are an estimate of the eventual return: pointer maps add overhead and rebuilding can also reduce partially filled btrees.

The requested initial command `df -k "$(getconf DARWIN_USER_TEMP_DIR)"` reported 95,127,132 KiB available (about 97.4 GB). The default temp-root uv cache was denied, so subsequent work used the permitted child `/private/tmp/store-compact-217` for cache and test basetemps. Benchmark admission reserves the 55 GB floor, refuses sources over 3 GB, and removes each source when its probe finishes.

The auto-vacuum propagation experiment started with mode 0, 252 pages and 125 free pages. Setting `PRAGMA auto_vacuum=INCREMENTAL` left the source reporting 0; `VACUUM INTO` produced 128 pages, zero free pages, and mode 2. This is the mechanism used by the pass. The evidence JSON records the interpreter and SQLite versions.

The 0.5 GB probe measured 122,421 pages and 25,231 free pages. It compacted 501,436,416 bytes to 398,450,688 bytes (97,278 pages, zero free pages, mode 2). Peak additional named-file allocation was 402,685,952 bytes, below the reported 934,961,152-byte need. The pass took 1,732.530 seconds on the heavily loaded host. This first measurement predates the final source-integrity/schema/cache refinements and includes a possible delayed fixture flush; it is an earlier-revision measurement, not an exact final-code runtime forecast. The shared-volume free-space delta was 29.46 GB, far larger than the files this probe created, and is recorded as unrelated shared-volume noise rather than attributed peak use.

An additional 0.05 GB probe against the exact final code completed in 213.112 seconds: 53,366,784 source bytes became 39,628,800 bytes. It started at 13,029 pages with 3,359 free pages and ended at 9,675 pages, zero free pages, mode 2; live bytes were 39,608,320 and reported need was 576,479,232 bytes. Peak extra allocation was 40,497,152 bytes, within that need. The source fixture was fsynced before timing. Swap milestones were checkpointed at 138.604 seconds, built at 204.284, verified at 212.136, output synced at 212.349, sidecars removed at 212.350, replaced at 212.463, and directory synced at 212.464. Its follow-up made three maintenance calls: two successful steps and one threshold skip, reclaiming 256 freelist pages and shrinking the file to 38,580,224 bytes. Mutex holds were median 507.18 ms/max 542.79 ms; whole-call maximum was 1,364.21 ms. This supplies final-code end-to-end content/space evidence without pretending it replaces the requested larger measurements.

The attempted 1 GB final-code fixture used a requested 90% freelist to reduce compaction time, but still did not finish construction after roughly 31 minutes. It was interrupted and cleaned up. The 2 GB fixture was not started before the time cutoff. Thus no elapsed/peak-space result is claimed for either requested large size. Raw evidence, explicit unfinished statuses, and the reproducible command are in `2026-11-07-store-compact-measurements.json` and `tools/benchmark_store_compaction.py`. Synthetic events use 32 KiB payloads to bound fixture construction; the live store's much larger index fraction makes runtime extrapolation especially uncertain.

For the supplied live store, 1,895,172 live pages mean 7,762,624,512 live bytes. Admission requires 8,299,495,424 bytes plus the existing WAL. Approximately 2,312 pointer-map pages add 9.47 MB, giving an output around 7.772 GB and a return around 2.069 GB. These are extrapolations; rebuilding partially filled btrees can also change the result. Scaling the earlier 0.5 GB elapsed time by live bytes gives roughly 9.4 hours under that run's host conditions and earlier cache/verification settings, not a prediction for an install window.

## Daemon maintenance

The writer reads `PRAGMA auto_vacuum` once at startup and caches whether it is INCREMENTAL. An existing probe cycle (normally every 60 seconds) attempts one step only when the freelist exceeds 1,024 pages. A step reclaims at most 128 freelist pages (512 KiB with 4 KiB pages), plus removable pointer-map bookkeeping pages, leaving a roughly 4 MiB churn reserve, eight measured-size batches. Mode-NONE and read-only stores issue no maintenance SQL. The startup feature read is the one new statement on a NONE writer; the zero-statement invariant/test concerns its maintenance calls.

The step takes the store mutex without waiting, uses `busy_timeout=0`, and runs one `BEGIN IMMEDIATE` / incremental-vacuum / `COMMIT` transaction. A cooperative 50 ms SQLite VM budget is checked every 64 instructions. Busy writers, cancellation, and close skip or roll back the step; the normal five-second busy timeout and progress handler are restored. There is no row mutation, audit event, or generation increment.

A PASSIVE checkpoint on a separate connection runs after the store mutex is released. This is necessary because tail-page reclamation can write too few WAL frames to trigger the ordinary automatic checkpoint. It lets a quiet database shrink while leaving admission's mutex available. Readers may safely delay checkpointing and file truncation.

The bounds are 128 freelist pages and 50 ms of cooperative VM execution, not a hard real-time wall-clock bound: SQLite FULL commit sync, individual I/O operations and OS scheduling cannot be preempted by a progress callback. The 0.5 GB follow-up made 256 calls: 106 successful steps and 150 budget skips reclaimed 13,568 freelist pages plus 16 pointer-map pages and took the file to 342,810,624 bytes. Mutex holds were median 98.64 ms, p95 650.99 ms, maximum 1,418.58 ms; whole-call maximum 2,793.02 ms includes checkpoint work outside the mutex. No measured mutex hold exceeded the existing two-second long-hold reporting threshold. An earlier small 128-page experiment measured median 7.286 ms/max 95.517 ms, versus a 256-page max of 407.039 ms. Loaded-host raw experiments reached much longer tails, strengthening the choice of 128. An unconditional maximum wall time cannot honestly be guaranteed by this synchronous SQLite design; the user's strict wall-time form of invariant 6 remains a limitation.

## Invariants and tests

| Invariant | Evidence/test |
| --- | --- |
| 1. Content: same rows/values, indexes answer the same queries | `test_compact_preserves_every_table_and_index_and_enables_incremental`; all-table digests and `integrity_check`; generated contents/delete sets |
| 2. Atomic: old or new at every crash point | `test_every_swap_crash_leaves_complete_readable_content` at all eight exported boundaries; fsync-before-rename test; retry cleanup |
| 3. Refusal: daemon, holder, schema skew, space shortage leave bytes unchanged | Daemon-lock, idle/transaction/raw-descriptor holder, older/newer/WAL-only schema, metadata/expression skew, and low-space tests |
| 4. Space: measured peak extra allocation stays below reported need | Completed 0.5 GB benchmark, named-file allocation sampling and filesystem-free observation; 1/2 GB measurements remain incomplete |
| 5. Shrinks: offline output approximates live pages; future deletes shrink | Content/property tests and `test_further_deletes_shrink_the_file_through_passive_checkpoints` |
| 6. Bounded steps | `test_each_step_reclaims_at_most_128_pages`, `test_vm_deadline_rolls_back_and_restores_connection`, `test_close_interrupts_a_step_before_closing_the_connection`; I/O limitation stated above |
| 7. Unchanged elsewhere | `test_none_store_issues_no_maintenance_statement` |

Core offline tests stub process inspection to isolate storage behavior and keep crash/property cases fast; separately marked integration tests run real holder inspection against test-owned files and processes. The independent test digests do not reuse the production verifier.

## Mutation tests

Each production-file mutant was applied alone, tested, and restored from a saved pristine copy; no `git checkout` was needed. Offline restoration SHA-256 was `1e1e8facc9dbe1d0f05f5ba656137a9ad6b23f035269a9417dcb72c2c03bafad` before and after the sweep. Later intended refinements are separate from those restorations. Store restoration SHA-256 was `64eff7b4a814f9e815b61209716ae1f9b892a6b348ef5670235782fbba90c696`.

| Mutant | Killing test and observed failure |
| --- | --- |
| 1. Omit digest comparison | `test_valid_output_with_changed_value_is_never_published`: DID NOT RAISE after a real valid output's event value was changed; 159.93 s |
| 2. Rename before output fsync | `test_temp_database_is_fsynced_before_rename`: `published before fsync`; 101.78 s |
| 3. Skip daemon check | `test_daemon_lock_refusal_changes_no_byte`: DID NOT RAISE with held daemon lock; 105.62 s |
| 4. Leave auto-vacuum NONE | `test_compact_preserves_every_table_and_index_and_enables_incremental`: output verifier rejects mode other than 2; 187.38 s |
| 5. Vacuum a NONE store | `test_none_store_issues_no_maintenance_statement`: unexpected `PRAGMA freelist_count`; 141.76 s |
| 6. Unbounded vacuum | `test_each_step_reclaims_at_most_128_pages`: the actual source mutant exceeded its enlarged execution budget and failed the progress assertion; a further isolated in-memory rerun deterministically failed the numeric SQL trace assertion on `incremental_vacuum(0)`; 89.00 s |

The offline sweep is reproducible with `tools/check_store_compact_mutations.py`; exact commands, failure excerpts and restoration hashes are saved in `2026-11-07-store-compact-mutants.json`.

## Command ledger and validation

Commands were run only in the assigned worktree and scratch. Exploratory reads used scoped `rg`, `sed`, `cat`, `ls`, and `tail` over these paths; no caller checkout, personal state, provider process, or live database was inspected. Repeated commands below differ only in the fresh basetemp/log path.

| Command | Result |
| --- | --- |
| `pwd; git status --short; git branch --show-current; git rev-parse HEAD; git show-ref --heads fix/store-compact-217; git symbolic-ref -q HEAD` | Clean detached source at the requested base; assigned branch already exists at that base |
| `getconf DARWIN_USER_TEMP_DIR; df -k "$(getconf DARWIN_USER_TEMP_DIR)"; date -u ...` | Temp path and initial free space recorded above; start 17:06 UTC |
| Scoped `rg --files` / `rg -n` and source/test/contract reads | Located Store, daemon flock, pruning pattern, schema migrations, timer and targeted tests; no worktree AGENTS.md found |
| `git switch -c fix/store-compact-217` | Refused: branch exists |
| `git switch fix/store-compact-217` | Refused: branch is occupied by the caller's worktree; detached checkout preserved |
| `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=$TMPDIR/uv-cache uv sync --group dev` | Default cache denied by sandbox |
| `TMPDIR=/private/tmp/store-compact-217 UV_CACHE_DIR=$TMPDIR/uv-cache uv --cache-dir "$UV_CACHE_DIR" sync --group dev` | Passed; initially selected Python 3.15.2 |
| Same sync with `--python /opt/homebrew/bin/python3.13` | Passed; Python 3.13.15 used after Hypothesis import/Unicode initialization problems on 3.15 |
| `uv run --no-sync python -m py_compile subfleet/compact_store.py` | Passed |
| Initial offline pytest run | Fixture lacked required decisions.policy_hash; fixed; run interrupted during Hypothesis initialization |
| Initial incremental pytest run | 10 passed, 1 failed; exposed absent physical shrink without explicit checkpoint; fixed |
| Incremental tests, `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 tests/unit/test_store_incremental_vacuum.py --basetemp ...` | 12 passed |
| Final incremental module, `uv run --no-sync pytest -q tests/unit/test_store_incremental_vacuum.py --basetemp ... --timeout=300` | 12 passed in 124.44 s after reducing fixture Python/SQLite round trips; the two former timeout cases also passed separately in 101.20 s |
| Same two incremental tests during low-disk interval | 2 skipped by the 55 GB safeguard; observed free space 54.1 GB; later free space recovered to 64.1 GB before the successful rerun |
| Final offline module, `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q tests/unit/test_compact_store.py -n 4 --tb=short --basetemp ... --timeout=300` | 47 passed in 703.49 s, including all five real holder checks and eight abrupt crash points |
| Offline intermediate runs | Python 3.15 Hypothesis import error; unbatched fixture run 7 timeout failures/2 passed; batched but larger fixture run 22 passed/1 timeout; one final startup was interrupted to avoid importing a mutant. Final fixture is batched, smaller and fully verified |
| Synthetic `lsof -nP -F p -- ...` smoke | Timed out at 30 seconds; refused safely |
| Synthetic native `proc_listpidspath` experiment | Empty-holder scan took 92.48 seconds; no production dependency added |
| Synthetic `lsof -nPl -F p -- ...` smoke | Correct empty result in 42.99 seconds; production uses numeric UID output and a 180-second inspection timeout |
| Direct synthetic empty-store apply smoke | Passed; output integrity/digests matched and auto_vacuum=2 |
| Direct synthetic populated-store apply smoke | Passed; 3,186,688-byte source compacted to 1,814,528 bytes with mode 2 |
| `uv run --no-sync python tools/check_store_compact_mutations.py --scratch-root ... --output docs/reports/2026-11-07-store-compact-mutants.json` | All four offline mutants killed; exact pristine bytes restored |
| Isolated daemon mutant sweeps | Both killed; pristine file bytes restored; deterministic unbounded-SQL rerun also killed without touching shared production source |
| `uv run --no-sync python -m subfleet.compact_store --help` and compilation of all seven new/changed Python code/test/tool modules | Passed |
| Standalone Hypothesis UTF-8 cache warmup in scratch | Passed in 388.99 s, explaining why a cold 120-second test bound was unsuitable on this host |
| Completed root/agent synthetic-basetemp cleanup | Removed only owned completed test trees; offline suite confirmed no SQLite files left |
| `uv run --no-sync python tools/benchmark_store_compaction.py --sizes-gb 0.01 ...` | Initial smoke refused safely on the original 30-second holder-inspection timeout; fixture removed |
| Initial 0.5/1/2 GB benchmark command, 1 KiB events | Interrupted during fixture construction; cleaned up; no measurement claimed |
| Revised 0.5/1/2 GB command, 32 KiB events, reused experiments and owned PID file | 0.5 GB measurement completed; interrupted during the next fixture; completed result preserved and scratch removed |
| Final-code `--sizes-gb 1 2 --freelist-fraction 0.9 --incremental-steps 32 --append-probes --reuse-experiments ...` | 1 GB construction interrupted at time cutoff; 2 GB not started; both statuses saved explicitly |
| Final direct-development-Python `--sizes-gb 0.05 --incremental-steps 8 --append-probes --reuse-experiments ...` | End-to-end final-code probe completed in 213.112 s; all owned benchmark processes, scratch trees and PID files removed |
| Tool-session Ctrl-C cancellation and attempted signal to recorded owned benchmark PID | Early cancellation was delayed in SQLite/native I/O; cross-exec signal/inspection was sandbox-denied. No unrelated process was targeted. All benchmark sessions eventually exited and were cleaned up before 19:55 UTC |
| `git diff --check` | Passed during implementation and final verification |
| `git add subfleet/store.py subfleet/timers.py tests/unit/test_store_incremental_vacuum.py` | Denied: parent repository index.lock outside writable sandbox; no commit possible; files retained for salvage |
| `git add subfleet/compact_store.py tests/unit/test_compact_store.py docs/acceptance-contract.md; git commit -m 'Add verified offline store compaction and incremental mode'` | Both denied by the same index.lock restriction |
| Named-file staging attempts for the final offline and incremental test files | Denied by the same index.lock restriction |
| Final named-file staging of all 15 code/test/contract/report/evidence files | Denied by the same index.lock restriction; nothing staged or committed |

The required regression command, with the scratch/cache environment above, was:

```sh
/usr/bin/lockf -k -t 60 /private/tmp/claude-501/subfleet-suites.lock \
  uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 \
  tests/unit/test_store_incremental_vacuum.py \
  tests/unit/test_store_readers.py tests/unit/test_store_contention_queries.py \
  tests/unit/test_timers_retention.py tests/fake/test_native_maintenance_startup.py \
  tests/fake/test_state_contract.py tests/unit/test_test_module_names.py \
  tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py \
  tests/unit/test_prune_decisions.py tests/unit/test_prune_restore.py \
  tests/unit/test_timers_probe.py \
  --basetemp "$TMPDIR/pytest-regression-final-1" --timeout=120 --tb=short \
  --junitxml "$TMPDIR/regression-final-1.xml"
```

It ran with four workers and a fresh basetemp: **836 passed, 15 failed in 2,623.49 seconds**. Every failure was a timeout, an elapsed-time assertion, or Hypothesis cache initialization/flakiness after a timeout; there was no storage-content assertion failure. Two new incremental tests timed out while digesting synthetic fixtures (one before the step, one after shrink assertions); both and the full 12-test module pass with smaller fixtures retaining more than 1,500 free pages and all digest/bound checks. The remaining failures were three reader timing tests, five handler-refusal property/AST tests, one socket timeout, one prune-restore timeout, one prune-report Hypothesis initialization timeout, and two probe timing tests.

A two-worker retry of those 13 existing failures, with `--timeout=300`, produced **7 passed, 6 failed in 1,784.52 seconds**. The unresolved tests are `test_a_long_snapshot_holds_the_wal_back_without_holding_a_commit_and_it_shrinks_after`, `test_every_refusal_the_daemon_builds_names_a_known_reason`, `test_a_refusal_reads_its_most_specific_checked_form_and_never_its_exception`, `test_c16_1_state_socket_handler_recovers_after_malformed_line`, `test_restore_recipe_preserves_backup_and_displaced_files`, and `test_usage_timeout_does_not_block_cycle_or_shutdown`. Their failures are timeout/latency results, including Hypothesis character-strategy initialization; the probe latency assertion measured 4.27 seconds against 0.4 seconds. These remain failed validation, not a clean-suite claim. Both runs' logs, the offline success log and machine-readable counts are preserved under `2026-11-07-store-compact-data/`.

All new functional tests pass. The full suite was not run, as requested. The larger performance measurements and the remaining existing regression failures require a quieter host or a longer follow-up window.

## Install-kit responsibilities

Stop the daemon and prevent automatic restart, close every store reader, invoke dry run and inspect the ordered space report, then run `--apply`. Decide whether to preserve the original with `--keep-original` and when to remove it to realize the space return. Capture stdout outside the state root if a durable operator report is desired. Restart only after successful verification/publication. A killed pass may be retried while still stopped; it never treats a leftover output as verified. This build changes no retention policy or application write rate.

Before release, complete the 1/2 GB measurements and resolve or reproduce the six remaining regression timing failures. The install kit must also accept the stated cooperative work budget versus uninterruptible I/O limitation; this implementation does not establish a universal wall-clock write-lock guarantee.

Git cannot stage or commit through the read-only parent metadata. The caller's occupied branch and this detached HEAD were left intact; Subfleet can collect the named changes. No history was rewritten and nothing was pushed.

Built head: `266c3ab8b0be2548fbe0abc77273fa1ccd7fa9a9`
