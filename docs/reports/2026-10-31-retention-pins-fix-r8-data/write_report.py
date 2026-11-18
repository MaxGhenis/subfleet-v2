"""Assemble the durable r8 report from counted evidence, without running tests."""
import json
from pathlib import Path

DATA=Path(__file__).resolve().parent
ROOT=DATA.parents[2]
summary=json.loads((DATA/'summary.json').read_text())
assert not summary['unresolved_ordinary_failures']
assert not summary['incomplete_runs']
context=json.loads((DATA/'execution-context.json').read_text())
files=json.loads((DATA/'files.json').read_text())
required_nodes=sum(f['passed'] for f in files if f['file'].startswith('tests/'))
extra_nodes=summary['unique_ordinary_nodes']-required_nodes
assert (required_nodes,extra_nodes)==(779,9)
def table(name):return (DATA/name).read_text()

body='''# Retention dependency pins — fix round r8

R8-F1's two executed inputs are repaired. **779 distinct tests under `tests/`
and nine report controls pass.** All nine new named mutants die; thirteen of
twenty older mutants die at their r7 assertions and seven now have redundant
guards. The new property detects generated-only reversions of decisions 1, 2,
3 and R7-F1. The tightened focused property detects R7-F1.

The literal converse invariant is **not universal**: the prescribed scan of
all direct storage symlinks can record a target unused by the commit read.
The executed counterexample and D-R8-1 are below. Per the stop rule, discovery's
reach is not changed further. Retention stays dormant pending review r9 and CI.

## Workspace, delivery and commits

Started approximately **2026-10-31 07:05 UTC**, at prerequisite `373a7f5bf588`.
The requested macOS scratch path was denied; scratch was
`/private/tmp/sf-fix272r8`. Only this workspace, that scratch root and the
specifically supplied review/rule inputs were used for the work. No remote Git
operation, GitHub call, live store, real provider or live Subfleet CLI was run.
The lane rule prohibiting sub-agents was honored.

The shared index rejected `index.lock`. Job-local `.git-local` metadata holds
the assigned branch `fix/retention-dependency-pins-217`; the original detached
checkout metadata remains at `373a7f5bf588`. Delivery is
`docs/reports/2026-10-31-retention-pins-fix-r8.bundle`, created from that branch
with **`373a7f5bf588` as prerequisite**, and verified before delivery. The final
reply identifies its head. No history was rewritten and nothing was pushed.

Implementation commits:

- `63abcee86`: repair and the review mark removal, with red/initial green data.
- `241d1b898`: targeted tests, the 55-input property and focused oracle.
- `c93c09250`: linked-TMPDIR red/green evidence. **Workflow deviation:** the
  one-line CI path correction landed in `241d1b898` while that commit was being
  staged; this following commit records its evidence. History was preserved.
- `334b82f22`: a red/green follow-up separates scanned storage targets from
  physical entry exclusions and makes the failed walk stop at 64 hops without
  restarting resolution. This stays within decisions 1, 3 and 4.
- `dcba31c79`: nine r8 mutant definitions and results, plus the twenty older names.

The final text/evidence commit follows these. Every commit carries the required
co-author trailer. `source-audit.json` proves the final executable AST is the
same as `334b82f22`; subsequent product edits are docstrings only.

## Decisions 1–12

| Decision | Built and evidence |
|---|---|
| 1 | Component-order `lstat`/`readlink` walk, no initial lexical normalization, physical link entries as canonical parent plus name, 64-hop bound, quiet ENOENT/ENOTDIR/ELOOP. `_target` retains link targets and identities as well as final path/identity; same-final-target chain changes are incomplete. |
| 2 | Successful regular pointer reads retain the resolved file location. Alternates register their pathname before opening and their physical file after opening; existing dangling/read-failure evidence remains. |
| 3 | One scan of each distinct actual git/common/object/alternate path, resolving direct symlink entries and retaining their locations and targets. Missing folders are routine, other scan failures incomplete. Neither inferred parent nor its objects is scanned. |
| 4 | Locations within recorded places or directly inside the checkout/ancestors are omitted. Scanned storage targets retain their own dependency meaning, including a sibling target. Five exact routine controls match prerequisite code. Untouched dependency-unit exact assertions pass. |
| 5 | All consumers audited; none requires each recorded place to be a directory or resolves it again. Details below. |
| 6 | Only the strict decorator on review r8's two parametrized cases is removed. Own tests cover two relative links, `..`, an intermediate component, linked `.git`/`commondir`/alternates files, legacy linked HEAD and linked `objects/pack`. |
| 7 | New module `tests/unit/test_retention_location_inputs.py`: 55 deterministic generated real-Git inputs, no explicit examples, complete evidence and owner-removal control. Eleven positions, chains 1–3, absolute/relative links, named/unnamed admins. Independent count is zero. |
| 8 | Focused property's successful reads require no incomplete evidence. Its owner/object assertions no longer accept a global pin. Original property's oracle is unchanged. |
| 9 | `check_input` resolves its base before constructing paths. Red at prerequisite and green under linked TMPDIR. Every new test compares physical paths; the focused helper already canonicalizes comparisons. |
| 10 | Nine r8 and twenty r7 named mutants executed individually under the lock, no wider sweep. Results below distinguish survivors and unchanged assertion kills. |
| 11 | Eight generated-only scratch-copy runs: each of decisions 1, 2, 3 and R7-F1 reverted separately against both properties. Original pointer module's 15 explicit decorators removed in scratch; selected property bodies/settings unchanged, hashes and first attempts recorded. |
| 12 | Module/`git_storage`/alias docstrings, desktop description and D-R8-1, main report section, this report/data and PR-body lines updated. C-8.3/C-8.4 and the entire acceptance-contract file stay byte-identical. |

Input adjustment: arbitrary absolute pathname symlinks at `HEAD` are rejected
by installed Git (`fatal: not a git repository: (null)`). The accepted legacy
form is `HEAD -> refs/heads/main`, whose ref then traverses the owner chain.
The initial failed fixture is retained in `green-locations.log`; this minimal
fixture adjustment preserves HEAD traversal coverage without changing reader
scope. An initial helper also called Hypothesis `note` outside a generated
case, and a copied repo raced automatic maintenance; these fixture issues were
corrected. New fixture Git commands disable automatic maintenance and hooks.

## Consumer reading (decision 5)

`needed` emits role/path strings. `fenced` returns them directly, doing legacy
discovery off-transaction. `fences` uses normalized folded strings and the
retention lease range. `live` adds those strings; `users` indexes their string
ancestors; `dependents` compares them against a tree without filesystem reads.
`legacy` spells the job workdir, not recorded Git paths.

Daemon `_needs` passes these to `_fence_hold` and reservation. The early check
near 7318 and reservation near 8540 use `dependencies.fences`; attribution near
8601 uses the same string key. Its field named `folder` can carry a metadata
file. Retention census near 270, selection near 885, archive job-folder fencing
near 644 and final rechecks near 932 consume strings through these functions.
`render._retiring` and conversation hold descriptions only format/fold them.
No reader assumes a Git place is a folder or spells it again. Output paths were
already files. `source-audit.json` records identical consumer AST hashes and
byte-identical daemon/retention/archive/render/conversation files.

## Exact routine comparison against 373a7f5bf

`converse-bound-confirmed` runs prerequisite source and current source on the
same physical fixtures and asserts `base == fix == expected`; the new unit
file independently uses exact `==`. Paths below are schematic; raw full values
are in that hold's JSON `base-comparison` observations.

| Layout | Exact `recorded` value |
|---|---|
| Plain | `git=[repository/.git, repository/.git/objects]` |
| Worktree add | `git=[repository/.git/worktrees/linked, repository/.git, repository/.git/objects]` |
| Shared clone | `git=[shared/.git, shared/.git/objects, repository/.git/objects]` |
| git-new-workdir inside its own repository tree | `git=[repository/linked/.git, repository/.git/objects, repository/.git]` |
| Folder with no `.git` | `{}` |

`tests/unit/test_retention_dependency_units.py` is byte-identical and all 19
cases pass, including its exact-list assertions.

## Generated classes

The new property's **55/55** reads succeed before removal with complete
evidence, omit `unrelated`, record a place in `owner`, and fail the identical
`git show HEAD:f.txt` read after removing `owner`. **Independent inputs: 0/55.**
These are counted input classes, not an unbounded invariant claim.

'''+table('class-table.md')+'''
The original property still runs **100 generated + 15 explicit** inputs:
43 read the owner and 72 are rejected; 32 successful reads are complete and
11 deliberately oversized successful reads are incomplete. Its oracle is
unchanged. The focused property runs **40 successful reads, all complete**:
common absent/LF/CRLF = 15/10/15; own plain/link/alternate = 12/12/16;
parent absent/ordinary/repository/unrelated-commondir = 10/6/16/8. All nine
common/own combinations remain represented. Raw counts: `classes.json`.

## Generated-only reversions

First failures below are the **first generated failing attempts**, before
shrinking, from the recorder around each unchanged helper. The manifest
records exact edits, source hashes, removed decorators and unchanged selected
function-body hashes. Each scratch copy was removed after its two holds.

'''+table('reversion-table.md')+'''
Focused boundaries: `NAMED_OWN_INPUTS` at
`tests/unit/test_retention_pointer_inputs.py:149` has common/own/parent/slot
selectors, with no metadata-only owner, intermediate-path or storage-entry
selector. Its construction at line 157 records owner object storage directly,
so dropping location discovery alone leaves that oracle satisfied. The new
strategy at `test_retention_location_inputs.py:36` reaches all three decisions.
R7-F1 is detected by both properties' complete-evidence assertions. No decision
was missed by the new property. All eight final trials use the corrected
executable product source.

**Process-rule deviation:** cancelling the first queued trial did not stop its
separately grouped lock child. That child admitted at 07:30:34 UTC after the
parent was cancelled; it returned 4 after 2.72 seconds without collecting any
tests because its scratch test path had gone. Its receipt/log is retained as
`aborted-pre-correction-reversion1`; it ended at 07:30:38 UTC. The corrected
trial was rerun. The retained harness now waits explicitly for the child's
interrupt cleanup before deleting its source copy and records process IDs.
This accounts for a seventieth locked invocation; it is not counted as a
successful trial. Every process has ended before delivery.

## Named mutants

All fixtures/teardowns passed. **Nine new mutants killed; thirteen older ones
killed at their r7 assertions; seven older ones survive their unchanged named
inputs.** Those survivors are recorded as survivors, not mutant kills.

'''+table('mutant-table.md')+'''
Why seven older single-site removals are now redundant on their named inputs:

- `r2-post-read-identity-check-dropped`: the registered alternates pathname is
  also revalidated, detecting replacement.
- `r3-final-path-check-dropped`: the alternates filename's walked chain detects
  the changed `info` link.
- `r4-initial-objects-revalidation-dropped` and
  `r7-own-objects-revalidation-dropped`: the direct-entry scan registers the
  objects link for revalidation as well.
- `r4-initial-objects-recording-dropped`,
  `r6-empty-common-initial-objects-not-recorded` and
  `r6-parent-common-fallback-restored`: the new scan also retains the own
  objects link's resolved target.

`mutants.json` adapts old source anchors to the refactored code while retaining
the old mutation's meaning and named assertion. The two r7 report seam probes
also pass unmutated in `baseline-r7-seams`.

## Executed boundaries and stop rule

**D-R8-1:** `stable/.git/refs/heads/main -> owner/branch`, with an outside
checkout naming the stable gitdir. Git reads `HEAD:f.txt` (rc 0). Discovery
records only stable gitdir/objects with no incomplete evidence. Removing owner
makes that same Git read return **128** (`invalid object name 'HEAD'`). A link
deeper than one level in storage is not scanned unless it also lies on another
tracked metadata path. The desktop explicitly lists `refs/heads/x`, loose
objects and links inside `objects/pack`. Code is deliberately unchanged.

**Additional executed converse-bound gap:**
`stable/.git/unused-entry -> unrelated/unused`. The required unconditional
one-level scan records `unrelated/unused`, without incomplete evidence. The
commit read returns **0 before and after** removing `unrelated`, with identical
content. Therefore the literal converse bound does not hold for arbitrary
unused top-level entries. This follows decision 3 exactly. The smallest scoped
response is to preserve that decision and list this extra-pin counterexample,
as the stop rule requires; no whitelist or deeper/read-specific Git probing
was added. The required property's relevant HEAD/refs fixtures are narrower
than this counterexample. Both observations are in
`converse-bound-confirmed.json`, with runnable report probes.

## Red, green and linked temporary paths

- Prerequisite review r8 with `--runxfail`: **2 fail** at owner retention, after
  successful Git setup; both owner trees were pruned and the later read was 128.
- Prerequisite original pointer property under `link-tmp -> real-tmp`: **1
  property fails**, including the two `padding=over` explicit cases. Evidence
  paths were physical; the assertion used the unresolved link spelling.
- Repair: review r8 **2 pass**; new final locations hold **25 pass** (22 unit,
  one property with 55 inputs, two r8 regressions); pointer module **2 pass**
  under linked TMPDIR (115 original + 40 focused inputs).
- The follow-up sibling storage-target control was red, then green. A 66-link
  fixture also verifies both initial walk and revalidation stay within 64 hops.
- Linked TMPDIR holds cover each requested file: pointer module, both new
  modules, review r8, the untouched dependency units and fake dependency tests.
  The extra spelling/recovery files also pass in the linked hold. **No other
  file fails only because TMPDIR is reached through a link.**

Archive's first ordinary run is **102 pass, 6 fail, 1 deselected** because of
the prescribed temporary-root classification trap. The following six pass
unchanged when TMPDIR is the new worktree `archive-fixtures/` directory
(`archive-worktree-path`, **07:39:50 UTC**, load **31.42/30.97/31.88**):

- `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`
- `test_blob_only_in_a_local_branch_is_archived_not_omitted`
- `test_local_path_remote_is_not_a_remote`
- `test_borrowed_or_shallow_object_store_is_scratch[alternates]`
- `test_borrowed_or_shallow_object_store_is_scratch[shallow]`
- `test_survey_is_read_only_and_predicts_the_pass`

The explicitly deselected live case is
`test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` (`real_lsof`).

## Counts, starts and loads

**779 distinct tests + nine report controls = 788 distinct passing nodes.**
Counts below union passing phases after the six archive retries. For repeated
nodes, use their latest positive phase; Start/load is the earliest hold
contributing to that file's final union. Every individual hold (including
splits, retries, red, mutant and reversion runs) has its own start/load in
`hold-table.md` and raw JSON. All dates are **October 31, UTC**.

'''+table('file-table.md')+'''
There are **70 completed locked pytest invocations**, including the one invalid
pre-collection cancellation described above (69 fully classified evidence
runs), peak concurrency **2**, longest
**464.14 seconds**, and peak recorded pytest RSS **203.52 MiB**. No timeout
interrupt fired and no run used the 30-minute direct fallback. The four
existing dependency properties ran in separate holds. The host wrapper's
`sysctl` is sandbox-denied; each admitted run separately records actual
`os.getloadavg()` values in its JSON. The first hold began at 07:07:37 UTC,
within an hour of start; the final test finished at 07:40:07 UTC, before the
four-hour hold cutoff. No process was started to load the host.

Python/Git and measured scratch disk are recorded in `execution-context.json`.
Scratch stayed below 1.5 GiB (largest recorded snapshot '''+str(round(context['scratch_allocated_kib']/1024,2))+''' MiB).
All final foreground/tool-background runs completed; the cancellation exception
is explicitly accounted for above. Scratch,
worktree archive fixtures and generated caches are removed with guarded cleanup;
`cleanup.json` records completion, including making one owned read-only archive
fixture removable before the final guarded deletion. Tests, reports, definitions and raw evidence
remain in the worktree and commits.

## What did not run

The live `lsof` case was explicitly deselected. The full suite, Python 3.12,
frontend live probe and remote CI were not run on this lane; the lane rules
reserve CI for those checks and the frontend probe is blocked by its
person-only boundary. Every requested file, every named mutant and all eight
reversions ran. Nothing is left unrun from the brief apart from its prescribed
live-case deselection. No live store or provider was started.

## PR-body lines

'''
pr='''**Fix round r8 repairs R8-F1's two real-Git inputs by retaining the physical pointer files and symlink entries Git traverses, plus one level of links in actual storage directories.** The inferred parent stays a hint. Routine records match `373a7f5bf` exactly, and pointer assertions now hold under linked TMPDIR. Retention stays dormant.

- **Evidence:** 779 distinct tests plus nine report controls pass; red r8 2 failures becomes 2 passes; the linked-TMPDIR pointer failure becomes green. The new property has 55/55 complete owner-dependent inputs, while all 40 focused inputs require complete evidence. Generated-only reversions of decisions 1–3 and R7-F1 are detected by the new property; focused also detects R7-F1. Nine new mutants die; thirteen of twenty older names die at the same assertions, and seven now have redundant guards.
- **Reader boundary:** D-R8-1 demonstrates a deeper ref link that remains undiscovered. The prescribed direct-entry scan also conservatively pins an unused symlink target; the executed converse-bound counterexample is reported, with no further scope expansion. C-8.3/C-8.4 remain unchanged. Review r8 completed six checks and requested R8-F1's repair.
- **Next:** review r9 (Sol), including the bounded reader and extra-pin counterexample; all-pass CI, mergeability and approval remain merge gates.
'''
(DATA/'pr-body.md').write_text(pr)
(ROOT/'docs/reports/2026-10-31-retention-pins-fix-r8.md').write_text(body+pr)
print('Report and PR-body lines assembled from counted evidence.')
