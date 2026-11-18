"""Build the deliverable from review r11's own durable evidence."""
import collections,json
from pathlib import Path
D=Path(__file__).resolve().parent;ROOT=D.parent

def read(n):
 p=D/(n+'.json');return json.loads(p.read_text()) if p.exists() else {}
def obs(n,k):return [json.loads(o['value']) for o in read(n).get('observations',[]) if o['key']==k]
def link(p,label=None,line=None):return f"[{label or p}]({ROOT / p}{':'+str(line) if line else ''})"
def table(n):
 p=D/n;return p.read_text() if p.exists() else '(Pending.)\n'
summary=read('summary');counts=read('evidence-comparison-counts')
trial=read('r11-trial');green=read('r11-green')
perf=obs('routine-cost-repeat','routine-cost')
perf=perf[0]['summaries'] if perf else {}
cases=read('r11-case-comparisons')
mutants=read('mutant-results')
regen=read('unfinished-regenerable');early=read('unfinished-early-fences')
runs=[]
for p in D.glob('*.json'):
 try:r=json.loads(p.read_text())
 except json.JSONDecodeError:continue
 if isinstance(r,dict) and 'phases' in r:runs.append(r)
original=set(read('collect-earlier-corrected').get('collected',[]))
for r in runs:
 if r['name'].startswith('round-'):original.update(r.get('collected',[]))
original.add('tests/unit/test_retention_regenerable_properties.py::test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored')
ordinary=[r for r in runs if not r['name'].startswith(('mutant-','r11-red','r11-trial'))]
passed={p['node'] for r in ordinary for p in r['phases'] if p['phase']=='call' and p['outcome']=='passed'}
remaining=sorted(original-passed)
active=[r['name'] for r in runs if 'end' not in r]
archive_failures=read('archive-path-environment').get('failed_nodes',[])
archive_bullets='\n'.join('- `'+n+'`' for n in archive_failures)
completion=read('completion')
coverage_text=(f"All requested holds ended. Of {len(original)} requested original nodes, {len(original & passed)} passed; only the explicitly guarded early-fence property remains incomplete. No requested hold is unrun." if completion.get('final') else f"Work is still running: {len(remaining)} original nodes lack a completed pass, and active holds are {', '.join(active) or 'none'}.")
report=f'''VERDICT #272: REQUEST_CHANGES

Review r11 at **546b63ea170319705f2642da459663353dce6989**, against **475f6327890e3d76330890b0a26be37a50658ab9**. R10-F1's original cycle regressions are repaired. **R11-F1 is a new P2 introduced by the return filters:** a still-readable Git dependency can be omitted with complete discovery, after which retention retires its owner. This meets this round's stop rule and does not request a deeper reader.

Started **2026-10-31 16:08:30 UTC**; first admitted pytest **2026-10-31 16:12:39 UTC**. The macOS temporary folder was denied, so scratch is `/private/tmp/sf-r272r11`; scripts, tests, notes and receipts are durable in this worktree. The four-hour hold-start cutoff is 20:08:30 UTC. No sub-agents were used, per the lane rules.

## New findings

**R11-F1 — P2: a case-only rename makes the new filter silently drop a readable object store.**

Locations: {link('subfleet/dependencies.py','subfleet/dependencies.py:168 (found filter)',168)}, {link('subfleet/dependencies.py','subfleet/dependencies.py:297 (entries filter)',297)}, and their {link('subfleet/dependencies.py','predicate',174)}. The existing {link('subfleet/dependencies.py','alias observation',237)} / {link('subfleet/dependencies.py','comparison',301)} do not observe this spelling change.

Executed inputs are in {link('tests/fake/test_retention_dependency_review_r11.py','the new strict regression file')}. An allocated finished owner contains either (1) a repository borrowed by a normal `git clone --shared`, or (2) the pack directory reached by a link directly inside another repository's `objects`. A running borrower reads the owner's private commit. After canonicalization has returned the old spelling, one real rename changes `objects` to `OBJECTS`, or `packs` to `PACKS`, before the return filter runs. The tree root, object-store inode and contents stay unchanged. Both spellings still resolve on the tested case-insensitive volume; fsck and the identical private-commit read succeed before and after the rename.

The predicate rejects the earlier canonical spelling even though `os.stat` succeeds. `_walk` retains the supplied case for ordinary components, so `_target` still sees the same lexical result, device/inode and link chain. The moved check therefore reports no incomplete evidence. Head records only the two stable checkout storage paths, retires the required owner, and changes the same read from 0 to 128. The baseline keeps the third path inside the owner and leaves the read working. Both inputs stay within the promised alternates/direct-entry routes; neither uses a listed deeper-reader difference, an invalid Git layout, ABA or a dependency introduced after discovery.

{table('r11-case-table.md')}

The initial `--runxfail` run is **2 baseline passes / 2 head failures**, both at the owner-retention assertion, with passing setup/teardown. The ordinary file is **2 passed / 2 strict expected failures** naming R11-F1. {link('review-r11-data/r11-red.json','Red receipt')}; {link('review-r11-data/r11-green.json','ordinary receipt')}; {link('review-r11-data/r11-case-comparisons.json','full records and Git results')}.

**Fix:** retain or re-spell readable rejected locations, or detect their spelling change and fail closed. Comparing only the current `_target` tuple is insufficient. A small trial adds the canonical spelling to each readable alias observation and lets the existing post-spelling comparison detect its change. It skips that extra spelling observation when the original target is unreadable, preserving complete unused cycles. The {link('review-r11-data/r11-spelling-revalidation-trial.diff','in-memory trial diff')} passes **{sum(p['phase']=='call' and p['outcome']=='passed' for p in trial.get('phases',[]))} selected nodes**, including all four new controls, the 20 cycle cases, locations and the spelling property. Removing that trial observation restores the head regression and kills the added named trial mutant. Product source is unchanged; mutation hashes are in the receipts.

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

   A plain routine record makes **3 → 7** canonical calls. In the repeated 30-pair, alternating-order sample, median CPU time is **{perf.get('base',{}).get('cpu_ms',{}).get('median',0):.3f} → {perf.get('head',{}).get('cpu_ms',{}).get('median',0):.3f} ms**, and median wall time **{perf.get('base',{}).get('wall_ms',{}).get('median',0):.3f} → {perf.get('head',{}).get('wall_ms',{}).get('median',0):.3f} ms**. The earlier high-load 15-pair wall medians were 1.89 → 21.32 ms, with pauses up to 261 ms even at baseline; they do not isolate canonicalization cost. The measured CPU increment is about 0.15 ms per ordinary record, with no routine-repository retirement failure. Raw samples and loads are retained in `route-independent.json` and `routine-cost-repeat.json`.

2. **The move is implemented and useful, but does not cover every change.** Both list portions are built before `check`. Nothing in that gap writes `incomplete`; `locations` reads the captured sets, `recorded`, and the live spelling of `start`/entry paths, then performs string ancestry/coverage checks. The original check does not depend on a side effect from the old ordering. The two supplied race tests retarget inside `locations`, which is after the old check position and before the auxiliary list is built. Both pass at head. Moving check back kills the named resolved-target race at its evidence assertion. R11-F1 nevertheless leaves the observation tuple unchanged and gets no evidence. An executed controlled spelling-exception probe also shows the ordering change: baseline reaches check once, head zero times; both propagate the exception and return no record. That exception path is not a silently usable record.

3. **No stable-input evidence moved in the executed comparison grid.** There are **{counts.get('incomplete_comparisons',0)}** same-filesystem base/head comparisons: 240 calls from the two unchanged strategy modules across normal/linked runs, nine route rows, five routine controls, 18 longer-prefix rows and nine independent return-equation rechecks. These include repeated parameter recipes on fresh filesystem fixtures. Full incomplete lists, including order, file and reason, match entry for entry; the two strategy modules' full records also match. Unused cycles remain complete. The literal “every input” statement needs the stable-input qualification: decision 2 deliberately adds evidence to changes in its old gap. R11-F1 instead has equal empty evidence lists while losing the only owner dependency. That is the P2.

4. **Tests match the requested construction, with the oracle distinction made explicit.** The 20-case module has nine spelling/evidence rows, nine healthy-sibling cases and two races. `expected_error_files` is compared as a list, preserving order and any repeats; all nine baseline file lists match. The healthy-sibling unit test alone hardcodes two current storage places and checks a subset, so it does not itself execute 475f63278. The round's companion `test_route_comparison_against_475f6327890e` does execute that code on the same sibling with the cycle removed; its nine stored comparisons exist and match. This review independently reran that comparison for all nine rows. The race's placement is the named gap and its reversion fails. Both strategy modules and the entire contract are byte-identical to baseline; only the strict decorator was removed from r10's file. {link('review-r11-data/byte-audit.json','Byte/AST audit')}.

5. **Named mutants hold.** All three r10, four r9 and nine r8 mutants die at their named call assertions; all 16 match the fix-r10 table. The 13 older ones match r9 and all nine r8 ones also match r8. Setup and teardown pass. The seven previously explained survivors were not rerun. An added mutant removes the trial spelling observation and fails the new alternates regression at owner retention. Full table below.

6. **The stable-cycle text describes the nine fixtures, but cannot justify the general drop.** The cycle invariant, route expansion beyond the scan-only trial, unchanged contract and listed boundaries are supported. “Every dropped baseline string raises ELOOP” is true of the nine stored cycle fixtures; as an unrestricted statement it is false for R11-F1, where stat and Git reads succeed. “Nothing can be read through it” is true of those cyclic targets, not of every string rejected by the predicate. The desktop's broad assurance about changes during spelling also needs the observation qualification below. No temporary R10 listed-difference line was added. The 92/96 counts are consistent: all 92 planned holds have receipts, plus `baseline-route-files`, `red-cycles`, `red-cycles-final`, and `red-review-r10`. Neither count means the two guarded properties finished.

## Checks 1–6 and route comparison

**1. Routes:** the hub's unmodified seven-route script ran at baseline and head, and the two-link scan/alternates forms ran independently. The nine rows match the round's fsck/read, incomplete counts and unspelled counts exactly. Both equations were evaluated independently for every returned string, including recording the raw fixed-point result for physical links before applying their exemption. Below, `<B>` is the common fixture base, and `name×n` or `(a/b)×n` is a lossless repeated-component abbreviation. Every listed incomplete reason is ELOOP; full dictionaries and unabridged strings are in {link('review-r11-data/route-comparisons.json','the route receipts')}.

{table('route-table.md')}

The incomplete count sequence is **0, 0, 2, 2, 4, 3, 2, 0, 2**. Head returns no unspelled nonphysical string on these routes. R11-F1 is outside this cycle-only grid and removes a readable place instead.

**2. Earlier inputs:** the original findings and case/normalization/home variants above pass. The executed listed-difference controls below preserve their documented boundaries.

**3. Terminal paths and transactions:** the four database-only terminal/gone-target controls pass for legacy/recorded jobs, with and without old incomplete evidence. No pending output means no new discovery and no terminal Git pin. The two link-retarget/unlink fence controls and all six unused-target width/lifetime controls pass with `record` and `canonical` forbidden inside the transaction. A full maintenance pass with an active legacy borrower asserts that every canonical call occurs outside the SQLite transaction and still retires an unrelated owner. The round adds no filesystem work to database consumers.

**4. Linked TMPDIR:** all seven requested files pass with `link-tmp -> real-tmp`. The cycle file resolves its input base before expected-path comparisons. No new assertion compares a canonical recorded path with an unresolved TMPDIR path. This review's new r11 file also runs under linked TMPDIR, with two baseline passes and the same two strict expected failures. Both spelling runs execute 55 distinct generated inputs across all 33 position/spelling classes, without unsupported-volume skips.

**5. CI filesystem:** the new code/tests need Git, symlinks and the already-used macOS spelling API, with no new package or native helper. The 18 extra route runs use physical fixture bases of 180 and 260 bytes reached through a link. The longest pinned error file is **913 bytes**, below the measured **1024-byte PATH_MAX**. These assertions necessarily depend on path length: the 128-tail object path is base length + 653 bytes, so an arbitrary base of 371 bytes would not fit that bound. The tested 260-byte base leaves room for the supplied CI-style temporary layout. No CI result was fetched; the hub owns that check.

**6. The two previously unfinished properties:**

| Property | Own hold start (UTC) | Start load 1/5/15 | Outcome |
|---|---|---|---|
| Regenerable output, only/all dropped and rest restored | {regen.get('start','pending')} | {'/'.join(f'{x:.2f}' for x in regen.get('load',[]))} | Passed; {regen.get('seconds',0):.2f} s wrapper, 429.35 s pytest |
| Early fences equal reservation before/after quarantine | {early.get('start','pending')} | {'/'.join(f'{x:.2f}' for x in early.get('load',[]))} | Nine-minute guard; {early.get('seconds',0):.2f} s including cleanup; no assertion failure |

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

Current durable totals: **{summary.get('product_passing_nodes',0)} distinct passing product nodes** and **{summary.get('control_passing_nodes',0)} report-control nodes**, across **{summary.get('completed_runs',0)} completed invocations**. Repeated linked runs count once in the passing union; intentional red and mutant failures and the repair trial are excluded. Every invocation used the shared lock; peak pytest concurrency is **{summary.get('peak_pytest_concurrency',0)}**, longest hold **{summary.get('longest_seconds',0):.2f} s**, peak pytest RSS **{summary.get('peak_rss_mib',0):.2f} MiB**. No outside-lock fallback was needed. The wrapper's sysctl header is sandbox-denied; os.getloadavg vectors inside admitted pytest are retained. Environment setup ran once through uv_sync_hold with the locked dev group and Python 3.14.

{coverage_text} Full node coverage is in completion.json. The archive live-lsof node was explicitly deselected: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`.

The collection-only attempt `collect-earlier` exited before collection because this review's manifest mistakenly included the literal turn-file glob; it was corrected and the next collection enumerated all 743 earlier nodes. This is a review-runner error, not a branch finding. The early-fence property has its own incomplete hold, and the new head cases are strict expected failures. The six environment-only archive failures were:\n\n{archive_bullets}\n\nAll six pass unchanged with TMPDIR under this worktree's `sf-r272r11-fixtures`; that fixture folder is removed during final cleanup. They are environment failures, not findings.

Not run: the live-lsof exclusion; full suite; Python 3.12; the person-only frontend live probe; remote CI; the seven previously explained survivors and unnamed older mutants. No real provider, live Subfleet CLI/store or caller checkout was used.

{table('file-table.md')}

{table('hold-table.md')}

## Mutant table

The first 16 are the requested named head mutants. The added r11 row removes the repair-trial observation and restores the failing head behavior. Assertions and source hashes are retained in {link('review-r11-data/mutant-results.json','mutant-results.json')} and each hold receipt.

{table('mutant-table.md')}

## PR-body lines to change

Replace the new lead with:

> **Review r11 at 546b63ea1 requests changes for R11-F1 (P2).** R10-F1's cycle regressions pass, but the new return predicate can drop a still-readable Git dependency after a case-only rename during spelling. The existing alias observation sees the same lexical target, inode and link chain, so discovery stays complete. Two healthy real-Git inputs retire a running borrower's required owner and change the same read from 0 to 128; baseline 475f63278 preserves the pin. Retention stays dormant pending repair, approving rereview and completed CI.

Scope the evidence sentence as: “Every string dropped in the nine stable cycle fixtures raised ELOOP.” Add: “Review r11's readable case-rename inputs are a separate return-filter regression (R11-F1).” The 92 planned / 96 total invocation counts need no correction; keep the explicit statement that two fix-r10 properties were unfinished.

For the discovery docstring/desktop paragraph, replace the broad change-detection implication with current factual text until repaired:

> Non-link storage and scan strings are filtered by both spelling equations. This removes unreadable cyclic targets, but the current filter can also omit readable case aliases during discovery without incomplete evidence (R11-F1). Revalidation compares walked targets, identities, link chains and pointer contents; it does not yet observe a case-only name change that leaves those values unchanged.

The main report's r10 section and fix report should retain their dated nine-route results and add the r11 finding rather than generalizing the cycle fixture's unreadability to every rejected string. Keep C-8.3/C-8.4 and the listed reader boundaries unchanged.

Replace Next with: “Repair R11-F1 without extending metadata depth; rerun its strict regressions, cycle and spelling controls; obtain approving review and the hub's all-pass CI/mergeability checks. Activation remains separate.”

## Delivery

No product source or pre-existing test was edited. Added deliverables are this report, {link('tests/fake/test_retention_dependency_review_r11.py','the strict r11 tests')}, and {link('review-r11-data/notes.md','review-r11-data scripts, tables, notes and receipts')}. The working repository was not committed, fetched, merged, pushed or published; no GitHub call was made. Nothing was staged; deliverables remain untracked. Fixture Git commits/transfers use local isolated repositories only. All test runs ended before cleanup. The final scratch measurement was 577,990,656 allocated bytes (about 551 MiB); the worktree archive fixture added 3,883,008 bytes. Scratch and both worktree fixture roots were removed with the guarded cleanup; cleanup.json records the permission repair confined to one owned directory and all removals.

DONE
'''
(ROOT/'review-r11.md').write_text(report)
(D/'coverage-progress.json').write_text(json.dumps(dict(selected=len(original),passed=len(original&passed),remaining=remaining,active=active),indent=2))
print('report bytes',len(report.encode()),'requested remaining',len(remaining))
