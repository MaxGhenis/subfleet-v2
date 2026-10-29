VERDICT #272: REQUEST_CHANGES

Reviewed head: `dd0e4ea8def74285c4aaa581be93a46ca87ca3ba`; whole PR base:
`e8ecb42e7` (`release/217`); fix comparison: `3893c7120..dd0e4ea8d`.
Started 2026-10-29 06:21:11 UTC; first hold queued 06:22:20 UTC.
The four-hour stop is 10:21 UTC and the six-hour wall is 12:21 UTC.

The original review r1 reproducers pass, including its separate pre-existing
job-directory boundary. Two further incomplete-discovery gaps remain.
Production code and the reviewed documentation were left unchanged.

## Findings, ranked

1. **P2, R2-F1 — real alternates changes during discovery are not detected.**
   `subfleet/dependencies.py:230` opens a regular file, reads its descriptor to
   EOF, and accepts the result without checking the descriptor or pathname for
   changes. An ordinary unlink does not make a read from an open descriptor
   raise. The existing disappearance test manufactures a `FileNotFoundError`;
   it does not execute this behavior.

   Executed real atomic replacement and in-place rewriting after the first
   `readline`. Both leave complete-looking dependency evidence without the
   required object directory. Immediately after recording, installed Git reads
   a commit available only inside the owner's worktree. Maintenance nevertheless
   returns `pruned: ['owner']`. A third input unlinks the open file; discovery
   reaches EOF without recording `incomplete`.

   Regressions:
   `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace]`,
   `[rewrite]`, and `test_c8_4_unlinked_open_alternates_is_incomplete`.
   Fix: check descriptor identity/size/mtime/ctime before and after the read and
   compare the final pathname's identity as well; disappearance, replacement or
   a detected write makes discovery incomplete. Keep all this filesystem work
   outside transactions. The in-memory stability-check repair passes all three
   inputs with `--runxfail`. This concerns changes during discovery; it does not
   claim to cover arbitrary dependencies introduced after submission.

2. **P2, R2-F2 — legacy incomplete evidence disappears for terminal exporters.**
   `subfleet/dependencies.py:328` discovers legacy evidence only for `_LIVE`
   jobs. At `:377`, the exporter loop carries recorded `depends.incomplete` but
   does not carry `legacy_places.incomplete`. Thus finishing a legacy job after
   its census loses an already-known failure while its `out:` lease remains.
   Subsequent passes cannot rediscover it either, because terminal exporters
   are absent from the legacy discovery query.

   Executed a malformed alternates file, a legacy `job.submitted` without
   `depends`, and an output lease outside the candidate owner. The database
   recheck loses the named file. Both a live-to-terminal transition immediately
   after the census and an already-terminal pending exporter produce
   `pruned: ['owner']`, contrary to decision 4's global fail-closed rule.
   These three inputs are the `database`, `full-pass` and `already-exporting`
   parameters of
   `test_c8_4_legacy_incomplete_census_survives_terminal_export`.
   Fix: include legacy pending exporters in off-transaction discovery and
   carry their incomplete evidence through the exporter loop, or persist an
   equivalent durable record. The in-memory repair passes all three inputs.
   This is separate from the fixed `jobs.out_path` destination join.

The six failing inputs are marked strict expected failures naming R2-F1/R2-F2.
An independent `--runxfail` hold executes all six as real failures; the trial
repairs execute them as six passes. No product repair was applied.

## Checks 1–7

| Check | Verdict | Executed inputs and evidence |
|---|---|---|
| 1. Review r1 findings 1–4 and job directory | PASS for the original findings | All 18 unchanged r1 cases pass: absolute/relative octal alternates, valid >64 KiB alternates, modern/legacy terminal exports in dotless-i/default-ignorable roots, and eleven spelling controls. All 38 imported review checksums match. The original `job-directory-probe.py.txt` was copied byte for byte into scratch and executed directly: one pass. The existing 12-case job-directory file also passes. The old timing sentence is corrected at head. |
| 2. Git C quoting | PASS, with conservative differences | Installed Git 2.55.0 executed all standard C escapes, octal ASCII and UTF-8, an octal invalid-UTF-8 byte, trailing backslash, unterminated quote, text after closing quote, `#` inside a quoted path and after an entry, and quoted/raw CRLF. Added 15 Git-backed cases pass. The original 23-case alternates file, including the 100-generated/3-explicit quoting property, passes. No decoder difference observed can retire a tree Git reads: malformed/NUL cases instead fail closed. R2-F1 concerns concurrent file discovery, not quote decoding. |
| 3. Incomplete discovery | FAIL | The existing 19 cases pass: unreadable files, injected interrupted reads, malformed quotes, FIFO, overlong line, live/legacy/recorded-export states, admission and database-only rechecks. Actual unlink/replacement/rewrite and legacy exporter probes expose R2-F1/R2-F2. A 2 MiB file of repeated short valid lines and a cycle across five alternate levels pass. The input buffer is bounded; results remain proportional to distinct paths. |
| 4. Exporter join and cost | PASS for the join; R2-F2 applies to incomplete metadata | Terminal modern/legacy exports under both special spellings pass. Added NULL `jobs.out_path` controls retain the lease path or recorded output independently, then release the pin with the lease. They execute database rechecks with filesystem discovery/canonicalization helpers forbidden. Join is `jobs.job_id = leases.holder`; the measured plan uses `sqlite_autoindex_jobs_1`, and submission lookups use `events_job`. One 3,000/100 run: old SQL 305.836 ms, current pin read 27.578 ms (0.09017×), 22 matching pins, 147,801 events, query load 29.261/24.992/23.414. No repetitions or tuning. |
| 5. Job-directory pin and canonical fence | PASS | Original probe; 12 existing census/fence/commit, three-role, pending-export and symlinked-parent cases; nine new nested dependency cases across census/fence/commit and case/NFC/Greek aliases; a turn job preserving `turn-folder` precedence. Late commit tests disable the general pin callback to isolate the commit's own recheck. `6f54e9ee6` saves canonical `job_folder` before the transaction and reads that same spelling before the final transaction; old journals' fallback spelling is also computed off the lock. |
| 6. Text | CHANGES REQUIRED | C-6.5, C-6.11 and C-8.3 describe their implemented changes correctly. Keep C-8.4's normative guarantee and I16's intended invariant; the implementation does not meet the incomplete-discovery promise for the new inputs. The descriptive changelog, desktop dependency paragraph, main report, c4a resolution, fix report and live PR body need the qualifications below. Historical counts and timings remain dated evidence. |
| 7. CI | PENDING | The single allowed `gh pr checks 272` read returned 8 at approximately 06:24 UTC: pending 10, pass 0, fail 0, skipping 0, cancel 0. All ten are Python 3.12/3.14 macOS shards, five each. This is consistent with reported macOS runner congestion; the snapshot alone does not distinguish queued from running. Neither intermittent D-GLIDE-CI1 frontend failure appears. |

## Executed Git comparison

The scratch probe gives the borrower a blob available only in the source
objects. Successful `cat-file -p` proves Git used that alternate.
Exact bytes, stdout/stderr, resolved paths and incomplete reasons are in
`2026-10-29-retention-pins-review-r2-data/git-compare.json`.

| Input | Installed Git | Reader / retirement consequence |
|---|---|---|
| `\a`, `\b`, `\f`, `\n`, `\r`, `\t`, `\v`, `\\`, `\"` | Reads intended object directory for every escape | Same canonical object directory |
| `\157`, `\303\251` | ASCII o, UTF-8 é | Same bytes before filesystem decoding |
| `\377` | Decodes byte ff; object lookup fails because the host refuses creating that filename | Returns the same surrogate-escaped byte path. Actual byte-ff filename creation was attempted and rejected; no successful byte-ff object access is claimed. |
| Trailing backslash / unterminated quote | Falls back to the literal malformed entry, then fails object lookup for these inputs | Marks incomplete; keeps allocated trees |
| Closing quote followed by `xtrailing` | Reads first path; skips x, also parses trailing | Same two paths |
| `#` inside quotes | Literal path component; reads object | Same |
| Quoted entry followed by ` # ignored` | Ignores the comment | Same |
| Quoted entry with CRLF | Reads intended path | Same |
| Unquoted entry with CRLF | CR remains a path byte; absent such a directory, lookup fails | Same CR-suffixed path |
| Extra `\000junk` after an existing object path | Git uses the prefix through NUL and reads the object | Python rejects embedded NUL; incomplete evidence conservatively keeps trees |

Existing tests also rerun quoted absolute/relative paths, spaces, blank lines,
comments, malformed short/large octal and the Git-generated quoting property.
No production Git subprocess was introduced.

## Bounds, changes and exporter evidence

`_ALTERNATE_LINE_LIMIT` is 1,048,576 bytes; `readline` requests at most
1,048,577 bytes to detect overflow. The bound is 256 times the 4096-byte
reference PATH_MAX and 1024 times this host's measured 1024-byte PATH_MAX,
well above four-byte octal expansion of such a path. Files have no whole-file
cap. A single overlong line fails closed rather than truncating. The result
set and frontier grow with distinct dependencies; the memory claim concerns
streamed input and deduplicated repeated paths, not constant memory for an
unlimited number of distinct dependencies.
The observed repeated-line input was 2,097,264 bytes with one distinct result;
peak traced allocation was 135,665 bytes. The final regression run checks both
a five-node cycle within the traversal bound and a dependency at all five
alternate levels.

Real-change probes use actual open regular files and actual replacement,
truncating write or unlink, without synthetic read exceptions.
The cycle inputs use five and six nodes including the initial objects directory.
The five-node case exercises deduplication when the fifth hop returns to the
start; the six-node case reaches a dependency at the full five-alternate bound.
Git reads the final source commit in each case; discovery yields the four or
five distinct alternates exactly once.

For recorded incomplete evidence and live legacy jobs, existing tests check
that the reason includes job id and filename, all allocated worktrees remain,
and ending the job and releasing the export lease permits retirement.
Admission still admits an incompletely discovered job. The new failures
identify where those promises stop holding.

The single measurement uses the prior tool, with only its import path adapted
for scratch. It is an end-to-end pin-read comparison, not an isolated estimate
of join overhead. Ten export leases are included. The SQL retains its lease-key
range predicate; after ANALYZE on this small table, SQLite chose `SCAN leases`.
The job join uses the primary key and each event lookup uses `events_job`.

## Corrected text and PR-body replacements

Preserve the normative C-8.4 requirement and intended I16 invariant. Fix the
implementation; do not weaken the requirement to authorize these retirements.
Add this implementation-status qualification to explanatory material and
current descriptive status paragraphs:

> Review r2 at `dd0e4ea8d` confirms that review r1's original reproducers and the job-directory boundary pass. Two incomplete-discovery gaps remain: real unlink, replacement or rewriting of alternates during discovery is not detected, and legacy incomplete evidence is omitted for terminal pending exporters. Until R2-F1 and R2-F2 are fixed, the fail-closed guarantee is not met for those inputs.

| Text at reviewed head | Assessment / correction |
|---|---|
| `docs/acceptance-contract.md:449` C-6.5 | Dependency recording, output identity separation and job-directory fences are true; no correction needed. |
| `docs/acceptance-contract.md:455` C-6.11 | Shared key, folder/needs attribution and job-directory wording are true; no correction needed. |
| `docs/acceptance-contract.md:475` C-8.3 | Actual job-row, lease and recorded output pins, including legacy exporters and NULL fallbacks, are true; no correction needed. |
| `docs/acceptance-contract.md:477` C-8.4 | Incomplete-discovery requirement is not fully met. Keep it and add the qualification above as an implementation-status note. Explicitly include a detected change of identity or contents during discovery in the definition of incomplete. |
| `docs/acceptance-contract.md:9` fix-round changelog | Replace “incomplete discovery admits jobs but keeps allocated worktrees while the job is live or exporting, naming the job and file” with “incomplete discovery admits jobs and pins allocated worktrees for recorded evidence and live legacy jobs; review r2 identifies file-change detection and terminal legacy incomplete-evidence gaps that still require fixes.” Other dated changelog lines remain historical and true. |
| `docs/desktop/retention-archive.md:631` dependency description | Replace the unreadable/interrupted-file and global-pin sentences with: “Read errors, overlong lines and invalid quotes record incomplete discovery; recorded failures and live legacy failures pin allocated worktrees with the job and filename. Review r2 found that ordinary unlink, replacement and rewriting during discovery can escape detection, and legacy incomplete evidence does not survive a terminal pending-export state. Submission and admission still accept the job.” |
| `docs/desktop/retention-archive.md:658` former line 639 timing sentence | True: both continuation 4b comparisons are completed and linked. No correction. |
| `docs/desktop/retention-archive.md:865` I16 | Keep the invariant. Add the qualification after the evidence link; its fail-closed implementation claim at `:874` currently has the two exceptions. |
| `docs/reports/2026-10-27-retention-dependency-pins.md:3` status | Replace with: “Review r1's original findings and the separate job-directory boundary pass review r2 at `dd0e4ea8d`. Review r2 requests changes for R2-F1 and R2-F2 in incomplete discovery; CI has ten pending macOS shards in the single snapshot. Earlier counts and timings remain dated evidence.” |
| Same report `:7`, `:13`, `:17` | Original-finding resolution and historical counts are true. Add the shared qualification after the summary. Replace the unconditional sentence at `:13` with: “Recorded incomplete evidence and live legacy evidence keep allocated worktrees; file changes during discovery and terminal legacy exporters remain gaps identified in review r2.” |
| `docs/reports/2026-10-29-retention-pins-c4a-audit.md:7` resolution | Original three fixes and boundary resolution are true. Append the shared qualification; do not undo D1 or original r1 resolutions. |
| `docs/reports/2026-10-29-retention-pins-fix-r1.md:10` decision 4 | Replace the disappearance/evidence/global-pin sentences with: “Read errors, FIFOs, oversized lines and malformed quotes record incomplete evidence. The disappearance test injected a FileNotFoundError; it did not exercise an ordinary unlink of an open file. Recorded incomplete evidence and live legacy evidence survive database rechecks, but review r2 found real file-change detection and terminal legacy exporter gaps. Admission still proceeds; C-8.4's full fail-closed requirement remains to be met for those inputs.” |
| Same report `:12` decision 6 | Append the shared qualification to the current-text claim. Original resolved findings and dated evidence remain unchanged. |

The live PR body was read exactly once. Replace its line 1 status paragraph with:

> **Review r2 at `dd0e4ea8d`: REQUEST_CHANGES.** Fix round r1 resolves review r1's original dependency reproducers, stale timing sentence and the separate release/217 job-directory boundary. Two incomplete-discovery gaps remain: real alternates changes during discovery can omit a tree Git reads (R2-F1), and terminal legacy exporters lose incomplete-discovery protection (R2-F2). The new regression file keeps these cases as strict expected failures; separate executed repairs pass them. The exporter destination join and canonical job-directory rechecks pass. CI's single snapshot has ten pending macOS shards and no failed bucket. Retention remains dormant.

Other PR-body changes, using saved-body line numbers:

- Line 3: “Status at `dd0e4ea8d` after review r2 (2026-10-29)”.
- Line 4: replace the unconditional retention sentence with “The dependency census pins recorded folders, review roots, output destinations and discovered Git storage; the two incomplete-discovery exceptions in the status paragraph remain open.” Keep the verified admission and shared-key statements.
- Line 5: retain verified transaction, output destination, retention-only fence, ownership and legacy-fold statements; append “The full incomplete-discovery guarantee remains open for R2-F1 and R2-F2.”
- Line 7: label 169 nodes as continuation 5's dated evidence, rather than “at the head”; link this review's final hold ledger and regressions.
- Line 18: “Retention pins the folders and dependency places in its census and admission fences those places; the current incomplete-discovery exceptions are listed above.”
- Line 29: replace the last-known-gap sentence with “This PR addresses the dependency gaps required before re-enablement; review r2's remaining findings and the merge gates must be resolved first.”
- Line 34: “Resolve review r2's R2-F1 and R2-F2, then rerun their named regression cases and obtain an approving review.”


## Final test accounting

All 159 prescribed holds completed. Counts are distinct nodes using final
ordinary source runs; red diagnostic holds, mutants and trial repairs are
excluded. The new fake file's final full run has 15 passes and six strict
expected failures; the new unit file has 15 passes. The scratch copy of the
original job-directory probe adds one passing node outside the repository.
The 543 prescribed nodes all pass. Total including added tests and the original
scratch probe: **574 passed, 6 strict expected failures**;
no unexpected final failure, ordinary skip or unfinished prescribed node.

P/F/S/X/U below means passed/failed/skipped/expected-failure/unrun. First starts
are observed after lock admission, all on 2026-10-29 UTC. Every split and all
three load averages are retained in the complete hold table.

| File | P/F/S/X/U | First counted start UTC | Load (1/5/15 min) |
|---|---|---|---|
| `test_job_directory_probe.py` | 1/0/0/0/0 | 06:35:31.657 | 26.12 / 23.73 / 23.09 |
| `tests/fake/test_detached_retention_fence.py` | 89/0/0/0/0 | 06:44:12.880 | 18.87 / 21.33 / 22.42 |
| `tests/fake/test_retention_dependencies.py` | 30/0/0/0/0 | 06:23:09.875 | 14.59 / 19.71 / 21.99 |
| `tests/fake/test_retention_dependency_incomplete.py` | 19/0/0/0/0 | 06:22:28.445 | 18.42 / 21.13 / 22.58 |
| `tests/fake/test_retention_dependency_review_r1.py` | 18/0/0/0/0 | 06:22:28.445 | 18.42 / 21.13 / 22.58 |
| `tests/fake/test_retention_dependency_review_r2.py` | 15/0/0/6/0 | 06:38:45.040 | 28.43 / 25.32 / 23.83 |
| `tests/fake/test_retention_dependency_spelling.py` | 16/0/0/0/0 | 06:23:23.793 | 14.40 / 19.51 / 21.89 |
| `tests/fake/test_state_contract.py` | 37/0/0/0/0 | 06:48:22.723 | 15.22 / 18.37 / 20.84 |
| `tests/fake/test_writer_retention_fence.py` | 23/0/0/0/0 | 06:45:42.353 | 16.10 / 19.45 / 21.56 |
| `tests/unit/test_invariants_index.py` | 25/0/0/0/0 | 06:48:46.623 | 14.05 / 17.83 / 20.57 |
| `tests/unit/test_retention_alternates.py` | 23/0/0/0/0 | 06:22:38.250 | 17.15 / 20.76 / 22.44 |
| `tests/unit/test_retention_alternates_review_r2.py` | 15/0/0/0/0 | 06:29:34.585 | 29.24 / 25.06 / 23.45 |
| `tests/unit/test_retention_archive.py` | 109/0/0/0/0 | 06:32:57.771 | 16.67 / 22.02 / 22.53 |
| `tests/unit/test_retention_cwd_rechecks.py` | 3/0/0/0/0 | 06:44:09.617 | 19.55 / 21.51 / 22.49 |
| `tests/unit/test_retention_dependency_properties.py` | 4/0/0/0/0 | 06:24:02.199 | 13.69 / 18.70 / 21.48 |
| `tests/unit/test_retention_dependency_units.py` | 19/0/0/0/0 | 06:23:35.369 | 13.93 / 19.16 / 21.72 |
| `tests/unit/test_retention_folded_pin.py` | 38/0/0/0/0 | 06:23:32.701 | 14.19 / 19.29 / 21.78 |
| `tests/unit/test_retention_job_dependencies.py` | 12/0/0/0/0 | 06:23:15.129 | 14.78 / 19.67 / 21.96 |
| `tests/unit/test_retention_shared_folders.py` | 62/0/0/0/0 | 06:29:16.019 | 23.96 / 23.78 / 22.97 |
| `tests/unit/test_retention_turn_workdirs.py` | 1/0/0/0/0 | 06:44:06.619 | 19.55 / 21.51 / 22.49 |
| `tests/unit/test_retention_turns.py` | 7/0/0/0/0 | 06:47:12.190 | 15.54 / 19.31 / 21.36 |
| `tests/unit/test_retention_worktree_cwd.py` | 7/0/0/0/0 | 06:47:15.770 | 14.93 / 19.12 / 21.29 |
| `tests/unit/test_test_module_names.py` | 1/0/0/0/0 | 06:46:13.382 | 24.24 / 21.23 / 22.14 |

## Properties at normal settings

The four required properties retain their normal phases, maxima and strategies:
Unicode attribution 100 generated + 1 explicit, independent reference 100,
monotonicity 100, fence differential 400 generated + 12 explicit.
The Git-quoted path property completes 100 generated + 3 explicit.
The observer records inner engine counts without changing ordinary settings.
Named mutants use explicit/generate phases and stop at the first failure,
as in fix round r1; repairs select only deterministic new regression cases.

| Property node | Generated valid | Explicit | Settings maximum |
|---|---:|---:|---:|
| `tests/unit/test_retention_alternates.py::test_c8_4_git_quoted_generated_paths_resolve_to_the_same_canonical_objects` | 100 | 3 | 100 |
| `tests/unit/test_retention_folded_pin.py::test_the_pin_keeps_a_tree_exactly_when_the_folded_fence_holds_the_job` | 500 | 3 | 500 |
| `tests/unit/test_retention_folded_pin.py::test_dependency_hold_names_every_place_the_canonical_caseless_query_blocks` | 100 | 1 | 100 |
| `tests/unit/test_retention_folded_pin.py::test_on_the_volume_the_pin_keeps_a_tree_exactly_when_the_fence_holds_the_job` | 150 | 0 | 150 |
| `tests/unit/test_retention_dependency_units.py::test_d1_git_storage_includes_what_git_uses_for_any_layout` | 12 | 0 | 12 |
| `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | 150 | 0 | 150 |
| `tests/unit/test_retention_dependency_units.py::test_d4_a_job_that_has_not_ended_never_releases_a_pin_and_ending_one_never_adds_one` | 60 | 0 | 60 |
| `tests/unit/test_retention_dependency_units.py::test_d5_fences_reads_what_retiring_reads_place_by_place` | 200 | 0 | 200 |
| `tests/unit/test_retention_dependency_properties.py::test_selection_never_retires_a_tree_any_live_job_needs` | 100 | 0 | 100 |
| `tests/unit/test_retention_dependency_properties.py::test_python_pins_and_transaction_agree_with_independent_reference` | 100 | 0 | 100 |
| `tests/unit/test_retention_dependency_properties.py::test_live_dependents_are_monotone_and_the_last_ending_allows_retirement` | 100 | 0 | 100 |
| `tests/unit/test_retention_dependency_properties.py::test_submitted_dependencies_wait_in_any_spelling_then_are_admitted` | 24 | 1 | 24 |
| `tests/unit/test_retention_shared_folders.py::test_early_fences_equal_reservation_before_and_after_quarantine` | 400 | 12 | 400 |
| `tests/unit/test_retention_shared_folders.py::test_fence_hold_against_a_model_of_the_fence` | 150 | 5 | 150 |
| `tests/unit/test_retention_shared_folders.py::test_the_turn_folder_pin_is_exactly_an_owned_tree_a_turn_is_in` | 150 | 0 | 150 |
| `tests/unit/test_retention_shared_folders.py::test_interleavings_never_delete_a_folder_named_by_a_turn` | 40 | 9 | 40 |
| `tests/fake/test_detached_retention_fence.py::test_retiring_folded_is_retention_on_the_folder_or_above_with_names_folded` | 300 | 4 | 300 |
| `tests/fake/test_detached_retention_fence.py::test_interleavings_never_run_a_detached_job_in_a_tree_being_retired` | 12 | 5 | 12 |
| `tests/fake/test_writer_retention_fence.py::test_c8_4_c6_5_writer_reservation_agrees_with_an_oracle_on_any_leases` | 60 | 5 | 60 |
| `tests/fake/test_detached_retention_fence.py::test_c8_4_c6_5_detached_reservation_agrees_with_an_oracle_on_any_leases` | 80 | 6 | 80 |

## Named mutant table

Fourteen prescribed definitions were replayed against their named killers,
plus four explicitly named probes for the new escape, line-streaming, join-key
and final-directory-check coverage. Every replacement matched once, compiled,
and was observed loading its intended module. All eighteen are killed.
The two proof repairs are separate successful experiments, not surviving
mutants: R2-F1 3 passed, R2-F2 3 passed with expected-failure marks disabled.
The separate original-source diagnostic hold has 6 failed / 14 deselected.

| Named mutant | Result / observed killing node | P/F/S/X/U | Start UTC | Load (1 min) |
|---|---|---|---|---|
| `git-storage-no-commondir` | KILLED: `tests/unit/test_retention_dependency_units.py::test_d1_commondir_outside_the_worktrees_layout_is_followed` | 0/1/0/0/0 | 06:25:25.188 | 14.24 |
| `git-storage-no-worktrees-fallback` | KILLED: `tests/unit/test_retention_dependency_units.py::test_d1_storage_a_gitfile_names_counts_when_it_is_gone` | 0/1/0/0/0 | 06:25:31.982 | 14.62 |
| `git-storage-no-alternates` | KILLED: `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | 0/1/0/0/0 | 06:25:36.980 | 16.41 |
| `git-storage-alternates-one-level` | KILLED: `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | 0/1/0/0/0 | 06:25:41.648 | 18.22 |
| `git-storage-gitfile-relative-to-cwd` | KILLED: `tests/unit/test_retention_dependency_units.py::test_d1_a_separate_git_dir_and_a_relative_gitfile` | 0/1/0/0/0 | 06:25:46.449 | 19.96 |
| `git-storage-nearest-ignores-gitfiles` | KILLED: `tests/unit/test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in` | 0/1/0/0/0 | 06:25:49.605 | 20.12 |
| `record-raw-spelling` | KILLED: `tests/unit/test_retention_dependency_units.py::test_record_spells_each_place_once` | 0/1/0/0/0 | 06:25:55.241 | 20.52 |
| `fences-not-retention-only` | KILLED: `tests/unit/test_retention_dependency_units.py::test_d5_fences_reads_what_retiring_reads_place_by_place` | 0/1/0/0/0 | 06:26:00.676 | 19.83 |
| `record-no-output` | KILLED: `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[output]` | 0/1/0/0/0 | 06:26:10.581 | 19.20 |
| `record-no-git` | KILLED: `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` | 0/1/0/0/0 | 06:26:16.984 | 20.62 |
| `review-quoted-decode-removed` | KILLED: `tests/fake/test_retention_dependency_review_r1.py::test_c8_4_git_c_quoted_alternate_keeps_the_tree_it_reads[absolute]` | 0/1/0/0/1 | 06:26:27.457 | 34.85 |
| `review-export-row-removed` | KILLED: `tests/fake/test_retention_dependency_review_r1.py::test_c8_3_pending_export_keeps_its_actual_path_beside_its_lease_identity[legacy-dotless-i]` | 2/1/0/0/1 | 06:26:37.726 | 39.07 |
| `review-alternates-cap-restored` | KILLED: `tests/fake/test_retention_dependency_review_r1.py::test_c8_4_large_valid_alternates_file_keeps_the_tree_git_reads` | 0/1/0/0/0 | 06:26:50.297 | 37.78 |
| `review-job-directory-pin-removed` | KILLED: `tests/unit/test_retention_job_dependencies.py::test_c8_4_job_directory_dependencies_are_rechecked_atomically[census-out_path]` | 0/1/0/0/0 | 06:26:59.820 | 34.50 |
| `r2-c-escape-bell-wrong` | KILLED: `tests/unit/test_retention_alternates_review_r2.py::test_c8_4_extended_git_c_escapes_resolve_the_same_objects[\a-\x07]` | 0/1/0/0/11 | 06:35:09.343 | 26.70 |
| `r2-alternates-file-cap` | KILLED: `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_many_short_valid_lines_use_bounded_input_memory` | 0/1/0/0/0 | 06:35:12.713 | 26.70 |
| `r2-export-join-request-id` | KILLED: `tests/fake/test_retention_dependency_review_r1.py::test_c8_3_pending_export_keeps_its_actual_path_beside_its_lease_identity[legacy-dotless-i]` | 0/1/0/0/1 | 06:35:16.185 | 26.33 |
| `r2-jobdir-final-check-removed` | KILLED: `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_nested_job_directory_aliases_pin_at_every_stage[case-commit]` | 0/1/0/0/2 | 06:35:25.452 | 26.66 |

## Complete hold ledger

One pytest per hold, no xdist, serial within each hold, at most four of our
pytest processes active or queued and at most two admitted by the shared lock.
All requested runs used the lock; no 30-minute fallback was needed. Whole-pytest
alarms were 570 seconds. Longest completed pytest: test_retention_dependency_properties-3,
148.93 seconds. No timeout or local harness-start retry was needed.
One test initially failed because its broad "retention not in text" assertion
matched that word in our hold directory, which was rendered as part of the
correct lease path. It passed unchanged in the single neutral-directory rerun
(detached-neutral), at the load in the ledger. This is a fixture-path assertion
artifact, not an incorrect admission hold. Both observations are retained;
final file counts use the neutral-directory result.
The shared wrapper's sysctl probes are denied by this sandbox; reported loads
come from os.getloadavg() inside the admitted Python process. Observer preimports
produce two assertion-rewrite warnings; test assertions and settings are unchanged.

| Hold | Start UTC | Load (1/5/15 min) | P/F/S/X/U | Slot | Pytest seconds |
|---|---|---|---|---|---|
| `test_retention_dependency_incomplete` | 06:22:28.445 | 18.42 / 21.13 / 22.58 | 19/0/0/0/0 | B | 32.8 |
| `test_retention_dependency_review_r1` | 06:22:28.445 | 18.42 / 21.13 / 22.58 | 18/0/0/0/0 | A | 6.01 |
| `test_retention_alternates` | 06:22:38.250 | 17.15 / 20.76 / 22.44 | 23/0/0/0/0 | A | 29.53 |
| `test_retention_dependencies` | 06:23:09.875 | 14.59 / 19.71 / 21.99 | 30/0/0/0/0 | A | 23.88 |
| `test_retention_job_dependencies` | 06:23:15.129 | 14.78 / 19.67 / 21.96 | 12/0/0/0/0 | B | 6.95 |
| `test_retention_dependency_spelling` | 06:23:23.793 | 14.40 / 19.51 / 21.89 | 16/0/0/0/0 | B | 7.13 |
| `test_retention_folded_pin` | 06:23:32.701 | 14.19 / 19.29 / 21.78 | 38/0/0/0/0 | B | 74.43 |
| `test_retention_dependency_units` | 06:23:35.369 | 13.93 / 19.16 / 21.72 | 19/0/0/0/0 | A | 16.95 |
| `test_retention_dependency_properties-1` | 06:24:02.199 | 13.69 / 18.70 / 21.48 | 1/0/0/0/0 | A | 60.88 |
| `test_retention_dependency_properties-2` | 06:24:54.705 | 13.65 / 17.79 / 20.96 | 1/0/0/0/0 | B | 62.91 |
| `mutant-git-storage-no-commondir` | 06:25:25.188 | 14.24 / 17.47 / 20.73 | 0/1/0/0/0 | A | 1.83 |
| `mutant-git-storage-no-worktrees-fallback` | 06:25:31.982 | 14.62 / 17.50 / 20.72 | 0/1/0/0/0 | A | 2.06 |
| `mutant-git-storage-no-alternates` | 06:25:36.980 | 16.41 / 17.82 / 20.82 | 0/1/0/0/0 | A | 1.99 |
| `mutant-git-storage-alternates-one-level` | 06:25:41.648 | 18.22 / 18.17 / 20.92 | 0/1/0/0/0 | A | 2.17 |
| `mutant-git-storage-gitfile-relative-to-cwd` | 06:25:46.449 | 19.96 / 18.54 / 21.04 | 0/1/0/0/0 | A | 1.41 |
| `mutant-git-storage-nearest-ignores-gitfiles` | 06:25:49.605 | 20.12 / 18.59 / 21.04 | 0/1/0/0/0 | A | 2.66 |
| `mutant-record-raw-spelling` | 06:25:55.241 | 20.52 / 18.70 / 21.06 | 0/1/0/0/0 | A | 2.01 |
| `test_retention_dependency_properties-3` | 06:26:00.676 | 19.83 / 18.59 / 21.01 | 1/0/0/0/0 | B | 148.93 |
| `mutant-fences-not-retention-only` | 06:26:00.676 | 19.83 / 18.59 / 21.01 | 0/1/0/0/0 | A | 6.33 |
| `mutant-record-no-output` | 06:26:10.581 | 19.20 / 18.49 / 20.95 | 0/1/0/0/0 | A | 3.79 |
| `mutant-record-no-git` | 06:26:16.984 | 20.62 / 18.79 / 21.04 | 0/1/0/0/0 | A | 7.49 |
| `mutant-review-quoted-decode-removed` | 06:26:27.457 | 34.85 / 21.87 / 22.10 | 0/1/0/0/1 | A | 6.34 |
| `mutant-review-export-row-removed` | 06:26:37.726 | 39.07 / 23.26 / 22.60 | 2/1/0/0/1 | A | 10.35 |
| `mutant-review-alternates-cap-restored` | 06:26:50.297 | 37.78 / 23.78 / 22.79 | 0/1/0/0/0 | A | 6.5 |
| `mutant-review-job-directory-pin-removed` | 06:26:59.820 | 34.50 / 23.54 / 22.72 | 0/1/0/0/0 | A | 2.23 |
| `review-r2` | 06:27:47.044 | 32.63 / 24.46 / 23.09 | 12/0/0/4/0 | A | 35.66 |
| `test_retention_dependency_properties-4` | 06:28:30.295 | 26.51 / 23.94 / 22.97 | 1/0/0/0/0 | A | 45.89 |
| `test_retention_shared_folders-1` | 06:29:16.019 | 23.96 / 23.78 / 22.97 | 2/0/0/0/0 | B | 9.87 |
| `test_retention_shared_folders-2` | 06:29:19.069 | 23.96 / 23.78 / 22.97 | 4/0/0/0/0 | A | 6.42 |
| `test_retention_shared_folders-4` | 06:29:28.970 | 28.24 / 24.72 / 23.31 | 2/0/0/0/0 | A | 4.11 |
| `quotes-r2` | 06:29:34.585 | 29.24 / 25.06 / 23.45 | 15/0/0/0/0 | B | 7.9 |
| `test_retention_shared_folders-6` | 06:29:36.057 | 29.24 / 25.06 / 23.45 | 2/0/0/0/0 | A | 4.35 |
| `test_retention_shared_folders-8` | 06:29:42.984 | 28.90 / 25.06 / 23.46 | 8/0/0/0/0 | A | 7.22 |
| `review-r2-final` | 06:29:44.577 | 28.67 / 25.07 / 23.47 | 14/0/0/5/0 | B | 23.11 |
| `test_retention_shared_folders-10` | 06:29:52.040 | 27.57 / 24.91 / 23.42 | 2/0/0/0/0 | A | 4.05 |
| `test_retention_shared_folders-12` | 06:29:58.383 | 26.49 / 24.72 / 23.36 | 1/0/0/0/0 | A | 145.95 |
| `review-r2-red` | 06:30:09.679 | 22.57 / 23.94 / 23.11 | 0/5/0/0/0 | B | 13.8 |
| `test_retention_shared_folders-3` | 06:30:27.499 | 25.73 / 24.59 / 23.35 | 4/0/0/0/0 | B | 8.01 |
| `test_retention_shared_folders-5` | 06:30:36.778 | 23.32 / 24.11 / 23.20 | 2/0/0/0/0 | B | 2.02 |
| `test_retention_shared_folders-7` | 06:30:40.180 | 22.25 / 23.87 / 23.12 | 4/0/0/0/0 | B | 7.96 |
| `test_retention_shared_folders-9` | 06:30:49.837 | 20.45 / 23.43 / 22.97 | 1/0/0/0/0 | B | 3.41 |
| `test_retention_shared_folders-11` | 06:30:57.185 | 21.30 / 23.56 / 23.02 | 1/0/0/0/0 | B | 7.05 |
| `test_retention_shared_folders-13` | 06:31:09.486 | 18.95 / 22.93 / 22.80 | 1/0/0/0/0 | B | 3.17 |
| `test_retention_shared_folders-15` | 06:31:14.590 | 18.71 / 22.81 / 22.76 | 1/0/0/0/0 | B | 5.34 |
| `test_retention_shared_folders-17` | 06:31:22.734 | 22.82 / 23.60 / 23.04 | 1/0/0/0/0 | B | 4.03 |
| `test_retention_shared_folders-19` | 06:31:29.184 | 22.83 / 23.58 / 23.04 | 2/0/0/0/0 | B | 2.97 |
| `test_retention_shared_folders-21` | 06:31:34.582 | 26.85 / 24.40 / 23.33 | 1/0/0/0/0 | B | 3.25 |
| `test_retention_shared_folders-23` | 06:31:39.787 | 26.79 / 24.42 / 23.35 | 2/0/0/0/0 | B | 7.39 |
| `test_retention_shared_folders-25` | 06:31:56.791 | 24.90 / 24.13 / 23.26 | 4/0/0/0/0 | B | 18.09 |
| `test_retention_shared_folders-27` | 06:32:30.084 | 20.98 / 23.21 / 22.96 | 1/0/0/0/0 | B | 17.91 |
| `test_retention_shared_folders-14` | 06:32:31.494 | 20.98 / 23.21 / 22.96 | 1/0/0/0/0 | A | 7.79 |
| `test_retention_shared_folders-16` | 06:32:47.584 | 18.23 / 22.49 / 22.71 | 1/0/0/0/0 | A | 138.11 |
| `test_retention_archive-2` | 06:32:57.771 | 16.67 / 22.02 / 22.53 | 1/0/0/0/0 | B | 7.34 |
| `test_retention_archive-4` | 06:33:15.299 | 14.10 / 21.08 / 22.18 | 1/0/0/0/0 | B | 7.78 |
| `test_retention_archive-6` | 06:33:32.931 | 13.27 / 20.55 / 21.98 | 1/0/0/0/0 | B | 12.05 |
| `test_retention_archive-8` | 06:33:57.260 | 17.04 / 20.87 / 22.05 | 2/0/0/0/0 | B | 4.7 |
| `test_retention_archive-10` | 06:34:10.636 | 22.47 / 21.82 / 22.36 | 1/0/0/0/0 | B | 2.62 |
| `test_retention_archive-12` | 06:34:18.978 | 26.04 / 22.57 / 22.63 | 1/0/0/0/0 | B | 3.57 |
| `test_retention_archive-14` | 06:34:25.647 | 30.41 / 23.64 / 23.00 | 1/0/0/0/0 | B | 9.85 |
| `test_retention_archive-16` | 06:34:37.648 | 30.98 / 23.99 / 23.14 | 1/0/0/0/0 | B | 6.62 |
| `test_retention_archive-18` | 06:34:47.740 | 29.20 / 23.84 / 23.09 | 1/0/0/0/0 | B | 5.42 |
| `test_retention_archive-20` | 06:34:57.221 | 28.02 / 23.76 / 23.07 | 1/0/0/0/0 | B | 15.22 |
| `mutant-r2-c-escape-bell-wrong` | 06:35:09.343 | 26.70 / 23.67 / 23.05 | 0/1/0/0/11 | A | 1.42 |
| `mutant-r2-alternates-file-cap` | 06:35:12.713 | 26.70 / 23.67 / 23.05 | 0/1/0/0/0 | A | 1.45 |
| `mutant-r2-export-join-request-id` | 06:35:16.185 | 26.33 / 23.64 / 23.04 | 0/1/0/0/1 | A | 6.79 |
| `mutant-r2-jobdir-final-check-removed` | 06:35:25.452 | 26.66 / 23.80 / 23.11 | 0/1/0/0/2 | A | 1.9 |
| `job-directory-original` | 06:35:31.657 | 26.12 / 23.73 / 23.09 | 1/0/0/0/0 | A | 1.34 |
| `review-r2-latest` | 06:35:35.913 | 27.47 / 24.05 / 23.20 | 14/0/0/6/0 | A | 35.51 |
| `review-r2-latest-red` | 06:36:15.092 | 22.84 / 23.29 / 22.96 | 0/6/0/0/0 | A | 19.05 |
| `proof-r2-f1` | 06:36:37.701 | 23.33 / 23.37 / 23.00 | 3/0/0/0/0 | A | 5.67 |
| `proof-r2-f2` | 06:36:46.885 | 23.81 / 23.47 / 23.04 | 3/0/0/0/0 | A | 7.14 |
| `test_retention_archive-22` | 06:37:00.629 | 26.15 / 24.00 / 23.24 | 1/0/0/0/0 | A | 3.11 |
| `test_retention_archive-24` | 06:37:07.448 | 25.10 / 23.82 / 23.18 | 1/0/0/0/0 | A | 3.48 |
| `test_retention_archive-26` | 06:37:13.796 | 26.77 / 24.19 / 23.31 | 1/0/0/0/0 | A | 10.48 |
| `test_retention_archive-28` | 06:37:26.052 | 27.29 / 24.41 / 23.40 | 1/0/0/0/0 | A | 33.45 |
| `test_retention_archive-30` | 06:38:03.240 | 27.02 / 24.74 / 23.57 | 7/0/0/0/0 | A | 36.95 |
| `memory-observed` | 06:38:45.040 | 28.43 / 25.32 / 23.83 | 1/0/0/0/0 | A | 9.63 |
| `test_retention_shared_folders-18` | 06:38:45.205 | 28.43 / 25.32 / 23.83 | 4/0/0/0/0 | B | 27.87 |
| `test_retention_shared_folders-20` | 06:39:16.603 | 23.71 / 24.45 / 23.56 | 2/0/0/0/0 | B | 1.86 |
| `test_retention_shared_folders-22` | 06:39:20.899 | 22.85 / 24.25 / 23.50 | 2/0/0/0/0 | B | 4.11 |
| `test_retention_shared_folders-24` | 06:39:27.852 | 22.46 / 24.15 / 23.47 | 4/0/0/0/0 | B | 11.23 |
| `test_retention_shared_folders-26` | 06:39:40.973 | 23.05 / 24.21 / 23.50 | 2/0/0/0/0 | B | 2.58 |
| `test_retention_archive-1` | 06:39:45.474 | 22.80 / 24.14 / 23.48 | 1/0/0/0/0 | B | 4.39 |
| `test_retention_archive-3` | 06:39:51.096 | 21.78 / 23.90 / 23.40 | 1/0/0/0/0 | B | 5.21 |
| `test_retention_archive-5` | 06:39:59.039 | 20.92 / 23.69 / 23.33 | 1/0/0/0/0 | B | 3.15 |
| `test_retention_archive-7` | 06:40:05.223 | 19.08 / 23.20 / 23.16 | 1/0/0/0/0 | B | 2.87 |
| `test_retention_archive-9` | 06:40:10.227 | 19.24 / 23.17 / 23.15 | 1/0/0/0/0 | B | 1.1 |
| `test_retention_archive-11` | 06:40:13.625 | 19.24 / 23.17 / 23.15 | 2/0/0/0/0 | B | 8.86 |
| `test_retention_archive-13` | 06:40:25.293 | 19.84 / 23.11 / 23.13 | 1/0/0/0/0 | B | 4.38 |
| `test_retention_archive-15` | 06:40:32.225 | 22.34 / 23.57 / 23.29 | 1/0/0/0/0 | B | 7.78 |
| `test_retention_archive-17` | 06:40:42.764 | 27.39 / 24.63 / 23.67 | 1/0/0/0/0 | B | 3.24 |
| `test_retention_archive-19` | 06:40:47.494 | 27.03 / 24.60 / 23.67 | 1/0/0/0/0 | B | 15.61 |
| `test_retention_archive-21` | 06:41:05.435 | 24.30 / 24.18 / 23.54 | 1/0/0/0/0 | B | 2.36 |
| `test_retention_archive-23` | 06:41:09.206 | 24.30 / 24.18 / 23.54 | 1/0/0/0/0 | B | 1.67 |
| `test_retention_archive-25` | 06:41:11.989 | 22.99 / 23.91 / 23.45 | 1/0/0/0/0 | B | 1.31 |
| `test_retention_archive-27` | 06:41:14.825 | 23.31 / 23.96 / 23.47 | 1/0/0/0/0 | B | 4.19 |
| `test_retention_archive-29` | 06:41:20.523 | 22.57 / 23.80 / 23.41 | 1/0/0/0/0 | B | 3.01 |
| `test_retention_archive-31` | 06:41:25.023 | 22.20 / 23.70 / 23.38 | 1/0/0/0/0 | B | 4.44 |
| `test_retention_archive-33` | 06:41:30.862 | 21.54 / 23.54 / 23.32 | 1/0/0/0/0 | B | 1.24 |
| `test_retention_archive-35` | 06:41:33.353 | 21.54 / 23.54 / 23.32 | 16/0/0/0/0 | B | 11.5 |
| `test_retention_archive-37` | 06:41:46.733 | 19.43 / 22.97 / 23.13 | 1/0/0/0/0 | B | 3.66 |
| `test_retention_archive-39` | 06:41:52.414 | 19.31 / 22.89 / 23.10 | 2/0/0/0/0 | B | 6.5 |
| `test_retention_archive-41` | 06:42:02.218 | 17.73 / 22.43 / 22.93 | 1/0/0/0/0 | B | 6.21 |
| `review-r2-closeout` | 06:42:07.188 | 18.15 / 22.44 / 22.93 | 15/0/0/6/0 | A | 28.0 |
| `test_retention_archive-43` | 06:42:09.770 | 18.14 / 22.37 / 22.90 | 1/0/0/0/0 | B | 2.48 |
| `test_retention_archive-32` | 06:42:13.916 | 18.14 / 22.37 / 22.90 | 1/0/0/0/0 | B | 3.47 |
| `test_retention_archive-34` | 06:42:19.186 | 17.97 / 22.26 / 22.86 | 2/0/0/0/0 | B | 3.67 |
| `test_retention_archive-45` | 06:42:24.667 | 20.09 / 22.58 / 22.97 | 1/0/0/0/0 | B | 3.06 |
| `test_retention_archive-47` | 06:42:29.364 | 20.09 / 22.58 / 22.97 | 1/0/0/0/0 | B | 5.74 |
| `test_retention_archive-49` | 06:42:36.859 | 19.49 / 22.36 / 22.88 | 1/0/0/0/0 | A | 1.78 |
| `test_retention_archive-51` | 06:42:40.031 | 19.05 / 22.23 / 22.83 | 1/0/0/0/0 | A | 2.87 |
| `test_retention_archive-53` | 06:42:44.383 | 19.05 / 22.23 / 22.83 | 1/0/0/0/0 | A | 1.53 |
| `test_retention_archive-55` | 06:42:47.591 | 19.20 / 22.21 / 22.82 | 1/0/0/0/0 | A | 1.83 |
| `test_retention_archive-57` | 06:42:50.793 | 18.55 / 22.02 / 22.75 | 1/0/0/0/0 | A | 2.89 |
| `test_retention_archive-59` | 06:42:58.085 | 17.70 / 21.79 / 22.67 | 1/0/0/0/0 | A | 6.72 |
| `test_retention_archive-61` | 06:43:07.206 | 17.23 / 21.55 / 22.57 | 1/0/0/0/0 | A | 3.87 |
| `test_retention_archive-63` | 06:43:12.799 | 18.01 / 21.64 / 22.60 | 1/0/0/0/0 | A | 5.03 |
| `test_retention_archive-65` | 06:43:19.606 | 17.41 / 21.39 / 22.49 | 1/0/0/0/0 | A | 1.78 |
| `test_retention_archive-67` | 06:43:23.091 | 17.41 / 21.39 / 22.49 | 1/0/0/0/0 | A | 3.03 |
| `test_retention_archive-69` | 06:43:28.135 | 17.22 / 21.28 / 22.45 | 1/0/0/0/0 | A | 3.53 |
| `test_retention_archive-71` | 06:43:33.330 | 17.52 / 21.27 / 22.44 | 1/0/0/0/0 | A | 2.41 |
| `test_retention_archive-73` | 06:43:37.361 | 17.96 / 21.30 / 22.44 | 1/0/0/0/0 | A | 1.96 |
| `test_retention_archive-75` | 06:43:41.040 | 20.68 / 21.81 / 22.62 | 1/0/0/0/0 | A | 1.79 |
| `test_retention_archive-77` | 06:43:44.514 | 20.68 / 21.81 / 22.62 | 1/0/0/0/0 | A | 5.74 |
| `test_retention_archive-79` | 06:43:51.730 | 21.89 / 22.03 / 22.69 | 1/0/0/0/0 | A | 4.19 |
| `test_retention_archive-81` | 06:43:59.158 | 20.94 / 21.83 / 22.61 | 1/0/0/0/0 | A | 1.74 |
| `test_retention_archive-83` | 06:44:03.100 | 20.39 / 21.70 / 22.56 | 1/0/0/0/0 | A | 2.15 |
| `test_retention_turn_workdirs` | 06:44:06.619 | 19.55 / 21.51 / 22.49 | 1/0/0/0/0 | A | 1.9 |
| `test_retention_cwd_rechecks` | 06:44:09.617 | 19.55 / 21.51 / 22.49 | 3/0/0/0/0 | A | 1.86 |
| `test_detached_retention_fence-2` | 06:44:12.880 | 18.87 / 21.33 / 22.42 | 4/0/0/0/0 | A | 12.61 |
| `test_detached_retention_fence-4` | 06:44:29.898 | 17.07 / 20.79 / 22.20 | 1/0/0/0/0 | A | 3.67 |
| `test_detached_retention_fence-6` | 06:44:35.771 | 16.18 / 20.54 / 22.10 | 1/0/0/0/0 | A | 1.44 |
| `test_detached_retention_fence-8` | 06:44:38.505 | 16.18 / 20.54 / 22.10 | 4/0/0/0/0 | A | 2.49 |
| `test_detached_retention_fence-10` | 06:44:42.116 | 15.93 / 20.41 / 22.05 | 2/0/0/0/0 | A | 3.4 |
| `test_detached_retention_fence-12` | 06:44:46.479 | 15.37 / 20.22 / 21.97 | 2/0/0/0/0 | A | 1.63 |
| `test_detached_retention_fence-14` | 06:44:49.400 | 15.37 / 20.22 / 21.97 | 1/0/0/0/0 | A | 3.27 |
| `test_detached_retention_fence-16` | 06:44:56.494 | 14.39 / 19.85 / 21.82 | 4/0/0/0/0 | A | 2.94 |
| `test_detached_retention_fence-18` | 06:45:02.938 | 14.04 / 19.69 / 21.75 | 8/0/0/0/0 | A | 4.22 |
| `test_detached_retention_fence-20` | 06:45:09.019 | 13.80 / 19.55 / 21.69 | 1/0/0/0/0 | A | 18.75 |
| `test_retention_archive-36` | 06:45:23.061 | 13.83 / 19.27 / 21.55 | 2/0/0/0/0 | B | 3.87 |
| `test_retention_archive-38` | 06:45:28.326 | 14.57 / 19.33 / 21.56 | 1/0/0/0/0 | B | 3.12 |
| `test_detached_retention_fence-22` | 06:45:29.059 | 14.57 / 19.33 / 21.56 | 1/0/0/0/0 | A | 1.81 |
| `test_detached_retention_fence-24` | 06:45:32.137 | 15.16 / 19.38 / 21.56 | 2/0/0/0/0 | A | 2.38 |
| `test_retention_archive-40` | 06:45:32.863 | 15.16 / 19.38 / 21.56 | 1/0/0/0/0 | B | 4.03 |
| `test_detached_retention_fence-26` | 06:45:35.808 | 16.11 / 19.50 / 21.60 | 1/0/0/0/0 | A | 1.68 |
| `test_retention_archive-42` | 06:45:38.310 | 16.11 / 19.50 / 21.60 | 1/0/0/0/0 | B | 1.06 |
| `test_detached_retention_fence-28` | 06:45:38.825 | 16.11 / 19.50 / 21.60 | 2/0/0/0/0 | A | 1.81 |
| `test_retention_archive-44` | 06:45:40.660 | 16.10 / 19.45 / 21.56 | 1/0/0/0/0 | B | 2.72 |
| `test_writer_retention_fence` | 06:45:42.353 | 16.10 / 19.45 / 21.56 | 23/0/0/0/0 | A | 29.27 |
| `test_retention_archive-46` | 06:45:45.088 | 17.53 / 19.69 / 21.64 | 1/0/0/0/0 | B | 2.98 |
| `test_retention_archive-48` | 06:45:49.987 | 25.02 / 21.20 / 22.16 | 1/0/0/0/0 | B | 8.77 |
| `test_retention_archive-50` | 06:46:00.173 | 22.85 / 20.87 / 22.03 | 1/0/0/0/0 | B | 4.15 |
| `test_retention_archive-52` | 06:46:05.836 | 23.74 / 21.08 / 22.10 | 1/0/0/0/0 | B | 1.39 |
| `test_retention_archive-54` | 06:46:08.934 | 23.74 / 21.08 / 22.10 | 1/0/0/0/0 | B | 3.08 |
| `test_test_module_names` | 06:46:13.382 | 24.24 / 21.23 / 22.14 | 1/0/0/0/0 | A | 0.71 |
| `test_retention_archive-56` | 06:46:18.993 | 23.50 / 21.13 / 22.10 | 1/0/0/0/0 | A | 1.94 |
| `test_retention_archive-58` | 06:46:22.042 | 22.74 / 21.01 / 22.05 | 1/0/0/0/0 | A | 2.36 |
| `test_retention_archive-60` | 06:46:25.451 | 21.72 / 20.83 / 21.98 | 1/0/0/0/0 | A | 3.42 |
| `test_retention_archive-62` | 06:46:29.938 | 20.70 / 20.63 / 21.91 | 1/0/0/0/0 | A | 1.39 |
| `test_retention_archive-64` | 06:46:32.506 | 20.70 / 20.63 / 21.91 | 1/0/0/0/0 | A | 2.52 |
| `test_retention_archive-66` | 06:46:36.323 | 19.85 / 20.45 / 21.84 | 1/0/0/0/0 | A | 3.4 |
| `test_retention_archive-68` | 06:46:41.473 | 18.82 / 20.23 / 21.75 | 1/0/0/0/0 | A | 2.43 |
| `test_retention_archive-70` | 06:46:45.205 | 18.03 / 20.04 / 21.67 | 1/0/0/0/0 | A | 1.93 |
| `test_retention_archive-72` | 06:46:48.319 | 18.03 / 20.04 / 21.67 | 1/0/0/0/0 | A | 3.53 |
| `test_retention_archive-74` | 06:46:54.177 | 18.11 / 20.03 / 21.66 | 1/0/0/0/0 | A | 5.08 |
| `test_retention_archive-76` | 06:47:02.847 | 16.83 / 19.68 / 21.52 | 1/0/0/0/0 | A | 1.62 |
| `test_retention_archive-78` | 06:47:05.765 | 16.28 / 19.52 / 21.45 | 1/0/0/0/0 | A | 0.89 |
| `test_retention_archive-80` | 06:47:07.774 | 16.28 / 19.52 / 21.45 | 1/0/0/0/0 | A | 0.69 |
| `test_retention_archive-82` | 06:47:09.479 | 16.28 / 19.52 / 21.45 | 1/0/0/0/0 | A | 1.52 |
| `test_retention_turns` | 06:47:12.190 | 15.54 / 19.31 / 21.36 | 7/0/0/0/0 | A | 2.48 |
| `test_retention_worktree_cwd` | 06:47:15.770 | 14.93 / 19.12 / 21.29 | 7/0/0/0/0 | A | 3.93 |
| `test_detached_retention_fence-1` | 06:47:21.038 | 15.50 / 19.17 / 21.29 | 6/0/0/0/0 | A | 3.91 |
| `test_detached_retention_fence-3` | 06:47:26.119 | 15.54 / 19.12 / 21.26 | 1/0/0/0/0 | A | 1.55 |
| `test_detached_retention_fence-5` | 06:47:29.105 | 15.54 / 19.12 / 21.26 | 0/1/0/0/0 | B | 1.54 |
| `test_detached_retention_fence-7` | 06:47:31.832 | 15.17 / 18.98 / 21.20 | 4/0/0/0/0 | B | 2.44 |
| `test_detached_retention_fence-9` | 06:47:35.465 | 15.48 / 18.98 / 21.18 | 2/0/0/0/0 | B | 1.42 |
| `test_detached_retention_fence-11` | 06:47:38.062 | 15.48 / 18.98 / 21.18 | 2/0/0/0/0 | B | 1.9 |
| `test_detached_retention_fence-13` | 06:47:41.026 | 15.36 / 18.90 / 21.14 | 2/0/0/0/0 | B | 1.75 |
| `test_detached_retention_fence-15` | 06:47:43.912 | 15.36 / 18.90 / 21.14 | 6/0/0/0/0 | B | 3.02 |
| `test_detached_retention_fence-17` | 06:47:48.055 | 15.41 / 18.85 / 21.11 | 8/0/0/0/0 | B | 4.29 |
| `test_detached_retention_fence-19` | 06:47:54.822 | 14.49 / 18.54 / 20.98 | 1/0/0/0/0 | A | 7.51 |
| `test_detached_retention_fence-21` | 06:48:04.246 | 13.81 / 18.33 / 20.89 | 4/0/0/0/0 | A | 1.02 |
| `test_detached_retention_fence-23` | 06:48:06.413 | 14.39 / 18.38 / 20.89 | 1/0/0/0/0 | A | 1.57 |
| `test_detached_retention_fence-25` | 06:48:09.105 | 14.39 / 18.38 / 20.89 | 1/0/0/0/0 | A | 1.5 |
| `detached-neutral` | 06:48:11.830 | 15.24 / 18.49 / 20.91 | 1/0/0/0/0 | A | 1.72 |
| `test_detached_retention_fence-27` | 06:48:11.985 | 15.24 / 18.49 / 20.91 | 8/0/0/0/0 | B | 4.24 |
| `test_detached_retention_fence-29` | 06:48:17.371 | 14.98 / 18.38 / 20.86 | 8/0/0/0/0 | A | 4.15 |
| `test_state_contract` | 06:48:22.723 | 15.22 / 18.37 / 20.84 | 37/0/0/0/0 | A | 22.69 |
| `test_invariants_index` | 06:48:46.623 | 14.05 / 17.83 / 20.57 | 25/0/0/0/0 | A | 0.58 |

## What did not run and delivery

No requested local file or named mutant remains unrun. The full suite,
Python 3.12 and tests/frontend/test_core_live.py were not run on this lane.
The live frontend's person-only daemon check prevents a lane run; CI owns it.
GitNexus tools are unavailable, so this review used local Git and source traces.
No macOS CI completion is inferred from a pending snapshot. A byte-ff filename
could not be created; its decoding and failing Git lookup were executed.

No live store, real provider or real subfleet CLI was used. All retirement
fixtures use scratch homes. The prescribed archive test invokes cli.main only
with SUBFLEET_HOME set to its scratch World. Git transport in fixtures targets
only local bare repositories; no assigned-repository fetch, merge or push ran.
Network was used only for the required locked environment sync and the two
explicitly allowed GitHub reads. No sub-agents or external messages were used.

Staging the two named test files failed because the sandbox cannot create the
shared worktree index.lock. Per the task's fallback, files remain in this
workspace; no commit was created and HEAD stays at the reviewed commit.
Deliverables are:

- tests/fake/test_retention_dependency_review_r2.py
- tests/unit/test_retention_alternates_review_r2.py
- docs/reports/2026-10-29-retention-pins-review-r2.md
- docs/reports/2026-10-29-retention-pins-review-r2-data/

The data directory includes original-source diagnostics, trial-repair definitions,
Git comparisons, CI reads, the measurement, all completed hold observations and
logs, source hashes and cleanup evidence.

## Closeout

189 pytest holds and one measurement hold completed; the required environment
sync ran once under its separate setup lock. The largest observed scratch size
was 414 MiB, below 1.5 GB; retained review evidence is about 2.2 MiB.
All owned tool sessions and fixture teardowns completed, with no detached runner.
The guarded scratch cleanup and final absence check are recorded in closeout.json.
No bytecode, build directory, worktree virtualenv, pytest cache or Hypothesis
cache was left in the workspace. Product and existing tracked files match head.

DONE.
