# Retention dependency pins: fix round r1, 2026-10-29

Base: `3893c7120b38004fcd43539bb557a2cd25c319e3`. First recorded clock 05:18:07 UTC; first correctness hold admitted 05:20:19 UTC. New holds stop before 09:18 UTC and the wall ends before 11:18 UTC. All work, including fixture stores and local Git remotes, uses the assigned workspace or `sf-fix272r1` scratch. No live store, real provider, production CLI, sub-agent, repository fetch/merge/push or external message was used. Network was used only for the prescribed locked dev environment sync. Fixture Git transport uses only local bare remotes.

## Decisions built

1. Imported the review report, its evidence directory and the 18-case test file byte for byte: all 38 SHA-256 entries verified before the first commit. The review file remains unchanged. At base source it gave **13 passed / 5 failed**; after findings 1–3 it gave **18 passed**, and the final implementation repeats all 18.
2. C-quoted alternates decode on raw bytes before `fsdecode`, relative resolution against the objects directory and canonical spelling. Git's three-digit octal range and C escapes apply. Spaces are path bytes, not whitespace to strip. Traversal remains five levels, with distinct-path deduplication.
3. `_EXPORTING` joins `jobs` on its primary job key and yields `jobs.out_path` beside lease identity and `depends.out_path`. The lease range and indexed event lookup remain; no filesystem discovery runs in a transaction. Both terminal legacy identity cases now retain their tree.
4. Alternates files stream to EOF without a whole-file cap. The input buffer holds at most a 1 MiB line plus its overflow-detection byte; path results and the frontier grow with distinct dependencies. The bound is 256 times a 4096-byte PATH_MAX and well above even four-byte C escaping of such a path. Read errors, FIFOs, oversized lines and malformed quotes record incomplete evidence. The disappearance test injected a FileNotFoundError; it did not exercise an ordinary unlink of an open file. Recorded incomplete evidence and live legacy evidence survive database rechecks, but review r2 at `dd0e4ea8d` found real file-change detection and terminal legacy exporter gaps. Admission still proceeds. Fix round r2 now closes those six inputs under C-8.4's fail-closed rule: descriptor metadata and pathname identity are checked after reading, terminal `out:` holders enter legacy discovery, and exporter rechecks carry legacy incomplete evidence. See [fix round r2](2026-10-29-retention-pins-fix-r2.md) for the corrected observation bounds, the r3 follow-up and dated r2 results.
5. The separate **job-directory boundary already present in release/217** is closed at the dependency census, the job-directory fence transaction before either rename, and the final commit recheck. The converted probe plus role/stage/export tests are **11 failed at base / 11 passed after the first boundary fix**. A further symlinked `jobs/` parent case failed on the initial boundary fix: the fence was canonical but the final check used the journal alias. The follow-up captures `job_folder` off the store lock and uses it at commit, preserving `job_dir` for the rename's symlink refusal; old archived journals get their fallback spelling off the lock. All **12 boundary tests pass**. Eight crash/removal recovery cases are rerun after this follow-up. Existing turn protection retains its `turn-folder` precedence.
6. The desktop timing sentence uses review r1's corrected text. C-6.5, C-6.11, C-8.3, C-8.4, relevant change-log entries, desktop I16 and dependency description, the main report's “Review r1 and fix round r1” section and c4a resolution describe this implementation. Historical evidence stays dated. Review r2 confirmed the original fixes but found two further incomplete-discovery gaps at `dd0e4ea8d`; fix round r2 resolves them as documented in the linked report. The original round r1 checks did not establish those later cases.
7. The three review mutants, one boundary-pin mutant and all ten continuation 4b mutants targeting `dependencies.py` are killed by their named tests. The four required properties run at normal maxima and phases, with seed 217. A new property writes Git's own quoted path output into alternates and checks both Git object access and canonical discovery.

No hub decision required a departure. The boundary needed a separate spelling follow-up, after findings 1–3 were committed, as decision 5 permits.

## Git comparison

Git **2.55.0** was executed against every listed spelling. The unit comparisons give the borrower a blob available only in the alternate, so `cat-file` success proves object access. The generated property obtains quoting from Git's `ls-files --others --directory` with `core.quotePath=true`; its borrower then reads that blob and discovery must yield the canonical object directory. No production Git subprocess was added.

| Executed line | Git result | Reader result |
|---|---|---|
| Quoted absolute | reads intended object directory | same canonical directory |
| Quoted relative | resolves against objects directory | same canonical directory |
| ASCII octal `\157` | byte `o` | same byte before fsdecode |
| Non-ASCII octal `\303\251` | UTF-8 bytes for é | same bytes before fsdecode |
| `\"` | literal quote | same |
| `\\` | literal backslash | same |
| `\t` | tab in path | same |
| `\n` | newline in path | same |
| Comment at entry start | ignored | ignored |
| Blank line | ignored | ignored |
| Leading space outside quotes | part of path; absent path fails lookup | byte preserved |
| Trailing space outside quotes | part of path; absent path fails lookup | byte preserved |
| Leading/trailing spaces inside quotes | intended directory is read | spaces preserved |
| Quoted entry followed by a separator and another entry | first directory is read; next entry also parsed | same parse, including missing-path evidence |
| Unknown escape, short octal, octal above `377` | rejects quoted spelling | incomplete discovery, conservative pin |

The initial additional invalid-UTF-8-byte fixture could not create byte `0xff` in this sandbox. It was replaced before the quote fix commit by a pure raw-byte decoder assertion; executed non-ASCII Git comparisons still cover UTF-8 octal bytes. This changes no imported review test or hub decision. [Probe output](2026-10-29-retention-pins-fix-r1-data/git-compare.json) and the committed comparison tests retain the inputs.

## Red and green development evidence

| Hold | Result |
|---|---|
| `red-review` | 13 passed / 5 expected failures, 66.46 s; 05:20:19.773 UTC, load 34.83 / 28.08 / 26.19 |
| `quote-unit` | 15 passed / 1 fixture failure creating byte 0xff; corrected as above |
| `quote-unit-fixed` | 16 passed |
| `export-green` | 4 passed / 14 deselected |
| `incomplete-green` | 17 passed / 1 new-test mapping-key error; corrected before commit |
| `incomplete-admission` | corrected admission assertion passed |
| `review-green` | unchanged review: 18 passed |
| `boundary-red` | premature baseline extraction attempt; pytest unavailable, no tests collected; superseded by next hold |
| `boundary-red-base` | exact archived base source: 11 expected failures; 05:31:41.440 UTC, load 29.72 / 30.26 / 29.03 |
| `boundary-green` | 11 passed |
| `boundary-canonical-red` | 1 expected failure against initial boundary fix |
| `boundary-canonical-green` | 12 passed after saving canonical fence spelling |

Full logs and the run ledger retain failed development attempts separately from successful implementation coverage. Production source has no expected-failure annotations. The imported review file was never edited.

## File coverage

Counts below are distinct selected node ids with completed setup, call and teardown, using their latest ordinary implementation run; mutant and expected-red runs are excluded. “First start/load” is the first admitted observed run for that file. The per-hold table below retains each observed pytest start after lock admission and all three load averages. The two-case turn contract is repeated with the final guards so it also has an observation record.

| File | P / F / S / U | First start UTC | First load (1/5/15 min) |
|---|---|---|---|
| `tests/fake/test_detached_retention_fence.py` | 89 / 0 / 0 / 0 | 06:03:18.265 | 31.55 / 31.87 / 29.03 |
| `tests/fake/test_retention_dependencies.py` | 30 / 0 / 0 / 0 | 05:36:08.023 | 13.11 / 22.02 / 25.79 |
| `tests/fake/test_retention_dependency_incomplete.py` | 19 / 0 / 0 / 0 | 06:02:25.987 | 30.32 / 31.92 / 28.89 |
| `tests/fake/test_retention_dependency_review_r1.py` | 18 / 0 / 0 / 0 | 06:06:46.729 | 18.45 / 25.55 / 26.99 |
| `tests/fake/test_retention_dependency_spelling.py` | 16 / 0 / 0 / 0 | 05:37:11.700 | 20.75 / 22.83 / 25.83 |
| `tests/fake/test_state_contract.py` | 37 / 0 / 0 / 0 | 06:08:16.385 | 20.18 / 24.28 / 26.34 |
| `tests/fake/test_writer_retention_fence.py` | 23 / 0 / 0 / 0 | 06:02:54.393 | 29.82 / 31.65 / 28.89 |
| `tests/unit/test_invariants_index.py` | 25 / 0 / 0 / 0 | 06:09:06.545 | 18.65 / 23.22 / 25.83 |
| `tests/unit/test_retention_alternates.py` | 23 / 0 / 0 / 0 | 05:35:37.965 | 14.16 / 23.18 / 26.32 |
| `tests/unit/test_retention_archive.py` | 109 / 0 / 0 / 0 | 05:46:59.741 | 26.86 / 21.59 / 23.16 |
| `tests/unit/test_retention_cwd_rechecks.py` | 3 / 0 / 0 / 0 | 06:00:48.704 | 30.52 / 32.16 / 28.58 |
| `tests/unit/test_retention_dependency_properties.py` | 4 / 0 / 0 / 0 | 05:39:03.032 | 17.79 / 21.81 / 25.12 |
| `tests/unit/test_retention_dependency_units.py` | 19 / 0 / 0 / 0 | 05:38:01.200 | 23.36 / 23.44 / 25.89 |
| `tests/unit/test_retention_folded_pin.py` | 38 / 0 / 0 / 0 | 05:38:29.479 | 19.31 / 22.50 / 25.47 |
| `tests/unit/test_retention_job_dependencies.py` | 12 / 0 / 0 / 0 | 06:01:02.285 | 31.22 / 32.25 / 28.66 |
| `tests/unit/test_retention_shared_folders.py` | 62 / 0 / 0 / 0 | 05:43:20.183 | 17.79 / 19.45 / 23.18 |
| `tests/unit/test_retention_turn_contract.py` | 2 / 0 / 0 / 0 | 06:09:10.568 | 18.76 / 23.17 / 25.80 |
| `tests/unit/test_retention_turn_workdirs.py` | 1 / 0 / 0 / 0 | 06:00:26.643 | 29.81 / 32.03 / 28.43 |
| `tests/unit/test_retention_turns.py` | 7 / 0 / 0 / 0 | 06:02:45.516 | 30.52 / 31.85 / 28.93 |
| `tests/unit/test_retention_worktree_cwd.py` | 7 / 0 / 0 / 0 | 06:03:03.306 | 29.44 / 31.54 / 28.87 |
| `tests/unit/test_test_module_names.py` | 1 / 0 / 0 / 0 | 06:08:57.440 | 19.59 / 23.55 / 25.98 |

## Properties at normal settings

Counts observe completed engine runs without changing generation, shrinking, phases or maxima. Other engine calls include discarded/overrun evaluations and are not passing examples. Nested properties use the observer because pytest's terminal Hypothesis summary does not expose their inner statistics. Explicit counts are successful explicit-example executions. Settings and strategies are unchanged; the new quote property uses 100 examples plus three explicit inputs.

| Hold | Generated passing | Other engine calls | Explicit passing |
|---|---:|---:|---:|
| `quoted-property` | 100 | 0 | 3 |
| `units-property-1` | 12 | 0 | 0 |
| `units-property-3` | 60 | 4 | 0 |
| `units-property-2` | 150 | 23 | 0 |
| `units-property-4` | 200 | 20 | 0 |
| `folded-property-1` | 500 | 224 | 3 |
| `folded-property-2` | 150 | 24 | 0 |
| `folded-property-3` | 100 | 46 | 1 |
| `dependency-property-1` | 100 | 0 | 0 |
| `dependency-property-3` | 100 | 4 | 0 |
| `dependency-property-2` | 100 | 0 | 0 |
| `dependency-property-4` | 24 | 1 | 1 |
| `shared-property-1` | 150 | 39 | 5 |
| `shared-property-3` | 40 | 5 | 9 |
| `shared-property-2` | 400 | 90 | 12 |
| `shared-property-4` | 150 | 42 | 0 |
| `writer-fence` | 60 | 29 | 5 |
| `detached-fence` | 300 | 39 | 4 |
| `detached-fence` | 80 | 40 | 6 |
| `detached-fence` | 12 | 0 | 5 |

Required four: `folded-property-3` (attribution), `dependency-property-2` (independent reference), `dependency-property-3` (monotonicity), `shared-property-2` (fence differential). The last retains its 12 explicit examples. New Git quote property: `quoted-property`, 100 generated and 3 explicit.

## Named mutants

Every replacement matches exactly once, compiles, and is observed loading its target module. Mutants run in memory without editing production source on disk. Each has one pytest in one lock hold, seed 217, `-x`, and explicit/generate phases without shrinking; configured example maxima and strategies stay unchanged. U is selected but not executed after the first failure, never an implementation failure. All fourteen are killed, with no survivor or load error.

| Mutant | Observed killing node | P / F / S / U | Start UTC | Load (1 min) |
|---|---|---|---|---:|
| `git-storage-no-commondir` | `tests/unit/test_retention_dependency_units.py::test_d1_commondir_outside_the_worktrees_layout_is_followed` | 0/1/0/0 | 05:40:03.211 | 18.97 |
| `git-storage-no-worktrees-fallback` | `tests/unit/test_retention_dependency_units.py::test_d1_storage_a_gitfile_names_counts_when_it_is_gone` | 0/1/0/0 | 05:40:08.354 | 18.89 |
| `git-storage-no-alternates` | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | 0/1/0/0 | 05:40:11.202 | 18.89 |
| `git-storage-alternates-one-level` | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | 0/1/0/0 | 05:40:13.947 | 18.74 |
| `git-storage-gitfile-relative-to-cwd` | `tests/unit/test_retention_dependency_units.py::test_d1_a_separate_git_dir_and_a_relative_gitfile` | 0/1/0/0 | 05:40:18.527 | 19.40 |
| `git-storage-nearest-ignores-gitfiles` | `tests/unit/test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in` | 0/1/0/0 | 05:40:28.155 | 19.82 |
| `record-raw-spelling` | `tests/unit/test_retention_dependency_units.py::test_record_spells_each_place_once` | 0/1/0/0 | 05:40:35.479 | 20.08 |
| `fences-not-retention-only` | `tests/unit/test_retention_dependency_units.py::test_d5_fences_reads_what_retiring_reads_place_by_place` | 0/1/0/0 | 05:53:15.943 | 35.02 |
| `record-no-output` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[output]` | 0/1/0/0 | 05:53:43.933 | 36.19 |
| `record-no-git` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` | 0/1/0/0 | 05:54:03.560 | 34.79 |
| `review-quoted-decode-removed` | `tests/fake/test_retention_dependency_review_r1.py::test_c8_4_git_c_quoted_alternate_keeps_the_tree_it_reads[absolute]` | 0/1/0/1 | 05:54:48.259 | 40.10 |
| `review-export-row-removed` | `tests/fake/test_retention_dependency_review_r1.py::test_c8_3_pending_export_keeps_its_actual_path_beside_its_lease_identity[legacy-dotless-i]` | 2/1/0/1 | 05:56:27.019 | 26.90 |
| `review-alternates-cap-restored` | `tests/fake/test_retention_dependency_review_r1.py::test_c8_4_large_valid_alternates_file_keeps_the_tree_git_reads` | 0/1/0/0 | 05:58:15.821 | 37.76 |
| `review-job-directory-pin-removed` | `tests/unit/test_retention_job_dependencies.py::test_c8_4_job_directory_dependencies_are_rechecked_atomically[census-out_path]` | 0/1/0/0 | 05:59:21.247 | 43.98 |

[Exact definitions and target hashes](2026-10-29-retention-pins-fix-r1-data/mutant-definitions.json), [observed results](2026-10-29-retention-pins-fix-r1-data/mutants.json). Ten are the requested continuation 4b replays; four isolate this round's decisions.

## Observed ordinary implementation holds

One pytest per hold, serial inside each hold and no xdist. At most four pytest workers were active or queued; the shared script admits at most two concurrently. The whole-pytest alarm is 570 seconds. No hold required an unlocked fallback or a timeout interruption. The longest completed pytest was `archive-2`, 513.46 seconds; its full lock wall was longer but still below ten minutes. Shared-script `sysctl` probes were denied by the sandbox; loads here are `os.getloadavg()` measured inside the admitted process. Two observer-preimport assertion-rewrite warnings in later holds do not change test assertions or property settings.

The ordinary compatibility runs began at the initial boundary implementation; after the canonical journal follow-up, the complete boundary file, unchanged review file, final new-test file, eight crash/removal recovery cases and remaining compatibility files validate that follow-up. Earlier passing properties are not re-labelled as new executions.

| Hold | Start UTC | Load (1/5/15 min) | P/F/S/U |
|---|---|---|---|
| `quoted-property` | 05:35:37.965 | 14.16 / 23.18 / 26.32 | 17/0/0/0 |
| `fake-dependencies` | 05:36:08.023 | 13.11 / 22.02 / 25.79 | 30/0/0/0 |
| `spelling` | 05:37:11.700 | 20.75 / 22.83 / 25.83 | 16/0/0/0 |
| `units-property-1` | 05:38:01.200 | 23.36 / 23.44 / 25.89 | 1/0/0/0 |
| `units-static` | 05:38:01.200 | 23.36 / 23.44 / 25.89 | 15/0/0/0 |
| `units-property-3` | 05:38:19.162 | 21.00 / 22.93 / 25.66 | 1/0/0/0 |
| `units-property-2` | 05:38:19.162 | 21.00 / 22.93 / 25.66 | 1/0/0/0 |
| `units-property-4` | 05:38:29.479 | 19.31 / 22.50 / 25.47 | 1/0/0/0 |
| `folded-static` | 05:38:29.479 | 19.31 / 22.50 / 25.47 | 35/0/0/0 |
| `folded-property-1` | 05:38:40.949 | 18.17 / 22.16 / 25.31 | 1/0/0/0 |
| `folded-property-2` | 05:38:53.374 | 18.46 / 22.01 / 25.20 | 1/0/0/0 |
| `folded-property-3` | 05:38:55.209 | 18.46 / 22.01 / 25.20 | 1/0/0/0 |
| `dependency-property-1` | 05:39:03.032 | 17.79 / 21.81 / 25.12 | 1/0/0/0 |
| `dependency-property-3` | 05:40:14.013 | 18.74 / 21.19 / 24.60 | 1/0/0/0 |
| `dependency-property-2` | 05:40:39.259 | 19.27 / 21.16 / 24.49 | 1/0/0/0 |
| `dependency-property-4` | 05:42:06.063 | 15.08 / 19.44 / 23.51 | 1/0/0/0 |
| `shared-property-1` | 05:43:20.183 | 17.79 / 19.45 / 23.18 | 1/0/0/0 |
| `shared-static` | 05:43:23.619 | 19.41 / 19.76 / 23.26 | 58/0/0/0 |
| `shared-property-3` | 05:44:13.806 | 16.75 / 19.28 / 22.90 | 1/0/0/0 |
| `shared-property-2` | 05:46:14.045 | 18.60 / 19.57 / 22.58 | 1/0/0/0 |
| `archive-1` | 05:46:59.741 | 26.86 / 21.59 / 23.16 | 20/0/0/0 |
| `archive-3` | 05:48:36.016 | 30.22 / 23.91 / 23.89 | 19/0/0/0 |
| `archive-5` | 05:50:11.639 | 27.73 / 25.10 / 24.37 | 11/0/0/0 |
| `shared-property-4` | 05:51:04.820 | 20.88 / 23.94 / 24.01 | 1/0/0/0 |
| `archive-2` | 05:52:28.615 | 21.40 / 22.79 / 23.52 | 41/0/0/0 |
| `turn-workdirs` | 06:00:26.643 | 29.81 / 32.03 / 28.43 | 1/0/0/0 |
| `cwd-rechecks` | 06:00:48.704 | 30.52 / 32.16 / 28.58 | 3/0/0/0 |
| `boundary-canonical-green` | 06:01:02.285 | 31.22 / 32.25 / 28.66 | 12/0/0/0 |
| `archive-4` | 06:01:07.586 | 30.40 / 32.06 / 28.61 | 18/0/0/0 |
| `quoted-static-final` | 06:02:03.182 | 30.86 / 32.15 / 28.87 | 22/0/0/0 |
| `incomplete-vanished` | 06:02:25.987 | 30.32 / 31.92 / 28.89 | 1/0/0/0 |
| `turns` | 06:02:45.516 | 30.52 / 31.85 / 28.93 | 7/0/0/0 |
| `writer-fence` | 06:02:54.393 | 29.82 / 31.65 / 28.89 | 23/0/0/0 |
| `worktree-cwd` | 06:03:03.306 | 29.44 / 31.54 / 28.87 | 7/0/0/0 |
| `detached-fence` | 06:03:18.265 | 31.55 / 31.87 / 29.03 | 89/0/0/0 |
| `boundary-final` | 06:04:07.224 | 25.78 / 30.67 / 28.78 | 12/0/0/0 |
| `archive-recovery-final` | 06:04:20.917 | 23.13 / 29.86 / 28.52 | 8/0/0/0 |
| `incomplete-final` | 06:06:07.745 | 19.35 / 26.67 / 27.43 | 19/0/0/0 |
| `review-final` | 06:06:46.729 | 18.45 / 25.55 / 26.99 | 18/0/0/0 |
| `guard-state` | 06:08:16.385 | 20.18 / 24.28 / 26.34 | 37/0/0/0 |
| `guard-module-names` | 06:08:57.440 | 19.59 / 23.55 / 25.98 | 1/0/0/0 |
| `guard-invariants` | 06:09:06.545 | 18.65 / 23.22 / 25.83 | 25/0/0/0 |
| `turn-contract-final` | 06:09:10.568 | 18.76 / 23.17 / 25.80 | 2/0/0/0 |

## PR-body lines

- Status: Review r1's findings 1–4 and the separate release/217 job-directory boundary are fixed; CI remains pending. Earlier continuation results remain historical evidence.
- What changes: Decode Git C-quoted alternates on bytes, stream complete alternates with a bounded line buffer and conservative incomplete-discovery pins, retain terminal exporters' actual job-row destinations, and protect jobs/<id> through census, canonical fence and final commit checks.
- Invariants: C-6.5/C-6.11 retain shared dependency comparison and hold attribution; C-8.3/C-8.4 and desktop I16 keep needed trees through live use and pending export. Discovery stays outside transactions; selection/fence and final rechecks use database evidence under the store lock. Incomplete discovery admits the job and prevents allocated-worktree retirement while it is live or exporting.
- Evidence this round: Imported review red 13/5 then green 18/0; boundary 0 passed / 11 failed at base then 11 passed / 0 failed after the initial fix, with a further canonical-spelling red/green and 12 final passing cases; Git comparisons; four normal properties at 100/100/100/400 plus 12 explicit fence inputs; new Git quote property at 100 plus 3 explicit; fourteen named mutants killed; requested compatibility files and guards pass. No new timing claim is made; continuation 4b's two comparisons stay dated.

## Delivery and what did not run

Shared `index.lock` creation was denied. Ordered commits use private scratch Git metadata on `fix/retention-dependency-pins-217`, with the assigned `.git` file unchanged; no history was rewritten. Every commit ends with the requested co-author footer. The final commit and bundle head are reported in the final response; the bundle is `retention-fix272r1.bundle` at the assigned workspace root, based on `3893c7120`, verified before private metadata cleanup.

All requested local files are covered: **545 distinct passing nodes, 0 failing, 0 skipped and 0 unfinished**; the three guards contribute 63 of those passes. The final scratch sample was 554.5 MiB, below the 1.5 GB budget. CI, the full suite, Python 3.12 and the person-only live frontend were not run. No live host/state survey, remote CI/PR lookup or new timing measurement was performed. The final response confirms that every owned process ended and scratch was removed with the lane rules' guarded cleanup; no bytecode, build output or Hypothesis storage is kept in the worktree. Exact per-run observations, logs and replay scripts accompany this report.
