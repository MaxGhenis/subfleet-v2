# Codex shell snapshot and cross-vendor credentials (D-ENV3)

Base: `release/217`, `2c981cba5` (merged forward to `cf1f78d80`). Reported on 2026-10-27 after Subfleet job `20261026-230224-hub-rev-axiom-encode-2011` (lane `codex-4`, GPT-6.1 Sol, `workspace-write` with network). A failed test in that job printed its process environment. The printout held `ANTHROPIC_API_KEY`, so the key reached that run's stdout, its frozen stream and OpenAI as model input. Rotation is the person's decision (d2083). This investigation never read, printed or handled a credential value. Every check below reads variable names only, and every credential in the experiments and tests is a dummy.

## What the daemon passed

The daemon's selection was not the leak. `Daemon._launch_environment` builds every provider launch from D-ENV1's allowlist plus the launch's own additions. It then removes `CODEX_API_KEY`, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` (`subfleet/daemon.py`). The credential-word backstop drops any other name holding `KEY`, `TOKEN` and the like. The live release (`20261022T162633Z`, source `4f5983f4`) includes #222 (D-ENV1). The live policy has no `env.pass`.

The leaked printout itself carries names that no Subfleet launch selects. Besides `ANTHROPIC_API_KEY` it shows `BUN_INSTALL`, which is neither allowlisted nor a credential and is exported only by the person's `~/.zshrc` (not by `~/.zshenv` or `~/.zprofile`).

## Root cause: Codex's shell snapshot

`codex features list` (codex-cli 0.159.0, the version the incident job's guard preflight recorded) reports `shell_snapshot  stable  true`. The feature is on by default. With it, Codex runs the person's shell startup, including `~/.zshrc`, and saves what it exports to `$CODEX_HOME/shell_snapshots/<thread>.sh`. Before every command it replays that file. The replay happens after Subfleet's selection and after Codex's own `shell_environment_policy`, whose default excludes (`*KEY*`, `*TOKEN*`, `*SECRET*`) apply only to the process environment it hands the shell.

Evidence, names only:

- `codex-4`'s home held three leftover snapshots (threads that never closed cleanly, 2026-10-24 and 2026-10-25). Each exports 59 to 60 names. Twenty-one hold a credential word by `launch_env.CREDENTIAL_PATTERN`. Among them are `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` and `OPENAI_API_KEY`, the last of which Subfleet removes from every launch.
- A mock-API experiment ran the real codex 0.159.0 binary against a Responses server on 127.0.0.1. HOME was a throwaway folder whose `.zshrc` exports a dummy, and no real credential was present. The model's one shell call printed the variable.

| Codex configuration | Command saw the `.zshrc`-only dummy |
| --- | --- |
| defaults | yes |
| `features.unified_exec=false` (the incident job's setting) | yes |
| `features.shell_snapshot=false` | no |
| `features.shell_snapshot=false`, `features.unified_exec=false` | no |
| `app-server` (conversation turns), defaults | yes |
| `app-server` with the three D-ENV3 keys | no |

The `codex exec` rows were run with the model slugs `mock-model` and `gpt-6.1-sol`, and the results matched. An isolated review was already safe: `adapters/isolation.py` disabled `shell_snapshot` and `experimental_use_profile` for C-23's metadata isolation. Ordinary dispatches, reviews, resumes and conversation turns did not.

## Claude lanes

Claude lanes have no matching hole today, for two reasons.

- **Process environment.** The same `_launch_environment` removes `OPENAI_API_KEY` and `CODEX_API_KEY` from every launch, Claude's included, and the backstop drops every other credential word.
- **Bash tool.** Claude Code's shell snapshots export only `PATH`. Of 534 snapshots in `~/.claude/shell-snapshots`, 469 export `PATH` and none exports any other name. The same mock-API experiment ran the real Claude Code 2.1.284 binary against a Messages server: its Bash tool printed neither a `.zshrc`-only dummy `OPENAI_API_KEY` nor a second `.zshrc` sentinel.

D-ENV3's vendor rule (below) makes this explicit for `env.pass` and launch additions. `tests/process/test_provider_shell_environment.py` pins Claude Code's behaviour, so a later Claude Code that starts to replay exports fails the suite.

## Change

1. **Codex shell configuration.** `launch_env.CODEX_SHELL_CONFIG` holds `features.shell_snapshot=false`, `features.shell_snapshot_v2=false` and `shell_environment_policy.experimental_use_profile=false`. It is passed as `-c` pairs on every Codex launch that runs a model turn: `codex exec`, `codex exec resume`, and the `app-server` a conversation turn runs. An isolated review's `CODEX_CONFIG` includes the same keys, once each. `shell_snapshot_v2` is the snapshot's successor, under development and off in 0.159.0, and Codex accepts a `features.*` key it does not know (`codex -c features.no_such_feature_xyz=false features list` exits 0). The guard preflight's `app-server` and the isolation inspection start no thread and run no command, so they are unchanged.
2. **Vendor rule (D-ENV3).** A name is a vendor's credential when it holds that vendor's word (`OPENAI` or `CODEX` for Codex, `ANTHROPIC` or `CLAUDE` for Claude, in any case) and a credential word. `ProviderEnvironment.assemble(..., provider=P)` drops every other vendor's credential last, even one `env.pass` or the launch's own additions name. Without a provider, it keeps a vendor credential only as the caller's own addition, never from the parent or `env.pass`. `Daemon._launch_environment`, `ProviderEnvironment.build` and the three guard-preflight environments pass the lane's provider. Homes (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`) hold no credential word, so a test session's children keep inheriting them (D-ST1). No allowlisted, test-session or launch-owned name is a vendor credential, and a test holds that.

The process environment already had an explicit allowlist (D-ENV1), so this change adds no second allowlist. The gap was that Codex rebuilt the command's environment from the person's startup files after the allowlist had run. Turning that off makes D-ENV1's allowlist the environment a command sees.

## Invariants

- **I1.** For every parent environment, `env.pass` list and launch path (detached, resume, turn, probe, isolated; both providers), the launch environment equals D-ENV1's selection plus the launch's additions, minus mandatory removals and minus every other vendor's credential. An oracle independent of `launch_env` decides the expected set. Hypothesis checks this: `test_property_every_launch_is_the_allowlist_minus_foreign_vendors`, plus the updated `test_bounded_property_all_lane_launches`.
- **I2.** `foreign_credentials` equals the oracle on every name list, for a known provider and for an unknown one. Hypothesis checks this.
- **I3.** Every `codex exec` launch carries each `CODEX_SHELL_CONFIG` key among its options, before `resume`, with no later `-c` setting that key or a table holding it. Checked exhaustively over sandbox × review task × network × resume × unified-exec switch (32 cases), plus the isolated review and both conversation-turn forms. The checker has its own refusal tests.
- **I4.** Differential, against the real binaries. With Subfleet's argv minus `CODEX_SHELL_CONFIG`, the `.zshrc` dummy reaches the command whenever the installed Codex has the snapshot on by default. With the launch's argv, neither the `.zshrc` dummy nor the daemon parent's `ANTHROPIC_API_KEY` reaches a command of `codex exec`, its resume, or `app-server`.

## Not covered

- **Login-shell startup files.** Codex still runs commands as a login shell (`zsh -lc`), which reads `~/.zshenv`, `~/.zprofile` and `~/.zlogin`. On this host they export no credential (checked by name): Homebrew's variables and `PATH`. A credential exported from any of them would reach lane commands again. Credential exports belong in `~/.zshrc`, which a non-interactive shell does not read.
- **Reads outside the environment.** This change governs the environment a command inherits. It does not stop a command from reading files or stores the sandbox lets it read; that boundary is C-14.4's.
- **Leftover snapshot files.** Snapshots written before this change stay in each Codex lane home's `shell_snapshots/` until removed, and they hold the exported values in plain text (mode 0600). The fix stops new ones. Removing the old ones is the person's step.

## Verification

All runs were local, serial, under the hub's suite lock, with a private `TMPDIR`. They used codex-cli 0.159.0 and Claude Code 2.1.284.

- **The six files this change adds or edits:** `test_launch_env_vendor_shell.py`, `test_provider_shell_environment.py`, `test_daemon_launch.py`, `test_codex_turn.py`, `test_launch_env_policy.py` and `test_launch_env_allowlist.py`. Result: 206 passed. The three real-binary process tests ran and were not skipped: Codex exec plus resume, the Codex conversation turn, and Claude Code's Bash.
- **The related suite:** the 66 test files that import a changed module or test launch environments, plus the two new files. Result: 4,100 passed, 4 xfailed.
- **Four existing tests were updated** for the new behaviour:
  - `test_daemon_launch` C-5.1 now checks that a Codex lane keeps its own vendor's token and drops a Claude token;
  - `test_launch_env_policy`'s exact-pass case now uses the name's own vendor, and checks that the other vendor's build drops a vendor credential;
  - the two `test_codex_turn` argv tests now expect the shell keys at the end.
- **A names-only scan of 10,315 retained Codex job stdouts.** It found one environment printout that holds a credential name: the incident job's. Other hits were code or diffs that mention a name.
- **CI** runs on the pull request.
