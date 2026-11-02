# Retention's repository walk (D-RET-WALK1, D-RET-SALVAGE-WALK1, D-RET-PIN-SCAN1)

Subfleet 2.1.12 went live on 2026-11-01 with retention on, and retention gave almost nothing. Its first pass ran 16 min 17 s and pruned 0 jobs, against a 180 s deadline. This report covers the three stalls behind that, how this branch removes them, and what stays unproven.

## What stalled

Each stall sits on the path of a job whose tree and workdir are both gone. A pass takes the oldest eligible jobs first, and of the 482 writable jobs among the oldest 1,000, 238 are of this kind. So almost every batch of 32 holds one.

| Stall | Where | Live cost |
| --- | --- | --- |
| Repository census | `known_repositories` ran one `git rev-parse --git-common-dir` per distinct workdir on disk and never read the pass deadline | 2,292 workdirs, 12 to 14 min at load 120 to 150 |
| Salvage search | `source_of_gone_tree` listed `refs/subfleet-salvage/` in every census repository, with no deadline | 542 repositories held refs; those listings took 100.7 s at load 70 to 90 |
| Pin read | `probe_tmpdir_rows(job_id=...)` walked every `probe.state` row, under the store lock, at each start and each commit | 3.555 s for that query, 4.3 to 5.7 s for `_pin_reasons` at load 100 |

2,379 terminal jobs have salvage rows and neither tree nor workdir, so the salvage search was the next wall after the census. The pin read would have turned a working census into about 3 minutes of store lock per pass (32 starts).

## The first fix did not work, and only live data showed it

The first commit on this branch, e05fd3a26, passed 181 tests. Measured over the live workdirs, it answered 1,001 of 2,279 without a process. The other 1,278 fell back to Git, about 350 s at the 3.6 per second measured at load 100, so every gone-tree job would have deferred at the deadline.

Its parser also had a defect. It ignored sections other than `core`, so it answered for repositories that Git refuses: `push.default=bogus`, `branch.autosetupmerge=maybe` and `user.useconfigonly=maybe` each make `git rev-parse` fail under Git 2.55.

The synthetic fixture could not show either problem. Its repositories all had default config. `tools/measure_retention_repo_walk.py` therefore proves the scale of the loop, not the live hit rate.

## What changed

- **A certain negative.** A walk that reaches the root with no `.git` and no `HEAD` on the way needs no process. This covers 611 live workdirs. A filesystem boundary stays with Git.
- **Config by allow-list.** Plain sections accept only names tried against real Git with bad values: `extensions.worktreeConfig` (a boolean, since Git dies on anything else), `extensions.partialClone`, format versions 0 and 1, `core.hooksPath`, `core.untrackedCache`, the core booleans (including sparse checkout), and the named keys of `pull`, `http`, `submodule`, `rerere`, `gc`, `blame` and `user`. Repository subsections are limited to `remote`, `branch` and `submodule`, whose keys Git reads lazily; differential cases try bad values on several keys in each. Every other subsection, including `[color "advice"]` and unknown sections, goes to Git. `core.compression`, `index.sparse` and `user.useconfigonly` fail Git on a bad value, so they stay refused. Every value containing `#`, `;`, a literal double quote or a backslash goes to Git, including values in the admitted subsections.
- **`config.worktree`.** Git reads the checkout's own file only with the extension on, so the parser does the same. It accepts only plain `[core]` sections there; any subsection or other section goes to Git.
- **An ambient-config guard.** Once per census, `rgit.ambient_plain` uses `rgit.run` and retention's `rgit.environment()` to check system, global and XDG config. Scoped config outside any repository must load successfully and contain no `include.*`, `includeif.*` or `core.repositoryformatversion` key. Git ignores the last outside local config, so the guard conservatively refuses it instead of parsing its value. Git must also initialize a fresh repository under the job's `TMPDIR` with `--template=`, empty its local config, and discover it, catching ambient values rejected during repository startup without template or init-written overrides. Failure logs once that pass and sends every workdir to Git. The successful guard costs exactly three Git processes per census.
- **A targeted salvage search.** `rgit.no_refs_under` proves absence only for an ASCII `refs/` prefix without `*`, `?`, `[` or backslash, in a files ref store whose common directory has no `commondir` redirect. Packed files must use a conservative subset of Git's exact LF-terminated format, with one ASCII space between each oid and refname; other separators, malformed lines or a missing final LF require Git. A repository without one of the job's refs can't be the job's, so the search skips it. Any doubt (a loose folder, a packed line, a reftable store, an unreadable file, another common-directory redirect or unsupported prefix) falls through to `for-each-ref`, as before.
- **A deadline on listings.** `Context.salvage_refs` checks the census deadline and passes the time left to `refs_under`, capped at 120 s. Each probe, fallback and listing computes its remaining budget once and refuses to start Git when that value is zero or negative. A listing failure after the deadline defers the job for one hour through the existing path and is not cached; other failures keep the existing behavior. `rgit.run` starts Git in its own process group; timeout or cancellation sends that group SIGKILL, bounds the pipe drain to one second, and closes pipes if the drain still cannot finish.
- **The job's own index.** `probe_tmpdir_rows` writes `+kind` when it filters by job, so the planner takes `events_job`. No schema changes.

## Review r1 findings and fixes

Review r1 found a decision change: unchecked subsections in a linked checkout's `config.worktree` could put a repository in the fast census even though Git refused it. Ambient conditional includes could do the same. The parser now limits repository subsections and refuses every subsection in `config.worktree`; the ambient guard sends every workdir to Git on includes or startup failures. Real-Git comparisons cover the admitted sections, the bad advice color, conditional includes and uniform bad ambient values.

The review also found three unsound ref-absence proofs: a second `commondir`, Unicode normalization and Git pattern matching. All three now require Git. A slow salvage listing previously received the default 120 s even near the deadline and cached a timeout as an empty listing; it now gets the remaining time and a failure after expiry defers without caching. The contract and invariants below state those limits. The parked-job batch-slot finding P3-4 predates this branch and remains out of scope.

## Review r2 findings and fixes

The ambient probe could inherit a template override or a value written by `git init`, hiding invalid global config from the startup check. It now passes `--template=` and empties the fresh repository's config before discovery. Ambient `core.repositoryformatversion` is conservatively refused because Git ignores it globally and cannot validate it through startup; invalid `core.logallrefupdates` is rejected by init itself. Config values containing comment markers or literal quotes could resolve differently from Git; all such values now require Git in every admitted section.

TAB and CR packed-ref separators let Git list salvage refs that the substring proof missed. The proof also refuses prefixes outside `refs/`, because an option-like string is interpreted by `for-each-ref` as an option. It validates a conservative subset of the exact packed format before searching; noncanonical separators, malformed lines and missing final LF require Git. Real-Git tests exhaust all 256 bytes in the separator, before the oid and after the refname, and a bounded generated-file property checks the same implication. End-to-end TAB/CR regressions compare retirement and bundle restoration with the base lookup.

The four budget call sites now reject their computed non-positive timeout. Commands started by `rgit.run` run in a separate process group, which is killed on timeout or cancellation; a one-second drain bound also covers children retaining pipes. The literal r1 shell shim is covered. The measurement now counts every `rgit.run` subprocess, including the three ambient commands. P3-4 remains outside this branch.

## Live results

These historical measurements predate review r2; the hub will remeasure live shapes. Refusing quoted values can increase Git fallbacks, so this table is not a claim about the current live hit rate. All reads were on the 2.1.12 store and the real workdirs, at load 70 to 110. Rows of the income jobs and `gr-v` stayed out.

| Measure | Before | After |
| --- | --- | --- |
| Live workdirs that need Git | 2,279 of 2,279 at base, 1,278 at e05fd3a26 | 9 of 2,280 |
| Census of one pass | 12 to 14 min | 4.17 s |
| Repositories one salvage search lists | every census repository | 1 (25 of 25 sampled refs), 0.05 s a job |
| `probe_tmpdir_rows` for one job | 3.555 s | 0.002 s |
| `_pin_reasons` for one job | 4.3 s profiled, 5.7 s unprofiled | 0.29 s profiled |

The 9 that used Git in that measurement: 3 `core.worktree`, 2 dead gitfiles, 2 stray `HEAD` files, 1 `core.longpaths` in `config.worktree`, and 1 refused earlier in the walk.

Historical differential against real Git on the live set: 1,117 positives, covering every distinct repository, with 0 differences; 150 sampled negatives with 0 differences; 200 sampled `no_refs_under` negatives with 0 wrong.

## Scale measurement

`tools/measure_retention_repo_walk.py`, one worker, 18,000 job rows, 5,140 distinct workdirs, 2,300 on disk, three repositories. The review-r2 rerun uses an isolated HOME under the job's TMPDIR; host load is recorded in the raw logs. Fixture creation and setup are excluded.

| Measure | Old | New |
| --- | ---: | ---: |
| First job-row loop | 5.826 s | 0.305 s |
| Whole census | 114.000 s | 3.544 s |
| Git processes, whole census | 2,300 | 3 |
| Git processes, first-loop measurement | 0 | 3 |

The counter wraps every `rgit.run`, rather than just `common_dir`. New's three processes are the ambient `config`, template-free `init` and `rev-parse`. Its first-loop proxy calls a separate census and includes that fixed guard too. Both modes found the expected three repositories. Raw measurements are in the r2 handoff's data directory.

## Invariants

The safety rule is that speed never changes a retention decision. These invariants are checked against real Git over the committed corpus; unsupported input requires Git rather than extending its parser.

1. **Git is the oracle.** With the template-free, local-config-free ambient guard satisfied, `fast_common_dir(path)` equals `common_dir(path)` across the composed repository shapes and config lines the tests cover. Values with comment markers, quotes or backslashes require Git. A refused shape uses Git; a failed ambient guard makes the entire census use Git. A difference in a completed discovery is a bug.
2. **Determinism.** The probe keeps no memory, so a second call gives the same answer.
3. **Same census.** On stable inputs, a complete `known_repositories` returns the same set as the old walk.
4. **No partial census.** At the deadline `ctx.repositories` stays unset, the job defers, and no journal, lease, quarantine or archive remains.
5. **The bound holds.** Each discovery probe, fallback or salvage listing starts Git only with one computed positive remaining budget, capped by its existing timeout; a zero or negative budget starts no Git. Timeout or cancellation kills the Git process group and bounds the final drain to one second.
6. **Same salvage answer.** For a completed search over stable, readable repositories, skipping only proved absences preserves the candidate order and finds the same repository as listing every repository. Common-directory redirects, option-like prefixes, noncanonical packed files and unsupported stores or prefixes use Git; the absence proof never skips the TAB/CR records Git accepts.
7. **The negative is sound.** For the same stable ref store, `no_refs_under` returning True implies a successful `refs_under` listing is empty. The proof refuses common-directory redirects, prefixes outside `refs/`, non-ASCII prefixes and Git pattern characters; it never proves absence from an unsupported or unreadable store. A packed file must pass the exact-format subset check (including one-space separators and final LF) before its bytes can prove absence.
8. **Same probe rows.** One job's rows match what the `kind` scan returns.
9. **Fail safe.** A repository missing from the census keeps the job, its registration and its salvage refs.

## Tests

The pre-review run passed 384 tests on 8738366e7 across the five retention test files (`test_retention_git_common_dir`, `test_retention_repository_walk`, `test_attempt_tmpdir_retention`, `test_retention_81_notes`, `test_retention_archive`). CI runs the whole suite.

Review-r1 validation: all 461 tests passed serially, without `-n`, with `UV_CACHE_DIR=$TMPDIR/uv-cache`, in 1557.68 s under Git 2.55.0. Per-file case times include fixture work:

| File | Passed | Time |
| --- | ---: | ---: |
| `test_retention_git_common_dir.py` | 249 | 94.64 s |
| `test_retention_repository_walk.py` | 59 | 80.15 s |
| `test_attempt_tmpdir_retention.py` | 6 | 0.72 s |
| `test_retention_81_notes.py` | 38 | 264.83 s |
| `test_retention_archive.py` | 109 | 1085.75 s |

Review-r2 validation: all **560 cases** in the five requested files passed serially, without `-n`, with `UV_CACHE_DIR=$TMPDIR/uv-cache`, under Python 3.14.7 and Git 2.55.0. Logs are in [the r2 build report](2026-11-02-retention-repo-walk-r2.md) and its data directory. The archive suite completed as 37 plus 72 cases after its existing real-lsof test was restricted to its test-owned child; its time is the sum of both chunks.

| File | Passed | Time |
| --- | ---: | ---: |
| `test_retention_git_common_dir.py` | 348 | 388.14 s |
| `test_retention_repository_walk.py` | 59 | 187.21 s |
| `test_attempt_tmpdir_retention.py` | 6 | 17.60 s |
| `test_retention_81_notes.py` | 38 | 922.58 s |
| `test_retention_archive.py` | 109 | 1442.14 s |

The **69 additional r2 regression cases passed in 245.28 s**. The 768-listing byte census found only TAB (9), CR (13) and space (32) accepted as separators, and no accepted added byte before the oid or after the refname; every listed ref made the absence proof return False. A 60-example generated packed-file property passed, as did the literal r1 shell shim (0.5 s deadline, return asserted below 1.5 s) and all four TAB/CR base/head retirement and bundle-restoration comparisons. The supplied retention scratch rechecks passed all 18 selected cases in 78.90 s, with only the out-of-scope parked-job case deselected. Mutant results are in the handoff. The live measurements above remain historical.

The committed driver is `tools/mutants_retention_repo_walk.py`; it resolves its source and test paths from the repository and mutates one rule at a time in a disposable scratch copy. Its baseline runs both original fix test files plus the deadline, packed-ref and process-lifecycle r2 regression files; each mutant runs focused tests first and retries that complete suite if those pass. All runs are serial. Its output is committed in `docs/reports/2026-11-01-retention-repo-walk-mutants.log`. The original 30-rule run below is historical evidence; the review-r1 run adds mutants for the subsection, ambient-config, ref-absence and listing-deadline rules. The review-r2 driver adds independent mutations for template isolation, local config emptying, each refused value character, packed-format boundaries, each non-positive budget guard, session/group killing and bounded draining, plus the conservative prefix and ambient-format guards.

Review-r2 mutants: the final-source baseline passed all 472 tests in the five discovery/r2 files. Of 99 mutations (61 existing plus 38 new r2 rules), **98 were killed, one was equivalent, and none survived unexpectedly**; no mutation was unapplied and no driver error occurred. Every new r2 mutant died, including all four non-positive-budget sites, each refused value character, packed-format boundaries, template/config isolation and process-group cleanup. `include-unrefused` remains equivalent, confirmed with all 472 tests passing: the plain-section and subsection allowlists still reject includes. The complete run is appended to the same mutant log, with a standalone JSONL copy in the r2 data directory.

Review-r1 mutants: the final-source baseline passed all 308 fix tests. Of 61 mutations (the original 30 plus 31 review rules), 60 were killed, one was equivalent, and none survived unexpectedly; no mutation was unapplied and no driver error occurred. `include-unrefused` is now equivalent: removing its explicit check still leaves the plain-section and subsection allow-lists rejecting every include form. The driver confirmed that equivalence with all 308 fix tests passing. Every new review-rule mutant died, including the slow-Git timeout-forwarding mutant. The appended run is recorded in the committed mutant log.

The original driver removed 30 safety rules one at a time. Its first run killed 28. The two survivors were real gaps, and each gained a test:

- `include-unrefused`: no test used an include with a subsection, which is the only form the section allow-list doesn't already refuse. `[includeIf "gitdir:/"]` now has one, and a second test shows a bad included file failing Git and the probe alike.
- `salvage-proof-needs-all`: the answer stayed the same and only the cost changed, because a repository lacking one of the job's refs still got listed. A test now counts the listings.

The original rerun killed both, so all 30 died on those tests. `unterminated-quote-passes` matched its pattern and died in the first run.

## What remains

- **The stalls bind under load, not always.** Retention on unpatched 2.1.12 pruned 353 jobs in the 02Z hour of 2026-11-02, with load near 10 while the disk floor held every lane. Its passes completed with 21 to 32 retired, against 1 to 6 at load 100 to 150. Two things changed together, the load and the 24-hour defers on jobs already tried, and nothing here separates them. This branch's rate under load stays unproven until a release carries it and the lanes return.
- **Bytes.** The 48 GB estimate from the 2026-10-31 survey has no support so far. By 03:04Z retention had pruned 491 jobs, freed 0.785 GB on disk and added 0.749 GB to the archive, about 0.04 GB net. Of the 5.2 GB in the 586 archives on disk at 03:06Z, 2.59 GB sits in 72 jobs whose repository has no network remote, and those free nothing. Regenerable output came to 0 bytes. Trees on disk sit throughout the age order (142 of the oldest 1,000 jobs, 366 of the newest 1,000), so later jobs may yield more, but no measurement says so.
- **Jobs it still keeps.** 1,851 terminal jobs have neither tree, workdir nor salvage rows. Those it can't place defer 24 h as `repository not found`, by design.
- **Per-job Git calls.** `begin` still runs `common_dir`, `repository_near` and `object_format` for each job. They are bounded per job, and this branch leaves them alone.
- **Other Git versions.** The allow-list rests on Git 2.55, the installed version. The differential tests run against whichever Git the host has, so an upgrade that changes a rule fails a test.
