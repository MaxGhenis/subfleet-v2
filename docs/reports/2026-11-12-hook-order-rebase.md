# Hook-order rebase report — 2026-11-12

Started **2026-11-12T05:51:30Z** from the first command's `date -u`; report prepared **2026-11-12T06:28:10+00:00**, before the 08:51:30Z deadline. Workspace: `/Users/maxghenis/.subfleet/worktrees/20261111-145018-hor-build-r1`.

Rebased #86's two commits onto **b51f08f961e1b1dcfb93347eb080f9ddd9a9260d**, resolved the contract, and committed the new differential test separately. All nine mutants were killed. Validation: equality **18 passed**; differential **3 passed**; targeted matrix **2,204 passed, 10 skipped**; final contract/state group **425 passed**. Invariant 8 is limited by three sandbox-related skips in the unchanged core release files.

**Delivery limitation:** the sandbox refused the shared Git administrative directory. The history was built in private metadata on the assigned branch name, `rebase/hook-order-drift-217`. Original refs and the workspace gitfile remain untouched at `62103194a83650d3ac4ea532776f632e476390d5`. Import the accompanying `docs/reports/2026-11-12-hook-order-rebase.gitbundle` with `git bundle unbundle <bundle-path>` before the hub sets #86's branch with its lease. The bundle is generated after the report commit as a delivery artifact; nothing was pushed. *(Fix r1, 2026-11-15: this step is done. The hub imported the bundle, so a84182d58, 511322701, aacdfeac9 and this report's commit a0da4be0e are in #86's history; the `.gitbundle` was never committed and is not needed.)*

The code/test history before this report is:

- `a84182d5862bad5470295de0bdcd9a8889a7ff97`: original 839c6a4db, message preserved exactly.
- `511322701b3a66ff193a2ae29de0671b1e2e54e4`: original 62103194a, original message retained with the requested resolution note.
- `aacdfeac9c16e77939080dafee8785741d06d037`: differential test commit.

## Step 0: release evidence

The target commit `b51f08f961e1b1dcfb93347eb080f9ddd9a9260d` exists.
All `b51f08f96:path:line` citations below refer to that exact commit.
Release does not already contain #86's behavior: the requested symbol/drift search
finds only the unrelated `_same_process` at `b51f08f96:subfleet/cli.py:1975`.
There is no `_place_ours`, `_drift(`, hook `_same`, or `"drift"` key in the three
implementation files.
Release C-23.25 lacks “at any position” (`b51f08f96:docs/acceptance-contract.md:711`).
Its installer strips our entry, appends it last, and uses Python `!=`
(`b51f08f96:subfleet/hooks.py:913`, `:914`, `:915`).
`installed()` derives `matches` from `changed_events` and returns those events as
`missing_events` (`b51f08f96:subfleet/hooks.py:991`).

The old-base/release hooks diff has no hunk from `desired_groups` through
`installed()` (`b51f08f96:subfleet/hooks.py:806`, `:984`); #86's install intent
therefore carries through without adapting intervening installer changes.
Earlier hook changes remain release's, including the settings-path guard.
`settings_path()` delegates to `home_paths.home_path`
(`b51f08f96:subfleet/hooks.py:165`); under pytest, that helper rejects personal
root overrides and missing overrides (`b51f08f96:subfleet/home_paths.py:269`, `:272`).
The only equality-file CLI calls first set `hooks.SETTINGS_ENV` to temporary files
(`tests/unit/test_hooks_install_equality.py:138`, `:158`); other calls pass paths.

Doctor reads `missing_events` (`b51f08f96:subfleet/doctor.py:140`); installation
reads `changed_events` (`b51f08f96:subfleet/cli.py:3283`, `:3290`).
The only other external caller is `_hooks-uninstall`'s `cmd_uninstall_hooks`,
which calls `plan(remove=True)` and `apply(remove=True)`
(`b51f08f96:subfleet/operations.py:245`, `:247`, `:254`).

The official [hooks page](https://code.claude.com/docs/en/hooks) was fetched on
2026-11-12. The historical literal “All matching hooks run in parallel.” is
absent; the current page says “When an event fires, its matching hooks run in
parallel.” The current wording confirms the behavior. The PR's historical quote
and explicit 2026-09-28 fetch attribution remain unchanged
(`docs/reference/claude-hooks.md:30`); that literal quote rests on the PR's fetch.

## Rebase and conflict resolutions

The code/tests commit applied cleanly. The documentation commit conflicted only
in `docs/acceptance-contract.md`: the version-3 log and the C-23.25/C-23.26 pair.
The log keeps all 343 release entries, their order, and whitespace; the sole
addition is #86's original first entry with “rebased 2026-11-12, PR #86” added
inside its parenthesis (`docs/acceptance-contract.md:9`). C-23.25 is verbatim #86
(`docs/acceptance-contract.md:712`); C-23.26 is verbatim release (`:713`).
The rebased documentation commit message names both resolutions. Nothing was
renumbered. C-2.1's cross-reference remains unchanged (`:432`), and the entire
ledger, including row 70 (`docs/invariants.json:906`), remains release's bytes.
The equality tests remain the original PR's bytes; the three required release
test files remain release's bytes. A reusable independent audit checks all of this.

## Invariants and their tests

All counts below are the committed Hypothesis `max_examples`, unchanged for #86.
Test names without a filename refer to `tests/unit/test_hooks_install_equality.py`.

1. **Installed anywhere:** `test_any_permutation_holding_the_entry_once_is_installed`
   (150 examples, `:402`) checks every owned event's permutation, no drift,
   doctor PASS, byte preservation, and no backup.
2. **Drift iff:** `test_install_drifts_exactly_when_it_should_and_preserves_the_rest`
   (150, `:423`) checks missing/duplicated/differs exactly;
   `test_any_single_change_to_the_entry_is_drift_and_is_replaced_in_place`
   (200, `:457`) exercises unequal single entries at arbitrary positions.
3. **Placement:** those same 150- and 200-example properties (`:423`, `:457`)
   check first-owned position, or last when absent, including shared groups.
4. **Preservation:** the 150-example install property (`:423`) and
   `test_uninstall_removes_only_v2s_entries_in_place` (80, `:482`) preserve foreign
   content/order. The install property checks top-level/unowned preservation; the new uninstall differential property also explicitly checks those unowned values.
5. **Idempotence:** the install property (`:423`, 150) checks second-install bytes
   and backup count; the uninstall property (`:482`, 80) checks a second removal.
6. **JSON equality:** `test_same_is_json_value_equality` (300, `:542`) checks
   reflexivity, symmetry, round trips, and the independent `canonical` reference
   (`:553`), separating bools from numbers while equating numeric spellings.
7. **Agreement where release was right:** in `tests/unit/test_hooks_install_differential.py`,
   `test_install_agrees_with_release_when_owned_entries_are_alone_and_last`
   (`:120`, 150 plus an explicit missing-entry example) and
   `test_uninstall_agrees_with_release_for_list_valued_events` (`:135`, 150)
   compare `changed_events` and canonical JSON documents with the frozen verbatim
   release loop (`:44`; source `b51f08f96:subfleet/hooks.py:911`). Install generation
   includes absent entries and last standalone exact/unequal entries without
   boolean/number aliases. Uninstall generation includes rich list-valued shapes.
   `test_uninstall_intentionally_preserves_a_non_list_event_without_drift`
   (`:148`, one example) labels and pins the intended retained-value versus `[]`
   difference when another event changes.
8. **Release tests unchanged:** `tests/unit/test_hooks.py`, `tests/unit/test_doctor.py`,
   and `tests/unit/test_hooks_native_guard.py` are byte-identical to release.
   The targeted run and a skip-detail follow-up report 143 passed and 3 skipped: test_hooks.py:341 needs native ps inspection after its hook output/mark assertions passed; test_doctor.py:605 and :617 need permitted sysctl/ps inspection and macOS boot identity. Thus full execution of invariant 8 remains for CI.
   No exact comparison required an edit: relevant assertions are substrings
   (`test_hooks.py:885`, `:901`; `test_daemon_verbs.py:216`); the doctor row label
   (`test_doctor.py:52`) remains unchanged.

## Mutants

Every mutant was applied alone from a pristine byte copy and restored afterward. Each killer pytest exit was 1 with the expected assertion failure, not a setup/collection failure. No example-count overrides were used. Independent audit confirmed committed test bytes and final `hooks.py` SHA-256 `83907562e9621a5fc2311a5262bb0a418b584680cb400ca6f4e064875f567812`.

`E` means `tests/unit/test_hooks_install_equality.py`; `D` means `tests/unit/test_hooks_install_differential.py`. Counts are committed `max_examples`, or one deterministic example.

| Mutant | Killer | Count | Failed assertion |
|---|---|---:|---|
| 1. Append ours last | `E::test_any_permutation_holding_the_entry_once_is_installed` | 150 | E:414, no drift |
| 2. Python == in plan | `E::test_any_single_change_to_the_entry_is_drift_and_is_replaced_in_place` | 200 | E:472, differs reason |
| 3. Re-render unchanged events | `E::test_an_unchanged_event_keeps_its_own_spelling_when_another_is_rewritten` | 1 | E:262, key order |
| 4. Duplicate reported as differs | `E::test_a_duplicate_is_drift_and_install_keeps_the_first` | 1 | E:237, duplicated reason |
| 5. Insert after shared group | `E::test_an_entry_shared_with_another_tools_hook_differs` | 1 | E:224, placement |
| 6. Conflate bool and number | `E::test_same_is_json_value_equality` | 300 | E:550, independent canonical equality |
| 7. Use last owned position | `E::test_a_duplicate_is_drift_and_install_keeps_the_first` | 1 | E:241, first position |
| 8. Insert missing entry at index zero | `D::test_install_agrees_with_release_when_owned_entries_are_alone_and_last` | 150 | D:129, canonical proposed document; explicit example |
| 9. Drop installed drift key | `E::test_a_missing_entry_is_drift_and_is_appended` | 1 | E:157, PostToolUse (missing) doctor detail |

## Verification and limits

Each invocation had a fresh `--basetemp`. The required file search selected 13 actual test files, all run together with `-n 4`: the six named release files, equality/differential, and five additional matches in incidental “would write” prose (`test_retention_regenerable_properties.py`, `test_launch_env_round7.py`, `test_daemon_log_sites.py`, `test_steer_invariants.py`, `tests/frontend/test_core_steer.py`). Helpers and fixture/model files were excluded. The full suite was not run.

| Run | Result |
|---|---|
| Equality file alone | 18 passed |
| Differential file alone | 3 passed |
| All 13 matching files, one four-worker run | 2,204 passed, 10 skipped |
| State contract, module names, invariant index, handler refusals | 425 passed |
| Three unchanged release files, skip-detail follow-up | 143 passed, 3 skipped |
| Independent contract/file/frozen-loop audit | All 9 assertions passed, repeated after tests |
| Whitespace check | `git diff --check` passed |
| Code-history bundle check | Valid bundle; release target is its prerequisite |

The core-file repeat was justified by the combined run's unexplained skips. The three core skips are native process/boot-identity checks unavailable in this sandbox; no release test or expectation was edited to bypass them. The other seven combined-run skips remain recorded in that run's summary. No test failed on the restored tree.

## Deviations and cleanup

- Native rebase could not create the shared `rebase-merge` directory. Private Git metadata in `/private/tmp/sf-hor-b1/git`, with a read-only parent-object alternate, allowed the requested rebase and commits without changing caller refs. The hub must import the bundle before using the final tip.
- `getconf DARWIN_USER_TEMP_DIR` returned `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`, but creating its job scratch folder was denied. Tests therefore used `TMPDIR=/private/tmp/sf-hor-b1` and `UV_CACHE_DIR=$TMPDIR/uv-cache`.
- The exact requested `uv sync --group dev --extra api` failed because this release defines no `api` extra. `uv sync --group dev` succeeded. `uv pip install pytest-xdist` supplied xdist only in the disposable environment; pyproject and lock stayed unchanged.
- An external, archived `sf_job_paths` pytest plugin redirected explicit `/tmp` fixture folders into job scratch and `tests.failed_keep.ROOT` there, preventing another job's scratch/prune state from being read. Workspace fixture temp folders got the `sf-hor-b1` prefix and guarded deletion. Swift/Hypothesis/uv caches stayed in scratch; the repository-supported Swift nested-sandbox option was disabled inside the outer sandbox. Repository fixtures/tests were unchanged.
- A contract-resolution script first matched the wrong log prefix/date. Its initial shell continued after the error and temporarily finalized unresolved markers. Before any tests, the new private documentation commit was corrected to the verified resolution and requested message. Only the commits being rebased were corrected; no shared history changed.
- Invariant 8 has the three explicit environment skips above. The literal historical hooks sentence could not be reconfirmed, although today's official page confirms its parallel execution behavior.

All outside-scratch generated folders are recorded below, relative to the workspace given at the top. The fixture folder was already removed with the job guard; dependency/cache/bytecode folders were moved into job scratch and deleted with `shutil.rmtree` after asserting `sf-hor-b1` in the deletion path. All tracked files were checked and preserved. Completed basetemps and scratch caches were also removed. Exact original/deletion paths are in `2026-11-12-hook-order-rebase-evidence/cleanup.json`.

- `sf-hor-b1-pytest-cache-files-zkht98p3`
- `.venv`
- `.pytest_cache`
- `build`
- `subfleet/__pycache__`
- `subfleet/adapters/__pycache__`
- `subfleet/sessions/__pycache__`
- `subfleet/conversations/__pycache__`
- `subfleet/gate/__pycache__`
- `subfleet/guard/__pycache__`
- `tests/__pycache__`
- `tests/unit/__pycache__`
- `tests/frontend/__pycache__`
- `tests/fake/__pycache__`

## Commands and results

`S=/private/tmp/sf-hor-b1`; `P=.venv/bin/python -m pytest -p sf_job_paths`. Git mutations below used the private `GIT_DIR=S/git`; test commands explicitly removed that variable. The archived `commands.jsonl` retains full unabridged arguments, durations, and output filenames. Every output log is in `2026-11-12-hook-order-rebase-evidence/`. Mutant exit 1 is the expected successful kill.

| Command | Exit | Seconds |
|---|---:|---:|
| `git add docs/acceptance-contract.md` | 0 | 0.73 |
| `git commit --file S/rebase-message.txt` | 128 | 2.12 |
| `git add docs/acceptance-contract.md` | 0 | 0.09 |
| `git commit --amend --file S/rebase-message.txt` | 0 | 0.26 |
| `uv sync --group dev --extra api` | 2 | 0.86 |
| `git status --short` | 0 | 3.41 |
| `uv sync --group dev` | 0 | 11.27 |
| `uv pip install pytest-xdist` | 0 | 1.04 |
| `git switch -c rebase/hook-order-drift-217` | 0 | 0.02 |
| `git diff --check` | 0 | 0.5 |
| `P tests/unit/test_hooks_install_equality.py --basetemp=S/equality -q` | 0 | 144.5 |
| `P tests/unit/test_hooks_install_differential.py --basetemp=S/differential -q` | 0 | 84.36 |
| `git add tests/unit/test_hooks_install_differential.py` | 0 | 0.53 |
| `git commit -m 'Test hook install against the frozen release/217 loop (C-23.25)'` | 0 | 4.14 |
| `P tests/unit/test_hooks_install_equality.py::test_any_permutation_holding_the_entry_once_is_installed --basetemp=S/mutant-1 -q` | 1 | 284.17 |
| `P tests/unit/test_hooks_install_equality.py::test_any_single_change_to_the_entry_is_drift_and_is_replaced_in_place --basetemp=S/mutant-2 -q` | 1 | 65.78 |
| `git log b51f08f961e1b1dcfb93347eb080f9ddd9a9260d..HEAD '--format=%H %s'` | 0 | 0.27 |
| `git diff --stat b51f08f961e1b1dcfb93347eb080f9ddd9a9260d..HEAD` | 0 | 1.44 |
| `P tests/unit/test_hooks_install_equality.py::test_an_unchanged_event_keeps_its_own_spelling_when_another_is_rewritten --basetemp=S/mutant-3 -q` | 1 | 24.65 |
| `P tests/unit/test_hooks_install_equality.py::test_a_duplicate_is_drift_and_install_keeps_the_first --basetemp=S/mutant-4 -q` | 1 | 8.43 |
| `P tests/unit/test_hooks_install_equality.py::test_an_entry_shared_with_another_tools_hook_differs --basetemp=S/mutant-5 -q` | 1 | 7.21 |
| `P tests/unit/test_hooks_install_equality.py::test_same_is_json_value_equality --basetemp=S/mutant-6 -q` | 1 | 13.68 |
| `P tests/unit/test_hooks_install_equality.py::test_a_duplicate_is_drift_and_install_keeps_the_first --basetemp=S/mutant-7 -q` | 1 | 6.74 |
| `P tests/unit/test_hooks_install_differential.py::test_install_agrees_with_release_when_owned_entries_are_alone_and_last --basetemp=S/mutant-8 -q` | 1 | 23.4 |
| `P tests/unit/test_hooks_install_equality.py::test_a_missing_entry_is_drift_and_is_appended --basetemp=S/mutant-9 -q` | 1 | 7.13 |
| `P -n 4 tests/unit/test_hooks.py tests/unit/test_doctor.py tests/unit/test_hooks_native_guard.py tests/unit/test_live_store_guard_round4.py tests/unit/test_compat.py tests/unit/test_daemon_verbs.py tests/frontend/test_core_steer.py tests/unit/test_launch_env_round7.py tests/unit/test_retention_regenerable_properties.py tests/unit/test_hooks_install_equality.py tests/unit/test_daemon_log_sites.py tests/unit/test_steer_invariants.py tests/unit/test_hooks_install_differential.py --basetemp=S/targeted -q` | 0 | 492.67 |
| `P tests/unit/test_hooks.py tests/unit/test_doctor.py tests/unit/test_hooks_native_guard.py --basetemp=S/release-core-skip-check -q -rs` | 0 | 33.49 |
| `git bundle create S/code.bundle b51f08f961e1b1dcfb93347eb080f9ddd9a9260d..rebase/hook-order-drift-217` | 0 | 4.24 |
| `git bundle verify S/code.bundle` | 0 | 4.0 |
| `P tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py --basetemp=S/contracts -q` | 0 | 107.6 |
| `git diff --check` | 0 | 3.0 |
| `env GIT_DIR=S/git python3 S/audit-verification.py` | 0 | 0.98 |
| `git status --short` | 0 | 0.18 |
| `python3 S/cleanup.py` | 0 | 10.13 |
| `python3 S/build-report.py` | 0 | 3.12 |
| `git diff --check` | 0 | 3.15 |
| `python3 S/build-report.py` | 0 | 0.3 |
| `git diff --check` | 0 | 0.16 |

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

The completion plumbing writes this report/evidence, stages those named files only, commits the report, creates the final branch bundle, verifies/unbundles it in isolated scratch, and confirms the advertised tip and release ancestry. The code-only trial bundle verification is logged above; the final tip is supplied with delivery because a commit cannot embed its own hash.

## Paragraph for #86

Rebased the two original commits onto release/217 at `b51f08f961e1b1dcfb93347eb080f9ddd9a9260d`. The contract resolution preserves all release change-log entries, prepends #86's dated C-23.25 amendment, takes PR C-23.25 and release C-23.26 verbatim, and records both resolutions in the documentation commit. A separate Hypothesis differential test freezes release's old install loop, proves install/uninstall agreement where it was correct, and pins the intended non-list uninstall preservation difference. All nine mutants are killed; equality/differential and contract checks pass. The targeted run passed 2,204 tests with 10 conditional skips; three skips in the unchanged core files need native process/boot-identity access in CI.

## Fix r1

Review r1 approved #86 at ae95ae451 with one P2 and several P3s. This round fixes the P2 and every P3 but two, which are explained below. Job start 2026-11-15T04:38:21Z (first `date -u`); write-up started 06:02Z. Load stayed between 86 and 169 throughout. Built on d0f0c5c27 (ae95ae451 plus the hub's merge of release/217 at 82d3dd819). Mutants ran in a scratch copy of the source, never in the worktree.

### Step 0: merge release/217

- **Commit:** 3fc4b2c6a merges `origin/release/217` at `c8b61c95f4f5772e3ce8c85365b4335e07309c34` (PR #90, notify off screen) as its own commit. `docs/acceptance-contract.md` auto-merged with no conflict.
- **What release brought:** 16 files, mostly notification work. Of the PR's files, it touches only the contract: two new C-29.9 log entries and an amended C-29.9 clause. It changes no line of `hooks.py`, `doctor.py`, `cli.py`, the hooks reference or the hook tests (`git diff --stat 82d3dd819 c8b61c95f`).
- **The merge holds only the PR's own changes:** `git diff --name-only c8b61c95f 3fc4b2c6a` lists the six code, test and doc files, the build report, and the report's 66 evidence files.
- **The contract:** the log keeps release's 350 entries in their order, with #86's C-23.25 entry first (351 in all). C-23.25 hashes the same as at d0f0c5c27, and C-23.26 the same as at c8b61c95f.

### The P2: `duplicated` for two v2 hooks inside one group

- **The test:** `tests/unit/test_hooks_install_equality.py:269-298`, `test_two_v2_hooks_are_duplicated_in_one_group_or_two`, with its helpers at lines 249-266. It takes `live_shape()`, swaps v2's PreToolUse group for each shape below, and asserts the same results for all three:
  - **The shapes:**
    - one group holding v2's hook twice (`_twice_in_one_group`);
    - one group holding v2's hook under an old path, then as written (`_old_path_and_current_in_one_group`);
    - the control: two groups holding one each (`_once_in_each_of_two_groups`).
  - **What it asserts:**
    - `plan()` and `installed()` report `{"PreToolUse": "duplicated"}`;
    - `doctor` fails with `<path>: PreToolUse (duplicated) would change`;
    - the dry run says `would write <path>: PreToolUse (duplicated)`;
    - `apply` writes, leaving one entry where the group was.
- **The mutant, M1** (review r1's R3): `_drift` counts the groups that hold v2's hook, not the hooks (`ours = sum(_holds_ours(group, event, command) for group in groups)`, `subfleet/hooks.py:914-917`).
  - **Without the new test it survives:** under M1 the equality file's other 18 tests pass, and so does the new test's control: 2 failed, 19 passed.
  - **Killer:** `test_two_v2_hooks_are_duplicated_in_one_group_or_two[_twice_in_one_group]` and `[_old_path_and_current_in_one_group]`, both with `assert {'PreToolUse': 'differs'} == {'PreToolUse': 'duplicated'}`.

### The P3s

1. **The differential test checked only agreement.**
   - **The fix:** `tests/unit/test_hooks_install_differential.py:149-220` adds `test_install_differs_from_release_only_where_release_was_wrong`, with six cases in `INTENDED_DIFFERENCES`. Every other owned event holds v2's exact entry alone. Each case asserts both sides' `changed_events`, head's `drift`, and the list each side proposes, compared through `canonical`. It also asserts that the two sides differ.
   - **Exact entry not last** (release changes it, head does not): first; in the middle; before malformed groups; in the middle with keys reversed and `timeout` 600.0.
   - **Changed entry not last:** both change it, head in place and release last.
   - **`asyncRewake: 1` for `true`, last:** head reports `differs`; release, using Python's `1 == True`, reports nothing.
   - **Mutant M2:** the frozen release loop takes #86's placement and equality (`_place_ours`, `_same`) inside `release_plan`. Under it the three existing differential tests still pass, and all six new cases fail.
   - **Killer:** `test_install_differs_from_release_only_where_release_was_wrong`, all six cases.
2. **The "duplicated" reasoning and the hooks page.** I fetched the page on 2026-11-15 (`https://code.claude.com/docs/en/hooks.md`, 293,539 bytes, SHA-256 `cf74269e…b764af5e`). Under "Hook handler fields" it says: "When an event fires, its matching hooks run in parallel." The next paragraph says: "If you define the same handler in more than one settings file, it runs once. A plugin's or skill's copy of the same handler stays separate." No other sentence on the page has "dedup", "duplicate" or "identical".
   - **What follows:** review r1's memory was partly right. The page promises a single run across settings files only, and says nothing about one file that lists a handler twice.
   - **The fix:** `test_a_duplicate_is_drift_and_install_keeps_the_first`'s docstring (`test_hooks_install_equality.py:230-235`) no longer claims two equal entries deliver every notice twice. It now says two v2 entries are not what `--hooks` writes, and that two with different commands both run.
   - **The reference:** `docs/reference/claude-hooks.md:35-44` quotes both sentences with the fetch date.
   - **Mutant:** none; a docstring and a reference page have no behaviour to mutate.
3. **The quote's attribution.** Done by the same reference paragraph: today's wording, its section and the 2026-11-15 date sit beside the 2026-09-28 quote. Mutant: none (prose).
4. **Nits.**
   - **"As the file spelled it":** C-23.25 (`docs/acceptance-contract.md:737`) now says an event without drift keeps "the JSON value the file gave it, key order included (a rewrite renders the whole file again, so whitespace, escapes and number spellings such as `1e3` change)". A probe confirmed this. On a file whose drift-free PreToolUse says `"timeout": 5e0` while another event is missing, `apply` keeps PreToolUse's value but writes `5.0`; the escape `\u00e9` comes back as `é` and the whitespace changes. The differential test's non-list docstring (line 227) now uses the same wording.
   - **Module list:** C-23.25 now names `subfleet/cli.py` alongside `hooks.py` and `doctor.py`. `test_invariants_index.py` doesn't compare module lists and still passes.
   - **Formats:** the dry run now prints the same `Event (reason)` list as `doctor`: `would write <path>: PreToolUse (duplicated)` (`subfleet/cli.py:3299-3303`). Only `test_hooks_install_equality.py` pinned the old `(Event: reason)` form, and it now asserts the full new note (line 160, and the P2 test). Elsewhere tests check only the substring "would write", which is unchanged.
     - **Mutant M3:** the dry run goes back to `(Event: reason)`.
     - **Killers:** `test_a_missing_entry_is_drift_and_is_appended` and all three P2 cases. `test_hooks.py` and `test_the_cli_says_it_already_matches` pass under M3 (4 failed, 82 passed).
   - **Stale report instruction:** line 7 of this report now marks the bundle import as done. a84182d58, 511322701, aacdfeac9 and a0da4be0e are ancestors of this head, and no `.gitbundle` is in the tree. Mutant: none (prose).
5. **Pre-existing: "three" entries.** `cmd_daemon_install_hooks`'s docstring (`subfleet/cli.py:3274`) and `--hooks`'s help text (line 3870) now say four. No test pins either string. Mutant: none (prose).

**Not fixed:**
- **The evidence folder:** review r1 scanned it and found no secrets. Deleting the build's record of its runs is the hub's call, not a fix round's.
- **Pre-existing: a non-list owned event:** install replaces it with `[ours]` and reports `missing`, exactly as release/217 does. Changing that is a behaviour change outside #86's scope, and it belongs in its own PR.

### Review r1's unrun mutants

Applied in the scratch copy like the rest:
- **R1** (`_same` loses its list length check): killed by `test_a_missing_entry_is_drift_and_is_appended` (`assert [] == ['PostToolUse']`).
- **R2** (`kept.insert(at or len(kept), group)`): killed by `test_an_entry_shared_with_another_tools_hook_differs`.

Both match review r1's predictions.

### Mutant procedure

- **The copy:** `git archive cd5682fcc subfleet tests pyproject.toml` into scratch `src/`. The pristine copies of `subfleet/hooks.py` (`83907562…f567812`, review r1's hash), `subfleet/cli.py` (`5d5ccb7b…8c08788b`) and `tests/unit/test_hooks_install_differential.py` (`dcb2fbc2…9a08ff77`) went to `pristine/`, hashed.
- **For each mutant:** check all three hashes, apply one exact-string edit (count asserted 1), run, restore from pristine, and recheck the hashes. All five restores matched.
- **Was the copy really what ran?** Pytest's `pythonpath = ["."]` imports the copy. M1's run printed `.../sf-hor-f1/src/subfleet/hooks.py` and "mutated" before starting. The worktree's `hooks.py` hash was unchanged throughout.

### Commands and results

The runs used `TMPDIR=<scratch>/tmp-<label>/`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `HYPOTHESIS_STORAGE_DIRECTORY` and `PYTHONPYCACHEPREFIX` in scratch, and `env -u PYTHONPATH`. Each ran `<scratch>/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp <scratch>/pytest-bt-<label>` under an internal `timeout`. All ten runs below finished; none timed out.

| Run | Start–end (UTC) | Load at start → end (1 min) | Files | Result |
|---|---|---|---|---|
| r1-new (head) | 05:27:58–05:29:33 | 110 → 117 | new P2 test, missing-entry, duplicate, new difference test, non-list uninstall (`-n 4`) | 12 passed, rc 0 |
| m1 (copy, M1) | 05:31:10–05:34:21 | 96 → 117 | equality file (`-n 4`) | 2 failed, 19 passed, rc 1 (killed) |
| m2 (copy, M2) | 05:34:51–05:37:46 | 130 → 132 | differential file (`-n 4`) | 6 failed, 3 passed, rc 1 (killed) |
| m3 (copy, M3) | 05:38:09–05:40:15 | 127 → 121 | missing-entry, P2 test, already-matches, `test_hooks.py` (`-n 4`) | 4 failed, 82 passed, rc 1 (killed) |
| r1mut (copy, R1) | 05:40:52–05:41:48 | 129 → 169 | missing-entry, unchanged-spelling (`-n 2`) | 1 failed, 1 passed, rc 1 (killed) |
| r2mut (copy, R2) | 05:41:55–05:42:36 | 168 → 153 | shared-group, reordered-equal (`-n 2`) | 1 failed, 1 passed, rc 1 (killed) |
| head-install | 05:43:03–05:46:19 | 141 → 110 | `test_hooks_install_equality.py`, `test_hooks_install_differential.py` (`-n 4`) | 30 passed, rc 0 |
| head-callers1 | 05:46:34–05:48:44 | 110 → 134 | `test_hooks.py`, `test_doctor.py`, `test_hooks_native_guard.py` (`-n 4`) | 146 passed, rc 0 |
| head-callers2 | 05:49:00–05:55:25 | 107 → 104 | `test_daemon_verbs.py`, `test_compat.py`, `test_live_store_guard_round4.py` (`-n 4`) | 1,643 passed, rc 0 |
| head-contract | 05:55:54–05:57:11 | 86 → 101 | `tests/fake/test_state_contract.py`, `tests/unit/test_test_module_names.py`, `tests/unit/test_invariants_index.py` (`-n 4`) | 63 passed, rc 0 |

The runs in the worktree were at cd5682fcc, whose code and tests match this section's commit.

Other commands:
- **Git:**
  - `git update-index -q --refresh`: rc 0 in 6 min 7 s at load ~150.
  - `git merge --no-ff c8b61c95f`: rc 0, giving 3fc4b2c6a.
  - Three commits of named files: c4540f961 (P2 test, dry-run format, docstrings), 183b38427 (differential differences) and cd5682fcc (contract, reference, report line 7).
- **Environment:**
  - `uv sync --locked --group dev --python /opt/homebrew/bin/python3.14` with `UV_PROJECT_ENVIRONMENT=<scratch>/venv`: rc 0 in 10 s.
  - `uv pip install pytest-xdist` into that venv: rc 0.
  - `py_compile` of the three edited Python files: rc 0.
- **The hooks page:**
  - WebFetch of `https://code.claude.com/docs/en/hooks`: five calls, two of them on the part past character 200,000.
  - `curl -sS -L --max-time 60 https://code.claude.com/docs/en/hooks.md` into scratch: rc 0, then `grep` for the quoted sentences.
- **The spelling probe:** `<scratch>/probe/spelling.py` (`hooks.apply` on a scratch file), rc 0, output as quoted under nit 1.

### Deviations

- **`git status` under load:** the first `git status` hung past the tool's 120 s limit and was moved to the background. I stopped it, and a retry under `timeout 60` exited 124. Refreshing the index fixed it. A recursive `grep` was also moved to the background and stopped; `git grep` replaced it. All three were read-only.
- **The venv:** it lives in scratch, built with `--locked --group dev` and no `--extra api`, as the build found, since release defines no `api` extra. xdist went into the scratch venv only; the lock file is unchanged. Python was 3.14 only; I skipped 3.12.
- **The network:** I fetched the public hooks page, as the build brief's step 0.6 allowed for this quote. The tool saved two oversized WebFetch results in its own session folder. I did not read them, and used the `curl` copy in scratch instead.
- **Scope:**
  - **Beyond the P2:** I fixed every P3 but two, including one user-visible change: the dry-run note's format.
  - **Extra mutants:** I also ran review r1's R1 and R2.
  - **The full suite:** not run; CI runs it.
  - **The suite lock:** none, since the build brief's rules use none. Load is recorded per run instead.
- **Cleanup outside scratch:** test subprocesses wrote `subfleet/__pycache__/`, `subfleet/adapters/__pycache__/` and `subfleet/sessions/__pycache__/` into the worktree (39 `.pyc` files, ignored, untracked) during the callers runs.
  - **How:** each was moved under `<scratch>/moved-pycache/` and removed by `shutil.rmtree` after an assertion that `sf-hor-f1` is in the path. `git status --porcelain --ignored` is empty afterwards.
  - **Other jobs' folders:** six `sf-test-homes-*` folders in the shared temp root date from this window, among other live jobs' folders. My runs pointed `TMPDIR` into scratch, so I left them.
- **No branch** was created, moved or deleted. Nothing was pushed, and no live state was read.

Head built: this section's commit, whose parent is cd5682fcc86e662742af0ce9c0cb554fad5e8bd0.
