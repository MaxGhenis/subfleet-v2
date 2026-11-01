VERDICT #272: REQUEST_CHANGES

Reviewed `d2f1f36ab12bcde0577283c30b00500ecdd8e9c4`, round range `236c80883..d2f1f36ab`. The whole PR base is release/217 at `b942d24f3`. Started 2026-10-31 10:33:20 UTC. The environment sync and the first locked hold began within the first hour. The requested Darwin scratch directory was sandbox-denied; `/private/tmp/sf-r272r10` was used.

No new P1 or P2 was found. R10-F1 is a P3 because the round newly asserts a universal invariant that is false at head. Its underlying cyclic extra-target behavior also exists at 236c80883; the stop rule explicitly includes a false sentence introduced by this round. This verdict applies the supplied stop rule and the documented reader boundaries. Remote CI belongs to the hub and was not queried.

## R9-F1 and earlier findings

**R9-F1: repaired.** The original gitfile, commondir, alternates and objects/pack cases each pass real-Git fsck, clean status and an owner-only commit read before retention. Discovery is complete, retention keeps owner, and the identical read remains successful afterward. Eight additional cases repeat all four through other-case and other-normalization spellings, using a non-ASCII fixture ancestor; all pass. Two fixtures physically under this job's `/Users/...` worktree reach owner metadata through `/System/Volumes/Data/Users/...`; gitfile and objects/pack both pass end to end. The sandbox permitted these reads. Their raw alias spellings and before/after return codes are retained in [the added-input receipts](/Users/maxghenis/.subfleet/worktrees/20261031-063315-pr272-review-r10/review-r10-data/R9-F1-added-inputs.json) and [the original hold](/Users/maxghenis/.subfleet/worktrees/20261031-063315-pr272-review-r10/review-r10-data/r9.json).

**R8-F1: still repaired.** Both original physical-gitfile/admin-path cases pass. Separate forced-removal controls prove each required owner changes the same Git read from 0 to 128. The location tests exercise intermediate links, link-then-dotdot, HEAD/refs and pack entries.

**R6-F1/F2 and R7-F1: still repaired.** The r6 file passes all 16 cases, including absent/LF/CRLF commondir own stores and oversized pointers. Pointer-failure controls pass seven. All three r7 cases and two parent-hint controls pass, including the healthy named repository and vanished target that must retire unrelated owner. Earlier r1-r5, alias and recovery-census files pass too.

## Decisions 1–8

| Decision | Verdict | Executed evidence and code assessment |
|---|---|---|
| 1. Three sites | Built; R10-F1 qualification | Text files at dependencies.py:250, alternates files at :408 and scan targets at :270 take canonical. Physical link entries still spell only their parent and append their own name. All four original R9 inputs, all added aliases and R8 controls pass. check() is unchanged: it compares original resolved path, inode/absence and complete walked link chain, plus pointer text. Canonical on a stopped suffix-growing cycle still returns an unspelled target. |
| 2. start | PASS | Base, three-site trial and full repair run on the same plain/linked fixtures through Data-volume spelling, through both git_storage and record. Plain is unchanged. The linked trial adds checkout/.git, and canonical start restores the exact base record. All five routine layouts equal 236c80883; six plain/worktree alias controls pass. The property passes all three spellings to record without first replacing them with plain spelling and still finds the required owner. No required-location loss was found. |
| 3. Invariant | REQUEST_CHANGES | The helper asserts both exact equations from the brief and derives exemptions from actual _Aliases.links. An independent wrapper checks the very return produced by every git_storage call in all five round files: 440 calls pass, including incomplete/race records. Two added healthy cyclic-entry cases fail the exact helper at head. Existing 440 passing calls do not cover this input. |
| 4. Prior review file | PASS | Literal byte comparison: only the strict xfail decorator is removed; all bodies remain identical. |
| 5. Spelling property | PASS | 55 distinct generated inputs, 33 nonempty cells, zero skips. Real Git follows the spelling in the metadata/link payload before owner is removed. Discovery receives aliased checkóut too. Consumer membership is folders.within(place, canonical_owner), with no realpath/canonical of the recorded place in that assertion. The old location-input module is byte-identical. |
| 6. Mutants | PASS | Four r9 and nine r8 mutants fail their named call assertion; setup and teardown pass. All thirteen expressions equal fix r9's receipts, and all nine r8 expressions equal the earlier r8 receipts. No older survivor was rerun. |
| 7. Reversions | PASS | Four isolated source copies retain byte-identical property code, max_examples=55, derandomize=True, database=None and no explicit examples. Deterministic seed derivation is unchanged. All four first failures and assertions match the round byte for byte. |
| 8. Text | REQUEST_CHANGES | The universal one-spelling claim in the docstring, desktop discovery text, main/fix report leads and PR lead is false for R10-F1. D-R8-2 remains true about unused extra pins but does not state an exception to the spelling invariant. C-8.3/C-8.4 and the entire acceptance-contract file are byte-identical. The contract remains the requirement; repair returned stopped targets rather than weaken it. |

The initial strategy's five empty cells are correctly called a strategy failure in the report. The corrected strategy draws a finite 11-position × 5-variant product: unnamed/named Data-volume and other-case variants, plus unnamed normalization. All 55 combinations execute, yielding 2/2/1 in every position. Pointer-file classes apply the alternate spelling to the owner-held physical file before stable symlinks are written; other classes apply it to owner-held traversal entries. These aliases remain in payloads Git reads and paths discovery walks. Normalizing fixture base paths does not erase these later spellings.

## Decision 3: every write and the return

**Yes: a stopped suffix-growing cycle passes through scan → self.entries → locations → return as an unspelled string.** The syntactic writes all call canonical, but canonical's partial result for such a loop is not a fixed point. There is no separate missing raw-path write beyond the enumerated sites.

- `found` receives _storage results at :158/:162, including raw alternates from the append at :417. The final comprehension at :164 canonicalizes every element before return.
- `self.files` has exactly two successful writes: text at :250 and alternates at :408, both canonical.
- `self.links` has one update, resolve at :242, from _walk's canonical-parent-plus-entry-name locations. Following the final entry is deliberately excluded.
- `self.entries` has one write, scan at :270, canonical.
- `locations` at :279–286 canonicalizes start for its ancestor set, filters/unions the three sets and returns a sorted list. It introduces no new string. The return at :165 concatenates that list with canonical storage.

The helper's second equation exempts only places actually in the links set, exactly as the brief permits. The returned loop/tail/... target is not a physical link entry exemption; it fails even the canonical-parent equation. The independent audit checks the same discovery result once rather than relying on a second discovery. [Audit counts](/Users/maxghenis/.subfleet/worktrees/20261031-063315-pr272-review-r10/review-r10-data/invariant-audit.json) and every captured set/path remain available.

## Checks 1–6

| Check | Verdict | Executed inputs |
|---|---|---|
| 1. R9-F1 | PASS | Four original Data-volume cases plus all four layouts in each other spelling: 12 healthy complete end-to-end cases. All keep owner and read 0 before/after retention. |
| 2. Home firmlink | PASS | gitfile and objects/pack under this worktree's /Users path, reached via /System/Volumes/Data/Users; before/after 0, owner kept. No path denial for these inputs. |
| 3. Earlier findings | PASS | R8 file 2 plus two independent forced-removal reads; R7 3; parent hints 2; R6 16; pointer failures 7; all earlier review files. |
| 4. Terminal/transactions | PASS | Recorded/legacy ended jobs with gone targets, with/without incomplete evidence, retire; retargeted/unlinked recorded link fences stay stable. DB probes forbid both record and canonical while the transaction is open. A legacy running borrower forces census while a spy asserts every canonical call is outside the transaction. All pass. |
| 5. Linked TMPDIR | PASS | Each of r9, spelling, locations, location-inputs and pointer-inputs rerun separately through scratch/link-tmp -> scratch/real-tmp. Additional r8, dependency-unit and fake-dependency files pass there too. New equality operands are canonicalized independently of TMPDIR; none compares a recorded string with unresolved TMPDIR. |
| 6. CI filesystem | PASS on inspected requirements; remote result unclaimed | Workflow targets macos-15. The strategy requires no account-specific folder, mounted image or privileged setup. Each alternate spelling is checked by inode identity and accepted only if the volume supports it. Unsupported observations carry their spelling and skipped outcome in the receipt/event; sibling/routine tests use explicit pytest.skip. Locally all 55 are needs-owner, so the zero-skip claim is backed by observations, not pytest's single property-pass count. |

Canonicalization can follow beyond a component walk's quiet stop: a valid gitdir behind 65 links resolves further under canonical than the 64-link walker. Direct Git reads the destination but rejects the excessive-link route. Dangling and self-looping entries remain quiet and the output invariant holds. Separate mutations of the original link, its resolved target and an initially missing target between walk and canonicalization all yield incomplete evidence. This exposes no stable supported Git read that retention can lose, but suffix-growing loop targets violate the new return invariant (R10-F1). The documented post-discovery/ABA observation boundaries remain unchanged.

## Class table

Independently counted from `spelling.json`; byte-identical to fix r9:

| Position | Data-volume | Other case | Other normalization |
|---|---:|---:|---:|
| gitfile-target | 2 | 2 | 1 |
| intermediate | 2 | 2 | 1 |
| gitfile-pointer | 2 | 2 | 1 |
| commondir-file | 2 | 2 | 1 |
| commondir-target | 2 | 2 | 1 |
| objects | 2 | 2 | 1 |
| objects-info | 2 | 2 | 1 |
| alternates-file | 2 | 2 | 1 |
| alternates-line | 2 | 2 | 1 |
| git-entry | 2 | 2 | 1 |
| objects-pack | 2 | 2 | 1 |

## Generated-only reversion table

Byte-identical to the round, including first failing inputs before shrinking and assertions:

| Single-site reversion | Result | First failing generated input, before shrinking | Assertion |
|---|---|---|---|
| r9-text-canonicalization-dropped | Detected | `{"named": false, "position": "gitfile-target", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-alternates-canonicalization-dropped | Detected | `{"named": true, "position": "alternates-file", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-scan-canonicalization-dropped | Detected | `{"named": false, "position": "commondir-file", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-start-spelling-dropped | Detected | `{"named": false, "position": "gitfile-target", "spelling": "data-volume"}` | `assert str(checkout / ".git") not in recorded.get("git", []), recorded` |

## New findings

**R10-F1 — P3: the new universal spelling invariant is false for stopped scan targets.**

Location: dependencies.py:270, forwarded unchanged at :285/:165. False new claims occur in dependencies.py:140, docs/desktop/retention-archive.md:643, the main/fix report leads and pr-body-r10.md's new lead.

Executed input: healthy checkout/.git/unused -> outside/loop; outside/loop -> loop/tail. A second input uses loop -> other/tail and other -> loop/extra. Both repositories pass fsck, clean status and HEAD:f.txt reads. Discovery reports no incomplete evidence. The component walker stops at 64 links, then canonical follows the unresolved cycle again. Returned p includes a tail sequence; canonical(p) adds another suffix, and canonical(dirname(p))+basename(p) differs too. Thus the exact helper fails in both inputs.

This is not ranked P2: Git does not need the unused cyclic target, and the extra record is within D-R8-2's older boundary. Base comparison reproduces the path problem at 236c80883. The new P3 is this round's false assertion that every returned place meets the specified invariant, a reason the stop rule explicitly permits. The brief's instruction to leave folders.spelling's doubt path alone is respected: this is a false new unconditional claim, not a P2 about that older boundary. No change to canonical's doubt handling or deeper Git metadata reader is requested.

Fix: keep physical link locations, and omit an unresolved scan target that cannot satisfy the spelling equations (or retain an explicit walk-stop result so such a target is not inserted). A small in-memory trial filters only non-fixed-point scan targets and passes all 49 selected original r9, added r10, spelling and location controls with the xfail marks disabled. The proposed diff is retained as r10-scan-guard-trial.diff. It is evidence for a local repair, not a product edit; retain the invariant check on the final return too, since merely calling canonical once is insufficient for an unresolved cycle.

Two strict expected failures in tests/fake/test_retention_dependency_review_r10.py name R10-F1. The explicit red run fails at the invariant assertion with passing Git setup/teardown; the ordinary file remains green with ten passes and two expected failures. Receipt: r10-invariant-red.json; base comparison: cycle-base-comparison.json; trial: r10-trial.json.

Temporary corrected wording, until repaired: “Fix r9 canonicalizes successful pointer-file locations and scan targets, but unresolved suffix-growing symlink cycles still violate the spelling invariant (R10-F1).” Temporary line replacements (the intended invariant and C-8.4 must remain the repair target):

- dependencies.py:140: “Returned physical link entries use canonical parent plus entry name. Other returned places take canonical once; unresolved suffix-growing scan cycles can still violate the invariant (R10-F1).”
- desktop doc :643: “Successful physical file locations and scan targets take canonical. Unresolved suffix-growing cyclic targets can still violate the one-spelling invariant (R10-F1).” Keep the following accepted-alias/link-parent and transaction descriptions.
- Main report lead :3, fix report lead :3, the fix report's PR replacement lead and pr-body-r10.md :1: use the qualified Fix r9 sentence above or the review-status paragraph below.
- Fix report decision 3: “The helper asserts both spelling equations for the round's selected inputs; review r10's cyclic scan targets still fail them (R10-F1).”

After repairing the return behavior, the original unconditional wording can stand. These are factual corrections while a repair is pending, not a proposal to weaken the requested invariant.

## Listed differences

These are documented boundaries under the supplied stop rule and do not hold the verdict.

| Difference | Executed input/result | One-line documentation |
|---|---|---|
| D-R6-1 | Spaced admin, trimmed decoy, spaced common alias and physical spaced common through survey/archive; legacy readers miss/select trimmed paths while discovery retains actual storage. Private HEAD remains recoverable in the archive controls. | Outside discovery, archive/survey retain legacy whitespace parsing (:746). |
| D-R6-2 | Nested gitfile target ending in space or tab; survey's _into_admin classifies the trimmed target as internal. | Nested gitfile classification strips target bytes (:747). |
| D-R6-3 | Actual common folder `common ` plus `common` twin; discard_registration removes the twin admin and retains actual. | Registration cleanup strips common-directory output (:748). |
| D-R6-4 | Complete standalone bundle with spaced common alias verifies successfully; the verifier constructs its alternate using stripped common. | Bundle verification strips common targets (:749). |
| D-R7-1 | 1,048,577-byte unused-parent alternates line, healthy fsck/read, unrelated owner conservatively kept with incomplete evidence. | Unused inferred-parent alternates still report read failures (:750). |
| D-R8-1 | refs/heads/main -> owner/branch; complete discovery omits owner, forced removal changes read 0->128. | Links deeper than one level are unread unless on another tracked path (:734). |
| D-R8-2 | Unused direct entry -> unrelated/unused; reads remain unchanged after target removal. Width controls cover inside-owner, owner, worktrees, fixture home/root and /. Contained/equal targets pin only while the job is active; broad ancestor targets do not pin descendants. | Auxiliary discovery may retain unused targets/input links; equal/contained recorded paths pin and complete Git pins end with the job (:730). |

The suffix-growing unused-target behavior also exists at the base and belongs to the older unused-target difference; a temporary doc line is “Unresolvable cyclic scan targets may retain partially resolved names.” R10-F1 holds the verdict only because r9 newly states the contrary unconditional spelling invariant.

The D-R6 reader probes and D-R8 controls are retained under review-r10-data; their source is copied locally so their JSON writes do not alter prior report data. No deeper reader is requested.

## Holds and file counts

815 distinct ordinary passing node IDs are retained across 85 completed locked invocations, including independent review controls; mutation/reversion failures are intentional. All pre-existing requested repository nodes pass after the documented environment reruns; the two new R10-F1 cases are strict expected failures. Peak pytest concurrency 2; longest hold 269.84 seconds; peak pytest RSS 142.95 MiB. No run approaches ten minutes; no direct-lock fallback was needed. Per-test setup/call/teardown, timestamps, loads and observations are in the JSON receipts.

| File | Passing nodes | First contributing start (UTC) | Load 1/5/15 | Hold |
|---|---:|---|---|---|
| docs/reports/2026-10-31-retention-pins-review-r9-data/probes_r9.py | 14 | 2026-10-31T10:40:05.471282+00:00 | 38.66/38.13/36.34 | transactions-and-r8-controls |
| docs/reports/2026-10-31-retention-pins-review-r9-data/round_probes.py | 2 | 2026-10-31T10:40:44.671362+00:00 | 39.68/38.47/36.54 | deep-and-unused |
| review-r10-data/cycle_comparison.py | 2 | 2026-10-31T10:54:24.146022+00:00 | 42.35/41.13/39.03 | cycle-base-comparison |
| review-r10-data/edge_probes.py | 9 | 2026-10-31T10:39:56.267698+00:00 | 37.80/37.94/36.25 | edges |
| review-r10-data/loop_growth_probe.py | 3 | 2026-10-31T10:52:36.769642+00:00 | 44.69/40.73/38.62 | loop-growth |
| review-r10-data/probes.py | 7 | 2026-10-31T10:37:08.538106+00:00 | 39.51/37.66/35.80 | start-and-routine |
| review-r10-data/readers_probe.py | 8 | 2026-10-31T10:40:52.930600+00:00 | 39.49/38.47/36.57 | listed-reader-differences |
| tests/fake/test_retention_dependencies.py | 30 | 2026-10-31T10:39:38.047326+00:00 | 38.47/38.08/36.27 | linked-test_retention_dependencies |
| tests/fake/test_retention_dependency_incomplete.py | 19 | 2026-10-31T10:43:59.934102+00:00 | 42.20/39.71/37.41 | regressions-r1 |
| tests/fake/test_retention_dependency_review_r1.py | 18 | 2026-10-31T10:43:59.934102+00:00 | 42.20/39.71/37.41 | regressions-r1 |
| tests/fake/test_retention_dependency_review_r10.py | 10 | 2026-10-31T10:36:50.875113+00:00 | 39.01/37.43/35.67 | r10-end-to-end |
| tests/fake/test_retention_dependency_review_r2.py | 21 | 2026-10-31T10:42:38.287890+00:00 | 38.42/38.38/36.75 | regressions-r4-r2 |
| tests/fake/test_retention_dependency_review_r3.py | 6 | 2026-10-31T10:42:38.287890+00:00 | 38.42/38.38/36.75 | regressions-r4-r2 |
| tests/fake/test_retention_dependency_review_r4.py | 53 | 2026-10-31T10:42:38.287890+00:00 | 38.42/38.38/36.75 | regressions-r4-r2 |
| tests/fake/test_retention_dependency_review_r5.py | 60 | 2026-10-31T10:41:09.147253+00:00 | 39.57/38.54/36.63 | regressions-r5 |
| tests/fake/test_retention_dependency_review_r6.py | 16 | 2026-10-31T10:40:19.215475+00:00 | 39.15/38.26/36.42 | regressions-r7-r6 |
| tests/fake/test_retention_dependency_review_r7.py | 3 | 2026-10-31T10:40:19.215475+00:00 | 39.15/38.26/36.42 | regressions-r7-r6 |
| tests/fake/test_retention_dependency_review_r8.py | 2 | 2026-10-31T10:38:57.510513+00:00 | 39.13/37.99/36.14 | linked-test_retention_dependency_review_r8 |
| tests/fake/test_retention_dependency_review_r9.py | 4 | 2026-10-31T10:34:15.949304+00:00 | 38.44/36.10/34.93 | r9 |
| tests/fake/test_retention_dependency_spelling.py | 16 | 2026-10-31T10:44:46.984881+00:00 | 38.99/39.28/37.37 | dependencies-light |
| tests/fake/test_retention_parent_hint.py | 2 | 2026-10-31T10:40:19.215475+00:00 | 39.15/38.26/36.42 | regressions-r7-r6 |
| tests/fake/test_retention_pointer_failures.py | 7 | 2026-10-31T10:40:19.215475+00:00 | 39.15/38.26/36.42 | regressions-r7-r6 |
| tests/fake/test_retention_recovery_census.py | 2 | 2026-10-31T10:46:50.491693+00:00 | 38.42/39.12/37.58 | aliases-census |
| tests/fake/test_state_contract.py | 37 | 2026-10-31T11:06:52.891184+00:00 | 34.76/34.38/36.50 | guards |
| tests/fake/test_turn_retention_cwd.py | 6 | 2026-10-31T11:00:28.047755+00:00 | 32.42/37.99/38.58 | test_turn_retention_cwd |
| tests/fake/test_turn_retention_cwd_properties.py | 1 | 2026-10-31T11:01:15.462218+00:00 | 33.50/37.46/38.35 | test_turn_retention_cwd_properties |
| tests/fake/test_turn_retention_job_directory.py | 2 | 2026-10-31T11:02:15.462077+00:00 | 28.47/35.49/37.56 | test_turn_retention_job_directory |
| tests/fake/test_turn_retention_job_properties.py | 1 | 2026-10-31T11:02:54.184990+00:00 | 30.62/35.16/37.34 | test_turn_retention_job_properties |
| tests/fake/test_turn_retention_quarantine.py | 10 | 2026-10-31T11:03:44.723901+00:00 | 36.76/36.07/37.55 | test_turn_retention_quarantine |
| tests/unit/test_invariants_index.py | 25 | 2026-10-31T11:06:52.891184+00:00 | 34.76/34.38/36.50 | guards |
| tests/unit/test_retention_81_notes.py | 38 | 2026-10-31T11:18:46.668132+00:00 | 46.65/42.19/39.69 | notes |
| tests/unit/test_retention_alternates.py | 23 | 2026-10-31T10:44:46.984881+00:00 | 38.99/39.28/37.37 | dependencies-light |
| tests/unit/test_retention_alternates_review_r2.py | 15 | 2026-10-31T10:42:38.287890+00:00 | 38.42/38.38/36.75 | regressions-r4-r2 |
| tests/unit/test_retention_archive.py | 108 | 2026-10-31T11:07:45.195454+00:00 | 40.30/36.22/37.06 | archive-0 |
| tests/unit/test_retention_archive_properties.py | 2 | 2026-10-31T11:13:31.630731+00:00 | 37.38/38.57/38.14 | archive-properties |
| tests/unit/test_retention_dependency_aliases.py | 10 | 2026-10-31T10:46:50.491693+00:00 | 38.42/39.12/37.58 | aliases-census |
| tests/unit/test_retention_dependency_properties.py | 4 | 2026-10-31T10:46:59.136039+00:00 | 38.20/39.05/37.57 | dependency-property-0 |
| tests/unit/test_retention_dependency_units.py | 19 | 2026-10-31T10:39:09.336140+00:00 | 39.69/38.17/36.24 | linked-test_retention_dependency_units |
| tests/unit/test_retention_folded_pin.py | 38 | 2026-10-31T10:44:46.984881+00:00 | 38.99/39.28/37.37 | dependencies-light |
| tests/unit/test_retention_job_dependencies.py | 12 | 2026-10-31T10:44:46.984881+00:00 | 38.99/39.28/37.37 | dependencies-light |
| tests/unit/test_retention_location_inputs.py | 1 | 2026-10-31T10:35:10.565945+00:00 | 39.72/36.78/35.25 | location-inputs |
| tests/unit/test_retention_location_spelling.py | 11 | 2026-10-31T10:34:27.831764+00:00 | 38.74/36.27/35.01 | spelling |
| tests/unit/test_retention_locations.py | 22 | 2026-10-31T10:34:57.522089+00:00 | 38.88/36.48/35.12 | locations |
| tests/unit/test_retention_nested_leases.py | 27 | 2026-10-31T11:04:42.335545+00:00 | 35.45/35.74/37.33 | nested-leases |
| tests/unit/test_retention_pointer_inputs.py | 2 | 2026-10-31T10:35:35.760544+00:00 | 39.30/36.93/35.35 | pointer-inputs |
| tests/unit/test_retention_pointer_whitespace.py | 9 | 2026-10-31T10:41:09.147253+00:00 | 39.57/38.54/36.63 | regressions-r5 |
| tests/unit/test_retention_salvage_properties.py | 2 | 2026-10-31T11:14:47.497257+00:00 | 33.77/37.47/37.77 | salvage-properties |
| tests/unit/test_retention_shared_folders.py | 62 | 2026-10-31T10:51:24.685253+00:00 | 41.42/39.24/37.96 | shared-examples |
| tests/unit/test_retention_sweep_race.py | 13 | 2026-10-31T11:20:44.449367+00:00 | 47.54/44.58/41.00 | sweep-examples |
| tests/unit/test_test_module_names.py | 1 | 2026-10-31T11:06:52.891184+00:00 | 34.76/34.38/36.50 | guards |


The [complete hold table](/Users/maxghenis/.subfleet/worktrees/20261031-063315-pr272-review-r10/review-r10-data/hold-table.md) includes every repeated, environment-failing and intentional mutant/reversion invocation, with UTC start and 1/5/15-minute loads. The two skipped calls in r10-green are the strict R10-F1 expected failures, not unsupported-volume skips. Scratch-control node counts include the earlier unparameterized changed-target probe as well as its final three parameterized variants; all receipts retain the exact executed form.

Under `/private/tmp`, these six cases fail solely because scratch_reason classifies their temporary source path as scratch:

- `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`
- `test_blob_only_in_a_local_branch_is_archived_not_omitted`
- `test_local_path_remote_is_not_a_remote`
- `test_borrowed_or_shallow_object_store_is_scratch[alternates]`
- `test_borrowed_or_shallow_object_store_is_scratch[shallow]`
- `test_survey_is_read_only_and_predicts_the_pass`

All six pass unchanged with TMPDIR under this worktree's new `sf-r272r10-fixtures` folder. The archive union is 108 passing nodes. Both archive properties, both salvage properties, all 38 notes cases and all 13 sweep nodes pass at their normal settings. The live lsof case is excluded from the explicit archive selections.


Not run: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` (mandated live-case exclusion); the seven older survivors and other older mutants (not requested); full suite, Python 3.12, the person-only frontend live probe, remote CI. No requested hold remains unrun.

## Mutant table

Each has one failing call assertion and passing setup/teardown. All thirteen assertion expressions match fix r9; the nine r8 expressions also match r8.

| Mutant | Named killer | Result | Same r8 assertion |
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
| r9-text-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | Killed | New |
| r9-alternates-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[alternates] | Killed | New |
| r9-scan-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | Killed | New |
| r9-start-spelling-dropped | tests/unit/test_retention_location_spelling.py::test_routine_alias_keeps_the_exact_record[data-volume-worktree] | Killed | New |

Own gap check: the head's absent stopped-target guard fails both R10-F1 regressions; the in-memory guard trial passes all 49 selected controls with expected-failure marks disabled. Removing that guard restores the exact tested head code and its two invariant failures. This repair trial is separate from the thirteen named production mutants above.

## PR-body lines

Replace the new lead and Next status in pr-body-r10.md:

**Review r10 at `d2f1f36ab12b` requests changes for R10-F1 (P3).** R9-F1 passes all four original Data-volume inputs, eight case/normalization variants, and two home-firmlink controls. An unused direct link to a suffix-growing symlink cycle returns a location that changes under canonicalization, contradicting this round's new spelling invariant and lead paragraph. The stable checkout passes fsck, status and Git reads with complete discovery; this is not a new P2 retention-loss finding. The spelling property executes 55 distinct inputs across all 33 classes with no skips; an independent audit checks all 440 discovery returns made by the five round files. All 13 named mutants die at the recorded assertions; all four generated-only reversions reproduce the exact first failures. The listed reader boundaries remain unchanged. Retention stays dormant pending repair, approving rereview and the hub's completed CI and mergeability checks.

**Next:** repair R10-F1 without extending metadata depth, rerun its strict regressions and the spelling controls, obtain approving review and the hub's all-pass CI/mergeability checks. Activation remains separate.

## Delivery and cleanup

No product file was edited. HEAD remains d2f1f36ab12b. No branch commit, merge, remote push, GitHub call or external publication was performed. Git fixture commits and transfers use isolated local remotes/stores. No real provider or live Subfleet CLI/store was used. Every started session was waited to completion and no process detached. Scratch and worktree fixture cleanup are recorded in cleanup.json.

Durable added files: [review report](/Users/maxghenis/.subfleet/worktrees/20261031-063315-pr272-review-r10/review-r10.md), [r10 regression controls](/Users/maxghenis/.subfleet/worktrees/20261031-063315-pr272-review-r10/tests/fake/test_retention_dependency_review_r10.py), and the scripts, tables, notes and receipts in review-r10-data/. They remain uncommitted as requested; no shared Git-metadata write was attempted.

DONE
