Writable Claude lanes stop loading the host user's personal startup files and memory (D-LANE-STARTUP1), and a repository's `disableAllHooks: true` no longer turns off the lane guard (D-HOOKS-OFF1). This builds Design A from `docs/reports/2026-11-14-lane-startup-design.md`.

## What changes

Every workspace-write Claude `build_launch` and `resume_launch`, review or not, merges `job_startup_settings(workdir, env)` into its one `--settings`:

- `env`: `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, and `autoMemoryEnabled: false`.
- `disableAllHooks: false`. Claude Code 2.1.318's source lets `--settings` override project and local settings here, so a repository's `disableAllHooks: true` can't switch off the guard hook (design note, F7).
- `claudeMdExcludes`:
  - `<C>/CLAUDE.md` and `<C>/rules/**` for each config home C (`$HOME/.claude`, and `CLAUDE_CONFIG_DIR` when the launch sets it);
  - the six startup file names in every ancestor of each spelling of the job's repository root R, and in `$HOME`. R's spellings are git's, its real path, and the NFC form of each, since Claude Code normalizes its working folder to NFC while git and the file system give a name as it is stored;
  - one negation sparing R in every spelling, so the job's own repository still loads its files.
- Writable launches now remove the memory variables (`claude_env_remove`), as read-only launches already did.

`glob_literal` writes each path as a pattern that matches only that path. It escapes with one-character classes and, for the comma and the double quote, one-character ranges, since Claude Code's matcher turns every backslash into a slash.

R comes from `git rev-parse --show-toplevel`, run with a 10 s cap and with `core.fsmonitor=false` appended to the config git reads from `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`. Git's answer is used only when it is not `/` and the workdir's real folder is that folder or lies under it, compared as folders, not spellings. A checkout's own config (`core.worktree`, or a `.git` file whose `gitdir:` names another repository) can move the answer, and `/` made the negation `!/**`. Otherwise, and on any git failure, R is the workdir, which excludes more of the repository's own files and adds no personal one.

Read-only launches, isolated reviews, `lane_settings()` and `claude_turn`'s argv stay byte-for-byte as before (189 recorded cases). The other 48 recorded cases are writable and differ only by these keys.

C-12.9 states the rule, with a change-log line.

## Evidence and limits

- No Claude run has exercised these keys. How Claude Code reads them rests on its 2.1.318 source, as the design note quotes it. A resumed session keeps what its first turn loaded.
- Claude Code's own matcher (picomatch and its `FWo`, extracted from the 2.1.318 binary and run under node) agrees with the test oracle on all 27,487 generated paths, and on 44,160 more whose repository names hold NFD letters (1,432 in the committed NFD fixture, 42,728 from six more seeds).
- A real repository named `café`, stored in NFD, keeps its `CLAUDE.md`, `AGENTS.md` and `sub/CLAUDE.md` under the bundle's matcher and node's NFC `process.cwd()`; before fix round 1 all were excluded.
- Invariants I1-I9 are tests (`tests/unit/test_lane_startup.py`, `tests/unit/test_lane_startup_glob.py`), and the build's 17 mutants each fail a test.
- A `core.worktree` naming a folder that holds the workdir is still accepted, as git's answer for a subfolder job always is: the folders between it and the workdir then load as the checkout's. `$HOME`'s and the config home's files stay excluded by name.

## Merging with #310

#310 (D-EFFORT-ENV1) edits the same launch paths, and it merges first. Review r1 merged this PR's 2234e7196 with #310's 4fd796b49 without committing:

- Conflicts: `claude.py:1510` and `:1562` (now `:1565` and `:1617`), the two settings lines, and the contract's change log.
- Resolved to `lane_settings(startup, effort=effort)`, the merge failed 181 tests and passed 1,828. 179 failures are this PR's recorded cases (154 in `test_i5_i6…`, 24 in the writable differential, 1 grid test): the `argv-adf66f2e5.json` recording predates #310's effort env key and its 70 added cases. The other 2 are #310's property tests in `test_effort_env.py`, which need `without_job_startup`.

After #310 merges, this PR merges release/217, keeps both change-log lines, adds `without_job_startup` to those two tests, and re-records the argv fixture from a base that includes #310.

## Tests

The build ran 2,228 unit tests (3 xfailed) and 542 fake, e2e and process tests, all passing. `tests/e2e/test_conversations.py` timed out on its harness conditions under host load in a separate run. After merging release/217 (only the contract's change log conflicted), the lane-startup, Claude adapter, state-contract, invariant-index and module-name tests passed: 583.

Fix round 1 added 9 tests to `test_lane_startup.py` and an NFD recording to `test_lane_startup_glob.py`; 19 of the new cases fail on 2234e7196. With the fix: the new and changed tests 135 passed; the lane-startup and Claude adapter files 566 passed; the state-contract, module-name, invariant-index and handler-refusal files 435 passed.

Report: `docs/reports/2026-11-15-lane-startup-build.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
