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

Starting head: `a2c155ed72b9`; requested base: `be7afd599` (`release/217`). Both CI failures originate in `427daf6a41a5`, which made adapter success structural to satisfy unconditional model-text erasure. That broadened acceptance beyond the task's provider-evidence boundary.

- Claude: removing `_final_text(...).strip()` made a whitespace-only rc-0 result `ok`, despite no deliverable. The original `test_claude_attest.py::test_an_empty_deliverable_is_none` reproduces this exactly. The final-text check and original empty-result detail are restored, with rate readings retained. Text-error suppression requires verified nonblank success.
- Codex: accepting completion/agent frames without a deliverable also changed the success detail to `Codex completed with exit 0` and changed admission evidence. Deliverable-only success, `deliverable with exit 0` evidence and `Codex completed with a deliverable` are restored. Six agent-fallback and ten captured-file cases independently reproduce the original detail mismatch and pass on the base and fix. The original cancellation/notice e2e test is unchanged.

C-9.2/C-9.8/C-12.6 distinguish deliverable nonemptiness from its wording. Literal erasure can remove acceptance; genuine CLI/provider diagnostics can then decide a failed attempt. Model words never supply failure signatures or closure clocks. Verified nonblank success still supersedes recovered text errors; structured Claude admission rejection and terminal Codex failure remain authoritative. The four original 150-example generated checks retain their size and now preserve deliverable presence during wording changes/neutralization. Two additional 150-example checks pin literal erasure. This corrects the unconditional-erasure contract that caused the regressions, while retaining coverage of every classifier/clock regex and arbitrary Unicode.

The 334-case table pins all seven outcome classes across rc-0 missing, empty, whitespace and malformed results; provider diagnostics; structured refusal/rejection; damaged tails; transcript preference; last-file/agent fallback; and a final blank agent message after earlier progress. Its expectations all pass against exact `be7afd599`. Existing representation checks remain unchanged: Claude rejects Unicode whitespace; Codex tests captured bytes with ASCII whitespace, preserving nonbreaking-space/thin-space UTF-8 payloads as before. Six explicit cases pin this distinction, which C-12.6 now records. The existing empty-result fixture expectation and generator are restored from the base; captured provider artifacts are untouched.

Counts below use the complete files for the five unit classifier files and the notice e2e file. CLI/daemon rows use the branch's new release cases plus the extended routing test (`-k 'release_limits or lanes_actions_reach'`). P = passed, F = failed, S = skipped.

| File | Starting head | Requested base | Fixed |
| --- | ---: | ---: | ---: |
| tests/unit/test_claude_attest.py | 18 P / 1 F | 19 P | 19 P |
| tests/unit/test_claude_adapter.py | 170 P | 170 P | 170 P |
| tests/unit/test_codex_adapter.py | 90 P | 90 P | 90 P |
| tests/unit/test_limit_from_model_text.py | 90 P | absent | 92 P |
| tests/unit/test_empty_provider_results.py | 130 P / 204 F | 334 P | 334 P |
| tests/e2e/test_notice_job_state.py | 1 S | 1 S | 1 S |
| tests/unit/test_cli.py (release/routing) | 8 P | absent | 8 P |
| tests/fake/test_lanes_enroll.py (new release cases) | 11 P | absent | 11 P |

Run evidence: initial unchanged-head batch **256 failed, 447 passed, 1 skipped in 591.37 s** included 86 new-helper errors from an omitted required `model_id`. That mistake is corrected in `566d6127710c`; those preliminary table counts are superseded by the entire corrected table replay. Corrected starting-head table/release replay: **204 failed, 149 passed, 181 deselected in 13.64 s**, with no helper errors. Exact base: **613 passed, 1 skipped in 24.04 s**. Final fixed run: **724 passed, 1 skipped, 181 deselected in 45.96 s**. Every expected table failure is gone. Six classifier properties passed **900 examples**, zero failures, with 89 invalid generation attempts; the existing two Codex properties passed another 200 examples. All three incident regressions and all 20 Claude fixture differentials passed.

The notice test skips unchanged on all three heads with `C-5.3 e2e requires permitted ps/sysctl inspection: macOS boot identity is unavailable`. It was attempted, but neither its original failure nor a passing end-to-end execution can be claimed in this sandbox. The supplied CI log and the sixteen unit detail cases establish the Codex cause and correction. No original CI test was weakened, deleted or mocked around the host restriction.

All runs used CPython **3.14.7 with free threading**, the task-local UV cache and the prescribed TMPDIR prefix. One foreground pytest process ran at a time, each under its own `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock` hold. A shell banner immediately after acquisition measured the holds; the longest was approximately ten minutes, with queue waits outside the hold. TMPDIR cleanup used Python `shutil.rmtree`, and no detached runner is retained. Syntax parsing and `git diff --check` passed. Commits are on `fix/limit-from-model-text` in workspace-local `.git-local`, because shared checkout metadata is outside the writable workspace; all carry the required Co-Authored-By trailer. Caller changes and shared Git metadata are preserved. No network Git remotes, sub-agents, live accounts, logs, captured JSON evidence or bundles were used/committed. The changed expected.json is existing fixture expectation metadata.

Unrun: actual execution of the skipped cancellation/notice e2e case, Python 3.12, the rest of the repository suite (including CLI/enrollment cases outside the named selections), and live-provider operations. Task-owned replay directories and the UV cache are removed with Python shutil; `.git-local` and the installed workspace `.venv` are retained.
