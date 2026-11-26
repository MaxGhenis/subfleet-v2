# PR #286: review r2 build

Base: `548abd20cec22836df40b3ffa2b9619a78ed5bf1`. This build answers review r2 of PR #286.

The fast path now refuses ambiguous config values and unsupported packed files, and its ambient probe has neither templates nor local overrides. All four deadline call sites reject a computed non-positive budget before starting Git. `rgit.run` kills its own process group on timeout/cancel and bounds the subsequent drain to one second. The census measurement counts every helper-started Git process. The existing real-lsof archive test now restricts its query to its test-owned child. Both C-8.4 entries and the original report describe the product rules; historical live tables are unchanged.

## Validation

Tests run serially without `-n`, using `UV_CACHE_DIR=$TMPDIR/uv-cache`, under Git 2.55.0 and Python 3.14.7. Results are in [the r2 data directory](2026-11-02-retention-repo-walk-r2-data/).

| Requested file | Passed | Seconds |
| --- | ---: | ---: |
| `test_retention_git_common_dir.py` | 348 | 388.14 |
| `test_retention_repository_walk.py` | 59 | 187.21 |
| `test_attempt_tmpdir_retention.py` | 6 | 17.60 |
| `test_retention_81_notes.py` | 38 | 922.58 |
| `test_retention_archive.py` | 109 | 1442.14 |

The archive suite runs in two serial chunks: the initial run was interrupted after 37 passing cases so its existing full-machine lsof test could be narrowed; the remaining 72 cases passed with the child-only probe. The table reports the sum of both chunk times. No live process census is used for that test.

The four new r2 files passed **all 69 cases in 245.28 s**: 45 packed-ref cases, 9 process-lifecycle cases, 11 deadline cases and 4 retirement/restoration cases. This includes the literal r1 shell shim with a 0.5 s listing deadline and an asserted return within 1.5 s, plus independent Unicode-policy cases for mutation coverage. Both clamped and inherited QoS modes preserve Git's PID as the new session and group leader. A caller audit found only synchronous output/completion consumers of `rgit.run`; daemon cancellation uses the passed threading event, with no caller depending on a shared process group.

The exhaustive test ran 768 real Git listings: each of 256 bytes in three positions. Git listed the canonical ref with separator bytes **9 (TAB), 13 (CR), and 32 (space)**, and with no added byte before the oid or after the refname. Every listed case made `no_refs_under` return False. A 60-example Hypothesis property over generated packed files also passed.

Reviewer scratch rechecks: **18 passed, one deselected, in 78.90 s**. The supplied tests were copied temporarily below `tests/unit` to inherit the repository's safety fixtures and removed afterward. `test_review_r2_oracle_scratch.py`: 6 passed; `test_review_r2_deadline_scratch.py`: 6 passed (including the four non-positive-budget finding cases); `test_review_r2_port.py`: 5 passed with the parked case deselected; `test_review_r2_pin_port.py`: 1 passed. The unrelated PR #127 conversation/wake test file `test_review_r2_repro.py` is outside this retention task. P3-4's parked-job scenario is excluded from this build. The packed scratch reproduction asserts the old bug; its corrected assertions are ported to `test_retention_packed_retirement_r2.py`, covering TAB and CR with both base and head lookup and verifying private-commit restoration from the bundle.

The full **99-mutation driver passed: 98 killed, one equivalent, zero unexpected survivors, zero unapplied mutations and zero driver errors**. All 38 new r2 mutations died. Its final-source baseline passed all 472 tests in 324.23 s across the five discovery/r2 regression files. The historical `include-unrefused` equivalence was confirmed by all 472 tests passing with that guard removed: the plain-section and subsection allowlists independently reject every include form. The salvage checkpoint mutation died in the new clock-advance regression after its focused test passed. Every mutation is recorded in `mutants-r2.jsonl`, and the complete run was appended to `docs/reports/2026-11-01-retention-repo-walk-mutants.log`.

## Synthetic measurement

Both modes completed on the same synthetic fixture: 18,000 rows, 5,140 distinct workdirs, 2,300 existing workdirs and three repositories. The fixture and isolated HOME are under this job's TMPDIR; no live shapes or store were measured. Setup and fixture creation are excluded; every `rgit.run` in each measured census is counted.

| Measure | Old | New |
| --- | ---: | ---: |
| First job-row loop | 5.826 s | 0.305 s |
| Whole census | 114.000 s | 3.544 s |
| Git processes, whole census | 2,300 | 3 |
| Git processes, first-loop measurement | 0 | 3 |

Both modes found the expected three repositories. The new census's three Git processes are `config`, template-free `init` and `rev-parse`; its separate first-loop proxy is another census and includes the same three-process guard. Raw `scale-*.json` and mode logs are in the data directory.
