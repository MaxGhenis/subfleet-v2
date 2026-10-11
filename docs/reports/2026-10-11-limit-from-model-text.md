# Model text and lane limits — 2026-10-11

Claude reports now cannot supply classifier signatures or closure clocks. Codex agent/final-answer text is excluded from failure patterns; structured completion precedes recovered authentication diagnostics. `lanes release <lane> --limits --note <text>` releases active guessed provider-limit/credits closures and records the note and closure ids. Reported clocks are preserved; reported-only and no-open-closure requests are refused. Mixed clocks release only guessed closures. A separate daemon action protects operator holds when talking to an older daemon.

Incident: seven rc-0 Claude Opus 5.5 attempts between 12:30Z and 13:23Z quoted usage-limit language, failing three jobs and closing five accounts for an hour. The three regression reports are minimal reconstructions of the two US-hub triage jobs and the app inventory, rather than copied live artifacts.

C-9.8 decision: a structured `rate_limit_event status=rejected` remains authoritative and can override rc-0 non-error success. `overageStatus` remains diagnostic. Text cannot override success. To satisfy exact model-text erasure, Claude success and Codex completion/agent-frame success are structural; the daemon still rejects empty deliverables under C-12.6 and retains `provider_verdict`.

| Regression | Before, base 4c6a8a9c989e | After, first fixed run |
| --- | --- | --- |
| triage-first: “hit its usage limit” | limited | ok, no closure |
| triage-second: “hit its usage limit” | limited | ok, no closure |
| inventory: “Continues elsewhere when an account hits its usage limit” | limited | ok, no closure |

The unchanged base source plus the initial regression file at b917b218a ran in a local replay directory: **3 failed in 12.61 s**, all for the expected limited-versus-ok mismatch. First fixed run: **804 passed, 6 failed, 3 warnings in 256.24 s**, over 12 files and 810 tests. Three new CLI tests used non-callable fake handlers; these were corrected. Three existing process checks depended on unavailable host inspection; their absent-process, changed-start-time and dead-guardian inputs are now explicitly stubbed. Production process handling is unchanged. Final same-suite rerun: **PENDING**.

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

Runs use the prescribed TMPDIR and `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q`, with the workspace-local UV_CACHE_DIR and `--hypothesis-show-statistics` on fixed-code runs. The 12 files are test_limit_from_model_text, test_claude_adapter, test_claude_stream, test_claude_synthetic_error, test_claude_large_stream, test_codex_adapter, test_cli and test_lane_answers under tests/unit, plus test_lanes_enroll, test_notice_header, test_lane_fault and test_state_contract under tests/fake. TMPDIR cleanup uses the prescribed Python shutil command. Lock holds were 12.61 s plus package setup for the baseline and 256.24 s for the first full run; the final hold is pending. Syntax compilation and git diff --check passed.

Commits are on fix/limit-from-model-text in workspace-local .git-local, based on 4c6a8a9c989e, with the required Co-Authored-By trailer. The shared detached checkout and caller branch are preserved. No logs or captured JSON evidence are committed; the existing expected.json is test metadata. Unrun: the rest of the repository suite and live-provider/account operations. The process-check base replay was prepared but not run; those tests instead use isolated inputs in the final suite.
