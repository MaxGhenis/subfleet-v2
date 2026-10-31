from pathlib import Path
import json

DATA = Path(__file__).resolve().parent
ROOT = DATA.parent
summary = json.loads((DATA / 'summary.json').read_text())
context = json.loads((DATA / 'execution-context.json').read_text())
cleanup = json.loads((DATA / 'cleanup.json').read_text())
marked = json.loads((DATA / 'r9-marked-final.json').read_text())
marked_load = '/'.join(f'{value:.2f}' for value in marked['load'])
assert summary['unique_ordinary_nodes'] == 829
assert not summary['incomplete_runs'] and not summary['unresolved_ordinary_failures']
assert cleanup['owned_test_sessions_completed']
link = lambda label, path, line=None: f'[{label}]({ROOT / path}' + (f':{line}' if line else '') + ')'

pr = '''**Review r9 at `a3b9cc246` requests changes for R9-F1 (P2).** The original canonical-path R8-F1 regressions and the earlier R6/R7 regressions pass. Auxiliary physical-file locations and scanned targets still retain macOS Data-volume spellings, while allocated trees use canonical spellings. Four healthy real-Git inputs record complete evidence, lose their required owner tree during retention, and then fail the identical read. Canonicalize those auxiliary locations outside transactions while preserving physical symlink-entry names. Retention stays dormant pending repair, approving rereview and completed CI.

- **Review r9 evidence:** all 745 requested nodes and 84 independent controls pass; four new strict expected failures name R9-F1. All nine r8 mutants die; thirteen old mutants die at their same r7 assertions; seven survive and each replacement guard is demonstrated by a failing double-removal trial. Class and generated-only reversion tables match the round exactly. An in-memory three-site canonicalization trial passes the four new cases and five routine controls; independently dropping either repair category fails its matching case.
- **Reader boundaries:** keep D-R6-1 through D-R6-4, D-R7-1 and D-R8-1 as listed differences. D-R8-2 describes auxiliary unused-link records: broad ancestor targets do not pin their contained trees, and complete Git pins end with the job. R9-F1 follows the covered pointer-file and direct-entry routes and requires repair.
- **Next:** canonicalize successful physical file locations and resolved scan targets; preserve the four new regressions and existing passing tests; obtain approving rereview and the hub's all-pass CI/mergeability checks.
'''
(DATA / 'pr-body-lines.md').write_text(pr)

parts = [f'''VERDICT #272: REQUEST_CHANGES

Reviewed head: `a3b9cc246701147d09c24980f7602c79c1c61453`. Round range: `373a7f5bf..a3b9cc246`.

**R9-F1 is a P2 under the age-independent stop rule.** The original R8-F1 inputs pass, but covered physical-file and direct-entry locations reached through macOS's Data-volume firmlink still miss the canonical tree comparison. No deeper reader is requested.

## New finding

**R9-F1 — P2: canonicalize auxiliary locations before pin comparisons.**

Locations: {link('dependencies.py:247', 'subfleet/dependencies.py', 247)} (`_Aliases.text`), line 404 (`_alternates`) and line 267 (`_Aliases.scan`); line 281 forwards the raw locations. `files` and `entries` retain the component walk's spelling, while `links` correctly canonicalizes each physical entry's parent.

On this host, `/System/Volumes/Data/private/tmp/...` and `/private/tmp/...` reach the same inode, and `folders.canonical` returns the latter. Recorded auxiliary locations retain the former prefix. `live`, `users`, `dependents` and fences intentionally use string operations, so those records do not match the canonical allocated owner tree.

Four final executed inputs use an outside checkout and stable Git storage:

| Location in owner | Route Git uses | fsck / clean status / initial read | Incomplete evidence | Retention | Identical read afterward |
|---|---|---|---|---|---|
| Regular gitfile | `checkout/.git -> Data/owner/pointer`, naming stable gitdir | 0 / 0 / 0 | None | Prunes owner | 128 |
| Regular commondir | `admin/commondir -> Data/owner/pointer`, naming stable common | 0 / 0 / 0 | None | Prunes owner | 128 |
| Regular alternates | `objects/info/alternates -> Data/owner/pointer`, naming stable objects | 0 / 0 / 0 | None | Prunes owner | 128 |
| Pack directory | `stable/.git/objects/pack -> Data/owner/packs` | 0 / 0 / 0 | None | Prunes owner | 128 |

Each read serves the owner's commit and `owner-only.txt`. Discovery finishes before a running borrower is recorded; retention then retires the required tree. The final tests fail only at the owner-retention assertion, with passing setup and teardown. These are stable repositories, and both pointer files and direct `objects/pack` links are explicitly within the round's claimed routes.

Evidence: {link('four executed inputs', 'review-r9-data/R9-F1-inputs.json')}, {link('final red hold', 'review-r9-data/red-r9-firmlink-final.json')}, {link('regression file', 'tests/fake/test_retention_dependency_review_r9.py', 24)}. Run with `--runxfail` to expose all four failures. Normally the four are strict expected failures naming R9-F1, so the added file stays green. It skips where the macOS Data-volume alias is unavailable.

**Fix:** canonicalize successful physical file locations in `_Aliases.text` and `_alternates`, and canonicalize resolved scan targets before adding them to `entries`, outside transactions. Keep `links` as canonical parent plus name; following the final link entry would lose the location R8-F1 needs.

The {link('three-site in-memory trial', 'review-r9-data/r9-canonical-trial.diff')} passes **nine tests**: all four new cases and all five exact routine-record controls. Independently omitting file canonicalization fails `[gitfile]`; independently omitting scanned-target canonicalization fails `[objects-pack]`, both at owner retention after valid Git setup. Product source was not changed.

## R8-F1 and earlier findings

- **Original R8-F1: PASS, bounded repair.** Both canonical-path inputs pass fsck, clean status and owner-commit reads, survive maintenance, and remain readable. Independent forced-removal controls for both change the identical read from 0 to 128. All seven new traversal forms, the sibling storage-target control and all 55 generated location inputs pass their owner-removal checks. Firmlink variants expose R9-F1 above.
- **R6-F1 / R6-F2: PASS.** All 16 r6 cases pass, including named own stores with absent/LF/CRLF commondir and both padded pointers. Pointer-failure controls pass seven cases.
- **R7-F1: PASS.** All three r7 cases pass, including the two original valid-repository/vanished-target inputs. Parent-hint tests pass two. The independent historical matrix passes 37 controls, including healthy unused parents, direct parent-reader checks, pointer-read failures and terminal gone targets.
- R1–R5 files, aliases and recovery census pass; all requested turn, dependency, recovery/archive and contract guards pass. Their exact counts are below.

## Decisions 1–12

''', (DATA / 'decisions.md').read_text(), '\n## Checks 1–7\n\n', (DATA / 'checks.md').read_text(), '''
## Generated input classes

The location property runs 55 complete owner-dependent inputs: all eleven positions appear, unrelated is omitted, and the identical real-Git read fails after owner removal. Independent inputs: 0/55. The following table is byte-identical to the round's table.

''', (DATA / 'class-table.md').read_text(), '''
The original property still has 100 generated + 15 explicit inputs: 43 successful owner reads and 72 rejections, with 32 complete and 11 deliberately over-bound successful reads. The focused property has 40 successful, complete reads; common absent/LF/CRLF = 15/10/15, own plain/link/alternate = 12/12/16 and parent absent/ordinary/repository/unrelated-commondir = 10/6/16/8. All nine common/own combinations appear. No property has a firmlink selector.

## Generated-only reversions

All eight scratch-copy trials preserve property bodies, settings and deterministic seed derivation. They use `max_examples=55/40`, `derandomize=True`, `database=None`; 15 explicit original examples are removed per copy. The following table is byte-identical to the round's, including every first failing generated input before shrinking. The focused strategy has only slot/common/own/parent selectors and records object storage directly, so it cannot isolate decisions 1–3's metadata locations.

''', (DATA / 'reversion-table.md').read_text(), '\n## Seven surviving older mutants\n\nAll seven pass their named tests. Each has the second required answer: a named newer guard covers the input, and removing that guard along with the old one fails the same named test. Every double-removal setup/teardown passes. The named-input redundancy claim is therefore supported; it does not claim the old guard is universally unnecessary.\n\n', (DATA / 'survivors.md').read_text(), '\n', (DATA / 'listed-differences.md').read_text()]

parts += [f'''
## Holds and coverage

**All 745 requested nodes and 84 independent controls pass: 829 distinct passing nodes.** The new review file has **four strict expected failures**, separately counted. No requested local node or named mutant remains unrun, apart from the mandated live-case deselection.

There were **{summary['completed_runs']} completed locked pytest invocations**, peak pytest concurrency **{summary['peak_pytest_concurrency']}**, longest **{summary['longest_seconds']:.2f} seconds**, and peak recorded pytest RSS **{summary['peak_rss_mib']:.2f} MiB**. None used the 30-minute direct fallback. The first hold began `{context['first_hold']}` and the last test ended `{context['last_test_end']}`, well before the four-hour start cutoff. Python 3.14.7, Git 2.55.0, locked dev environment; environment-dependent property settings match their normal defaults. The wrapper's `sysctl` is sandbox-denied; admitted runs record actual `os.getloadavg()` independently.

Qualifications:

- Archive's first hold hit its 540-second interrupt and ended after 563.07 seconds including teardown, below ten minutes. It had 69 passes, five specified path-classification failures and 34 nodes uncalled. All six named path cases passed unchanged with worktree TMPDIR; the other 33 nodes passed in groups of at most twelve. The final union is 108 passes. This was an environment/hold split, not another finding.
- The six worktree retries were `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`, `test_blob_only_in_a_local_branch_is_archived_not_omitted`, `test_local_path_remote_is_not_a_remote`, `test_borrowed_or_shallow_object_store_is_scratch[alternates]`, `test_borrowed_or_shallow_object_store_is_scratch[shallow]`, and `test_survey_is_read_only_and_predicts_the_pass`.
- Six early R9 probe receipts are explicitly superseded/qualified and excluded from final aggregate coverage. Initial ref-directory and packed-ref symlink forms allowed owner reads but fsck rejected their types. The final objects/pack form passes fsck, clean status and the initial read; all four final cases fail at owner retention. Those superseded invocations are included in the 99 completed-process count.
- Shared-folder properties, dependency properties, salvage properties and sweep examples/property were split into separate holds. Normal example counts were preserved.

Per-file counts, earliest contributing starts and load 1/5/15 follow, all October 31 UTC. Repeated nodes count once. The {link('complete hold table', 'review-r9-data/hold-table.md')} records every hold's start/load/duration/status; raw JSON records all phases and per-file node IDs.

''', (DATA / 'file-table.md').read_text(), f'''
New file, separately: `tests/fake/test_retention_dependency_review_r9.py` — 0 passed, 4 strict xfailed in its final ordinary marked hold at `{marked['start']}` (load {marked_load}); the {link('red hold', 'review-r9-data/red-r9-firmlink-final.json')} has four failures at the intended assertion. The file's counts are not folded into the 829 passing nodes.

Explicitly not run: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`, full suite, Python 3.12, the person-only `tests/frontend/test_core_live.py` probe, and remote CI. The hub verifies CI. No live store, real provider or real Subfleet CLI was used.

## Mutant table

All 29 round-named results reproduce: nine new kills, thirteen older kills at identical r7 assertions, seven old survivors. The seven double-removal trials add seven kills; R9-F1's two partial-repair trials add two. Total: 31 killed, seven surviving old named cases, and one positive trial. Fixtures and teardown pass in every final mutant/trial run.

''', (DATA / 'mutant-table.md').read_text(), '''
## Text and PR-body lines

C-8.3/C-8.4 and the full acceptance-contract file are byte-identical. C-8.3's export/census behavior passes. C-8.4's canonical-spelling requirement is violated by R9-F1; keep the requirement and repair discovery rather than weaken it.

The current repair-closure lead in the PR body, main report and fix report needs this qualification pending repair:

> Fix round r8 passes the original canonical-path R8-F1 regressions. Review r9 at a3b9cc246 requests changes for R9-F1: physical pointer files and scanned targets reached through macOS Data-volume aliases are recorded under noncanonical spellings, so complete discovery can still let retention retire a running borrower's required tree. The five prescribed routine controls, class counts, reversion results and named-mutant accounting remain verified.

Add the same current-status qualification to the desktop discovery description until repaired. In docstrings, qualify the general “spelled once” description for auxiliary records at this head: storage directories are canonicalized, but physical files and scanned targets currently retain the walk's spelling (R9-F1). The in-memory fix restores the intended canonical behavior. Separately replace the old outside-checkout sentence with the D-R8-2 wording above.

Replace the PR's current lead/evidence/Next blocks with the following; preserve earlier rounds as dated history:

''', pr, f'''
## Retained files and cleanup

The requested macOS scratch folder was denied, so this review used `/private/tmp/sf-r272r9`. Final measured scratch was {context['scratch_kib']/1024:.2f} MiB, under 1.5 GB; worktree fixtures were {context['worktree_fixture_kib']/1024:.2f} MiB. Guarded cleanup completed at `{cleanup['completed']}`. Scratch, worktree fixtures, bytecode, `.hypothesis` and build outputs are absent. Every owned test/tool session completed; no run was detached.

Retained review artifacts are `review-r9.md`, `review-r9-data/` and `tests/fake/test_retention_dependency_review_r9.py`. No product source changed. No commit, push, merge or publication was made; the review-only rule prohibits commits. The new regression remains in the worktree for the hub.

DONE
''']
(ROOT / 'review-r9.md').write_text(''.join(parts))
print('Wrote review-r9.md and exact PR-body replacements.')
