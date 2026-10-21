# D-ST3: a pytest process never resolves the account's live roots, whatever os.environ holds

Branch `fix/st3-process-isolation-217`, cut from `origin/release/217` at `473fe2941`
(#222, the launch-env allowlist, merged as `f99da54b0`). The caller's checkout holds
that branch, so each job committed on a detached HEAD in its own worktree; Subfleet
keeps the commits as a salvage ref. Nothing was pushed. No live store, live daemon,
`~/.subfleet`, `~/.claude`, `~/.codex` or `~/chief-of-staff/state` file was opened,
written or listed, and no live process was signalled. There is one exception, by the
third job: it read its own job's prompt (`~/.subfleet/jobs/20261020-174853-st3-build-d2/a1/prompt.sent.md`)
once, to compare it with the hub's copy of the brief. It opened no store, log or other
file there. The first job signalled only its own lockf waiter; the second and third
signalled nothing (Runs says how the third job's holds were bounded). No sub-agents.

**Three jobs.** The first job (`20261020-073517-st3-build-b`) wrote the commits up to
`d6e7fd601` and the body of this report. It was lost at about 15:25Z on 2026-10-20,
when the host lost power, so it never committed this report or reported its final runs.

The second job (`20261020-114751-st3-build-c`) had two attempts:

- The first ended without a final answer. Subfleet salvaged its worktree at 19:22Z,
  holding only this report. It had run three holds at `d6e7fd601` but had not written
  their results into the report; they are the table labelled as that attempt's, under
  Runs.
- The second attempt checked every item of the brief against the commits (below). It
  reran the `grep` and the fallback scan, and found `origin/release/217`, as the hub had
  then fetched it, still at `473fe2941`. It committed this report as `d07ae296e`, with
  its holds still queued. Its 6-hour wall cancelled it at 21:48Z, after about 1 h 45 m of
  host freezes, while its holds still waited for the suite lock.

The third job (`20261020-174853-st3-build-d2`) started at 03:05Z on 2026-10-21 and did
four things:

- checked the brief's items against the commits again;
- merged `origin/release/217` as the hub last fetched it (`a26d08336`, #237), with no
  conflict (Commits);
- ran the briefed files, then every other file in `tests/unit` and `tests/fake`, at the
  merge (Runs);
- filled in Runs and committed this report.

Every count under "This job's holds" comes from the third job's own runs at
`b38cc9f2f`. The table labelled as the second job's first attempt is kept for
comparison and is not counted there. The release run under "The cause, as executed" is
the first job's, and that section says so.

## Commits

| Commit | Step |
|---|---|
| `be8cbb767` | Tests: D-ST3's regressions, property, mutations and the `production_process` seam (fail at release/217) |
| `81a672496` | Fix: the process rule, the seam, the `~` override and home-spelling refusals, and the two unguarded fallbacks |
| `70bad7117` | Tests: the existing tests that mean production behaviour take the seam |
| `f45fdabab` | Test: the spies leave this process's own interpreter and environment alone |
| `3e104df38` | Docs: C-20.6 amendment and change-log entry; the D-ST3 tests cite C-20.6 |
| `6b07533ab` | Test: the property's oracle leaves this process's own code alone; `~user` junk |
| `d6e7fd601` | Tests: the 20 more tests that replaced os.environ name their own store paths or take the seam; a stat-recording test for `home/<store>/..` |
| `d07ae296e` | Report, committed by the second job with its holds still queued |
| `b38cc9f2f` | Merge of `origin/release/217` at `a26d08336` (#237), by the third job |
| (this commit) | Report: the third job's runs |

**The merge.** Since `473fe2941`, release/217 has gained only #237 (`5cd792ad7`,
`1c6815c35`, `8c0e466aa`, merged as `a26d08336`), which changes only
`tests/unit/test_daemon_connections.py`. This branch does not touch that file, so `git
merge` reported no conflict and nothing needed resolving. `docs/acceptance-contract.md`
is unchanged by #237, so the contract keeps this branch's C-20.6 amendment and change-log
entry as they were. The tested tree is `d6e7fd601`'s code with #237's test change.

## The brief's items, checked against the commits

| Item | Where | Status |
|---|---|---|
| #222 in the base (`git log --oneline origin/release/217 \| grep -m1 '#222'`) | `f99da54b0`, an ancestor of `473fe2941` | Done |
| Release merged first, if it moved (the continuation briefs) | `b38cc9f2f` merges `a26d08336` (#237) | Done; no conflict (Commits) |
| 1. Process-level detection that never reads the environment | `81a672496`: `home_paths.pytest_loaded` | Done; why it cannot be spoofed or missed is under The fix |
| 1. No `subfleet/` code imports pytest | `grep -rn pytest subfleet/` finds only comments, strings and the `under_pytest`/`PYTEST_*` names (rerun by the second and third jobs; no `import pytest`, `import _pytest` or `from pytest` line at `b38cc9f2f`); static test `test_no_subfleet_module_imports_pytest` | Done |
| 1. Folded into every live-root refusal | `81a672496`: `under_pytest()` is either rule for this process | Done |
| 2. One explicit seam, only while HOME is not the account's | `be8cbb767` (`production_process`, `waive_process_rule`), `81a672496` (`_process_rule_waived`, read again at every call) | Done |
| 2. Every test changed, and why | `70bad7117`, `d6e7fd601`; listed under Tests changed | Done |
| 3. Every fallback to the account's home under `subfleet/`, routed through the guarded functions | `81a672496` routes the two that were not (`compat.v1_binary`, `prune_decisions`); listed below | Done. The second job reran the scan (an AST walk of every `Path.home()`, `pwd.getpw*`, `expanduser`, `"HOME"` and `~`-literal site: 137 sites) and read each site outside the table. Each expands a value its caller supplied. The merge changes nothing under `subfleet/` (`git diff d6e7fd601 b38cc9f2f -- subfleet/` is empty), so the scan holds at the merge. |
| Regressions under `{}` and a PATH-only dict, with spies on `os.chmod`, `open` and `sqlite3.connect` (and more) | `be8cbb767`, `f45fdabab`, `d6e7fd601` | Done; at release they fail (the first job's run, below) |
| The same for `production_state_root`, `home_path` with each default, `protected_provider_homes`, `codex_app_home`, `desktop_login_path` | `be8cbb767` | Done |
| Property over arbitrary environment dicts (bounded Hypothesis) | `be8cbb767`, `6b07533ab` | Done; 150 derandomized examples, no database |
| Mutation: the process rule removed fails a named test | `test_live_store_guard_st3_mutations.py::...[no-process-rule]` | Done; all 8 mutants were killed at `b38cc9f2f` (this job's H1) and at `d6e7fd601` (the second job's first attempt) (Runs) |
| Live store spied, never read or statted | the spies in `test_live_store_guard_st3.py` | Done |
| Contract amendment with change-log entry | `3e104df38` | Done |
| Briefed runs, then the whole of `tests/unit` and `tests/fake` | the third job, at `b38cc9f2f` | Done: all 433 files, 14,995 tests; none fails and none is unrun (Runs) |
| Report | this file | Committed by the second job (`d07ae296e`); its Runs filled in and committed by the third |

## The cause, as executed

D-ST1's refusals key on `home_paths.under_pytest()`, which read `os.environ` alone:
the session marker with a live sentinel, else `PYTEST_CURRENT_TEST`. A test that
replaces `os.environ` with a plain dict naming neither makes its own pytest process
read as production. Every guarded fallback then resolves the account's home: `home_path`
returns its default, `production_state_root` and `protected_provider_homes` skip their
account-HOME refusal, and `Path.home()` itself, with no HOME in the dict, reads `pwd`'s
home, the account's.

**Executed at release/217 (the first job's run).** A `git archive` of `473fe2941`
with this branch's D-ST3 test file (at `f45fdabab`) copied in ran under the suite lock
with the worktree's interpreter: 121 failed, 3 errors, 12 passed (`test_live_store_guard_st3.py`). The
spies refuse a call on a live spelling before it runs, so these failures are the first
access each unfixed path attempted, refused:

| Test (release/217) | What the unfixed code did first |
|---|---|
| `test_daemon_start_refuses_the_live_root_before_any_access` (empty dict, PATH-only) | `_root` resolved `~/.subfleet`; `os.mkdir` on it, refused |
| `test_production_state_root_refuses` | returned the account's `~/.subfleet` (did not raise) |
| `test_every_resolver_refuses_its_default` (48) | 46 returned the account's default; `global_config_path`'s `os.stat` of `~/.claude/.config.json` refused (2) |
| `test_home_path_refuses_each_store_default` (32) | evaluated the personal default |
| `test_each_guarded_function_refuses` (16) | 14 returned the account's paths; `keychain_command`'s `os.stat` of `~/bin/agent-secret` refused (2) |
| `test_the_v1_install_default_is_refused` | `os.stat` of `~/chief-of-staff/subfleet/bin/subfleet`, refused |
| `test_prune_decisions_state_root_default_is_refused` | `os.stat` of `~/.subfleet/state.sqlite3`, refused |
| `test_production_state_root_refuses_every_spelling_of_the_accounts_home` | 3 of 10 spellings failed `is_account_home`: `~/st3-missing-component/..`, `~/st3-missing/../`, `/USERS/<name>/st3-missing/..` |
| `test_a_home_spelled_through_a_link_past_a_missing_component_is_the_accounts` | `is_account_home` False |
| `test_a_tilde_override_naming_the_accounts_home_is_refused` (6) | returned the account's `~/.subfleet`, `~/.claude`, `~/.codex` |
| the property | 3 distinct failures, for example `transcripts.claude_dir()` returning `~/.claude` under an empty dict |
| process-rule and seam tests (2 + 4 + 3) | `pytest_loaded` and the `production_process` fixture do not exist |

The 12 that pass at release check unchanged behaviour or the test machinery: the
oracle, the spies, the static no-pytest-import check, a `~` override under a test HOME,
the prune default under SUBFLEET_HOME, and the 7 home spellings release already
recognized. `test_a_home_spelled_through_a_store_is_decided_without_a_stat` was added
after this run; the `no-lexical-spelling` mutant shows it fails without the lexical
check.

One access went through. The spies leave this process's own interpreter alone, and the
first job's `.venv` sat in its checkout under `~/.subfleet/worktrees`, so a metadata lookup
of one of its ancestors passes. At release `production_state_root`'s `resolve()`
therefore ran `lstat` on `~/.subfleet` itself (the directory's metadata; nothing in it
was listed, opened or written) before the test failed on its return value.

**The same gap in 20 more tests.** The first post-fix hold refused 20 test cases that
replaced `os.environ` the same way: 17 in `tests/fake/test_launch_env_daemon.py`, the 2
in `tests/unit/test_launch_env_mutations.py` that call them, and 1 in
`tests/unit/test_gate_service.py`. Each in-process daemon's admission or status reached
`_desktop_in_use` → `sessions.registry.listing()` → `transcripts.claude_dir()`
(traceback in the hold's JUnit report). Their dicts held no marker, no
`SUBFLEET_CLAUDE_DIR` and no HOME. So at release, by that code path, each listed the
person's live `~/.claude/sessions` and read its rows (read-only; `ps` on the rows'
pids). This is deduced from the code and the post-fix traceback; it was not run at
release, because running it would read the live store. Two doctor cases in
`tests/unit/test_doctor_ca_bundle.py` hit `cli.launchd_plist_path`; their dict set HOME
to the test's own directory, so at release they read only that.

## The fix

**Process-level detection (`home_paths.pytest_loaded`).** The process rule is
`"_pytest" in sys.modules or "pytest" in sys.modules`. I chose pytest's own modules
over a conftest sentinel:

- *A test cannot miss it.* pytest's entry point imports `_pytest` and `pytest` before it
  loads any conftest, plugin or test module, and they stay in `sys.modules` for the life
  of the process. The rule therefore holds during configure, collection, every test and
  teardown, whichever conftest ran (a file run from outside `tests/`, `--noconftest`, a
  pytester run), across `importlib.reload(home_paths)`, and however a test replaced
  `os.environ`. A conftest sentinel misses the first three, and a reload resets it.
  Only a test that deliberately deletes both entries from `sys.modules` defeats it,
  which is no more an accident than editing the code.
- *A production run cannot spoof it into loosening.* No module under `subfleet/`
  imports `pytest` or `_pytest` (`grep -rn pytest subfleet/` finds only comments,
  strings and the `under_pytest`/`PYTEST_*` names; the static test
  `test_no_subfleet_module_imports_pytest` walks every module's imports, including
  `importlib.import_module` and `__import__`). The package depends on the standard
  library alone. So a production process holds these modules only if something imports
  pytest into it, and the rule can only add refusals: a false positive makes a
  production fallback raise `RuntimeError` naming its variable, never resolve
  anything it otherwise would not.

**Folded into every refusal.** For this process `home_paths.under_pytest()` is now
`pytest_process() or <the environment rule>`; `under_pytest(environ)`, for a child
given an environment, keeps the environment rule alone. Every refusal already asked
`under_pytest()`: `home_path`'s missing-override refusal and its `test_only` seam,
`production_state_root`, `protected_provider_homes`, `adapters/claude_mcp.global_config_path`,
`host_push._environment`, `credentials.keychain_command` and the keychain exec guard's
`_guard_active`. So each now refuses when either rule says pytest, with no call site
changed. `launch_env._selected` passes the parent's probe explicitly, so session-name
selection for children is unchanged (C-12.3).

**The one seam (`production_process`, `tests.conftest.waive_process_rule`).** It
replaces `home_paths._process_rule_waived` for the test's monkeypatch. The waiver is
honoured inside production code, which reads HOME again at every call:
`pytest_process()` returns `is_account_home(Path.home())` while waived, so the
account's HOME by any spelling, or no HOME at all (which `Path.home()` reads through
`pwd` as the account's), keeps the rule.

**Spellings beyond the reported shape.** Writing the property's HOME spellings
turned up two more ways the account's home reached `Path.resolve`; the release run
above confirms both:

- `is_account_home` compared the spelling and then `samefile`. A HOME spelled
  `home/missing/..` fails every stat (the component is missing) but `Path.resolve`
  (non-strict) collapses it lexically, so `production_state_root` built the account's
  `~/.subfleet` and resolved it. `is_account_home` now also compares the lexically
  normalized spelling (`os.path.abspath`, no stat) and the identity of the path
  `Path.resolve` would build on (`os.path.realpath`, then `samefile`). The second
  catches `<tmp>/missing/../link` with `link` pointing at the home, and on macOS
  `/USERS/<name>/missing/..`. The lexical check comes first, so `home/.subfleet/..`
  is decided without a stat through the store.
- An override beginning `~` (`SUBFLEET_HOME=~/.subfleet`) expands through HOME, and
  without one through `pwd`, to the account's home. In a pytest process `home_path`
  now refuses such an override (`_names_account_home`). Under a test's own HOME it is
  the test's and resolves there.

## Every fallback to the account's home under `subfleet/`

Found with an AST scan for `Path.home()`, `pwd.getpwuid`, `HOME` reads, `~`-rooted
constants and every `expanduser` call (scratch script, not committed), each site read.

Through `home_path`'s default, or guarded by `under_pytest()` (all now take the
process rule):

| Site | Default |
|---|---|
| `client.state_root` (`cli._root`, `Client`, offline reads) | `SUBFLEET_HOME`, `~/.subfleet` |
| `daemon.main`, `importer.main`, `guard/preflight._state_root` | `SUBFLEET_HOME`, `~/.subfleet` |
| `hooks.settings_path`, `doctor.check_never_rules` | `~/.claude/settings.json` |
| `notify_push.claude_dir`, `sessions/transcripts.claude_dir` | `~/.claude` |
| `sessions/mirror.store_dir`, `sessions/revive.session_store_dir` | `~/Library/Application Support/Claude/claude-code-sessions` |
| `sessions/desktop.log_path` | `~/Library/Logs/Claude/main.log` |
| `home_paths.codex_app_home` | `~/.codex` |
| `home_paths.desktop_login_path` (`capacity`) | `~/.claude.json` |
| `home_paths.provider_home` (`importer`, `lanes_transfer`) | `~` |
| `home_paths.protected_provider_homes` (`conversations/service`) | `~/.claude`, `~/.codex`, and HOME's |
| `home_paths.production_state_root` (`conversations/service`) | `~/.subfleet` |
| `adapters/claude.ClaudeAdapter._config_projects_dir` (`_launch_home`) | the launch's HOME's `.claude` |
| `adapters/claude_mcp.global_config_path` | `~/.claude.json`, `~/.claude` (guarded by `under_pytest()`) |
| `credentials.keychain_command` | `~/bin/agent-secret` |
| `cli.launchd_plist_path` | `~/Library/LaunchAgents/com.subfleet.daemon.plist` |
| `host_push._credential_paths`, `_environment` | HOME, `XDG_CONFIG_HOME` (guarded by `under_pytest()`) |
| `importer.import_v1`, `import_legacy_cockpit` | v1 state, delegate state, v1 roster, reset hold |
| `lanes_transfer` | v1 LaunchAgents, roster and state |

Routed through `home_path` by this branch:

| Site | Was | Now |
|---|---|---|
| `compat.v1_binary` | `~/chief-of-staff/subfleet/bin/subfleet`, the person's v1 install, which runs against v1's live state | `home_path("SUBFLEET_V1_BIN", …, test_only=True)`; `SUBFLEET_V1_BIN` joins `SAFE_HOME_PATHS` (`legacy/bin/subfleet`) |
| `python -m subfleet.prune_decisions` | `--state-root` default `os.environ.get("SUBFLEET_HOME") or "~/.subfleet"` | default `None`, then `home_path("SUBFLEET_HOME", lambda: "~/.subfleet")` after parsing; unchanged outside pytest |

The other `expanduser` calls expand a value the caller supplied: a CLI argument (`-C`,
`-p`, `-o`, `--state-root`), a lane's home or credential reference, a policy value
(`claude_cards.logins_dir`, the mirror's archive glob), or a function's parameter. None
supplies a default of its own. Outside a session HOME is the test's isolated home, so a
supplied `~` value lands there.

The brief's line references `credentials.py:226` and `:323` do not match this tree; at
`473fe2941` those lines are in the exec guard's wrapper tables. The `credentials`
callers of `under_pytest()` are `_guard_active` and `keychain_command`, both covered
above.

## Tests

New files. Counts are collected test cases; the final counts per file are in Runs.

`tests/unit/test_live_store_guard_st3.py` (C-20.6, D-ST3):

- **Regressions under a plain environment** (`{}` and `{"PATH": ...}`), each under spies
  that refuse, before the call, `os` path calls (`chmod`, `mkdir`, `stat`, `lstat`,
  `open`, `listdir`, `scandir`, `unlink`, `rename`, `replace` and more), `open`,
  `io.open`, `io.FileIO`, `sqlite3.connect` (path or URI), Unix-socket `connect`, and
  any `subprocess.Popen`, on a spelling beneath the account's stores, v1's tree,
  Claude's log and LaunchAgents, delegate state or the agent-secret helper:
  `cmd_daemon_start` (with `_daemon_alive` recording, so no live socket is reached),
  `production_state_root`, every D-ST1 resolver (`RESOLVERS`, 24 defaults),
  `home_path` with each store default under four option sets and both `os.environ`
  and a given `env`, `protected_provider_homes`, `codex_app_home`,
  `desktop_login_path`, `provider_home`, `client.state_root({})`,
  `host_push._credential_paths` and `_environment`, `keychain_command`'s helper
  default, `compat.v1_binary` and `prune_decisions.main`.
- **The process rule**: holds whatever os.environ holds; a child that loads no pytest
  keeps the marker rule, and one that imports pytest is a test process (4 cases);
  no `subfleet/` module imports pytest (static).
- **The seam**: waives the rule under a test HOME and never under any spelling of the
  account's home or with no HOME; keeps the live root refused under the account's HOME;
  restores production defaults under a test HOME.
- **Spellings**: `production_state_root` and `protected_provider_homes` refuse 10 (on
  macOS) spellings of the account's home; a link reached past a missing component;
  `home/<store>/..` decided with no stat through the store; a `~` override refused
  with HOME unset or the account's, and honoured under a test HOME.
- **Property** (Hypothesis, derandomized, 150 examples, no database): over any subset
  of the session's environment names plus up to three junk names, each value the
  session's, a junk string, a test path, or (for HOME) a spelling of the account's
  home, no guarded function (30 of them) returns a path at or under an account store,
  judged by spelling and by the parent's identity, with the spies on.
- **Not vacuous**: the oracle names each store by each spelling and nothing else; each
  spy refuses a live spelling before its call.

`tests/unit/test_live_store_guard_st3_mutations.py`: each mutant, applied to a shadow
copy, must fail its named test in a fresh child while the unmutated control passes:

| Mutant | Named test |
|---|---|
| `no-process-rule` (the brief's: `pytest_loaded` returns False) | `test_daemon_start_refuses_the_live_root_before_any_access` |
| `process-rule-not-folded` (`under_pytest()` without the process rule) | `test_production_state_root_refuses` |
| `seam-ignores-home` (a waiver honoured under any HOME) | `test_the_seam_keeps_the_live_root_refused_under_the_accounts_home` |
| `no-tilde-refusal` | `test_a_tilde_override_naming_the_accounts_home_is_refused` |
| `no-resolved-spelling` (no `realpath` identity) | `test_a_home_spelled_through_a_link_past_a_missing_component_is_the_accounts` |
| `no-lexical-spelling` (no `abspath` spelling) | `test_a_home_spelled_through_a_store_is_decided_without_a_stat` |
| `v1-default-unguarded` | `test_the_v1_install_default_is_refused` |
| `prune-default-unguarded` | `test_prune_decisions_state_root_default_is_refused` |

The first job's first post-fix hold killed seven of the eight. `no-lexical-spelling`
survived there, because its named test was then the spelling test above. That test's
spies allow metadata lookups of the `.venv`'s ancestors, and that job's `.venv` sat
under `~/.subfleet`. The mutant now names the stat-recording test. At `d6e7fd601`, in
the second job's first attempt's hold h1, every control passed and all eight mutants
failed their named tests (8 passed). At `b38cc9f2f`, in this job's H1, they did again
(8 passed, Runs).

## Tests changed, and why

| Test | Why |
|---|---|
| `test_live_store_guard_round4.py::test_session_requires_the_original_sentinel`, `::test_cleanup_expires_marker_when_store_directory_survives`, `::test_new_session_at_same_path_does_not_revive_old_capture` | They read the environment rule's session lifetime in this process (`not under_pytest()` once the sentinel is gone). They take `production_process`; their HOME is the test's own. |
| `test_live_store_guard_round6.py::test_stripped_markers_keep_the_session_refusal` | It shows the exec guard's pin refuses once the markers are stripped. The process rule would refuse by itself, so it takes `production_process`. |
| `test_live_store_guard_round6.py::test_python_child_of_the_session_is_guarded_from_import` | Its child imports the keychain safety net, which imports pytest, so the child is a test process by the process rule. It waives the rule (`waive_process_rule`) before stripping the marker, so the pin is again what refuses. |
| `test_live_store_guard_round7_mutations.py` (`pin` probe, `m15`) | The `pin` probe's child imports pytest; it waives the rule so mutant m03 (pins dropped) still fails. m15's exact original is `is_account_home`'s new identity line. |
| `test_compat.py::test_the_v1_binary_is_never_this_process` | It deleted `SUBFLEET_V1_BIN` and patched the default; under pytest the override is now the v1 seam, so it names an absent install there. |
| `test_launch_env_allowlist.py::test_claude_transcript_location_uses_selected_launch_environment` | Its `default` case replaces os.environ with a plain dict and expects production's HOME fallback for the projects folder. It takes `production_process`; its HOME is the test's. |
| `tests/fake/test_launch_env_daemon.py` (`daemon_spawn_environment` and 2 more dicts) | The 17 refused cases above: each replaced os.environ for a polluted parent. They keep the test's own `SUBFLEET_CLAUDE_DIR` (`tests.conftest.isolated_paths`); it is an allowlisted name, so the environment assertions are unchanged. |
| `tests/unit/test_gate_service.py::test_gate_and_merge_helpers_receive_exact_policy` | The same, through the gate's admission. |
| `tests/unit/test_launch_env_mutations.py::test_named_transcript_regression_kills_ambient_mutant` | It calls the seamed allowlist test directly, so it takes `production_process` and passes it on. |
| `tests/unit/test_doctor_ca_bundle.py::test_doctor_version_check_uses_root_policy_environment` | It models doctor in a production environment under its own HOME (its launchd row reads HOME's default plist), so it takes `production_process`. |

## Contract

C-20.6 is amended in place, with a change-log entry dated 2026-10-20: the process
rule, the child rule, the seam and its HOME condition, the `~` override and spelling
refusals, and the two routed defaults. It states the rule's boundary: an override that
names a path other than through `~` is the test's own selection.

## Boundaries, not changed here

- A child a test launches after replacing os.environ gets no session names
  (`launch_env._selected` reads the parent's probe), so that child is an ordinary
  process. This is the hub's decision: children keep the environment-marker rule.
- The keychain exec guard still installs at configure or at import with a marker;
  once installed it is active in every pytest process (it asks `under_pytest()`).
- Path aliases (a symlink into a store) remain out of C-20.6's scope, as before.

## Runs

**Result:** every file in `tests/unit` and `tests/fake` ran at `b38cc9f2f`, the merge.
The tree is `d6e7fd601`'s code with #237's test change. Nothing is unrun, and nothing
fails:

| Directory | Files | Tests | Passed | Skipped | Expected failures | Failed |
|---|---|---|---|---|---|---|
| `tests/unit` | 327 | 13,137 | 13,136 | 1 | 0 | 0 |
| `tests/fake` | 106 | 1,858 | 1,856 | 0 | 2 | 0 |
| Total | 433 | 14,995 | 14,992 | 1 | 2 | 0 |

These counts take the serial rerun's outcome (R1, below) for the 6 tests it reran.
Before that rerun, the first pass had 2 failures and 4 more skips; all 6 passed in R1.
The briefed files are 52 of the 433, and all ran in H1 with no failure. Per file:

| Group | File (under `tests/`) | Passed |
|---|---|---|
| 1 | `unit/test_live_store_guard.py` | 324 |
| 1 | `unit/test_live_store_guard_round10.py` | 60 |
| 1 | `unit/test_live_store_guard_round10_mutations.py` | 13 |
| 1 | `unit/test_live_store_guard_round3.py` | 22 |
| 1 | `unit/test_live_store_guard_round4.py` | 8 |
| 1 | `unit/test_live_store_guard_round5.py` | 11 |
| 1 | `unit/test_live_store_guard_round6.py` | 112 |
| 1 | `unit/test_live_store_guard_round7.py` | 50 |
| 1 | `unit/test_live_store_guard_round7_mutations.py` | 23 |
| 1 | `unit/test_live_store_guard_round8.py` | 100 |
| 1 | `unit/test_live_store_guard_round8_mutations.py` | 4 |
| 1 | `unit/test_live_store_guard_round9.py` | 89 |
| 1 | `unit/test_live_store_guard_round9_mutations.py` | 12 |
| 1 | `unit/test_live_store_guard_st3.py` | 137 |
| 1 | `unit/test_live_store_guard_st3_mutations.py` | 8 (all 8 mutants killed, each control passing) |
| 1 | `unit/test_live_store_guard_state_root.py` | 22 |
| 2 | `unit/test_claude_mcp.py` | 32 |
| 2 | `unit/test_claude_mcp_launch.py` | 11 |
| 2 | `unit/test_host_push.py` | 118 |
| 2 | `unit/test_host_push_admission.py` | 15 |
| 2 | `unit/test_host_push_build.py` | 10, and 1 skipped |
| 2 | `unit/test_host_push_build_budget.py` | 15 |
| 2 | `unit/test_host_push_cli.py` | 7 |
| 2 | `unit/test_host_push_config_cost.py` | 7 |
| 2 | `unit/test_host_push_copy.py` | 11 |
| 2 | `unit/test_host_push_export.py` | 10 |
| 2 | `unit/test_host_push_intake.py` | 13 |
| 2 | `unit/test_host_push_metadata.py` | 33 |
| 2 | `unit/test_host_push_mutations.py` | 24 |
| 2 | `unit/test_host_push_names.py` | 13 |
| 2 | `unit/test_host_push_quarantine_restart.py` | 8 |
| 2 | `unit/test_host_push_restart.py` | 4 |
| 2 | `unit/test_host_push_retention.py` | 2 |
| 2 | `unit/test_host_push_storage_recovery.py` | 2 |
| 2 | `unit/test_host_push_storage_retry_budget.py` | 60 |
| 2 | `unit/test_host_push_store_completion_pr183.py` | 2 |
| 2 | `unit/test_host_push_store_interleavings.py` | 26 |
| 2 | `unit/test_host_push_trees.py` | 5 |
| 2 | `unit/test_host_push_validated_top.py` | 8 |
| 2 | `unit/test_launch_env_allowlist.py` | 27 |
| 2 | `unit/test_launch_env_helpers.py` | 20 |
| 2 | `unit/test_launch_env_mutations.py` | 41 |
| 2 | `unit/test_launch_env_policy.py` | 26 |
| 2 | `unit/test_launch_env_round2.py` | 67 |
| 2 | `unit/test_launch_env_round3.py` | 67 |
| 2 | `unit/test_launch_env_round4.py` | 40 |
| 2 | `unit/test_launch_env_round5.py` | 39 |
| 3 | `unit/test_cli.py` | 222 |
| 3 | `unit/test_daemon_accept.py` | 5 |
| 3 | `unit/test_daemon_verbs.py` | 20 |
| 4 | `fake/test_state_contract.py` | 37 |
| 4 | `unit/test_test_module_names.py` | 1 |

By group: (1) 16 files, 995 passed; (2) 31 files, 763 passed and 1 skipped; (3) 3 files,
247 passed; (4) 2 files, 38 passed. That is 2,043 passed and 1 skipped, the same counts
as the second job's first attempt (below). The briefed patterns matched 52 files: 16
`test_live_store_guard*`, 8 `test_launch_env*`, 2 `test_claude_mcp*` and 21
`test_host_push*`, plus the five named files. No `test_home_paths*.py` or
`test_credentials*.py` exists in `tests/unit`.

The one skip is `test_host_push_build.py::test_real_repo_admission_pass_measurement`, an
explicit local-repo benchmark that skips by design. The two expected failures are
documented limitations marked `xfail`:

- `fake/test_canonical_identity_fix3.py::test_hung_mount_identity_work_has_a_real_deadline`
  (P3-6);
- `fake/test_admission_properties.py::test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again`
  (C-6.10).

#237's changed file, `unit/test_daemon_connections.py`, passed all 31 of its tests (H3).

### This job's holds

**Setup.**

- Interpreter and packages: GIL CPython 3.14.7 (`sys._is_gil_enabled()` true), pytest
  9.1.1, Hypothesis 6.168.1, `HYPOTHESIS_PROFILE=ci`, and `SUBFLEET_LIVE=0`, as CI sets
  it. The scratch environment came from `uv sync --locked --group dev`, plus
  `pytest-xdist` 3.8.0, installed offline from uv's cache.
- Environment: `PYTHONPATH` was unset; the lane's `SUBFLEET_JOB` and `SUBFLEET_ATTEMPT`
  markers were left as the lane sets them.
- The command: each hold was one pytest process through the hub's wrapper,
  `suite_hold.sh env -u SUBFLEET_RETENTION_DORMANT uv run pytest -q -p no:cacheprovider …`.
  Every hold took slot A; free disk was under the wrapper's 50 GB line, so slot B stayed
  closed.
- Workers: `-n 4 --dist loadfile`, so each file ran whole on one worker. H3 to H5 also
  passed `--no-loadscope-reorder`, keeping the order given.

**How the holds were bounded.** A scratch plugin in the scratch environment (not
committed) stops starting tests once a set time has passed. pytest then tears down and
writes its JUnit report. The time was 960 s for H2, and 840 s for H3 to H5 and R1. The
plugin's backstop would have sent this pytest process SIGINT 150 to 240 s later; it
never fired, so this job signalled nothing.

The counts come from each hold's JUnit report and its collected node ids. A file counts
in the hold in which every one of its collected tests reached an outcome. Every file did
so in exactly one hold.

| Hold | Queued, held, ended (UTC, 2026-10-21) | Load at start | Files | Result | Pytest time |
|---|---|---|---|---|---|
| H1: the briefed run list (all four groups) | 03:14:04, 03:14:04, 03:19:13 | 49.8 | 52 | 2,043 passed, 1 skipped | 304 s |
| H2: the other 381 files | 03:19:32, 03:19:32, 03:38:06 | 44.1 | 86 complete | 8,699 passed; stopped at 960 s | 1,102 s |
| H3: a third of the 295 left | 03:39:59, 03:44:17, 03:51:53 | 47.0 | 93 | 1,440 passed, 1 failed, 1 xfailed | 455 s |
| H4: a third | 03:52:03, 04:03:24, 04:12:33 | 38.8 | 93 | 1,305 passed, 1 failed | 548 s |
| H5: a third | 04:12:55, 04:26:21, 04:35:08 | 18.3 | 109 | 1,499 passed, 4 skipped, 1 xfailed | 527 s |
| R1: serial rerun of H3's and H4's failures and H5's skips | 04:36:11, 04:40:15, 04:41:30 | 22.1 | 3 (6 tests) | 6 passed | 58 s |
| C1: `--collect-only tests/unit tests/fake`, serial | 04:41:48, 05:01:00, 05:01:20 | 61.5 | 433 | 14,995 collected, no errors | 14 s |

The longest a lock was held was 18 m 34 s (H2). After H2's stop, the tests already
running took 142 s to finish. So H3 to H5 stopped 120 s sooner and were split to fit,
and none of them reached its stop.

- **H2's stop.** H2 ran xdist's default order, which sorts files by test count, so some
  of the slowest files started late. When it stopped, 295 files were incomplete or
  unstarted. H2 had also passed 62 tests in 4 of those files; they are not counted, and
  each of those files ran again whole.
- **C1, the collection check.** It collected `tests/unit` and `tests/fake` in one
  process: 14,995 tests with no collection error. Its node ids are exactly the union of
  the ids that H1 to H5 completed.

**The two failures were load timeouts, and both passed when rerun.** Each failed inside
`tests/fake/conftest.py`'s `Harness.start`: the fake daemon had not answered
`daemon.status` within the harness's 5 s wait (`condition timed out after 5s`). The two
tests are:

- `fake/test_census_escapers_daemon.py::test_d_cen1_a_retitled_child_alive_after_the_exit_receipt_is_not_released[retitled-node]`.
  It failed at 5.19 s. The kept state root showed the daemon had started (the first line
  of its `daemon.log`). Its sibling case, `[retitled-nul]`, passed in the same hold in
  3.36 s, so a daemon took over 3 s to start then.
- `fake/test_descriptor_exhaustion.py::test_c16_7_more_clients_than_descriptors_get_answers_and_the_daemon_stays_up`.

This branch changes neither test file, `tests/fake/conftest.py` nor `subfleet/daemon.py`;
its runtime changes are path checks in `subfleet/home_paths.py`, `compat.v1_binary` and
`prune_decisions`. Every other daemon start in those holds succeeded, and both tests
passed in R1, run serially. They are not on `.github/known-flaky.txt`.

**The four skips were uv's cache, and the tests passed once it held the package.**
`unit/test_retention_ordinary_trees.py` builds a `.venv` by running `uv pip install
--offline sortedcontainers==2.4.0`, and skips when uv's cache cannot serve it. That
install failed the same way outside pytest ("sortedcontainers was not found in the
cache"), so the cause was the cache's contents, not the code. This job then installed
that one package online into a throwaway virtualenv in its scratch directory, which
added it to the default uv cache. The same offline install then succeeded, and all 4
tests passed in R1.

### The second job's first attempt (at `d6e7fd601`; not counted above)

These ran at `d6e7fd601` with `pytest-xdist` added to the scratch environment. An
in-process alarm bounded each hold, stopping pytest with teardown. Counts are from each
hold's JUnit report.

| Hold (UTC) | Files | Passed | Failed | Skipped | Errors |
|---|---|---|---|---|---|
| h1, 16:02 to 16:11, serial: the 16 `test_live_store_guard*` files, `_st3` and `_st3_mutations` included | 16 | 995 | 0 | 0 | 0 |
| h2, 17:15 to 17:30, serial: the other briefed files | 36 | 1,048 | 0 | 1 | 0 |
| h3, 18:42 to 19:00, `-n 4`: the other 381 files of `tests/unit` and `tests/fake`, stopped by its alarm after 18 minutes | 53 complete (49 unit, 4 fake) | 7,197 | 0 | 0 | 0 |

h2's skip is `test_host_push_build.py::test_real_repo_admission_pass_measurement`, an
explicit local-repo benchmark that is skipped by design. h3 also passed 156 tests in
files it did not finish, and failed none. That attempt never ran the remaining 328
files; this job's holds ran them all.
