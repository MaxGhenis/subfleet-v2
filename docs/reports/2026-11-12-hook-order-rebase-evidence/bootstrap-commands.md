Bootstrap/read-only command inventory (before the JSONL runner, plus independent audit reads):

- `date -u '+%Y-%m-%dT%H:%M:%SZ'`: start 2026-11-12T05:51:30Z; repeated only for deadline checks.
- `git status --short --branch`, `git status --porcelain=v1`, `git show -s --format='%H %s' HEAD`: clean detached 62103194a83650d3ac4ea532776f632e476390d5.
- `git cat-file -e 'b51f08f961e1b1dcfb93347eb080f9ddd9a9260d^{commit}'`: exit 0.
- `rg --files -g AGENTS.md -g pyproject.toml -g uv.lock -g '*hooks_install*' -g test_invariants_index.py -g home_paths.py`: found pyproject, lock, equality and index tests.
- `git branch --list rebase/hook-order-drift-217`: existing branch checked out elsewhere; no branch mutation.
- `rg --files --hidden -g AGENTS.md -g '!.git'`: exit 1, no instructions file.
- `git grep -n -E '_place_ours|_drift\(|def _same|"drift"' b51f08f961e1b1dcfb93347eb080f9ddd9a9260d -- subfleet/hooks.py subfleet/doctor.py subfleet/cli.py`: only unrelated cli.py:1975 `_same_process`; repeated confirmation.
- `git show b51f08f961e1b1dcfb93347eb080f9ddd9a9260d:subfleet/hooks.py | nl -ba | sed -n '145,180p;795,1010p'`: inspected release paths, desired entries, recognition, old plan/apply/installed.
- `git diff f1bd2ab58 b51f08f961e1b1dcfb93347eb080f9ddd9a9260d -- subfleet/hooks.py`: read full diff; last hunk new line758, no install-region changes.
- `git diff --unified=0 f1bd2ab58 b51f08f961e1b1dcfb93347eb080f9ddd9a9260d -- subfleet/hooks.py | rg '^@@'`: confirms hunk locations.
- `git show b51f08f961e1b1dcfb93347eb080f9ddd9a9260d:subfleet/doctor.py | nl -ba | sed -n '115,150p'` (also123–145): doctor consumes missing_events.
- `git show b51f08f961e1b1dcfb93347eb080f9ddd9a9260d:subfleet/cli.py | nl -ba | sed -n '3260,3305p'` (also3264–3302): installer consumes changed_events.
- `git show b51f08f961e1b1dcfb93347eb080f9ddd9a9260d:subfleet/operations.py | nl -ba | sed -n '235,265p'` (also245–260): uninstall is other caller.
- `git grep -n -E 'hooks\.(plan|apply|installed)\(' b51f08f961e1b1dcfb93347eb080f9ddd9a9260d -- subfleet`: five external call sites in three modules, as reported.
- `git grep -n -E 'def home_path|C-23.25|C-23.26|Changes in version 3' b51f08f961e1b1dcfb93347eb080f9ddd9a9260d -- subfleet docs/acceptance-contract.md`: contract and guard references recorded.
- `git show b51f08f961e1b1dcfb93347eb080f9ddd9a9260d:subfleet/home_paths.py | nl -ba | sed -n '1,200p'` and `sed -n '/def home_path(/,+45p'`: pytest overrides required; personal paths refused.
- `sed -n '1,180p' subfleet/home_paths.py` at old tip: expected absent-file error; corrected to target git show.
- `rg -n 'home_path|SETTINGS_ENV|cli.main|def test_|max_examples|reference' tests/unit/test_hooks_install_equality.py subfleet/home_paths.py subfleet/_home_paths.py pyproject.toml`: absent old-base guard files; equality path assignments/counts inspected.
- `sed -n '1,180p' pyproject.toml` and `git show b51f08f961e1b1dcfb93347eb080f9ddd9a9260d:pyproject.toml | tail -95`: no api extra or xdist dependency.
- `sed -n '1,85p' docs/reference/claude-hooks.md`: historical quote attribution.
- Official web `open https://code.claude.com/docs/en/hooks`, `find All matching hooks run in parallel`, `find All matching hooks`: fetched 2026-11-12, current parallel-hook wording confirmed; historical literal absent.
- `getconf DARWIN_USER_TEMP_DIR`: /var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/.
- Python scratch mkdir under getconf temp root: PermissionError; no folder created.
- `git -c core.hooksPath=/dev/null rebase --onto b51f08f961e1b1dcfb93347eb080f9ddd9a9260d f1bd2ab58`: shared gitdir rebase-merge creation denied (tool exit0 despite error).
- Python private metadata setup: mkdir /private/tmp/sf-hor-b1; `git init --bare /private/tmp/sf-hor-b1/git`; read-only object alternate to parent objects; detached initial HEAD; configure private core.bare/worktree/hooksPath/user.name/user.email; `git --git-dir ... read-tree HEAD`: succeeded. No original refs changed.
- `GIT_DIR=/private/tmp/sf-hor-b1/git git rebase --onto b51f08f961e1b1dcfb93347eb080f9ddd9a9260d f1bd2ab58`: code clean, docs conflict only (exit1).
- `ls /private/tmp/sf-hor-b1/git/rebase-merge`, `cat .../git-rebase-todo`, `GIT_DIR=... git status --short --branch`, `git rev-parse HEAD`: inspected only job-owned rebase state.
- `command -v uv`: /Users/maxghenis/.local/bin/uv.
- `rg -n '<<<<<<<|=======|>>>>>>>' docs/acceptance-contract.md`, sed ranges1–16/307–318/711–725, `git diff --name-only --diff-filter=U`, `git log -2 --format='%h %s'`: only requested contract conflicts.
- Two Python contract-resolution attempts: StopIteration (wrong log prefix), then AssertionError (wrong original date); neither wrote resolution. The second shell continued, stages unresolved file, commit --file fails absent message, `GIT_EDITOR=true git rebase --continue` finalized temporary incorrect docs commit. Corrected immediately with final script (release full text + PR clause/log), named-file add and amend; final audit passed. No shared refs changed.
- Python writes of scratch run.py, sf_job_paths.py, mutants.py, checks.py, step0.md, environment.md and bootstrap-commands.md: succeeded; source scripts retained with evidence.
- `sed`/`nl` reads of equality.py (1–280,277–610,185–265,395–510,504–570), original-tip git show equality.py, hooks.py (730–840,825–1090,870–1010,1038–1065), differential.py (full), tests/conftest.py (1–180), tests/unit/conftest.py (1–170), tests/fake/conftest.py (235–275), tests/failed_keep.py (1–125), tests/frontend/swift.py (1–175), test_core_steer.py (1–175): inspected strategies, assertions, fixture isolation, compiler/cache paths. All succeeded.
- `rg -n` searches across those files for fixture temp paths, subprocesses, cache paths and settings calls; missing root conftest.py in one multi-file rg yielded exit2; actual fixture files inspected successfully.
- `git show 62103194a:docs/acceptance-contract.md | head -13`: found original log date2026-09-30.
- `rg -n 'optional-dependencies|api|pytest-xdist' pyproject.toml uv.lock`: exit1, none.
- `rg -n 'pytest|timeout' .github/workflows/tests.yml | head -45`: inspected CI bounds; full suite not run.
- `rg -n 'def test_|max_examples' tests/unit/test_hooks_install_equality.py tests/unit/test_hooks_install_differential.py`: test/count map.
- Python comparison of frozen loop to target source: pass. Python first-commit message comparison: pass. Original worktree HEAD remains62103194a83650d3ac4ea532776f632e476390d5.
- `rg -n 'PostToolUse|AssertionError|failed|Falsifying' /private/tmp/sf-hor-b1/mutant-1.log` before completion: exit2 (log not yet written); no test rerun.
- Python JSONL timing summaries, audit/evidence reads and repeated `date -u`: successful monitoring only; no live process inspected.

Independent agents also ran `pwd`, `git --version`, the required exact grep, targeted filename rg, `rg -n -C 4` context inspection of matched tests, `nl -ba` citations, target git show, byte-equality assertion scripts, and `wc -l` on scratch evidence (all successful). Their full substantive results are in audit-verification.log, exact-compare-grep.log, targeted-filenames.log and report-audit-sections.md. The differential agent used apply_patch to create only the new test, then `git diff --no-index /dev/null tests/unit/test_hooks_install_differential.py` (expected exit1). No agent ran tests or changed refs.

All runner-managed commands, their exact arguments, exit status, duration and output-log filenames appear in commands.jsonl. Mutant test failures are expected assertion failures; each mutation was restored from hooks-pristine.py.

Additional read-only completion commands: date checks; own test-fixture directory counts under scratch; rg --files build/frontend-probes (one compiled probe); Python mutant log assertion summaries; Python first-commit-message equality and original HEAD checks; command-log summaries; independent mutant auditor cat/sed/cmp/rg/AST/git-show checks (all successful). One repeat of the three required release test files with -rs is intentional: the combined run had10 conditional skips, so it resolves whether invariant8 has a skipped-test limitation. No code change triggered a repeat.

Final artifact checks: independent `git init --bare --template= --initial-branch=rebase/hook-order-drift-217 S/import-check`, parent-object prerequisite alternate, `git --git-dir S/import-check bundle unbundle S/code.bundle`, `cat-file -e aacdfeac9^{commit}`, and `merge-base --is-ancestor b51f08f96 aacdfeac9`: all exit0; bundle independently imports code/test history. `git check-ignore -v docs/reports/2026-11-12-hook-order-rebase.bundle`: exit0, ignored by .gitignore:15. Therefore the final artifact uses .gitbundle; `git check-ignore -v docs/reports/2026-11-12-hook-order-rebase.gitbundle` exits1. No ignore rule was edited. Final report read-only review found no blocking issue; pytest exit wording clarified.
