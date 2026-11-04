# API credit lanes: the harness (`subfleet.api_agent`)

Build report, 2026-11-04. Branch `feat/credit-lane-harness-217`, based on `release/217`,
for draft PR #292. The build implements "The harness" in
`docs/decisions/2026-11-03-api-credit-lanes.md` to that document's fixed "Interface
between the adapter and the harness". It does not build the adapter.

## Outcome

- **The harness.** `python -I -m subfleet.api_agent` takes the interface's argv, writes
  its records (`stream.jsonl`, `messages.jsonl`, `last.md`), and exits with its codes.
  It runs a streaming Messages API loop against `https://api.anthropic.com` only, with
  a loopback test override that works only inside a live pytest session. Section 31 of
  the contract (C-31.7 to C-31.16) states what it does.
- **The sandbox.** The build chose Subfleet's own seatbelt profile under
  `/usr/bin/sandbox-exec`. It passed all 12 required checks of the matrix (six checks,
  network off and on). `codex sandbox` failed 5 of its 12.
- **Tests.** The harness's 83 tests pass at `40197d802`. The whole suite ran once, in
  12 shards of 4 workers: 26,710 tests, 26,560 passed, 0 failed, 40 skipped, 110
  xfailed. That count includes two shards rerun after fixing the two failures the
  first pass found; this build caused both. The one test file the shards left out
  fails in any Subfleet lane, for a reason this build doesn't touch (see "Tests run").
- **Beyond the brief, fixed here:**
  1. The key stayed readable after `os.environ.pop`. The kernel's copy of the
     environment from exec still held it, and any process of the same user can read
     that copy with `sysctl(KERN_PROCARGS2)`, sandboxed tools included.
  2. The shell could read credential stores: other Codex lanes' `auth.json`, `~/.ssh`,
     `~/.config/gh` and the like.
  3. The editor's `create` could make empty folders outside the worktree when a
     background job swapped a folder for a symlink.
- **Found, not fixed (outside this build):** the daemon starts every guardian with
  the lane's credentials in its exec-time environment, which other processes can read
  the same way. That has to change before an API lane carries a key; see "For the
  hub and the adapter build".

## Commits

| commit | what |
|---|---|
| `e27d936ab` | the `api` extra pinning `anthropic==2.4.0`, `uv lock`, and `--extra api` in CI's `uv sync` |
| `d4dcab493` | the harness: steps 2 to 6 of the brief in one commit, since `harness.py` wires every tool |
| `4dae48651` | the fake Messages API, the first tests and a first report |
| `16184b555` | the sandbox matrix script and its results |
| `e2a3d1e97` | test fixes: one `end` per launch, listing lines compared whole |
| `92f5c7260` | the matrix keeps its scratch inside the checkout |
| `19606e530` | fix: overwrite secrets in the exec-time environment block |
| `deaf12ae4` | fix: refuse the shell's reads of keychains and credential stores |
| `a0c3d668a` | contract section 31 (C-31.7 to C-31.16), and a SIGTERM process test |
| `16b032d20` | fix: the editor walks to its target folder without following links |
| `93237d8bb` | fix: classify the harness's two `SUBFLEET_*` names (a full-suite failure) |
| `bb1157564` | test: invariant 2 end to end, as a Hypothesis property over job prompts and efforts |
| `40197d802` | test: exempt the harness from the daemon-log stream scan (a full-suite failure) |
| (this commit) | this report |

## Files changed

- `pyproject.toml`, `uv.lock`: the `api` extra, `anthropic==2.4.0` with hashes.
- `.github/workflows/tests.yml`: `uv sync --locked --group dev --extra api`. The harness
  tests fail, not skip, without the SDK.
- `subfleet/api_agent/`:
  - `__main__.py`: overwrites the secrets in the exec block, takes the key out of
    `os.environ`, then calls `harness.main`.
  - `exec_env.py`: the in-place overwrite of the exec-time environment block.
  - `cli.py`: the interface's argv; a bad argv exits 1, not argparse's 2.
  - `harness.py`: setup, environment scrubbing, launch refusals, SIGTERM, wiring.
  - `client.py`: the only module that imports `anthropic`: the endpoint, the streaming
    request, error kinds and the neutral credit check.
  - `loop.py`: the turn loop, limits, retries, stop reasons, exit codes and resume.
  - `prompt.py`: the four system prompts and the two tool definitions.
  - `pricing.py`: dollars from usage.
  - `records.py`: the records and the redactor.
  - `sandbox.py`: the writable roots, the seatbelt profile and the tools' environment.
  - `shell.py`: the persistent bash.
  - `hooks.py`: the guard and publish hooks.
  - `editor.py`: `str_replace_based_edit_tool`.
- `subfleet/launch_env.py`: `SUBFLEET_API_LANE` and `SUBFLEET_API_AGENT_TEST_URL` join
  `LAUNCH_NAMES`, so no launch inherits either from its parent.
- `tests/fake/anthropic_api.py`: a fake Messages API on 127.0.0.1 with real
  server-sent events, scripted replies, errors, mid-stream failures, and the
  `request-id` and `anthropic-organization-id` headers.
- `tests/unit/test_api_agent_units.py` (46 tests), `tests/unit/test_api_agent_editor.py`
  (17), `tests/process/test_api_agent_process.py` (20).
- `tests/unit/test_daemon_log_sites.py`: `subfleet/api_agent/harness.py` joins
  `TERMINAL_MODULES`, the processes whose stderr isn't daemon.log.
- `tools/credit_lane_sandbox_matrix.py` and
  `docs/reports/credit-lane-harness/sandbox-matrix.json`: the matrix and its results.
- `docs/acceptance-contract.md`: section 31 with C-31.7 to C-31.16, and the change entry.
- `docs/reports/credit-lane-harness-report.md`: this report.

## The SDK pin

`anthropic==2.4.0`, in the optional `api` extra. Nothing outside `subfleet/api_agent/`
imports it, and inside it only `client.py` does (`test_nothing_outside_the_harness_imports_anthropic`).

- **Why 2.4.0:** it's the newest release. PyPI's JSON API listed it as the latest at
  01:41 EST on 2026-11-04, uploaded 2026-11-02 19:08 UTC, after 2.3.0 on 2026-10-23.
- **What the harness uses:** every SDK name comes from the claude-api skill's Python
  docs and the installed 2.4.0 source:
  - the client: `anthropic.Anthropic(api_key=, base_url=, max_retries=, http_client=,
    open_telemetry=False)` and `anthropic.DefaultHttpxClient(trust_env=False)`;
  - the request: `client.beta.messages.stream(...)` with `get_final_message()`,
    `stream.response.headers` and `current_message_snapshot`;
  - the credit check: `client.messages.with_raw_response.create(...).parse()`;
  - the errors: `APIStatusError`, `APIConnectionError` and `APITimeoutError`, plus
    `httpx2.HTTPError` from `httpx2`, the transport the SDK builds on.
- **The request fields:** `thinking={"type": "adaptive"}`,
  `output_config={"effort": ...}`, `context_management={"edits": [{"type":
  "compact_20260112"}]}` with `betas=["compact-2026-01-12"]`, a top-level
  `cache_control`, and the client tools `bash_20250124` and `text_editor_20250728`.
  The skill's migration guide lists compaction for all three lane models (Opus 5.5,
  Sonnet 5.5, Haiku 5.5).

## The sandbox matrix

`tools/credit_lane_sandbox_matrix.py` runs outside pytest, on this host (macOS 26.6.2,
codex-cli 0.159.0):

```
env -u PYTHONPATH .venv/bin/python tools/credit_lane_sandbox_matrix.py \
    --json docs/reports/credit-lane-harness/sandbox-matrix.json
```

It runs the same checks on both candidates, network off and on:

- **Candidate `subfleet`:** the harness's profile under `/usr/bin/sandbox-exec`, with
  the harness's tool environment.
- **Candidate `codex`:** `codex sandbox -P :workspace`, with an empty temporary
  `CODEX_HOME` and the raw environment, so Codex's own policy decides what passes.

All of a run's scratch lives in a fresh `.sandbox-matrix-*` folder in the checkout,
which the run removes. It plants two credential-shaped variables. It removes
`$HOME/credit-lane-check` and its `/private/tmp` probes only if a check created them;
no check created the first. The committed JSON came from the run at `deaf12ae4`, the
last profile change. A rerun at `40197d802` (01:42 EST) wrote it again byte for byte.

| check | required | subfleet off | subfleet on | codex off | codex on |
|---|---|---|---|---|---|
| write inside the worktree | yes | pass | pass | pass | pass |
| write `$HOME/credit-lane-check` refused | yes | pass | pass | pass | pass |
| `security find-generic-password` refused (see below) | yes | pass | pass | pass | pass |
| `ls ~/Library/Keychains` refused | yes | pass | pass | **FAIL**: listed | **FAIL**: listed |
| `curl https://example.com`: fails off, 200 on | yes | pass | pass (200) | pass | **FAIL**: host not resolved |
| `env` shows no credential variable | yes | pass | pass | **FAIL**: both planted, plus 4 of the session's own | **FAIL**: same |
| unix socket outside the writable roots refused | no | pass | pass | pass | pass |
| write in the worktree's `.git` refused | no | pass | pass | pass | pass |
| append to an attempt record refused | no | pass | pass | not run | not run |
| write in `/private/tmp` refused | no | pass | pass | **FAIL** | **FAIL** |
| read another process's exec block (`KERN_PROCARGS2`) refused | no | **FAIL**: read | **FAIL**: read | **FAIL**: read | **FAIL**: read |

Totals: 42 rows, 31 pass, 11 FAIL. The Subfleet profile passed all 12 of its required
rows and `codex sandbox` 7 of 12.

- **`security`:** under every profile the CLI exited 44 with "The specified item could
  not be found in the keychain.", so its message can't tell a sandbox refusal from a
  missing item. In the same sandbox, the matrix asks the bootstrap server for
  `com.apple.SecurityServer`, `com.apple.securityd` and `com.apple.securityd.xpc`. All
  three answered 1100 (`BOOTSTRAP_NOT_PRIVILEGED`), which is the sandbox's refusal. Under
  `codex sandbox` the keychain files themselves stay readable, so that candidate's "not
  found" may also come from a real search.
- **Codex's network:** `-c sandbox_workspace_write.network_access=true` didn't turn
  network on under `-P :workspace`, so the matrix never exercised Codex's network
  profile.
- **`KERN_PROCARGS2`:** neither sandbox mediates this sysctl. Rules on `sysctl-read`
  and `process-info*` didn't stop it either. The harness therefore overwrites its own
  secrets in that block (C-31.12).

**Choice: the Subfleet profile.**

- It passed every required check in both modes. `codex sandbox` leaves the keychain
  listable, passes credential variables through, wouldn't turn network on in this
  form, and writes `/private/tmp`.
- Owning the profile lets each attempt make `.git` and Subfleet's records read-only,
  refuse credential-store reads, and keep unix sockets inside the writable roots.
- API lanes don't depend on the TRUST-pinned Codex binary.

## Invariants and their tests

The design's invariants 2 and 6 belong to the harness; the adapter build owns 1, 3, 4,
5 and 7. The build also states and tests the harness's own properties.

| invariant | statement | tests |
|---|---|---|
| 2, no identity prompt | for any job prompt and effort, no request's `system` holds an identity line, "Claude Code" or "Agent SDK" after case folding and whitespace normalization | `test_no_request_carries_an_identity_prompt_for_any_job_prompt` (process, Hypothesis: generated job prompts with an identity line or phrase spliced in, every effort, sandbox and network; a mutant that appends the job prompt to `system` fails it); `test_every_system_prompt_is_identity_free` (all four system prompts); `test_identity_check_catches_every_spelling` (Hypothesis, the checker); the tool round trip, whose job prompt is "You are Claude Code … Agent SDK" |
| 6, the key stays in the harness | the key and every 12-character run of it appear in no tool or hook environment, record, attempt file, stdout, stderr or request body | `test_a_tool_round_trip_records_everything_and_keeps_the_key_in` (tool and hook environment dumps, `printenv ANTHROPIC_API_KEY` exits 1, every file in the attempt folder, stdout, stderr, every request body; the key reaches the fake only as the auth header); `test_no_process_reads_the_key_from_the_harness_exec_environment` (a sandboxed tool's `KERN_PROCARGS2` read and a hook's `ps -E` of the harness find `*`s); `test_the_shell_cannot_read_credential_stores`; unit: `test_redaction_leaves_no_run` and `test_redaction_keeps_text_without_runs` (Hypothesis), `test_redactor_never_shows_its_secret`, `test_records_lines_are_versioned_utc_and_redacted`, `test_tool_environment_has_no_credential_name` (Hypothesis), `test_overwrite_hides_secret_values_from_the_exec_block_and_keeps_it_whole` |
| pricing | `usd` is 0 for no tokens, never negative, never falls when a count rises, adds each compaction iteration, and agrees with a reference formula | `test_usd_agrees_with_the_reference_and_is_not_negative`, `test_usd_is_monotone_in_each_count`, `test_compaction_iterations_add` (Hypothesis); `test_interface_prices`, `test_zero_and_missing_counts_cost_nothing`, `test_one_hour_cache_writes_cost_twice_input` |
| hooks fail closed | any hook output that is not an allow decision denies; a denied command never runs | `test_hooks_decide_and_fail_closed`, `test_hook_that_cannot_run_denies`, `test_hook_gets_codex_input_shape`, `test_the_guard_and_the_publish_hook_deny_commands` |
| append-only history | each request's `messages` extends the previous one; a resume continues the file and only appends | the tool round trip (`third[:len(second)] == second`), `test_resume_continues_from_messages_jsonl`, `test_resume_answers_a_call_that_never_got_its_result`, `test_load_history_cuts_a_torn_last_line_and_refuses_other_damage` |
| one `start`, one `end` | each launch appends one `start` and, unless SIGTERM stops it, one `end` last | `Run.end()` asserts it wherever a process test reads the `end` event; `test_sigterm_kills_the_shell_and_writes_no_end` (a mutant that skips the kill fails) |
| outcomes and exit codes | `done` 0, `credit` 3, `refused-by-kind` 4, `workspace-limit` 5, `budget`/`turns` 6, else 1 | `test_credit_refusals_run_the_neutral_check` (both verdicts), `test_a_workspace_limit_ends_without_a_check`, `test_an_inconclusive_check_is_an_error`, `test_max_turns_ends_the_attempt`, `test_max_usd_ends_the_attempt` |
| one endpoint | requests go to `https://api.anthropic.com`; `ANTHROPIC_BASE_URL` and the test override have no effect outside a test session | `test_outside_a_test_session_neither_the_override_nor_anthropic_base_url_is_used` (under a loopback-only profile, so nothing leaves the host), `test_endpoint_is_api_anthropic_com_whatever_the_environment_says`, `test_client_ignores_anthropic_base_url`, the round trip's decoy |
| path confinement | the editor reaches nothing outside the worktree and the attempt folder, symlinks included, and writes only where the sandbox could | `test_paths_outside_are_refused`, `test_a_symlink_that_leaves_the_worktree_is_refused`, `test_a_folder_swapped_for_a_link_after_the_check_leads_nowhere`, `test_hard_links_are_not_written`, `test_git_and_records_are_not_writable`, `test_layout_writes` |

## Tests run

All runs used `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`,
`UV_CACHE_DIR=$TMPDIR/uv-cache`, a fresh `--basetemp` under that TMPDIR, and
`-p no:cacheprovider`. Suite-sized runs held the repository's suites lock
(`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`).

**The harness's tests,** on the final code (`40197d802`), serially:

```
.venv/bin/python -m pytest -q tests/unit/test_api_agent_units.py \
    tests/unit/test_api_agent_editor.py tests/process/test_api_agent_process.py
83 passed in 128.34s
```

**The whole suite,** run once. pytest-xdist isn't in the dev group, so `-n 4` came from
an overlay (`uv run --no-sync --with 'pytest-xdist>=3.6'`, xdist 3.8.0, the same pytest
9.1.1). The suite takes about 175 minutes of CI time, so the CI shard plugin split it
into 12 shards, each one foreground call:

```
uv run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 -q -p no:cacheprovider \
    -p ci_shard_plugin --ci-shard I/12 --basetemp=<new> --junitxml=<shard>.xml \
    --timeout=540 -rfE --ignore=tests/frontend/test_core_live.py      # PYTHONPATH=tools
```

| shard | commit | passed | skipped | xfailed | failed | wall time |
|---|---|---|---|---|---|---|
| 1 | `16b032d20` | 882 | 0 | 1 | 0 | 1025 s |
| 2 | `16b032d20` | 22 | 0 | 0 | 0 | 543 s |
| 3 | `16b032d20` | 149 | 0 | 0 | 0 | 370 s |
| 4 | `16b032d20` | 1,812 | 2 | 13 | 0 | 690 s |
| 5 (rerun) | `40197d802` | 2,155 | 3 | 4 | 0 | 288 s |
| 6 | `bb1157564` | 2,808 | 7 | 1 | 0 | 263 s |
| 7 | `bb1157564` | 2,186 | 16 | 11 | 0 | 207 s |
| 8 | `bb1157564` | 2,586 | 1 | 4 | 0 | 269 s |
| 9 | `bb1157564` | 2,073 | 0 | 18 | 0 | 352 s |
| 10 | `bb1157564` | 3,905 | 10 | 7 | 0 | 293 s |
| 11 | `bb1157564` | 5,436 | 1 | 19 | 0 | 225 s |
| 12 (rerun) | `40197d802` | 2,546 | 0 | 32 | 0 | 517 s |
| **total** | | **26,560** | **40** | **110** | **0** | |

That's 26,710 tests: the 26,710 that `--collect-only` found before the run, less the one
ignored test, plus the invariant 2 property added during the run (shard 7).

- **Two failures in the first pass, both from this build, both fixed:**
  - Shard 5: `test_launch_env_round3.py::test_every_subfleet_name_the_package_reads_is_listed`.
    The harness spelled two unclassified `SUBFLEET_*` names; fixed in `93237d8bb`.
  - Shard 12: `test_daemon_log_sites.py::test_no_daemon_module_writes_its_standard_streams_past_daemon_log`.
    The harness prints to its own stderr; fixed in `40197d802`.

  Both shards then passed on `40197d802`; the table shows those reruns.
- **What the earlier shards missed:** later commits changed only `launch_env.py`'s name
  sets, the stream scan's exempt list and the harness's tests. The suites that read
  those name sets passed afterwards (`test_launch_env_round2` to `round6`,
  `test_launch_env_vendor_shell`, `test_review_publish_env_round8`: 381 passed), and so
  did `test_daemon_log_sites.py` (83 passed).
- **`tests/frontend/test_core_live.py`, run alone with `--timeout=540`:** 1 failed, in
  8 s. The development daemon refused the app probe's `approval.get` with "reading an
  approval is a person's decision: the caller carries Subfleet's attempt markers". That
  refusal comes from the person-only peer check in `subfleet/conversations/peers.py`.
  It refuses a caller whose process chain runs under a guardian or carries
  `SUBFLEET_ATTEMPT=` or `SUBFLEET_JOB=`, and every process of this job does both. The
  build changed nothing in that path, so the failure belongs to running inside a lane.
  The CI workflow sets neither marker.
- **Mutation checks during the build:**
  - a harness that skips killing the shell on SIGTERM fails the SIGTERM test;
  - a harness that appends the job prompt to `system` fails the invariant 2 property;
  - the old editor code fails the folder-swap test (it made `outside/b`);
  - the exec-block and credential-store tests failed before their fixes
    (`19606e530`, `deaf12ae4`).

One note on output during the build: in a deliberate mutant run of the key-leak test,
before `19606e530`, pytest's assertion message printed the test's fake key, and the
session's output filter masked it as a credential. It was the random value the test
makes for each run, never a real key. `assert_no_key` now reports only the record's
name and a count of matching runs.

## Deviations from the design and the brief

1. **Writes are narrower.** Under `workspace-write`, the worktree's `.git` and
   Subfleet's records in the attempt folder are read-only, as on Codex lanes. So the
   model can't commit; the workspace-write prompt tells it to leave its work in the
   tree, where Subfleet collects it.
2. **The profile limits reads.** The design doesn't. The profile refuses each
   home's keychains, the credential stores in `DENY_READ`, and every home entry whose
   name starts with `.codex` (Codex's home and lanes `~/.codex-1` to `~/.codex-9`, each
   with an `auth.json`). With network, a tool could otherwise send them away.
3. **The tools' environment drops more than the brief lists.** Besides
   `ANTHROPIC_API_KEY` and the KEY, TOKEN, SECRET and PASSWORD names, it drops every
   `ANTHROPIC_*` name, names matching the repo's `CREDENTIAL_ENV_PATTERN`, bash's
   start-up variables and exported functions.
4. **The exec-block overwrite** isn't in the design. It's the only way the key stops
   being readable to other processes once the harness has started (C-31.12).
5. **The `security` check** shows the denial through the bootstrap look-ups, because
   the CLI prints "could not be found" either way.
6. **Retries.** The design relies on the SDK's retries (`max_retries` 2). The loop
   adds up to two retries, after 5 s and 20 s, for a stream that breaks mid-way with
   `overloaded_error` or `api_error` or loses its connection. The SDK doesn't retry
   those.
7. **Credit outcomes the brief didn't name:**
   - A neutral check that neither fails the same way nor succeeds (a 401, say, or no
     answer) ends `error`, exit 1, with no `credit` event.
   - The workspace-limit text counts on any 4xx, not only a 400, and needs no check.
8. **The harness refuses a workspace-write launch without `--guard-hook`.** The design
   puts that refusal in the adapter; the harness repeats it.
9. **The loop checks `--max-usd` before each request,** so one response can carry an
   attempt past the cap: up to 64,000 output tokens plus that request's input.
10. **Prompt caching** covers the conversation too: a top-level `cache_control` on top
    of the design's breakpoints on the system prompt and the tools, three breakpoints
    in all. `max_tokens` is 64,000.
11. **No server-side `fallbacks`,** so the served model is the requested one. The
    harness records each response's `model` but doesn't judge attestation; classify
    has to compare it with the requested model.
12. **Changes outside `subfleet/api_agent/`:** two names in `launch_env.LAUNCH_NAMES`,
    and the harness in the stream scan's `TERMINAL_MODULES`. The full suite required
    both.
13. **The full suite's `-n 4`** came from an xdist overlay, in 12 sharded foreground
    runs, as "Tests run" describes.

## For the hub and the adapter build

1. **The guardian's exec block holds each lane's credentials.** Resolved for credit
   lanes on 2026-11-04: the hub ruled that the key travels only over pipes, and the
   integration build took the second way below. The daemon sends the key on the launch
   descriptor, the guardian hands it to the harness on a pipe of its own, and no
   environment holds it (C-31.17; `docs/reports/credit-lane-integration-report.md`). The
   harness's own block no longer holds the key at all, so the window in the last point
   is gone too. Other lanes' credentials still ride in their guardians' blocks; see that
   report's open questions.
   - What happens: `_spawn_guardian` in `subfleet/daemon.py` starts the guardian with
     `env=env`, the launch's `env_add` included, and the guardian builds its child's
     environment from its own `os.environ` (`subfleet/guardian.py`). So a lane's
     credential variables sit in the guardian's exec-time block for the attempt's
     whole life.
   - Why it matters: the matrix shows a sandboxed process reading another same-user
     process's block, given its pid. Once the adapter passes `ANTHROPIC_API_KEY`
     through `env_add`, any lane's tools that find a guardian's pid can read that key,
     and invariant 6 fails at the fleet level.
   - Two ways to close it: the guardian overwrites credential values in its own block
     at start, as `exec_env` does, keeping a copy for its child; or the daemon hands
     credentials over the launch descriptor instead of the environment.
   - Even with the first, the harness's own block holds the key from exec until
     `__main__` overwrites it, a window of tens of milliseconds.
2. **The daemon drops the key today.** `_launch_environment` always removes
   `ANTHROPIC_API_KEY`, and `build_environment` applies removals after additions. D-ENV3
   (`foreign_credentials`) also drops any vendor credential whose vendor isn't the
   launch's provider, and a provider missing from `VENDOR_WORDS` keeps none. The adapter
   build has to change both for a credit lane's key to reach the harness.
3. **Resume.** The harness reads `messages.jsonl` from `--attempt-dir`. A resume gets a
   new attempt folder, so the adapter has to copy the previous attempt's
   `messages.jsonl` there before launching with `--resume`.
4. **Classify.** Read the last `end` event. SIGTERM writes none, so exit code 1 decides
   then. Compare each `response.model` with the requested model for attestation.

## Open questions

- **Reads stay broad.** The shell can still read `~/.subfleet` (the store, other jobs'
  prompts and worktrees), `~/.claude` apart from `.credentials.json`, and other
  repositories. Should the profile narrow reads to the worktree, the toolchains and the
  system?
- **The shell keeps state across commands.** The guard judges each command's text, but
  the persistent shell carries functions, aliases and variables, so a command can
  define what a later, harmless-looking command runs. Should the guard see a
  normalized form, or the shell refuse `alias`, `function` and `shopt -s
  expand_aliases`?
- **A resume on another lane:** the API binds thinking blocks to the model and the
  conversation. No test covers whether a resume on a lane from a different
  organization replays them cleanly.
- **The neutral check against the real API:** the fake accepts Haiku 5.5 at
  `max_tokens` 1 with no thinking field. Haiku 5.5 thinks adaptively by default, so the
  first real exhaustion should confirm that the check answers 200 (it counts as
  "success" whatever the stop reason).
- **Codex's network profile:** the matrix couldn't turn it on. An earlier manual probe,
  not repeated by the matrix, suggested that it opens `com.apple.SecurityServer` and
  unix sockets outside the writable roots. That only matters if anyone reconsiders
  `codex sandbox`.
