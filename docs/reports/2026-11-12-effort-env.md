# D-EFFORT-ENV1

Implemented named-effort pins for Claude jobs and conversation turns. Focused tests pass and all seven mutants are killed. Three broader failures reproduce with the starting production code. Commits are blocked by the sandbox's parent-repository `index.lock` permissions; all changes remain in this assigned checkout.

Started **2026-11-12 05:51:27 UTC**, from `date -u` in the first command. Finished **2026-11-12 06:36:43 UTC**, within the three-hour limit. Evidence, exact test commands, exit codes and logs are in `docs/reports/2026-11-12-effort-env-data/`; `command-ledger.md`, `fixture-commands.md` and `test-summary.json` index them.

Step 0 passed before source edits. `ClaudeAdapter` defaults to `claude`; the daemon's registry constructs it with that default. `command -v claude` returned `/Users/maxghenis/.local/bin/claude`; `--version` returned **2.1.318 (Claude Code)**, rather than the brief's previously observed 2.1.321.

Each probe used an isolated HOME/config, user `settings.json`, `env -u` for inherited CLAUDE_CODE_* and ANTHROPIC_* variables, and `-p --input-format stream-json --output-format stream-json --verbose --effort high`. Only a `get_settings` control request was sent. No user message, login, credential access or model API call. All four children exited 0 without stderr.

| User env effort | CLI settings env effort | Child env effort | Applied |
| --- | --- | --- | --- |
| low | absent | absent | low |
| low | medium | absent | medium |
| absent | absent | absent | high |
| low | medium | max | medium |

The command-line settings value wins. The four raw case JSON files and `step0-probe.py` retain exact commands and replies.

Changes and locations:

- `subfleet/adapters/claude.py:343–362,1442`: optional effort is merged centrally into the existing settings env; both sandboxes receive it. `LANE_SETTINGS_ENV`, other settings/env keys, publish hooks and process environment additions retain their previous values. No-effort calls retain their argv.
- `subfleet/conversations/claude_turn.py:128–157`: one canonical effort supplies both `--effort` and settings. Ultracode maps to `xhigh`, including read-only turns; existing `ultracode`, `fastMode` and `disableAllHooks: false` behavior remains.
- `reconstruct_v1_argv`, `claude.py:2124–2171`: optional effort and the fifth documented v2 difference. Twelve offline parity cases cover None/five named efforts and both sandboxes.
- `docs/acceptance-contract.md:574,780`: C-12.4 and C-26.8 amended by hand; no other clauses renumbered. The invariants-index tests passed in the guard run.
- Tests/fixtures: new `test_effort_env.py`; extended `lane_settings_cases.py`, `test_lane_settings.py` and pinned argv; updated exact named-effort expectations in `test_claude_turn.py`, `test_claude_conversation_mcp_scope.py`, and `test_ultracode_effort.py`.
- `test_claude_v1_parity.py`: source parity now requires explicit `SUBFLEET_V1_RUNNER`, avoiding its former automatic access to the forbidden host v1 tree. Offline parity remains covered.

Pinned argv: **all 237 original entries remain byte-identical**, including 129 unnamed and 108 named cases. Removing only the added lines recovers the entire original fixture. Added 70 medium-effort cases: 36 jobs, 2 gates and 32 turns; total **307**. All were regenerated from git object `676906f854f5`. The historical differential removes the watchdog and effort pins; the new properties independently rebuild b51's whole argv with its pre-existing watchdog. Raw original/generated records and `fixture-audit.json` are retained. The policy file is untouched.

Three new Hypothesis tests in `tests/unit/test_effort_env.py`:

- **J:** `test_property_job_effort_pins_settings_and_preserves_argv`.
- **T:** `test_property_turn_effort_pins_settings_and_preserves_argv`.
- **M:** `test_property_settings_merge_preserves_other_values`.

They generate efforts including None and ultracode for turns, both sandboxes, all permissions, valid isolated reviews, arbitrary settings/env dictionaries, MCP, Fast, resume and private scratch. Final statistics: 120 passing job examples, 120 turn examples, 180 settings examples, plus explicit edge cases.

| Invariant | Test |
| --- | --- |
| 1. Named means pinned: env and `--effort` agree, ultracode uses xhigh | J, T |
| 2. Unnamed means unchanged: complete argv equals pre-change bytes | J, T, explicit None cases |
| 3. Nothing else moves: settings, env, watchdog, hooks and process additions match | J, T, M |
| 4. One flag: exactly one `--settings` | J, T, M |
| 5. The premise | `test_real_claude_settings_effort_precedence`, four cases, skipped unless `SUBFLEET_REAL_CLAUDE=1` |

Every mutant was applied alone, tested, restored from a pristine copy and byte-checked. `mutants.json` records restoration SHA-256 values, independently matched against baseline restoration and final source bytes.

| Mutant | Killing test | Result |
| --- | --- | --- |
| 1. Drop job variable | J | killed |
| 2. Drop turn variable | T | killed |
| 3. Put job pin in `env_add` | J | killed |
| 4. Use literal ultracode | T | killed |
| 5. Pin unnamed job | J | killed |
| 6. Add second settings flag | J | killed |
| 7. Lose watchdog key | J | killed |

Test commands used `uv run --no-sync --with 'pytest-xdist>=3.6'`, fresh basetemps and at most four workers. Exact commands/file lists and exits are in each labeled JSON/log pair.

| Run / command arguments | Result |
| --- | --- |
| Initial `test_effort_env.py -q` | 2 passed, 1 failed, 4 skipped; corrected a test assumption about the existing trailing scratch `--add-dir` |
| Same file after correction | 3 passed, 4 skipped |
| `SUBFLEET_REAL_CLAUDE=1 SUBFLEET_CLAUDE_BIN=/Users/maxghenis/.local/bin/claude`, real cases | 4 passed, 3 deselected |
| `test_claude_adapter.py -q` | 184 passed |
| `test_lane_settings.py -q` | 1,115 passed |
| All 75 symbol-matching files, one `-n 4` run | 4,002 passed, 97 failed, 69 skipped, 3 xfailed |
| Required state-contract/module-name/invariants-index/handler-refusal files | 425 passed |
| Affected session/importer/legacy-hold files and archived fixture check after harness correction, `-n 4` | 227 passed, 14 skipped |
| Three residual tests with starting production bytes | Same 3 failures |
| Seven isolated mutant commands | Each produced its expected named failure; 7/7 killed |
| Restored `test_effort_env.py -q --hypothesis-show-statistics` | 3 passed, 4 skipped; 420 passing generated examples |
| Exact-compare grep and `git diff --check` | 355 comparison lines audited; diff check passed |

The full selected run is not green. Its `/tmp` workdir refusals and legacy-import exclusions were caused by the initial scratch location. The process-safety bootstrap also eagerly imported the current package before the archived-fixture check selected its old package. Correcting the scratch basetemp and making the bootstrap lazy resolved the affected recheck. The three remaining failures are `test_two_legacy_alias_holders_oldest_exports_and_releases`, `test_codex_exec_and_resume_commands_see_neither_dummy`, and `test_codex_conversation_turn_commands_see_neither_dummy`. All reproduce on b51's production bytes; the Codex control reports `sandbox-exec: sandbox_apply: Operation not permitted`.

Commands/setup/deviations:

- The requested `uv sync --group dev --extra api` exited 2: this commit has no api extra. `uv sync --group dev` succeeded, installing nine packages with the existing CPython 3.14.7+freethreaded interpreter.
- Creating the default DARWIN temp-root child was denied. Initial cache/config/venv scratch was `/private/tmp/sf-eff-b1`. The affected recheck used `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261111-122612-eff-build-r1s-a1-d5eqgqp5/sf-eff-b1`. Every cache and test temp was contained in these roots, except the generated worktree folders listed below.
- Report-only test helpers redirect hardcoded `/tmp` allocations and failed-test retention into scratch, scope process enumeration to this runner's children, and temporarily bootstrap filtered-environment guardians. Temporary bootstraps were removed after each run. Swift nested sandboxing was disabled using the test harness's supported environment option during the broad run.
- The real CLI regression briefly overlapped adapter-check startup; the newly added test modules otherwise ran separately before the broad selection.
- Six external v1-source tests skipped rather than accessing live state. Other process-inspection/opt-in skips remain visible in logs. The whole CI suite was deliberately not run. No live-state command, login, push or history rewrite occurred.
- Early discovery commands had several harmless missing-path/option-order errors. One ledger-writing heredoc hit a false-positive command hook; apply_patch completed that write safely. Command ledgers record these diagnostics.
- The assigned branch already belongs to the caller's worktree; both branch-switch attempts refused. This checkout remains detached. All four coherent-step/final commit attempts were denied by the parent Git worktree's `index.lock`; only named files were offered for staging.

Cleanup: both scratch roots, their config/cache/venv/pristine copies, and generated worktree caches were removed after durable evidence was copied. Generated folders outside scratch were `build/`, `docs/reports/2026-11-12-effort-env-data/__pycache__/`, and `subfleet/{__pycache__,adapters/__pycache__,conversations/__pycache__,sessions/__pycache__}/`, all under this assigned workspace. They were moved into scratch before guarded `shutil.rmtree` calls asserting `sf-eff-b1` in each removal path. `cleanup.json` records absolute paths, time, zero remaining bootstraps and final source hashes. All probe/test commands exited.

Head built (working-tree changes; commits denied): b51f08f961e1b1dcfb93347eb080f9ddd9a9260d
