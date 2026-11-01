VERDICT #272: APPROVE

Fix rounds r11 and r12 hold at `5d3a56ff5`. I found no P1, no P2, and no P3 that either round introduced. Head keeps every pin `475f63278` keeps on every input I compared. It drops only strings that `stat` refuses with `ELOOP`, and `incomplete` never changed. One wording point ("because its path holds a live cycle") is true on a filesystem that holds still but too broad as a general statement; it's under Listed differences with corrected text and doesn't hold the verdict.

**Setup.** Started 21:10:26Z (load 95.23). Scratch was granted at `$(getconf DARWIN_USER_TEMP_DIR)sf-r272r13`, not `/private/tmp`. Every hold ran with `TMPDIR` reached through two links (`/var` → `/private/var` and `scratch/link` → `scratch/real`), so they match CI's path trap. Holds ran from 21:20:28Z to 00:29:23Z; the red replay and cost probe ran by 00:40Z. Scratch is removed.

## Earlier findings at head (rows)

| Finding | Test at head | Result |
|---|---|---|
| R12-F1 | `test_retention_dependency_review_r12.py::test_filter_keeps_readable_normalization_length[alternates,scan]` | 2 pass. With `92a8635a0`'s reader swapped in: 2 fail at the owner-retention assertion (line 107), baselines 2 pass. **Fixed.** |
| R11-F1 | `test_retention_dependency_review_r11.py::test_return_filter_keeps_readable_store_across_case_rename[alternates,scan]` and the readable file's `test_original_rename_record_equals_baseline[alternates,scan]` | 4 pass. With `ced02912b`'s reader: 2 fail / 2 pass. **Fixed.** |
| R10-F1 | review r10 `test_returned_cycle_target_obeys_the_claimed_spelling_invariant[self-suffix,two-link-suffix]` | 2 pass |
| R9-F1 | review r9 `test_firmlink_metadata_location_keeps_the_tree_real_git_reads[4 layouts]` | 4 pass |
| R9-F1 other-case / other-normalization | review r10 `test_other_spelling_preserves_owner_through_retention` | 8 pass |
| Home firmlink | review r10 `test_users_home_firmlink_preserves_owner_through_retention[gitfile,objects-pack]` | 2 pass |
| R8-F1 | review r8 `test_running_job_keeps_tree_containing_required_git_pointer[admin-path,gitfile]` | 2 pass |
| R7-F1 | review r7 `test_unused_parent_pointer_must_not_globally_pin_trees` (2) and `test_named_own_objects_swap_keeps_actual_storage` (1) | 3 pass |
| R6-F1 / R6-F2 | review r6 `test_empty_common_target_keeps_the_own_store_git_reads` (12) and `test_crlf_padded_pointer_keeps_the_storage_real_git_reads` (4) | 16 pass |

## Decisions of `brief-fix-r12.md`

1. **ELOOP alone: built and holds.** The product line is exactly the hub's trial. The four questions are answered below.
2. **What stayed: built.** `git diff ced02912b 5d3a56ff5 -- subfleet/` touches only `dependencies.py`:
   - the docstring;
   - the predicate in the `found` filter;
   - the new `_unreadable_location`;
   - the predicate in the `entries` filter.

   `_target`, `check`, `_walk` and building the list before `check` are byte-identical to `ced02912b`.
3. **The cost (D-R12-1): built.** Executed rows (`test_real_enametoolong_cycle`, linked `TMPDIR`):

   | Route, base | `475f63278` (places / incomplete) | `ced02912b` | Head |
   |---|---|---|---|
   | gitfile, 371 | 2 / 4 | 0 / 4 | 1 / 4 |
   | gitfile, 500 | 2 / 4 | 0 / 4 | 1 / 4 |
   | commondir, 371 | 3 / 3 | 1 / 3 | 2 / 3 |
   | commondir, 500 | 3 / 3 | 1 / 3 | 2 / 3 |

   - Head keeps the 1029- or 1158-byte `ENAMETOOLONG` string and drops the 701- or 830-byte `ELOOP` string. `incomplete` is equal entry for entry.
   - No routine repository reaches this: it needs a symlink cycle. All five routine controls equal `475f63278`.
4. **Tests: built, with one deviation.** The deviation is the smallest change that keeps the invariant (details below).
5. **Mutants: built.** All 19 die at the same assertions as fix r12's receipts. `r11-enametoolong-kept` is now the product, so it stays retired.
6. **Text: built.** Every sentence checked is true at head as scoped (details below).

## Parts of `brief-fix-r11.md` that r12 didn't replace
- **Decision 1's shape holds.** `_spelled_location` keeps both equations, and both filters use one predicate.
- **Decision 2 holds.** Review r11's trial is not taken, and `check` still runs after the list is built.
- **Decision 3 holds.** No false evidence (see "Did any evidence move" below). Review r11's inputs give records equal to `475f63278`: three places, no incomplete entry.
- **Decision 4 holds.** The readable module's 36 nodes pass, and both rename forms ran rather than skipped.
- **Decision 5 holds.** Its two surviving mutants die at their named assertions.
- **Decision 6 holds.** C-8.3 and C-8.4 (`docs/acceptance-contract.md`, `docs/invariants.md`) are unchanged since `475f63278`. The r11 section of the main report stays dated.

## The four questions under decision 1
- **Stable filesystem: the hub's argument holds.** I re-ran the round's own differential tests with a logger on `folders.canonical` for the found and scan places (9 cycle rows, 5 routine, 4 long-cycle rows, the 55 location and 55 spelling draws).
  - That gave **501 places over 128 inputs**. `stat` before and after `canonical` agreed in every pair: 0→0 in 436, `ENOENT`→`ENOENT` in 39, `ELOOP`→`ELOOP` in 18, `ENAMETOOLONG`→`ENAMETOOLONG` in 8.
  - No string that `stat` accepted before `canonical` raised `ELOOP` after it. Every stat-0 string meets both equations.
  - A check of the argument's premise: `canonical` resolves a 40-link acyclic chain to its physical target (stat 0).
- **A target that changes: `check` reports it.** My new row is `test_gitfile_retargeted_into_cycle_between_read_and_filter`, with a "link" and a "resolved-target" variant. It retargets once, between discovery's read and the filter's `stat`.

  | Variant | Head | `475f63278` |
  |---|---|---|
  | link | same two places, plus alias-changed evidence on `<base>/loop` | same two places, no evidence |
  | resolved-target | places dropped on `ELOOP`, plus four alias-changed entries | stale `<base>/admin` and `<base>/loop/tail/objects`, no evidence |

  Git reads 0 before and 128 after in all four cases. Head is never worse than `475f63278` here.
- **Embedded NUL: equal outcomes (a listed difference).** Both readers raise `ValueError("lstat: embedded null character in path")`. Git reads through the bytes before the NUL (read 0, fsck 0).
- **`errno` at the gate: no other reading.** The code reads exactly as the table says:
  - both equations hold: kept, with no `stat`;
  - `stat` succeeds: kept;
  - `stat` raises `ELOOP`: dropped;
  - `stat` raises any other errno: kept.

  A `ValueError` from `stat` can't arise, because `canonical` raises first on the only route that would produce one.

## Did any evidence move
**No.** My own three-way wrapper on the modules' own tests made **143 comparisons**: 18 from the cycle file, 5 routine, the location module's 55 draws, and the spelling module's 65 (55 draws, 4 r9-sibling, 6 routine-alias).
- `incomplete` is identical entry for entry at `475f63278`, `ced02912b` and `5d3a56ff5` in every one.
- Records equal `475f63278` in 127. The other 16 (cycle rows) differ only by 20 dropped strings, each refused with `ELOOP`.
- An unused cycle stays complete: the scan rows have 0 incomplete entries at all three heads.
- The readable file's own differential (128 inputs) also passes.
- Review r12's inputs give head records equal to baseline: three places, no incomplete entry.

## Decision 4 details
- **The deviation.** Head equals the baseline record minus the one shorter string, and `stat` refuses that string with `ELOOP`. The early `return` is unreachable on macOS: head recorded a result in all four rows. Where `PATH_MAX` is larger, the test fails at its first `stat` assertion, so it can't hide a row.
- **The differential.** It compares the right records: `475f63278`, `ced02912b` and head on one filesystem that holds still.
  - Location property: 55 draws give 53 distinct inputs and never reach `alternates-line` (10 of 11 positions).
  - Spelling property: 55 draws give 55 distinct classes, the full cross product.
  - 496 = 2 environments × 128 readable comparisons + 2 × 120 strategy-module comparisons. That counts comparisons, not distinct inputs, as the report says.
- **`test_filter_stat_table`.** Every row's `stat` is real; the `ENAMETOOLONG` comes from a 4096-byte component.
  - The failed equation is real for "success" (a case alias) and `ELOOP` (a suffix cycle) on the entries route.
  - It's controlled for `ENOENT`, `ENOTDIR`, `EACCES` and `ENAMETOOLONG` on both routes. The found route also stubs `_storage`.
  - A controlled row proves only how the gate dispatches on errno once an equation fails.
- **`TMPDIR` length.** `base_of_length` pads the base to the exact length, so the string lengths don't depend on `TMPDIR`. The test errors rather than skips only if the resolved `tmp_path` reaches length − 1 bytes; CI's is about 110.
- **The Unicode rename row.** It needs a volume that treats normalization forms as one name. It skips, rather than fails, where the alias doesn't resolve. Here it ran and passed.
- **The shard map.** `groups`, the comment and the validator are unchanged. `source` is rewritten; 153 files are added and none removed. Every named path exists in the branch or in `785dda05c`. Three newer files (review r11, review r12, readable) have no entry and are placed by `crc32`.

## Checks
1. **Seven routes:** `ced02912b` and head match.

   | Route | fsck / read | Returned / incomplete |
   |---|---|---|
   | control | 0 / 0 | 2 / 0 |
   | scan | 0 / 0 | 2 / 0 |
   | alternates-line | 0 / 0 | 2 / 2 |
   | alternates-line-tail | 0 / 0 | 2 / 2 |
   | gitfile | 128 / 128 | 0 / 4 |
   | commondir | 128 / 128 | 1 / 3 |
   | objects+alternates | 0 / 0 | 2 / 2 |

   No string comes back unspelled, and I read each returned string through both equations: all meet them, all stat 0.
2. **Earlier findings:** see the table above.
3. **Transactions:** `record` runs in `submit` with no transaction. Review r11's database-only probes give 32 passed. Review r12's committed `transaction_probes.py` doesn't parse (an indentation error at line 246); a copy with that one line dedented gives 32 passed. Those probes cover:
   - `dependents` inside a transaction while `canonical`, `_unreadable_location` and `record` raise if called;
   - an old job whose target is gone still retiring, in 4 cases with no discovery.
4. **`TMPDIR` through a link:** all ten files pass (24, 36, 53, 3).
5. **CI's filesystem:** nothing new beyond what CI ran green at `970ed106b`, plus review r12's file. That file needs a volume that treats normalization forms as one name and skips otherwise.
6. **Cost:** a routine record makes the same number of `os.stat` calls at both heads (44/44 plain, 49/49 worktree). Minimum times match within noise: 0.811 vs 0.777 ms, and 0.863 vs 0.869 ms.

## New findings
None at P1, P2 or P3.

## Listed differences (non-blocking)
- **"because its path holds a live cycle"** (docstring and doc). It's true for canonical strings on a filesystem that holds still. But `ELOOP` also means more than 32 links: `stat` on a 40-link acyclic chain raises `ELOOP`, and `_unreadable_location` on that raw string returns True.
  - Only a race that swaps in such a chain would bring that state to the filter. I didn't build that input (out of scope).
  - Corrected text: "…is left out: on a filesystem that holds still, `canonical` resolves every acyclic chain, so that string holds a live cycle."
- **NUL in an alternates line:** `record` raises `ValueError` at both heads; Git reads the part before the NUL. Doc line: "An alternates line with an embedded NUL makes `record` raise, as at `475f63278`."
- **Location differential coverage:** its draws skip `alternates-line`, which the spelling differential covers. The spelling differential also starts from the canonical checkout, not the alias the module's own test supplies; my three-way run covers that test's 65 inputs.
- **Retarget "link" variant:** head's alias-changed evidence adds a global pin until the job ends (an extra pin only).
- **D-R12-1 rows** already carry incomplete evidence, so in them the retained string adds nothing beyond it.
- **Salvage properties (not this PR):** `tests/unit/test_retention_salvage_properties.py`, byte-identical to `release/217`, fails both properties under my linked `TMPDIR`. It compares an unresolved path with Git's `/private/var` path, and passes with a resolved `TMPDIR` (2 passed). I couldn't explain why CI passes it at `970ed106b`; I've left that doubt for the hub.

## Holds (all linked `TMPDIR` unless marked)

| Hold | Start (UTC) | Load (1-min) | Result |
|---|---|---|---|
| Review r12/r11/r10/r9 files | 21:20 | 93 | 4 / 4 / 12 / 4 passed |
| Readable file | 21:23 | 92 | 36 passed |
| Cycles / spelling / locations | 21:31 | 96 | 20 / 11 / 22 passed |
| Location inputs / pointer inputs | 21:34 | 81 | 1 / 2 passed |
| My probe file | 21:47–22:03 | 65–90 | 10 passed |
| Transaction probes | 22:06–22:10 | 38–45 | first try: collection error (r12 probe file); then r11 file 32 passed, r12 copy 31 passed + 1 failed on a missing sibling fixture in my copy, which passed on rerun |
| Shard plugin / retry flakes | 22:11 | 41 | 50 / 72 passed |
| Earlier review files A | 22:14 | 49 | 97 passed |
| Earlier review files B | 22:19 | 37 | 144 passed |
| r8, worktrees, qos, five turn files | 22:23 | 36 | 45 passed |
| Dependency set A | 22:27 | 48 | 138 passed (12:09) |
| Dependency properties | 22:40–23:20 | 51–90 | 2 + 1 + 1 passed |
| Shared folders | 23:24–00:29 | 43–79 | 58 + 2 (11:30) + 1 + 1 passed |
| Guards | 23:46 | 110 | 37 / 1 / 25 passed |
| Archive (live `lsof` case deselected) | 23:50 | 101 | 108 passed (13:07) |
| Archive properties / nested leases / 81 notes | 00:05 | 64 | 2 / 27 / 38 passed |
| Salvage + sweep-race examples | 00:09 | 46 | sweep 12 passed; salvage 2 failed (environment, above) |
| Salvage, resolved `TMPDIR` | 00:13 | 47 | 2 passed |
| Sweep-race property | 00:22 | 45 | 1 passed |

- None of the archive file's six path-trap cases failed.
- Three holds ran past the 10-minute split target, under the rules file's 20 minutes. The tool moved them to the background itself, and I polled each to completion in the foreground.
- **Not run:**
  - `tests/frontend/test_core_live.py` (can't run on a lane);
  - the live case `test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` (deselected);
  - the full suite and Python 3.12 (left to CI);
  - the ten files under a `TMPDIR` with no link (the fix round ran those).

## Mutant table
Every mutant was killed, and every failing assertion matches fix r12's receipt.

| Mutant | Named killer | Fails at |
|---|---|---|
| r8-alias-location-recording-dropped | r8 `[admin-path]` | owner pruned |
| r8-pointer-file-location-recording-dropped | r8 `[gitfile]` | owner pruned |
| r8-intermediate-link-walk-dropped | locations `[intermediate-HEAD]` | `assert held` |
| r8-lexical-dotdot | `test_dotdot_applies_after_the_link_before_it` | admin missing |
| r8-link-location-followed | locations `[gitfile-target-HEAD]` | `assert held` |
| r8-top-level-links-dropped | locations `[git-entry-HEAD]` | `assert held` |
| r8-parent-hint-scanned | `test_inferred_parent_is_not_scanned` | unrelated path pinned |
| r8-walk-revalidation-dropped | `test_changed_link_chain_with_the_same_final_target_is_incomplete` | missing error |
| r8-routine-skip-dropped | routine `[worktree]` | `recorded == expected` |
| r9-text-canonicalization-dropped | r9 `[gitfile]` | owner pruned |
| r9-alternates-canonicalization-dropped | r9 `[alternates]` | owner pruned |
| r9-scan-canonicalization-dropped | r9 `[objects-pack]` | owner pruned |
| r9-start-spelling-dropped | routine alias `[data-volume-worktree]` | `recorded == expected` |
| r10-found-filter-dropped | cycles `[alternates-self-suffix]` | equation 1 |
| r10-entries-filter-dropped | cycles `[scan-self-suffix]` | equation 1 |
| r10-check-before-spelling | `test_scan_retargeted_into_cycle_before_spelling_is_incomplete[resolved-target]` | alias-changed evidence |
| r11-stat-gate-dropped | r11 `[alternates]` | owner pruned |
| r11-any-oserror-drops | stat table `[found-ENOENT]` | `(place in result) == (outcome != "ELOOP")` |
| r12-enametoolong-drops | r12 `[alternates]` and stat table `[found-ENAMETOOLONG]` | owner pruned; the stat-table assertion |

## PR-body lines to change
- **Add a lead paragraph:** "**Review r13 (Opus 5.5, lane job 20261031-170839-pr272-review-r13) approves fix rounds r11 and r12 at `5d3a56ff5`.** No finding. Incomplete evidence is identical to `475f63278` and `ced02912b` in 143 three-way comparisons. The seven routes match the hub's probe. All 19 named mutants die at the same assertions."
- **`Next`, item 1:** "CI at `5d3a56ff5` all-pass, MERGEABLE."
- **Optional:** after "496 same-input…", add "(128 + 120 per environment)".

## Files left in the worktree (uncommitted)
- `tests/unit/test_retention_location_review_r13.py`: my probe file. All 10 tests pass at head, so no strict mark was needed. It writes JSON into the data folder; point it at `tmp_path` before committing it.
- `docs/reports/2026-10-31-retention-pins-review-r13-data/`: notes, scripts, junit files, mutant receipts and the JSON results behind this report.

DONE