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

Claude lanes have no matching *snapshot* hole, for two reasons.

- **Process environment.** The same `_launch_environment` removes `OPENAI_API_KEY` and `CODEX_API_KEY` from every launch, Claude's included, and the backstop drops every other credential word.
- **Bash tool.** Claude Code's shell snapshots export only `PATH`. Of 534 snapshots in `~/.claude/shell-snapshots`, 469 export `PATH` and none exports any other name. The same mock-API experiment ran the real Claude Code 2.1.284 binary against a Messages server: its Bash tool printed neither a `.zshrc`-only dummy `OPENAI_API_KEY` nor a second `.zshrc` sentinel.

D-ENV3's vendor rule (below) makes this explicit for `env.pass` and launch additions. `tests/process/test_provider_shell_environment.py` pins Claude Code's `.zshrc` behaviour, so a later Claude Code that starts to replay `.zshrc` exports fails the suite.

Claude Code does inject variables after the launch through two other channels: its settings' `env` block, and a `CLAUDE_ENV_FILE`. Review r1 ran both against Claude Code 2.1.284, and the dummies reached Bash. A writable Claude launch loads the user's settings, so whatever that `env` names reaches its commands, and no Subfleet selection removes it. On this host the user settings' `env` names credential-word variables (checked by name), including another vendor's key. Moving them out is the person's step; see "Not covered".

## Change

1. **Codex shell configuration.** `launch_env.CODEX_SHELL_CONFIG` holds `features.shell_snapshot=false`, `features.shell_snapshot_v2=false` and `shell_environment_policy.experimental_use_profile=false`. It is passed as `-c` pairs on every Codex launch that runs a model turn: `codex exec`, `codex exec resume`, and the `app-server` a conversation turn runs. An isolated review's `CODEX_CONFIG` includes the same keys, once each. `shell_snapshot_v2` is the snapshot's successor, under development and off in 0.159.0, and Codex accepts a `features.*` key it does not know (`codex -c features.no_such_feature_xyz=false features list` exits 0). The guard preflight's `app-server` and the isolation inspection start no thread and run no command. The preflight is unchanged. The inspection's shared configuration gains the v2 key, through `CODEX_CONFIG`. Review r1 started both against the real binary under a throwaway `CODEX_HOME`, with and without the keys, and found no snapshot file either way.
2. **Vendor rule (D-ENV3).** A name is a vendor's credential when it holds that vendor's word (`OPENAI` or `CODEX` for Codex, `ANTHROPIC` or `CLAUDE` for Claude, in any case) and a credential word. A name holding any vendor's word other than the launch's own is foreign, so a name holding both vendors' words reaches neither (review r1, P2-1). Three places drop foreign credentials:
   - `ProviderEnvironment.assemble(..., provider=P)` drops every other vendor's credential last, even one `env.pass` or the launch's own additions name. Without a provider, it keeps a vendor credential only as one of the caller's own additions.
   - `ProviderEnvironment.additions(P, ...)` drops them at the source (review r1, P2-2), so nothing selected from a provider's additions can carry one, including a later provider-less assembly.
   - The isolated review's metadata inspection, both the adapter's environment and `isolation.inspect_codex`'s own, selects as a Codex process (P2-2).

   `Daemon._launch_environment`, `ProviderEnvironment.build` and the three guard-preflight environments pass the lane's provider. Homes (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`) hold no credential word, so a test session's children keep inheriting them (D-ST1). No allowlisted, test-session or launch-owned name is a vendor credential, and a test holds that.

The process environment already had an explicit allowlist (D-ENV1), so this change adds no second allowlist. The gap was that Codex rebuilt the command's environment from the person's startup files after the allowlist had run. Turning that off stops the `~/.zshrc` replay. A command still sees D-ENV1's selection plus whatever the login-shell startup files export (see "Not covered").

## Invariants

- **I1.** For every parent environment, `env.pass` list and launch path (detached, resume, turn, probe, isolated; both providers), the launch environment equals D-ENV1's selection plus the launch's additions, minus mandatory removals and minus every other vendor's credential. An oracle independent of `launch_env` decides the expected set. Hypothesis checks this: `test_property_every_launch_is_the_allowlist_minus_foreign_vendors`, plus the updated `test_bounded_property_all_lane_launches`.
- **I2.** `foreign_credentials` equals the oracle on every name list, for a known provider and for an unknown one. Hypothesis checks this. Since review r1, both generators also draw names holding both vendors' words, which r1's counterexample showed they had missed.
- **I3.** Every `codex exec` launch carries each `CODEX_SHELL_CONFIG` key among its options, before `resume`. No later `-c` or `--config`, in any spelling the CLI accepts (`-c V`, `-cV`, `-c=V`, `--config V`, `--config=V`) and wherever it stands, `resume`'s own options included, sets that key or a table holding it. No `--enable` anywhere names a shell feature: review r1 showed with the real binary that `--enable shell_snapshot` beats a `-c ...=false` from either side. Checked exhaustively over sandbox × review task × network × resume × unified-exec switch (32 cases), plus the isolated review and both conversation-turn forms. The checker has its own refusal tests.
- **I4.** Differential, against the real binaries. With Subfleet's argv minus `CODEX_SHELL_CONFIG`, the `.zshrc` dummy reaches the command whenever the installed Codex has the snapshot on by default. With the launch's argv, neither the `.zshrc` dummy nor the daemon parent's `ANTHROPIC_API_KEY` reaches a command of `codex exec`, its resume, or `app-server`.

## Not covered

- **Login-shell startup files.** Codex still runs commands as a login shell (`zsh -lc`), which reads `~/.zshenv`, `~/.zprofile` and `~/.zlogin`. On this host they export no credential (checked by name): Homebrew's variables and `PATH`. A credential exported from any of them would reach lane commands again. Credential exports belong in `~/.zshrc`, which a non-interactive shell does not read.
- **Reads outside the environment.** This change governs the environment a command inherits. It does not stop a command from reading files or stores the sandbox lets it read; that boundary is C-14.4's.
- **Claude Code's own injection.** A writable Claude launch loads the user's settings, and Claude Code adds its `env` block and a `CLAUDE_ENV_FILE`'s exports after the launch (review r1).
- **`--enable` flags, later overrides and the PATH shim.** No managed launch passes `--enable` or a later `-c`/`--config` that sets a shell key; the tests' argv checker refuses both. The repository's `bin/codex` PATH shim forwards its caller's argv unchanged and lies outside the daemon's launches. A person's own interactive `codex` keeps Codex's defaults, snapshot included.
- **Name heuristics.** The vendor rule reads names, so a few cases fall outside it:
   - a vendor word split by a separator (`OPEN_AI_API_KEY`) is not recognized;
   - a generic alias (`MY_LLM_KEY`) belongs to no vendor;
   - a value stored under a non-credential name is not caught.

   D-ENV1's word list also matches `PAT` inside `PATH`, so a vendor-named path such as `CODEX_CACHE_PATH` counts as that vendor's credential. It then leaves only the other vendor's launches, and no current lane needs one there.
- **Leftover snapshot files.** Snapshots written before this change stay in each Codex lane home's `shell_snapshots/` until removed, and they hold the exported values in plain text (mode 0600). The fix stops new ones. Removing the old ones is the person's step.

## Review r1 (GPT-6.1 Sol, `20261027-050331-pr270-review-r1`, REQUEST_CHANGES)

| Finding | Disposition |
| --- | --- |
| P2-1: a name holding both vendors' words passed either provider's filter | Fixed in `foreign_credentials`. The reviewer's regression is in `test_a_both_vendor_credential_reaches_neither_vendor`, run through pass and addition for both providers. Both Hypothesis generators now draw such names. |
| P2-2: the isolated review's inspection selected from unfiltered additions | Fixed at the source (`ProviderEnvironment.additions`), in the adapter and in `inspect_codex`. Tests cover each layer on its own, and a mutant that drops only `inspect_codex`'s filter fails. |
| P3: C-12.3 said a command's environment equals the launch's | Reworded. Login-shell startup files still run, which the reviewer showed with a `.zprofile` dummy. |
| P3: the PR body's stopgap appended a second `[features]` table | Reworded to set the keys inside an existing `[features]` table, and to add the table only where none exists. |
| Check 2: `--enable shell_snapshot` beats the pins | The argv checker now refuses it anywhere; no managed launch passes one. Listed under "Not covered". |
| Check 4: Claude settings `env` and `CLAUDE_ENV_FILE` | The Claude claim is now confined to `.zshrc`; the channel is listed under "Not covered". |
| Check 6: other wording | The isolation-inspection sentence and the PATH-shim scope are corrected. |

The four new test families hold 30 cases. Against `4179f7c94`'s code, the head r1 reviewed, 29 fail and both amended properties fail (31 failed). The one that passes, `test_inspect_codex_selects_as_a_codex_process[ANTHROPIC_API_KEY]`, is a control: the mandatory removal already dropped that name. All pass now. Review r2 found the count first; the run was repeated here.

## Review r2 (GPT-6.1 Sol, `20261027-060221-pr270-review-r2`, APPROVE)

r2 found every r1 finding closed. It found no P1 or P2 defect in the delta, and two P3s, both fixed:

| Finding | Disposition |
| --- | --- |
| P3-1: the argv checker read only `-c V`, so `--config V`, `--config=V`, `-cV` and `-c=V` slipped past it, though real codex 0.159.0 accepts each and lets it turn the snapshot back on | The checker now parses every spelling, and reads options after `resume` too: `codex exec resume --help` lists `-c, --config` and `--enable`. No managed launch passes an override, so this was a test gap. The previous checker accepts 9 of the 10 new refusal cases (each spelling at the tail and after `resume`); the new one refuses all 10. A companion test checks that other keys, in every spelling, still pass. |
| P3-2: wrong claims in this report and the PR body | The regression count above is corrected. The "Change" section no longer says the allowlist is the whole environment a command sees, and the r1 table no longer cites a stopgap script that is not in the repository. |

r2 also confirmed the following:

- Every caller of `ProviderEnvironment.additions` passes the launching lane's own provider. `codex` and `claude` are the only production providers.
- A Claude token lane's `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` survive.
- An env-kind credential reference is renamed to the canonical token name before the filter runs.

## Verification

All runs were local, serial, under the hub's suite lock, with a private `TMPDIR`. They used codex-cli 0.159.0 and Claude Code 2.1.284.

- **The six files this change adds or edits:** `test_launch_env_vendor_shell.py`, `test_provider_shell_environment.py`, `test_daemon_launch.py`, `test_codex_turn.py`, `test_launch_env_policy.py` and `test_launch_env_allowlist.py`. Result: 206 passed. The three real-binary process tests ran and were not skipped: Codex exec plus resume, the Codex conversation turn, and Claude Code's Bash.
- **The related suite:** the 66 test files that import a changed module or test launch environments, plus the two new files. Result: 4,100 passed, 4 xfailed.
- **Four existing tests were updated** for the new behaviour:
  - `test_daemon_launch` C-5.1 now checks that a Codex lane keeps its own vendor's token and drops a Claude token;
  - `test_launch_env_policy`'s exact-pass case now uses the name's own vendor, and checks that the other vendor's build drops a vendor credential;
  - the two `test_codex_turn` argv tests now expect the shell keys at the end.
- **A names-only scan of 10,315 retained Codex job stdouts.** It found one environment printout that holds a credential name: the incident job's. Other hits were code or diffs that mention a name.
- **After fix round r1:** the related suite again, plus the contract guards (`test_state_contract`, `test_test_module_names`, `test_invariants_index`) and `test_claude_adapter`. Result: 4,363 passed, 4 xfailed, 2 failed. Both failures were `test_launch_env_mutations`' withheld-name mutant, which rewrote `additions`' body; that body is now `_additions`. Retargeted, the file passes (70). `test_launch_env_vendor_shell.py` alone: 104 passed.
- **After review r2's P3s:** `test_launch_env_vendor_shell.py` alone, 105 passed. The previous checker against the new refusal cases: 9 of 10 accepted. The head test file against `4179f7c94`'s code: 31 failed, 1 passed (the control above).
- **CI** runs on the pull request.
