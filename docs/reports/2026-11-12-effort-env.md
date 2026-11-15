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

## Fix r1

Review r1 of #310 (`~/reviews/subfleet-hub/effort-env-2026-11-10/review-r1.md`) found the fix working and raised three P2s, the resume path and three P3s. This round fixes all of them in a detached checkout of 120f606ed. It started at **2026-11-15 01:42:42 UTC** (`date -u`, first command) and wrote this section at 03:21 UTC, inside the three-hour limit. Commits: `e6ddae8d1` (code and tests), `a7dab1a75` (contract), then this report. Evidence is in `docs/reports/2026-11-12-effort-env-data/fix-r1/`.

### Findings and what changed

1. **P2, v1 source parity was off by default.** `tests/unit/test_claude_v1_parity.py:30-36` looks for v1's runner at the base's path again (`~/chief-of-staff/subfleet/bin/subfleet-claude`, found through `pwd`) and runs whenever that file exists. A non-empty `SUBFLEET_V1_RUNNER` overrides the path; a path that does not exist skips the tests. The module docstring is the base's again.
   - New `test_a_named_effort_is_the_fifth_documented_change` (`:147`), 10 cases (both sandboxes, five efforts). It reads v1's source and checks four things:
     - v1's launch line goes straight from `--output-format json` to `PERM_ARGS`;
     - neither `PERM_ARGS` array carries `--effort`;
     - v1 assigns no `CLAUDE_CODE_EFFORT_LEVEL`;
     - `reconstruct_v1_argv(effort=E)` is the effort-free line plus `--effort E` right after `--verbose` and the pin in the settings env, and its permission flags are still v1's arrays.
   - New `test_v1_source_parity_runs_by_default_where_the_v1_runner_exists` (`tests/unit/test_effort_env.py:294`). It loads the parity module under a stand-in home (patched `pwd.getpwuid`, scratch files only) and checks four cases: with no variable it skips when the runner is absent and runs when it is present; an empty variable counts as unset; and the override wins. It runs in CI and never touches the real v1 tree.
   - **I could not run the parity file against v1**: `~/chief-of-staff` is off limits, so I wrote it against the base's detection. Every run of mine set `SUBFLEET_V1_RUNNER` to a missing scratch path (16 skipped). As a mechanics check only, I ran it against a **synthetic** runner (`fix-r1/synthetic-v1-subfleet-claude.SYNTHETIC`), written from nothing but the patterns the tests parse: 16 passed. That proves the test code runs, not that v2 matches v1.
   - The one assumption in the new test is that v1 assigns no `CLAUDE_CODE_EFFORT_LEVEL`. If it fails on that line, v1 pins effort itself and the fifth documented difference needs rewording.
2. **P2, no change-log entry.** `docs/acceptance-contract.md:9` adds one line for C-12.4 and C-26.8 at the top of the list (newest first), with no blank line inside the list.
3. **P2, ultracode against the real CLI.** New opt-in `test_real_claude_ultracode_turn_keeps_ultracode_with_the_effort_pin` (`tests/unit/test_effort_env.py:415`), 4 cases. It builds the turn argv with `claude_turn.argv` itself on `claude-opus-5-5` (`SUBFLEET_REAL_CLAUDE_OPUS` can name another model) and asks only `get_settings`.
   - The premise test now shares the helper `_real_applied` (`:350`). Its command, environment and assertions are unchanged.
   - The variable does not break ultracode, so no code fix was needed. Results are under "Real-CLI result" below.
4. **The resume path (r1's first "outside this PR" point).**
   - `ClaudeAdapter.resume_launch` (`subfleet/adapters/claude.py:1465-1499`) takes `effort`. It adds `--effort` right after `--verbose` (`:1491`), the same place a fresh launch puts it, and pins `lane_settings(effort=effort)` in the one `--settings` (`:1499`).
   - The daemon passes `effort=model.get("effort")`, the same value `build_launch` gets (`subfleet/daemon.py:9797-9801`).
   - The base signature gains the keyword with a docstring (`subfleet/adapters/base.py:120-133`). Codex accepts and ignores it (`subfleet/adapters/codex.py:626-632`), so Codex launches are unchanged. `tests/fake_adapter.py:125-139` accepts it and passes it through.
   - Tests:
     - `test_property_resume_effort_pins_settings_and_preserves_argv` (`test_effort_env.py:196`): 120 generated examples plus 2 explicit ones over every resume shape and effort, checked against the 676906f85 recording with only the watchdog restored;
     - review r1's probe P2 as `test_resumed_attempt_pins_the_effort_its_fresh_launch_pins` (`:208`): both sandboxes and all three tasks, where the fresh launch and its resume carry the same `--effort medium` and byte-identical `--settings`;
     - `test_c12_resume_invokes_native_launch_on_original_lane` (`tests/fake/test_resume_contract.py:34`), now run with no effort and with `medium`: the daemon hands the model entry's effort to `resume_launch` for both providers.
   - C-12.4 (`:575`) now covers resumes and revives.
5. **P3s.**
   - **C-12.4 (`:575`).** The base's watchdog sentence, with its October 22 peer-probe clause, is restored word for word, so the clause again follows the watchdog pin. The effort sentences come after it: the pin covers resumes, and for the effort variable both conflicts are verified (user, project and local settings, and the process environment: the build's case 4 and r1's K2). Managed settings were not present to test.
   - **C-26.8 (`:781`).** A turn that runs at an effort pins it, whether the effort is the person's or the policy default `service.py:2962-2965` writes into the turn. Only a turn left with no effort is unchanged. The clause also records the ultracode result.
   - **MH.** `test_an_empty_effort_pins_nothing_on_any_path` (`test_effort_env.py:260`): `lane_settings(effort="")` adds no variable, and a job (both sandboxes), a resume (both sandboxes) and a turn (all four permissions) naming `""` build the argv they build for None.
6. **Not this round.** I did not change `launch.json` (r1's second "outside" point).

### Real-CLI result (Claude Code 2.1.318, `/Users/maxghenis/.local/bin/claude`)

All isolated cases used the argv `claude_turn.argv` builds for a writable (bypass) ultracode turn on `claude-opus-5-5`: `--effort xhigh` and `{"disableAllHooks":false,"env":{…},"ultracode":true}`. Each had a fresh HOME and config folder, every `CLAUDE_*` and `ANTHROPIC_*` variable removed, no credential and no message; only `get_settings` was asked.

| Case | Settings env | User settings | `applied` |
| --- | --- | --- | --- |
| u0, the base's turn | watchdog only | none | ultracode true, effort xhigh |
| u1, this head's turn | watchdog, pin xhigh | none | ultracode true, effort xhigh |
| u2 | watchdog, pin xhigh | effort low | ultracode true, effort xhigh |
| u3, control | watchdog, pin high | none | ultracode true, effort **high** |
| u4, control, the base's turn | watchdog only | effort low | ultracode true, effort **low** |

Every answer also held `ultracodeAvailable: true`, `ultracodeRequested: true`, `model: claude-opus-5-5`, every child exited 0, and stderr was empty.

- **The pin is compatible with ultracode.** `applied.ultracode` stays true with the variable set beside the flag, and the effort is xhigh even under a user `low`.
- **The controls show the probe can tell the difference.** The variable sets the effort that applies (u3). At the base, a user `low` silently ran an ultracode turn at `low` (u4).
- **Side finding for the hub's defects file.** The driver records the served effort as `ultracode` whenever `applied.ultracode` is true (`claude_turn.py:548`), whatever `applied.effort` says. So at the base, the u4 turn would have recorded `ultracode` while running at `low`. The pin makes that record true for pinned turns, but the record still never checks `applied.effort`.
- **The lane run.** I ran the CLI once with my own environment: inherited HOME, no `CLAUDE_CONFIG_DIR`, the session's `CLAUDE_*` and `SUBFLEET_*` markers removed, `--no-session-persistence`, and the tiny prompt "Reply with OK." followed by `get_settings`.
  - `applied` came back ultracode true, effort xhigh, model `claude-opus-5-5`.
  - The turn itself ended `is_error: true`, rc 1.
  - An `initialize`-only call (no message) showed why: `account` held only `apiProvider` and `tokenSource: none`, with no email. This job's environment carries no lane credential (no `CLAUDE_CODE_OAUTH_TOKEN` and no `CLAUDE_CONFIG_DIR`), and the default config has no login a child can use.
  - **So no credentialed result was obtained, and no model request reached the API.** Getting one would mean reading a secret, which the rules forbid. The `applied` answer is the CLI's local resolution and matches the isolated cases.
- The opt-in test run (`SUBFLEET_REAL_CLAUDE=1`) passed all 8 real-CLI cases: the 4 premise cases and the 4 ultracode cases (u1, u2, an `ask` turn under a user `low`, and the u4 control).

### Mutants

Each mutant was applied alone to a `git archive HEAD` copy in scratch, never the worktree, with its killers run from the copy. The file was then restored from a pristine copy; every restored file's SHA-256 equals HEAD's blob (`fix-r1/mutants*.json`). Control: the same killers passed on the unmutated copy (13 passed).

| Mutant | Change | Killed by | Run |
| --- | --- | --- | --- |
| MH | `if effort:` becomes `if effort is not None:` in `lane_settings` (`claude.py:353`) | `test_an_empty_effort_pins_nothing_on_any_path` | 1 failed |
| Resume pin dropped | `resume_launch` passes `lane_settings()` | `test_property_resume_effort_pins_settings_and_preserves_argv`; alone, also `test_resumed_attempt_pins_the_effort_its_fresh_launch_pins[read-only-build]` | 1 failed each |
| Resume flag dropped (pin kept) | no `--effort` on resume | the same two tests | 1 failed each |
| Daemon drops the effort | `resume_launch(…, model["id"])` | `test_c12_resume_invokes_native_launch_on_original_lane[codex-astra-medium]` | 1 failed, 1 passed |
| Parity default back to env-only | 120f606ed's `V1_RUNNER` and skip | `test_v1_source_parity_runs_by_default_where_the_v1_runner_exists` | 1 failed |

The hub verifies the parity-default mutant locally as well (see below).

### Commands and results

Setup:
- `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`; scratch was `$TMPDIR/sf-eff-f1`.
- `UV_CACHE_DIR=$TMPDIR/uv-cache` (shared and already present, so I didn't delete it); `UV_PROJECT_ENVIRONMENT=<scratch>/venv`.
- `uv sync --group dev` installed 9 packages, with load 74.6 at the start and 80.6 at the end. The venv runs CPython 3.15.2.
- Every pytest run went through `fix-r1/pt.sh`, which does five things:
  - sets `PYTHONDONTWRITEBYTECODE=1` and unsets `PYTHONPATH`;
  - adds `-p no:cacheprovider` and a new `--basetemp=<scratch>/pytest-<label>`;
  - wraps the run in `timeout 560`;
  - adds the scratch-venv plugin `sf_eff_f1_safety`, which points `failed_keep.ROOT` into scratch;
  - sets `HYPOTHESIS_STORAGE_DIRECTORY=<scratch>/hypothesis` and `SUBFLEET_V1_RUNNER=<scratch>/no-such-v1-runner` (both added before chunk c1).
- `-n 4` runs used `uv run --no-sync --with 'pytest-xdist>=3.6'`. Load figures are the one-minute averages from `uptime` at each run's start and end. Every run listed finished; none hit the timeout. `fix-r1/runs.json` holds each run's arguments, times, exit code and result line.

| Run | What | Result | Load start → end |
| --- | --- | --- | --- |
| t01 | `tests/unit/test_effort_env.py`, serial (before the v1-default test existed) | 11 passed, 8 skipped, 104 s | 112.5 → 78.0 |
| t02 | same file, `-k real_claude`, `SUBFLEET_REAL_CLAUDE=1 SUBFLEET_CLAUDE_BIN=/Users/maxghenis/.local/bin/claude` | 8 passed, 11 deselected, 88 s | 67.7 → 67.5 |
| t03 | `test_claude_v1_parity.py`, override to a missing path | 16 skipped | 72.4 → 83.4 |
| t04 | `test_claude_v1_parity.py`, override to the synthetic runner | 16 passed | 83.4 → 94.4 |
| t05 | `test_effort_env.py -k "v1_source or empty_effort"` | 2 passed, 18 deselected | 77.4 → 93.3 |
| t06 | `tests/fake/test_resume_contract.py` | 25 passed, 72 s | 93.3 → 80.4 |
| t07 | contract guards, `-n 4`: `test_invariants_index.py`, `test_protocol_registry.py`, `test_desktop_ledger.py`, `test_retention_turn_contract.py`, `test_test_module_names.py` and the four contract-quoting tests in `test_live_store_guard_round8/9/10.py` | 40 passed, 38 s | 101.5 → 91.6 |
| probes | ultracode u0 to u2 / u3 and u4 / lane run | as above | 74.8 → 78.2 / 73.7 → 86.0 / 94.0 → 112.3 |
| m01 | mutant killers on the unmutated copy | 13 passed | 141.9 → 110.5 |
| mutants | MH, the resume pin, the daemon effort, the resume flag, the parity default; then the two resume mutants against the P2 test alone | 7 of 7 killed | 99.8 → 142.5; 148.8 → 161.2 |
| c1 to c6b | 76 files (below), 11 chunks, `-n 4` | 4,193 passed, 2 failed, 39 skipped, 3 xfailed | see below |
| t08 | `tests/fake/test_state_contract.py tests/unit/test_handler_refusals.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py`, `-n 4` | 425 passed, 98 s | 180.3 → 184.1 |
| t09 | c1's two failures, serial | 2 passed, 22 s | 186.7 → 184.7 |

The 76 files are every test file naming `lane_settings`, `claude_settings_args`, `reconstruct_v1_argv`, `build_launch`, `resume_launch`, `PERMISSION_FLAGS`, `ULTRACODE` or `effort`. That is the build's 75 plus `tests/unit/test_codex_turn_completed.py`, which calls `resume_launch` (`fix-r1/selected-tests.txt`, chunk lists `fix-r1/chunk-c*.txt`).

| Chunk | Files | Result | Seconds | Load start → end |
| --- | --- | --- | --- | --- |
| c1 | 11 | 518 passed, 2 failed, 8 skipped | 467 | 157.0 → 163.4 |
| c2a | 6 | 1,383 passed | 270 | 171.4 → 173.0 |
| c2b | 6 | 86 passed | 106 | 169.5 → 180.6 |
| c3a | 7 | 615 passed | 452 | 184.2 → 110.3 |
| c3b | 6 | 82 passed, 3 xfailed | 121 | 118.5 → 99.3 |
| c4a | 7 | 393 passed | 142 | 99.3 → 173.0 |
| c4b | 6 | 155 passed | 257 | 175.9 → 176.1 |
| c5a | 7 | 370 passed, 15 skipped | 225 | 184.4 → 166.5 |
| c5b | 7 | 64 passed | 161 | 168.3 → 167.8 |
| c6a | 7 | 456 passed | 152 | 166.0 → 167.4 |
| c6b | 6 | 71 passed, 16 skipped (the parity file, overridden) | 50 | 167.1 → 182.5 |

c1's two failures are timing failures under load. Both passed alone in t09.
- `tests/unit/test_cli.py::test_suggest_open_pipe_is_bounded`: 0.85 s against a 0.75 s bound.
- `tests/fake/test_gate_end_to_end.py::test_plan_peer_process_finalizes_through_real_daemon`: `subprocess.TimeoutExpired` on `python -m subfleet gate plan`. Its kept state root went to scratch.

`git diff --check 120f606ed..HEAD` is clean. The contract diff against 120f606ed has three hunks: the change log (line 9), C-12.4 and C-26.8.

### What the hub must run locally

1. **The v1 source parity, on this Mac, with no `SUBFLEET_V1_RUNNER`:** `tests/unit/test_claude_v1_parity.py`. Expect 16 passed (6 old, 10 new). A skip means the default is still off. A failure on the `CLAUDE_CODE_EFFORT_LEVEL` assignment check means v1 pins effort itself, and the fifth documented difference needs rewording.
2. **The parity-default mutant:** put 120f606ed's env-only `V1_RUNNER` and skip back. The same file should then skip all 16 here, and `test_v1_source_parity_runs_by_default_where_the_v1_runner_exists` should fail.
3. **A credentialed ultracode turn, if wanted:** one real conversation turn on an Opus lane with effort `ultracode`. Its `get_settings` answer should report `ultracode: true` with effort `xhigh`. My authenticated run had no credential (see above).
4. **CI at this head, and the full round-2 review.**

### Deviations

- **The real-CLI run.** I couldn't make a credentialed run, for the reason above. I started the CLI twice in lane mode, not once: the turn with the tiny prompt, then an `initialize`-only call (no message) to diagnose the error. Neither reached a model. The lane-mode CLI used the default config folder, which it may update with its own state; I neither read nor inspected it. I didn't record the load for the `initialize` call.
- **Tests ran in 11 chunks of `-n 4`, not one run,** because each tool call is capped at 10 minutes and nothing may run in the background. t01 to t06 ran before the safety plugin, the Hypothesis directory and the `SUBFLEET_V1_RUNNER` override were in `pt.sh`. None of those runs collected the parity file without an explicit override: t03 and t04 set one, and the v1-default test patches `pwd`.
- **Generated folders.** The probes ran `python3 -I`, which ignores `PYTHONDONTWRITEBYTECODE`, and test subprocesses dropped it. So `__pycache__/`, `.hypothesis/` and `build/` appeared in the worktree. The repo's frontend fixtures (`tests/frontend/test_core_outbox.py:28`, `test_core_protocol.py:43`) call `mkdtemp(dir="/tmp")` and never remove the folder. That left 12 `sf-ob-*` folders, inside my c2a window and one per fixture user, and 4 `sf-app-h-*` folders, inside my c4b window and one per `harness` user. That leak is the tests' own and I didn't fix it here.
- **Cleanup.** Each folder below was moved into scratch and removed with `shutil.rmtree` after asserting `sf-eff-f1` is in its path (`fix-r1/removed-outside.json`):
  - in this worktree: `.hypothesis`, `build`, `subfleet/__pycache__`, `subfleet/adapters/__pycache__`, `subfleet/conversations/__pycache__`, `subfleet/gate/__pycache__`, `subfleet/sessions/__pycache__`, `tests/__pycache__` and `tests/fake/__pycache__`;
  - in `/private/tmp`: `sf-ob-20ot5ye5`, `sf-ob-2_hzdhc6`, `sf-ob-9_cq55ah`, `sf-ob-9tud62y4`, `sf-ob-cp4auiqr`, `sf-ob-dwn0zldf`, `sf-ob-igy_6kz3`, `sf-ob-n69quh91`, `sf-ob-wep8b4la`, `sf-ob-xol4io1s`, `sf-ob-xorp7xzs`, `sf-ob-zww9mm0q`, `sf-app-h-82zaj5sy`, `sf-app-h-jjchlcyr`, `sf-app-h-qlv0o16p` and `sf-app-h-r__hgevj`.
  - I left two `sf-test-homes-*` folders in the user temp root (22:13 local) because I can't attribute them. Every run of mine exited normally, so conftest's own cleanup ran for mine.
  - The scratch folder `$TMPDIR/sf-eff-f1` (its venv, mutant copy, logs and probe folders) is removed the same way after this commit; the durable evidence is in `fix-r1/`.
- **Not changed, for the hub's defects file:**
  - Codex's `resume_launch` passes no effort either: it calls `_launch(…, None, None, thread)`. I haven't checked whether a resumed Codex thread keeps its effort.
  - The ultracode served-effort record (`claude_turn.py:548`) ignores `applied.effort`.
- **No other process.** None of mine outlives the session. I ran nothing against live state; I didn't read `~/chief-of-staff`, `~/.claude` or `~/.subfleet`, or any key. I pushed nothing, and I created, moved or deleted no branch. Commits added named files only.


## Fix round 2

**P1 and C-12.4 are fixed.** The final resume suite passed 29 tests, its three importing test files passed 42, and the four required guard files passed 435. Broader daemon-importer validation remains incomplete and has unresolved failures/errors.

Report finalized **2026-11-15 22:21:46 UTC**, before the 23:00:58 UTC deadline. Started **2026-11-15 20:00:58 UTC**, from `date -u` in the first command, at checkpoint `4fd796b49ae63417e845c93ad8f456978c161f1c`. The required review, report and PR-body reads completed before edits. The workspace's preserved uncommitted regression and evidence were retained; the daemon initially matched the checkpoint. Preserved `before.*` evidence predates this continuation and is not counted below. All round-2 evidence is under `docs/reports/2026-11-12-effort-env-data/fix-r2/`. `command-ledger.md`, `import-audit.md` and the per-run JSON/log pairs record commands, results and counts, including read/setup commands without test counts.

**P1 — a resume can pin another alias's effort.** `_resume_submission` discarded the source attempt's recorded `model_short`, then resolving its provider model ID selected the first matching policy alias. A source at `high` or no effort could therefore resume at `low`. Resume construction now retains the recorded alias, with provider-ID fallback for legacy evidence lacking one. The manifest still binds the provider ID/native session. The alias's current policy entry supplies effort; this does not freeze effort across policy edits.

The new `test_resume_keeps_source_effort_when_policy_aliases_share_a_model_id` has four cases: explicit pin or task-chain routing, each with `high` or absent effort on an alternate alias sharing the first alias's model ID. It checks the resumed alias/session, `--effort` and effort env pin in the single `--settings`, against a fresh Claude launch, without spawning a provider. The exact final test fails all four cases against the original daemon read from the checkpoint's Git object, then passes with the fix. `baseline-restoration.json` verifies byte-for-byte restoration after that baseline run.

The fix also preserves already accepted resume retries. Keeping the alias initially changed their payload digests; the expanded regression failed all four cases against that intermediate fix. Resume digest construction now retains the old provider-ID normalization while routing retains the source alias. The test installs the older accepted digest and replays the identical request, requiring the same job and `created: false`. Existing legacy-evidence and source-identity tests also pass.

**C-12.4 — corrected.** The clause and its existing change-log line now describe recorded-alias retention, legacy ID fallback and effort from the current model entry; the change log also records accepted-retry compatibility. C-26.8 is unchanged. No round-2 finding is left for the hub to file.

Every pytest command uses `uv run --no-sync --with 'pytest-xdist>=3.6'` through `fix-r2/run.py`, a new basetemp containing `pytest`, and at most four total workers. Exact expanded argv, timestamps and exit codes are in each named JSON file. Results below overlap and must not be summed as unique coverage.

| Run label / command scope | Result | Exit |
| --- | --- | --- |
| `resumed-before`: initial regression, unchanged daemon | 4 failed, 25 deselected (expected) | 1 |
| `resumed-after`: resume file, alias-only fix | 29 passed | 0 |
| `alias-only-retry`: expanded regression, intermediate fix | 4 failed, 25 deselected (digest compatibility) | 1 |
| `final-regression-before`: exact final test on 4fd796b49 | 4 failed, 25 deselected (wrong effort) | 1 |
| `final-resume`: final resume file | 29 passed | 0 |
| `importers`, `final-importers`: 238-file starts | Interrupted during startup, no test results | 130 each |
| `safe-importers`: 229-file start | Interrupted after 21m22s without test results | 130 |
| `chunk-01`: 46 files | Partial: 190 passed, 5 failed, 4 errors, 5 skipped, 1 xfailed | 130 |
| `chunk-02`: 46 files | Partial: 381 passed, 4 failed, 15 skipped | 130 |
| `chunk-03`: 46 files | Partial: 34 passed, 3 failed, 1 skipped / 1,144 collected | 130 |
| `chunk-04`: 46 files | Early stop: 93 passed, 2 failed / 1,989 collected | 1 |
| `chunk-05`: 45 files | Partial: 334 passed, 1 failed / 2,856 collected | 130 |
| `isolate-catalog`, `isolate-inspection`, `isolate-launch-env` | 1 passed each | 0 each |
| `isolate-timers`: three failed timer cases | 2 passed, 1 failed (socket path too long) | 1 |
| `isolate-socket-short`: shorter pytest basetemp only | 1 failed (fixture uses tempfile directly) | 1 |
| `isolate-socket-temp-short`: shorter tempfile root too | 1 passed | 0 |
| `test-importers`: three direct changed-test importers | 42 passed | 0 |
| `guards`, then `guards-final`: four required guard files | 435 passed each | 0 each |

The `git grep -l` audit selected 238 files: all 235 direct daemon importers plus three embedded-import cases. The three importers of the changed resume test are included and also completed explicitly: `test_admission_priority.py`, `test_census_round3_interleavings.py`, `test_census_round3_leases.py`. Nine host-push files and one retention-archive case were excluded because their fixtures execute prohibited local or stub pushes; exact paths/rationale are in `write-command-exclusions.md`. The initial 238-file starts were stopped before fixture execution. The remaining 229 files were attempted in five batches after repeated startup stalls.

These batches did not complete their selections. Chunks 01/02 had seven worker crashes; their quiet logs do not identify every failure/error, and those outcomes remain unclassified. Chunks 04/05 had three additional worker crashes. All six named failures from the verbose batches pass in isolation: catalog and inspection properties took 100.36s and 122.11s with a 180s timeout (batch timeout: 90s); both timer-cycle variants passed; the socket case passed after shortening the tempfile root; the launch-helper case passed in 17.18s. This does not establish a cause for every earlier failure. `partial-progress.json` and `verbose-progress.json` retain partial counts and exact available nodes. The guards were repeated after the extra importer/isolation checks so they remain the final test stage.

**Setup, cleanup and deviations.** The prescribed `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, scratch/cache locations and `uv sync --group dev` were attempted first. Scratch/cache writes were denied (sync exit 2). Keeping that TMPDIR, setup succeeded with nine packages/CPython 3.15.2 using this writable fallback scratch, venv and cache:

`/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261115-100031-eff-fix-r2-a2-vxzuixz_/sf-eff-fix2`

The safety helper redirects tempfile and retained failures into scratch; the runner isolates Hypothesis, disables bytecode/cacheprovider and uses a missing synthetic v1-runner path. For socket diagnosis only, the extra short scratch root was used. Outside-primary-scratch generated folders were recorded in `cleanup.json` before guarded removal and are listed here:

- `/Users/maxghenis/.subfleet/worktrees/20261115-100031-eff-fix-r2/subfleet/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261115-100031-eff-fix-r2/subfleet/adapters/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261115-100031-eff-fix-r2/subfleet/sessions/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261115-100031-eff-fix-r2/subfleet/conversations/__pycache__`
- `/private/tmp/sf-eff-fix2-201115200058`

`cleanup.py` records paths first, moves outside folders beneath scratch, then uses `shutil.rmtree` guarded by assertions that `sf-eff-fix2` is in each removal path; it also removes the primary scratch. Cleanup completed with exit 0; all five listed folders and the primary scratch are absent. The tracked `private/guard` files were preserved.

The importer exclusions, fallback paths, timeout adjustments, interrupted/incomplete importer runs and extra final guard pass are the validation deviations. Full CI, live CLI/provider probes, excluded push fixtures and the unexecuted remainder of importer selections did not run. The hub's scoped round 3 still needs to resolve the broader validation gap. No live state/process inspection, real credential, forbidden write-shaped command, history rewrite or push was used; all owned test runs ended before cleanup.

All four named-file staging/commit steps (regression, alias/contract fix, retry compatibility and final report) were denied with exit 128 by the parent repository's `index.lock` permissions. No new commit was created; the hub must collect the working-tree changes. Final commit argv/results are in `final-commit.json`/`final-commit.log`. `git diff --check`, final status, HEAD verification and evidence/cleanup assertions all passed (exit 0); results are in `final-checks.json`. Read-only discovery/setup results are in the command ledger.

Head built (working-tree changes; commits denied): `4fd796b49ae63417e845c93ad8f456978c161f1c`
