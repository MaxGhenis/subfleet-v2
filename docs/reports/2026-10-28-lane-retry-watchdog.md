# Claude lanes run with the retry watchdog off (D-WATCHDOG1, d1031)

Base: `release/217` at `676906f85`. A Subfleet build job made this change on 2026-10-28 for Max's d1031 (2026-10-22 08:02, "travel only"). That ruling allows `CLAUDE_CODE_RETRY_WATCHDOG=1` in `~/.claude/settings.json` `env` for a trip, once a check shows that the Subfleet Claude lanes do not inherit it. The 10/22 check found that they did, so the watchdog stayed off for the 10/24–10/28 trip. The next trip is in November.

The lanes must not inherit it. Under the watchdog, 429 and 529 responses retry without end (`~/reviews/codex-vs-claude-network-2026-10-03/REPORT.md`, as d1031 quotes it). A session that reaches its plan's ceiling waits out the window instead of exiting. On a lane, that holds the attempt until the 6-hour wall kills it, so C-4.5's `limited` failover never moves the job on.

## What was open

No Claude lane has a home of its own, so each launch could read the user's `settings.json`. The default launch-environment allowlist does not pass `CLAUDE_CODE_RETRY_WATCHDOG`. A loaded user or lane-home settings file can supply it, and an explicit policy `env.pass` can also supply it in the process environment (review r1).

## Every Claude launch path

`tests/unit/test_lane_settings.py::test_every_claude_p_builder_in_the_package_is_a_case_path` walks the package's AST. The syntax test checks the six known builders for list/tuple literals beginning with a `claude_bin` identifier and `-p`; split argv, `shlex` and differently named helper builders can escape it (review r1, P3-1). Existing paths were also independently audited. Each launch path below goes through one of them.

| Launch path | Builder | `--settings` at 676906f85 | User settings loaded at 676906f85 | Now |
| --- | --- | --- | --- | --- |
| Detached job, writable, build (or no task) | `ClaudeAdapter.build_launch` | none | yes (no `--setting-sources`) | one, `{"env":{…}}`, after the permission flags |
| Detached job, writable review | `build_launch` → `review_publish.claude_settings_args` | one: the publish hook, after `--setting-sources user` | yes (`user`) | the same one, `{"env":{…},"hooks":{…}}`, still after `--setting-sources user` |
| Detached job, read-only (build or review), isolated review | `build_launch` | none | no (`--setting-sources ""`) | one, `{"env":{…}}`, after `--disable-slash-commands` (and the review root's `--add-dir`) |
| Resume, every sandbox and task | `ClaudeAdapter.resume_launch` (→ `claude_settings_args`) | as the job | as the job | as the job |
| Admission probe (C-11.4) | `Daemon._execute_probe` → `build_launch`, read-only | none | no | one, `{"env":{…}}` |
| Keepalive and heal timer turns (C-6.14) | `Daemon._timer_turn` → `_execute_probe` → `build_launch`, read-only | none | no | one, `{"env":{…}}` |
| Gate round (C-23.9) | `gate.round.prepare` → a `gate-review` job (read-only, isolated) → `build_launch` | none | no | one, `{"env":{…}}` |
| Conversation turn, `ask`, `accept-edits`, `bypass` | `conversations.launch.claude_launch` → `claude_turn.argv` | one: `{"disableAllHooks":false[,"fastMode":true][,"ultracode":true]}` | yes | the same one, `"env"` appended last |
| Conversation turn, `read-only` | `claude_turn.argv` | none; with Fast one, `{"fastMode":true}`, from a second site in `argv` | no | one, `{"env":{…}}` or `{"fastMode":true,"env":{…}}` |
| Enrolment (C-10.2) | `ClaudeAdapter._turn_argv` via `enroll` → `_run_turn` | none | yes | one, `{"env":{…}}` |
| Direct probes `probe`, `probe_with_model`, `probe_outcome` | `_turn_argv` | none | yes | one, `{"env":{…}}` |
| Card heal (C-23.47) | `claude_cards.heal_turn` | none | yes (the login folder's, `CLAUDE_CONFIG_DIR`) | one, `{"env":{…}}` |
| v1 reference (not a launch) | `reconstruct_v1_argv` | none | | one, as `build_launch` |

`{"env":{…}}` is `{"env":{"CLAUDE_CODE_RETRY_WATCHDOG":"0"}}`.

Non-turn launches include the card and doctor version checks and the operator's `claude auth status --json`; the watchdog cannot hold a model turn in these helpers. Subfleet performs no provider login (C-23.52), so no login helper starts a turn beyond enrolment and the heal. The timers read a Claude lane's capacity through `probe_status` (the usage endpoint, no turn), and call `probe` only for an adapter without `probe_status`. No other code in the package calls `probe`, `probe_with_model` or `probe_outcome`; they carry the override anyway.

## What the help text says about `--settings`

`docs/reference/claude-help.txt` (last changed in `53a1b68b6`):

- lines 221–222: "`--settings <file-or-json>` Path to a settings JSON file or a JSON string to load additional settings from"
- lines 219–220: "`--setting-sources <sources>` Comma-separated list of setting sources to load (user, project, local)."
- lines 188–196, under `--restricted`: "ignores user, project and local settings files (managed settings and --settings still apply; add --strict-mcp-config to skip MCP servers too)"
- lines 206–216, under `--safe-mode`: "Start with all customizations (CLAUDE.md, skills, plugins, hooks, MCP servers, custom commands and agents, output styles, workflows, custom themes, keybindings, and more) disabled … Admin-managed (policy) settings still apply."

So the help says that `--settings` loads *additional* settings, takes a JSON string or a path, and still applies when Claude Code ignores the user, project and local files. It does not say which value wins when `--settings` and the user's file set the same key. The `claude_turn.py` comment on C-26.11 covers only project and local settings. The only evidence that command-line `env` outranks the user's is the peer session's live probe on 10/22: `--settings '{"env":{"CLAUDE_CODE_RETRY_WATCHDOG":"0"}}'` turned the watchdog off. This job ran no live Claude process and did not repeat that probe. The help also does not say whether `--safe-mode` keeps a `--settings` `env`. Read-only launches drop user settings anyway (`--setting-sources ""`), so for them the override is the rule's uniformity (decision 3), not a fix.

## The decisions as built

1. **One override on every path.** `subfleet/adapters/claude.py` holds `LANE_SETTINGS_ENV = MappingProxyType({"CLAUDE_CODE_RETRY_WATCHDOG": "0"})`, the one place the name and value live. Its comment names d1031 and C-4.5's failover. `lane_settings(settings)` merges it over a payload's `env` and keeps every other key and variable, without changing its input. `lane_settings_args(settings)` gives the flag. The paths take them as follows:
   - `build_launch` and `resume_launch` pass `lane_settings()` to `claude_settings_args`. That covers detached jobs, admission probes, timer turns and gate rounds.
   - `claude_turn.argv` covers every permission.
   - `_turn_argv` covers enrolment and the direct probes.
   - `heal_turn` covers the card heal.
   - `reconstruct_v1_argv` ends the same way.
2. **Exactly one `--settings` per launch.**
   - `claude_settings_args(job, sandbox, attempt_dir, *, settings)` now always returns one `--settings`. For a writable review it merges the publish hook into the launch's settings, after `--setting-sources user` as before. Every other launch gets the lane settings alone; the function used to return `[]` for those.
   - A turn's `disableAllHooks`, `fastMode` and `ultracode` share the one payload with `env`.
   - At 676906f85 a read-only fast turn passed one `--settings`, `{"fastMode":true}`, from a second site in `claude_turn.argv`. Adding the override there as its own flag would have made two. The two sites are now one, so `fastMode` and `env` share one payload. (The fix commit's message says that turn's "two collapse into one"; it had one flag and two sites.)
   - The help text takes a JSON string, and turns have passed JSON in production since C-26.11, so no launch writes a settings file.
3. **Read-only launches get it too.** Their payload holds `env` alone, or `env` and `fastMode` for a fast turn, which it carried before. It names no hook, permission, MCP server, plugin or settings source. The `review_publish.claude_settings_args` docstring says that Claude Code 2.1.284 enables the `--settings` source whatever `--setting-sources` names. C-12.4 records that no Claude run here has exercised that. Either way, a read-only launch gains at most a command-line source holding one variable (two for a fast turn), and none of the files that `--setting-sources ""` removes. I4 tests the flags and the payload's keys. Nothing here called for a stop.
4. **v1 parity.** `reconstruct_v1_argv` ends with `lane_settings_args()`, and its docstring now lists four documented v2 changes. `test_claude_v1_parity.py` (6 tests, which read v1's `bin/subfleet-claude` on this Mac) and `test_launch_argv_equals_v1s_line_for_the_same_inputs` pass. `without_publish_hook` now removes `--setting-sources user` and the hook and keeps the lane settings' `--settings`.
5. **Contract.** C-12.4 gains: "Every Claude launch's command-line settings set `CLAUDE_CODE_RETRY_WATCHDOG` to `0` (`LANE_SETTINGS_ENV`, d1031), so a lane that reaches its plan's ceiling exits and C-4.5 can move the job on; each launch passes exactly one `--settings`, and a writable review's publish hook rides in it." C-26.11 gains: "Every Claude turn, `read-only` included, passes exactly one `--settings`, whose `env` sets `CLAUDE_CODE_RETRY_WATCHDOG` to `0` (d1031), so a turn that reaches its plan's ceiling ends rather than waiting out the window." A change-log line naming d1031 heads "Changes in version 3". Each clause is still one `- **C-x.y**` line, and no clause id repeats (`merge_contract.py`'s check).

Deviations:

- **Key order.** `env` comes last in a turn's payload and first in a writable review's (`{**settings, "hooks": …}`). A JSON reader ignores key order. The differential compares what remains, after taking the override out, byte for byte.
- **The differential's oracle.** `tests/fixtures/lane_settings/argv-676906f85.json` records 676906f85's argv for every case, taken before any code changed. `test_the_recording_is_what_676906f85_builds` checks the recording against the commit itself: it extracts the commit's `subfleet/` with `git archive` and runs the same cases in a separate `python -I`. It skips only where the clone lacks the commit. This replaces a scratch `git worktree add`, which would have written the shared git metadata.
- **The run list.** `tests/fake/test_conversation*turn*.py` and `tests/unit/*v1*argv*` match no file. The v1 parity tests are in `tests/unit/test_claude_v1_parity.py` and `tests/unit/test_claude_adapter.py`. In place of the first glob, the Claude-turn and probe tests that drive the fakes ran: `tests/e2e/test_conversations.py`, `tests/fake/test_probe_turn.py`, `tests/fake/test_conversation_pools.py`.
- **Four commits, not three.** The fix commit missed two assertions that sliced a writable launch's tail: the resume test, and its copy in `tests/process/test_claude_isolation.py`. Hold B1 caught the first. Their own commit fixes both.

## Invariants

- **I1.** Every Claude launch argv the tree can build has exactly one `--settings`, whose settings carry `env.CLAUDE_CODE_RETRY_WATCHDOG == "0"`.
- **I2.** Every other key a launch's settings carried before is unchanged: `disableAllHooks` (on every turn outside read-only), `fastMode` (with Fast), `ultracode` (an ultracode turn outside read-only) and the publish hook (writable reviews, after `--setting-sources user`). `expected_settings` states these independently of the builders, and the differential checks them against 676906f85.
- **I3.** A user `settings.json` whose `env` sets the variable to `"1"` changes no launch's argv, and no builder opens a `settings.json` or `settings.local.json`. An audit hook watches every `open`.
- **I4.** The read-only flags are unchanged: `--setting-sources ""` exactly once, `--safe-mode`, plan mode and the tool list, flag for flag `ClaudeAdapter.permission_args(READ_ONLY, isolated=…)`. A read-only launch's settings name only `env`, or `env` and a fast turn's `fastMode`.

## Tests

`tests/lane_settings_cases.py` builds every launch path through its builder, the way its caller calls it, over the builders' input grid: 237 cases.

| Path | Cases | Grid |
| --- | --- | --- |
| job | 72 | sandbox × task (build, review, none) × isolated (read-only only) × effort × `TMPDIR` × MCP server |
| resume | 48 | sandbox × task × `TMPDIR` × MCP server × model given |
| turn | 96 | permission (4) × Fast × effort (none, high, ultracode) × new or resumed × `TMPDIR` |
| probe | 8 | the waiting job's sandbox × task × the model's effort |
| timer | 4 | keepalive or heal × effort |
| gate | 4 | plan or PR × effort |
| enrol, probe turns, probe outcome, heal | 5 | |

The cases spawn nothing. The enrolment and probe turns get a runner that records their argv, and the heal gets a `popen` that does the same, with `_kill_group` replaced so the heal signals nothing. The profile opener answers 503.

`tests/unit/test_lane_settings.py` (871 tests):

- **Examples.** I1, I2 and the differential run on all 237 cases, and I4 on the 112 read-only cases, all built once under a user whose `~/.claude/settings.json` says `{"env":{"CLAUDE_CODE_RETRY_WATCHDOG":"1"}}`. I3 builds every case twice: under that user, and under a user with no settings file.
- **Properties (Hypothesis, derandomized, no database).** `test_property_i1_i2_i4_over_the_builders_inputs` draws every path's inputs (sandbox, task, isolated, Fast, effort including ultracode and the efforts outside the grid, permission, resume or not, `TMPDIR`, MCP server). It ran 200 passing examples, 0 failing and 5 invalid (the isolated-writable filter). `test_property_the_merge_keeps_every_other_key_and_turns_the_watchdog_off` draws arbitrary settings, an arbitrary `env` and the user's value of the variable. It ran 300 passing, 0 failing and 30 invalid.
- **The differential.** On each case, the new argv with the override taken out of its one `--settings` (the variable removed, then `env` once empty, then the flag once empty) equals 676906f85's argv, flag for flag. `test_the_recording_is_what_676906f85_builds` ran, not skipped.
- **The fake provider.** `tests/bin/claude` learned `SUBFLEET_FAKE_SETTINGS_REPORT`, which reports the value a session would apply and exits. The fake reads the user's file (`$CLAUDE_CONFIG_DIR/settings.json`, else `~/.claude/settings.json`) and the project and local files, each only when `--setting-sources` names it. It then applies every `--settings` last, as JSON or as a path. For one case of each launch shape (21 cases), and with the user's "1" either in `~/.claude` or in a lane home under `CLAUDE_CONFIG_DIR`, the session sees "0" through exactly one `--settings`. At 676906f85, every one of those 42 tests failed. Run against the recorded 676906f85 argv, the fake reports "1" for the writable build job, the writable review, an `ask` turn and the heal, and no value for a read-only job or a read-only fast turn. That flags outrank files is the fake's assumption, from C-26.11's premise and the 10/22 probe. The fake proves what the argv carries, not how real Claude Code layers it.
- **Builder coverage.** The AST walk described above.

The existing tests that pinned the old argv now expect the merged `--settings`:

- `test_claude_adapter.py`: `without_publish_hook`, the writable tail, the resume tail, the enrolment argv and the isolated parity slice;
- `test_claude_v1_parity.py`, `test_claude_turn.py`, `test_ultracode_effort.py` and `test_claude_conversation_mcp_scope.py`;
- `test_review_publish_guard.py` and `test_publish_rule_round7.py`: the hook rides in the one `--settings`, and every other launch has the lane settings alone;
- `test_claude_cards.py`: the heal argv;
- `tests/process/test_claude_isolation.py` and `tests/live/test_claude_live.py`.

### Red first

At 676906f85 plus the new tests, `tests/unit/test_lane_settings.py` ran in one hold: queued 00:05:27Z, slot B, finished 00:10:56Z (329 s with the wait). The result was **768 failed, 103 passed** in 207 s. The 103 that passed are the grid and recording checks, the builder-coverage walk, and the differential on the 100 cases whose old argv already had a `--settings` with no `env`. Every I1, I2, I3, I4, property and fake-provider test failed. The module then built each case per test; caching the builds per module came after.

### Green holds

One pytest per hold, through `suite_hold.sh`, at most one worker, `TMPDIR` and the Hypothesis store in the job's scratch folder. Holds started 00:18Z to 01:12Z (2026-10-29).

| Hold | Held (UTC), slot, 1-min load | Result | Per file |
| --- | --- | --- | --- |
| new file, first green | 00:18:49Z B, 22.75 | 871 passed, 375.6 s | before the module cached its builds |
| new file | 00:41:43Z B, 61.33 | 871 passed, 146.4 s | after caching |
| new file, final | 01:09:21Z A, 13.93 | 871 passed, 10.4 s | `test_lane_settings.py` 871 |
| B1, first | 00:46:09Z B, 43.89 | 1 failed, 369 passed | `test_resume_launch_uses_resume_not_session_id` (fixed in `e5e072cb7`) |
| B1 | 01:07:47Z A, 12.74 | 385 passed, 14.2 s | `test_claude_adapter.py` 172, `test_claude_attest.py` 19, `test_claude_cards.py` 96, `test_claude_cards_wiring.py` 15, `test_claude_conversation_mcp_scope.py` 8, `test_claude_identity.py` 30, `test_claude_large_stream.py` 30, `tests/process/test_claude_isolation.py` 15 |
| B2 | 00:48:47Z B, 31.08 | 544 passed, 70.9 s | `test_claude_mcp.py` 32, `test_claude_mcp_launch.py` 11, `test_claude_stream.py` 60, `test_claude_synthetic_error.py` 63, `test_claude_turn.py` 69, `test_claude_usage.py` 28, `test_claude_v1_parity.py` 6, `test_ultracode_effort.py` 39, `test_publish_rule_round7.py` 236 |
| C | 00:50:05Z B, 23.03 | 722 passed, 107.6 s | `test_review_publish_guard.py` 135, `test_review_publish_env_round8.py` 17, `test_launch_env_allowlist.py` 27, `_helpers` 20, `_mutations` 70, `_policy` 26, `_round2` 67, `_round3` 70, `_round4` 40, `_round5` 39, `_round6` 43, `_round7` 28, `_round8` 35, `_vendor_shell` 105 |
| D1 | 00:52:46Z A, 12.39 | 97 passed, 38.2 s | `tests/fake/test_probe_turn.py` 61, `test_review_launch_permissions.py` 34, `test_conversation_pools.py` 2 |
| D2 | 00:58:32Z A, 15.60 | 9 passed, 25.6 s | `tests/e2e/test_review_publish_e2e.py` 4, `test_guard_and_isolation.py` 5 |
| D3 | 00:59:47Z B, 11.69 | 29 passed, 15 skipped, 146.6 s | `tests/e2e/test_conversations.py`: the 15 skips are lane-only (10 "person-only requests cannot come from inside a Subfleet turn or job", 5 "inside a Subfleet turn or job the markers refuse first"); CI runs them |
| guards | 01:06:39Z B, 12.30 | 63 passed, 28.2 s | `tests/fake/test_state_contract.py` 37, `tests/unit/test_invariants_index.py` 25, `tests/unit/test_test_module_names.py` 1 |
| guards, on the final contract | 01:12:22Z B, 12.51 | 63 passed, 22.4 s | the same three files, the same counts |

No run went outside the lock.

### Mutants

`tools/lane_settings_mutations.py` edits one site, runs its killing test by node id over the cases it names, and restores the file. A mutant counts as killed only when every failure is an assertion and nothing errors. Each ran in its own hold, 01:06:03Z to 01:06:30Z, at a 1-minute load of 12.6 to 13.1.

| Mutant | Site | Killing test, cases | Result |
| --- | --- | --- | --- |
| one path without the override | `heal_turn` drops `*lane_settings_args()` | I1, the heal case | killed, 1 of 1 failed |
| a second `--settings` instead of a merge | `claude_turn.argv` gives a read-only fast turn `{"fastMode":true}` as its own flag again | I1, the 12 read-only fast turns | killed, 12 of 12 |
| the value `"1"` | `LANE_SETTINGS_ENV` | I1, all 237 cases | killed, 237 of 237 |
| the override drops `disableAllHooks` | `lane_settings` starts from `{}` | I2, the 72 writable turns | killed, 72 of 72 |
| read-only launches skipped | `claude_settings_args` returns `[]` unless writable | I1, the 88 read-only job, resume, probe, timer and gate cases | killed, 88 of 88 |

## What did not run, and what this does not cover

- **No live Claude run.** The job started no live process, as the brief requires. Whether real Claude Code lets command-line `env` override the user's file rests on the 10/22 probe. A live check for the hub, with the watchdog on in a scratch user's settings: start one lane job and one conversation turn, and confirm that `launch.json`'s argv has one `--settings` holding the variable. Then confirm, through the live probe, that the session ran with it off.
- **The full suite.** Only the files listed above ran; CI runs the rest. `tests/e2e/test_conversations.py` ran 29 of its 44 tests, and the 15 lane-only skips run in CI.
- **The operator's `env.pass`.** An explicit policy `env.pass` naming the variable puts it in the process environment. Neither the build nor review r1 established its precedence against command-line settings for real Claude, and neither inspected live policies.
- **Snapshot maps.** `docs/desktop/maps/v2-claude-adapter.md` and `legacy-broker.md` describe the argv as it stood when written, and this change leaves them so.
