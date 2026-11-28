VERDICT #272: REQUEST_CHANGES

Review r11 at **546b63ea170319705f2642da459663353dce6989**, against **475f6327890e3d76330890b0a26be37a50658ab9**. R10-F1's original cycle regressions are repaired. **R11-F1 is a new P2 introduced by the return filters:** a still-readable Git dependency can be omitted with complete discovery, after which retention retires its owner. This meets this round's stop rule and does not request a deeper reader.

Started **2026-10-31 16:08:30 UTC**; first admitted pytest **2026-10-31 16:12:39 UTC**. The macOS temporary folder was denied, so scratch is `/private/tmp/sf-r272r11`; scripts, tests, notes and receipts are durable in this worktree. The four-hour hold-start cutoff is 20:08:30 UTC. No sub-agents were used, per the lane rules.

## New findings

**R11-F1 — P2: a case-only rename makes the new filter silently drop a readable object store.**

Locations: [subfleet/dependencies.py:168 (found filter)](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/subfleet/dependencies.py:168), [subfleet/dependencies.py:297 (entries filter)](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/subfleet/dependencies.py:297), and their [predicate](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/subfleet/dependencies.py:174). The existing [alias observation](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/subfleet/dependencies.py:237) / [comparison](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/subfleet/dependencies.py:301) do not observe this spelling change.

Executed inputs are in [the new strict regression file](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/tests/fake/test_retention_dependency_review_r11.py). An allocated finished owner contains either (1) a repository borrowed by a normal `git clone --shared`, or (2) the pack directory reached by a link directly inside another repository's `objects`. A running borrower reads the owner's private commit. After canonicalization has returned the old spelling, one real rename changes `objects` to `OBJECTS`, or `packs` to `PACKS`, before the return filter runs. The tree root, object-store inode and contents stay unchanged. Both spellings still resolve on the tested case-insensitive volume; fsck and the identical private-commit read succeed before and after the rename.

The predicate rejects the earlier canonical spelling even though `os.stat` succeeds. `_walk` retains the supplied case for ordinary components, so `_target` still sees the same lexical result, device/inode and link chain. The moved check therefore reports no incomplete evidence. Head records only the two stable checkout storage paths, retires the required owner, and changes the same read from 0 to 128. The baseline keeps the third path inside the owner and leaves the read working. Both inputs stay within the promised alternates/direct-entry routes; neither uses a listed deeper-reader difference, an invalid Git layout, ABA or a dependency introduced after discovery.

| Route | Reader | Git places | Incomplete | fsck before / after rename | Same read before / after rename / after retention | Owner pruned |
|---|---|---:|---:|---|---|---|
| alternates | baseline | 3 | 0 | 0 / 0 | 0 / 0 / 0 | No |
| scan | baseline | 3 | 0 | 0 / 0 | 0 / 0 / 0 | No |
| alternates | head | 2 | 0 | 0 / 0 | 0 / 0 / 128 | Yes |
| scan | head | 2 | 0 | 0 / 0 | 0 / 0 / 128 | Yes |


The initial `--runxfail` run is **2 baseline passes / 2 head failures**, both at the owner-retention assertion, with passing setup/teardown. The ordinary file is **2 passed / 2 strict expected failures** naming R11-F1. [Red receipt](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/review-r11-data/r11-red.json); [ordinary receipt](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/review-r11-data/r11-green.json); [full records and Git results](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/review-r11-data/r11-case-comparisons.json).

**Fix:** retain or re-spell readable rejected locations, or detect their spelling change and fail closed. Comparing only the current `_target` tuple is insufficient. A small trial adds the canonical spelling to each readable alias observation and lets the existing post-spelling comparison detect its change. It skips that extra spelling observation when the original target is unreadable, preserving complete unused cycles. The [in-memory trial diff](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/review-r11-data/r11-spelling-revalidation-trial.diff) passes **57 selected nodes**, including all four new controls, the 20 cycle cases, locations and the spelling property. Removing that trial observation restores the head regression and kills the added named trial mutant. Product source is unchanged; mutation hashes are in the receipts.

## R10-F1 and earlier findings

| Finding | Executed input/status at head |
|---|---|
| R10-F1 | Both original self-suffix and two-link-suffix scans pass, with healthy Git reads and complete evidence. All 20 new cycle tests pass, including nine route rows and two retargeting races. |
| R9-F1 | All four original Data-volume inputs pass, as do eight case/normalization variants and two home-firmlink controls; both normal and linked-TMPDIR files pass. |
| R8-F1 | Both original pointer-file/admin-path cases pass. Independent forced-owner-removal controls still turn the identical owner-commit read from 0 to 128, confirming the dependency is real. |
| R7-F1 | Original three cases and both parent-hint cases pass, including valid and vanished targets without the former false global pin. |
| R6-F1 / R6-F2 | All 16 r6 cases and seven pointer-failure cases pass: named admin own stores, blank/absent commondir and oversized/unreadable pointers remain covered. |

These statuses refer to their original inputs; R11-F1 is a new changing-spelling input on the covered routes.

## Decisions 1–6

1. **Built as specified; preservation fails in R11-F1.** One predicate is applied to `found`'s canonical strings and to `self.entries`. `links`, `files`, `scan`, the walker and the original check body are unchanged. All seven linked round files were independently instrumented: **402 records and 521 storage returns**. Every returned string was checked against the return equations, with physical link entries exempt from following the final link. There were **226 physical-entry predicate rejections** and **zero nonphysical rejections among returned records**. Those 226 entries must remain exempt. The five routine controls equal the baseline exactly (2, 3, 3, 3, 0 places). This audit cannot detect omitted readable places; the new rename inputs do.

   A plain routine record makes **3 → 7** canonical calls. In the repeated 30-pair, alternating-order sample, median CPU time is **1.510 → 1.663 ms**, and median wall time **1.845 → 2.511 ms**. The earlier high-load 15-pair wall medians were 1.89 → 21.32 ms, with pauses up to 261 ms even at baseline; they do not isolate canonicalization cost. The measured CPU increment is about 0.15 ms per ordinary record, with no routine-repository retirement failure. Raw samples and loads are retained in `route-independent.json` and `routine-cost-repeat.json`.

2. **The move is implemented and useful, but does not cover every change.** Both list portions are built before `check`. Nothing in that gap writes `incomplete`; `locations` reads the captured sets, `recorded`, and the live spelling of `start`/entry paths, then performs string ancestry/coverage checks. The original check does not depend on a side effect from the old ordering. The two supplied race tests retarget inside `locations`, which is after the old check position and before the auxiliary list is built. Both pass at head. Moving check back kills the named resolved-target race at its evidence assertion. R11-F1 nevertheless leaves the observation tuple unchanged and gets no evidence. An executed controlled spelling-exception probe also shows the ordering change: baseline reaches check once, head zero times; both propagate the exception and return no record. That exception path is not a silently usable record.

3. **No stable-input evidence moved in the executed comparison grid.** There are **281** same-filesystem base/head comparisons: 240 calls from the two unchanged strategy modules across normal/linked runs, nine route rows, five routine controls, 18 longer-prefix rows and nine independent return-equation rechecks. These include repeated parameter recipes on fresh filesystem fixtures. Full incomplete lists, including order, file and reason, match entry for entry; the two strategy modules' full records also match. Unused cycles remain complete. The literal “every input” statement needs the stable-input qualification: decision 2 deliberately adds evidence to changes in its old gap. R11-F1 instead has equal empty evidence lists while losing the only owner dependency. That is the P2.

4. **Tests match the requested construction, with the oracle distinction made explicit.** The 20-case module has nine spelling/evidence rows, nine healthy-sibling cases and two races. `expected_error_files` is compared as a list, preserving order and any repeats; all nine baseline file lists match. The healthy-sibling unit test alone hardcodes two current storage places and checks a subset, so it does not itself execute 475f63278. The round's companion `test_route_comparison_against_475f6327890e` does execute that code on the same sibling with the cycle removed; its nine stored comparisons exist and match. This review independently reran that comparison for all nine rows. The race's placement is the named gap and its reversion fails. Both strategy modules and the entire contract are byte-identical to baseline; only the strict decorator was removed from r10's file. [Byte/AST audit](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/review-r11-data/byte-audit.json).

5. **Named mutants hold.** All three r10, four r9 and nine r8 mutants die at their named call assertions; all 16 match the fix-r10 table. The 13 older ones match r9 and all nine r8 ones also match r8. Setup and teardown pass. The seven previously explained survivors were not rerun. An added mutant removes the trial spelling observation and fails the new alternates regression at owner retention. Full table below.

6. **The stable-cycle text describes the nine fixtures, but cannot justify the general drop.** The cycle invariant, route expansion beyond the scan-only trial, unchanged contract and listed boundaries are supported. “Every dropped baseline string raises ELOOP” is true of the nine stored cycle fixtures; as an unrestricted statement it is false for R11-F1, where stat and Git reads succeed. “Nothing can be read through it” is true of those cyclic targets, not of every string rejected by the predicate. The desktop's broad assurance about changes during spelling also needs the observation qualification below. No temporary R10 listed-difference line was added. The 92/96 counts are consistent: all 92 planned holds have receipts, plus `baseline-route-files`, `red-cycles`, `red-cycles-final`, and `red-review-r10`. Neither count means the two guarded properties finished.

## Checks 1–6 and route comparison

**1. Routes:** the hub's unmodified seven-route script ran at baseline and head, and the two-link scan/alternates forms ran independently. The nine rows match the round's fsck/read, incomplete counts and unspelled counts exactly. Both equations were evaluated independently for every returned string, including recording the raw fixed-point result for physical links before applying their exemption. Below, `<B>` is the common fixture base, and `name×n` or `(a/b)×n` is a lossless repeated-component abbreviation. Every listed incomplete reason is ELOOP; full dictionaries and unabridged strings are in [the route receipts](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/review-r11-data/route-comparisons.json).

| Route / cycle | Base → head fsck/read | Incomplete files, base = head (order preserved) | Base unspelled → head |
|---|---|---|---|
| control / self-suffix | 0/0 → 0/0 | [] | [] → [] |
| scan / self-suffix | 0/0 → 0/0 | [] | `<B>/loop/tail×64` → [] |
| alternates / self-suffix | 0/0 → 0/0 | `<B>/loop/tail×64`; `<B>/loop/tail×64/info/alternates` | `<B>/loop/tail×65` → [] |
| alternates-tail / self-suffix | 0/0 → 0/0 | `<B>/loop/tail×64/x`; `<B>/loop/tail×64/x/info/alternates` | `<B>/loop/tail×65/x` → [] |
| gitfile / self-suffix | 128/128 → 128/128 | `<B>/loop/tail×64`; `<B>/loop/tail×128/objects`; `<B>/loop/tail×64/objects/info/alternates`; `<B>/loop/tail×64/commondir` | `<B>/loop/tail×65`; `<B>/loop/tail×129/objects` → [] |
| commondir / self-suffix | 128/128 → 128/128 | `<B>/loop/tail×64`; `<B>/loop/tail×128/objects`; `<B>/loop/tail×64/objects/info/alternates` | `<B>/loop/tail×65`; `<B>/loop/tail×129/objects` → [] |
| objects-alternates / self-suffix | 0/0 → 0/0 | `<B>/loop/tail×64`; `<B>/loop/tail×64/info/alternates` | `<B>/loop/tail×65` → [] |
| scan / two-link-suffix | 0/0 → 0/0 | [] | `<B>/other/(tail/extra)×32/tail` → [] |
| alternates / two-link-suffix | 0/0 → 0/0 | `<B>/loop/(extra/tail)×32`; `<B>/loop/(extra/tail)×32/info/alternates` | `<B>/loop/(extra/tail)×33` → [] |


The incomplete count sequence is **0, 0, 2, 2, 4, 3, 2, 0, 2**. Head returns no unspelled nonphysical string on these routes. R11-F1 is outside this cycle-only grid and removes a readable place instead.

**2. Earlier inputs:** the original findings and case/normalization/home variants above pass. The executed listed-difference controls below preserve their documented boundaries.

**3. Terminal paths and transactions:** the four database-only terminal/gone-target controls pass for legacy/recorded jobs, with and without old incomplete evidence. No pending output means no new discovery and no terminal Git pin. The two link-retarget/unlink fence controls and all six unused-target width/lifetime controls pass with `record` and `canonical` forbidden inside the transaction. A full maintenance pass with an active legacy borrower asserts that every canonical call occurs outside the SQLite transaction and still retires an unrelated owner. The round adds no filesystem work to database consumers.

**4. Linked TMPDIR:** all seven requested files pass with `link-tmp -> real-tmp`. The cycle file resolves its input base before expected-path comparisons. No new assertion compares a canonical recorded path with an unresolved TMPDIR path. This review's new r11 file also runs under linked TMPDIR, with two baseline passes and the same two strict expected failures. Both spelling runs execute 55 distinct generated inputs across all 33 position/spelling classes, without unsupported-volume skips.

**5. CI filesystem:** the new code/tests need Git, symlinks and the already-used macOS spelling API, with no new package or native helper. The 18 extra route runs use physical fixture bases of 180 and 260 bytes reached through a link. The longest pinned error file is **913 bytes**, below the measured **1024-byte PATH_MAX**. These assertions necessarily depend on path length: the 128-tail object path is base length + 653 bytes, so an arbitrary base of 371 bytes would not fit that bound. The tested 260-byte base leaves room for the supplied CI-style temporary layout. No CI result was fetched; the hub owns that check.

**6. The two previously unfinished properties:**

| Property | Own hold start (UTC) | Start load 1/5/15 | Outcome |
|---|---|---|---|
| Regenerable output, only/all dropped and rest restored | 2026-10-31T16:13:28.526095+00:00 | 103.17/111.03/113.70 | Passed; 439.13 s wrapper, 429.35 s pytest |
| Early fences equal reservation before/after quarantine | 2026-10-31T16:21:17.388838+00:00 | 128.03/109.71/110.46 | Nine-minute guard; 548.89 s including cleanup; no assertion failure |

The unfinished early-fence hold is not a finding. The regenerable property has no live/exporting borrower requiring the modified discovery path. The early-fence property can call discovery for legacy admission; its only new cost is the bounded per-place spelling predicate measured above. There is no new loop or search depth in either property.

## Listed differences

These are the existing documented boundaries; none holds this verdict.

| Difference | Executed input/result | One line for the doc |
|---|---|---|
| D-R6-1 | Spaced admin, trimmed decoy, spaced common alias and physical spaced common through survey/archive; legacy selection differs while the private HEAD remains recoverable in the controls. | Outside discovery, archive and survey keep their legacy whitespace parsing. |
| D-R6-2 | Nested gitfile target ending in space or tab is classified using the trimmed name. | Nested gitfile classification strips target bytes. |
| D-R6-3 | `common ` plus a `common` twin: registration cleanup removes the twin admin and retains the actual one. | Registration cleanup strips common-directory output. |
| D-R6-4 | A complete standalone bundle with a spaced common alias verifies while the verifier constructs a stripped alternate. | Bundle verification strips common targets. |
| D-R7-1 | A 1,048,577-byte unused-parent alternates line preserves healthy fsck/read but globally pins an unrelated owner with incomplete evidence. | Unused inferred-parent alternates still report read failures. |
| D-R8-1 | `refs/heads/main -> owner/branch`: complete discovery omits that deeper link, and forced owner removal changes read 0 → 128. | Links deeper than one level remain unread unless reached on another tracked path. |
| D-R8-2 | Unused direct link plus inside-owner/tree/worktrees/home/fixture-root/slash widths: equal/contained targets pin only while active; broad ancestors do not pin descendants; terminal complete Git pins end. | Auxiliary discovery may retain unused target/input links; only equal/contained paths pin, and complete Git pins end with the job. |

The existing 64-hop walker is unchanged. The quiet-stop probe still shows that canonicalization can reach a later acyclic target while Git rejects the original overlong link route; no extension of that reader is requested.

## Holds, counts, and omissions

Current durable totals: **817 distinct passing product nodes** and **71 report-control nodes**, across **117 completed invocations**. Repeated linked runs count once in the passing union; intentional red and mutant failures and the repair trial are excluded. Every invocation used the shared lock; peak pytest concurrency is **2**, longest hold **548.89 s**, peak pytest RSS **173.89 MiB**. No outside-lock fallback was needed. The wrapper's sysctl header is sandbox-denied; os.getloadavg vectors inside admitted pytest are retained. Environment setup ran once through uv_sync_hold with the locked dev group and Python 3.14.

All requested holds ended. Of 816 requested original nodes, 815 passed; only the explicitly guarded early-fence property remains incomplete. No requested hold is unrun. Full node coverage is in completion.json. The archive live-lsof node was explicitly deselected: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`.

The collection-only attempt `collect-earlier` exited before collection because this review's manifest mistakenly included the literal turn-file glob; it was corrected and the next collection enumerated all 743 earlier nodes. This is a review-runner error, not a branch finding. The early-fence property has its own incomplete hold, and the new head cases are strict expected failures. The six environment-only archive failures were:

- `tests/unit/test_retention_archive.py::test_blob_only_in_a_local_branch_is_archived_not_omitted`
- `tests/unit/test_retention_archive.py::test_borrowed_or_shallow_object_store_is_scratch[alternates]`
- `tests/unit/test_retention_archive.py::test_borrowed_or_shallow_object_store_is_scratch[shallow]`
- `tests/unit/test_retention_archive.py::test_local_path_remote_is_not_a_remote`
- `tests/unit/test_retention_archive.py::test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`
- `tests/unit/test_retention_archive.py::test_survey_is_read_only_and_predicts_the_pass`

All six pass unchanged with TMPDIR under this worktree's `sf-r272r11-fixtures`; that fixture folder is removed during final cleanup. They are environment failures, not findings.

Not run: the live-lsof exclusion; full suite; Python 3.12; the person-only frontend live probe; remote CI; the seven previously explained survivors and unnamed older mutants. No real provider, live Subfleet CLI/store or caller checkout was used.

| File | Passing nodes | First passing start (first attempt if zero) (UTC) | Load 1/5/15 | Hold |
|---|---:|---|---|---|
| review-r11-data/ci_path_probes.py | 18 | 2026-10-31T16:43:49.430926+00:00 | 44.06/45.27/65.92 | ci-longer-paths |
| review-r11-data/edge_probes.py | 8 | 2026-10-31T16:31:23.691679+00:00 | 56.14/84.57/99.68 | edge-controls |
| review-r11-data/exception_probe.py | 2 | 2026-10-31T16:41:55.095652+00:00 | 37.45/45.72/69.06 | exception-order |
| review-r11-data/listed_probes.py | 2 | 2026-10-31T16:41:34.542331+00:00 | 37.12/46.26/69.80 | listed-deep-unused |
| review-r11-data/performance_probe.py | 1 | 2026-10-31T16:42:59.246484+00:00 | 38.92/44.89/67.05 | routine-cost-repeat |
| review-r11-data/reader_probes.py | 8 | 2026-10-31T16:31:59.830286+00:00 | 50.85/80.29/97.52 | reader-differences |
| review-r11-data/rename_probe.py | 2 | 2026-10-31T16:32:42.743507+00:00 | 48.83/75.63/94.91 | case-rename |
| review-r11-data/round_probes.py | 16 | 2026-10-31T16:22:45.244929+00:00 | 128.37/115.29/112.62 | route-independent |
| review-r11-data/transaction_probes.py | 14 | 2026-10-31T16:26:12.057379+00:00 | 100.95/107.29/109.70 | transaction-controls |
| tests/fake/test_retention_dependencies.py | 30 | 2026-10-31T16:33:43.064016+00:00 | 42.99/69.12/91.16 | earlier-test_retention_dependencies-1 |
| tests/fake/test_retention_dependency_incomplete.py | 19 | 2026-10-31T16:34:23.958704+00:00 | 40.76/65.17/88.70 | earlier-test_retention_dependency_incomplete-1 |
| tests/fake/test_retention_dependency_review_r1.py | 18 | 2026-10-31T16:35:14.747798+00:00 | 41.78/61.59/86.02 | earlier-test_retention_dependency_review_r1-1 |
| tests/fake/test_retention_dependency_review_r10.py | 12 | 2026-10-31T16:12:39.203569+00:00 | 108.09/113.39/114.66 | round-test_retention_dependency_review_r10 |
| tests/fake/test_retention_dependency_review_r11.py | 2 | 2026-10-31T17:01:02.429058+00:00 | 59.29/59.94/61.41 | r11-green |
| tests/fake/test_retention_dependency_review_r2.py | 21 | 2026-10-31T16:35:33.369223+00:00 | 41.73/60.28/84.98 | earlier-test_retention_dependency_review_r2-1 |
| tests/fake/test_retention_dependency_review_r3.py | 6 | 2026-10-31T16:36:53.704314+00:00 | 45.11/56.86/81.45 | earlier-test_retention_dependency_review_r3-1 |
| tests/fake/test_retention_dependency_review_r4.py | 53 | 2026-10-31T16:36:11.492304+00:00 | 42.61/58.48/83.33 | earlier-test_retention_dependency_review_r4-1 |
| tests/fake/test_retention_dependency_review_r5.py | 60 | 2026-10-31T16:38:23.216574+00:00 | 43.13/53.49/77.70 | earlier-test_retention_dependency_review_r5-1 |
| tests/fake/test_retention_dependency_review_r6.py | 16 | 2026-10-31T16:45:37.141961+00:00 | 53.74/47.34/64.26 | earlier-test_retention_dependency_review_r6-1 |
| tests/fake/test_retention_dependency_review_r7.py | 3 | 2026-10-31T16:40:33.054983+00:00 | 40.33/48.40/72.21 | earlier-test_retention_dependency_review_r7-1 |
| tests/fake/test_retention_dependency_review_r8.py | 2 | 2026-10-31T16:47:09.488087+00:00 | 60.36/51.48/64.07 | earlier-test_retention_dependency_review_r8-1 |
| tests/fake/test_retention_dependency_review_r9.py | 4 | 2026-10-31T16:20:01.792368+00:00 | 99.63/102.86/108.43 | round-test_retention_dependency_review_r9 |
| tests/fake/test_retention_dependency_spelling.py | 16 | 2026-10-31T16:41:05.189377+00:00 | 41.10/47.82/71.17 | earlier-test_retention_dependency_spelling-1 |
| tests/fake/test_retention_parent_hint.py | 2 | 2026-10-31T16:48:02.528076+00:00 | 59.51/52.90/63.90 | earlier-test_retention_parent_hint-1 |
| tests/fake/test_retention_pointer_failures.py | 7 | 2026-10-31T16:41:43.968527+00:00 | 36.49/45.83/69.37 | earlier-test_retention_pointer_failures-1 |
| tests/fake/test_retention_recovery_census.py | 2 | 2026-10-31T16:49:23.639230+00:00 | 58.95/54.79/63.60 | earlier-test_retention_recovery_census-1 |
| tests/fake/test_state_contract.py | 37 | 2026-10-31T16:43:24.542458+00:00 | 41.35/44.97/66.43 | earlier-test_state_contract-1 |
| tests/fake/test_turn_retention_cwd.py | 6 | 2026-10-31T16:47:01.964318+00:00 | 63.03/51.68/64.29 | earlier-test_turn_retention_cwd-1 |
| tests/fake/test_turn_retention_cwd_properties.py | 1 | 2026-10-31T16:51:56.392376+00:00 | 49.95/54.00/61.97 | earlier-test_turn_retention_cwd_properties-1 |
| tests/fake/test_turn_retention_job_directory.py | 2 | 2026-10-31T16:48:28.373121+00:00 | 58.91/53.42/63.70 | earlier-test_turn_retention_job_directory-1 |
| tests/fake/test_turn_retention_job_properties.py | 1 | 2026-10-31T16:53:54.387721+00:00 | 59.84/56.15/61.74 | earlier-test_turn_retention_job_properties-1 |
| tests/fake/test_turn_retention_quarantine.py | 10 | 2026-10-31T16:50:57.152883+00:00 | 56.75/55.81/63.15 | earlier-test_turn_retention_quarantine-1 |
| tests/unit/test_invariants_index.py | 25 | 2026-10-31T16:55:44.645154+00:00 | 56.71/55.55/60.78 | earlier-test_invariants_index-2 |
| tests/unit/test_retention_81_notes.py | 38 | 2026-10-31T16:56:15.111043+00:00 | 59.97/56.23/60.83 | earlier-test_retention_81_notes-2 |
| tests/unit/test_retention_alternates.py | 23 | 2026-10-31T16:59:26.279835+00:00 | 65.75/60.29/61.67 | earlier-test_retention_alternates-2 |
| tests/unit/test_retention_alternates_review_r2.py | 15 | 2026-10-31T17:03:40.843624+00:00 | 43.81/53.98/58.79 | earlier-test_retention_alternates_review_r2-1 |
| tests/unit/test_retention_archive.py | 108 | 2026-10-31T17:03:45.230178+00:00 | 42.70/53.58/58.62 | earlier-test_retention_archive-1 |
| tests/unit/test_retention_archive_properties.py | 2 | 2026-10-31T17:08:29.354294+00:00 | 35.90/43.22/52.41 | earlier-test_retention_archive_properties-2 |
| tests/unit/test_retention_dependency_aliases.py | 10 | 2026-10-31T17:09:58.994824+00:00 | 40.89/42.62/51.23 | earlier-test_retention_dependency_aliases-1 |
| tests/unit/test_retention_dependency_properties.py | 4 | 2026-10-31T17:09:48.172793+00:00 | 37.16/41.97/51.15 | earlier-test_retention_dependency_properties-1 |
| tests/unit/test_retention_dependency_units.py | 19 | 2026-10-31T17:21:34.441400+00:00 | 51.46/49.52/50.25 | earlier-test_retention_dependency_units-1 |
| tests/unit/test_retention_folded_pin.py | 38 | 2026-10-31T17:16:29.072433+00:00 | 38.02/44.59/49.64 | earlier-test_retention_folded_pin-1 |
| tests/unit/test_retention_job_dependencies.py | 12 | 2026-10-31T17:17:05.812373+00:00 | 42.93/45.03/49.60 | earlier-test_retention_job_dependencies-1 |
| tests/unit/test_retention_location_cycles.py | 20 | 2026-10-31T16:15:51.101630+00:00 | 100.47/106.33/111.34 | round-test_retention_location_cycles |
| tests/unit/test_retention_location_inputs.py | 1 | 2026-10-31T16:31:39.139703+00:00 | 56.46/83.27/98.95 | round-test_retention_location_inputs |
| tests/unit/test_retention_location_spelling.py | 11 | 2026-10-31T16:29:51.085183+00:00 | 74.70/98.31/105.86 | round-test_retention_location_spelling |
| tests/unit/test_retention_locations.py | 22 | 2026-10-31T16:31:00.084412+00:00 | 52.04/86.39/100.75 | round-test_retention_locations |
| tests/unit/test_retention_nested_leases.py | 27 | 2026-10-31T17:18:55.415373+00:00 | 55.16/46.64/49.51 | earlier-test_retention_nested_leases-2 |
| tests/unit/test_retention_pointer_inputs.py | 2 | 2026-10-31T16:32:32.439364+00:00 | 49.54/77.14/95.78 | round-test_retention_pointer_inputs |
| tests/unit/test_retention_pointer_whitespace.py | 9 | 2026-10-31T17:30:50.427385+00:00 | 61.60/53.79/51.49 | earlier-test_retention_pointer_whitespace-1 |
| tests/unit/test_retention_qos.py | 8 | 2026-10-31T17:21:14.617410+00:00 | 55.31/50.03/50.44 | earlier-test_retention_qos-1 |
| tests/unit/test_retention_regenerable_properties.py | 1 | 2026-10-31T16:13:28.526095+00:00 | 103.17/111.03/113.70 | unfinished-regenerable |
| tests/unit/test_retention_salvage_properties.py | 2 | 2026-10-31T17:21:50.682511+00:00 | 50.91/49.50/50.23 | earlier-test_retention_salvage_properties-2 |
| tests/unit/test_retention_shared_folders.py | 61 | 2026-10-31T17:26:04.706425+00:00 | 46.06/47.79/49.31 | earlier-test_retention_shared_folders-2 |
| tests/unit/test_retention_sweep_race.py | 13 | 2026-10-31T17:33:17.264268+00:00 | 63.07/55.91/52.60 | earlier-test_retention_sweep_race-2 |
| tests/unit/test_retention_worktrees.py | 15 | 2026-10-31T17:43:36.017308+00:00 | 43.35/48.07/51.07 | earlier-test_retention_worktrees-1 |
| tests/unit/test_test_module_names.py | 1 | 2026-10-31T17:41:19.723352+00:00 | 43.15/51.55/52.67 | earlier-test_test_module_names-1 |


| Hold | Start (UTC) | Load 1/5/15 | Seconds | Passed / failed / skipped | Exit | Locked |
|---|---|---|---:|---|---:|---|
| round-test_retention_dependency_review_r10 | 2026-10-31T16:12:39.203569+00:00 | 108.09/113.39/114.66 | 165.85 | 12/0/0 | 0 | True |
| unfinished-regenerable | 2026-10-31T16:13:28.526095+00:00 | 103.17/111.03/113.70 | 439.13 | 1/0/0 | 0 | True |
| round-test_retention_location_cycles | 2026-10-31T16:15:51.101630+00:00 | 100.47/106.33/111.34 | 162.18 | 20/0/0 | 0 | True |
| collect-earlier | 2026-10-31T16:18:57.729684+00:00 | 99.62/102.74/108.75 | 41.28 | 0/0/0 | 4 | True |
| round-test_retention_dependency_review_r9 | 2026-10-31T16:20:01.792368+00:00 | 99.63/102.86/108.43 | 135.71 | 4/0/0 | 0 | True |
| unfinished-early-fences | 2026-10-31T16:21:17.388838+00:00 | 128.03/109.71/110.46 | 548.89 | 0/0/0 | 2 | True |
| route-independent | 2026-10-31T16:22:45.244929+00:00 | 128.37/115.29/112.62 | 188.67 | 16/0/0 | 0 | True |
| transaction-controls | 2026-10-31T16:26:12.057379+00:00 | 100.95/107.29/109.70 | 209.07 | 14/0/0 | 0 | True |
| round-test_retention_location_spelling | 2026-10-31T16:29:51.085183+00:00 | 74.70/98.31/105.86 | 63.44 | 11/0/0 | 0 | True |
| collect-earlier-corrected | 2026-10-31T16:30:32.930712+00:00 | 55.46/89.93/102.39 | 39.72 | 0/0/0 | 0 | True |
| round-test_retention_locations | 2026-10-31T16:31:00.084412+00:00 | 52.04/86.39/100.75 | 30.41 | 22/0/0 | 0 | True |
| edge-controls | 2026-10-31T16:31:23.691679+00:00 | 56.14/84.57/99.68 | 29.78 | 8/0/0 | 0 | True |
| round-test_retention_location_inputs | 2026-10-31T16:31:39.139703+00:00 | 56.46/83.27/98.95 | 46.42 | 1/0/0 | 0 | True |
| reader-differences | 2026-10-31T16:31:59.830286+00:00 | 50.85/80.29/97.52 | 34.84 | 8/0/0 | 0 | True |
| round-test_retention_pointer_inputs | 2026-10-31T16:32:32.439364+00:00 | 49.54/77.14/95.78 | 70.17 | 2/0/0 | 0 | True |
| case-rename | 2026-10-31T16:32:42.743507+00:00 | 48.83/75.63/94.91 | 18.20 | 2/0/0 | 0 | True |
| earlier-test_retention_dependencies-1 | 2026-10-31T16:33:43.064016+00:00 | 42.99/69.12/91.16 | 36.72 | 20/0/0 | 0 | True |
| r11-red | 2026-10-31T16:33:47.407421+00:00 | 42.99/69.12/91.16 | 20.05 | 2/2/0 | 1 | True |
| earlier-test_retention_dependencies-2 | 2026-10-31T16:34:16.345969+00:00 | 39.96/65.86/89.22 | 54.22 | 10/0/0 | 0 | True |
| earlier-test_retention_dependency_incomplete-1 | 2026-10-31T16:34:23.958704+00:00 | 40.76/65.17/88.70 | 64.12 | 19/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r1-1 | 2026-10-31T16:35:14.747798+00:00 | 41.78/61.59/86.02 | 29.22 | 18/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r2-1 | 2026-10-31T16:35:33.369223+00:00 | 41.73/60.28/84.98 | 68.65 | 20/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r2-2 | 2026-10-31T16:35:51.437228+00:00 | 43.73/59.78/84.37 | 13.52 | 1/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r4-1 | 2026-10-31T16:36:11.492304+00:00 | 42.61/58.48/83.33 | 59.83 | 20/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r3-1 | 2026-10-31T16:36:53.704314+00:00 | 45.11/56.86/81.45 | 32.47 | 6/0/0 | 0 | True |
| linked-test_retention_dependency_review_r10 | 2026-10-31T16:37:19.687353+00:00 | 47.01/56.36/80.56 | 43.68 | 12/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r4-2 | 2026-10-31T16:37:33.541994+00:00 | 45.77/55.62/79.87 | 45.65 | 20/0/0 | 0 | True |
| linked-test_retention_location_cycles | 2026-10-31T16:38:09.241659+00:00 | 44.90/54.34/78.43 | 33.15 | 20/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r5-1 | 2026-10-31T16:38:23.216574+00:00 | 43.13/53.49/77.70 | 50.25 | 20/0/0 | 0 | True |
| linked-test_retention_dependency_review_r9 | 2026-10-31T16:38:45.952539+00:00 | 41.66/52.50/76.78 | 22.47 | 4/0/0 | 0 | True |
| linked-test_retention_location_spelling | 2026-10-31T16:39:13.030170+00:00 | 40.94/51.27/75.50 | 56.82 | 11/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r5-3 | 2026-10-31T16:39:17.643998+00:00 | 40.94/51.27/75.50 | 67.77 | 20/0/0 | 0 | True |
| linked-test_retention_locations | 2026-10-31T16:40:16.718580+00:00 | 38.93/48.73/72.89 | 23.80 | 22/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r7-1 | 2026-10-31T16:40:33.054983+00:00 | 40.33/48.40/72.21 | 26.61 | 3/0/0 | 0 | True |
| linked-test_retention_location_inputs | 2026-10-31T16:40:45.962131+00:00 | 40.54/48.18/71.85 | 42.76 | 1/0/0 | 0 | True |
| earlier-test_retention_dependency_spelling-1 | 2026-10-31T16:41:05.189377+00:00 | 41.10/47.82/71.17 | 33.64 | 16/0/0 | 0 | True |
| listed-deep-unused | 2026-10-31T16:41:34.542331+00:00 | 37.12/46.26/69.80 | 14.12 | 2/0/0 | 0 | True |
| earlier-test_retention_pointer_failures-1 | 2026-10-31T16:41:43.968527+00:00 | 36.49/45.83/69.37 | 17.51 | 7/0/0 | 0 | True |
| exception-order | 2026-10-31T16:41:55.095652+00:00 | 37.45/45.72/69.06 | 11.91 | 2/0/0 | 0 | True |
| linked-test_retention_pointer_inputs | 2026-10-31T16:42:08.041099+00:00 | 38.24/45.49/68.57 | 96.09 | 2/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r4-3 | 2026-10-31T16:42:13.798273+00:00 | 41.58/46.06/68.63 | 39.53 | 13/0/0 | 0 | True |
| routine-cost-repeat | 2026-10-31T16:42:59.246484+00:00 | 38.92/44.89/67.05 | 19.75 | 1/0/0 | 0 | True |
| earlier-test_state_contract-1 | 2026-10-31T16:43:24.542458+00:00 | 41.35/44.97/66.43 | 50.12 | 20/0/0 | 0 | True |
| ci-longer-paths | 2026-10-31T16:43:49.430926+00:00 | 44.06/45.27/65.92 | 59.62 | 18/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r5-2 | 2026-10-31T16:44:21.214336+00:00 | 45.56/45.38/65.23 | 68.76 | 20/0/0 | 0 | True |
| mutant-r10-check-before-spelling | 2026-10-31T16:44:55.976733+00:00 | 45.69/45.49/64.47 | 25.65 | 0/1/0 | 1 | True |
| mutant-r10-entries-filter-dropped | 2026-10-31T16:45:31.201041+00:00 | 51.54/46.80/64.17 | 20.92 | 0/1/0 | 1 | True |
| earlier-test_retention_dependency_review_r6-1 | 2026-10-31T16:45:37.141961+00:00 | 53.74/47.34/64.26 | 78.10 | 16/0/0 | 0 | True |
| mutant-r10-found-filter-dropped | 2026-10-31T16:45:57.333011+00:00 | 53.63/47.77/64.03 | 24.86 | 0/1/0 | 1 | True |
| mutant-r8-alias-location-recording-dropped | 2026-10-31T16:46:33.638594+00:00 | 62.02/50.38/64.22 | 27.85 | 0/1/0 | 1 | True |
| earlier-test_turn_retention_cwd-1 | 2026-10-31T16:47:01.964318+00:00 | 63.03/51.68/64.29 | 77.53 | 6/0/0 | 0 | True |
| earlier-test_retention_dependency_review_r8-1 | 2026-10-31T16:47:09.488087+00:00 | 60.36/51.48/64.07 | 45.26 | 2/0/0 | 0 | True |
| earlier-test_retention_parent_hint-1 | 2026-10-31T16:48:02.528076+00:00 | 59.51/52.90/63.90 | 43.88 | 2/0/0 | 0 | True |
| earlier-test_turn_retention_job_directory-1 | 2026-10-31T16:48:28.373121+00:00 | 58.91/53.42/63.70 | 45.55 | 2/0/0 | 0 | True |
| mutant-r8-intermediate-link-walk-dropped | 2026-10-31T16:48:57.006379+00:00 | 64.41/55.33/64.10 | 26.51 | 0/1/0 | 1 | True |
| earlier-test_retention_recovery_census-1 | 2026-10-31T16:49:23.639230+00:00 | 58.95/54.79/63.60 | 32.39 | 2/0/0 | 0 | True |
| mutant-r8-lexical-dotdot | 2026-10-31T16:49:38.844502+00:00 | 61.66/55.57/63.72 | 23.47 | 0/1/0 | 1 | True |
| earlier-test_state_contract-2 | 2026-10-31T16:50:03.529468+00:00 | 59.69/55.69/63.53 | 45.56 | 17/0/0 | 0 | True |
| mutant-r8-link-location-followed | 2026-10-31T16:50:04.634481+00:00 | 59.69/55.69/63.53 | 28.01 | 0/1/0 | 1 | True |
| mutant-r8-parent-hint-scanned | 2026-10-31T16:50:44.709236+00:00 | 58.80/56.18/63.36 | 19.83 | 0/1/0 | 1 | True |
| earlier-test_turn_retention_quarantine-1 | 2026-10-31T16:50:57.152883+00:00 | 56.75/55.81/63.15 | 81.83 | 10/0/0 | 0 | True |
| mutant-r8-pointer-file-location-recording-dropped | 2026-10-31T16:51:08.530335+00:00 | 51.92/54.82/62.67 | 40.73 | 0/1/0 | 1 | True |
| earlier-test_turn_retention_cwd_properties-1 | 2026-10-31T16:51:56.392376+00:00 | 49.95/54.00/61.97 | 109.84 | 1/0/0 | 0 | True |
| mutant-r8-routine-skip-dropped | 2026-10-31T16:52:25.360200+00:00 | 51.61/54.01/61.69 | 24.41 | 0/1/0 | 1 | True |
| mutant-r8-top-level-links-dropped | 2026-10-31T16:52:58.439257+00:00 | 53.16/54.25/61.48 | 51.28 | 0/1/0 | 1 | True |
| mutant-r8-walk-revalidation-dropped | 2026-10-31T16:53:54.387379+00:00 | 59.84/56.15/61.74 | 29.45 | 0/1/0 | 1 | True |
| earlier-test_turn_retention_job_properties-1 | 2026-10-31T16:53:54.387721+00:00 | 59.84/56.15/61.74 | 100.48 | 1/0/0 | 0 | True |
| mutant-r9-alternates-canonicalization-dropped | 2026-10-31T16:54:39.828165+00:00 | 55.01/55.18/61.09 | 42.81 | 0/1/0 | 1 | True |
| mutant-r9-scan-canonicalization-dropped | 2026-10-31T16:55:34.838166+00:00 | 55.87/55.37/60.78 | 28.39 | 0/1/0 | 1 | True |
| earlier-test_invariants_index-2 | 2026-10-31T16:55:44.645154+00:00 | 56.71/55.55/60.78 | 16.22 | 5/0/0 | 0 | True |
| earlier-test_retention_81_notes-2 | 2026-10-31T16:56:15.111043+00:00 | 59.97/56.23/60.83 | 168.05 | 18/0/0 | 0 | True |
| route-equations-final | 2026-10-31T16:56:19.854073+00:00 | 62.21/56.76/60.99 | 52.79 | 9/0/0 | 0 | True |
| mutant-r9-start-spelling-dropped | 2026-10-31T16:57:34.898165+00:00 | 68.09/59.70/61.75 | 29.78 | 0/1/0 | 1 | True |
| earlier-test_invariants_index-1 | 2026-10-31T16:58:24.217068+00:00 | 63.27/59.69/61.63 | 34.22 | 20/0/0 | 0 | True |
| earlier-test_retention_81_notes-1 | 2026-10-31T16:59:12.464319+00:00 | 55.81/58.22/60.99 | 164.44 | 20/0/0 | 0 | True |
| earlier-test_retention_alternates-2 | 2026-10-31T16:59:26.279835+00:00 | 65.75/60.29/61.67 | 28.11 | 3/0/0 | 0 | True |
| mutant-r9-text-canonicalization-dropped | 2026-10-31T17:00:03.514962+00:00 | 56.45/58.87/61.11 | 51.01 | 0/1/0 | 1 | True |
| r11-green | 2026-10-31T17:01:02.429058+00:00 | 59.29/59.94/61.41 | 58.64 | 2/0/2 | 0 | True |
| r11-trial | 2026-10-31T17:02:04.583662+00:00 | 55.36/58.85/60.90 | 95.30 | 57/0/0 | 0 | True |
| earlier-test_retention_alternates-1 | 2026-10-31T17:02:04.589862+00:00 | 55.36/58.85/60.90 | 89.07 | 20/0/0 | 0 | True |
| earlier-test_retention_alternates_review_r2-1 | 2026-10-31T17:03:40.843624+00:00 | 43.81/53.98/58.79 | 19.90 | 15/0/0 | 0 | True |
| earlier-test_retention_archive-1 | 2026-10-31T17:03:45.230178+00:00 | 42.70/53.58/58.62 | 98.00 | 20/0/0 | 0 | True |
| mutant-r11-trial-spelling-revalidation-dropped | 2026-10-31T17:04:05.270927+00:00 | 37.62/51.78/57.86 | 20.52 | 0/1/0 | 1 | True |
| earlier-test_retention_archive-2 | 2026-10-31T17:04:30.579961+00:00 | 42.38/51.81/57.70 | 127.25 | 20/0/0 | 0 | True |
| earlier-test_retention_archive-3 | 2026-10-31T17:05:28.873411+00:00 | 38.36/48.86/56.19 | 74.27 | 17/3/0 | 1 | True |
| earlier-test_retention_archive-4 | 2026-10-31T17:06:42.654936+00:00 | 35.49/45.87/54.49 | 58.04 | 19/1/0 | 1 | True |
| earlier-test_retention_archive-5 | 2026-10-31T17:06:45.191592+00:00 | 35.05/45.61/54.34 | 112.74 | 18/2/0 | 1 | True |
| earlier-test_retention_archive-6 | 2026-10-31T17:07:46.823106+00:00 | 42.11/45.29/53.60 | 35.94 | 8/0/0 | 0 | True |
| earlier-test_retention_archive_properties-2 | 2026-10-31T17:08:29.354294+00:00 | 35.90/43.22/52.41 | 71.45 | 1/0/0 | 0 | True |
| earlier-test_retention_archive_properties-1 | 2026-10-31T17:08:43.110057+00:00 | 35.52/42.90/52.19 | 70.75 | 1/0/0 | 0 | True |
| earlier-test_retention_dependency_properties-1 | 2026-10-31T17:09:48.172793+00:00 | 37.16/41.97/51.15 | 358.63 | 1/0/0 | 0 | True |
| earlier-test_retention_dependency_aliases-1 | 2026-10-31T17:09:58.994824+00:00 | 40.89/42.62/51.23 | 17.80 | 10/0/0 | 0 | True |
| earlier-test_retention_dependency_properties-2 | 2026-10-31T17:10:26.526977+00:00 | 44.99/43.38/51.25 | 241.65 | 1/0/0 | 0 | True |
| earlier-test_retention_dependency_properties-4 | 2026-10-31T17:14:36.000328+00:00 | 44.24/46.62/50.93 | 100.58 | 1/0/0 | 0 | True |
| archive-path-controls | 2026-10-31T17:15:53.052413+00:00 | 43.03/46.02/50.35 | 60.16 | 6/0/0 | 0 | True |
| earlier-test_retention_folded_pin-1 | 2026-10-31T17:16:29.072433+00:00 | 38.02/44.59/49.64 | 31.31 | 20/0/0 | 0 | True |
| earlier-test_retention_job_dependencies-1 | 2026-10-31T17:17:05.812373+00:00 | 42.93/45.03/49.60 | 99.77 | 12/0/0 | 0 | True |
| earlier-test_retention_dependency_properties-3 | 2026-10-31T17:17:05.812410+00:00 | 42.93/45.03/49.60 | 260.45 | 1/0/0 | 0 | True |
| earlier-test_retention_nested_leases-2 | 2026-10-31T17:18:55.415373+00:00 | 55.16/46.64/49.51 | 130.35 | 7/0/0 | 0 | True |
| earlier-test_retention_qos-1 | 2026-10-31T17:21:14.617410+00:00 | 55.31/50.03/50.44 | 25.66 | 8/0/0 | 0 | True |
| earlier-test_retention_dependency_units-1 | 2026-10-31T17:21:34.441400+00:00 | 51.46/49.52/50.25 | 86.87 | 19/0/0 | 0 | True |
| earlier-test_retention_salvage_properties-2 | 2026-10-31T17:21:50.682511+00:00 | 50.91/49.50/50.23 | 236.64 | 1/0/0 | 0 | True |
| earlier-test_retention_folded_pin-2 | 2026-10-31T17:23:11.257574+00:00 | 48.26/48.90/49.93 | 321.57 | 18/0/0 | 0 | True |
| earlier-test_retention_shared_folders-2 | 2026-10-31T17:26:04.706425+00:00 | 46.06/47.79/49.31 | 114.87 | 18/0/0 | 0 | True |
| earlier-test_retention_shared_folders-4 | 2026-10-31T17:28:12.171039+00:00 | 52.30/50.28/50.13 | 225.15 | 1/0/0 | 0 | True |
| earlier-test_retention_nested_leases-1 | 2026-10-31T17:28:45.490433+00:00 | 52.75/50.53/50.22 | 104.89 | 20/0/0 | 0 | True |
| earlier-test_retention_pointer_whitespace-1 | 2026-10-31T17:30:50.427385+00:00 | 61.60/53.79/51.49 | 84.41 | 9/0/0 | 0 | True |
| earlier-test_retention_shared_folders-6 | 2026-10-31T17:32:12.112482+00:00 | 53.82/53.07/51.42 | 43.76 | 1/0/0 | 0 | True |
| earlier-test_retention_salvage_properties-1 | 2026-10-31T17:32:28.106370+00:00 | 55.64/53.52/51.60 | 406.06 | 1/0/0 | 0 | True |
| earlier-test_retention_sweep_race-2 | 2026-10-31T17:33:17.264268+00:00 | 63.07/55.91/52.60 | 478.90 | 1/0/0 | 0 | True |
| earlier-test_retention_shared_folders-1 | 2026-10-31T17:39:24.575189+00:00 | 48.51/55.55/54.05 | 66.84 | 20/0/0 | 0 | True |
| earlier-test_retention_shared_folders-3 | 2026-10-31T17:40:35.858785+00:00 | 46.52/53.40/53.35 | 20.19 | 1/0/0 | 0 | True |
| earlier-test_retention_shared_folders-5 | 2026-10-31T17:41:02.450870+00:00 | 43.70/52.22/52.92 | 79.11 | 20/0/0 | 0 | True |
| earlier-test_test_module_names-1 | 2026-10-31T17:41:19.723352+00:00 | 43.15/51.55/52.67 | 14.47 | 1/0/0 | 0 | True |
| r11-linked | 2026-10-31T17:41:38.902719+00:00 | 42.92/51.11/52.49 | 26.79 | 2/0/2 | 0 | True |
| earlier-test_retention_sweep_race-1 | 2026-10-31T17:42:27.355492+00:00 | 40.94/49.19/51.70 | 62.98 | 12/0/0 | 0 | True |
| earlier-test_retention_worktrees-1 | 2026-10-31T17:43:36.017308+00:00 | 43.35/48.07/51.07 | 50.28 | 15/0/0 | 0 | True |


## Mutant table

The first 16 are the requested named head mutants. The added r11 row removes the repair-trial observation and restores the failing head behavior. Assertions and source hashes are retained in [mutant-results.json](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/review-r11-data/mutant-results.json) and each hold receipt.

| Mutant | Named killer | Result | Same r9 assertion |
|---|---|---|---|
| r8-alias-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[admin-path] | Killed | Yes |
| r8-pointer-file-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[gitfile] | Killed | Yes |
| r8-intermediate-link-walk-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[intermediate-HEAD] | Killed | Yes |
| r8-lexical-dotdot | tests/unit/test_retention_locations.py::test_dotdot_applies_after_the_link_before_it | Killed | Yes |
| r8-link-location-followed | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[gitfile-target-HEAD] | Killed | Yes |
| r8-top-level-links-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[git-entry-HEAD] | Killed | Yes |
| r8-parent-hint-scanned | tests/unit/test_retention_locations.py::test_inferred_parent_is_not_scanned | Killed | Yes |
| r8-walk-revalidation-dropped | tests/unit/test_retention_locations.py::test_changed_link_chain_with_the_same_final_target_is_incomplete | Killed | Yes |
| r8-routine-skip-dropped | tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged[worktree] | Killed | Yes |
| r9-text-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | Killed | Yes |
| r9-alternates-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[alternates] | Killed | Yes |
| r9-scan-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | Killed | Yes |
| r9-start-spelling-dropped | tests/unit/test_retention_location_spelling.py::test_routine_alias_keeps_the_exact_record[data-volume-worktree] | Killed | Yes |
| r10-found-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[alternates-self-suffix] | Killed | New |
| r10-entries-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[scan-self-suffix] | Killed | New |
| r10-check-before-spelling | tests/unit/test_retention_location_cycles.py::test_scan_retargeted_into_cycle_before_spelling_is_incomplete[resolved-target] | Killed | New |
| r11-trial-spelling-revalidation-dropped | tests/fake/test_retention_dependency_review_r11.py::test_return_filter_keeps_readable_store_across_case_rename[alternates] | Killed | New |


## PR-body lines to change

Replace the new lead with:

> **Review r11 at 546b63ea1 requests changes for R11-F1 (P2).** R10-F1's cycle regressions pass, but the new return predicate can drop a still-readable Git dependency after a case-only rename during spelling. The existing alias observation sees the same lexical target, inode and link chain, so discovery stays complete. Two healthy real-Git inputs retire a running borrower's required owner and change the same read from 0 to 128; baseline 475f63278 preserves the pin. Retention stays dormant pending repair, approving rereview and completed CI.

Scope the evidence sentence as: “Every string dropped in the nine stable cycle fixtures raised ELOOP.” Add: “Review r11's readable case-rename inputs are a separate return-filter regression (R11-F1).” The 92 planned / 96 total invocation counts need no correction; keep the explicit statement that two fix-r10 properties were unfinished.

For the discovery docstring/desktop paragraph, replace the broad change-detection implication with current factual text until repaired:

> Non-link storage and scan strings are filtered by both spelling equations. This removes unreadable cyclic targets, but the current filter can also omit readable case aliases during discovery without incomplete evidence (R11-F1). Revalidation compares walked targets, identities, link chains and pointer contents; it does not yet observe a case-only name change that leaves those values unchanged.

The main report's r10 section and fix report should retain their dated nine-route results and add the r11 finding rather than generalizing the cycle fixture's unreadability to every rejected string. Keep C-8.3/C-8.4 and the listed reader boundaries unchanged.

Replace Next with: “Repair R11-F1 without extending metadata depth; rerun its strict regressions, cycle and spelling controls; obtain approving review and the hub's all-pass CI/mergeability checks. Activation remains separate.”

## Delivery

No product source or pre-existing test was edited. Added deliverables are this report, [the strict r11 tests](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/tests/fake/test_retention_dependency_review_r11.py), and [review-r11-data scripts, tables, notes and receipts](/Users/maxghenis/.subfleet/worktrees/20261031-120716-pr272-review-r11/review-r11-data/notes.md). The working repository was not committed, fetched, merged, pushed or published; no GitHub call was made. Nothing was staged; deliverables remain untracked. Fixture Git commits/transfers use local isolated repositories only. All test runs ended before cleanup. The final scratch measurement was 577,990,656 allocated bytes (about 551 MiB); the worktree archive fixture added 3,883,008 bytes. Scratch and both worktree fixture roots were removed with the guarded cleanup; cleanup.json records the permission repair confined to one owned directory and all removals.

DONE
