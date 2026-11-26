# Command ledger

All commands ran from the assigned worktree unless a script explicitly selected a scratch cwd. Exact test invocations/results are in each `<label>.json`; combined output is in `<label>.log`. The retained scripts make the probes, mutations and test environment reproducible.

Setup and discovery:

- `date -u`: start Thu Nov 12 05:51:27 UTC 2026; repeated for deadline checks. `pwd`: assigned worktree. `git status --short --branch`: clean detached HEAD initially; repeated for final changes.
- `rg --files`, including hidden `AGENTS.md` lookup: no AGENTS.md found; located source and tests. Read the installed GitNexus debugging skill; no matching tool was available and no indexing or external lookup occurred.
- `rg -n`, `sed -n`, `cat`, `nl -ba` over adapter, turn, daemon/registry, review-publish, process census, pyproject and test harness/test sources: confirmed default binary, merge/control protocol and test safety. Early guesses for absent module paths produced rg missing-path diagnostics; later reads used known tracked files.
- `command -v claude`: `/Users/maxghenis/.local/bin/claude`; `claude --version`: `2.1.318 (Claude Code)`, exit 0.
- `getconf DARWIN_USER_TEMP_DIR`: `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`. `mkdir -p "$(getconf DARWIN_USER_TEMP_DIR)/sf-eff-b1"`: denied. `mkdir -p /private/tmp/sf-eff-b1`: succeeded.
- `python3 /private/tmp/sf-eff-b1/probe.py`: four isolated no-message cases, all exit 0. Copied script and records into this directory. Each case record contains its exact env-unset command, response and exit code.
- `git switch -c fix/effort-env-217`: branch exists. `git show-ref --heads fix/effort-env-217`: b51f08f961e1b1dcfb93347eb080f9ddd9a9260d. `git switch fix/effort-env-217`: refused because caller uses it; no forced switch.
- `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=/private/tmp/sf-eff-b1/uv-cache UV_PROJECT_ENVIRONMENT=/private/tmp/sf-eff-b1/venv UV_PYTHON_INSTALL_DIR=/private/tmp/sf-eff-b1/python uv --cache-dir /private/tmp/sf-eff-b1/uv-cache sync --group dev --extra api`: exit 2, undefined api extra.
- Same cache/environment/interpreter setup with `TMPDIR=/private/tmp/sf-eff-b1` and `uv ... sync --group dev`: exit 0, nine packages installed using existing CPython 3.14.7+freethreaded. No interpreter created outside scratch.

Source, fixture and evidence:

- Edits used apply_patch, Python writes and heredocs; final diff lists each file. No policy changes.
- `git log --all -- bin/subfleet-claude`, `git rev-list --all --objects -- '*subfleet-claude'`: no available runner blob. No host v1 tree access; parity source selection is now explicit opt-in.
- `git archive --format=tar 676906f854f5 subfleet`, scratch-only extraction, then `python -I -B tests/lane_settings_cases.py <old-package> <scratch-cases>`: exit 0, 307 cases. See fixture-commands.md for agent commands and fixture-audit.json for counts, IDs and hashes.
- Independent `git show b51f08f961e1:tests/fixtures/lane_settings/argv-676906f85.json` comparison: all 237 old argv and literal case lines retained; 70 added, total 307. Removing the new lines recovers the entire pristine fixture.
- `rg -l 'lane_settings|claude_settings_args|reconstruct_v1_argv|build_launch|PERMISSION_FLAGS|ULTRACODE|effort' tests -g 'test_*.py' | sort`: 75 files, saved as selected-tests.txt.
- `rg -n --glob 'test_*.py' 'assert .*argv|assert .*settings|--settings.*==|== .*--settings' tests`: 355 comparison lines, saved as all-exact-compares.txt. Targeted Claude modules saved in exact-compares.txt (90 lines). Reviewed changed shapes; updated named-high/ultracode literals and retained None literals.
- Python ast.parse: new tests and harness syntax valid. `git diff --check`: passed repeatedly. `git diff --stat`, `git diff --numstat`, targeted diffs: reviewed. `nl -ba`/`rg` saved source locations in change-lines.txt.
- Repeated `tail`/`wc`, scratch-only bootstrap `ls`/`cat`: monitored test runs. Optional `rg --files /private/tmp/sf-eff-b1/failed-tests` initially returned no directory; no live-state search.
- One shell heredoc attempting to write this ledger was rejected by an existing stash-command hook because the prose contained that command's name. No Git mutation occurred; apply_patch wrote this file safely.

Commit attempts used exact named files:

- `git add` of report, probe script and four Step 0 JSON records; `git commit -m 'docs: verify isolated Claude effort settings precedence'`: both denied because the parent Git worktree index.lock is outside writable roots.
- `git add subfleet/adapters/claude.py subfleet/conversations/claude_turn.py docs/acceptance-contract.md`; `git commit -m 'fix: pin named Claude effort in command-line settings'`: both denied for the same lock.

Test commands are complete in their JSON records, including the selected file list and every fresh basetemp. Main report adds final validation, mutation and cleanup results.

Final commands:

- Named test/fixture staging and `git commit -m 'test: cover Claude effort pins and preserve unnamed argv'`: both denied by index.lock.
- Required guards: 425 passed; repeated exact-compare grep and diff whitespace check passed afterward.
- Affected-file recheck with SF_EFF_BASETEMP_ROOT in the permitted Darwin child: 227 passed, 14 skipped. The report-only bootstrap was changed from eager package import to a lazy procs import hook.
- `python3 .../run-baseline.py`: three residual tests reproduced the same failures on starting production bytes, then restored and hashed both changed modules. Diagnostic wrapper exited 0; the recorded pytest exit was 1.
- `python3 .../run-mutants.py`: seven isolated expected pytest failures, driver exit 0, pristine restoration after every mutant. Final hash comparison matched baseline-restored hashes.
- Restored property module with `--hypothesis-show-statistics`: 3 passed, 4 opt-in skips, 420 generated passing examples.
- Copied raw original/generated fixture oracle JSON into durable evidence; generated test-summary.json from every command/result record.
- Cleanup Python script verified zero bootstrap files/manifests and production hashes, moved six untracked generated cache folders into scratch, checked the two scratch roots' birth times against job start, then guarded shutil.rmtree on both roots. cleanup.json records completion at 06:34:35 UTC.
- Final named-file staging/commit attempt, diff check, fixture byte audit, source hash audit, head and status: results in final-audit.json. No test, cache or config directory remains outside the report artifacts.
