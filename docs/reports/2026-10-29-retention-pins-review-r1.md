VERDICT #272: REQUEST_CHANGES

Reviewed the whole branch at `3893c7120b38004fcd43539bb557a2cd25c319e3` against `release/217` (`e8ecb42e7`). Started 2026-10-29 03:40:10 UTC; first admitted correctness hold 03:41:35 UTC. The initial checkout was clean. Production source is unchanged; the review adds one regression file and this report/evidence. No sub-agents, live store, real provider, production CLI or PR comment was used. The reviewed repository was never fetched, merged or pushed; Git fixture setup uses only local bare remotes inside scratch. All retirement fixtures have homes under the prescribed scratch subtree.

**Findings, in priority order**

1. **P2 — C-quoted Git alternates can lose both retention pins and dependency fences.** `subfleet/dependencies.py:175–177` removes the surrounding quotes but never decodes Git's C escapes. Executed absolute and relative alternates containing `\157wner` for the ordinary `owner` tree. `git cat-file -t <commit>` successfully reads the borrowed commit before retention, but `record()` stores a nonexistent literal `\157wner` path; maintenance prunes `owner` while the borrowing job is running. Both full-retirement regressions fail at head with `pruned: ['owner']`. Fix: parse Git's C-quoted alternate format, including octal byte escapes, before resolving relative paths against the objects directory and canonicalizing. Preserve the five-level traversal. Do not merely strip quotes. Reproducer: `tests/fake/test_retention_dependency_review_r1.py::test_c8_4_git_c_quoted_alternate_keeps_the_tree_it_reads[absolute]` and `[relative]`.

2. **P2 — A terminal legacy exporter can lose the tree containing its actual output.** `subfleet/dependencies.py:286–288` reads the output lease identity and recorded `depends.out_path`, but never the exporting job's `jobs.out_path`. A legacy event has no `depends`, and terminal jobs are omitted from `legacy()`. On an insensitive volume the output ownership identity deliberately strips default-ignorables and equates dotless i with i; `dependencies.key` preserves those distinctions. Executed real submit/admission, accepted-artifact creation and terminal pending-export states under both `dotless-ı/state` and `ignored-<U+200B>/state`, replaying an old event without `depends`. Both legacy cases prune the destination tree despite its pending export; their recorded-dependency controls retain the tree. Fix: include the export holder's actual `jobs.out_path` in `_EXPORTING`/`live`, preferably with an indexed join, retaining both lease-path and submitted-path evidence. Apply no filesystem discovery inside the transaction. Reproducer: `tests/fake/test_retention_dependency_review_r1.py::test_c8_3_pending_export_keeps_its_actual_path_beside_its_lease_identity[legacy-dotless-i]` and `[legacy-default-ignorable]`.

3. **P2 — A valid alternates file above 64 KiB is silently discarded.** `subfleet/dependencies.py:138–140` applies `_FILE_LIMIT` to the entire file, catches the oversized-file `ValueError`, and returns `None`; `_alternates` treats that as no object dependencies. Executed a valid file just over 64 KiB containing repeated absolute object-directory lines. Git reads the borrowed commit, but `record()` omits the owner's storage and maintenance prunes the owner while the borrower is running. Fix: consume the complete alternates file with bounded memory, or make incomplete discovery block admission/retirement conservatively; a larger finite cap alone does not establish the guarantee. Reproducer: `tests/fake/test_retention_dependency_review_r1.py::test_c8_4_large_valid_alternates_file_keeps_the_tree_git_reads`. An in-memory larger read cap is only a proof for this particular input, not the proposed complete fix.

4. **P3 — The desktop design still says the timings are pending.** `docs/desktop/retention-archive.md:639` says “new timing measurements remain pending”, although continuation 4b completed both comparisons and the main report/contract were updated. Corrected text: “The self-join compared finished owners with live jobs. Continuation 4b completed one old/new pin-query comparison at 3,000/100 and 6,000/600 jobs/live, with matching pin sets; timings and loads are in `docs/reports/2026-10-27-retention-dependency-pins.md`.”

**Additional P2 boundary, pre-existing and outside the stated allocated-worktree scope:** `subfleet/retention.py:305–312` and `subfleet/retention_archive.py:621–642` protect job-directory turn rows/cwds, but do not apply the new dependency census to `jobs/<id>`. A queued job with a recorded output dependency on `jobs/owner/result.md` lets maintenance prune `owner` and remove that directory. The scratch `job-directory-boundary` hold executes this case; a separate release-pin-module control substitutes only `e8ecb42e7:subfleet/retention.py` in memory, retaining the current surrounding modules. That control is not a whole-release test and is not counted as a killed mutant. Source inspection and the control establish the existing boundary. To guarantee protection for every retired tree, extend dependency pinning, the job-directory fence transaction and the final recheck to the job directory as well. Preserve a distinction between this existing boundary and the three defects in the new allocated-worktree implementation. The probe source and traces are retained with the review evidence.

The three P2 issues violate the new C-8.3/C-8.4 guarantees and desktop I16. Preserve those contract requirements and fix the implementation. Until then, add this status qualification to the main report, audit resolution and desktop dependency description: “Review r1 at `3893c7120` found three remaining dependency gaps: C-quoted Git alternates are not decoded, oversized valid alternates files are silently omitted, and terminal legacy exports can lose pins when their output lease identity differs from the actual destination. The retention dependency guarantees are not yet met for those inputs.” Historical test counts, mutant counts and measurement results remain historical evidence; these findings do not invalidate the recorded executions.

**Checks 1–8**

| Check | Verdict | Executed inputs and conclusion |
|---|---|---|
| 1. Every needed place/spelling | FAIL | All required dependency/spelling/unit/folded-pin files; generated real-volume checks; 11 added explicit recording/pin/fence fixtures for case, NFC, NFD, Greek dialytika/tonos, ß/SS, ﬁ/FI, Kelvin/k, Data firmlink, symlink components, `..`, trailing slashes. Each added fixture records all three dependency roles with absent tails and checks `dependents`, `fences` and `_pin_reasons` inside a transaction. Linked checkouts and `-o` inside another job's tree pass existing end-to-end cases. C-quoted alternates, oversized valid alternates files and legacy export identities fail the new full-retirement cases above. The separate job-directory probe also fails outside the PR’s stated allocated-worktree scope. |
| 2. Atomicity | PASS for recorded evidence; encoding failures above remain | Executed existing submission-after-selection and begin/lock/quarantine/archive/final-check interleavings, selection/journal spelling rechecks, pending-export completion, and no-filesystem-in-transaction cases. `Store.transaction` uses `BEGIN IMMEDIATE`; its thread's `store.query` reads the writer connection. Selection rechecks and fence insertion share one transaction; final pin/dependent rechecks and row deletion share another. `record` and once-per-pass legacy discovery stay outside. No additional interleaving losing a correctly recorded dependency was found. |
| 3. D1 and legacy folds | PASS | Greek `ΐ`/`Ϊ́` early/transaction regression; 100-setting attribution property with all three roles, Unicode components and normalized tails; attribution-restoration mutant killed. Dependency blocking and attribution use `dependencies.key`. Legacy source blocking and source attribution both retain `folders.folded` (NFC plus casefold); modern absent sources use `folders.fold` in both places. The source and dependency domains need not use identical keys. `subfleet/folders.py` is unchanged from release. |
| 4. Submission record | FAIL in the terminal legacy-export case | An old **client** does not choose `depends`: the current daemon records it for every successful submit. A pre-feature **stored job** without `depends` gets `record()` at admission and once-per-pass legacy discovery while live, as executed by the existing legacy Git/firmlink tests. A missing gitdir named by an existing gitfile is retained, with worktrees/common fallback; a missing output filename is recorded and pinned. An initially missing review root is refused by strict validation; disappearance after validation is covered by the quarantine cases. A job exporting into another job's allocated tree pins it while live and until export completes in modern records. A terminal legacy exporter needs its row output path added as finding 2 describes. |
| 5. Performance | PASS | Read continuation 4b's two one-shot measurements: 3,000/100 = 164.445 → 10.267 ms (0.0624×), load 12.7/13.8/24.0; 6,000/600 = 1,565.193 → 32.488 ms (0.0208×), load 12.4/13.4/22.7. Revalidated old SQL byte-for-byte against local `c9430763f`, checked an untimed small synthetic store against both queries and final `_pin_reasons`, then ran the unchanged tool exactly once at 3,000/100. This review: 123.581 → 11.569 ms, 0.093616×, query loads 7.933/8.751/10.730 on both sides; 22 matching pins, 147,801 events, Python 3.14.7, SQLite 3.53.4. No tuning or second timing run. |
| 6. Evidence | PASS, with new counterexamples added | All eight requested sampled mutants killed after their actual target module loaded; table below. Four required properties completed with their original normal settings: attribution 100, independent reference 100, monotonicity 100 and fence differential 400 (plus its explicit examples). The remaining tests in the two dependency property files and shared-folder file also ran. Generated counts here describe configured settings unless terminal statistics are retained; nested properties do not emit independent terminal statistics. Three additional named mutants isolate the new gaps against an in-memory repaired source. The initial 17-case and final 18-case repair trials pass; removing each repair kills its regressions. |
| 7. Text | FAIL | Compared every PR-touched clause (C-6.5, C-6.11, C-8.3, C-8.4), related C-13.4/C-24.5/C-26.3, added changelog entries, decision note, desktop design, main report, 202-sentence audit, c4b/c5 evidence and the single live PR-body read. D1 text matches the fix. Output/Git-storage guarantees need the three P2 fixes or the explicit temporary status qualification above; desktop line 639 needs correction. The live installed state and historical 78 GB survey were supplied context and were not re-read, as required. PR-body replacements are below. |
| 8. Merge and CI | Merge PASS; CI snapshot below | At `ce9e154eb`, all release-side and branch-side added contract lines survive. All 255 clause IDs are unique, with no duplicate complete sentence in a touched clause. Release's C-27.5 survives; branch C-6.5/C-6.11/C-8.3/C-8.4 survive, followed by the four expected c5 commits. All 548 other files' byte-for-byte diffs `e8ecb42e7..ce9e154eb` equal `f1d2f116..ce8c3e985`; combined diff SHA-256 `0199c15e5fa60c734529dc4446ab7f9b68d74093ad84a4f628be14388aa88c98`. One `gh pr checks 272` read is recorded below. |

**PR-body replacements** (line numbers in the single captured body)

- Line 1: “Review r1 at `3893c7120` requests changes for three remaining dependency gaps. Continuations 1–5 and their historical evidence are retained.”
- Line 4, replace the unconditional retention sentence: “Recorded review-root, output and decoded Git-storage dependencies are checked before workspace preparation and in reservation. Review r1 found missing Git C-quote decoding, silently omitted oversized alternates files and a terminal legacy-export identity gap; those can still allow retirement of a needed tree.”
- Line 5, replace “output pins until export completes”: “output pins until export completes for recorded dependencies; the terminal legacy-export identity gap remains open”. The transaction, ownership, retention-only fence and legacy-fold statements remain true.
- Line 18, after the first sentence: “That intended guarantee still has the three r1 exceptions above.”
- Line 24, after its first sentence: “Git C-quoted alternate paths still need decoding, and valid oversized alternates files must not be treated as empty, before canonical recording.”
- Line 29, replace “these pins are the last known gap before retention can go live”: “Review r1 found three further dependency gaps that must be fixed before retention can go live.”
- Retain the c4b numbers as dated measurements. Add the new 3,000/100 result as review evidence, with its loads, rather than replacing the earlier observations.

**Delivery and permissions**

`git add tests/fake/test_retention_dependency_review_r1.py` failed with exit 128: the sandbox could not create the assigned checkout's shared `index.lock`. No index entry or commit was written, HEAD is unchanged, and no private Git metadata or alternate branch was created. The user explicitly permits leaving the files when shared metadata cannot be written. Intended commit message:

```
test: reproduce retention dependency encoding gaps found in review r1

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

The new tests deliberately retain five red assertions at head; they are executable review evidence, not expected-failure annotations. The trial repairs ran only through an in-memory import loader and are not production changes. Its C-quote decoder is a narrow proof for these inputs; the production fix should implement Git's full byte-oriented quoting rules and preserve dependencies across read bounds.


**Verification accounting**

Existing requested tests, latest complete outcome per node: **{'passed': 1269, 'xfail': 1, 'skipped': 4}**. Added head regressions: **13 passed / 5 failed**, deliberately red for the three findings. Initial in-memory repair trial: **17 passed**; final three-repair trial: **18 passed**. The three repair-removal mutants fail their expected cases. There are **181 completed pytest holds**, each with one pytest and a supplied lock admission, no xdist, no cap interruption and no outside-lock fallback. Maximum recorded pytest wall 219.80 s; maximum Python RSS 312.33 MiB. The wrapper's denied `sysctl` is not load evidence; all loads below are `os.getloadavg()` inside the admitted process.

Twelve initial holds collected zero tests because the review observer used the wrong pytest hook argument name. The observer was corrected and every requested test was run in the later holds; those startup attempts receive no test credit. One existing wording test initially failed because the review basetemp path itself contained “retention”; its hold and ordinary-writer wording were correct. It passed under a neutral basename, which the runner now uses. Neither event changed product code, tests, bounds or Hypothesis settings.

**Per-file head counts** (latest complete result, repeats and mutants excluded)

| File | Passed | Failed | Skipped | Xfail | Unfinished |
|---|---:|---:|---:|---:|---:|
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_priority.py` | 33 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_properties.py` | 27 | 0 | 0 | 1 | 0 |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_route_isolation.py` | 93 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_stateful.py` | 17 | 0 | 0 | 0 | 0 |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | 0 | 0 | 0 |
| `tests/fake/test_detached_retention_fence.py` | 89 | 0 | 0 | 0 | 0 |
| `tests/fake/test_retention_dependencies.py` | 30 | 0 | 0 | 0 | 0 |
| `tests/fake/test_retention_dependency_review_r1.py` | 13 | 5 | 0 | 0 | 0 |
| `tests/fake/test_retention_dependency_spelling.py` | 16 | 0 | 0 | 0 | 0 |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 | 0 |
| `tests/fake/test_salvage_adversarial.py` | 8 | 0 | 0 | 0 | 0 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 | 0 | 0 |
| `tests/fake/test_salvage_worktree_gone.py` | 6 | 0 | 0 | 0 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | 0 |
| `tests/fake/test_writer_retention_fence.py` | 23 | 0 | 0 | 0 | 0 |
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | 0 |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_archive.py` | 109 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_archive_properties.py` | 2 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_case_alias.py` | 56 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_cwd_rechecks.py` | 3 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_dependency_properties.py` | 4 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_dependency_units.py` | 19 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_folded_pin.py` | 38 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_nested_leases.py` | 27 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_ordinary_trees.py` | 0 | 0 | 4 | 0 | 0 |
| `tests/unit/test_retention_qos.py` | 8 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_rediscovery.py` | 17 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_regenerable.py` | 26 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_regenerable_properties.py` | 1 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_shared_folders.py` | 62 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_sweep_race.py` | 13 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_turn_contract.py` | 2 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_turn_workdirs.py` | 1 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_turns.py` | 7 | 0 | 0 | 0 | 0 |
| `tests/unit/test_retention_worktree_cwd.py` | 7 | 0 | 0 | 0 | 0 |
| `tests/unit/test_salvage.py` | 31 | 0 | 0 | 0 | 0 |
| `tests/unit/test_salvage_optional_transient.py` | 15 | 0 | 0 | 0 | 0 |
| `tests/unit/test_salvage_ref_race.py` | 9 | 0 | 0 | 0 | 0 |
| `tests/unit/test_salvage_unindexable.py` | 86 | 0 | 0 | 0 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | 0 |

**Holds** (all starts UTC on October 29; P/F/S/X/U = passed/failed/skipped/xfail/unfinished)

| Hold | Start UTC | Load 1 / 5 / 15 | P/F/S/X/U | Slot | Seconds |
|---|---|---|---|---|---:|
| `retention_dependencies` | 03:41:35.353469 | 10.88 / 9.19 / 11.78 | 30/0/0/0/0 | A | 45.34 |
| `retention_dependency_spelling` | 03:42:21.506305 | 13.94 / 10.44 / 12.11 | 16/0/0/0/0 | A | 10.12 |
| `retention_dependency_units` | 03:42:32.626416 | 13.83 / 10.54 / 12.12 | 19/0/0/0/0 | A | 25.13 |
| `folded-basic` | 03:43:09.071466 | 11.73 / 10.40 / 12.01 | 37/0/0/0/0 | A | 14.42 |
| `property-attribution` | 03:43:24.211797 | 11.29 / 10.36 / 11.96 | 1/0/0/0/0 | A | 18.78 |
| `property-reference` | 03:43:45.241418 | 11.86 / 10.56 / 12.00 | 1/0/0/0/0 | A | 42.19 |
| `property-monotone` | 03:44:28.720203 | 9.95 / 10.24 / 11.81 | 1/0/0/0/0 | A | 130.94 |
| `mutant-collate-nocase-restored` | 03:45:13.276275 | 8.73 / 9.91 / 11.60 | 0/1/0/0/0 | B | 1.35 |
| `mutant-record-raw-spelling` | 03:45:15.234793 | 8.73 / 9.91 / 11.60 | 0/1/0/0/0 | B | 1.53 |
| `mutant-attribution-back-on-folded` | 03:45:17.258956 | 8.91 / 9.92 / 11.60 | 0/1/0/0/0 | B | 1.78 |
| `mutant-transaction-own-not-filtered` | 03:45:19.516390 | 8.91 / 9.92 / 11.60 | 0/1/0/0/0 | B | 2.56 |
| `mutant-transaction-hold-names-no-place` | 03:45:22.669787 | 8.84 / 9.89 / 11.58 | 0/1/0/0/0 | B | 1.96 |
| `mutant-git-storage-no-commondir` | 03:45:25.153411 | 8.84 / 9.89 / 11.58 | 0/1/0/0/0 | B | 1.33 |
| `mutant-git-storage-no-alternates` | 03:45:26.940316 | 9.17 / 9.94 / 11.59 | 0/1/0/0/0 | B | 1.07 |
| `mutant-fences-not-retention-only` | 03:45:28.523968 | 9.17 / 9.94 / 11.59 | 0/1/0/0/0 | B | 4.53 |
| `extra-selection_never` | 03:45:33.741946 | 9.40 / 9.98 / 11.59 | 1/0/0/0/0 | B | 68.64 |
| `shared-basic` | 03:46:40.429409 | 7.67 / 9.30 / 11.21 | 60/0/0/0/0 | A | 102.47 |
| `extra-submitted_dependencies_wait` | 03:46:42.968130 | 8.10 / 9.36 / 11.22 | 1/0/0/0/0 | B | 48.94 |
| `extra-interleavings` | 03:47:32.404087 | 7.47 / 8.93 / 10.95 | 1/0/0/0/0 | B | 59.23 |
| `property-fences` | 03:48:25.685206 | 7.33 / 8.69 / 10.74 | 1/0/0/0/0 | A | 84.41 |
| `admission_disk-01` | 03:48:32.117990 | 7.58 / 8.70 / 10.72 | 15/0/0/0/0 | B | 3.49 |
| `legacy-export-probe` | 03:48:36.424200 | 7.93 / 8.75 / 10.73 | 1/1/0/0/0 | B | 2.58 |
| `state_contract` | 03:49:52.794839 | 11.25 / 9.43 / 10.81 | 37/0/0/0/0 | A | 28.21 |
| `test_module_names` | 03:50:26.583583 | 17.51 / 11.07 / 11.34 | 1/0/0/0/0 | A | 2.09 |
| `invariants_index` | 03:50:30.158068 | 17.51 / 11.07 / 11.34 | 25/0/0/0/0 | A | 1.79 |
| `quoted-alternate-probe` | 03:50:32.840831 | 17.55 / 11.19 / 11.38 | 0/1/0/0/0 | A | 1.89 |
| `admission_disk-02` | 03:50:38.137231 | 19.59 / 11.72 / 11.56 | 12/0/0/0/0 | A | 3.45 |
| `admission_held_waiters-01` | 03:50:43.007693 | 18.90 / 11.70 / 11.56 | 1/0/0/0/0 | A | 20.11 |
| `admission_latency-01` | 03:51:05.162715 | 18.27 / 11.99 / 11.67 | 1/0/0/0/0 | A | 4.47 |
| `admission_latency-02` | 03:51:10.716423 | 17.44 / 11.92 / 11.64 | 1/0/0/0/0 | A | 18.68 |
| `admission_latency-03` | 03:51:31.388947 | 15.85 / 12.03 / 11.69 | 1/0/0/0/0 | A | 13.44 |
| `admission_latency-04` | 03:51:53.922492 | 13.55 / 11.75 / 11.60 | 10/0/0/0/0 | A | 5.61 |
| `admission_latency-05` | 03:52:01.853267 | 11.92 / 11.46 / 11.50 | 1/0/0/0/0 | A | 1.18 |
| `admission_lease_waiters-01` | 03:52:03.425040 | 11.92 / 11.46 / 11.50 | 15/0/0/0/0 | A | 6.04 |
| `admission_lease_waiters-02` | 03:52:10.321918 | 11.61 / 11.41 / 11.48 | 1/0/0/0/0 | A | 14.07 |
| `admission_liveness-01` | 03:52:25.699312 | 14.29 / 12.01 / 11.69 | 12/0/0/0/0 | A | 9.43 |
| `admission_liveness-02` | 03:52:36.289345 | 15.40 / 12.39 / 11.83 | 17/0/0/0/0 | A | 10.37 |
| `admission_liveness-03` | 03:52:53.040919 | 16.88 / 12.91 / 12.03 | 2/0/0/0/0 | A | 5.83 |
| `admission_liveness-04` | 03:53:00.902434 | 16.00 / 12.80 / 12.00 | 1/0/0/0/0 | A | 86.22 |
| `admission_priority-01` | 03:54:30.955699 | 18.18 / 14.36 / 12.68 | 10/0/0/0/0 | A | 4.23 |
| `admission_priority-02` | 03:54:36.050273 | 17.21 / 14.22 / 12.64 | 11/0/0/0/0 | A | 3.21 |
| `admission_priority-03` | 03:54:40.658757 | 16.31 / 14.09 / 12.60 | 12/0/0/0/0 | A | 13.62 |
| `admission_properties-01` | 03:54:55.226776 | 14.11 / 13.71 / 12.49 | 15/0/0/0/0 | A | 3.72 |
| `admission_properties-02` | 03:54:59.580021 | 13.54 / 13.60 / 12.46 | 10/0/0/0/0 | A | 3.70 |
| `admission_properties-03` | 03:55:04.075486 | 13.02 / 13.49 / 12.43 | 2/0/0/1/0 | A | 3.38 |
| `admission_ramp-01` | 03:55:08.704527 | 13.34 / 13.54 / 12.45 | 11/0/0/0/0 | A | 6.34 |
| `admission_ramp-02` | 03:55:17.254121 | 15.85 / 14.10 / 12.66 | 3/0/0/0/0 | A | 2.68 |
| `admission_ramp-03` | 03:55:20.938077 | 15.85 / 14.10 / 12.66 | 1/0/0/0/0 | A | 6.86 |
| `admission_ramp-04` | 03:55:28.846224 | 20.43 / 15.16 / 13.06 | 1/0/0/0/0 | A | 3.18 |
| `admission_ramp-05` | 03:55:32.862439 | 19.83 / 15.12 / 13.06 | 1/0/0/0/0 | A | 8.96 |
| `admission_ramp-06` | 03:55:43.481244 | 19.35 / 15.18 / 13.10 | 2/0/0/0/0 | A | 7.44 |
| `admission_ramp_retention-01` | 03:55:54.228599 | 17.51 / 14.93 / 13.04 | 11/0/0/0/0 | A | 10.53 |
| `admission_route_isolation-01` | 03:56:06.704111 | 16.05 / 14.72 / 12.99 | 12/0/0/0/0 | A | 5.06 |
| `admission_route_isolation-02` | 03:56:12.530540 | 15.72 / 14.67 / 12.99 | 13/0/0/0/0 | A | 7.52 |
| `admission_route_isolation-03` | 03:56:20.909329 | 15.58 / 14.66 / 12.99 | 16/0/0/0/0 | A | 2.69 |
| `admission_route_isolation-04` | 03:56:24.349871 | 15.54 / 14.67 / 13.01 | 11/0/0/0/0 | A | 4.29 |
| `admission_route_isolation-05` | 03:56:29.230475 | 14.93 / 14.56 / 12.98 | 15/0/0/0/0 | A | 3.18 |
| `admission_route_isolation-06` | 03:56:33.057315 | 14.94 / 14.56 / 12.99 | 12/0/0/0/0 | A | 2.88 |
| `admission_route_isolation-07` | 03:56:36.631803 | 14.78 / 14.54 / 12.99 | 14/0/0/0/0 | A | 19.82 |
| `admission_stateful-01` | 03:56:57.447093 | 12.84 / 14.12 / 12.88 | 1/0/0/0/0 | A | 1.43 |
| `admission_stateful-02` | 03:56:59.341532 | 12.84 / 14.12 / 12.88 | 1/0/0/0/0 | A | 1.61 |
| `admission_stateful-03` | 03:57:01.385044 | 12.61 / 14.06 / 12.86 | 2/0/0/0/0 | A | 2.14 |
| `admission_stateful-04` | 03:57:04.411517 | 12.61 / 14.06 / 12.86 | 2/0/0/0/0 | A | 3.66 |
| `admission_stateful-05` | 03:57:09.392833 | 14.41 / 14.40 / 12.99 | 1/0/0/0/0 | A | 2.23 |
| `admission_stateful-06` | 03:57:12.393831 | 15.09 / 14.55 / 13.05 | 8/0/0/0/0 | A | 3.06 |
| `admission_visibility-01` | 03:57:16.857773 | 14.29 / 14.39 / 13.00 | 19/0/0/0/0 | A | 9.74 |
| `admission_visibility-02` | 03:57:28.856078 | 19.84 / 15.58 / 13.44 | 17/0/0/0/0 | A | 11.00 |
| `admission_visibility-03` | 03:57:42.227853 | 22.17 / 16.26 / 13.72 | 7/0/0/0/0 | A | 13.46 |
| `detached_retention_fence-01` | 03:57:59.395957 | 19.02 / 15.84 / 13.61 | 25/1/0/0/0 | A | 34.19 |
| `detached_retention_fence-02` | 03:58:35.909593 | 17.40 / 15.78 / 13.68 | 6/0/0/0/0 | A | 6.25 |
| `detached_retention_fence-03` | 03:58:43.676288 | 16.81 / 15.70 / 13.67 | 1/0/0/0/0 | A | 3.07 |
| `detached_retention_fence-04` | 03:58:48.853985 | 16.43 / 15.64 / 13.66 | 27/0/0/0/0 | A | 26.14 |
| `mutant-review-repair-both` | 03:58:55.621723 | 16.39 / 15.65 / 13.68 | 17/0/0/0/0 | B | 9.97 |
| `mutant-review-quoted-decode-removed` | 03:59:08.685593 | 16.33 / 15.69 / 13.73 | 0/2/0/0/0 | B | 11.75 |
| `detached_retention_fence-05` | 03:59:19.981827 | 15.02 / 15.44 / 13.66 | 1/0/0/0/0 | A | 35.27 |
| `mutant-review-export-row-removed` | 03:59:22.809136 | 14.22 / 15.26 / 13.61 | 2/2/0/0/0 | B | 10.69 |
| `admission-state-machine` | 03:59:34.094676 | 13.28 / 15.03 / 13.54 | 1/0/0/0/0 | B | 19.78 |
| `admission-provider-state-machine` | 03:59:55.277362 | 13.73 / 15.03 / 13.58 | 1/0/0/0/0 | B | 31.13 |
| `detached_retention_fence-06` | 03:59:58.885753 | 13.20 / 14.90 / 13.54 | 28/0/0/0/0 | A | 21.59 |
| `retention_dormant-01` | 04:00:25.248542 | 16.72 / 15.62 / 13.84 | 7/0/0/0/0 | A | 3.43 |
| `review-guards-state` | 04:00:28.343155 | 18.50 / 16.01 / 13.99 | 37/0/0/0/0 | B | 37.24 |
| `salvage_adversarial-01` | 04:00:30.310053 | 18.50 / 16.01 / 13.99 | 8/0/0/0/0 | A | 8.04 |
| `salvage_finalization-01` | 04:00:39.710119 | 17.04 / 15.78 / 13.93 | 10/0/0/0/0 | A | 14.71 |
| `salvage_finalization-02` | 04:01:00.580057 | 14.70 / 15.34 / 13.82 | 10/0/0/0/0 | A | 11.61 |
| `review-guards-names` | 04:01:10.225772 | 13.96 / 15.16 / 13.77 | 1/0/0/0/0 | B | 3.24 |
| `salvage_finalization-03` | 04:01:14.553280 | 13.24 / 14.99 / 13.72 | 10/0/0/0/0 | A | 11.52 |
| `review-guards-index` | 04:01:14.672944 | 13.24 / 14.99 / 13.72 | 25/0/0/0/0 | B | 1.18 |
| `review-regressions` | 04:01:17.157351 | 12.82 / 14.87 / 13.69 | 13/4/0/0/0 | B | 23.75 |
| `salvage_finalization-04` | 04:01:27.415043 | 16.04 / 15.51 / 13.93 | 8/0/0/0/0 | A | 19.37 |
| `salvage_finalization-05` | 04:01:51.072897 | 19.42 / 16.39 / 14.28 | 1/0/0/0/0 | A | 9.41 |
| `salvage_finalization-06` | 04:02:03.420005 | 17.11 / 16.02 / 14.19 | 8/0/0/0/0 | A | 39.11 |
| `salvage_worktree_gone-01` | 04:02:48.366230 | 24.78 / 17.93 / 14.96 | 6/0/0/0/0 | A | 22.35 |
| `writer_retention_fence-01` | 04:03:15.187924 | 20.48 / 17.43 / 14.87 | 20/0/0/0/0 | A | 56.63 |
| `writer_retention_fence-02` | 04:04:20.075639 | 25.20 / 19.71 / 15.94 | 3/0/0/0/0 | A | 23.38 |
| `admission_uncapped-01` | 04:04:48.885953 | 29.46 / 21.40 / 16.68 | 1/0/0/0/0 | A | 22.24 |
| `admission_uncapped-02` | 04:05:16.177725 | 25.90 / 21.18 / 16.74 | 1/0/0/0/0 | A | 14.11 |
| `admission_uncapped-03` | 04:05:31.736804 | 27.29 / 21.88 / 17.10 | 1/0/0/0/0 | A | 12.78 |
| `admission_uncapped-04` | 04:05:46.877297 | 27.65 / 22.20 / 17.29 | 11/0/0/0/0 | A | 6.73 |
| `admission_uncapped-05` | 04:05:56.289789 | 26.15 / 21.98 / 17.24 | 1/0/0/0/0 | A | 13.15 |
| `admission_uncapped-06` | 04:06:11.218479 | 22.90 / 21.46 / 17.14 | 1/0/0/0/0 | A | 13.55 |
| `admission_uncapped-07` | 04:06:25.609148 | 20.52 / 21.02 / 17.06 | 1/0/0/0/0 | A | 7.43 |
| `admission_uncapped-08` | 04:06:34.259956 | 20.35 / 20.98 / 17.09 | 1/0/0/0/0 | A | 7.34 |
| `admission_uncapped-09` | 04:06:43.658810 | 17.99 / 20.46 / 16.95 | 1/0/0/0/0 | A | 0.97 |
| `admission_uncapped-10` | 04:06:45.523567 | 17.99 / 20.46 / 16.95 | 1/0/0/0/0 | A | 1.28 |
| `admission_uncapped-11` | 04:06:47.215938 | 16.87 / 20.18 / 16.88 | 1/0/0/0/0 | A | 6.85 |
| `admission_uncapped-12` | 04:06:55.050237 | 18.48 / 20.46 / 16.99 | 1/0/0/0/0 | A | 2.79 |
| `admission_uncapped-13` | 04:06:59.191850 | 18.93 / 20.52 / 17.03 | 1/0/0/0/0 | A | 1.99 |
| `admission_uncapped-14` | 04:07:02.230607 | 18.29 / 20.36 / 17.00 | 1/0/0/0/0 | A | 5.75 |
| `admission_uncapped-15` | 04:07:08.348601 | 17.31 / 20.12 / 16.93 | 3/0/0/0/0 | A | 1.86 |
| `retention_81_notes-01` | 04:07:10.660637 | 17.31 / 20.12 / 16.93 | 9/0/0/0/0 | A | 26.71 |
| `ordinary-writer-wording-retry` | 04:07:39.387941 | 14.22 / 19.11 / 16.67 | 1/0/0/0/0 | B | 3.21 |
| `retention_81_notes-02` | 04:07:39.841198 | 14.22 / 19.11 / 16.67 | 1/0/0/0/0 | A | 8.48 |
| `retention_81_notes-03` | 04:07:50.006025 | 12.89 / 18.66 / 16.54 | 11/0/0/0/0 | A | 31.61 |
| `retention_81_notes-04` | 04:08:27.582448 | 9.90 / 17.25 / 16.13 | 14/0/0/0/0 | A | 36.46 |
| `retention_81_notes-05` | 04:09:14.735839 | 7.53 / 15.57 / 15.56 | 3/0/0/0/0 | A | 14.57 |
| `retention_archive-01` | 04:09:32.396202 | 6.76 / 14.87 / 15.31 | 11/0/0/0/0 | A | 35.95 |
| `retention_archive-02` | 04:10:16.315232 | 6.68 / 13.83 / 14.91 | 11/0/0/0/0 | A | 45.34 |
| `retention_archive-03` | 04:11:09.532710 | 7.12 / 12.72 / 14.43 | 16/0/0/0/0 | A | 81.78 |
| `retention_archive-04` | 04:12:35.238980 | 9.40 / 12.13 / 14.03 | 28/0/0/0/0 | A | 51.21 |
| `retention_archive-05` | 04:13:28.830308 | 13.65 / 12.68 / 14.11 | 10/0/0/0/0 | A | 36.70 |
| `retention_archive-06` | 04:14:07.050291 | 17.00 / 13.74 / 14.43 | 10/0/0/0/0 | A | 25.31 |
| `retention_archive-07` | 04:14:33.652755 | 15.22 / 13.57 / 14.35 | 10/0/0/0/0 | A | 19.86 |
| `retention_archive-08` | 04:14:58.668110 | 13.40 / 13.28 / 14.22 | 10/0/0/0/0 | A | 17.83 |
| `retention_archive-09` | 04:15:18.203655 | 11.99 / 12.97 / 14.09 | 3/0/0/0/0 | A | 4.37 |
| `retention_archive_properties-01` | 04:15:23.297649 | 11.99 / 12.95 / 14.08 | 1/0/0/0/0 | A | 28.68 |
| `retention_archive_properties-02` | 04:15:53.755414 | 13.29 / 13.19 / 14.12 | 1/0/0/0/0 | A | 67.72 |
| `retention_case_alias-01` | 04:17:04.367122 | 15.43 / 13.61 / 14.19 | 22/0/0/0/0 | A | 45.39 |
| `retention_case_alias-02` | 04:18:00.886943 | 17.75 / 14.59 / 14.51 | 8/0/0/0/0 | A | 29.48 |
| `retention_case_alias-03` | 04:18:35.055931 | 25.42 / 16.93 / 15.38 | 1/0/0/0/0 | A | 27.10 |
| `retention_case_alias-04` | 04:19:07.094027 | 25.00 / 17.70 / 15.71 | 24/0/0/0/0 | A | 7.19 |
| `retention_case_alias-05` | 04:19:16.929515 | 27.96 / 18.58 / 16.05 | 1/0/0/0/0 | A | 189.83 |
| `retention_cwd_rechecks-01` | 04:22:30.643130 | 22.14 / 18.57 / 16.42 | 3/0/0/0/0 | A | 6.49 |
| `retention_nested_leases-01` | 04:22:39.039614 | 21.85 / 18.63 / 16.47 | 26/0/0/0/0 | A | 87.66 |
| `job-directory-boundary` | 04:22:55.940637 | 20.27 / 18.44 / 16.43 | 0/1/0/0/0 | B | 4.94 |
| `retention_nested_leases-02` | 04:24:09.473339 | 10.54 / 15.96 / 15.67 | 1/0/0/0/0 | A | 6.20 |
| `mutant-review-job-directory-release-control` | 04:24:11.863336 | 10.33 / 15.83 / 15.62 | 0/1/0/0/0 | B | 1.71 |
| `retention_ordinary_trees-01` | 04:24:16.589005 | 10.33 / 15.83 / 15.62 | 0/0/4/0/0 | A | 5.48 |
| `retention_qos-01` | 04:24:22.750336 | 13.16 / 16.27 / 15.78 | 8/0/0/0/0 | A | 3.07 |
| `retention_readback-01` | 04:24:26.320812 | 13.16 / 16.27 / 15.78 | 34/0/0/0/0 | A | 105.45 |
| `retention_readback-02` | 04:26:14.284434 | 12.90 / 15.48 / 15.56 | 3/0/0/0/0 | A | 5.49 |
| `retention_rediscovery-01` | 04:26:20.449498 | 12.58 / 15.37 / 15.52 | 17/0/0/0/0 | A | 6.98 |
| `retention_regenerable-01` | 04:26:28.106409 | 11.95 / 15.14 / 15.44 | 10/0/0/0/0 | A | 12.69 |
| `retention_regenerable-02` | 04:26:41.451275 | 11.11 / 14.86 / 15.33 | 13/0/0/0/0 | A | 19.86 |
| `retention_regenerable-03` | 04:27:01.997689 | 10.02 / 14.32 / 15.12 | 3/0/0/0/0 | A | 3.61 |
| `retention_regenerable_properties-01` | 04:27:06.001122 | 10.02 / 14.32 / 15.12 | 1/0/0/0/0 | A | 28.17 |
| `ordinary-tree-offline-retry` | 04:27:21.097458 | 9.97 / 14.09 / 15.03 | 0/0/4/0/0 | B | 4.35 |
| `retention_salvage_properties-01` | 04:27:34.618184 | 10.80 / 14.07 / 15.00 | 1/0/0/0/0 | A | 88.23 |
| `retention_salvage_properties-02` | 04:29:03.754813 | 14.21 / 14.40 / 15.03 | 1/0/0/0/0 | A | 152.08 |
| `retention_sweep_race-01` | 04:31:37.667328 | 23.25 / 17.68 / 16.23 | 11/0/0/0/0 | A | 37.85 |
| `retention_sweep_race-02` | 04:32:21.180816 | 20.29 / 17.65 / 16.28 | 1/0/0/0/0 | A | 10.02 |
| `retention_sweep_race-03` | 04:32:36.034376 | 18.24 / 17.32 / 16.19 | 1/0/0/0/0 | A | 219.80 |
| `large-alternates-boundary` | 04:36:13.732025 | 18.75 / 17.88 / 16.66 | 0/1/0/0/0 | B | 4.32 |
| `retention_turn_contract-01` | 04:36:16.978782 | 18.75 / 17.88 / 16.66 | 2/0/0/0/0 | A | 1.57 |
| `retention_turn_workdirs-01` | 04:36:19.620314 | 18.93 / 17.93 / 16.69 | 1/0/0/0/0 | A | 5.36 |
| `retention_turns-01` | 04:36:25.987337 | 17.90 / 17.73 / 16.63 | 7/0/0/0/0 | A | 4.91 |
| `retention_worktree_cwd-01` | 04:36:31.658684 | 17.02 / 17.55 / 16.57 | 7/0/0/0/0 | A | 5.17 |
| `salvage-01` | 04:36:37.463165 | 16.36 / 17.39 / 16.52 | 12/0/0/0/0 | A | 5.51 |
| `salvage-02` | 04:36:43.793170 | 16.33 / 17.37 / 16.52 | 10/0/0/0/0 | A | 5.52 |
| `salvage-03` | 04:36:51.163120 | 15.90 / 17.26 / 16.49 | 9/0/0/0/0 | A | 5.40 |
| `salvage_optional_transient-01` | 04:36:58.348216 | 15.14 / 17.05 / 16.42 | 15/0/0/0/0 | A | 5.91 |
| `salvage_ref_race-01` | 04:37:08.739425 | 19.08 / 17.81 / 16.70 | 3/0/0/0/0 | A | 16.55 |
| `review-large-alternates-head` | 04:37:23.557599 | 18.84 / 17.83 / 16.72 | 0/1/0/0/0 | B | 16.66 |
| `salvage_ref_race-02` | 04:37:29.611354 | 18.69 / 17.82 / 16.73 | 1/0/0/0/0 | A | 33.52 |
| `mutant-review-repair-all-three` | 04:37:46.410829 | 17.07 / 17.50 / 16.63 | 18/0/0/0/0 | B | 22.03 |
| `salvage_ref_race-03` | 04:38:05.215623 | 15.49 / 17.11 / 16.51 | 5/0/0/0/0 | A | 5.09 |
| `salvage_unindexable-01` | 04:38:12.740081 | 14.57 / 16.86 / 16.43 | 15/0/0/0/0 | A | 36.17 |
| `mutant-review-alternates-cap-restored` | 04:38:13.442981 | 14.57 / 16.86 / 16.43 | 0/1/0/0/0 | B | 24.88 |
| `salvage_unindexable-02` | 04:39:04.990969 | 15.18 / 16.62 / 16.36 | 33/0/0/0/0 | A | 50.61 |
| `salvage_unindexable-03` | 04:40:05.679543 | 30.46 / 21.04 / 18.04 | 5/0/0/0/0 | A | 20.92 |
| `salvage_unindexable-04` | 04:40:34.649394 | 33.24 / 22.46 / 18.65 | 1/0/0/0/0 | A | 15.59 |
| `salvage_unindexable-05` | 04:40:57.694130 | 37.45 / 24.35 / 19.44 | 20/0/0/0/0 | A | 40.70 |
| `salvage_unindexable-06` | 04:41:48.708725 | 36.18 / 25.99 / 20.33 | 1/0/0/0/0 | A | 21.95 |
| `salvage_unindexable-07` | 04:42:19.093421 | 30.18 / 25.49 / 20.34 | 4/0/0/0/0 | A | 11.71 |
| `salvage_unindexable-08` | 04:42:35.956817 | 27.42 / 25.11 / 20.29 | 1/0/0/0/0 | A | 16.77 |
| `salvage_unindexable-09` | 04:42:58.803504 | 30.41 / 25.88 / 20.71 | 1/0/0/0/0 | A | 9.11 |
| `final-guard-contract` | 04:43:01.518291 | 30.41 / 25.88 / 20.71 | 37/0/0/0/0 | B | 36.11 |
| `salvage_unindexable-10` | 04:43:14.855655 | 29.97 / 26.02 / 20.84 | 1/0/0/0/0 | A | 36.12 |
| `final-guard-module-names` | 04:43:46.903934 | 23.07 / 24.74 / 20.56 | 1/0/0/0/0 | B | 6.00 |
| `salvage_unindexable-11` | 04:43:53.387352 | 21.52 / 24.35 / 20.47 | 1/0/0/0/0 | A | 47.32 |
| `final-guard-invariants` | 04:43:56.642934 | 21.52 / 24.35 / 20.47 | 25/0/0/0/0 | B | 2.35 |
| `salvage_unindexable-12` | 04:44:43.002477 | 27.58 / 25.34 / 21.05 | 3/0/0/0/0 | A | 18.84 |

**Zero-collection startup attempts**

| Intended hold | Start UTC | Load 1 / 5 / 15 | Tests collected |
|---|---|---|---:|
| `folded-basic` | 03:41:19.993954 | 10.04 / 8.96 / 11.75 | 0 |
| `invariants_index` | 03:41:23.932007 | 9.56 / 8.88 / 11.70 | 0 |
| `property-attribution` | 03:41:20.476904 | 10.04 / 8.96 / 11.75 | 0 |
| `property-fences` | 03:41:22.401044 | 9.56 / 8.88 / 11.70 | 0 |
| `property-monotone` | 03:41:21.378594 | 9.56 / 8.88 / 11.70 | 0 |
| `property-reference` | 03:41:20.913753 | 9.56 / 8.88 / 11.70 | 0 |
| `retention_dependencies` | 03:41:18.159612 | 10.04 / 8.96 / 11.75 | 0 |
| `retention_dependency_spelling` | 03:41:18.686197 | 10.04 / 8.96 / 11.75 | 0 |
| `retention_dependency_units` | 03:41:19.406037 | 10.04 / 8.96 / 11.75 | 0 |
| `shared-basic` | 03:41:21.906889 | 9.56 / 8.88 / 11.70 | 0 |
| `state_contract` | 03:41:22.885957 | 9.56 / 8.88 / 11.70 | 0 |
| `test_module_names` | 03:41:23.420803 | 9.56 / 8.88 / 11.70 | 0 |

**Mutants**

| Named mutant | Observed killing test | P/F/S/X/U | Result |
|---|---|---|---|
| `collate-nocase-restored` | `test_a_job_id_typed_with_letters_the_volume_folds_keeps_the_tree[recorded-submitted-all]` | 0/1/0/0/0 | killed |
| `record-raw-spelling` | `test_record_spells_each_place_once` | 0/1/0/0/0 | killed |
| `attribution-back-on-folded` | `test_c6_11_unicode_dependency_hold_names_its_place[transaction]` | 0/1/0/0/0 | killed |
| `transaction-own-not-filtered` | `test_c8_4_a_fence_on_an_in_place_writers_own_folder_that_its_output_is_in_is_named_once` | 0/1/0/0/0 | killed |
| `transaction-hold-names-no-place` | `test_c8_4_a_fence_on_a_needed_place_taken_after_the_early_look_holds_the_job_in_its_transaction[output-read-only]` | 0/1/0/0/0 | killed |
| `git-storage-no-commondir` | `test_d1_commondir_outside_the_worktrees_layout_is_followed` | 0/1/0/0/0 | killed |
| `git-storage-no-alternates` | `test_d1_alternates_are_followed` | 0/1/0/0/0 | killed |
| `fences-not-retention-only` | `test_d5_fences_reads_what_retiring_reads_place_by_place` | 0/1/0/0/0 | killed |
| `review-repair-both` | complete added regression file | 17/0/0/0/0 | green repair trial, not a killed mutant |
| `review-quoted-decode-removed` | `test_c8_4_git_c_quoted_alternate_keeps_the_tree_it_reads[absolute]`; `test_c8_4_git_c_quoted_alternate_keeps_the_tree_it_reads[relative]` | 0/2/0/0/0 | killed |
| `review-export-row-removed` | `test_c8_3_pending_export_keeps_its_actual_path_beside_its_lease_identity[legacy-dotless-i]`; `test_c8_3_pending_export_keeps_its_actual_path_beside_its_lease_identity[legacy-default-ignorable]` | 2/2/0/0/0 | killed |
| `review-job-directory-release-control` | `test_output_in_another_jobs_directory_keeps_that_directory` | 0/1/0/0/0 | release-pin-module control, not a killed mutant |
| `review-repair-all-three` | complete added regression file | 18/0/0/0/0 | green repair trial, not a killed mutant |
| `review-alternates-cap-restored` | `test_c8_4_large_valid_alternates_file_keeps_the_tree_git_reads` | 0/1/0/0/0 | killed |

`transaction-hold-names-no-place` was adapted to the c5 attribution block while preserving its removal of `needs`. The commondir mutant used the relocated-admin killer added in c4b. Other sampled definitions are unchanged. The three review mutants remove one repair from the in-memory repaired source; they are separate from the eight sampled head mutants. Every mutant has a recorded module-load marker and full failure/teardown evidence. Exact substitutions/diffs are in the data directory.

**What did not run**

The full suite, Python 3.12 and `tests/frontend/test_core_live.py` were not run locally. The frontend test is CI-only: the lane daemon person-only check blocks its probe at step 5. The other 26 historical mutant definitions were not rerun; the requested sample was eight. No 6,000/600 timing was repeated. The four ordinary-tree assertions were skipped because uv could not resolve the fixture package offline. A retry constrained to the exact wheel URL in uv.lock also skipped: that direct-URL cache entry was unavailable. Both fixture commands used --offline; no extra network or environment installation was permitted or performed. The documented fixture skips/xfail below receive no passing credit.

- `tests/fake/test_admission_properties.py::test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again` — xfail: C-6.10: the wait is keyed on its kind as well as its verdict, so a job that alternates between lease-held and slot-kept records one verdict again on every alternation (not fixed here: the probe-pending key has no decision to key on).
- `tests/unit/test_retention_ordinary_trees.py::test_an_ordinary_tree_retires_without_an_unarchived_path[clone-False]` — skipped: ('/Users/maxghenis/.subfleet/worktrees/20261028-233931-pr272-review-r1/tests/unit/test_retention_ordinary_trees.py', 54, "Skipped: ('sortedcontainers==2.4.0',) not in uv's cache: × Failed to download `sortedcontainers @\n  │ https://files.pythonhosted.org/packages/32/46/9cb0e58b2deb7f82b84065f37f3bffeb12413f947f9388e4cac22c4621ce/sortedcontainers-2.4.0-py2.py3-none-any.whl`\n  ╰").
- `tests/unit/test_retention_ordinary_trees.py::test_an_ordinary_tree_retires_without_an_unarchived_path[clone-True]` — skipped: ('/Users/maxghenis/.subfleet/worktrees/20261028-233931-pr272-review-r1/tests/unit/test_retention_ordinary_trees.py', 54, "Skipped: ('sortedcontainers==2.4.0',) not in uv's cache: × Failed to download `sortedcontainers @\n  │ https://files.pythonhosted.org/packages/32/46/9cb0e58b2deb7f82b84065f37f3bffeb12413f947f9388e4cac22c4621ce/sortedcontainers-2.4.0-py2.py3-none-any.whl`\n  ╰").
- `tests/unit/test_retention_ordinary_trees.py::test_an_ordinary_tree_retires_without_an_unarchived_path[hardlink-False]` — skipped: ('/Users/maxghenis/.subfleet/worktrees/20261028-233931-pr272-review-r1/tests/unit/test_retention_ordinary_trees.py', 54, "Skipped: ('sortedcontainers==2.4.0',) not in uv's cache: × Failed to download `sortedcontainers @\n  │ https://files.pythonhosted.org/packages/32/46/9cb0e58b2deb7f82b84065f37f3bffeb12413f947f9388e4cac22c4621ce/sortedcontainers-2.4.0-py2.py3-none-any.whl`\n  ╰").
- `tests/unit/test_retention_ordinary_trees.py::test_an_ordinary_tree_retires_without_an_unarchived_path[hardlink-True]` — skipped: ('/Users/maxghenis/.subfleet/worktrees/20261028-233931-pr272-review-r1/tests/unit/test_retention_ordinary_trees.py', 54, "Skipped: ('sortedcontainers==2.4.0',) not in uv's cache: × Failed to download `sortedcontainers @\n  │ https://files.pythonhosted.org/packages/32/46/9cb0e58b2deb7f82b84065f37f3bffeb12413f947f9388e4cac22c4621ce/sortedcontainers-2.4.0-py2.py3-none-any.whl`\n  ╰").

Every historical planned node for the requested broader files was invoked. The four ordinary-tree fixture skips and the known xfail remain explicit coverage exceptions; no additional required local node is unattempted.


**CI snapshot**

Snapshot 2026-10-29T04:42:35Z.

| Bucket | Count |
|---|---:|
| pass | 0 |
| fail | 0 |
| pending | 10 |
| skipping | 0 |
| cancel | 0 |

One `gh pr checks 272` read, exit 8; exact output follows.

```text
Python 3.12 on macOS, shard 1/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248147213	
Python 3.12 on macOS, shard 2/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248147115	
Python 3.12 on macOS, shard 3/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248146772	
Python 3.12 on macOS, shard 4/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248147137	
Python 3.12 on macOS, shard 5/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248147389	
Python 3.14 on macOS, shard 1/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248147066	
Python 3.14 on macOS, shard 2/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248147033	
Python 3.14 on macOS, shard 3/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248147050	
Python 3.14 on macOS, shard 4/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248147116	
Python 3.14 on macOS, shard 5/5	pending	0	https://github.com/MaxGhenis/subfleet-v2/actions/runs/40813170554/job/123248146968
```

**Artifacts and closeout**

The data directory retains exact selectors, per-phase outcomes, starts/loads, sampled mutation definitions, failure traces, the single PR-body/checks reads, merge proof and one-shot measurement. Product source hashes match the reviewed head. HEAD remains `3893c7120b38004fcd43539bb557a2cd25c319e3`; named-file staging was denied as described above.

All test and driver tool sessions completed before closeout. The recorded test-PID presence probe returned []; a nonempty list would require checking possible PID reuse without signalling an unrelated process. Scratch measurements and guarded cleanup are recorded in the final closeout note.

Files left for delivery: `tests/fake/test_retention_dependency_review_r1.py`, `docs/reports/2026-10-29-retention-pins-review-r1.md` and `docs/reports/2026-10-29-retention-pins-review-r1-data/`.


Final filesystem verification at 2026-10-29T04:48:18.850994+00:00: scratch allocated **73.80 MiB**, below 1.5 GB; no workspace bytecode, Hypothesis cache, pytest cache or build directory was present. All recorded pytest PIDs were absent and all driver/tool test sessions had completed. Guarded scratch removal follows artifact verification.

Guarded scratch cleanup completed and absence verified at 2026-10-29T04:51:34.665033+00:00. One fixture directory needed owner access restored inside scratch before the guarded removal succeeded. No test/driver session remains running.

DONE
