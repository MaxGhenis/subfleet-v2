# Model text and lane limits — 2026-10-11

The initial build-lane record below describes `a2c155ed72b9`. Its structural-success decision is superseded by fix round 1 below.

Claude reports now cannot supply classifier signatures or closure clocks. Codex agent/final-answer text is excluded from failure patterns; structured completion precedes recovered authentication diagnostics. `lanes release <lane> --limits --note <text>` releases active guessed provider-limit/credits closures and records the note and closure ids. Reported clocks are preserved; reported-only and no-open-closure requests are refused. Mixed clocks release only guessed closures. A separate daemon action protects operator holds when talking to an older daemon.

Incident: seven rc-0 Claude Opus 5.5 attempts between 12:30Z and 13:23Z quoted usage-limit language, failing three jobs and closing five accounts for an hour. The three regression reports are minimal reconstructions of the two US-hub triage jobs and the app inventory, rather than copied live artifacts.

C-9.8 decision: a structured `rate_limit_event status=rejected` remains authoritative and can override rc-0 non-error success. `overageStatus` remains diagnostic. Text cannot override success. To satisfy exact model-text erasure, Claude success and Codex completion/agent-frame success are structural; the daemon still rejects empty deliverables under C-12.6 and retains `provider_verdict`, verified by the existing tests/fake/test_notice_header.py:345 test.

| Regression | Before, base 4c6a8a9c989e | After, fixed-code runs |
| --- | --- | --- |
| triage-first: “hit its usage limit” | limited | ok, no closure |
| triage-second: “hit its usage limit” | limited | ok, no closure |
| inventory: “Continues elsewhere when an account hits its usage limit” | limited | ok, no closure |

The unchanged base source plus the initial regression file at b917b218a ran in a local replay directory: **3 failed in 12.61 s**, all for the expected limited-versus-ok mismatch. First fixed run: **804 passed, 6 failed, 3 warnings in 256.24 s**, over 12 files and 810 tests. Three new CLI tests used non-callable fake handlers; these were corrected. Three existing process checks depended on unavailable host inspection; their absent-process, changed-start-time and dead-guardian inputs are now explicitly stubbed. Production process handling is unchanged. The same-suite rerun under heavy host load reached **746 passed, 2 failed in 1330.71 s**, then was interrupted at the lock-hold boundary before the remaining state checks. Both failures are existing CLI wall-clock assertions: monotonic poll gaps and completion within five seconds; both passed in the first run. All four new properties passed another 600 examples, with the same 82 invalid generation attempts. The final focused replay passed **39 tests in 137.54 s**, including both timing checks and every state-contract test. All six corrected tests have passed. All 810 selected cases passed across the fixed-code runs; an uninterrupted green 810-case rerun was not completed.

| New invariant | Passing examples in first fixed run |
| --- | ---: |
| Claude model-text mutation and frame insertion preserve class/closure | 150 |
| Existing limit evidence with surrounding model text preserves class/scope/clock | 150 |
| Claude stream equals stream with all model text erased, including damaged tails | 150 |
| Codex agent text/erasure preserves class/closure | 150 |
| Raw Claude fixture differential checks | 20 |

Hypothesis uses all 14 Claude/Codex classifier and clock regexes plus arbitrary Unicode; the four new properties passed 600 examples with zero failing examples (82 invalid generation attempts). Existing Hypothesis checks also passed. Release coverage includes 11 new daemon cases, seven new CLI cases, the extended routing test, and existing operator-hold behavior. All 20 Claude fixture replays and 13 required Codex fixture replays passed in the first fixed run.

Clock/source evidence: the existing error-result limit fixtures remain CLI evidence even with rc 0. The synthetic-model epoch frame yields reported 2026-09-05T16:00:00Z; the “You've hit your limit · resets 6:40pm (America/New_York)” reconstruction yields reported 2026-09-05T22:40:00Z. Model text cannot supply a clock to a genuine clockless CLI limit. `cli_placeholder` behavior was not broadened. The Fable fixture embeds a verbatim experiment-0 provider rejection payload/result in an assembled envelope; the session/no-clock/weekly fixtures are marked synthetic. No captured assistant limit shape requiring an extension was found; the epoch/prose assistant cases are reconstructions using the existing synthetic-model shape.

| Changed file:line | Change |
| --- | --- |
| subfleet/adapters/claude.py:1535 | CLI-only corpus, success guards, quota clocks, structural success; version/auth/transient prose uses the same boundary. |
| subfleet/adapters/claude_stream.py:318 | Document texts as diagnostics and cli_texts as classifier/clock evidence; placeholder predicate retained. |
| subfleet/adapters/codex.py:555 | Structural completion/agent evidence before recovered auth errors; only failure events/stderr enter patterns. |
| subfleet/daemon.py:1174 | Transactional guessed-limit release, note/ids event, refusal and mixed-clock behavior. |
| subfleet/cli.py:2083 | Flags, required note, older-daemon guard, released/kept counts; help at 3135. |
| subfleet/protocol.py:281 | Operator note for release-limits. |
| docs/acceptance-contract.md:9 | Dated incident Changes line; C-9.2/3/4/6/8 and C-12.6 clarified at 259–265 and 300. |
| tests/unit/test_limit_from_model_text.py:55 | Incident regressions, regex-generated properties, raw fixture differential, clock and success-order tests. |
| tests/unit/test_claude_adapter.py:287 | Structural empty-result expectation; deliverable still absent. |
| tests/unit/test_codex_adapter.py:444 | Completed empty turn has structural success; deliverable still absent. |
| tests/unit/test_cli.py:150 | Release validation, routing, older-daemon and help tests; deterministic process inputs in two existing tests. |
| tests/fake/test_lanes_enroll.py:128 | Selective guessed release, reported refusals, no-open states, notes, lane validation and event checks. |
| tests/fake/test_state_contract.py:338 | Stub process table/liveness in the existing missing-receipt check. |
| tests/fixtures/claude/make_fixtures.py:971 | Generate structural empty-result expectation. |
| tests/fixtures/claude/empty-result-rc0/expected.json:10 | Update existing fixture expectation/provenance, not captured evidence. |
| docs/reports/2026-10-11-limit-from-model-text.md:1 | This report. |

Runs use the prescribed TMPDIR and `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q`, with the workspace-local UV_CACHE_DIR and `--hypothesis-show-statistics` on the two larger fixed-code runs. The 12 files are test_limit_from_model_text, test_claude_adapter, test_claude_stream, test_claude_synthetic_error, test_claude_large_stream, test_codex_adapter, test_cli and test_lane_answers under tests/unit, plus test_lanes_enroll, test_notice_header, test_lane_fault and test_state_contract under tests/fake. TMPDIR cleanup uses the prescribed Python shutil command. Lock holds were 12.61 s plus package setup for the baseline and 256.24 s for the first full run; the second hold was 1330.71 s (the interrupt took effect after the approximately twenty-minute boundary); the final focused hold was 137.54 s. Syntax compilation and git diff --check passed. The local replay directory and task caches were removed with Python shutil; .git-local and the installed .venv are retained.

Commits are on fix/limit-from-model-text in workspace-local .git-local, based on 4c6a8a9c989e, with the required Co-Authored-By trailer. The shared detached checkout and caller branch are preserved. No logs or captured JSON evidence are committed; the existing expected.json is test metadata. Unrun: the rest of the repository suite, an uninterrupted final 810-case rerun, and live-provider/account operations. The process-check base replay was prepared but not run; those tests instead use isolated inputs and passed in the fixed-code replays.


## PR #186 fix round 1

Starting head: `a2c155ed72b9`; requested comparison base: `be7afd599` (`release/217`). CI on Python 3.12 and 3.14 found two regressions outside the build lane's selected files: `test_claude_attest.py::test_an_empty_deliverable_is_none` and `test_notice_job_state.py::test_c9_2_c15_1_a_codex_job_cancelled_while_running_is_announced_cancelled`. Both originated in `427daf6a41a5`: Claude's final-text emptiness check was removed, and Codex's deliverable check was broadened to empty completion/agent frames with a changed success detail. The reason was the new unconditional model-text erasure property, whose treatment of acceptance conflicts with the earlier C-12.6 requirement. The model's phrases must never be provider failure signatures, but a deliverable must still contain non-whitespace text.

Fix plan: reproduce the supplied head and exact requested base, restore deliverable acceptance and Codex's original success detail, pin the neighboring rc-0 cases in tables, and reconcile C-9.2/C-12.6 plus property tests around the distinction between failure evidence and deliverable presence. Preserve CLI/provider-only failure signatures and clocks, recovered-success ordering, structured rejection precedence, and guessed-closure release. Test results are pending. Commits use workspace-local `.git-local` because the shared checkout metadata is outside the writable workspace; the assigned local branch is `fix/limit-from-model-text`. No network git remotes or sub-agents are used.


The correction restores Claude's final-text check and its original empty-result detail, and restores Codex's deliverable-only acceptance, admission evidence and success detail. Claude's text-error suppression now requires verified success with nonblank final text. Empty completion/agent frames in Codex are no longer sufficient. Real provider diagnostics, structured verdicts, transcript preference, damaged-tail handling and existing fallback selection are pinned by the new table. The original CI tests are unchanged. The existing empty-result expected metadata and generator are restored from `be7afd599`.

C-9.2/C-9.8/C-12.6 now explicitly separate deliverable presence from model wording. Unconditional class/closure invariance under literal erasure cannot coexist with C-12.6: deleting a nonblank answer must be allowed to remove acceptance and reveal genuine provider failure evidence. The four original generated properties retain their 150-example settings, with mutation and neutralization preserving nonblank presence; two additional 150-example properties test literal erasure for Claude and Codex and forbid model-only failure/closure creation. Exact empty-result outcomes are independently pinned against the base in the table. Recovered-success precedence over real text diagnostics is retained only for a verified nonblank deliverable, as the original task requires. Structured Claude rejection and terminal Codex failure remain authoritative.

Test execution is pending the shared suite lock. The implementation is committed from a separate workspace-local index while the working files used by the queued starting-head run remain unchanged. After that run, the committed implementation will be materialized into the working files, and the base and corrected runs will follow under separate lock holds.

The neighbor review also pins an existing representation distinction: Codex tests captured bytes with the byte whitespace check, while Claude tests Unicode text. Nonbreaking-space and thin-space UTF-8 payloads remain accepted by Codex as before, without payload normalization; four table cases cover both `last.md` and agent-message fallback. C-12.6 records this existing behavior, and the Codex generated checks use the byte deliverable criterion. Two corresponding Claude cases pin Unicode whitespace rejection. The table now contains 334 cases. This preserves the pre-branch artifact boundary.


First round-1 run on the unchanged starting-head classifiers: **256 failed, 447 passed, 1 skipped in 591.37 s**. Per file: Claude attestation 18 passed/1 failed (the exact CI assertion); Claude adapter 170 passed; Codex adapter 90 passed; original model-text tests 90 passed (600 generated examples); new boundary table 79 passed/255 failed; notice e2e 1 skipped. Of the table failures, 86 were a new helper error: the required `make_launch(model_id=...)` argument was omitted. The helper is corrected in `566d6127710c`, and the entire table is queued against an immutable `a2c155ed72b9` replay together with the branch's release tests. These preliminary table failures are not all classifier regressions. The unmodified e2e case skipped under the sandbox's process-inspection restriction; no passing e2e execution is claimed. The original CI log establishes the receipt-detail mismatch, and the unit table's 16 successful-deliverable cases independently reproduce it.

The committed correction is now materialized in the working files. Corrected base and post-fix runtime results remain pending; no original CI test was edited. All pytest invocations are foreground, serial and separately locked. The first completed pytest lock hold was approximately 10 minutes; time spent in the queue was outside the hold.

Corrected starting-head table/release replay: **204 failed, 149 passed, 181 deselected in 13.64 s**. The table itself is **130 passed/204 failed**, all expected classifier/detail regressions; CLI release/routing selection **8 passed** and daemon release selection **11 passed**. There are no helper errors in this corrected run. Together with the preceding unchanged-file run this supplies the final before counts. Exact `be7afd599` comparison is now queued.

Exact `be7afd599` base comparison: **613 passed, 1 skipped in 24.04 s**. Claude attestation **19 passed**, Claude adapter **170 passed**, Codex adapter **90 passed**, boundary table **334 passed**, notice e2e **1 skipped**. Thus all table expectations are verified against the requested base. The unchanged notice test skips with `C-5.3 e2e requires permitted ps/sysctl inspection: macOS boot identity is unavailable`; it could not be reproduced or confirmed passing in this sandbox. Post-fix tests and all new release selections are now queued together under a separate hold.
