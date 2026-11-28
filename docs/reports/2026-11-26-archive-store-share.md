# D-STORE-SHARE1 · share identical files within a retention archive

Started 2026-11-26 at 10:30:16 UTC on detached HEAD at `53a7bac93d01afd9165588bcaf789dae75540757`. This work used synthetic test worlds. The first attempt to stage the tests was refused because the sandbox could not create the linked worktree's `index.lock`. The files are left in this worktree for Subfleet salvage.

## Implementation

- **Ruling 1, store sharing.** `subfleet/retention_archive.py:1683–1684, 2008–2038` keeps a builder-wide map keyed by SHA-256 and size. The link-count check at lines 1991–1992 defers the job when a file's open descriptor and walk entry disagree. After cloning or copying and checking the source signature, a one-link entry can reuse the first recorded store. The builder removes the duplicate clone before writing the alias progress record. An own-key hit keeps the fresh store. Multi-link entries keep their signature-keyed stores and `hl` grouping. A one-link entry can reuse a hard-link group's store.
- **Ruling 2, totals.** `subfleet/retention_archive.py:75–86, 1328–1366, 1781–1794, 1811–1823, 2029–2048, 2067–2103` counts one-link entries against a set of store names. The set is restored when a regenerable walk restarts, and it spans the three trees and the omission fallback. `shared_bytes` counts apparent bytes of entries reusing a counted store. Their originals' private blocks contribute to `freed_disk_bytes` after the temporary clone is removed. `freed_bytes` still sums omitted and regenerable bytes. `added_bytes` still counts the bundle, manifest, summary, rows and actual byte copies.
- **Ruling 2, reporting.** `subfleet/retention.py:389–398, 491, 744, 1186–1201` initializes and aggregates `shared_bytes` in the pass result and each pool using `ACCOUNTING_KEYS`. The two events use `accounting()` (`subfleet/retention_archive.py:954, 1357–1366`). Older accounting records default to zero. `subfleet/retention_cli.py:91–112` and `subfleet/retention_archive.py:2265–2282` publish the per-archive and total text figures and the per-archive JSON field. `tests/conftest.py:334–353` sums the new field when joining catch-up passes.
- **Ruling 3, resume.** `subfleet/retention_archive.py:1956–1980, 2029–2038, 2240–2249` accepts a valid cached alias for a one-link file, clears an unrecorded own-key clone before measuring, and registers and counts the alias by its referenced store. A multi-link cache entry must point to its own key. Cleanup keeps every referenced name, including one whose original owner is absent.
- **Rulings 4 and 5, verification and restore.** `subfleet/retention_archive.py:2011–2028, 2203–2238, 2289–2329` uses the source digest after cloning and checking for intervening writes, or the digest from a valid progress record. Builder readback and `check_archive` require all entries for a store to agree on SHA-256 and size. The builder discards a disagreeing store and defers retirement. The existing restore path recreates each independent entry with its own mode and mtime.
- **Tests and contract.** `tests/unit/test_retention_store_share.py` covers sharing, accounting, retries, verification, verified deletion, and restore metadata. `tests/unit/test_retention_archive_properties.py` generates independent identical files and checks store metadata and hard-link inode consistency. `tests/unit/test_retention_archive.py:736–739` now predicts the number of readbacks from distinct store names. In the fixture, `admin/HEAD` and `admin/ORIG_HEAD` share a 41-byte copy. `docs/acceptance-contract.md` updates C-8.4 under its existing clause ID. `docs/desktop/retention-archive.md` adds revision 9 and describes sharing, accounting, verification and independent restore.

## Readers

- `check_archive` reads the bytes of each store once and checks every entry's declared hash and size against the first entry, as directed by the later hub ruling.
- `_Copies.holds`, `_check_copies`, and `rfs.Reclaim(preserved=copies.holds)` continue to verify each entry's preservation. Their by-name verification and memoization use the agreed SHA-256.
- `_restore_tree` opens the referenced store separately for each independent entry. Only entries recorded with multiple links populate its hard-link lookup. Restore's `not_restored` reporting, bundle handling and `rows.json` readers have their original behavior.
- `_drop_unreferenced` works from the set of names used by entries. `list_archives` and `total_archive_bytes` use archive totals. `_require_file_bytes` checks each entry for preserved content, and `Retirement.watch` watches source inode identities. No other in-repository reader was found to require unique store names per entry.
- The hub supplied the out-of-tree refcount behavior of `archive_thin.py`. The #316 offload integration remains a merge-time hub check.

## Limits

- I1 covers the bytes, inode grouping, mode and mtime of each restored file. On APFS, clone-based restore of a shared file inherits the stored copy's extended attributes and file flags. The documented-limitation tests observed the first file's xattr and `UF_HIDDEN` on both restored files.
- On a byte-copy volume, the builder still copies a duplicate and removes the temporary copy after source verification. Only the retained copy contributes to `copied_bytes`.
- A hard-link inode present in two source trees retains the old per-tree accounting, so its one signature-keyed store may count twice. Published archives were left untouched. Older summaries without `shared_bytes` display zero. Valid in-flight cache records that already name distinct stores for identical bytes retain their names.
- The daemon log still computes net relief as `freed_disk_bytes - added_bytes`, which now includes unshared blocks of shared originals. The survey still estimates storage per file and can overstate `archived_bytes` for jobs with duplicates.

## Allocated bytes

Each source was written independently with `write_bytes`. The figures sum
`st_blocks * 512` over regular files in the published `files/` directory.

| Individual file size | Base, two stores | Sharing, one store | Reduction |
| --- | ---: | ---: | ---: |
| 64 KiB | 131,072 B | 65,536 B | 65,536 B |
| 1 MiB | 2,097,152 B | 1,048,576 B | 1,048,576 B |

Native APFS cloning and `FORCE_COPY` gave the same allocated byte totals.
With sharing, `archived_bytes`, `shared_bytes` and `freed_disk_bytes`
each equaled one file's size. With `FORCE_COPY`, `copied_bytes` also
equaled one file's size.

## Baseline evidence

The new tests ran before the production files and `tests/conftest.py` changed.
The base run gave 20 failures and 3 passes.

| Case | Base outcome |
| --- | --- |
| Identical files across worktree and job | `test_retention_store_share.py:89`, different stores |
| Hard-link group before single file | `test_retention_store_share.py:119`, different stores |
| Hard-link group after single file | Passed, the original separation was correct |
| Crash with fresh clone still present | `test_retention_store_share.py:189`, shared bytes were 0 instead of 39 |
| Crash after unlink and after recording the share | Both failed at `test_retention_store_share.py:154`, the duplicate clone was still present |
| Cached alias outlives the store owner | `test_retention_store_share.py:215`, progress named the alias's own store |
| Corrupted shared store | `test_retention_store_share.py:252`, the entries used different stores |
| Verified deletion after late write | `test_retention_store_share.py:276`, the entries used different stores |
| Accounting, native and forced-copy | Both failed at `test_retention_store_share.py:313`, 131,072 archived bytes instead of 65,536 |
| Frozen stream, with and without live hard link | Both failed at `test_retention_store_share.py:349`, stdout and the frozen stream used different stores |
| Regenerable walk restart | `test_retention_store_share.py:423`, the duplicate entries used different stores |
| Builder metadata disagreement, digest and size | Both failed at `test_retention_store_share.py:452`, the entries used different stores |
| Published metadata disagreement, digest and size | Both failed at `test_retention_store_share.py:472`, the entries used different stores |
| Shared APFS xattrs | `test_retention_store_share.py:495`, the entries used different stores |
| Equal size but different bytes | Passed, pure guard |
| Same-byte rewrite between clone and hash | Passed, pure guard |
| Trusted and scratch-source round trips | Both failed at `test_retention_archive_properties.py:164`, independently written identical files used different stores |

After the builder change, `02-focused` passed 21 cases, including both
properties and the APFS xattrs test. Its two accounting cases reached an
existing empty `retention.reclaimed` event and raised `KeyError` while reading
the payload. The assertion now checks nonempty accounting payloads, as the
existing archive accounting test does. Both cases passed in `03-accounting`,
bringing the focused coverage to 23 passing cases.

## Run setup

The workspace had no `.venv`. The default uv cache and the existing
`$DARWIN_USER_TEMP_DIR/uv-cache` both rejected writes, and
`uv sync --group dev --offline` stopped at cache initialization. I used the
already-installed lane environment read-only with uv's `--no-cache` flag.
`uv --no-cache run --no-sync python -c 'import subfleet; print(subfleet.__file__)'`
resolved to the assigned worktree. Pytest was 9.1.1 and runs were serial.

`mkdir "$TMPDIR/sshare-r1"` was denied with
`TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`. The scratch root was
`$PWD/build/sshare-r1`. Its real path and the fresh `pytest-<label>` paths
met the naming constraints. The test runner placed `TMPDIR`, Hypothesis,
pytest cache, and failure-preservation output under that root. A scratch-only
pytest plugin redirected `tests.failed_keep.ROOT` away from the host-wide
`/tmp/sf-failed`.

The following invocation was used for each labeled pytest run, with a
fresh basetemp and JUnit XML file. The test wrapper also captured stdout
and stderr together in `$run_root/$label.log`.

```sh
run_root="$PWD/build/sshare-r1"
export SSHARE_RUN_ROOT="$run_root" PYTHONPATH="$run_root:$PWD"
export PYTHONDONTWRITEBYTECODE=1
export UV_PROJECT_ENVIRONMENT=/Users/maxghenis/subfleet-v2-lanes/store-share/.venv
export HYPOTHESIS_STORAGE_DIRECTORY="$run_root/hypothesis"
export TMPDIR="$run_root/runtime"
uv --no-cache run --no-sync python -m pytest -q --timeout=600 \
  -m 'not real_lsof and not real_cwd' --basetemp "$run_root/pytest-$label" \
  -p sshare_local -o "cache_dir=$run_root/pytest-cache" \
  -o tmp_path_retention_policy=failed --junitxml="$run_root/$label.xml" "$@"
```

`01-base` and `02-focused` selected
`tests/unit/test_retention_store_share.py tests/unit/test_retention_archive_properties.py`.
`03-accounting` selected
`tests/unit/test_retention_store_share.py::test_shared_bytes_are_reported_in_archive_totals_events_and_cli`.
`04-end` started all 21 end-of-job modules. I interrupted the run after
`test_temporary_directory_source_is_scratch_by_default` failed. Its synthetic
manifest recorded `git.scratch = null` because the fallback `--basetemp`
was a sibling of `TMPDIR`. `05-end` reran all 21 modules with
`--basetemp "$run_root/runtime/pytest-05-end"` inside `TMPDIR`, restoring
the intended temporary-directory semantics. No production or test source
changed for that rerun.

`06-rechecks` used the nested basetemp and appended
`-o tmp_path_retention_policy=all` for the two cases below. The usual
`failed` retention policy had invoked the timer test's temporary unlink
stub during pytest cleanup, after its store was closed. The default
`all` policy lets the monkeypatch unwind before scratch cleanup.

```text
tests/unit/test_retention_archive.py::test_a_persistent_error_keeps_the_cache_and_backs_off
tests/unit/test_timers_retention.py::test_an_entry_deletion_cannot_remove_is_set_aside_not_left_half_deleted
```

`07-ordinary` reran `tests/unit/test_retention_ordinary_trees.py` with the
nested basetemp and these subprocess settings:

```sh
UV_OFFLINE=1 UV_NO_INDEX=1 UV_FIND_LINKS="$run_root/wheels" \
  UV_CACHE_DIR="$run_root/ordinary-artifact-cache"
```

The `05-end` ordinary-tree cases skipped because their `uv venv` subprocess
could not initialize the shared cache. I downloaded the locked 29,575-byte
`sortedcontainers==2.4.0` wheel with
`curl --fail --silent --show-error --limit-rate 300k --max-filesize 32768`
using the wheel URL from `uv.lock`. I checked its length and SHA-256 against
the lock. All four cases then retired and passed `check_archive` in
`07-ordinary` but failed the virtualenv regeneration assertion. A local
`--find-links` install has local provenance, which retention intentionally
keeps. I primed a scratch cache from the package index. The hash-checked
installation populated the wheel but omitted detached metadata needed by
the fixture's offline resolver. `08-ordinary-cache` skipped all four cases.
An ordinary index resolution populated the missing metadata. The matching
direct offline installation passed. `09-ordinary-offline` and
`10-ordinary-pinned` still skipped all four because the test process picked
up a different uv executable. In `11-ordinary-pinned` the test runner and
plugin put a delegating wrapper first on `PATH` after shell initialization,
using the same uv 0.11.7 binary that produced the cache.

The cache priming commands used the locked hash, then populated the index
metadata through the same uv executable:

```sh
export UV_CACHE_DIR="$run_root/ordinary-artifact-cache" UV_PYTHON_DOWNLOADS=never
export TMPDIR="$run_root/runtime"
uv venv --python "$UV_PROJECT_ENVIRONMENT/bin/python" "$run_root/ordinary-cache-prime"
uv pip install --default-index https://pypi.org/simple \
  --python "$run_root/ordinary-cache-prime/bin/python" \
  --require-hashes --no-deps -r "$run_root/ordinary-wheel-requirements.txt"
uv venv --python "$UV_PROJECT_ENVIRONMENT/bin/python" "$run_root/ordinary-cache-probe"
uv pip install --default-index https://pypi.org/simple \
  --python "$run_root/ordinary-cache-probe/bin/python" sortedcontainers==2.4.0
```

The passing rerun used the nested basetemp with:

```sh
SSHARE_UV_PATH="$run_root/uv-select" SSHARE_NEST_BASETEMP=1 UV_OFFLINE=1 \
  UV_DEFAULT_INDEX=https://pypi.org/simple UV_CACHE_DIR="$run_root/ordinary-artifact-cache" \
  "$run_root/run-tests" 11-ordinary-pinned tests/unit/test_retention_ordinary_trees.py
```

The measurement driver installed the same synthetic-home guard as pytest
and retired a separate job for each combination of file size and copy method.
Its commands were:

```sh
TMPDIR="$PWD/build/sshare-r1" UV_NO_CACHE=1 PYTHONPATH="$PWD" \
  uv run --no-sync python build/sshare-r1/measure.py base
TMPDIR="$PWD/build/sshare-r1" UV_NO_CACHE=1 PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1 \
  uv run --no-sync python build/sshare-r1/measure.py share
```

## Mutation evidence

`PYTHONDONTWRITEBYTECODE=1 python3 build/sshare-r1/mutants.py` applied
one mutation at a time to `subfleet/retention_archive.py` and passed the
following selectors to the pytest invocation above. The mutation runs
used `RETENTION_PROP_NO_SHRINK=1` to keep the required failure cases short.

The sandbox had refused Git staging, so the driver restored the source
from its scratch copy after each run. It compared the restored file and
the complete `git diff HEAD -- subfleet` to their pre-mutation contents
after each restore. All seven mutants failed their selected tests.

| Run | Selector under `tests/unit/test_retention_store_share.py::` unless given as a module | Observed failure |
| --- | --- | --- |
| M1 | `test_one_link_files_share_without_joining_a_hard_link_group[single-first]` | Line 121, the hard-link group used the unrelated earlier file's store |
| M2 | `test_equal_size_files_with_different_bytes_keep_separate_stores` and `tests/unit/test_retention_archive_properties.py` | Unit line 364 and property line 203 in both properties. Readback rejected a shared name with incompatible digests |
| M3 | `test_shared_bytes_are_reported_in_archive_totals_events_and_cli[default]` | Line 313, 131,072 archived bytes instead of 65,536 |
| M4 | `test_a_rewrite_that_would_share_defers_during_archive` | Line 391, deferred with `changed after archive` |
| M5 | `test_a_crashed_share_resumes_with_the_same_totals[shared-record]` | Line 188, resumed totals had two stores and zero shared bytes |
| M6 | `test_shared_bytes_are_reported_in_archive_totals_events_and_cli[default]` | Line 317, zero private bytes instead of at least 65,536 |
| M7 | `test_a_restarted_regenerable_walk_counts_shared_stores_once` | Line 438, all three totals disagreed with the manifest recount |

## End-of-job tests

`04-end` and `05-end` used the module paths in the table below in order as
pytest arguments. `05-end` reported 910 passes, one assertion failure,
one teardown error, four skips and one deselection. The assertion failure
was at `test_retention_archive.py:738`, where 21 reads were compared with
13 pre-readback reads plus nine entries. The fixture had eight distinct
non-lock stores to read back. The updated test and the timer case both
passed in `06-rechecks`. All four ordinary-tree cases passed in
`11-ordinary-pinned`. The table uses those six recheck outcomes for the
corresponding cases. The other 909 cases passed in `05-end`.

| Module | Pass | Fail | Skip |
| --- | ---: | ---: | ---: |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |
| `tests/unit/test_handler_refusals.py` | 374 | 0 | 0 |
| `tests/unit/test_retention_store_share.py` | 21 | 0 | 0 |
| `tests/unit/test_retention_archive.py` | 108 | 0 | 0 |
| `tests/unit/test_retention_archive_properties.py` | 2 | 0 | 0 |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 |
| `tests/unit/test_retention_regenerable.py` | 26 | 0 | 0 |
| `tests/unit/test_retention_regenerable_properties.py` | 1 | 0 | 0 |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 0 | 0 |
| `tests/unit/test_salvage_unindexable.py` | 86 | 0 | 0 |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 |
| `tests/unit/test_retention_ordinary_trees.py` | 4 | 0 | 0 |
| `tests/unit/test_retention_folded_pin.py` | 38 | 0 | 0 |
| `tests/unit/test_retention_turns.py` | 7 | 0 | 0 |
| `tests/unit/test_retention_qos.py` | 8 | 0 | 0 |
| `tests/unit/test_timers_retention.py` | 20 | 0 | 0 |
| `tests/fake/test_native_maintenance_startup.py` | 44 | 0 | 0 |
| **Total** | 915 | 0 | 0 |

One case was deselected by the requested marker filter,
`test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`. No case timed out.
The scans of exact accounting assertions and the end-of-job tests found
no other expectation that needed updating.

## Footprint and cleanup

These are the `date -u +%FT%TZ`, `df -H /` and `uptime` readings before
each run. Every pytest run started with at least 69 GB available.

| Run | UTC start | Available at `/` | Load (1, 5, 15 min) |
| --- | --- | ---: | --- |
| `01-base` | 2026-11-26T10:34:53Z | 91G | 44.49 37.05 35.35 |
| `measurement-base setup attempt` | 2026-11-26T10:39:33Z | 88G | 73.07 49.85 40.65 |
| `measurement-base` | 2026-11-26T10:39:51Z | 88G | 72.34 51.13 41.32 |
| `02-focused` | 2026-11-26T10:54:04Z | 70G | 85.09 72.55 64.52 |
| `measurement-share` | 2026-11-26T10:54:06Z | 70G | 84.45 72.62 64.59 |
| `03-accounting` | 2026-11-26T11:01:47Z | 70G | 34.08 68.70 68.71 |
| `m1` | 2026-11-26T11:02:16Z | 71G | 25.95 63.39 66.77 |
| `m2` | 2026-11-26T11:02:51Z | 71G | 18.47 57.38 64.45 |
| `m3` | 2026-11-26T11:04:27Z | 88G | 14.96 45.37 59.03 |
| `m4` | 2026-11-26T11:05:13Z | 88G | 14.45 41.03 56.73 |
| `m5` | 2026-11-26T11:05:51Z | 88G | 27.95 41.13 56.11 |
| `m6` | 2026-11-26T11:06:41Z | 86G | 36.85 41.07 55.12 |
| `m7` | 2026-11-26T11:07:13Z | 86G | 45.07 42.66 55.21 |
| `04-end` | 2026-11-26T11:08:02Z | 86G | 51.32 44.57 55.19 |
| `05-end` | 2026-11-26T11:33:54Z | 79G | 92.31 68.80 70.57 |
| `06-rechecks` | 2026-11-26T12:26:32Z | 71G | 16.92 21.71 32.89 |
| `07-ordinary` | 2026-11-26T12:27:02Z | 71G | 17.16 21.36 32.38 |
| `08-ordinary-cache` | 2026-11-26T12:28:10Z | 71G | 13.40 19.26 30.71 |
| `09-ordinary-offline` | 2026-11-26T12:29:02Z | 71G | 12.88 18.12 29.62 |
| `10-ordinary-pinned` | 2026-11-26T12:30:18Z | 69G | 19.87 19.63 29.19 |
| `11-ordinary-pinned` | 2026-11-26T12:30:48Z | 71G | 16.38 18.80 28.56 |

The largest observed test scratch footprint was about 39 MiB. The
measurement worlds under `build/sshare-r1` were 4,936 KiB for
`measurement-base` and 2,760 KiB for `measurement-share`. Both were removed
with guarded `rmtree` after measuring. The guarded final cleanup removed
the exact `build/sshare-r1` root, including all pytest bases and temporary
homes, mutation backups, logs, wheels, and the scratch uv cache and venvs.
It also removed the three worktree bytecode directories created by the
first measurement, `subfleet/__pycache__` (1,256 KiB), `tests/__pycache__`
(52 KiB) and `tests/unit/__pycache__` (20 KiB). The post-run checks found
no `retention-prop-*` leftovers. No test or measurement folders remain.

Git refused to create the linked worktree's index lock. HEAD stayed on
the base, and the ten changed files are available for salvage. No live
state or archive was accessed. The hub's #316 offload review is the
remaining merge-time check.

## Fix round 1 (review r1)

Started at 2026-11-28T02:52:01Z on detached HEAD
`3c21e5d3bfd0c4c669eaa0fc8eaea69534ea9bd9`. The tracked checkout was clean
and there were no untracked files.

- **P1, link count between walk and open.** `subfleet/retention_archive.py:1991–1992`
  compares the open descriptor's link count with the walk's link count,
  immediately after the inode check and before the omission branch.
  The existing mapping at lines 1894–1897 defers the job for one hour.
  `tests/unit/test_retention_store_share.py:399–446` changes an external link
  just before the open in both directions. Each case checks that the job
  folder keeps its bytes, modes and mtimes and that no archive is published.
  After the delay it checks retirement, archive verification and restore.
  Only `a-g1` and `z-g2` form a hard-link group. Restored `m-f1` has its
  own inode, one link, mode `0o755` and its original mtime. Its manifest entry
  has no `hl`. The added external link remains in place through the retry.
  The design records the deferral in revision 9 and step 6
  (`docs/desktop/retention-archive.md:3–7, 343–355`).
- **P3, C-8.4.** `docs/acceptance-contract.md:556` now explicitly allows a
  one-link file to reuse a hard-link group's stored copy and restore as its
  own file. The group keeps its own copy. The existing `pair-first` case of
  `test_one_link_files_share_without_joining_a_hard_link_group` exercises this
  case. The change-log entry at line 9 names both new tests.
- **P3, restore metadata and resume limits.** The flags test at
  `tests/unit/test_retention_store_share.py:553–570` set `UF_HIDDEN` only on
  `a-first`. Both entries named one store and both restored files had the
  flag. The xattr test at lines 531–550 likewise found the first file's
  xattr on both restored files. These results replace the design's blanket
  claim that restore drops the metadata. The revised bullet at
  `docs/desktop/retention-archive.md:1180–1187` describes what APFS
  clone-based restore inherits. The manifest excludes xattrs, flags and
  ACLs, and the byte-copy fallback writes only bytes. The review's deny-write
  ACL did not carry over. The limits above now cover file flags and retain
  the in-flight cache case. The design's tests row at line 1241 lists the
  race and flags coverage.

The fail-first run at the original PR head gave 22 passes and two failures
in `test_a_link_count_change_before_the_open_defers_the_job`:

- `[drop]`, `tests/unit/test_retention_store_share.py:432`:
  `assert ['link-count-change'] == []`. The job was pruned. In the review's
  separate reproduction, restore grouped `m-f1` with `z-g2` and gave
  `m-f1` mode `0o640`.
- `[add]`, `tests/unit/test_retention_store_share.py:433`:
  `assert 'changed its link count' in 'changed after archive: job/m-f1'`.
  The final check deferred it after the builder archived it.

The module passed all 24 cases with the guard in place, including both race
cases and both APFS metadata tests.

M8 removed the new check and both cases failed on the same lines as in the
fail-first run. M9 rejected only link drops with `before.st_nlink < st.st_nlink`.
`[drop]` passed and `[add]` failed at line 433 with
`changed after archive: job/m-f1`.

### Run commands and footprint

The lane venv resolved `subfleet` to the assigned worktree. Each run used
the following command with the label and selectors in the table. It used
four xdist workers and the default pytest temporary-path retention policy.
The scratch-only `sshare_local` plugin redirected the test harness's
failure-preservation and pruning root from `/tmp/sf-failed` into `$R`.
JUnit output supplied per-module counts.

```sh
R="$PWD/build/sshare-fix-r1"
export TMPDIR="$R/runtime" HYPOTHESIS_STORAGE_DIRECTORY="$R/hypothesis"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$R"
export UV_PROJECT_ENVIRONMENT=/Users/maxghenis/subfleet-v2-lanes/store-share/.venv
export SSHARE_RUN_ROOT="$R"
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider \
  -p sshare_local -n 4 --timeout=600 -m "not real_lsof and not real_cwd" \
  --basetemp "$TMPDIR/pytest-$label" -q -rs --junitxml "$R/$label.xml" "$@"
```

In the table, `share` is `tests/unit/test_retention_store_share.py`.
`full` uses all 21 module paths in the following module table, in order.

| Label | Selector | Pass | Fail | Skip | UTC start | Available at `/` | Load (1, 5, 15 min) | Basetemp (KiB) |
| --- | --- | ---: | ---: | ---: | --- | ---: | --- | ---: |
| `smoke` | `share::test_equal_size_files_with_different_bytes_keep_separate_stores` | 1 | 0 | 0 | 2026-11-28T02:53:39Z | 72G | 73.50 61.36 60.38 | 556 |
| `fail-first` | `share` | 22 | 2 | 0 | 2026-11-28T02:55:04Z | 72G | 42.84 57.47 59.19 | 13,740 |
| `after-change` | `share` | 24 | 0 | 0 | 2026-11-28T02:57:33Z | 72G | 67.97 63.43 61.32 | 13,548 |
| `m8` | `share::test_a_link_count_change_before_the_open_defers_the_job` | 0 | 2 | 0 | 2026-11-28T02:59:08Z | 71G | 41.24 57.30 59.30 | 1,120 |
| `m9` | `share::test_a_link_count_change_before_the_open_defers_the_job` | 1 | 1 | 0 | 2026-11-28T03:00:20Z | 71G | 56.95 58.07 59.38 | 1,128 |

### PR body changes for the hub

Read with `gh pr view 353 --repo MaxGhenis/subfleet-v2`. GitHub reported
`CONFLICTING` and `DIRTY` against `release/217`, and `gh pr checks`
reported no checks for the branch.

- Replace “Retention now stores one copy of each set of identical one-link
  files in an archive.” with “Within one archive, a one-link file can share
  a stored copy with matching SHA-256 and size, including a hard-link
  group's copy.”
- After “After it checks a one-link file's source signature, the builder
  points that entry at the copy it already stored and removes the duplicate
  clone.” add “The builder defers retirement if a file's link count differs
  between the walk and its open.”
- Replace “A hard-link group keeps its own stored copy, so it restores as a
  group and no unrelated file joins it.” with “A hard-link group keeps its
  own stored copy and restores as a group. A one-link file can share that
  copy and still restore on its own inode.”
- Expand “C-8.4 and the archive design describe the sharing and its limits.”
  to “C-8.4 explicitly covers reuse of a hard-link group's stored copy.
  The archive design documents open-time deferral and APFS restore metadata.”
- Replace “On APFS, a shared file restores with the extended attributes of
  the source that made the stored copy.” with “On APFS, clone-based restore
  of a shared file inherits the stored copy's extended attributes and file
  flags.”
- The sentence “A resumed build accepts a cached alias for a one-link file
  and counts it against the store it names.” needs the in-flight case in
  Limits. Add “A valid in-flight progress record can keep its own store
  for bytes that another entry already stores. A resumed build can
  therefore keep two stores for identical bytes, with totals matching its
  manifest.”
- Replace the Tests sentence starting with `test_retention_store_share.py`
  “(21 tests)” with “The 24 cases in `test_retention_store_share.py` and
  two round-trip properties in `test_retention_archive_properties.py`
  cover cross-tree sharing, both hard-link orders, crash and resume,
  shared-store corruption, verified deletion after a late write,
  accounting, and restore. The store-sharing cases also cover link-count
  deferral and retry for a dropped or added link, and APFS file flags.”
- Replace “On release/217, 20 of these 23 cases fail.” with “At the original
  release/217 base, 20 of the original 23 cases failed. At the original PR
  head, both new link-count cases failed and the other 22 store-sharing
  cases passed. With the guard in place, all 24 store-sharing cases passed.”
- Replace “Each of seven mutants fails its intended test.” with “The seven
  original mutants failed their intended tests. M8 failed both link-count
  cases and M9 failed the add case.”
- Keep “Hub run on 3c21e5d3b: 911 passed and 4 skipped” labeled as the
  original-head result. Add the fix-round lane result and the hub's
  collected-head result after the hub runs it.
