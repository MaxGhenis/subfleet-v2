# The restore recipe's fence survives any path, and no test failure can print a credential (D-PRR1)

Build on `fix/prune-restore-recipe-217`, cut from `release/217` at `2eacd5806`. Initial work began on 2026-10-22 at 04:34 UTC; this continuation resumed checkpoint `8c2e9de04` at 07:51 UTC. Report reconciled at 2026-10-22 09:29 UTC. The initial phase observed host load averages of 60–223; this continuation's managed sandbox denies host process and kernel queries. The caller's branch remains in its own checkout, so this job retains the supplied detached history. Nothing was pushed.

## What went wrong on 2026-10-21

**The flake.** `tests/unit/test_prune_restore.py::test_restore_recipe_preserves_backup_and_displaced_files` failed under `--hypothesis-seed=18`. This build reproduced it at `2eacd5806`. Hypothesis shrank it to `rows=[]`, `suffix='```'`, `wal=None`, `shm=None`. The backup path then holds three backticks. `_run_recipe` cut the recipe at the first triple backtick after the opening fence, which was inside the quoted path. So `/bin/sh` received a script that ended in an unterminated quote (`backup='…backup '"'"' $ ; `) and failed with `CalledProcessError`.

**The product side.** `RESTORE_RECIPE` opened its block with a fixed three-backtick fence. A CommonMark reader ends a fenced block only at a line holding nothing but a backtick run at least as long as the opening, so seed 18's mid-line run does not end it for a CommonMark renderer. But it does end it for any reader that looks for the next triple backtick, as the harness did. A path that holds a newline and then a line of three backticks ends it early for CommonMark too. At `2eacd5806`, `_restore_fix("/backups/state\n```\n…")` reads as a shell block cut off in the middle of `backup=…`, followed by a second, unclosed block (regression B2 below).

**The leak.** The failure's traceback printed the subprocess environment, a lane's token with it. `_run_recipe` passed `env={**os.environ, "state_root": …}` to `subprocess.run`.

## Decision 1: a fence no path can close

`subfleet/prune_decisions.py`:

- `RESTORE_SCRIPT` (`:113`) is now the shell block's own template, with `{copy_path}`.
- `RESTORE_RECIPE` (`:129`) holds `{fence}sh`, `{script}` and `{fence}`.
- `_restore_fix` (`:364`) quotes the path with `shlex.quote`, fills in the script, and fences it with `code_fence(script)` (`:384`).
- `code_fence` returns a backtick run one longer than the longest run in the body, and never shorter than three. No line inside the block can then be a closing fence, whatever the path holds: a closing fence must be at least as long as the opening.
- The migration guide's copy between the `prune-restore` markers is unchanged and still equals `_restore_fix("<backup copy>")` (`test_documented_restore_matches_error_recipe`). Its placeholder holds no backtick, so its fence is still three.

### The sweep: operator-facing fenced blocks that embed a runtime value

Searched `subfleet/` and `docs/` for backtick and tilde fences (`` ``` ``, `~~~`), for fences built in code (`"`" * n`, `chr(96)`, `\x60`), and for code that writes Markdown into `docs/`.

| Where | What it embeds | Can a value close it? | Done |
| --- | --- | --- | --- |
| `subfleet/prune_decisions.py` `RESTORE_RECIPE` through `_restore_fix`. It is printed as the `fix:` of `PruneStopped` (`:935`) and of both `PruneVerificationError`s (`:1171`, `:1207`), by `_fail` (`:1225`). | the backup copy's path | yes | fixed: `code_fence` |
| `docs/migration.md`, between `<!-- prune-restore:start -->` and `<!-- prune-restore:end -->` | `_restore_fix("<backup copy>")`, a fixed placeholder | no: the placeholder holds no backtick | unchanged; the existing test keeps it equal to the code |
| Every other fence under `subfleet/` | none: the recipe is the only one | — | — |
| `tools/soak_report.py`, which writes `docs/soak/<date>.md` from the store | job data | no fences in the writer | — |
| The other 120 files under `docs/` with fences | hand-written text | no runtime value | — |

There is one adjacent class, which is not fenced and was not changed. Fourteen f-strings in four modules put a value inside a single-backtick code span of terminal text. Nine of them embed a runtime value: `subfleet/compat.py:434` (a dropped token), `:689` and `:782` (an unknown verb), `subfleet/sessions/client.py:124` (a protocol op), and `subfleet/doctor.py:163`, `:169`, `:170`, `:335` and `:477` (a found binary's path). The five in `subfleet/cli.py` embed constants. These messages go to a terminal, not a Markdown renderer. A backtick in the value makes a span ambiguous to a reader but cannot hide a command. They are listed for the hub; no change was made.

## Decision 2: the harness reads the block as a reader does

- `tests/markdown_fences.py` reads backtick fences by CommonMark 0.31's section 4.5:
  - An opening fence has up to three spaces of indentation, three or more backticks, and an info string without a backtick.
  - The block ends at the first later line of up to three spaces, then at least as many backticks, then only spaces or tabs; otherwise at the end of the document.
  - Lines end at LF, CR LF or CR. Content keeps its line endings.
  - `test_the_fence_reader_follows_commonmark` holds six cases of these rules.
- `_run_recipe` takes the one closed `sh` block with `shell_block` and asserts that it is exactly `RESTORE_SCRIPT.format(copy_path=shlex.quote(path))` plus its final newline. A truncated extraction fails there, by name, not as a shell syntax error.
- `tests/unit/test_prune_217.py` had the same naive split, and now uses `shell_block` too.
- The restore property gained four `@example`s:
  - seed 18's shrunk case, `suffix="```"`;
  - `"````"` with both sidecars;
  - backticks mixed with the alphabet's other characters, `"a```$;'b````\"(!&"`;
  - `"\n```\n"`, a run on a line of its own, which executes a restore through a path that ends a three-backtick fence for CommonMark.

## Decision 3: no test can print a credential

### The mechanism

To establish it, a scratch test forced `_run_recipe` to fail at `2eacd5806`, through a backup path holding three backticks. Each child pytest ran in an `env -i` environment holding only `PATH`, `HOME`, `TMPDIR`, `LANG`, Hypothesis's storage directory and `SUBFLEET_DUMMY_TOKEN=SECRET7prr1`. The dummy was placed first in the environment in one run and fifteenth, after fourteen others, in another. Output went to scratch only. The software was pytest 9.1.1, Hypothesis 6.168.1 and Python 3.14.7.

| Options | Dummy first | Dummy fifteenth | Where it printed |
| --- | --- | --- | --- |
| `-q` (default `--tb=auto`) | printed once | not printed | the `kwargs = {'env': {…` argument line of the `subprocess.run` entry |
| `--tb=long`, `--full-trace` | printed once | not printed | the same line |
| `-vvv` | printed once | not printed | the same line, untruncated in length |
| `-l`, with or without `-q` | printed twice | not printed | the same argument line, plus the same frame's `kwargs` local |
| `-l -vv` | printed twice | printed once | the locals, pretty-printed in full: the whole environment |
| `--tb=short`, `--tb=native` | not printed | not printed | — |

- **The frame** is `subprocess.run` (`Lib/subprocess.py:578`, where it raises `CalledProcessError`). Its `**kwargs` holds `env`. `_run_recipe` holds no copy of its own, because the dict is an argument expression.
- **Arguments in the default traceback.** pytest maps `--tb=auto` to `long` for a test's failure (`Function.repr_failure`), and `Node._repr_failure_py` asks for `funcargs=True`. So every long-style entry prints its function's arguments through `saferepr`. That cuts the text to 240 characters, and its `reprlib` base cuts a dict after four entries. The first variable or two of `os.environ`, in the order the process inherited them, therefore print in full.
- **Locals.** `-l` adds `repr_locals` for the same frame. Verbosity above 1 turns off locals truncation and uses `safeformat`, a full pretty-print, which is why `-l -vv` prints every variable wherever it sits. Verbosity above 2 lifts the 240-character cap on arguments, but `reprlib`'s four-entry cut still applies.
- **What did not print it.** Not `CalledProcessError`'s text, which holds the command (the truncated script) and the status. Not Hypothesis's report: re-running seed 18 itself showed its `Failing test case: …(rows=[], suffix='```', …)` block holding only draws.
- **Which applied on 2026-10-21.** Not known: the builder's options and its environment's order are gone. In the initial build environment the first credential-shaped name sat 14th, so the default and `-l` reports would not have shown one here, while `-l -vv` would have shown all of them. The class fix covers every option.

Exact probe options, after `python -m pytest -p no:cacheprovider tests/unit/test_zz_prr1_probe.py`, were `-q`; `-q --tb=short`; `-q --tb=native`; `-q --tb=long`; `-q -l`; `-l`; `-vv -l`; `-vvv`; and `-q --full-trace`. Each was run with the dummy first and fifteenth. The seed-18 reproduction additionally used `-q --hypothesis-seed=18` and `-q -l --hypothesis-seed=18` on the original restore property, plus a late-position `-q --hypothesis-seed=18` run.

### The local fix

`_run_recipe` builds its child's environment from nothing, in `_recipe_environment`. It holds `PATH`, the session's temporary `HOME`, `TMPDIR`, `LANG` and `LC_ALL` when set, and `state_root`.

`test_run_recipe_gives_the_shell_only_the_variables_it_needs` puts a stand-in `sqlite3` first on `PATH`. The stand-in records the names, never the values, of the environment the recipe's shell gives it. The test asserts that they are exactly those, less what the shell and interpreter set for themselves (`PWD`, `OLDPWD`, `SHLVL`, `_`, `__CF_USER_TEXT_ENCODING`, `__PYVENV_LAUNCHER__`).

### The class fix

- **The pattern.** `subfleet/home_paths.py` holds `CREDENTIAL_ENV_PATTERN`, beside `SAFE_ENV_REMOVALS`. It matches names ending in `_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD` or `_COOKIE`, and names holding `CREDENTIAL` or `_AUTH`, ignoring case. The ASCII case-folding follows `launch_env.CREDENTIAL_PATTERN`'s precedent, and catches npm-style lower-case names like `npm_config__auth`. `credential_shaped(name)` never matches the session's markers (`PYTEST_MARKER`, `PYTEST_SESSION`, `PYTEST_SESSION_ID`). `remove_credentials(environ)` deletes the matching variables and returns them; `restore_credentials(environ, removed)` puts them back.
- **Configure.** `tests/conftest.py`'s `pytest_configure` calls `remove_credentials(os.environ)` before collection and keeps the result on `config`. The call comes after the existing baseline snapshot, so both snapshots hold the true originals. `CLAUDE_LANE_AGENT_SECRET` and `SUBFLEET_AGENT_SECRET` match the pattern and are in both, so they come back with the same value whichever restore runs last.
- **Unconfigure.** `pytest_unconfigure` restores them after the baseline's own restore.
- **Why nothing in the process can print one.** On a POSIX build, `os.environ` deletes through to the C environment, and the interpreter's startup snapshot is the same dict. A check in this job on macOS, printing booleans only, confirmed three things:
  - `os.environ._data is posix.environ`;
  - a deleted name is gone from `posix.environ`;
  - an inheriting `/bin/sh` child does not see it.

  So no test, no child that inherits the environment, and no traceback can print a credential the session inherited.
- **Out of the pattern's scope.** A credential in a variable whose name does not look like one: for example `OTEL_EXPORTER_OTLP_HEADERS`, or a URL with a password in it.

**What a lane's environment loses.** These are the names the pattern removes from the initial checkpoint phase's environment. The audit displayed names only; it displayed no values.

| Name | What it is for |
| --- | --- |
| `SSH_AUTH_SOCK` | The ssh-agent's socket. It is not a secret itself, but a test child that inherits it can use the person's SSH keys through the agent. `subfleet/host_push.py` already drops it under pytest. `launch_env.CREDENTIAL_EXCEPTIONS` admits it for production launches, which this change does not touch. |
| `CALLME_PHONE_AUTH_TOKEN` | Judged by its name: an auth token for a phone-call integration ("CallMe"). Nothing in this repository reads it. |
| `CALLME_OPENAI_API_KEY` | Judged by its name: that integration's OpenAI key. Nothing in this repository reads it. |
| `CALLME_NGROK_AUTHTOKEN` | Judged by its name: that integration's ngrok tunnel token. Nothing in this repository reads it. |
| `CLAUDE_CODE_MESSAGING_TOKEN` | Judged by its name: a Claude Code session's messaging token. Nothing in this repository reads it. |

The continuation inherited 21 matching names, listed below. Together with the initial phase's five names, these are 25 distinct names. Purposes are inferred from names unless the repository establishes them; no service was contacted and no credential value was displayed.

| Name | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API access. |
| `ATTIO_API_KEY` | Attio CRM API access. |
| `CENSUS_API_KEY` | Census API access; consumer inferred from the name. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth authentication. |
| `CLOUDFLARE_AXIOM_TOKEN` | Cloudflare API access for the Axiom integration. |
| `CLOUDFLARE_FARNESS_TOKEN` | Cloudflare API access for the Farness integration. |
| `CLOUDFLARE_GLOBAL_API_KEY` | Cloudflare account API access. |
| `GEMINI_API_KEY` | Gemini API access. |
| `GOOGLE_API_KEY` | Google API access; exact API not established by this audit. |
| `GRANOLA_API_KEY` | Granola integration API access. |
| `HUGGING_FACE_TOKEN` | Hugging Face service authentication. |
| `OC_PERSONAL_TOKEN` | A personal token for an OC integration; exact consumer unverified. |
| `OPENAI_API_KEY` | OpenAI API access. |
| `OPENROUTER_API_KEY` | OpenRouter API access. |
| `POLICYENGINE_SUPABASE_SERVICE_KEY` | PolicyEngine Supabase service access. |
| `POPULACE_SUPABASE_SECRET_KEY` | Populace Supabase service access. |
| `SSH_AUTH_SOCK` | The ssh-agent socket, also present in the initial phase. |
| `SUPABASE_ACCESS_TOKEN` | Supabase account or management API access. |
| `SUPABASE_GRANTKIT_SERVICE_KEY` | GrantKit Supabase service access. |
| `XAI_API_KEY` | xAI API access. |
| `ZENODO_TOKEN` | Zenodo API access. |

**Tests changed because they needed such a variable.** In `tests/unit/test_live_store_guard_round3.py`, the `agent` case of `test_host_credentials_after_configure_and_fixture_undo` and `test_host_credentials_accept_explicit_fixture_configs`.

- Both check that `host_push._environment` drops `SSH_AUTH_SOCK` under pytest. The first set the socket before its nested `pytest_configure`, which now removes it; the second relied on whatever the environment held.
- Either would now pass without reaching `host_push`'s drop. So each sets a socket path itself, and the drop is exercised everywhere, CI included.
- No other test reads a credential-shaped variable it does not set. Every absence check found (`GH_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` and others) sets its own value first. The fake providers read `CLAUDE_CODE_OAUTH_TOKEN` from the environment their launcher gives them.

**The sweep.** The requested patterns (`{**os.environ`, `dict(os.environ` and `os.environ.copy()`) match 72 lines in 42 files at `2eacd5806`, and 74 lines in 42 files in the final tree. This raw count includes non-launch copies. Following those copies through direct subprocess calls, launch helpers and blocked launch attempts gives **46 child-environment copy sites in 35 files**, both before and after the change: the old restore harness's site is replaced by the deliberate failing child fixture's site.

The 28 remaining final-tree matches are synthetic audit-event inputs, assertion or report snapshots, mocked runner observations, the quoted v1 source line in `tests/fixtures/compat/cases.json`, and two deliberate report channels in `tests/credential_leak_child.py` (captured printing and Hypothesis's example). They do not pass a copied environment toward a child launch. In particular, `refuses` and `audit` in the guard tests synthesize audit events; `refused` attempts `subprocess.run` and is counted as a launch site.

No retained child-copy site was rewritten. The session removal protects their inherited environment; tests that introduce credentials do so with their own dummies. The new failing child fixture intentionally copies the already-sanitized environment to exercise the report regression.

| File | Raw pattern hits | Child-environment copy sites |
| --- | ---: | ---: |
| `tests/credential_leak_child.py` | 3 | 1 |
| `tests/fake/conftest.py` | 1 | 1 |
| `tests/fake/interactive_claude.py` | 1 | 1 |
| `tests/fake/test_daemon_stacks.py` | 2 | 2 |
| `tests/fake/test_enrollment_guardian.py` | 1 | 1 |
| `tests/fake/test_gate_end_to_end.py` | 2 | 2 |
| `tests/fake/test_launch_gates_above_stdio.py` | 2 | 2 |
| `tests/fake/test_resume_cli.py` | 1 | 1 |
| `tests/fake/test_routing_end_to_end.py` | 1 | 1 |
| `tests/fixtures/compat/cases.json` | 1 | 0 |
| `tests/frontend/test_core_suggestions_r1b.py` | 1 | 1 |
| `tests/nonblocking.py` | 1 | 1 |
| `tests/process/test_census_responsibility_process.py` | 1 | 0 |
| `tests/process/test_claude_isolation.py` | 1 | 1 |
| `tests/process/test_pipe_above_stdio.py` | 1 | 1 |
| `tests/unit/test_capacity_steer_mutations.py` | 1 | 1 |
| `tests/unit/test_ci_retry_known_flakes.py` | 1 | 1 |
| `tests/unit/test_compat.py` | 1 | 1 |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 1 | 1 |
| `tests/unit/test_conversation_fifo_reads.py` | 1 | 1 |
| `tests/unit/test_host_push.py` | 1 | 1 |
| `tests/unit/test_launch_env_allowlist.py` | 1 | 0 |
| `tests/unit/test_launch_env_helpers.py` | 3 | 0 |
| `tests/unit/test_legacy_hold.py` | 1 | 1 |
| `tests/unit/test_live_store_guard.py` | 1 | 0 |
| `tests/unit/test_live_store_guard_round10.py` | 8 | 3 |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 1 | 1 |
| `tests/unit/test_live_store_guard_round6.py` | 3 | 3 |
| `tests/unit/test_live_store_guard_round7.py` | 1 | 1 |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 1 | 1 |
| `tests/unit/test_live_store_guard_round8.py` | 1 | 0 |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 1 | 1 |
| `tests/unit/test_live_store_guard_round9.py` | 11 | 1 |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 1 | 1 |
| `tests/unit/test_prune_217.py` | 1 | 1 |
| `tests/unit/test_rediscovery_git_trace.py` | 1 | 0 |
| `tests/unit/test_salvage.py` | 1 | 1 |
| `tests/unit/test_salvage_optional_transient.py` | 1 | 1 |
| `tests/unit/test_salvage_unindexable.py` | 6 | 4 |
| `tests/unit/test_soak_tools.py` | 1 | 1 |
| `tests/unit/test_stop_alarm.py` | 2 | 2 |
| `tests/unit/test_suggestion_secret_properties.py` | 1 | 1 |

## Decision 4: the contract

- **C-11.5** is the one-off prune pass's clause, and it points at `docs/migration.md`. It covered the pass and its backups but not the recipe's text. It gains one sentence: the recipe fences its shell block with a run longer than any inside it, and never shorter than three.
- **C-20.6** is the test-isolation clause that defines `tests/conftest.py`'s session baseline. It gains the credential removal, with the pattern, the markers' exemption, the restore at cleanup, and "a test that needs such a variable sets a dummy itself".
- One change-log entry covers both: C-11.5 and C-20.6, 2026-10-22, D-PRR1.

## The invariants

1. **The block is whole.** For every backup path, the recipe's fenced block, read by CommonMark's fence rule, is exactly `RESTORE_SCRIPT` with `shlex.quote(path)` substituted. Its fence is longer than any run inside the block and at least three, and the prose around it is the prose every path gets.
   - `test_restore_fix_block_is_the_template_script_for_every_path`: a property over paths drawn from backtick runs of 0 to 6, `'`, `"`, `$`, LF, CR, CR LF, tab, `/`, `;`, non-ASCII, and arbitrary text. 200 examples plus three explicit ones.
   - `test_restore_fix_fences_its_block_beyond_every_backtick_run_in_the_path`, on four paths.
2. **The restore still works.** For every drawn path and sidecar combination, the store's content equals the backup's, the displaced files are byte-identical and kept together, and the backup is unchanged.
   - `test_restore_recipe_preserves_backup_and_displaced_files`: 30 examples plus the four new `@example`s.
   - The two hot-WAL tests; `test_prune_217`'s executed restore.
3. **No credential in test output.** With dummy credential-shaped variables set before the session starts, a failing test's whole report holds no part of any value: the whole value, its fixed prefix, or any ten characters of its random tail.
   - `test_c20_6_a_failing_session_prints_no_part_of_an_inherited_credential` covers it under `-l --tb=long` and under `-l --tb=long -vv`.
   - It runs `tests/credential_leak_child.py` in a child session with nine dummies, the first of them first in the environment, and one of them empty.
   - The child fails through each channel, and the test checks that each channel printed: a frame's arguments, `-l` locals, captured output, an assertion's operands and Hypothesis's falsifying example.
4. **Restored after the session.** `pytest_unconfigure` puts every removed variable back exactly as it was, an empty value included.
   - The same child test hashes each dummy before `pytest.main` and after it returns.
   - `test_c20_6_unconfigure_puts_back_exactly_what_configure_removed` checks the session's own hook.
   - `test_c20_6_removal_takes_exactly_the_credentials_and_restore_puts_each_back` is a property over plain mappings, whatever the session sets or drops.

## Regressions at `2eacd5806` and mutations

Each case ran in an exported tree, in a minimal environment, with output in scratch.

| Case | Tree | Failed | Passed |
| --- | --- | --- | --- |
| B1: seed 18's shape as an `@example` | `2eacd5806` with only the `@example` added | `test_restore_recipe_preserves_backup_and_displaced_files`, with `CalledProcessError` from the truncated script | 0 |
| B2: the direct fence test | `2eacd5806` with the new test file | all 4 `test_restore_fix_fences_its_block_beyond_every_backtick_run_in_the_path` paths: `3 > 3`, `3 > 4`, and two `['sh', '']` (the block ended early) | 0 |
| B3: the child session | `2eacd5806` with the new test file and child | both `test_c20_6_a_failing_session_prints_no_part_of_an_inherited_credential` options: the dummies reached the report | 0 |
| M1: a fixed three-backtick fence | this build | the restore property (its `\n```\n` example), all 4 fence tests, 2 `test_run_recipe_hands_the_shell_the_whole_block` paths and the invariant 1 property | 2 |
| M2: the harness splits at the first triple backtick | this build | the restore property (seed 18's example) and all 4 `test_run_recipe_hands_the_shell_the_whole_block` paths | 1 |
| M3: `_run_recipe` copies `os.environ` | this build | `test_run_recipe_gives_the_shell_only_the_variables_it_needs` | 4 |
| M4: the session's removal dropped | this build | `test_c20_6_this_session_holds_no_inherited_credential` and both child-session options | 27 |
| M5: unconfigure does not restore | this build | `test_c20_6_unconfigure_puts_back_exactly_what_configure_removed` and both child-session options | 27 |

The first run of B3 and M4 failed on the child's outcome line, because a session that keeps its dummies also fails the child's own presence check. The leak check now runs first (commit `8c2e9de04`). Rerun after that commit, both cases fail on it directly: under both options, all 8 non-empty dummies are named as leaked (`assert ['SUBFLEET_DUMMY_TOKEN', …] == []`, "Left contains 8 more items"). M4's `test_c20_6_this_session_holds_no_inherited_credential` fails as before.

## Test counts

All target regressions and invariants pass. Whole-directory coverage is incomplete: the brief's cutoff for starting holds was 09:04 UTC (04:34 UTC start plus 4 h 30 m), and the last hold was submitted at 09:03:40 UTC. No holds were started after that cutoff. The tables name every unrun file and show every partial file.

Counts below use the latest completed outcome per pytest node ID, rather than adding repeated executions. Hypothesis examples are not counted as separate pytest tests. The baseline regression and mutation failures are separate experiments and are not included in these totals.

| Directory | Passed | Failed | Skipped | Xfailed | Complete files | Partial files | Unrun files | Unrun cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 8,630 | 3 | 10 | 0 | 222 | 1 | 103 | 4,582 |
| `tests/fake` | 452 | 0 | 0 | 2 | 17 | 1 | 88 | 1,518 |

Three unexpected failures remain, all in files unchanged from `2eacd580605e`:

- `tests/unit/test_daemon_qos_tools.py::test_thread_priorities_reads_every_thread_of_a_clamped_process`: `PermissionError: [Errno 1] Operation not permitted: '/bin/ps'`.
- `tests/unit/test_descriptors.py::test_c16_6_the_kernel_ceiling_is_read_on_macos`: the kernel-ceiling query returns `None` in this sandbox; the test requires a positive integer. The sandbox also denies `sysctl`.
- `tests/unit/test_hooks.py::test_the_hook_command_with_the_daemons_markers_spawns_nothing_and_surfaces_only_jobs`: the same `/bin/ps` permission denial.

These are sandbox-dependent failures, not failures of the restore or credential regressions. They were not skipped or rewritten. Their files have no diff against the base.

### Holds and repeated executions

| Hold | Passed | Failed | Skipped | Xfailed | Pytest seconds (excludes lock wait) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `h1-prune` | 144 | 1 | 0 | 0 | 186.463 |
| `h2-guard` | 1245 | 0 | 0 | 0 | 712.968 |
| `chunk0` | 261 | 0 | 0 | 0 | 705.648 |
| `resume-focused` | 205 | 0 | 0 | 0 | 181.578 |
| `resume-all1` | 206 | 0 | 0 | 2 | 958.010 |
| `resume-all2` | 2179 | 2 | 3 | 0 | 950.288 |
| `resume-all3` | 1607 | 0 | 0 | 0 | 1016.310 |
| `resume-all4` | 3582 | 1 | 7 | 0 | 952.761 |

- `h1-prune`'s one failure was the new test expecting `-l -vv` locals on one line. Commit `b43b4f191` fixed the test to read the multiline representation. Both child-report variants passed in later runs.
- `h2-guard` covers the requested `test_live_store_guard*` and `test_launch_env*` files selected by the original build; `test_live_store_guard_round3.py` was additionally verified after its dummy-socket adjustment.
- `chunk0` is the first whole-suite batch from the checkpoint.
- `resume-focused` passed all 205 distinct tests. Although two filename patterns overlapped, pytest collected the 48 tests in `test_prune_decisions.py` once; the XML contains 48 distinct names and no repetitions.
- Each broad `resume-all*` hold ended through the budget hook or its timer, returning status 2 for interruption. Every hold's pytest runtime stayed below 20 minutes; lock waits occurred before the pytest timer started. Partial files remained eligible for the next hold.
- All holds used the existing scratch venv, the hub's suite wrapper, scratch-local `TMPDIR` and Hypothesis storage, and `HYPOTHESIS_PROFILE=ci`. The final hold additionally selected `--hypothesis-profile=ci --hypothesis-seed=18` explicitly and ran `test_prune_restore.py` (20 passed) and `test_session_credential_env.py` (30 passed) first. No deep profile was used.
- The prior setup used the hub's uv setup wrapper once; the continuation reused the venv with `uv run --no-sync` and performed no installation or network operation.
- Process census was attempted before writing these deliverables with `pgrep -fl 'suite_hold.sh|pytest|lockf'`. This sandbox returned status 3, “Cannot get process list.” All task-started foreground hold commands had already returned; no detached task process was started. Other jobs were left alone.
- Scratch logs, probe trees, venv, storage and caches are removed after committing the reports. No logs, JSON evidence or bundles are staged.

Partial files: `tests/fake/test_capacity_steer_daemon.py`, `tests/unit/test_retention_81_notes.py`.

### Every test file in tests/unit

| File | Passed | Failed | Skipped | Xfailed | Collected | Status |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `tests/unit/test_admission_uncapped.py` | 0 | 0 | 0 | 0 | 27 | unrun |
| `tests/unit/test_agent_secret_credentials.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | 13 | complete |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | 6 | complete |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | 33 | complete |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | 10 | complete |
| `tests/unit/test_capacity_desktop.py` | 0 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 | 10 | complete |
| `tests/unit/test_capacity_steer_route.py` | 0 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_capacity_view.py` | 0 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_census_counted.py` | 5 | 0 | 0 | 0 | 5 | complete |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | 8 | complete |
| `tests/unit/test_census_procargs.py` | 52 | 0 | 0 | 0 | 52 | complete |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | 25 | complete |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | 20 | complete |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 | 17 | complete |
| `tests/unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 | 34 | complete |
| `tests/unit/test_census_survivors_review.py` | 0 | 0 | 0 | 0 | 14 | unrun |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 0 | 0 | 0 | 72 | complete |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 | 48 | complete |
| `tests/unit/test_claude_adapter.py` | 0 | 0 | 0 | 0 | 170 | unrun |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | 19 | complete |
| `tests/unit/test_claude_cards.py` | 0 | 0 | 0 | 0 | 96 | unrun |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | 15 | complete |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 0 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_claude_identity.py` | 0 | 0 | 0 | 0 | 30 | unrun |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | 30 | complete |
| `tests/unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 | 32 | complete |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 | 11 | complete |
| `tests/unit/test_claude_stream.py` | 60 | 0 | 0 | 0 | 60 | complete |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 | 63 | complete |
| `tests/unit/test_claude_turn.py` | 61 | 0 | 0 | 0 | 61 | complete |
| `tests/unit/test_claude_usage.py` | 0 | 0 | 0 | 0 | 28 | unrun |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | 6 | complete |
| `tests/unit/test_cli.py` | 0 | 0 | 0 | 0 | 222 | unrun |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | 18 | complete |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | 23 | complete |
| `tests/unit/test_codex_adapter.py` | 0 | 0 | 0 | 0 | 172 | unrun |
| `tests/unit/test_codex_ca_bundle.py` | 0 | 0 | 0 | 0 | 46 | unrun |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | 6 | complete |
| `tests/unit/test_codex_probe.py` | 0 | 0 | 0 | 0 | 55 | unrun |
| `tests/unit/test_codex_shim.py` | 0 | 0 | 0 | 0 | 31 | unrun |
| `tests/unit/test_codex_turn.py` | 58 | 0 | 0 | 0 | 58 | complete |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 | 41 | complete |
| `tests/unit/test_compat.py` | 1615 | 0 | 0 | 0 | 1615 | complete |
| `tests/unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 | 11 | complete |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 | 15 | complete |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 | 21 | complete |
| `tests/unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 | 39 | complete |
| `tests/unit/test_conversation_fifo_reads.py` | 0 | 0 | 0 | 0 | 26 | unrun |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 | 0 | 0 | 48 | complete |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 | 28 | complete |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_conversation_history.py` | 22 | 0 | 0 | 0 | 22 | complete |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 0 | 0 | 0 | 44 | complete |
| `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 | 36 | complete |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_conversation_service.py` | 0 | 0 | 0 | 0 | 98 | unrun |
| `tests/unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 | 18 | complete |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 | 45 | complete |
| `tests/unit/test_conversation_store.py` | 32 | 0 | 0 | 0 | 32 | complete |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 | 13 | complete |
| `tests/unit/test_conversation_titles.py` | 89 | 0 | 0 | 0 | 89 | complete |
| `tests/unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_conversation_wakes.py` | 0 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 | 5 | complete |
| `tests/unit/test_daemon_connections.py` | 31 | 0 | 0 | 0 | 31 | complete |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 | 27 | complete |
| `tests/unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 | 22 | complete |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 | 4 | complete |
| `tests/unit/test_daemon_integration.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_daemon_log_scrubbing.py` | 0 | 0 | 0 | 0 | 86 | unrun |
| `tests/unit/test_daemon_qos_tools.py` | 3 | 1 | 0 | 0 | 4 | complete, failed |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 | 8 | complete |
| `tests/unit/test_daemon_settle.py` | 61 | 0 | 0 | 0 | 61 | complete |
| `tests/unit/test_daemon_verbs.py` | 0 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 | 0 | 0 | 56 | complete |
| `tests/unit/test_descriptors.py` | 15 | 1 | 0 | 0 | 16 | complete, failed |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | 8 | complete |
| `tests/unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 | 3 | complete |
| `tests/unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 | 13 | complete |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | 4 | complete |
| `tests/unit/test_disk_admission.py` | 27 | 0 | 0 | 0 | 27 | complete |
| `tests/unit/test_disk_floor.py` | 60 | 0 | 0 | 0 | 60 | complete |
| `tests/unit/test_doctor.py` | 0 | 0 | 0 | 0 | 44 | unrun |
| `tests/unit/test_doctor_ca_bundle.py` | 0 | 0 | 0 | 0 | 16 | unrun |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 | 13 | complete |
| `tests/unit/test_empty_provider_results.py` | 334 | 0 | 0 | 0 | 334 | complete |
| `tests/unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_filesystem_identity_agreement.py` | 37 | 0 | 3 | 0 | 40 | complete |
| `tests/unit/test_folders.py` | 23 | 0 | 0 | 0 | 23 | complete |
| `tests/unit/test_forget_paced.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_fresh_until.py` | 1 | 0 | 0 | 0 | 1 | complete |
| `tests/unit/test_frozen_provider_streams.py` | 217 | 0 | 0 | 0 | 217 | complete |
| `tests/unit/test_gate_admission.py` | 24 | 0 | 0 | 0 | 24 | complete |
| `tests/unit/test_gate_merge.py` | 0 | 0 | 0 | 0 | 59 | unrun |
| `tests/unit/test_gate_replay.py` | 23 | 0 | 0 | 0 | 23 | complete |
| `tests/unit/test_gate_revision.py` | 16 | 0 | 0 | 0 | 16 | complete |
| `tests/unit/test_gate_service.py` | 0 | 0 | 0 | 0 | 25 | unrun |
| `tests/unit/test_gate_verdict.py` | 45 | 0 | 0 | 0 | 45 | complete |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 | 22 | complete |
| `tests/unit/test_guard_trust.py` | 0 | 0 | 0 | 0 | 98 | unrun |
| `tests/unit/test_guardian.py` | 0 | 0 | 0 | 0 | 12 | unrun |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 | 0 | 14 | complete |
| `tests/unit/test_handler_refusals.py` | 317 | 0 | 0 | 0 | 317 | complete |
| `tests/unit/test_held_waiter_holdback.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_hidden_environment.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_hook_cost.py` | 3 | 0 | 0 | 0 | 3 | complete |
| `tests/unit/test_hooks.py` | 66 | 1 | 0 | 0 | 67 | complete, failed |
| `tests/unit/test_hooks_native_guard.py` | 18 | 0 | 0 | 0 | 18 | complete |
| `tests/unit/test_host_push.py` | 0 | 0 | 0 | 0 | 118 | unrun |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 | 15 | complete |
| `tests/unit/test_host_push_build.py` | 10 | 0 | 1 | 0 | 11 | complete |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 | 15 | complete |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 | 11 | complete |
| `tests/unit/test_host_push_export.py` | 0 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 | 13 | complete |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 | 33 | complete |
| `tests/unit/test_host_push_mutations.py` | 0 | 0 | 0 | 0 | 24 | unrun |
| `tests/unit/test_host_push_names.py` | 0 | 0 | 0 | 0 | 13 | unrun |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 | 8 | complete |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 | 4 | complete |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_host_push_storage_retry_budget.py` | 60 | 0 | 0 | 0 | 60 | complete |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 | 26 | complete |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 | 5 | complete |
| `tests/unit/test_host_push_validated_top.py` | 0 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_importer.py` | 0 | 0 | 0 | 0 | 148 | unrun |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 | 1 | complete |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | 25 | complete |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 0 | 0 | 0 | 3 | complete |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 | 27 | complete |
| `tests/unit/test_lanes_transfer.py` | 0 | 0 | 0 | 0 | 79 | unrun |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 0 | 0 | 0 | 27 | complete |
| `tests/unit/test_launch_env_helpers.py` | 20 | 0 | 0 | 0 | 20 | complete |
| `tests/unit/test_launch_env_mutations.py` | 41 | 0 | 0 | 0 | 41 | complete |
| `tests/unit/test_launch_env_policy.py` | 26 | 0 | 0 | 0 | 26 | complete |
| `tests/unit/test_launch_env_round2.py` | 67 | 0 | 0 | 0 | 67 | complete |
| `tests/unit/test_launch_env_round3.py` | 67 | 0 | 0 | 0 | 67 | complete |
| `tests/unit/test_launch_env_round4.py` | 40 | 0 | 0 | 0 | 40 | complete |
| `tests/unit/test_launch_env_round5.py` | 39 | 0 | 0 | 0 | 39 | complete |
| `tests/unit/test_lease_waiter_selection.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_legacy_hold.py` | 50 | 0 | 6 | 0 | 56 | complete |
| `tests/unit/test_limit_from_model_text.py` | 0 | 0 | 0 | 0 | 95 | unrun |
| `tests/unit/test_live_store_guard.py` | 324 | 0 | 0 | 0 | 324 | complete |
| `tests/unit/test_live_store_guard_round10.py` | 60 | 0 | 0 | 0 | 60 | complete |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 13 | 0 | 0 | 0 | 13 | complete |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 0 | 0 | 0 | 22 | complete |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 0 | 0 | 0 | 8 | complete |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 0 | 0 | 0 | 11 | complete |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 0 | 0 | 0 | 112 | complete |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 0 | 0 | 0 | 50 | complete |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 0 | 0 | 0 | 23 | complete |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 0 | 0 | 0 | 100 | complete |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 0 | 0 | 0 | 4 | complete |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 0 | 0 | 0 | 89 | complete |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 0 | 0 | 0 | 12 | complete |
| `tests/unit/test_live_store_guard_state_root.py` | 22 | 0 | 0 | 0 | 22 | complete |
| `tests/unit/test_lockwatch.py` | 0 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 0 | 0 | 0 | 6 | complete |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 | 5 | complete |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 | 29 | complete |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 | 12 | complete |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 | 28 | complete |
| `tests/unit/test_notice_rendering.py` | 21 | 0 | 0 | 0 | 21 | complete |
| `tests/unit/test_notify_push.py` | 31 | 0 | 0 | 0 | 31 | complete |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 | 33 | complete |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 | 15 | complete |
| `tests/unit/test_operator_notices.py` | 0 | 0 | 0 | 0 | 79 | unrun |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 | 0 | 0 | 58 | complete |
| `tests/unit/test_person_boundary.py` | 97 | 0 | 0 | 0 | 97 | complete |
| `tests/unit/test_person_boundary_identity.py` | 0 | 0 | 0 | 0 | 25 | unrun |
| `tests/unit/test_picker.py` | 0 | 0 | 0 | 0 | 39 | unrun |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 | 26 | complete |
| `tests/unit/test_policy.py` | 178 | 0 | 0 | 0 | 178 | complete |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 | 0 | 57 | complete |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 | 0 | 46 | complete |
| `tests/unit/test_policy_support.py` | 0 | 0 | 0 | 0 | 14 | unrun |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 | 15 | complete |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 | 10 | complete |
| `tests/unit/test_pr124_review_r2_fixes.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 | 21 | complete |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_procs.py` | 0 | 0 | 0 | 0 | 47 | unrun |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 0 | 0 | 322 | complete |
| `tests/unit/test_provider_active_cap.py` | 50 | 0 | 0 | 0 | 50 | complete |
| `tests/unit/test_prune_217.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 0 | 0 | 0 | 23 | complete |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 | 15 | complete |
| `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 | 0 | 48 | complete |
| `tests/unit/test_prune_restore.py` | 20 | 0 | 0 | 0 | 20 | complete |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 | 28 | complete |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 | 3 | complete |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 | 17 | complete |
| `tests/unit/test_relay.py` | 24 | 0 | 0 | 0 | 24 | complete |
| `tests/unit/test_render.py` | 0 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_render_quota_projection.py` | 0 | 0 | 0 | 0 | 23 | unrun |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 | 5 | complete |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_reset_credit_demand.py` | 68 | 0 | 0 | 0 | 68 | complete |
| `tests/unit/test_reset_credit_properties.py` | 0 | 0 | 0 | 0 | 11 | unrun |
| `tests/unit/test_reset_credit_review_r1.py` | 0 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_reset_credit_review_r2.py` | 0 | 0 | 0 | 0 | 52 | unrun |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 | 23 | complete |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 | 12 | complete |
| `tests/unit/test_resource_identity.py` | 10 | 0 | 0 | 0 | 10 | complete |
| `tests/unit/test_retention_81_notes.py` | 28 | 0 | 0 | 0 | 38 | partial |
| `tests/unit/test_retention_archive.py` | 0 | 0 | 0 | 0 | 109 | unrun |
| `tests/unit/test_retention_archive_properties.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_retention_case_alias.py` | 0 | 0 | 0 | 0 | 56 | unrun |
| `tests/unit/test_retention_nested_leases.py` | 27 | 0 | 0 | 0 | 27 | complete |
| `tests/unit/test_retention_ordinary_trees.py` | 0 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_retention_qos.py` | 0 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 | 0 | 37 | complete |
| `tests/unit/test_retention_rediscovery.py` | 17 | 0 | 0 | 0 | 17 | complete |
| `tests/unit/test_retention_regenerable.py` | 0 | 0 | 0 | 0 | 26 | unrun |
| `tests/unit/test_retention_regenerable_properties.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_retention_shared_folders.py` | 0 | 0 | 0 | 0 | 62 | unrun |
| `tests/unit/test_retention_sweep_race.py` | 0 | 0 | 0 | 0 | 13 | unrun |
| `tests/unit/test_retention_turn_contract.py` | 2 | 0 | 0 | 0 | 2 | complete |
| `tests/unit/test_retention_turn_workdirs.py` | 1 | 0 | 0 | 0 | 1 | complete |
| `tests/unit/test_retention_turns.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_retention_worktrees.py` | 0 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 | 21 | complete |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 | 12 | complete |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 | 6 | complete |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_review_pr127_report.py` | 1 | 0 | 0 | 0 | 1 | complete |
| `tests/unit/test_review_pr127_requests.py` | 17 | 0 | 0 | 0 | 17 | complete |
| `tests/unit/test_review_pr127_service.py` | 0 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_review_r2_repro.py` | 0 | 0 | 0 | 0 | 31 | unrun |
| `tests/unit/test_review_r3_measure.py` | 6 | 0 | 0 | 0 | 6 | complete |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 | 12 | complete |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 | 27 | complete |
| `tests/unit/test_review_r5_repro.py` | 17 | 0 | 0 | 0 | 17 | complete |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 | 30 | complete |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 | 5 | complete |
| `tests/unit/test_round10_refusal_context.py` | 0 | 0 | 0 | 0 | 59 | unrun |
| `tests/unit/test_route_check.py` | 0 | 0 | 0 | 0 | 24 | unrun |
| `tests/unit/test_salvage.py` | 0 | 0 | 0 | 0 | 31 | unrun |
| `tests/unit/test_salvage_optional_transient.py` | 0 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_salvage_unindexable.py` | 0 | 0 | 0 | 0 | 86 | unrun |
| `tests/unit/test_scheduler.py` | 0 | 0 | 0 | 0 | 97 | unrun |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_scheduler_split.py` | 3 | 0 | 0 | 0 | 3 | complete |
| `tests/unit/test_scheduler_tier_preferences.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/unit/test_scheduler_turns.py` | 10 | 0 | 0 | 0 | 10 | complete |
| `tests/unit/test_scheduler_weekly.py` | 0 | 0 | 0 | 0 | 33 | unrun |
| `tests/unit/test_scoped_stream_capacity.py` | 17 | 0 | 0 | 0 | 17 | complete |
| `tests/unit/test_scrub_key_order.py` | 0 | 0 | 0 | 0 | 70 | unrun |
| `tests/unit/test_session_credential_env.py` | 30 | 0 | 0 | 0 | 30 | complete |
| `tests/unit/test_sessions_cli.py` | 0 | 0 | 0 | 0 | 142 | unrun |
| `tests/unit/test_sessions_handoff.py` | 0 | 0 | 0 | 0 | 117 | unrun |
| `tests/unit/test_sessions_handoff_excerpts.py` | 0 | 0 | 0 | 0 | 153 | unrun |
| `tests/unit/test_sessions_headless_properties.py` | 5 | 0 | 0 | 0 | 5 | complete |
| `tests/unit/test_sessions_mirror.py` | 41 | 0 | 0 | 0 | 41 | complete |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 4 | 0 | 0 | 0 | 4 | complete |
| `tests/unit/test_sessions_mirror_fifo.py` | 0 | 0 | 0 | 0 | 49 | unrun |
| `tests/unit/test_sessions_mirror_hot_health.py` | 6 | 0 | 0 | 0 | 6 | complete |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 0 | 0 | 0 | 0 | 16 | unrun |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 0 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_sessions_mirror_inventory.py` | 8 | 0 | 0 | 0 | 8 | complete |
| `tests/unit/test_sessions_mirror_load_gap.py` | 0 | 0 | 0 | 0 | 85 | unrun |
| `tests/unit/test_sessions_mirror_safety.py` | 0 | 0 | 0 | 0 | 28 | unrun |
| `tests/unit/test_sessions_mirror_write_churn.py` | 0 | 0 | 0 | 0 | 3 | unrun |
| `tests/unit/test_sessions_nudge.py` | 0 | 0 | 0 | 0 | 44 | unrun |
| `tests/unit/test_sessions_registry.py` | 24 | 0 | 0 | 0 | 24 | complete |
| `tests/unit/test_sessions_revive.py` | 0 | 0 | 0 | 0 | 45 | unrun |
| `tests/unit/test_sessions_revive_reads.py` | 0 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_sessions_transcripts.py` | 71 | 0 | 0 | 0 | 71 | complete |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 | 0 | 129 | complete |
| `tests/unit/test_soak_tools.py` | 0 | 0 | 0 | 0 | 49 | unrun |
| `tests/unit/test_status_json.py` | 0 | 0 | 0 | 0 | 56 | unrun |
| `tests/unit/test_steer_invariants.py` | 248 | 0 | 0 | 0 | 248 | complete |
| `tests/unit/test_stop_alarm.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_stop_watchdog.py` | 0 | 0 | 0 | 0 | 29 | unrun |
| `tests/unit/test_store_contention_queries.py` | 0 | 0 | 0 | 0 | 212 | unrun |
| `tests/unit/test_store_ids.py` | 27 | 0 | 0 | 0 | 27 | complete |
| `tests/unit/test_store_migration_2.py` | 10 | 0 | 0 | 0 | 10 | complete |
| `tests/unit/test_store_properties.py` | 3 | 0 | 0 | 0 | 3 | complete |
| `tests/unit/test_store_readers.py` | 0 | 0 | 0 | 0 | 26 | unrun |
| `tests/unit/test_store_snapshot_plans.py` | 4 | 0 | 0 | 0 | 4 | complete |
| `tests/unit/test_suggest_cli.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_suggest_hint.py` | 0 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_suggestion_error_boundaries.py` | 0 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_suggestion_properties.py` | 1 | 0 | 0 | 0 | 1 | complete |
| `tests/unit/test_suggestion_secret_properties.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_suggestion_structural_errors.py` | 0 | 0 | 0 | 0 | 40 | unrun |
| `tests/unit/test_suggestions.py` | 30 | 0 | 0 | 0 | 30 | complete |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 0 | 0 | 0 | 0 | 30 | unrun |
| `tests/unit/test_suggestions_r3_channels.py` | 0 | 0 | 0 | 0 | 14 | unrun |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 0 | 0 | 0 | 7 | complete |
| `tests/unit/test_suggestions_review_multiline.py` | 6 | 0 | 0 | 0 | 6 | complete |
| `tests/unit/test_suggestions_review_response.py` | 3 | 0 | 0 | 0 | 3 | complete |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | 1 | complete |
| `tests/unit/test_tick_prefilter.py` | 30 | 0 | 0 | 0 | 30 | complete |
| `tests/unit/test_timers_alerts.py` | 0 | 0 | 0 | 0 | 17 | unrun |
| `tests/unit/test_timers_busy_read.py` | 0 | 0 | 0 | 0 | 115 | unrun |
| `tests/unit/test_timers_daemon.py` | 0 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_timers_keepalive.py` | 14 | 0 | 0 | 0 | 14 | complete |
| `tests/unit/test_timers_probe.py` | 0 | 0 | 0 | 0 | 35 | unrun |
| `tests/unit/test_timers_reset_credits.py` | 0 | 0 | 0 | 0 | 47 | unrun |
| `tests/unit/test_timers_retention.py` | 20 | 0 | 0 | 0 | 20 | complete |
| `tests/unit/test_title_review_regressions.py` | 4 | 0 | 0 | 0 | 4 | complete |
| `tests/unit/test_turn_attachment_replay.py` | 13 | 0 | 0 | 0 | 13 | complete |
| `tests/unit/test_turn_overlap.py` | 10 | 0 | 0 | 0 | 10 | complete |
| `tests/unit/test_turn_replay.py` | 33 | 0 | 0 | 0 | 33 | complete |
| `tests/unit/test_turn_runner.py` | 76 | 0 | 0 | 0 | 76 | complete |
| `tests/unit/test_ultracode_effort.py` | 39 | 0 | 0 | 0 | 39 | complete |
| `tests/unit/test_unadmittable_pin.py` | 0 | 0 | 0 | 0 | 12 | unrun |
| `tests/unit/test_wait_hub.py` | 44 | 0 | 0 | 0 | 44 | complete |
| `tests/unit/test_wake_cli.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_weekly_rank_vs_base.py` | 0 | 0 | 0 | 0 | 26 | unrun |

### Every test file in tests/fake

| File | Passed | Failed | Skipped | Xfailed | Collected | Status |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | 27 | complete |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | 0 | 1 | complete |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | 0 | 14 | complete |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | 0 | 16 | complete |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | 0 | 32 | complete |
| `tests/fake/test_admission_priority.py` | 0 | 0 | 0 | 0 | 32 | unrun |
| `tests/fake/test_admission_properties.py` | 27 | 0 | 0 | 1 | 28 | complete |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 | 19 | complete |
| `tests/fake/test_admission_ramp_retention.py` | 0 | 0 | 0 | 0 | 11 | unrun |
| `tests/fake/test_admission_route_isolation.py` | 93 | 0 | 0 | 0 | 93 | complete |
| `tests/fake/test_admission_stateful.py` | 16 | 0 | 0 | 0 | 16 | complete |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | 0 | 0 | 43 | complete |
| `tests/fake/test_batch_label.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/fake/test_canonical_identity.py` | 24 | 0 | 0 | 0 | 24 | complete |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 0 | 0 | 0 | 21 | complete |
| `tests/fake/test_canonical_identity_fix3.py` | 17 | 0 | 0 | 1 | 18 | complete |
| `tests/fake/test_canonical_identity_fix4.py` | 11 | 0 | 0 | 0 | 11 | complete |
| `tests/fake/test_canonical_identity_fix5.py` | 0 | 0 | 0 | 0 | 14 | unrun |
| `tests/fake/test_capacity_steer_daemon.py` | 36 | 0 | 0 | 0 | 54 | partial |
| `tests/fake/test_capacity_wait_backoff.py` | 0 | 0 | 0 | 0 | 30 | unrun |
| `tests/fake/test_census_escapers_daemon.py` | 0 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_census_recycled_pid_daemon.py` | 0 | 0 | 0 | 0 | 29 | unrun |
| `tests/fake/test_codex_ca_bundle_fake.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_codex_turn_completed_fake.py` | 0 | 0 | 0 | 0 | 109 | unrun |
| `tests/fake/test_conversation_pools.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_conversation_wakes_fake.py` | 0 | 0 | 0 | 0 | 4 | unrun |
| `tests/fake/test_crash_matrix.py` | 0 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_daemon_contract.py` | 0 | 0 | 0 | 0 | 21 | unrun |
| `tests/fake/test_daemon_descriptors.py` | 0 | 0 | 0 | 0 | 13 | unrun |
| `tests/fake/test_daemon_launch.py` | 9 | 0 | 0 | 0 | 9 | complete |
| `tests/fake/test_daemon_stacks.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_descriptor_exhaustion.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_detached_cwd_pins.py` | 0 | 0 | 0 | 0 | 18 | unrun |
| `tests/fake/test_detached_retention_fence.py` | 0 | 0 | 0 | 0 | 89 | unrun |
| `tests/fake/test_enrollment_guardian.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_fake_provider.py` | 0 | 0 | 0 | 0 | 13 | unrun |
| `tests/fake/test_finalization_replay.py` | 0 | 0 | 0 | 0 | 10 | unrun |
| `tests/fake/test_folder_spelling.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_frozen_stream_nonregular.py` | 0 | 0 | 0 | 0 | 140 | unrun |
| `tests/fake/test_gate_end_to_end.py` | 0 | 0 | 0 | 0 | 11 | unrun |
| `tests/fake/test_identity_rollback.py` | 0 | 0 | 0 | 0 | 5 | unrun |
| `tests/fake/test_idle_cost.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_interactive_claude_titles.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_interactive_steer.py` | 0 | 0 | 0 | 0 | 13 | unrun |
| `tests/fake/test_lane_fault.py` | 0 | 0 | 0 | 0 | 20 | unrun |
| `tests/fake/test_lane_pilot.py` | 0 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_lanes_enroll.py` | 0 | 0 | 0 | 0 | 15 | unrun |
| `tests/fake/test_lanes_reenroll.py` | 0 | 0 | 0 | 0 | 19 | unrun |
| `tests/fake/test_lanes_reenroll_keychain.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_launch_env_daemon.py` | 0 | 0 | 0 | 0 | 49 | unrun |
| `tests/fake/test_launch_env_providers.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_launch_gates_above_stdio.py` | 0 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_lost_acknowledgement.py` | 0 | 0 | 0 | 0 | 3 | unrun |
| `tests/fake/test_main_refusal_write_dir.py` | 0 | 0 | 0 | 0 | 5 | unrun |
| `tests/fake/test_mcp_job_contract.py` | 0 | 0 | 0 | 0 | 12 | unrun |
| `tests/fake/test_native_maintenance_startup.py` | 0 | 0 | 0 | 0 | 41 | unrun |
| `tests/fake/test_notice_delivery.py` | 0 | 0 | 0 | 0 | 15 | unrun |
| `tests/fake/test_notice_header.py` | 0 | 0 | 0 | 0 | 23 | unrun |
| `tests/fake/test_picker_native.py` | 0 | 0 | 0 | 0 | 3 | unrun |
| `tests/fake/test_policy_sandbox.py` | 0 | 0 | 0 | 0 | 25 | unrun |
| `tests/fake/test_probe_cancel.py` | 0 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_probe_recovery.py` | 0 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_probe_turn.py` | 0 | 0 | 0 | 0 | 43 | unrun |
| `tests/fake/test_prompt_seam.py` | 0 | 0 | 0 | 0 | 8 | unrun |
| `tests/fake/test_provider_active_cap_fake.py` | 0 | 0 | 0 | 0 | 60 | unrun |
| `tests/fake/test_provider_cap_review_evidence.py` | 0 | 0 | 0 | 0 | 5 | unrun |
| `tests/fake/test_provider_qos.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_record_secrecy.py` | 0 | 0 | 0 | 0 | 44 | unrun |
| `tests/fake/test_request_routing.py` | 0 | 0 | 0 | 0 | 3 | unrun |
| `tests/fake/test_reserve_authorization.py` | 0 | 0 | 0 | 0 | 12 | unrun |
| `tests/fake/test_reserve_authorization_validation.py` | 0 | 0 | 0 | 0 | 11 | unrun |
| `tests/fake/test_reset_credit_incident.py` | 0 | 0 | 0 | 0 | 19 | unrun |
| `tests/fake/test_resume_cli.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_resume_contract.py` | 0 | 0 | 0 | 0 | 23 | unrun |
| `tests/fake/test_retention_dormant.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_review130_ab.py` | 0 | 0 | 0 | 0 | 12 | unrun |
| `tests/fake/test_review_r185_adversarial.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_route_view.py` | 0 | 0 | 0 | 0 | 8 | unrun |
| `tests/fake/test_routing_end_to_end.py` | 0 | 0 | 0 | 0 | 16 | unrun |
| `tests/fake/test_routing_tier_preferences.py` | 0 | 0 | 0 | 0 | 5 | unrun |
| `tests/fake/test_salvage_adversarial.py` | 0 | 0 | 0 | 0 | 8 | unrun |
| `tests/fake/test_salvage_finalization.py` | 0 | 0 | 0 | 0 | 47 | unrun |
| `tests/fake/test_sessions_end_to_end.py` | 0 | 0 | 0 | 0 | 33 | unrun |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | 37 | complete |
| `tests/fake/test_state_fixture_processes.py` | 0 | 0 | 0 | 0 | 4 | unrun |
| `tests/fake/test_status_payload.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_suggestions_fake.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_tier_hold_by_demand.py` | 0 | 0 | 0 | 0 | 27 | unrun |
| `tests/fake/test_timers_busy_admission.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_timers_end_to_end.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_turn_listing.py` | 0 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_turn_retention_cwd.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_turn_retention_cwd_properties.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_turn_retention_job_directory.py` | 0 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_turn_retention_job_properties.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_turn_retention_quarantine.py` | 0 | 0 | 0 | 0 | 10 | unrun |
| `tests/fake/test_turn_trees.py` | 0 | 0 | 0 | 0 | 18 | unrun |
| `tests/fake/test_turn_wait_reasons.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_unadmittable_pin_admission.py` | 0 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_unadmittable_pin_rereview.py` | 0 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_unadmittable_pin_review.py` | 0 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_workspace_contract.py` | 0 | 0 | 0 | 0 | 8 | unrun |
| `tests/fake/test_workspace_enospc.py` | 0 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_workspace_transient.py` | 0 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_writable_fanout.py` | 0 | 0 | 0 | 0 | 28 | unrun |
| `tests/fake/test_writer_retention_fence.py` | 0 | 0 | 0 | 0 | 23 | unrun |


## Commits

- `08dc3eb13` — test: the restore recipe's block stays whole and no report prints a credential (D-PRR1)
- `5362f6317` — fix: fence the restore recipe's block with a run no backup path can close (D-PRR1)
- `c90fbfb0c` — fix: a test session removes credential-shaped variables until cleanup (D-PRR1)
- `0b186e1ec` — docs: C-11.5 and C-20.6 cover the recipe's fence and the session's credential removal (D-PRR1)
- `b43b4f191` — test: read -vv's pretty-printed locals over several lines (D-PRR1)
- `b06b7b66c` — test: round-three host-push checks set their own agent socket (D-PRR1)
- `8c2e9de04` — test: check the child report for leaks before its outcome (D-PRR1)

The shared index rejected staging with “Operation not permitted.” As authorized in the brief, the final documentation commit uses workspace-local Git metadata at `.prr1-git/`, on the local branch `fix/prune-restore-recipe-217`, with history descended from checkpoint `8c2e9de04`. The supplied shared detached HEAD is unchanged. Review the complete history with `git --git-dir=.prr1-git log --oneline 2eacd580605e..HEAD`. This metadata remains in the assigned workspace; the scratch cleanup does not remove it.

Documentation commit `77ecaa595` records the reconciled report and PR body; a follow-up corrects their formatting and count explanation. Every commit uses the requested co-author trailer. No history was rewritten and nothing was pushed.
