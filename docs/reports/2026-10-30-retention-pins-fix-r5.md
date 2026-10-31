# Retention dependency pins: fix round r5, 2026-10-30

**Historical scope:** the decisions, results, counts and PR-body lines below describe fix r5 at `beabed1f7`. Review r6 later found R6-F1 (named-admin own-store omission) and R6-F2 (silently dropped over-bound pointer reads). [Fix round r6](2026-10-30-retention-pins-fix-r6.md) repairs both: current discovery keeps own and inferred stores, and nonmissing pointer read failures become incomplete evidence without lifting the 64 KiB bound. The earlier observation bounds still apply.

Base: `7eec7fa802863c9adb2443853fb9e07eceed543b`, including the hub's unchanged review-r5 test and evidence commits on `c1dfadceb`. First recorded clock: **16:12:50 UTC**. The conservative four-hour new-hold cutoff is **20:12 UTC**; the six-hour wall is **22:12 UTC**. First hold: **16:14:03 UTC**. Only the assigned workspace is edited. Shared Git metadata refuses staging, so ordered commits use private metadata on `fix/retention-dependency-pins-217`; delivery is a verified bundle excluding the base. No shared-repository fetch, merge or push, live store, real provider, external message or subagent is used.

## Decisions built

1. **R5-F1:** apply review r5's `r5-trial.diff` exactly. `_gitfile` consumes the literal `gitdir: ` prefix and removes only trailing CR/LF; `_storage` removes only trailing CR/LF from `commondir`. Every other pathname byte is preserved. There is no departure from the trial or the hub's product decisions. The [source audit](2026-10-30-retention-pins-fix-r5-data/source-audit.json) verifies the exact trial result and the sole mark removal, which covers three parametrized cases.
2. **Readers:** all dependency-discovery readers are in `dependencies.py`. Its raw readers and alternates parser already preserve pathname bytes. No additional reader repair is needed. Other readers serve archive registration/identity/verification, survey, host-push validation or Claude settings; each is audited below and left unchanged as requested.
3. **Tests:** remove only the expected-failure mark covering the three R5-F1 cases, leaving every other byte of the review file unchanged. Add `tests/unit/test_retention_pointer_whitespace.py`: eight executed Git syntax/input cases and a property with **100 generated plus two explicit examples**, at normal settings. Kill the nine required earlier head-source mutants, the commondir-strip mutant against repaired head source, and two new gitfile mutants. All mutation is in memory, with original/mutated source hashes and setup/call/teardown outcomes preserved.
4. **Text:** the desktop and main report describe the repaired head, preserving their swap-back, timestamp and later-dependency observation bounds. The new main-report section records review r5 and fix round r5. Review r5's `descriptive_qualifications` describe historical `c1dfadceb`, so they are not inserted as claims about this head. Earlier fix reports remain dated evidence. C-8.3 and C-8.4's normative clauses are byte-identical to base; only a change-log entry is added to the contract. No filesystem operation enters a transaction.

**Required invariant:** for every pointer Git follows, discovery records the target Git reads, or reports incomplete. R6-F1 and R6-F2 violated this at the r5 head; fix r6 repairs those gaps. Incomplete discovery retains every allocated worktree while its job is live or exporting and names the job and file. Admission still proceeds. Stable complete terminal-export controls continue to permit retirement when only the live borrower's Git storage would have pinned it.

## Installed Git comparisons

Installed version: **Git 2.55.0**. Each accepted case reads a blob present only in the target's object store, rather than merely checking that `rev-parse` produces a path. The checks run before and after repair; [before](2026-10-30-retention-pins-fix-r5-data/git-before.json), [after](2026-10-30-retention-pins-fix-r5-data/git-after.json) and [probe source](2026-10-30-retention-pins-fix-r5-data/git_probe.py.txt) preserve the inputs/results. The new test file repeats all eight cases.

| Input | Real Git | Repaired discovery |
|---|---|---|
| `gitdir: ` target with leading and trailing spaces | Reads target, exit 0 | Records canonical target and objects; no incomplete evidence |
| `gitdir:` with no space after colon, with a valid target present | Refuses, exit 128 (`not a git repository`) | Rejects the gitfile; records no syntactic target |
| Empty gitfile target | Refuses, exit 128 (`no path in gitfile`) | Rejects the gitfile; records no target |
| Gitfile with CRLF ending and significant spaces | Reads target, exit 0 | Removes ending CR/LF only; records target and objects |
| `commondir` with leading and trailing spaces | Reads target, exit 0 | Records common target and objects |
| `commondir` containing only three spaces, with that directory present | Reads the literal three-space target, exit 0 | Records that target's canonical storage; does not treat it as empty |
| LF-only `commondir`, admin without its own objects (original probe) | Refuses, exit 128 (`not a git repository`) | Conservatively records admin/objects paths |
| Zero-byte `commondir`, admin with its own objects | Refuses, exit 128 (`failed to read`) | Conservatively records admin/objects paths |
| LF-only or CRLF-only `commondir`, directory-`.git` admin with its own objects (r5 fixture) | Reads its own object store, exit 0 | Recorded gitdir and own objects in that fixture; named `worktrees/<id>` admins were missed until fix r6 |
| `commondir` with CRLF ending and significant spaces | Reads target, exit 0 | Removes ending CR/LF only; records target and objects |

For Git's rejected inputs, discovery need not invent incomplete evidence: Git has no usable storage dependency through that pointer. Rejecting invalid gitfiles cannot omit a target Git follows. The empty-common-target probe without local objects retains the inherited conservative admin/objects entries; none represents an omitted readable target. [Additional executed controls](2026-10-30-retention-pins-fix-r5-data/git-empty-common-valid-admin.json) distinguish a zero-byte common file from LF-only/CRLF-only content: the former is rejected even with a local object store, while the latter uses that store and r5 discovery recorded it for the tested directory `.git`. The broader named-admin result requires fix r6. Git also accepts trailing CR/LF padding above discovery's 64 KiB bound: r5 silently swallowed those read errors; current discovery reports incomplete (R6-F2). The test comment is qualified to its actual fixture; assertions/settings are unchanged.

The property generates ASCII and Unicode whitespace (including tabs, vertical/form feeds and embedded CR/LF), both pointer types, relative/absolute paths and LF/CRLF endings. A final significant space keeps embedded CR/LF inside the pathname; the independently written record ending is the only suffix Git removes. Git object access is the oracle, followed by a canonical-target-or-incomplete assertion. It starts no provider and runs Git children serially, with a 30-second bound per child.

## Reader audit

The complete machine-readable audit is [reader-audit.json](2026-10-30-retention-pins-fix-r5-data/reader-audit.json). Static call inspection confirms that `record` uses only `git_storage`, `_gitfile`, `_storage` and `_alternates` for pointer discovery; the archive helpers are not called from that path.

| File / reader | Trims pathname bytes? | Scope / action |
|---|---|---|
| `subfleet/dependencies.py`: `_text; _Aliases.text` | No trimming; fsdecode preserves bytes via surrogateescape; aliases retain the raw text for revalidation. | Discovery; unchanged |
| `subfleet/dependencies.py`: `_gitfile` | Literal gitdir: prefix including its following space; rstrip CR/LF only; preserves all target bytes. | Discovery; repaired |
| `subfleet/dependencies.py`: `_storage` | commondir removes trailing CR/LF only; a target of spaces remains a pathname. | Discovery; repaired |
| `subfleet/dependencies.py`: `_alternates; _alternate_paths; _unquote` | Only removes one trailing LF record separator; C unquoting on raw bytes; no pathname strip. | Discovery; unchanged; executed alternates file/property rerun |
| `subfleet/retention_git.py`: `gitfile_admin` | Strips whole gitfile text and target. | Archive registration/repository lookup; outside discovery; unchanged |
| `subfleet/retention_git.py`: `registration` | Strips gitdir backlink and commondir. | Archive registration validation; outside discovery; unchanged |
| `subfleet/retention_git.py`: `backlink` | Strips gitdir backlink. | Archive registration recovery; outside discovery; unchanged |
| `subfleet/retention_git.py`: `registration_in` | Strips gitdir backlink. | Archive registration search; outside discovery; unchanged |
| `subfleet/retention_git.py`: `moved_tree` | Strips gitdir backlink. | Archive moved-checkout detection; outside discovery; unchanged |
| `subfleet/retention_git.py`: `discard_registration` | Strips gitdir backlink. | Registration cleanup; outside discovery; unchanged |
| `subfleet/retention_git.py`: `verify_bundle` | Strips commondir. | Bundle verification alternate writer; outside discovery; unchanged |
| `subfleet/retention_survey.py`: `_into_admin` | Strips whole gitfile text and target. | Survey preflight; outside discovery; unchanged |
| `subfleet/retention_archive.py`: `_Builder._nested_gitfile` | Strips whole gitfile text and target. | Archive nested-checkout refusal; outside discovery; unchanged |
| `subfleet/retention_archive.py`: `gitfile_digest` | Hashes the complete bounded raw gitfile bytes; does not parse or trim a pathname. | Archive identity check; outside discovery; unchanged |
| `subfleet/adapters/claude_mcp.py`: `_small_text; canonical_git_root` | Strips gitfile, its target, commondir and gitdir backlink. | Claude local settings root; outside discovery; unchanged |
| `subfleet/host_push.py`: `_git_directories` | Strips gitfile text and commondir; takes gitfile target after the literal prefix. | Host-push validation; outside discovery; unchanged |
| `subfleet/host_push.py`: `check_object_store` | Alternates lines are stripped for nonempty/comment classification; no target is resolved. | Host-push refusal check; outside discovery; unchanged |
| `Git-output pathname readers`: `retention_git.common_dir/discard_registration; retention_survey._source_common; retention_archive._git_dir; salvage._git` | Strip Git command output rather than directly parsing the four pointer-file kinds. | Outside dependency discovery; unchanged |
| `Existence-only checks`: `retention_git.scratch_reason` | Checks alternates existence; reads no alternates line. | Outside discovery; unchanged |


## Red and green

At `7eec7fa80`, the complete review-r5 file with `--runxfail` gives **57 passed / 3 failed**. The failures are exactly the live leading-space gitfile, trailing-space gitfile and trailing-space commondir cases; each reaches its owner-retired assertion after successful real-Git object access. At fix `9082caeece4d4b420710c583f9b07ccf439762c3`, without the mark, the complete file gives **60 passed**. All three terminal-export controls pass in both runs. The standalone comparisons reproduce the pre-fix complete-looking wrong target and confirm its repaired canonical storage.

Red start: **16:14:03 UTC**, load **31.72/26.89/27.77**, 135.30 seconds including observer overhead. Green start: **16:16:32 UTC**, load **25.43/26.00/27.25**, 101.58 seconds. The whitespace test/property file starts **16:18:41 UTC**, load **22.89/24.21/26.31**, and passes all nine nodes in 17.52 seconds including observer overhead.

## Execution and operational departures

The prescribed sync wrapper installs the locked dev group once with Python **3.14.7**. The sandbox refuses the requested Darwin scratch parent, so environment/cache/Hypothesis/logs and most fixture homes use `/private/tmp/sf-fix272r5`. Archive omission controls need a fixture path without a literal `tmp` component; those homes use the allowed attempt directory's `sf-fix272r5-fixtures` child. Both roots use guarded cleanup. No caller checkout is modified.

The preserved driver runs one pytest per shared hold, with `uv run --no-sync`, the requested unset variables, disabled pytest cache and no bytecode writes. The fixture baseline isolates stores and blocks providers/keychain access. `SUBFLEET_RETENTION_QOS=inherit` preserves the prior rounds' fixture execution choice. No xdist is installed; at most two owned pytests run at once. Each pytest has a 570-second alarm. Properties and expensive files are split as in review r5; sweep-race examples and its property run separately at normal settings. Long drivers stay attached to tool-managed sessions and are polled to completion. The observer records starts after lock admission, 1/5/15-minute loads, phases, selected/all nodes and peak RSS. Sandbox sysctl denials suppress the wrapper's printed load, but `os.getloadavg()` supplies the recorded loads.

Final ordinary accounting: **917 distinct passes** (915 repository nodes and 2 scratch controls), with **12 named mutants killed**. All **82 holds** ran under the shared lock; no outside-lock fallback was needed. Maximum observer-measured pytest duration: **581.82 seconds**; peak observed pytest RSS: **320.2 MiB**. No pytest exceeded ten minutes. The complete selection audit has only the deliberately omitted real-lsof node unrun. One sweep-property attempt was interrupted by the 570-second guard after 581.82 seconds including teardown/observer overhead (pytest reported 576.20 seconds), with no completed assertion failure reported. Its normal-settings retry passed; no example count, time bound or test assertion was changed. The interrupted attempt is preserved separately in the ledger, excluded from ordinary pass accounting. Retry start: **18:15:21 UTC**, load **42.62/42.57/49.24**, **300.88 seconds** including observer overhead. Hypothesis reports **40 passing, 0 failing generated cases**, with four discarded draws; max_examples remains 40.

## Per-file results

Starts are UTC on October 30 after lock admission. Loads are 1/5/15-minute averages. Counts are distinct passing nodes with passing setup/call/teardown, excluding red and mutant runs. `F` is `tests/fake/`; `U` is `tests/unit/`. All split starts and results are in the [complete ledger](2026-10-30-retention-pins-fix-r5-data/ledger.json).

| File | Passed | First start UTC | Load 1/5/15 |
|---|---:|---|---|
| `F/test_retention_dependency_review_r5.py` | 60 | 16:16:32 | 25.43/26.00/27.25 |
| `F/test_retention_dependency_review_r4.py` | 53 | 16:18:19 | 23.03/24.34/26.41 |
| `U/test_retention_pointer_whitespace.py` | 9 | 16:18:41 | 22.89/24.21/26.31 |
| `probes.py` | 2 | 16:19:20 | 23.50/24.17/26.20 |
| `F/test_retention_dependency_review_r3.py` | 6 | 16:19:40 | 20.40/23.40/25.87 |
| `U/test_retention_dependency_aliases.py` | 10 | 16:19:50 | 20.40/23.30/25.81 |
| `F/test_retention_recovery_census.py` | 2 | 16:19:57 | 20.52/23.28/25.79 |
| `F/test_retention_dependency_review_r2.py` | 21 | 16:20:07 | 22.61/23.62/25.88 |
| `U/test_retention_alternates_review_r2.py` | 15 | 16:20:54 | 26.03/24.35/26.01 |
| `F/test_retention_dependency_review_r1.py` | 18 | 16:21:10 | 26.17/24.46/26.02 |
| `F/test_retention_dependency_incomplete.py` | 19 | 16:21:38 | 28.22/25.03/26.17 |
| `U/test_retention_alternates.py` | 23 | 16:22:52 | 25.40/24.15/25.70 |
| `U/test_retention_job_dependencies.py` | 12 | 16:24:30 | 24.19/24.96/25.90 |
| `F/test_retention_dependencies.py` | 30 | 16:24:58 | 18.07/23.40/25.30 |
| `F/test_retention_dependency_spelling.py` | 16 | 16:26:05 | 14.34/21.05/24.26 |
| `U/test_retention_dependency_units.py` | 19 | 16:26:30 | 13.03/20.22/23.87 |
| `U/test_retention_folded_pin.py` | 38 | 16:27:00 | 16.17/20.26/23.75 |
| `U/test_retention_dependency_properties.py` | 4 | 16:28:39 | 27.27/22.73/24.30 |
| `U/test_retention_shared_folders.py` | 62 | 16:34:05 | 21.54/24.60/24.98 |
| `F/test_detached_retention_fence.py` | 89 | 16:42:38 | 19.64/20.60/22.73 |
| `F/test_writer_retention_fence.py` | 23 | 16:51:38 | 14.37/18.04/20.33 |
| `U/test_retention_archive.py` | 108 | 16:51:57 | 15.43/18.05/20.28 |
| `U/test_retention_archive_properties.py` | 2 | 17:02:39 | 22.17/19.79/19.78 |
| `U/test_retention_nested_leases.py` | 27 | 17:04:46 | 32.58/25.61/22.17 |
| `U/test_retention_salvage_properties.py` | 2 | 17:18:41 | 51.24/50.00/35.88 |
| `U/test_retention_sweep_race.py` | 13 | 17:31:18 | 106.71/106.63/77.73 |
| `U/test_retention_81_notes.py` | 38 | 17:53:07 | 24.84/51.30/70.23 |
| `F/test_turn_retention_cwd.py` | 6 | 17:56:16 | 42.98/46.97/64.59 |
| `F/test_turn_retention_cwd_properties.py` | 1 | 17:57:12 | 34.96/44.04/62.40 |
| `F/test_turn_retention_job_directory.py` | 2 | 17:58:00 | 39.80/43.67/61.19 |
| `F/test_turn_retention_job_properties.py` | 1 | 17:58:32 | 43.39/44.14/60.75 |
| `F/test_turn_retention_quarantine.py` | 10 | 17:59:56 | 36.51/41.30/58.06 |
| `U/test_task_holds.py` | 64 | 18:01:15 | 48.58/44.19/57.67 |
| `F/test_task_hold_admission.py` | 49 | 18:02:05 | 48.88/44.99/57.20 |
| `F/test_state_contract.py` | 37 | 18:02:58 | 52.46/46.88/57.22 |
| `U/test_test_module_names.py` | 1 | 18:14:15 | 35.88/41.29/49.33 |
| `U/test_invariants_index.py` | 25 | 18:14:42 | 41.55/42.17/49.42 |


## Named mutants

Each named killer also passes on unmutated head source. Every mutant has passing setup and teardown and fails its named test assertion. R2/R3/R4/R5 below denote their respective `tests/fake/test_retention_dependency_review_rN.py` files; P is the preserved scratch alternates probe, and W is the new whitespace unit file. Definitions, logs and per-phase/source-hash JSON are preserved in the data folder.

| Mutant | Named killing test | Result |
|---|---|---|
| `r2-post-read-identity-check-dropped` | `R2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace]` | Killed |
| `r2-terminal-legacy-incomplete-dropped` | `R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database]` | Killed |
| `r3-post-traversal-alias-check-dropped` | `R3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live]` | Killed |
| `r3-census-moved-after-recovery` | `R3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy]` | Killed |
| `r3-write-metadata-check-dropped` | `P::test_changes[same-size-running]` | Killed |
| `r3-final-path-check-dropped` | `P::test_changes[symlink-info-running]` | Killed |
| `r3-terminal-census-live-only` | `R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting]` | Killed |
| `r4-initial-objects-revalidation-dropped` | `R4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live]` | Killed |
| `r4-initial-objects-recording-dropped` | `R4::test_stable_objects_alias_keeps_the_tree_git_reads[live]` | Killed |
| `r5-commondir-whitespace-stripping-restored` | `R5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing]` | Killed |
| `r5-gitfile-whitespace-stripping-restored` | `R5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading]` | Killed |
| `r5-gitfile-colon-without-space-accepted` | `W::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space]` | Killed |


## PR-body lines for the hub

Fix round r5 closes R5-F1 by preserving Git pointer pathname whitespace: gitfiles require the literal `gitdir: ` prefix, and gitfile/commondir targets lose only trailing CR/LF. All three real-Git regressions now pass; discovery records the storage Git actually reads. Retention remains dormant; approving review r6 and completed CI remain merge gates.

- **Changes:** Apply review r5's trial exactly, remove only its three repaired expected failures, and audit all pointer readers. Other readers outside dependency discovery remain unchanged.
- **Evidence:** Review r5 red **57 passed / 3 failed** at `7eec7fa80` → green **60 passed**; **917 distinct ordinary passes**, **12 named mutants killed**, and a real-Git whitespace property with 100 generated plus two explicit examples.
- **Invariants:** For every pointer Git follows, discovery records the target Git reads, or reports incomplete. Preserve C-8.3, C-8.4's normative fail-closed guarantee and the documented observation bounds; transactional rechecks remain database reads and strings only.
- **Historical evidence:** Fix round r4 repaired the original R4-F1/R4-F2 inputs. Fix round r3 repaired the original R3-F1/R3-F2 inputs. Review r5 identified the separate R5-F1 parsing gap, repaired in this round; earlier 452/682/851-node results and CI snapshots remain dated evidence.
- **Next:** Obtain approving **review r6** and complete every CI shard before merge or re-enablement. Retention remains dormant.

The saved PR snapshot's historical line replacements are in [pr-historical-replacements.json](2026-10-30-retention-pins-fix-r5-data/pr-historical-replacements.json), updated to distinguish the r3/r4 original repairs from R5-F1's completed repair.

## Not run

`tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` is deliberately deselected. The full suite, Python 3.12, external CI and `tests/frontend/test_core_live.py` are not run; the live frontend is person-only on a lane. No live PR/check status is read or changed. No other requested local node remains unrun.

## Delivery and cleanup

Fix commit: `9082caeece4d4b420710c583f9b07ccf439762c3`; property/mutant commit: `28e624095`. This report, the current desktop/main descriptions and the completed ledger form the final text commit. Every commit has the requested co-author footer. The final branch is bundled as `retention-fix272r5.bundle` in the assigned workspace, excluding `7eec7fa80`; bundle creation and verification are the final delivery checks. All owned hold drivers and pytests ended before guarded cleanup. Combined scratch measured **606.3 MiB** before removal; both roots are verified absent. No bytecode, pytest cache, Hypothesis store, build output or kept failure root from this build remains outside its committed evidence.

One read-only fixture directory needed owner write permission before the repeated guarded cleanup; only owned non-symlink directories were changed. The cleanup record verifies both roots absent.
