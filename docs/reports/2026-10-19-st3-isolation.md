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

## Review round 1: continuation audit (2026-10-22)

The continuation starts at `27c575d9542d`, after the earlier round-1 attempts.
Its wall starts at 11:11:35 UTC; no new hold starts after 15:41:35 UTC.
The original checkout was clean. The sandbox permits reading shared Git metadata
but writing only in this workspace and temporary folders, so this continuation
commits on `fix/st3-process-isolation-217` in workspace-local `.git-local`
metadata, with the original objects as read-only alternates. Nothing is pushed.

### Decisions checked against the commits since PR head `aa74cd62a`

| Decision | Existing commits and evidence | Status on arrival |
|---|---|---|
| Release merge, keeping both contract histories | `81ef0431d` merges `43f42d83d`, the locally available `origin/release/217`; that ref is an ancestor of `27c575d95` | Done; no fetch or further merge needed |
| 1. Every override gets the same refusal; deterministic draws and four exact regressions | `5fefefe11`, `b5b9c332d`, `093b061cd`: `checked_override`, raw-read call sites, sorted name draws, explicit review inputs | Code/tests done; resolver table and final runs partly done |
| 2. Deterministic `~user` overrides and mutant | `5fefefe11`: account name from pwd, `tilde-user-ignored` mutant with control | Done; rerun required |
| 3. Tilde-spelled account HOME and HOME inside a store | `b5b9c332d`, `093b061cd`: `_tilde_home`, `_home_kind`, bounded waiver and provider-home refusal | Done; rerun required |
| 4. Refuse v1 PATH fallback under pytest | `b5b9c332d`: `compat.v1_binary`; regression and mutant in `5fefefe11` | Done; rerun required |
| 5. Explicit live-root overrides; accept_empty wording; PR body | `b5b9c332d`, `093b061cd`, `27c575d95`: lexical/link walk and own-code handling; `66b465a77`: C-20.6 and change log | Code/contract done; PR body not started |
| 6. Establish daemon condition and report one line | `5fefefe11`, `b5b9c332d`: fresh plain/marked/importing-pytest children, `PersonalPathRefused`, `daemon.run_program` | Code/tests done; report partly done |
| 7. Static plugin-import check and fresh runtime import check | `5fefefe11`: `pytest_*` imports, import-every-module child | Done; rerun required |
| Five invariants, complete file counts, final report | Existing tests cover each invariant; original report stops before round 1 | Partly done; completed by this continuation's results below |

The prior scratch folder contains logs but no round-1 report certifying every
green file, so this continuation reruns all files rather than skipping any on that
evidence. Earlier build results above remain historical and are not counted as this
continuation's results.

**Discovery deviations.** The named brief was absent from the assigned checkout.
Before its full rules were located, one read-only discovery sub-agent was started
and ended; no further delegation is used. Discovery read job prompts and listed
directories under `~/.subfleet`, and printed the inherited environment before
learning the brief forbade those actions. It read no credential file, keychain,
live store or daemon log, and signalled no live process. Subsequent work uses the
hub's brief at `~/reviews/subfleet-hub/st3-2026-10-19/` and the required
`sf-r242f1` scratch folder.

### Round-1 final findings and release merge

**Status:** the requested focused groups plus doctor (32 files, 3,463 cases)
and all 17 ST3 mutation controls pass at
`d2b6cd52c`. Whole-suite validation is incomplete: 4 failures, 7 environment
skips, and 4,835 unrun cases remain. The ledger below names every file, including
every unstarted and partial file. This is not a green whole-suite result.

After H1, re-reading the hub's locally available release ref found `4f5983f4c`
(#223). The earlier check had found `43f42d83d` (#232/#238); no fetch was performed.
`30aa280cb` merges the newer ref. The only conflict was the version-3 change log
in `docs/acceptance-contract.md`: both head entries were retained. Runtime files
merged automatically. A first verification incorrectly required old clause
bodies to survive release's intentional amendments, and the shell continued to
commit after that assertion failed. The follow-up `c53acab2f` also restores one
older change-log entry that release had revised, keeping both historical forms.
The final check separately verifies all 140 entries from this branch's parent,
all 142 entries from release, all 254 clause IDs from both, release's current
clause bodies except C-20.6, and this branch's amended C-20.6. Neither history was
rewritten.

**1. MCP, the override class, and repeatable draws.** The MCP resolver read
`CLAUDE_CONFIG_DIR` raw, bypassing the tilde refusal; `~` could therefore offer
`~/.claude.json`. The inherited fix uses `checked_override`, and its four exact
review inputs are deterministic regressions. The sweep now records every guarded
call below. The continuation additionally found `doctor.check_pythonpath`: it
probed each `PYTHONPATH` entry without the shared check. It now checks all entries
before its first package probe, and preserves the unread behavior under `-E`/`-I`.
The old raw expression is a killed mutant. The property's set iteration was a
separate cause: process-specific hashes changed which value was assigned to which
name. `NAMES` is fixed, and an ordered list strategy replaces the drawn set.
Session options exist for every name, so absent session values do not change the
choice graph. Fresh-child digest checks normalize session-specific path values
before comparing choices.

**2. Named-user tilde overrides.** Random draws did not deterministically cover
`~user`, allowing the reviewer's bare-tilde-only mutant to survive. The tests now
obtain the account name from `pwd.getpwuid(os.getuid()).pw_name`, exercise all three
requested variables, and kill `tilde-user-ignored` with passing controls.

**3. A tilde-spelled HOME.** Literal comparison missed `HOME=~account`, allowing the
production seam to waive the process rule. HOME spellings are now interpreted
through pwd before comparison, including HOME inside a live root; unknown users
remain non-account homes. The waiver re-reads HOME on each call. Regressions cover
the three defaults, the bounded waiver, and protected provider homes.

**4. The v1 PATH fallback.** A missing explicit v1 binary could fall through to
the person's PATH wrapper. A test process now refuses that default and must name
its binary; plain production retains release's fallback. The regression and
`v1-path-fallback` mutant pass.

**5. Explicit roots, empty overrides, and the consumer's base.** The earlier
tilde-only check allowed literal live roots and paths that reached them through
dot segments or links. Overrides now take both the leading-tilde refusal and a
lexical/link-aware live-root check, before access on or beneath a live root.
The process's own checkout/interpreter/environment remain its own, including a
checkout under `~/.subfleet/worktrees`; its link walk starts at that own root.
`accept_empty=True` still returns `""` as the working-directory value, as release
does; C-20.6 names `home_path`, `guard/preflight._state_root` and `daemon.main`.

H2 exposed one additional defect in the inherited sweep: `cache_dir` consumes a
relative override against its supplied state root (C-2.1), but the shared guard
had judged it against cwd. Thus `../shared` from this checkout looked personal
even when the supplied root was temporary. `checked_override(..., relative_to=root)`
now judges that actual consumer path without resolving it first; the raw tilde
refusal still runs first. A stand-in-account regression checks both directions,
and `cache-base-ignored` is killed. Existing doctor and guard-trust assertions
were kept unchanged; no pre-existing test was weakened or waived.

**6. The daemon finding, executed.** The condition is a test process, not an
ordinary production invocation. Fresh children with temporary HOME and a held
dummy `daemon.lock` establish that a plain `python -m subfleet.daemon` resolves
HOME/.subfleet and exits 69 with `another daemon holds daemon.lock`, as release
does. Bare-marker, current-test, and importing-pytest children refuse before
root resolution. `daemon.run_program` converts `PersonalPathRefused` (a RuntimeError)
to exit 7 and one clear line; `daemon.main` still raises it for in-process callers.
No real daemon was started. The production differential passes 221 comparisons:
17 functions across 13 environments, in a fresh child with a stand-in account.
All 18 captured bodies (the 17 functions plus the test-only account-home helper)
were also compared as source and exactly match merged release `4f5983f4c`.

**7. Static and runtime imports.** The static import test now rejects `pytest_*`
plugin names as well as pytest and _pytest. A fresh minimal child imports every
discoverable Subfleet module except the CLI-running `__main__` entry points and
finds no pytest modules or plugins loaded. Static analysis still cannot prove
computed imports, aliased `import_module`, or `exec` on unexecuted paths; the
runtime test covers the paths actually executed during import.

### Final decision status and five invariants

| Brief decision | Final status |
|---|---|
| 1. Shared override refusal, sweep table, four exact inputs, repeatable property | Done, including the additional PYTHONPATH reader; all 16 seeded property runs passed |
| 2. Deterministic ~user cases and mutant | Done |
| 3. Tilde-spelled HOME and bounded waiver | Done |
| 4. v1 PATH default refusal | Done |
| 5. Explicit-root refusal, empty semantics, contract and PR body | Done; relative cache base corrected too |
| 6. Establish daemon condition and one-line marked-child refusal | Done; fresh children and production differential passed |
| 7. Static plugin imports and runtime import-all check | Done; dynamic limitations stated above |
| Briefed groups plus doctor | 32 files, 3,463 passed at the final code head; the extra guard-trust file has the timing failure listed below |
| Whole tests/unit and tests/fake | Partly done; exact outcomes and unrun files below |

| Invariant | Current tests and evidence |
|---|---|
| 1. No guarded resolver offers or touches an account live root, whatever os.environ holds | 271 ST3 cases, including spies, direct/tilde/user/relative/link spellings, own-code handling and per-entry PYTHONPATH checks; property and mutation controls |
| 2. Plain production retains release behavior | Fresh minimal child: 221 base/head comparisons; all 18 baseline bodies match merged release source; import-all check loads no pytest |
| 3. The waiver never holds for a personal HOME, re-read each time | Seam tests, tilde-HOME and live-root-HOME regressions; seam mutant killed |
| 4. Children keep the environment rule | ST3 child tests and the round-4 lifetime tests; importing pytest adds the process rule |
| 5. Derandomized choices are repeatable between processes | Ordered name strategy, normalized 40-draw digests under seeds 0/4/7 in both environments, and the 16 property runs below |

### Path-valued environment resolver sweep

The sweep covers `os.environ`, `os.getenv`, mapping `get`/index reads and dynamic
name selection. Dictionary record accesses and assignments are distinguished from
environment inputs. There are **32 `home_path` call sites for 19 override names**
and **9 `checked_override` call sites for 9 names**: **41 guarded call sites,
27 distinct override names**, because CLAUDE_CONFIG_DIR uses both routes.
A refusal judges relative paths against the consumer's base; only the cache
reader supplies a state-root base. All others retain cwd semantics.
| Variable | Resolver (module:line, function) | Shared check |
|---|---|---|
| `CLAUDE_CONFIG_DIR` | `subfleet/adapters/claude.py:1325`, `ClaudeAdapter._config_projects_dir` | `home_path` |
| `CLAUDE_CONFIG_DIR` | `subfleet/adapters/claude_mcp.py:140`, `global_config_path` | `checked_override` |
| `CLAUDE_CONFIG_DIR` | `subfleet/adapters/claude_mcp.py:146`, `global_config_path` | `home_path` |
| `CLAUDE_CONFIG_DIR` | `subfleet/home_paths.py:465`, `protected_provider_homes` | `home_path` |
| `CLAUDE_LANE_AGENT_SECRET` | `subfleet/credentials.py:594`, `keychain_command` | `checked_override` |
| `CODEX_HOME` | `subfleet/guard/preflight.py:731`, `_lane_home` | `checked_override` |
| `PYTHONPATH` | `subfleet/doctor.py:197`, `check_pythonpath` | `checked_override` |
| `SUBFLEET_AGENT_SECRET` | `subfleet/credentials.py:596`, `keychain_command` | `home_path` |
| `SUBFLEET_CLAUDE_DIR` | `subfleet/notify_push.py:63`, `claude_dir` | `home_path` |
| `SUBFLEET_CLAUDE_DIR` | `subfleet/sessions/transcripts.py:208`, `claude_dir` | `home_path` |
| `SUBFLEET_CLAUDE_SETTINGS` | `subfleet/doctor.py:390`, `check_never_rules` | `home_path` |
| `SUBFLEET_CLAUDE_SETTINGS` | `subfleet/hooks.py:167`, `settings_path` | `home_path` |
| `SUBFLEET_CODEX_APP_HOME` | `subfleet/home_paths.py:453`, `codex_app_home` | `home_path` |
| `SUBFLEET_CODEX_GUARD_CACHE` | `subfleet/guard/preflight.py:308`, `cache_dir` | `checked_override` |
| `SUBFLEET_DAEMON_BIN` | `subfleet/cli.py:2604`, `_daemond_argv` | `checked_override` |
| `SUBFLEET_DELEGATE_STATE` | `subfleet/importer.py:2125`, `import_v1` | `home_path` |
| `SUBFLEET_DESKTOP_LOG` | `subfleet/sessions/desktop.py:81`, `log_path` | `home_path` |
| `SUBFLEET_DESKTOP_LOGIN` | `subfleet/home_paths.py:534`, `desktop_login_path` | `home_path` |
| `SUBFLEET_DEV_APP_EXECUTABLE` | `subfleet/conversations/service.py:327`, `ConversationService._app_executables` | `checked_override` |
| `SUBFLEET_GUARD_TRUST` | `subfleet/guard/preflight.py:192`, `guard_paths` | `checked_override` |
| `SUBFLEET_HOME` | `subfleet/client.py:116`, `state_root` | `home_path` |
| `SUBFLEET_HOME` | `subfleet/daemon.py:10279`, `main` | `home_path` |
| `SUBFLEET_HOME` | `subfleet/guard/preflight.py:736`, `_state_root` | `home_path` |
| `SUBFLEET_HOME` | `subfleet/importer.py:2345`, `main` | `home_path` |
| `SUBFLEET_HOME` | `subfleet/prune_decisions.py:1320`, `main` | `home_path` |
| `SUBFLEET_HOST_CONFIG` | `subfleet/host_push.py:537`, `_credential_paths` | `home_path` |
| `SUBFLEET_HOST_HOME` | `subfleet/host_push.py:535`, `_credential_paths` | `home_path` |
| `SUBFLEET_LAUNCHD_PLIST` | `subfleet/cli.py:3050`, `launchd_plist_path` | `home_path` |
| `SUBFLEET_PROVIDER_HOME` | `subfleet/home_paths.py:540`, `provider_home` | `home_path` |
| `SUBFLEET_RESET_INHIBIT` | `subfleet/importer.py:2137`, `import_v1` | `home_path` |
| `SUBFLEET_SESSION_STORE` | `subfleet/sessions/mirror.py:314`, `store_dir` | `home_path` |
| `SUBFLEET_SESSION_STORE` | `subfleet/sessions/revive.py:152`, `session_store_dir` | `home_path` |
| `SUBFLEET_V1_BIN` | `subfleet/compat.py:516`, `v1_binary` | `home_path` |
| `SUBFLEET_V1_LAUNCH_AGENTS` | `subfleet/lanes_transfer.py:291`, `v1_codex_scope` | `home_path` |
| `SUBFLEET_V1_ROSTER_DIR` | `subfleet/importer.py:2127`, `import_v1` | `home_path` |
| `SUBFLEET_V1_ROSTER_DIR` | `subfleet/lanes_transfer.py:528`, `plan_transfer` | `home_path` |
| `SUBFLEET_V1_STATE` | `subfleet/importer.py:2267`, `import_legacy_cockpit` | `home_path` |
| `SUBFLEET_V1_STATE` | `subfleet/importer.py:2123`, `import_v1` | `home_path` |
| `SUBFLEET_V1_STATE` | `subfleet/lanes_transfer.py:536`, `plan_transfer` | `home_path` |
| `SUBFLEET_V1_STATE` | `subfleet/lanes_transfer.py:555`, `plan_transfer` | `home_path` |
| `TMPDIR` | `subfleet/retention_git.py:466`, `temp_roots` | `checked_override` |

Other path-valued reads are recorded here so forwarding, explicit arguments and
protocol metadata are not mistaken for additional personal-default resolvers.

| Names | Sites | Treatment |
|---|---|---|
| HOME | `home_paths` HOME/default/seam helpers; `adapters/claude._launch_home` | HOME is judged by `personal_home`/`is_account_home` before personal defaults; Claude's launch HOME is evaluated only inside the guarded CLAUDE_CONFIG_DIR default |
| HOME, XDG_CONFIG_HOME, SSH_AUTH_SOCK, PATH | `host_push._environment`, `_credential_paths` | Test HOME/config values come through the two home_path overrides; SSH_AUTH_SOCK is removed under pytest; PATH remains an explicit executable search |
| SUBFLEET_V1_BIN | `compat.v1_binary`'s first env.get | Its companion home_path call is evaluated before any candidate is probed; the PATH fallback is refused in a test process |
| SUBFLEET_RESET_INHIBIT | `importer.import_v1`'s presence test | Only selects a branch; a present value then goes through home_path. An explicit provider-home argument can locate its own default |
| CODEX_HOME | `adapters/codex.probe_status`, `_launch`; `conversations/launch.codex_launch` | These read an explicit lane credential/launch argument, never os.environ as a home fallback; the launch metadata read follows assignment from the lane home. Explicit arguments retain C-20.6's caller-selected-input rule |
| PATH | `doctor.path_matches`, `cli._plist`, `client`/`procs._read`, `credentials._launch_paths`, child selectors | Executable-search lists, not one path override. C-20.6 retains PATH searches; the credential audit guard checks launches for the keychain reader |
| PYTHONPATH | `sessions/nudge.spawn` | Reads the already-selected child environment to prepend this package; no home fallback or filesystem probe. The actual diagnostic reader in doctor is guarded above |
| SSL_CERT_FILE, SSL_CERT_DIR | `provider_env.ProviderEnvironment.additions` via `_present` | Operator values are selected and forwarded. The file read in that method is the explicit policy bundle, not an ambient CA value |
| SUBFLEET_PYTEST_SESSION | `home_paths._session_environment`/`session_alive`; `credentials.install_keychain_exec_guard` | Existing session-directory/sentinel identity protocol, not a personal-store default. The credentials read only records the session tuple; child marker/lifetime selection retains its environment rule |
| HOME, PATH, SHELL, TMPDIR, SSH_AUTH_SOCK, SSL_CERT_FILE/SSL_CERT_DIR and session overrides | `launch_env._present`/`_selected`, `provider_env`, guardian/helper callers | Named child-environment selection and forwarding under C-12.3; consumers that resolve personal defaults are the guarded sites above. Shell/PATH launches remain subject to the credential audit guard |
| CARPOOL_* aliases and SUBFLEET_CODEX_HOMES | `compat.map_env` | Alias copying and presence-only deprecation notices, without path resolution; an aliased value's eventual resolver takes its normal guard |
| SUBFLEET_HOOK_COMMAND | `hooks.hook_command` | An explicit command string, not a path-valued personal-default resolver; fixture commands require isolated inputs |

### This continuation's runs

GIL CPython 3.14.7, pytest 9.1.1, Hypothesis 6.168.1, ci profile; the property
explicitly uses 150 examples and no database. Locked setup used the required
setup wrapper and the same scratch venv throughout. pytest-xdist 3.8.0 and
execnet 2.1.2 were installed offline through that wrapper from locally repacked
cached wheel contents after a cache-index lookup failed. No package-index call
was made beyond locked uv sync. All venv/cache/wheel/probe/output files lived in
the required `sf-r242f1` folder; tests had temporary HOME and overrides.

Every pytest invocation used `suite_hold.sh`. H1 was serial; H2–H4 used four
workers with loadfile distribution. The property hold ran its 16 pytest
invocations sequentially, as the brief explicitly permits. Output stayed in
scratch. No prior attempt's green files were used to skip this continuation's
runs.

| Hold | Queued / acquired / ended (UTC, 2026-10-22) | Tested code | Outcome | Time held |
|---|---|---|---|---|
| H1, four focused files | 11:20:04 / 13:32:40 / 13:34:02 | 27c575d95 | 610 passed | about 82 s |
| Seeds, property alone | 13:45:14 / 13:57:25 / 13:59:55 | 6f0ce7538 | 16 passed; 2,400 passing examples | about 150 s |
| H2, 32 focused files | 14:01:20 / 14:15:36 / 14:17:02 | 6f0ce7538 | 3,460 passed, 1 failed: cache-base defect, fixed | about 86 s |
| H3, those files plus guard-trust | 14:22:12 / 14:48:31 / 14:52:41 | d2b6cd52c | 3,560 passed, 1 failed: fake-version startup timing | about 250 s |
| H4, 411 remaining files plus timing-case rerun | 14:53:48 / 14:57:21 / 15:16:48 | d2b6cd52c | 7,376 passed, 3 failed, 7 skipped; interrupted at backstop | 19 m 27 s |
| H5, unfinished files and failures | 15:19:01 / 15:41:57 / about 15:42:07 | d2b6cd52c | interrupted during collection; no collected-node file, JUnit or test outcomes | about 10 s |

**Bounds and cutoff.** The initial soft stop set pytest's session flag, which
xdist does not use to end its own loop; H4 therefore reached the in-process
SIGALRM backstop at 1,140 seconds. Teardown completed inside 20 minutes.
The next runner sets xdist's own stop flag as well. Neither helper is committed.
H4's JUnit contains four anonymous reporters from interruption; they are excluded
from the counts, which then agree exactly with pytest's summary.

At the 4 h 30 m cutoff (15:41:35), signalling this job's recorded H5 wrapper PID
was denied by the sandbox with EPERM. It acquired a slot 22 seconds after the
cutoff while execution-session interruption was being arranged. That control
ended the session with exit 130 and interrupted pytest during collection about
ten seconds after acquisition. No test cases ran in H5. This late acquisition
is recorded as a rule deviation, not counted as completed validation; no further
hold was started.

**Unresolved failures.**

- `tests/unit/test_guard_trust.py::test_version_timeout_is_reported_as_a_timeout`:
  the 0.5-second fake-version timeout fired before the fake wrote report.json.
  The timeout classification assertions passed; reading that absent report
  failed. This appears to be startup timing under load, but its scheduled rerun
  was not reached, so the failure remains unresolved.
- Three `test_importer.py` cases: `test_states_map_from_rc`,
  `test_the_daemon_never_adopts_an_external_run`, and
  `test_a_live_claude_process_outside_subfleet_keeps_history_out_and_fences_nothing[True]`.
  Native process inspection is unavailable in this sandbox. Importer's
  `_external_run` deliberately treats `procs.InspectionError` as a live external
  run (C-5.3), causing the first two expectations to differ; unavailable process
  environment inspection also prevents recognition of the Subfleet-owned sleeper
  in the third. Seven other cases explicitly skipped for unavailable native
  ps/sysctl inspection (one hook case, six legacy-hold cases). No production
  process-ownership logic was weakened to make these tests pass.

The brief's separate `test_prune_restore.py` flake was not reached. It was not
changed. Full remaining coverage needs another authorized run with process
inspection available where the native tests require it.

| Hash seed | Lane environment | Minimal environment | Passing examples per run |
|---:|---|---|---:|
| 0 | 1 passed | 1 passed | 150 |
| 1 | 1 passed | 1 passed | 150 |
| 2 | 1 passed | 1 passed | 150 |
| 3 | 1 passed | 1 passed | 150 |
| 4 | 1 passed | 1 passed | 150 |
| 5 | 1 passed | 1 passed | 150 |
| 6 | 1 passed | 1 passed | 150 |
| 7 | 1 passed | 1 passed | 150 |

The seeded run predates only the cache consumer-base refinement, not a strategy
change. After that refinement, H3 reran the property and normalized child-digest
test successfully at the final code head.

### Final per-directory and per-file ledger

These are unique current-head cases from H3 and H4, not summed rerun totals.
A complete file has an outcome for every collected case. Skipped cases are
reported as skips. Partial and unstarted cases remain unrun. H1 and H2 stay
historical in the hold table; the 16 standalone property runs are counted
separately above. The union of collection manifests covers all 444 files and
15,782 cases, with no missing file.

| Directory | Files | Collected | Passed | Failed | Skipped | Unrun | Complete / partial / unstarted files |
|---|---:|---:|---:|---:|---:|---:|---|
| tests/unit | 332 | 13,652 | 10,071 | 4 | 7 | 3,570 | 100 / 3 / 229 |
| tests/fake | 112 | 2,130 | 865 | 0 | 0 | 1,265 | 12 / 1 / 99 |


**Partial files:**

| File | Passed | Unrun |
|---|---:|---:|
| `tests/fake/test_probe_turn.py` | 41 | 2 |
| `tests/unit/test_retention_shared_folders.py` | 36 | 26 |
| `tests/unit/test_sessions_mirror.py` | 38 | 3 |
| `tests/unit/test_wait_hub.py` | 33 | 11 |


<details>
<summary>All 444 files, with exact counts and coverage status</summary>

| File | Collected | Passed | Failed | Skipped | Unrun | Coverage |
|---|---:|---:|---:|---:|---:|---|
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | 0 | 14 | unstarted |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | 0 | 16 | unstarted |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | 0 | 32 | unstarted |
| `tests/fake/test_admission_priority.py` | 32 | 0 | 0 | 0 | 32 | unstarted |
| `tests/fake/test_admission_properties.py` | 28 | 0 | 0 | 0 | 28 | unstarted |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 | 19 | unstarted |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 | 0 | 0 | 11 | unstarted |
| `tests/fake/test_admission_route_isolation.py` | 93 | 93 | 0 | 0 | 0 | complete |
| `tests/fake/test_admission_stateful.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/fake/test_admission_visibility.py` | 43 | 43 | 0 | 0 | 0 | complete |
| `tests/fake/test_batch_label.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/fake/test_canonical_identity.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 0 | 0 | 0 | 21 | unstarted |
| `tests/fake/test_canonical_identity_fix3.py` | 18 | 0 | 0 | 0 | 18 | unstarted |
| `tests/fake/test_canonical_identity_fix4.py` | 11 | 0 | 0 | 0 | 11 | unstarted |
| `tests/fake/test_canonical_identity_fix5.py` | 14 | 0 | 0 | 0 | 14 | unstarted |
| `tests/fake/test_capacity_steer_daemon.py` | 72 | 72 | 0 | 0 | 0 | complete |
| `tests/fake/test_capacity_wait_backoff.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/fake/test_census_escapers_daemon.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/fake/test_census_recycled_pid_daemon.py` | 29 | 0 | 0 | 0 | 29 | unstarted |
| `tests/fake/test_codex_ca_bundle_fake.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/fake/test_codex_turn_completed_fake.py` | 109 | 109 | 0 | 0 | 0 | complete |
| `tests/fake/test_conversation_pools.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/fake/test_conversation_wakes_fake.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/fake/test_crash_matrix.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/fake/test_daemon_contract.py` | 21 | 0 | 0 | 0 | 21 | unstarted |
| `tests/fake/test_daemon_descriptors.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/fake/test_daemon_launch.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/fake/test_daemon_stacks.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/fake/test_descriptor_exhaustion.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/fake/test_detached_cwd_pins.py` | 18 | 0 | 0 | 0 | 18 | unstarted |
| `tests/fake/test_detached_retention_fence.py` | 89 | 89 | 0 | 0 | 0 | complete |
| `tests/fake/test_enrollment_guardian.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_fake_provider.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/fake/test_folder_spelling.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/fake/test_frozen_stream_nonregular.py` | 140 | 140 | 0 | 0 | 0 | complete |
| `tests/fake/test_gate_end_to_end.py` | 11 | 0 | 0 | 0 | 11 | unstarted |
| `tests/fake/test_identity_rollback.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/fake/test_idle_cost.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/fake/test_interactive_claude_titles.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/fake/test_interactive_steer.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/fake/test_lane_fault.py` | 20 | 0 | 0 | 0 | 20 | unstarted |
| `tests/fake/test_lane_pilot.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/fake/test_lanes_enroll.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/fake/test_lanes_reenroll.py` | 19 | 0 | 0 | 0 | 19 | unstarted |
| `tests/fake/test_lanes_reenroll_keychain.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_launch_env_daemon.py` | 49 | 49 | 0 | 0 | 0 | complete |
| `tests/fake/test_launch_env_providers.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/fake/test_launch_gates_above_stdio.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/fake/test_lost_acknowledgement.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/fake/test_main_refusal_write_dir.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/fake/test_mcp_job_contract.py` | 12 | 0 | 0 | 0 | 12 | unstarted |
| `tests/fake/test_native_maintenance_startup.py` | 41 | 41 | 0 | 0 | 0 | complete |
| `tests/fake/test_notice_delivery.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/fake/test_notice_header.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/fake/test_picker_native.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/fake/test_policy_sandbox.py` | 25 | 0 | 0 | 0 | 25 | unstarted |
| `tests/fake/test_probe_cancel.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/fake/test_probe_turn.py` | 43 | 41 | 0 | 0 | 2 | partial |
| `tests/fake/test_prompt_seam.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/fake/test_provider_active_cap_fake.py` | 60 | 60 | 0 | 0 | 0 | complete |
| `tests/fake/test_provider_cap_review_evidence.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/fake/test_provider_qos.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/fake/test_record_secrecy.py` | 44 | 44 | 0 | 0 | 0 | complete |
| `tests/fake/test_request_routing.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/fake/test_reserve_authorization.py` | 12 | 0 | 0 | 0 | 12 | unstarted |
| `tests/fake/test_reserve_authorization_validation.py` | 11 | 0 | 0 | 0 | 11 | unstarted |
| `tests/fake/test_reset_credit_incident.py` | 19 | 0 | 0 | 0 | 19 | unstarted |
| `tests/fake/test_resume_cli.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_resume_contract.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/fake/test_retry_for_good_refusals.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/fake/test_review130_ab.py` | 12 | 0 | 0 | 0 | 12 | unstarted |
| `tests/fake/test_review_r185_adversarial.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_review_r223_wall_probes.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/fake/test_route_view.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/fake/test_routing_end_to_end.py` | 16 | 0 | 0 | 0 | 16 | unstarted |
| `tests/fake/test_routing_tier_preferences.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/fake/test_salvage_adversarial.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/fake/test_salvage_finalization.py` | 47 | 47 | 0 | 0 | 0 | complete |
| `tests/fake/test_sessions_end_to_end.py` | 33 | 0 | 0 | 0 | 33 | unstarted |
| `tests/fake/test_state_contract.py` | 37 | 37 | 0 | 0 | 0 | complete |
| `tests/fake/test_state_fixture_processes.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/fake/test_status_payload.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/fake/test_suggestions_fake.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_tier_hold_by_demand.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/fake/test_timers_busy_admission.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_timers_end_to_end.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/fake/test_turn_listing.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/fake/test_turn_retention_cwd.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/fake/test_turn_retention_cwd_properties.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_turn_retention_job_directory.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/fake/test_turn_retention_job_properties.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_turn_retention_quarantine.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/fake/test_turn_trees.py` | 18 | 0 | 0 | 0 | 18 | unstarted |
| `tests/fake/test_turn_wait_reasons.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/fake/test_unadmittable_pin_admission.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/fake/test_unadmittable_pin_review.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/fake/test_wall_cancel_races.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/fake/test_wall_inspection_order.py` | 18 | 0 | 0 | 0 | 18 | unstarted |
| `tests/fake/test_wall_running_time.py` | 35 | 0 | 0 | 0 | 35 | unstarted |
| `tests/fake/test_wall_stop_deliveries.py` | 25 | 0 | 0 | 0 | 25 | unstarted |
| `tests/fake/test_workspace_contract.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/fake/test_workspace_enospc.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/fake/test_workspace_transient.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/fake/test_writable_fanout.py` | 28 | 0 | 0 | 0 | 28 | unstarted |
| `tests/fake/test_writer_retention_fence.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | 33 | unstarted |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 | 20 | unstarted |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_capacity_view.py` | 20 | 0 | 0 | 0 | 20 | unstarted |
| `tests/unit/test_census_counted.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_census_procargs.py` | 52 | 52 | 0 | 0 | 0 | complete |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | 25 | unstarted |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | 20 | unstarted |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 | 34 | unstarted |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 | 14 | unstarted |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 72 | 0 | 0 | 0 | complete |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 48 | 0 | 0 | 0 | complete |
| `tests/unit/test_claude_adapter.py` | 170 | 170 | 0 | 0 | 0 | complete |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | 19 | unstarted |
| `tests/unit/test_claude_cards.py` | 96 | 96 | 0 | 0 | 0 | complete |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_claude_identity.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/unit/test_claude_mcp.py` | 32 | 32 | 0 | 0 | 0 | complete |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 11 | 0 | 0 | 0 | complete |
| `tests/unit/test_claude_stream.py` | 60 | 60 | 0 | 0 | 0 | complete |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 63 | 0 | 0 | 0 | complete |
| `tests/unit/test_claude_turn.py` | 69 | 69 | 0 | 0 | 0 | complete |
| `tests/unit/test_claude_usage.py` | 28 | 0 | 0 | 0 | 28 | unstarted |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_cli.py` | 248 | 248 | 0 | 0 | 0 | complete |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | 18 | unstarted |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/unit/test_client_availability.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_codex_adapter.py` | 172 | 172 | 0 | 0 | 0 | complete |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 46 | 0 | 0 | 0 | complete |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_codex_probe.py` | 55 | 55 | 0 | 0 | 0 | complete |
| `tests/unit/test_codex_shim.py` | 31 | 0 | 0 | 0 | 31 | unstarted |
| `tests/unit/test_codex_turn.py` | 67 | 67 | 0 | 0 | 0 | complete |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 | 41 | unstarted |
| `tests/unit/test_compat.py` | 1615 | 1615 | 0 | 0 | 0 | complete |
| `tests/unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 | 11 | unstarted |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 | 21 | unstarted |
| `tests/unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 | 39 | unstarted |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 0 | 0 | 0 | 26 | unstarted |
| `tests/unit/test_conversation_handoff.py` | 48 | 48 | 0 | 0 | 0 | complete |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 | 28 | unstarted |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_conversation_history.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 44 | 0 | 0 | 0 | complete |
| `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 | 36 | unstarted |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_conversation_service.py` | 100 | 100 | 0 | 0 | 0 | complete |
| `tests/unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 | 18 | unstarted |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_conversation_steer_service.py` | 45 | 45 | 0 | 0 | 0 | complete |
| `tests/unit/test_conversation_store.py` | 32 | 0 | 0 | 0 | 32 | unstarted |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_conversation_titles.py` | 89 | 89 | 0 | 0 | 0 | complete |
| `tests/unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_conversation_wakes.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/unit/test_daemon_connections.py` | 31 | 0 | 0 | 0 | 31 | unstarted |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_daemon_integration.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 86 | 0 | 0 | 0 | complete |
| `tests/unit/test_daemon_qos_tools.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_daemon_settle.py` | 61 | 61 | 0 | 0 | 0 | complete |
| `tests/unit/test_daemon_verbs.py` | 20 | 0 | 0 | 0 | 20 | unstarted |
| `tests/unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 56 | 0 | 0 | 0 | complete |
| `tests/unit/test_descriptors.py` | 16 | 0 | 0 | 0 | 16 | unstarted |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_disk_admission.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/unit/test_disk_floor.py` | 60 | 60 | 0 | 0 | 0 | complete |
| `tests/unit/test_doctor.py` | 44 | 44 | 0 | 0 | 0 | complete |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 | 0 | 16 | unstarted |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_empty_provider_results.py` | 334 | 334 | 0 | 0 | 0 | complete |
| `tests/unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_failed_keep.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_filesystem_identity_agreement.py` | 40 | 0 | 0 | 0 | 40 | unstarted |
| `tests/unit/test_folders.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/unit/test_forget_paced.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_fresh_until.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/unit/test_frozen_provider_streams.py` | 217 | 217 | 0 | 0 | 0 | complete |
| `tests/unit/test_gate_admission.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/unit/test_gate_merge.py` | 59 | 59 | 0 | 0 | 0 | complete |
| `tests/unit/test_gate_replay.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/unit/test_gate_revision.py` | 16 | 0 | 0 | 0 | 16 | unstarted |
| `tests/unit/test_gate_service.py` | 25 | 0 | 0 | 0 | 25 | unstarted |
| `tests/unit/test_gate_verdict.py` | 45 | 45 | 0 | 0 | 0 | complete |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_guard_trust.py` | 98 | 97 | 1 | 0 | 0 | complete |
| `tests/unit/test_guardian.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 | 0 | 14 | unstarted |
| `tests/unit/test_handler_refusals.py` | 317 | 317 | 0 | 0 | 0 | complete |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_hidden_environment.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_hook_cost.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/unit/test_hooks.py` | 73 | 72 | 0 | 1 | 0 | complete |
| `tests/unit/test_hooks_native_guard.py` | 18 | 0 | 0 | 0 | 18 | unstarted |
| `tests/unit/test_host_push.py` | 118 | 118 | 0 | 0 | 0 | complete |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_host_push_build.py` | 11 | 0 | 0 | 0 | 11 | unstarted |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 | 11 | unstarted |
| `tests/unit/test_host_push_export.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 | 33 | unstarted |
| `tests/unit/test_host_push_mutations.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/unit/test_host_push_names.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_host_push_storage_retry_budget.py` | 60 | 60 | 0 | 0 | 0 | complete |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 | 26 | unstarted |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/unit/test_host_push_validated_top.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_importer.py` | 148 | 145 | 3 | 0 | 0 | complete |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | 25 | unstarted |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/unit/test_lanes_transfer.py` | 79 | 79 | 0 | 0 | 0 | complete |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 27 | 0 | 0 | 0 | complete |
| `tests/unit/test_launch_env_helpers.py` | 20 | 20 | 0 | 0 | 0 | complete |
| `tests/unit/test_launch_env_mutations.py` | 41 | 41 | 0 | 0 | 0 | complete |
| `tests/unit/test_launch_env_policy.py` | 26 | 26 | 0 | 0 | 0 | complete |
| `tests/unit/test_launch_env_round2.py` | 67 | 67 | 0 | 0 | 0 | complete |
| `tests/unit/test_launch_env_round3.py` | 69 | 69 | 0 | 0 | 0 | complete |
| `tests/unit/test_launch_env_round4.py` | 40 | 40 | 0 | 0 | 0 | complete |
| `tests/unit/test_launch_env_round5.py` | 39 | 39 | 0 | 0 | 0 | complete |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_legacy_hold.py` | 56 | 50 | 0 | 6 | 0 | complete |
| `tests/unit/test_limit_from_model_text.py` | 95 | 95 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard.py` | 324 | 324 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round10.py` | 60 | 60 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 13 | 13 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 22 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 8 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 11 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 112 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 50 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 23 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 100 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 4 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 89 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 12 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_st3.py` | 271 | 271 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_st3_children.py` | 8 | 8 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_st3_mutations.py` | 17 | 17 | 0 | 0 | 0 | complete |
| `tests/unit/test_live_store_guard_state_root.py` | 22 | 22 | 0 | 0 | 0 | complete |
| `tests/unit/test_lockwatch.py` | 20 | 0 | 0 | 0 | 20 | unstarted |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 | 29 | unstarted |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 | 12 | unstarted |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 | 28 | unstarted |
| `tests/unit/test_notice_rendering.py` | 21 | 0 | 0 | 0 | 21 | unstarted |
| `tests/unit/test_notify_push.py` | 31 | 0 | 0 | 0 | 31 | unstarted |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 | 33 | unstarted |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_operator_notices.py` | 79 | 79 | 0 | 0 | 0 | complete |
| `tests/unit/test_outcome_unknown.py` | 58 | 58 | 0 | 0 | 0 | complete |
| `tests/unit/test_person_boundary.py` | 97 | 97 | 0 | 0 | 0 | complete |
| `tests/unit/test_person_boundary_identity.py` | 25 | 0 | 0 | 0 | 25 | unstarted |
| `tests/unit/test_picker.py` | 39 | 0 | 0 | 0 | 39 | unstarted |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 | 26 | unstarted |
| `tests/unit/test_policy.py` | 178 | 178 | 0 | 0 | 0 | complete |
| `tests/unit/test_policy_disk.py` | 57 | 57 | 0 | 0 | 0 | complete |
| `tests/unit/test_policy_ramp.py` | 46 | 46 | 0 | 0 | 0 | complete |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 | 0 | 14 | unstarted |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 | 21 | unstarted |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_procs.py` | 48 | 48 | 0 | 0 | 0 | complete |
| `tests/unit/test_protocol_secrecy.py` | 322 | 322 | 0 | 0 | 0 | complete |
| `tests/unit/test_provider_active_cap.py` | 50 | 50 | 0 | 0 | 0 | complete |
| `tests/unit/test_prune_217.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_prune_decisions.py` | 48 | 48 | 0 | 0 | 0 | complete |
| `tests/unit/test_prune_restore.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 | 28 | unstarted |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/unit/test_relay.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/unit/test_render.py` | 20 | 0 | 0 | 0 | 20 | unstarted |
| `tests/unit/test_render_quota_projection.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_reset_credit_demand.py` | 68 | 68 | 0 | 0 | 0 | complete |
| `tests/unit/test_reset_credit_properties.py` | 11 | 0 | 0 | 0 | 11 | unstarted |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 52 | 0 | 0 | 0 | complete |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 | 23 | unstarted |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 | 12 | unstarted |
| `tests/unit/test_resource_identity.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 | 38 | unstarted |
| `tests/unit/test_retention_archive.py` | 109 | 109 | 0 | 0 | 0 | complete |
| `tests/unit/test_retention_archive_properties.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_retention_case_alias.py` | 56 | 56 | 0 | 0 | 0 | complete |
| `tests/unit/test_retention_nested_leases.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/unit/test_retention_ordinary_trees.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_retention_qos.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 | 0 | 37 | unstarted |
| `tests/unit/test_retention_rediscovery.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/unit/test_retention_regenerable.py` | 26 | 0 | 0 | 0 | 26 | unstarted |
| `tests/unit/test_retention_regenerable_properties.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_retention_shared_folders.py` | 62 | 36 | 0 | 0 | 26 | partial |
| `tests/unit/test_retention_sweep_race.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_retention_turn_contract.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_retention_turn_workdirs.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/unit/test_retention_turns.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 | 21 | unstarted |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 | 12 | unstarted |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_review_pr127_report.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/unit/test_review_pr127_requests.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/unit/test_review_pr127_service.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_review_r223_conv_probes.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/unit/test_review_r2_repro.py` | 31 | 0 | 0 | 0 | 31 | unstarted |
| `tests/unit/test_review_r3_measure.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 | 12 | unstarted |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/unit/test_review_r5_repro.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/unit/test_round10_refusal_context.py` | 59 | 59 | 0 | 0 | 0 | complete |
| `tests/unit/test_route_check.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/unit/test_salvage.py` | 31 | 0 | 0 | 0 | 31 | unstarted |
| `tests/unit/test_salvage_optional_transient.py` | 15 | 0 | 0 | 0 | 15 | unstarted |
| `tests/unit/test_salvage_unindexable.py` | 86 | 86 | 0 | 0 | 0 | complete |
| `tests/unit/test_scheduler.py` | 97 | 97 | 0 | 0 | 0 | complete |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_scheduler_split.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/unit/test_scheduler_tier_preferences.py` | 9 | 0 | 0 | 0 | 9 | unstarted |
| `tests/unit/test_scheduler_turns.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_scheduler_weekly.py` | 33 | 0 | 0 | 0 | 33 | unstarted |
| `tests/unit/test_scoped_stream_capacity.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/unit/test_scrub_key_order.py` | 70 | 70 | 0 | 0 | 0 | complete |
| `tests/unit/test_sessions_cli.py` | 142 | 142 | 0 | 0 | 0 | complete |
| `tests/unit/test_sessions_handoff.py` | 117 | 117 | 0 | 0 | 0 | complete |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 153 | 0 | 0 | 0 | complete |
| `tests/unit/test_sessions_headless_properties.py` | 5 | 0 | 0 | 0 | 5 | unstarted |
| `tests/unit/test_sessions_mirror.py` | 41 | 38 | 0 | 0 | 3 | partial |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_sessions_mirror_fifo.py` | 49 | 49 | 0 | 0 | 0 | complete |
| `tests/unit/test_sessions_mirror_hot_health.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 16 | 0 | 0 | 0 | 16 | unstarted |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_sessions_mirror_inventory.py` | 8 | 0 | 0 | 0 | 8 | unstarted |
| `tests/unit/test_sessions_mirror_load_gap.py` | 85 | 85 | 0 | 0 | 0 | complete |
| `tests/unit/test_sessions_mirror_safety.py` | 28 | 0 | 0 | 0 | 28 | unstarted |
| `tests/unit/test_sessions_mirror_write_churn.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/unit/test_sessions_nudge.py` | 44 | 44 | 0 | 0 | 0 | complete |
| `tests/unit/test_sessions_registry.py` | 24 | 0 | 0 | 0 | 24 | unstarted |
| `tests/unit/test_sessions_revive.py` | 45 | 45 | 0 | 0 | 0 | complete |
| `tests/unit/test_sessions_revive_reads.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_sessions_transcripts.py` | 71 | 71 | 0 | 0 | 0 | complete |
| `tests/unit/test_settle_properties.py` | 129 | 129 | 0 | 0 | 0 | complete |
| `tests/unit/test_soak_tools.py` | 49 | 49 | 0 | 0 | 0 | complete |
| `tests/unit/test_status_json.py` | 56 | 56 | 0 | 0 | 0 | complete |
| `tests/unit/test_steer_invariants.py` | 248 | 248 | 0 | 0 | 0 | complete |
| `tests/unit/test_stop_alarm.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_stop_watchdog.py` | 29 | 0 | 0 | 0 | 29 | unstarted |
| `tests/unit/test_stopped_daemon.py` | 72 | 72 | 0 | 0 | 0 | complete |
| `tests/unit/test_store_contention_queries.py` | 212 | 212 | 0 | 0 | 0 | complete |
| `tests/unit/test_store_ids.py` | 27 | 0 | 0 | 0 | 27 | unstarted |
| `tests/unit/test_store_migration_2.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_store_properties.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/unit/test_store_readers.py` | 26 | 0 | 0 | 0 | 26 | unstarted |
| `tests/unit/test_store_snapshot_plans.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_suggest_cli.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_suggest_hint.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_suggestion_error_boundaries.py` | 22 | 0 | 0 | 0 | 22 | unstarted |
| `tests/unit/test_suggestion_properties.py` | 1 | 0 | 0 | 0 | 1 | unstarted |
| `tests/unit/test_suggestion_secret_properties.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_suggestion_structural_errors.py` | 40 | 0 | 0 | 0 | 40 | unstarted |
| `tests/unit/test_suggestions.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/unit/test_suggestions_r3_channels.py` | 14 | 0 | 0 | 0 | 14 | unstarted |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 0 | 0 | 0 | 7 | unstarted |
| `tests/unit/test_suggestions_review_multiline.py` | 6 | 0 | 0 | 0 | 6 | unstarted |
| `tests/unit/test_suggestions_review_response.py` | 3 | 0 | 0 | 0 | 3 | unstarted |
| `tests/unit/test_test_module_names.py` | 1 | 1 | 0 | 0 | 0 | complete |
| `tests/unit/test_tick_prefilter.py` | 30 | 0 | 0 | 0 | 30 | unstarted |
| `tests/unit/test_timers_alerts.py` | 17 | 0 | 0 | 0 | 17 | unstarted |
| `tests/unit/test_timers_busy_read.py` | 115 | 115 | 0 | 0 | 0 | complete |
| `tests/unit/test_timers_daemon.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_timers_keepalive.py` | 14 | 0 | 0 | 0 | 14 | unstarted |
| `tests/unit/test_timers_probe.py` | 35 | 0 | 0 | 0 | 35 | unstarted |
| `tests/unit/test_timers_reset_credits.py` | 47 | 47 | 0 | 0 | 0 | complete |
| `tests/unit/test_timers_retention.py` | 20 | 0 | 0 | 0 | 20 | unstarted |
| `tests/unit/test_title_review_regressions.py` | 4 | 0 | 0 | 0 | 4 | unstarted |
| `tests/unit/test_turn_attachment_replay.py` | 13 | 0 | 0 | 0 | 13 | unstarted |
| `tests/unit/test_turn_overlap.py` | 10 | 0 | 0 | 0 | 10 | unstarted |
| `tests/unit/test_turn_replay.py` | 33 | 0 | 0 | 0 | 33 | unstarted |
| `tests/unit/test_turn_runner.py` | 77 | 77 | 0 | 0 | 0 | complete |
| `tests/unit/test_ultracode_effort.py` | 39 | 0 | 0 | 0 | 39 | unstarted |
| `tests/unit/test_unadmittable_pin.py` | 12 | 0 | 0 | 0 | 12 | unstarted |
| `tests/unit/test_wait_hub.py` | 44 | 33 | 0 | 0 | 11 | partial |
| `tests/unit/test_wake_cli.py` | 2 | 0 | 0 | 0 | 2 | unstarted |
| `tests/unit/test_weekly_rank_vs_base.py` | 26 | 0 | 0 | 0 | 26 | unstarted |

</details>


### Commits and completion notes

| Commit | Step |
|---|---|
| `09e8f0c0d` | Audit the inherited round-1 decisions. |
| `30aa280cb` | Merge the locally available release/217 at 4f5983f4c (#223; #232 and #238 already included). |
| `c53acab2f` | Keep both parents' complete contract histories; retain the earlier revised history entry too. |
| `db0216c85` | Tests first: PYTHONPATH entries, raw-reader mutant, production differential. |
| `6f0ce7538` | Guard doctor package probes; amend C-20.6 and its change log. |
| `862468fa2` | Tests first: relative cache bases and the old-base mutant. |
| `728d46d1f` | Judge cache overrides against their state root; amend C-20.6/C-2.1 history. |
| `d2b6cd52c` | Keep the existing live-root mutant on the normalized candidate; document relative_to. |

The final report/PR-body commit follows those commits. All commits use the assigned
branch in workspace-local `.git-local` metadata; the original shared Git metadata
remains read-only. No push, remote call, main/master update, or history rewrite
was performed.

All execution sessions this continuation started have returned, including the
interrupted H5 session, and its log stopped changing. The required final
`pgrep -fl 'suite_hold.sh|pytest|lockf'` check was attempted; this sandbox returned
`Cannot get process list`, so a host-wide process-list verification is unavailable.
Other jobs' processes were not signalled. Cleanup removes the required scratch
folder after writing and checking these deliverables; no logs, JSON evidence,
wheels, venv, bundles or Hypothesis database are staged. The initial discovery
deviations are recorded above.

The round's code, contract, resolver audit and requested PR body are complete.
Whole-suite validation remains partial as explicitly counted here.

## Review round 2: fix round 2 (2026-10-23)

This job started at 07:17:54 UTC on 2026-10-23 at `c45c2e0ec`, in a detached
worktree of the caller's checkout. Review r2 (APPROVE, four P3s) and the hub's
four decisions are its brief. It fetched nothing, pushed nothing, used no
sub-agents, and opened, listed or signalled nothing under `~/.subfleet`,
`~/.claude`, `~/.codex`, `~/chief-of-staff/state`, the live store or the live
daemon, apart from this job's own worktree, which Subfleet placed under
`~/.subfleet/worktrees`. The new tests' live roots are a stand-in account's,
in temporary folders. The existing tests name the account's own spellings
only under their spies, which refuse any call on them. The venv, each hold's
`TMPDIR`, Hypothesis's storage, the two archives and every log lived in the
job's scratch folder (`sf-r242f2`).
Commits went into the shared worktree metadata, which this sandbox could write.

### Commits

| Commit | Step |
|---|---|
| `4c9dd4128` | Merge `origin/release/217` at `c2e080e44` (#243, #247, #249, #245, #233) |
| `558a031e4` | Tests first: review r2's three inputs, the hard-link limit, four mutants |
| `639afae45` | Test: the hard-link limit test carries `documented_limitation` |
| `1cfafe83a` | Fix: `run_program` writes its refusal through `daemon_log` (the merge's lint conflict) |
| `7dced5704` | Fix (P3-2): any override under `/.vol/` is refused |
| `8d817789a` | Fix (P3-3): `""` is judged as `.` |
| `18b3f89c3` | Fix (P3-1): `subfleetd` runs `daemon.run_program` |
| `ff7ec9913` | Docs: C-20.6 amendments and four change-log entries |
| `e570bd37f` | Docs: C-20.6 says a HOME under `/.vol/` counts as one in a live root |
| (this commit) | Report and PR body |

### The merge

`4c9dd4128` merges the release ref the hub last fetched, `c2e080e44`: #243,
#247, #249, #245 and #233 since `4f5983f4c`. The only textual conflict was
`docs/acceptance-contract.md`, in two places:

- **The change log.** Both sides' new entries are kept, this branch's first,
  except one (decision 4, below).
- **C-20.6.** Release changed one phrase ("or one to the reader" became "or a
  symlink to the reader"); the merge keeps this branch's clause with release's
  phrase.

**Decision 4, the stale entry.** The C-16.3/C-17.3/C-17.7/C-15.1/C-9.2 entry
that says "An attempt the daemon signalled is never `ok`" (624 bytes, 625 with
its newline) exists at `473fe2941` and `43f42d83d`, the branch's earlier bases,
but not at `4f5983f4c` or `c2e080e44`, which hold release's D-WALL1 revision
(797 bytes, 798 with its newline). `c53acab2f` had restored it beside
release's. The merge drops it, so only release's revision stays. A check
script compared the merged contract with `c2e080e44` and with this branch's
own entries:

- **Change log:** 219 entries at the merge: release's 215 and round 1's 4
  (the entries `c45c2e0ec` has and `4f5983f4c` lacks, the stale one aside).
  This round's 4 make 223 at head, checked again there. Each release entry
  appears exactly as often as release has it. Release itself carries two entries
  twice (the CI-flakes D-2/D-RL1 entry and the C-20.6/C-23.28 PR #198 round 8
  entry); the merge keeps both copies, as release has them. The stale entry is
  gone.
- **Clauses:** all 257 clause IDs survive. C-20.6 is the only body that differs
  from release's, and every other line outside the change log is identical to
  release's.

**A conflict git could not see.** #233 added a lint
(`tests/unit/test_daemon_log_sites.py`): no daemon module may write its
standard streams except through `daemon_log`, and the old
`print(f"subfleet daemon: {exc}", file=sys.stderr)` line in `daemon.main` is
pinned as the failing example. This branch's `daemon.run_program` printed
exactly that line, so at the merge both lint tests failed (P2, below:
`subfleet/daemon.py:10332 writes print(…) past daemon_log`, and the same line `passes exc raw`; the pin found 3 old lines where it expects `main`'s 2). `1cfafe83a` writes the line through `daemon_log.write_scrubbed`, as
`main`'s own refusals are written. The words and exit 7 are unchanged, and
every refusal names a variable, never its value. The exception is bound as
`refused`, because the lint's pin requires `main`'s own
`write_scrubbed(f"subfleet daemon: {exc}")` line to appear exactly once.

### The findings: cause and fix

**P3-1, `subfleetd`.**
- **Cause:** `pyproject.toml` mapped the console script to
  `subfleet.daemon:main`, which raises a test process's refusal; only
  `python -m subfleet.daemon` went through `run_program`. `cli._daemond_argv`
  launches the console script whenever it finds one.
- **Fix (`18b3f89c3`):** the script is `subfleet.daemon:run_program`. The
  venv was synced again through the setup wrapper, and its generated
  `subfleetd` now imports `run_program`. In production the two entries differ
  only in that one exception, which production never raises.
- **Tests:** `test_the_installed_subfleetd_without_subfleet_home` runs the
  installed script in a fresh child with a temporary HOME and no SUBFLEET_HOME,
  on its own shebang and as `daemon start` runs it (this interpreter, `-E -P`).
  A child marked with `SUBFLEET_UNDER_PYTEST=1` or `PYTEST_CURRENT_TEST` exits
  7 with one line. A plain child exits 69 at the held `daemon.lock`, as `-m`
  does. The test first checks that the installed entry point is the one
  `pyproject.toml` declares, so a stale venv fails with its own message.
  In-process, `test_the_subfleetd_console_script_is_the_program_entry` calls
  the declared entry as its generated script does, expects exit 7 and one line,
  and checks that it is the function `-m`'s `__main__` block runs.

**P3-2, paths by inode.**
- **Cause:** the live-root check recognizes a store by its name: a store-named
  component whose parent is the account's home. macOS's volfs names a file as
  `/.vol/<device>/<inode>`, which carries no name, so the stand-in account's
  `.subfleet` spelled that way passed.
- **Fix (`7dced5704`):** in a test process any path under `/.vol` is refused,
  by its spelling, folded as `_fold` folds a path (case, slashes, dot segments,
  the `/System/Volumes/Data` firmlink), before any call on it. The link walk
  refuses before the call that would reach `/.vol`, so a link whose target is
  `/.vol/...` is refused too. A HOME under `/.vol` counts as one inside a live
  root, which only refuses more.
- **The hard-link limit:** a hard link is another name for a live file's
  inode, and the check judges names. C-20.6 now puts it out of scope, and
  `test_a_hard_link_to_a_live_file_is_judged_by_its_name_documented_limitation`
  pins the stated limit: a link to the stand-in's `.claude.json`, named in
  `SUBFLEET_DESKTOP_LOGIN`, is returned, and the file's own name is refused.
  It carries the suite's `documented_limitation` marker. It passes at
  `c45c2e0ec` and at head, as a pin of a limit should.
- **Tests:** `test_an_override_by_inode_is_refused`, 11 spellings by 2 routes
  (`SUBFLEET_HOME` through `home_path`, `CODEX_HOME` through `checked_override`).
  The spellings are review r2's input (the stand-in store by inode), a path
  inside it, review r2's control (the home by inode, then `.subfleet`), upper
  case, a doubled slash, the firmlink, `/private/..`, relative, an absolute and a
  relative link, and a missing volume. Each must be refused with no call on a
  `/.vol` path and none on the stand-in's stores. The test first checks that
  review r2's spelling really is the stand-in store.

**P3-3, the empty override.**
- **Cause:** `home_path` passed `""` to `refuse_personal_override`, but
  `_live_kind` returns None for an empty value, and `checked_override` skipped
  empty values altogether. `""` names the working directory for the
  `accept_empty` callers, and for `CODEX_HOME`, which preflight resolves.
- **Fix (`8d817789a`):** in a test process `refuse_personal_override` judges
  `""` as `.`, and `checked_override` judges an empty value too. A working
  directory elsewhere, this process's own checkout included, still gets `""`
  back. Outside a test process nothing changes: `checked_override` returns the
  value untouched and `home_path` does not call the refusal.
- **Tests:** `test_an_empty_override_inside_a_live_root_is_refused`, with the
  working directory inside the stand-in's `.subfleet` and inside
  `.codex/sessions`, by 2 routes: `home_path(..., accept_empty=True)`,
  `guard/preflight._state_root` and `daemon.main` (its `Daemon` replaced by a
  failure) for `SUBFLEET_HOME`, and a raw `CODEX_HOME` read, each for `""` and
  `.`. `test_an_empty_override_names_the_working_directory` now also runs from
  this process's own checkout (which sits under `~/.subfleet/worktrees` in a
  job's worktree) and covers the raw read. The production differential gains
  a 14th environment, every override empty with the working directory inside
  the stand-in store; all 238 comparisons (14 environments by 17 functions)
  agree with release.

### The regressions, at `c45c2e0ec` (P1)

P1 ran the new and changed tests on a `git archive` of `c45c2e0ec`, with
`639afae45`'s two test files copied in: 31 failed and 5 passed. Every new
regression failed:

| Test | At `c45c2e0ec` |
|---|---|
| `test_the_subfleetd_console_script_is_the_program_entry` | 1 failed: the declared entry, `main`, raised `PersonalPathRefused` |
| `test_the_installed_subfleetd_without_subfleet_home`, marked children | 4 failed: exit 1 with a traceback (plain children: 2 passed, 69) |
| `test_an_override_by_inode_is_refused` | 22 failed: 20 did not raise; review r2's control (2) was refused, but only after `lstat` of `/.vol` and the paths beneath it |
| `test_an_empty_override_inside_a_live_root_is_refused` | 4 failed: did not raise |
| `test_a_hard_link_..._documented_limitation` | passed (the stated limit) |
| `test_an_empty_override_names_the_working_directory` | passed |
| `test_production_resolves_as_release_217` (14 environments) | passed |

The archive's tests imported its own `subfleet`. The `daemon start` launch of
the installed script runs with `-E`, so it imported the editable install's
code from this worktree instead: `639afae45`, plus the then-uncommitted edits
that became `1cfafe83a` and `7dced5704`. Neither edit is on the path that
child takes. The venv's script still named `main` then, as at `c45c2e0ec`,
and `main` raises.

### Mutations

Each mutant fails its named test in a fresh child, and each unmutated control
passes (B1 and B2, `test_live_store_guard_st3_mutations.py`, 21 passed both
times: round 1's 17 and these 4):

| Mutant | Change | Named test |
|---|---|---|
| `subfleetd-console-main` | `pyproject.toml`'s script back to `subfleet.daemon:main` | `test_the_subfleetd_console_script_is_the_program_entry` |
| `volfs-accepted` | `_by_inode` returns False (`/.vol/` accepted) | `test_an_override_by_inode_is_refused` (review r2's input, both routes) |
| `empty-not-judged` | `refuse_personal_override` leaves `""` unjudged | `test_an_empty_override_inside_a_live_root_is_refused` (`home_path`) |
| `empty-raw-read-skipped` | `checked_override` skips an empty value again | `test_an_empty_override_inside_a_live_root_is_refused` (raw read) |

The mutation harness copies `pyproject.toml` into its shadow tree, so the
console-script mutant changes what the named test reads. `volfs-accepted`
skips off macOS.

### This round's runs

GIL CPython 3.14.7, pytest 9.1.1, Hypothesis 6.168.1, ci profile. The venv was
built once through the setup wrapper (`uv sync --locked --group dev`) and synced
once more after `pyproject.toml` changed. `pytest-xdist` 3.8.0 and `execnet`
2.1.2 are not in the dev group, and uv's cache index lacked them. They were
installed through the wrapper with `uv pip install --offline --no-deps`, from
wheels repacked out of uv's own unpacked cache (`archive-v0`), with no network.
Every pytest ran through `suite_hold.sh`, one at a time, as
`uv run --no-sync` (so no hold synced the venv), or as the venv's interpreter
in the two archives, where `uv run` would have synced the venv to the archive's
project. From B1 on, a launcher (scratch, not committed) gave each hold a soft
stop at 1,020 seconds (pytest's flag and xdist's own) and SIGALRM at 1,170. No
hold reached either. P1 ran pytest directly and took 16 seconds.

| Hold | Queued / acquired / ended (UTC) | Tree | What ran | Result |
|---|---|---|---|---|
| P1 | 07:37:58 / 08:12:26 / 08:12:44 | archive of `c45c2e0ec` with the new tests | the new and changed tests | 31 failed, 5 passed (above) |
| B1 | 08:16:52 / 08:30:27 / about 08:32:40 | `ff7ec9913` | the D-ST3 files, the PR's other changed test files, `home_paths`'s other test files; `-n 4` | 2,345 passed |
| B2 | 08:33:18 / 08:56:14 / about 08:57:30 | `ff7ec9913` | every `test_live_store_guard*.py`, `test_daemon_verbs.py`, `test_daemon_log_sites.py`, `test_stopped_daemon.py`; `-n 4` | 1,369 passed |
| P2 | 08:57:51 / 09:13:55 / 09:14:02 | archive of `4c9dd4128` (the merge) | release's two stream-lint tests | 2 failed (above) |
| B3 | 09:14:23 / 09:17:43 / 09:17:45 | `ff7ec9913` | `tests/fake/test_state_contract.py`, `tests/unit/test_test_module_names.py` | 38 passed |
| U1 | 09:18:44 / 09:18:44 / about 09:23:33 | `ff7ec9913` | the first 155 of the 313 `tests/unit` files B1–B3 had not run; `-n 4` | 5,708 passed, 1 skipped |
| U2 | 09:23:44 / 09:27:19 / about 09:39:49 | `ff7ec9913` | the other 158; `-n 4` | 5,239 passed, 4 skipped |
| F1 | 09:40:29 / 09:40:40 / about 09:47:45 | `ff7ec9913` | U2's 4 skipped cases again, with `UV_CACHE_DIR` set, and all of `tests/fake`; `-n 4` | 2,128 passed, 4 skipped, 2 xfailed |
| E1 | 09:48:57 / 09:56:29 / about 09:59:09 | `ff7ec9913` | all of `tests/e2e` (not briefed: it runs the installed `subfleetd`); `-n 4` | 93 passed, 16 skipped |
| C1 | 10:00:15 / 10:08:44 / 10:08:49 | `e570bd37f` | the 7 files that read the contract, after its last edit; `-n 4` | 323 passed |

There is no `tests/unit/test_home_paths*.py` or `test_daemon_entry*.py`. B1
therefore ran the files that import `home_paths` and every test file this PR
changes. B2 ran the daemon's entry tests where they are: the D-ST3 children
file, `test_daemon_verbs.py` (`daemon start`, `_daemond_argv`),
`test_daemon_log_sites.py` (release's lint, which reads `run_program`) and
`test_stopped_daemon.py`.

**B1, per file (all passed):**

| File | Passed |
|---|---:|
| `tests/fake/test_launch_env_daemon.py` | 49 |
| `tests/unit/test_compat.py` | 1,615 |
| `tests/unit/test_doctor_ca_bundle.py` | 16 |
| `tests/unit/test_gate_replay.py` | 23 |
| `tests/unit/test_gate_service.py` | 25 |
| `tests/unit/test_launch_env_allowlist.py` | 27 |
| `tests/unit/test_launch_env_mutations.py` | 41 |
| `tests/unit/test_launch_env_round2.py` | 67 |
| `tests/unit/test_launch_env_round3.py` | 69 |
| `tests/unit/test_launch_env_round4.py` | 40 |
| `tests/unit/test_launch_env_round5.py` | 39 |
| `tests/unit/test_live_store_guard_st3.py` | 299 |
| `tests/unit/test_live_store_guard_st3_children.py` | 14 |
| `tests/unit/test_live_store_guard_st3_mutations.py` | 21 |

**B2, per file (all passed):**

| File | Passed |
|---|---:|
| `tests/unit/test_daemon_log_sites.py` | 82 |
| `tests/unit/test_daemon_verbs.py` | 20 |
| `tests/unit/test_live_store_guard.py` | 324 |
| `tests/unit/test_live_store_guard_round10.py` | 66 |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 18 |
| `tests/unit/test_live_store_guard_round3.py` | 22 |
| `tests/unit/test_live_store_guard_round4.py` | 8 |
| `tests/unit/test_live_store_guard_round5.py` | 11 |
| `tests/unit/test_live_store_guard_round6.py` | 112 |
| `tests/unit/test_live_store_guard_round7.py` | 50 |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 |
| `tests/unit/test_live_store_guard_round8.py` | 100 |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 |
| `tests/unit/test_live_store_guard_round9.py` | 89 |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 |
| `tests/unit/test_live_store_guard_st3.py` | 299 |
| `tests/unit/test_live_store_guard_st3_children.py` | 14 |
| `tests/unit/test_live_store_guard_st3_mutations.py` | 21 |
| `tests/unit/test_live_store_guard_state_root.py` | 22 |
| `tests/unit/test_stopped_daemon.py` | 72 |

**B3 (all passed):** `tests/fake/test_state_contract.py` 37, `tests/unit/test_test_module_names.py` 1.

**C1 (all passed), after `e570bd37f` changed only the contract:** `tests/unit/test_live_store_guard_round8.py` 100, `round9` 89, `round10` 66, `tests/unit/test_invariants_index.py` 25, `tests/unit/test_retention_turn_contract.py` 2, `tests/unit/test_desktop_ledger.py` 4, `tests/fake/test_state_contract.py` 37.

### The whole of `tests/unit`, `tests/fake` and `tests/e2e`

Every file in the three directories has an outcome for every collected case:
U1, U2, F1 and E1 recorded their collected test ids, and each id has an
outcome. The holds ran at `ff7ec9913`, and C1 at `e570bd37f`, whose only
change is the contract. None failed:

| Directory | Files | Cases | Passed | Failed | Skipped | Xfailed | Unrun |
|---|---:|---:|---:|---:|---:|---:|---:|
| `tests/unit` | 344 | 14,284 | 14,279 | 0 | 5 | 0 | 0 |
| `tests/fake` | 112 | 2,130 | 2,128 | 0 | 0 | 2 | 0 |
| `tests/e2e` | 18 | 109 | 93 | 0 | 16 | 0 | 0 |

<details>
<summary>Every file, its cases and the holds that ran it (a case run twice counts once, by its last outcome)</summary>

| File | Cases | Passed | Skipped | Xfailed | Holds |
|---|---:|---:|---:|---:|---|
| `tests/unit/test_admission_uncapped.py` | 27 | 27 | 0 | 0 | U1 |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 7 | 0 | 0 | U1 |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 13 | 0 | 0 | U1 |
| `tests/unit/test_approval_commit.py` | 6 | 6 | 0 | 0 | U1 |
| `tests/unit/test_approval_resolution.py` | 33 | 33 | 0 | 0 | U1 |
| `tests/unit/test_boot_identity.py` | 10 | 10 | 0 | 0 | U1 |
| `tests/unit/test_capacity_desktop.py` | 20 | 20 | 0 | 0 | U1 |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 10 | 0 | 0 | U1 |
| `tests/unit/test_capacity_steer_route.py` | 22 | 22 | 0 | 0 | U1 |
| `tests/unit/test_capacity_view.py` | 20 | 20 | 0 | 0 | U1 |
| `tests/unit/test_census_counted.py` | 5 | 5 | 0 | 0 | U1 |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 8 | 0 | 0 | U1 |
| `tests/unit/test_census_procargs.py` | 52 | 52 | 0 | 0 | U1 |
| `tests/unit/test_census_recycled_pid.py` | 25 | 25 | 0 | 0 | U1 |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 20 | 0 | 0 | U1 |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 17 | 0 | 0 | U1 |
| `tests/unit/test_census_responsibility_races.py` | 34 | 34 | 0 | 0 | U1 |
| `tests/unit/test_census_survivors_review.py` | 14 | 14 | 0 | 0 | U1 |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 72 | 0 | 0 | U1 |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 48 | 0 | 0 | U1 |
| `tests/unit/test_claude_adapter.py` | 170 | 170 | 0 | 0 | U1 |
| `tests/unit/test_claude_attest.py` | 19 | 19 | 0 | 0 | U1 |
| `tests/unit/test_claude_cards.py` | 96 | 96 | 0 | 0 | U1 |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 15 | 0 | 0 | U1 |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 8 | 0 | 0 | U1 |
| `tests/unit/test_claude_identity.py` | 30 | 30 | 0 | 0 | U1 |
| `tests/unit/test_claude_large_stream.py` | 30 | 30 | 0 | 0 | U1 |
| `tests/unit/test_claude_mcp.py` | 32 | 32 | 0 | 0 | U1 |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 11 | 0 | 0 | U1 |
| `tests/unit/test_claude_stream.py` | 60 | 60 | 0 | 0 | U1 |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 63 | 0 | 0 | U1 |
| `tests/unit/test_claude_turn.py` | 69 | 69 | 0 | 0 | U1 |
| `tests/unit/test_claude_usage.py` | 28 | 28 | 0 | 0 | U1 |
| `tests/unit/test_claude_v1_parity.py` | 6 | 6 | 0 | 0 | U1 |
| `tests/unit/test_cli.py` | 248 | 248 | 0 | 0 | U1 |
| `tests/unit/test_cli_batch.py` | 18 | 18 | 0 | 0 | U1 |
| `tests/unit/test_cli_turns.py` | 23 | 23 | 0 | 0 | U1 |
| `tests/unit/test_client_availability.py` | 22 | 22 | 0 | 0 | U1 |
| `tests/unit/test_codex_adapter.py` | 172 | 172 | 0 | 0 | U1 |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 46 | 0 | 0 | U1 |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 6 | 0 | 0 | U1 |
| `tests/unit/test_codex_probe.py` | 55 | 55 | 0 | 0 | U1 |
| `tests/unit/test_codex_shim.py` | 31 | 31 | 0 | 0 | U1 |
| `tests/unit/test_codex_turn.py` | 67 | 67 | 0 | 0 | U1 |
| `tests/unit/test_codex_turn_completed.py` | 41 | 41 | 0 | 0 | U1 |
| `tests/unit/test_compat.py` | 1615 | 1615 | 0 | 0 | B1 |
| `tests/unit/test_continuation_r2.py` | 11 | 11 | 0 | 0 | U1 |
| `tests/unit/test_conversation_catalog.py` | 15 | 15 | 0 | 0 | U1 |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 21 | 0 | 0 | U1 |
| `tests/unit/test_conversation_connections.py` | 9 | 9 | 0 | 0 | U1 |
| `tests/unit/test_conversation_diff.py` | 39 | 39 | 0 | 0 | U1 |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 26 | 0 | 0 | U1 |
| `tests/unit/test_conversation_handoff.py` | 48 | 48 | 0 | 0 | U1 |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 28 | 0 | 0 | U1 |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 9 | 0 | 0 | U1 |
| `tests/unit/test_conversation_history.py` | 22 | 22 | 0 | 0 | U1 |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 44 | 0 | 0 | U1 |
| `tests/unit/test_conversation_reconcile.py` | 36 | 36 | 0 | 0 | U1 |
| `tests/unit/test_conversation_redact.py` | 9 | 9 | 0 | 0 | U1 |
| `tests/unit/test_conversation_service.py` | 100 | 100 | 0 | 0 | U1 |
| `tests/unit/test_conversation_state_reads.py` | 18 | 18 | 0 | 0 | U1 |
| `tests/unit/test_conversation_status.py` | 9 | 9 | 0 | 0 | U1 |
| `tests/unit/test_conversation_steer_service.py` | 45 | 45 | 0 | 0 | U1 |
| `tests/unit/test_conversation_store.py` | 32 | 32 | 0 | 0 | U1 |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 13 | 0 | 0 | U1 |
| `tests/unit/test_conversation_titles.py` | 89 | 89 | 0 | 0 | U1 |
| `tests/unit/test_conversation_waits.py` | 9 | 9 | 0 | 0 | U1 |
| `tests/unit/test_conversation_wakes.py` | 28 | 28 | 0 | 0 | U1 |
| `tests/unit/test_cpu_scaling.py` | 2 | 2 | 0 | 0 | U1 |
| `tests/unit/test_daemon_accept.py` | 5 | 5 | 0 | 0 | U1 |
| `tests/unit/test_daemon_connections.py` | 31 | 31 | 0 | 0 | U1 |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 27 | 0 | 0 | U1 |
| `tests/unit/test_daemon_init_release.py` | 2 | 2 | 0 | 0 | U1 |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 22 | 0 | 0 | U1 |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 4 | 0 | 0 | U1 |
| `tests/unit/test_daemon_integration.py` | 7 | 7 | 0 | 0 | U1 |
| `tests/unit/test_daemon_log_cut_windows.py` | 169 | 169 | 0 | 0 | U1 |
| `tests/unit/test_daemon_log_first_pass.py` | 25 | 25 | 0 | 0 | U1 |
| `tests/unit/test_daemon_log_handler.py` | 127 | 127 | 0 | 0 | U1 |
| `tests/unit/test_daemon_log_mutation_tool.py` | 4 | 4 | 0 | 0 | U1 |
| `tests/unit/test_daemon_log_reminders.py` | 101 | 101 | 0 | 0 | U1 |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 86 | 0 | 0 | U1 |
| `tests/unit/test_daemon_log_sites.py` | 82 | 82 | 0 | 0 | B2 |
| `tests/unit/test_daemon_log_tail.py` | 16 | 16 | 0 | 0 | U1 |
| `tests/unit/test_daemon_qos_tools.py` | 4 | 4 | 0 | 0 | U1 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 8 | 0 | 0 | U1 |
| `tests/unit/test_daemon_settle.py` | 61 | 61 | 0 | 0 | U1 |
| `tests/unit/test_daemon_verbs.py` | 20 | 20 | 0 | 0 | B2 |
| `tests/unit/test_decision_horizon.py` | 7 | 7 | 0 | 0 | U1 |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 56 | 0 | 0 | U1 |
| `tests/unit/test_descriptors.py` | 16 | 16 | 0 | 0 | U1 |
| `tests/unit/test_descriptors_desktop.py` | 8 | 8 | 0 | 0 | U1 |
| `tests/unit/test_desktop_account_cache.py` | 3 | 3 | 0 | 0 | U1 |
| `tests/unit/test_desktop_in_use.py` | 13 | 13 | 0 | 0 | U1 |
| `tests/unit/test_desktop_ledger.py` | 4 | 4 | 0 | 0 | C1 |
| `tests/unit/test_disk_admission.py` | 27 | 27 | 0 | 0 | U1 |
| `tests/unit/test_disk_floor.py` | 60 | 60 | 0 | 0 | U1 |
| `tests/unit/test_doctor.py` | 44 | 44 | 0 | 0 | U1 |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 16 | 0 | 0 | B1 |
| `tests/unit/test_doctor_identity.py` | 13 | 13 | 0 | 0 | U1 |
| `tests/unit/test_empty_provider_results.py` | 334 | 334 | 0 | 0 | U1 |
| `tests/unit/test_enrollment_runner.py` | 7 | 7 | 0 | 0 | U1 |
| `tests/unit/test_failed_keep.py` | 2 | 2 | 0 | 0 | U1 |
| `tests/unit/test_failed_keep_layout.py` | 4 | 4 | 0 | 0 | U1 |
| `tests/unit/test_failed_keep_prune.py` | 25 | 25 | 0 | 0 | U1 |
| `tests/unit/test_failed_keep_teardown.py` | 4 | 4 | 0 | 0 | U1 |
| `tests/unit/test_fake_codex_http.py` | 7 | 7 | 0 | 0 | U1 |
| `tests/unit/test_filesystem_identity_agreement.py` | 40 | 40 | 0 | 0 | U1 |
| `tests/unit/test_folders.py` | 23 | 23 | 0 | 0 | U1 |
| `tests/unit/test_forget_paced.py` | 2 | 2 | 0 | 0 | U1 |
| `tests/unit/test_fresh_until.py` | 1 | 1 | 0 | 0 | U1 |
| `tests/unit/test_frontend_probe_cache.py` | 4 | 4 | 0 | 0 | U1 |
| `tests/unit/test_frozen_provider_streams.py` | 217 | 217 | 0 | 0 | U1 |
| `tests/unit/test_gate_admission.py` | 24 | 24 | 0 | 0 | U1 |
| `tests/unit/test_gate_merge.py` | 59 | 59 | 0 | 0 | U1 |
| `tests/unit/test_gate_replay.py` | 23 | 23 | 0 | 0 | B1 |
| `tests/unit/test_gate_revision.py` | 16 | 16 | 0 | 0 | U1 |
| `tests/unit/test_gate_service.py` | 25 | 25 | 0 | 0 | B1 |
| `tests/unit/test_gate_verdict.py` | 45 | 45 | 0 | 0 | U1 |
| `tests/unit/test_guard_overlay.py` | 22 | 22 | 0 | 0 | U1 |
| `tests/unit/test_guard_trust.py` | 98 | 98 | 0 | 0 | U1 |
| `tests/unit/test_guardian.py` | 13 | 13 | 0 | 0 | U1 |
| `tests/unit/test_guardian_qos.py` | 14 | 14 | 0 | 0 | U1 |
| `tests/unit/test_handler_refusals.py` | 317 | 317 | 0 | 0 | U1 |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 6 | 0 | 0 | U1 |
| `tests/unit/test_hidden_environment.py` | 7 | 7 | 0 | 0 | U1 |
| `tests/unit/test_hook_cost.py` | 5 | 5 | 0 | 0 | U1 |
| `tests/unit/test_hooks.py` | 73 | 73 | 0 | 0 | U1 |
| `tests/unit/test_hooks_native_guard.py` | 18 | 18 | 0 | 0 | U1 |
| `tests/unit/test_host_push.py` | 118 | 118 | 0 | 0 | U1 |
| `tests/unit/test_host_push_admission.py` | 15 | 15 | 0 | 0 | U1 |
| `tests/unit/test_host_push_build.py` | 11 | 10 | 1 | 0 | U1 |
| `tests/unit/test_host_push_build_budget.py` | 15 | 15 | 0 | 0 | U1 |
| `tests/unit/test_host_push_cli.py` | 7 | 7 | 0 | 0 | U1 |
| `tests/unit/test_host_push_config_cost.py` | 7 | 7 | 0 | 0 | U1 |
| `tests/unit/test_host_push_copy.py` | 11 | 11 | 0 | 0 | U1 |
| `tests/unit/test_host_push_export.py` | 10 | 10 | 0 | 0 | U1 |
| `tests/unit/test_host_push_intake.py` | 13 | 13 | 0 | 0 | U1 |
| `tests/unit/test_host_push_metadata.py` | 33 | 33 | 0 | 0 | U1 |
| `tests/unit/test_host_push_mutations.py` | 24 | 24 | 0 | 0 | U1 |
| `tests/unit/test_host_push_names.py` | 13 | 13 | 0 | 0 | U1 |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 8 | 0 | 0 | U1 |
| `tests/unit/test_host_push_restart.py` | 4 | 4 | 0 | 0 | U1 |
| `tests/unit/test_host_push_retention.py` | 2 | 2 | 0 | 0 | U1 |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 2 | 0 | 0 | U1 |
| `tests/unit/test_host_push_storage_retry_budget.py` | 61 | 61 | 0 | 0 | U1 |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 2 | 0 | 0 | U1 |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 26 | 0 | 0 | U1 |
| `tests/unit/test_host_push_trees.py` | 5 | 5 | 0 | 0 | U1 |
| `tests/unit/test_host_push_validated_top.py` | 8 | 8 | 0 | 0 | U1 |
| `tests/unit/test_importer.py` | 148 | 148 | 0 | 0 | U1 |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 1 | 0 | 0 | U1 |
| `tests/unit/test_invariants_index.py` | 25 | 25 | 0 | 0 | C1 |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 3 | 0 | 0 | U1 |
| `tests/unit/test_lane_answers.py` | 27 | 27 | 0 | 0 | U1 |
| `tests/unit/test_lanes_transfer.py` | 79 | 79 | 0 | 0 | U1 |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 27 | 0 | 0 | B1 |
| `tests/unit/test_launch_env_helpers.py` | 20 | 20 | 0 | 0 | U1 |
| `tests/unit/test_launch_env_mutations.py` | 41 | 41 | 0 | 0 | B1 |
| `tests/unit/test_launch_env_policy.py` | 26 | 26 | 0 | 0 | U1 |
| `tests/unit/test_launch_env_round2.py` | 67 | 67 | 0 | 0 | B1 |
| `tests/unit/test_launch_env_round3.py` | 69 | 69 | 0 | 0 | B1 |
| `tests/unit/test_launch_env_round4.py` | 40 | 40 | 0 | 0 | B1 |
| `tests/unit/test_launch_env_round5.py` | 39 | 39 | 0 | 0 | B1 |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 6 | 0 | 0 | U1 |
| `tests/unit/test_legacy_hold.py` | 56 | 56 | 0 | 0 | U1 |
| `tests/unit/test_limit_from_model_text.py` | 95 | 95 | 0 | 0 | U1 |
| `tests/unit/test_live_store_guard.py` | 324 | 324 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round10.py` | 66 | 66 | 0 | 0 | C1 |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 18 | 18 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 22 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 8 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 11 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 112 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 50 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 23 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 100 | 0 | 0 | C1 |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 4 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 89 | 0 | 0 | C1 |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 12 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_st3.py` | 299 | 299 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_st3_children.py` | 14 | 14 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_st3_mutations.py` | 21 | 21 | 0 | 0 | B2 |
| `tests/unit/test_live_store_guard_state_root.py` | 22 | 22 | 0 | 0 | B2 |
| `tests/unit/test_lockwatch.py` | 20 | 20 | 0 | 0 | U2 |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 6 | 0 | 0 | U2 |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 5 | 0 | 0 | U2 |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 29 | 0 | 0 | U2 |
| `tests/unit/test_mirror_flags_model.py` | 12 | 12 | 0 | 0 | U2 |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 2 | 0 | 0 | U2 |
| `tests/unit/test_native_operations.py` | 28 | 28 | 0 | 0 | U2 |
| `tests/unit/test_notice_rendering.py` | 21 | 21 | 0 | 0 | U2 |
| `tests/unit/test_notify_push.py` | 31 | 31 | 0 | 0 | U2 |
| `tests/unit/test_offline.py` | 33 | 33 | 0 | 0 | U2 |
| `tests/unit/test_operator_hold_closures.py` | 15 | 15 | 0 | 0 | U2 |
| `tests/unit/test_operator_notices.py` | 79 | 79 | 0 | 0 | U2 |
| `tests/unit/test_outcome_unknown.py` | 58 | 58 | 0 | 0 | U2 |
| `tests/unit/test_person_boundary.py` | 97 | 97 | 0 | 0 | U2 |
| `tests/unit/test_person_boundary_identity.py` | 25 | 25 | 0 | 0 | U2 |
| `tests/unit/test_picker.py` | 39 | 39 | 0 | 0 | U2 |
| `tests/unit/test_placement_ramp.py` | 26 | 26 | 0 | 0 | U2 |
| `tests/unit/test_policy.py` | 178 | 178 | 0 | 0 | U2 |
| `tests/unit/test_policy_disk.py` | 57 | 57 | 0 | 0 | U2 |
| `tests/unit/test_policy_ramp.py` | 46 | 46 | 0 | 0 | U2 |
| `tests/unit/test_policy_support.py` | 14 | 14 | 0 | 0 | U2 |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 15 | 0 | 0 | U2 |
| `tests/unit/test_pr124_fixes.py` | 10 | 10 | 0 | 0 | U2 |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 6 | 0 | 0 | U2 |
| `tests/unit/test_pr192_review_r1.py` | 21 | 21 | 0 | 0 | U2 |
| `tests/unit/test_pr192_review_r2.py` | 9 | 9 | 0 | 0 | U2 |
| `tests/unit/test_procs.py` | 48 | 48 | 0 | 0 | U2 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 322 | 0 | 0 | U2 |
| `tests/unit/test_provider_active_cap.py` | 50 | 50 | 0 | 0 | U2 |
| `tests/unit/test_prune_217.py` | 9 | 9 | 0 | 0 | U2 |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 23 | 0 | 0 | U2 |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 15 | 0 | 0 | U2 |
| `tests/unit/test_prune_decisions.py` | 48 | 48 | 0 | 0 | U2 |
| `tests/unit/test_prune_restore.py` | 4 | 4 | 0 | 0 | U2 |
| `tests/unit/test_quota_projection.py` | 28 | 28 | 0 | 0 | U2 |
| `tests/unit/test_rank_review_report.py` | 3 | 3 | 0 | 0 | U2 |
| `tests/unit/test_reap_liveness.py` | 7 | 7 | 0 | 0 | U2 |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 17 | 0 | 0 | U2 |
| `tests/unit/test_relay.py` | 30 | 30 | 0 | 0 | U2 |
| `tests/unit/test_render.py` | 20 | 20 | 0 | 0 | U2 |
| `tests/unit/test_render_quota_projection.py` | 23 | 23 | 0 | 0 | U2 |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 5 | 0 | 0 | U2 |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 2 | 0 | 0 | U2 |
| `tests/unit/test_reset_credit_demand.py` | 68 | 68 | 0 | 0 | U2 |
| `tests/unit/test_reset_credit_properties.py` | 11 | 11 | 0 | 0 | U2 |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 22 | 0 | 0 | U2 |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 52 | 0 | 0 | U2 |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 23 | 0 | 0 | U2 |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 12 | 0 | 0 | U2 |
| `tests/unit/test_resource_identity.py` | 10 | 10 | 0 | 0 | U2 |
| `tests/unit/test_retention_81_notes.py` | 38 | 38 | 0 | 0 | U2 |
| `tests/unit/test_retention_archive.py` | 109 | 109 | 0 | 0 | U2 |
| `tests/unit/test_retention_archive_properties.py` | 2 | 2 | 0 | 0 | U2 |
| `tests/unit/test_retention_case_alias.py` | 56 | 56 | 0 | 0 | U2 |
| `tests/unit/test_retention_nested_leases.py` | 27 | 27 | 0 | 0 | U2 |
| `tests/unit/test_retention_ordinary_trees.py` | 4 | 0 | 4 | 0 | F1 |
| `tests/unit/test_retention_qos.py` | 8 | 8 | 0 | 0 | U2 |
| `tests/unit/test_retention_readback.py` | 37 | 37 | 0 | 0 | U2 |
| `tests/unit/test_retention_rediscovery.py` | 17 | 17 | 0 | 0 | U2 |
| `tests/unit/test_retention_regenerable.py` | 26 | 26 | 0 | 0 | U2 |
| `tests/unit/test_retention_regenerable_properties.py` | 1 | 1 | 0 | 0 | U2 |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 2 | 0 | 0 | U2 |
| `tests/unit/test_retention_shared_folders.py` | 62 | 62 | 0 | 0 | U2 |
| `tests/unit/test_retention_sweep_race.py` | 13 | 13 | 0 | 0 | U2 |
| `tests/unit/test_retention_turn_contract.py` | 2 | 2 | 0 | 0 | C1 |
| `tests/unit/test_retention_turn_workdirs.py` | 1 | 1 | 0 | 0 | U2 |
| `tests/unit/test_retention_turns.py` | 7 | 7 | 0 | 0 | U2 |
| `tests/unit/test_retention_worktrees.py` | 15 | 15 | 0 | 0 | U2 |
| `tests/unit/test_review130_pass.py` | 21 | 21 | 0 | 0 | U2 |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 12 | 0 | 0 | U2 |
| `tests/unit/test_review_pr127_history.py` | 6 | 6 | 0 | 0 | U2 |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 7 | 0 | 0 | U2 |
| `tests/unit/test_review_pr127_report.py` | 1 | 1 | 0 | 0 | U2 |
| `tests/unit/test_review_pr127_requests.py` | 17 | 17 | 0 | 0 | U2 |
| `tests/unit/test_review_pr127_service.py` | 8 | 8 | 0 | 0 | U2 |
| `tests/unit/test_review_r223_conv_probes.py` | 17 | 17 | 0 | 0 | U2 |
| `tests/unit/test_review_r2_repro.py` | 31 | 31 | 0 | 0 | U2 |
| `tests/unit/test_review_r3_measure.py` | 6 | 6 | 0 | 0 | U2 |
| `tests/unit/test_review_r3_repro.py` | 12 | 12 | 0 | 0 | U2 |
| `tests/unit/test_review_r4_repro.py` | 27 | 27 | 0 | 0 | U2 |
| `tests/unit/test_review_r5_repro.py` | 17 | 17 | 0 | 0 | U2 |
| `tests/unit/test_review_r6_probes.py` | 30 | 30 | 0 | 0 | U2 |
| `tests/unit/test_review_r6_restart.py` | 5 | 5 | 0 | 0 | U2 |
| `tests/unit/test_round10_refusal_context.py` | 59 | 59 | 0 | 0 | U2 |
| `tests/unit/test_route_check.py` | 24 | 24 | 0 | 0 | U2 |
| `tests/unit/test_salvage.py` | 31 | 31 | 0 | 0 | U2 |
| `tests/unit/test_salvage_optional_transient.py` | 15 | 15 | 0 | 0 | U2 |
| `tests/unit/test_salvage_ref_race.py` | 9 | 9 | 0 | 0 | U2 |
| `tests/unit/test_salvage_unindexable.py` | 86 | 86 | 0 | 0 | U2 |
| `tests/unit/test_scheduler.py` | 97 | 97 | 0 | 0 | U2 |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 9 | 0 | 0 | U2 |
| `tests/unit/test_scheduler_split.py` | 3 | 3 | 0 | 0 | U2 |
| `tests/unit/test_scheduler_tier_preferences.py` | 9 | 9 | 0 | 0 | U2 |
| `tests/unit/test_scheduler_turns.py` | 10 | 10 | 0 | 0 | U2 |
| `tests/unit/test_scheduler_weekly.py` | 33 | 33 | 0 | 0 | U2 |
| `tests/unit/test_scoped_stream_capacity.py` | 17 | 17 | 0 | 0 | U2 |
| `tests/unit/test_scrub_key_order.py` | 70 | 70 | 0 | 0 | U2 |
| `tests/unit/test_sessions_cli.py` | 142 | 142 | 0 | 0 | U2 |
| `tests/unit/test_sessions_handoff.py` | 117 | 117 | 0 | 0 | U2 |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 153 | 0 | 0 | U2 |
| `tests/unit/test_sessions_headless_properties.py` | 5 | 5 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror.py` | 41 | 41 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 4 | 4 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_fifo.py` | 49 | 49 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_hot_health.py` | 6 | 6 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 16 | 16 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 22 | 22 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_inventory.py` | 8 | 8 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_load_gap.py` | 85 | 85 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_safety.py` | 28 | 28 | 0 | 0 | U2 |
| `tests/unit/test_sessions_mirror_write_churn.py` | 3 | 3 | 0 | 0 | U2 |
| `tests/unit/test_sessions_nudge.py` | 44 | 44 | 0 | 0 | U2 |
| `tests/unit/test_sessions_registry.py` | 24 | 24 | 0 | 0 | U2 |
| `tests/unit/test_sessions_revive.py` | 45 | 45 | 0 | 0 | U2 |
| `tests/unit/test_sessions_revive_reads.py` | 10 | 10 | 0 | 0 | U2 |
| `tests/unit/test_sessions_transcripts.py` | 71 | 71 | 0 | 0 | U2 |
| `tests/unit/test_settle_properties.py` | 129 | 129 | 0 | 0 | U2 |
| `tests/unit/test_soak_tools.py` | 49 | 49 | 0 | 0 | U2 |
| `tests/unit/test_status_json.py` | 56 | 56 | 0 | 0 | U2 |
| `tests/unit/test_steer_invariants.py` | 248 | 248 | 0 | 0 | U2 |
| `tests/unit/test_stop_alarm.py` | 2 | 2 | 0 | 0 | U2 |
| `tests/unit/test_stop_watchdog.py` | 29 | 29 | 0 | 0 | U2 |
| `tests/unit/test_stopped_daemon.py` | 72 | 72 | 0 | 0 | B2 |
| `tests/unit/test_store_contention_queries.py` | 212 | 212 | 0 | 0 | U2 |
| `tests/unit/test_store_ids.py` | 27 | 27 | 0 | 0 | U2 |
| `tests/unit/test_store_migration_2.py` | 10 | 10 | 0 | 0 | U2 |
| `tests/unit/test_store_properties.py` | 3 | 3 | 0 | 0 | U2 |
| `tests/unit/test_store_readers.py` | 26 | 26 | 0 | 0 | U2 |
| `tests/unit/test_store_snapshot_plans.py` | 4 | 4 | 0 | 0 | U2 |
| `tests/unit/test_suggest_cli.py` | 7 | 7 | 0 | 0 | U2 |
| `tests/unit/test_suggest_hint.py` | 10 | 10 | 0 | 0 | U2 |
| `tests/unit/test_suggestion_error_boundaries.py` | 22 | 22 | 0 | 0 | U2 |
| `tests/unit/test_suggestion_properties.py` | 1 | 1 | 0 | 0 | U2 |
| `tests/unit/test_suggestion_secret_properties.py` | 7 | 7 | 0 | 0 | U2 |
| `tests/unit/test_suggestion_structural_errors.py` | 40 | 40 | 0 | 0 | U2 |
| `tests/unit/test_suggestions.py` | 30 | 30 | 0 | 0 | U2 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 30 | 30 | 0 | 0 | U2 |
| `tests/unit/test_suggestions_r3_channels.py` | 14 | 14 | 0 | 0 | U2 |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 7 | 0 | 0 | U2 |
| `tests/unit/test_suggestions_review_multiline.py` | 6 | 6 | 0 | 0 | U2 |
| `tests/unit/test_suggestions_review_response.py` | 3 | 3 | 0 | 0 | U2 |
| `tests/unit/test_test_module_names.py` | 1 | 1 | 0 | 0 | B3 |
| `tests/unit/test_tick_prefilter.py` | 30 | 30 | 0 | 0 | U2 |
| `tests/unit/test_timers_alerts.py` | 17 | 17 | 0 | 0 | U2 |
| `tests/unit/test_timers_busy_read.py` | 115 | 115 | 0 | 0 | U2 |
| `tests/unit/test_timers_daemon.py` | 4 | 4 | 0 | 0 | U2 |
| `tests/unit/test_timers_keepalive.py` | 14 | 14 | 0 | 0 | U2 |
| `tests/unit/test_timers_probe.py` | 35 | 35 | 0 | 0 | U2 |
| `tests/unit/test_timers_reset_credits.py` | 47 | 47 | 0 | 0 | U2 |
| `tests/unit/test_timers_retention.py` | 20 | 20 | 0 | 0 | U2 |
| `tests/unit/test_title_review_regressions.py` | 4 | 4 | 0 | 0 | U2 |
| `tests/unit/test_turn_attachment_replay.py` | 13 | 13 | 0 | 0 | U2 |
| `tests/unit/test_turn_overlap.py` | 10 | 10 | 0 | 0 | U2 |
| `tests/unit/test_turn_replay.py` | 33 | 33 | 0 | 0 | U2 |
| `tests/unit/test_turn_runner.py` | 83 | 83 | 0 | 0 | U2 |
| `tests/unit/test_ultracode_effort.py` | 39 | 39 | 0 | 0 | U2 |
| `tests/unit/test_unadmittable_pin.py` | 12 | 12 | 0 | 0 | U2 |
| `tests/unit/test_wait_hub.py` | 44 | 44 | 0 | 0 | U2 |
| `tests/unit/test_wake_cli.py` | 2 | 2 | 0 | 0 | U2 |
| `tests/unit/test_weekly_rank_vs_base.py` | 26 | 26 | 0 | 0 | U2 |
| `tests/fake/test_admission_disk.py` | 27 | 27 | 0 | 0 | F1 |
| `tests/fake/test_admission_held_waiters.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_admission_latency.py` | 14 | 14 | 0 | 0 | F1 |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 16 | 0 | 0 | F1 |
| `tests/fake/test_admission_liveness.py` | 32 | 32 | 0 | 0 | F1 |
| `tests/fake/test_admission_priority.py` | 32 | 32 | 0 | 0 | F1 |
| `tests/fake/test_admission_properties.py` | 28 | 27 | 0 | 1 | F1 |
| `tests/fake/test_admission_ramp.py` | 19 | 19 | 0 | 0 | F1 |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 11 | 0 | 0 | F1 |
| `tests/fake/test_admission_route_isolation.py` | 93 | 93 | 0 | 0 | F1 |
| `tests/fake/test_admission_stateful.py` | 17 | 17 | 0 | 0 | F1 |
| `tests/fake/test_admission_visibility.py` | 43 | 43 | 0 | 0 | F1 |
| `tests/fake/test_batch_label.py` | 9 | 9 | 0 | 0 | F1 |
| `tests/fake/test_canonical_identity.py` | 24 | 24 | 0 | 0 | F1 |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 21 | 0 | 0 | F1 |
| `tests/fake/test_canonical_identity_fix3.py` | 18 | 17 | 0 | 1 | F1 |
| `tests/fake/test_canonical_identity_fix4.py` | 11 | 11 | 0 | 0 | F1 |
| `tests/fake/test_canonical_identity_fix5.py` | 14 | 14 | 0 | 0 | F1 |
| `tests/fake/test_capacity_steer_daemon.py` | 72 | 72 | 0 | 0 | F1 |
| `tests/fake/test_capacity_wait_backoff.py` | 30 | 30 | 0 | 0 | F1 |
| `tests/fake/test_census_escapers_daemon.py` | 9 | 9 | 0 | 0 | F1 |
| `tests/fake/test_census_recycled_pid_daemon.py` | 29 | 29 | 0 | 0 | F1 |
| `tests/fake/test_codex_ca_bundle_fake.py` | 6 | 6 | 0 | 0 | F1 |
| `tests/fake/test_codex_turn_completed_fake.py` | 109 | 109 | 0 | 0 | F1 |
| `tests/fake/test_conversation_pools.py` | 2 | 2 | 0 | 0 | F1 |
| `tests/fake/test_conversation_wakes_fake.py` | 4 | 4 | 0 | 0 | F1 |
| `tests/fake/test_crash_matrix.py` | 9 | 9 | 0 | 0 | F1 |
| `tests/fake/test_daemon_contract.py` | 21 | 21 | 0 | 0 | F1 |
| `tests/fake/test_daemon_descriptors.py` | 13 | 13 | 0 | 0 | F1 |
| `tests/fake/test_daemon_launch.py` | 9 | 9 | 0 | 0 | F1 |
| `tests/fake/test_daemon_stacks.py` | 6 | 6 | 0 | 0 | F1 |
| `tests/fake/test_descriptor_exhaustion.py` | 2 | 2 | 0 | 0 | F1 |
| `tests/fake/test_detached_cwd_pins.py` | 18 | 18 | 0 | 0 | F1 |
| `tests/fake/test_detached_retention_fence.py` | 89 | 89 | 0 | 0 | F1 |
| `tests/fake/test_enrollment_guardian.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_fake_provider.py` | 13 | 13 | 0 | 0 | F1 |
| `tests/fake/test_finalization_replay.py` | 10 | 10 | 0 | 0 | F1 |
| `tests/fake/test_folder_spelling.py` | 7 | 7 | 0 | 0 | F1 |
| `tests/fake/test_frozen_stream_nonregular.py` | 140 | 140 | 0 | 0 | F1 |
| `tests/fake/test_gate_end_to_end.py` | 11 | 11 | 0 | 0 | F1 |
| `tests/fake/test_identity_rollback.py` | 5 | 5 | 0 | 0 | F1 |
| `tests/fake/test_idle_cost.py` | 2 | 2 | 0 | 0 | F1 |
| `tests/fake/test_interactive_claude_titles.py` | 6 | 6 | 0 | 0 | F1 |
| `tests/fake/test_interactive_steer.py` | 13 | 13 | 0 | 0 | F1 |
| `tests/fake/test_lane_fault.py` | 20 | 20 | 0 | 0 | F1 |
| `tests/fake/test_lane_pilot.py` | 24 | 24 | 0 | 0 | F1 |
| `tests/fake/test_lanes_enroll.py` | 15 | 15 | 0 | 0 | F1 |
| `tests/fake/test_lanes_reenroll.py` | 19 | 19 | 0 | 0 | F1 |
| `tests/fake/test_lanes_reenroll_keychain.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_launch_env_daemon.py` | 49 | 49 | 0 | 0 | F1 |
| `tests/fake/test_launch_env_providers.py` | 6 | 6 | 0 | 0 | F1 |
| `tests/fake/test_launch_gates_above_stdio.py` | 9 | 9 | 0 | 0 | F1 |
| `tests/fake/test_lost_acknowledgement.py` | 3 | 3 | 0 | 0 | F1 |
| `tests/fake/test_main_refusal_write_dir.py` | 5 | 5 | 0 | 0 | F1 |
| `tests/fake/test_mcp_job_contract.py` | 12 | 12 | 0 | 0 | F1 |
| `tests/fake/test_native_maintenance_startup.py` | 41 | 41 | 0 | 0 | F1 |
| `tests/fake/test_notice_delivery.py` | 15 | 15 | 0 | 0 | F1 |
| `tests/fake/test_notice_header.py` | 23 | 23 | 0 | 0 | F1 |
| `tests/fake/test_picker_native.py` | 3 | 3 | 0 | 0 | F1 |
| `tests/fake/test_policy_sandbox.py` | 25 | 25 | 0 | 0 | F1 |
| `tests/fake/test_probe_cancel.py` | 24 | 24 | 0 | 0 | F1 |
| `tests/fake/test_probe_recovery.py` | 9 | 9 | 0 | 0 | F1 |
| `tests/fake/test_probe_turn.py` | 43 | 43 | 0 | 0 | F1 |
| `tests/fake/test_prompt_seam.py` | 8 | 8 | 0 | 0 | F1 |
| `tests/fake/test_provider_active_cap_fake.py` | 60 | 60 | 0 | 0 | F1 |
| `tests/fake/test_provider_cap_review_evidence.py` | 5 | 5 | 0 | 0 | F1 |
| `tests/fake/test_provider_qos.py` | 2 | 2 | 0 | 0 | F1 |
| `tests/fake/test_record_secrecy.py` | 44 | 44 | 0 | 0 | F1 |
| `tests/fake/test_request_routing.py` | 3 | 3 | 0 | 0 | F1 |
| `tests/fake/test_reserve_authorization.py` | 12 | 12 | 0 | 0 | F1 |
| `tests/fake/test_reserve_authorization_validation.py` | 11 | 11 | 0 | 0 | F1 |
| `tests/fake/test_reset_credit_incident.py` | 19 | 19 | 0 | 0 | F1 |
| `tests/fake/test_resume_cli.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_resume_contract.py` | 23 | 23 | 0 | 0 | F1 |
| `tests/fake/test_retention_dormant.py` | 7 | 7 | 0 | 0 | F1 |
| `tests/fake/test_retry_for_good_refusals.py` | 8 | 8 | 0 | 0 | F1 |
| `tests/fake/test_review130_ab.py` | 12 | 12 | 0 | 0 | F1 |
| `tests/fake/test_review_r185_adversarial.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_review_r223_wall_probes.py` | 23 | 23 | 0 | 0 | F1 |
| `tests/fake/test_route_view.py` | 8 | 8 | 0 | 0 | F1 |
| `tests/fake/test_routing_end_to_end.py` | 16 | 16 | 0 | 0 | F1 |
| `tests/fake/test_routing_tier_preferences.py` | 5 | 5 | 0 | 0 | F1 |
| `tests/fake/test_salvage_adversarial.py` | 8 | 8 | 0 | 0 | F1 |
| `tests/fake/test_salvage_finalization.py` | 47 | 47 | 0 | 0 | F1 |
| `tests/fake/test_sessions_end_to_end.py` | 33 | 33 | 0 | 0 | F1 |
| `tests/fake/test_state_contract.py` | 37 | 37 | 0 | 0 | C1 |
| `tests/fake/test_state_fixture_processes.py` | 4 | 4 | 0 | 0 | F1 |
| `tests/fake/test_status_payload.py` | 2 | 2 | 0 | 0 | F1 |
| `tests/fake/test_suggestions_fake.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_tier_hold_by_demand.py` | 27 | 27 | 0 | 0 | F1 |
| `tests/fake/test_timers_busy_admission.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_timers_end_to_end.py` | 7 | 7 | 0 | 0 | F1 |
| `tests/fake/test_turn_listing.py` | 9 | 9 | 0 | 0 | F1 |
| `tests/fake/test_turn_retention_cwd.py` | 6 | 6 | 0 | 0 | F1 |
| `tests/fake/test_turn_retention_cwd_properties.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_turn_retention_job_directory.py` | 2 | 2 | 0 | 0 | F1 |
| `tests/fake/test_turn_retention_job_properties.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_turn_retention_quarantine.py` | 10 | 10 | 0 | 0 | F1 |
| `tests/fake/test_turn_trees.py` | 18 | 18 | 0 | 0 | F1 |
| `tests/fake/test_turn_wait_reasons.py` | 7 | 7 | 0 | 0 | F1 |
| `tests/fake/test_unadmittable_pin_admission.py` | 24 | 24 | 0 | 0 | F1 |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 | 6 | 0 | 0 | F1 |
| `tests/fake/test_unadmittable_pin_review.py` | 7 | 7 | 0 | 0 | F1 |
| `tests/fake/test_wall_cancel_races.py` | 30 | 30 | 0 | 0 | F1 |
| `tests/fake/test_wall_inspection_order.py` | 18 | 18 | 0 | 0 | F1 |
| `tests/fake/test_wall_running_time.py` | 35 | 35 | 0 | 0 | F1 |
| `tests/fake/test_wall_stop_deliveries.py` | 25 | 25 | 0 | 0 | F1 |
| `tests/fake/test_workspace_contract.py` | 8 | 8 | 0 | 0 | F1 |
| `tests/fake/test_workspace_enospc.py` | 1 | 1 | 0 | 0 | F1 |
| `tests/fake/test_workspace_transient.py` | 24 | 24 | 0 | 0 | F1 |
| `tests/fake/test_writable_fanout.py` | 28 | 28 | 0 | 0 | F1 |
| `tests/fake/test_writer_retention_fence.py` | 23 | 23 | 0 | 0 | F1 |
| `tests/e2e/test_capacity_steer_e2e.py` | 2 | 2 | 0 | 0 | E1 |
| `tests/e2e/test_codex_turn_completed_e2e.py` | 3 | 3 | 0 | 0 | E1 |
| `tests/e2e/test_conversation_titles_e2e.py` | 9 | 9 | 0 | 0 | E1 |
| `tests/e2e/test_conversation_wakes_e2e.py` | 4 | 4 | 0 | 0 | E1 |
| `tests/e2e/test_conversations.py` | 44 | 29 | 15 | 0 | E1 |
| `tests/e2e/test_guard_and_isolation.py` | 5 | 5 | 0 | 0 | E1 |
| `tests/e2e/test_http_isolation.py` | 1 | 1 | 0 | 0 | E1 |
| `tests/e2e/test_legacy_cockpit.py` | 1 | 1 | 0 | 0 | E1 |
| `tests/e2e/test_milestone1_codex.py` | 9 | 9 | 0 | 0 | E1 |
| `tests/e2e/test_milestone2_claude.py` | 7 | 7 | 0 | 0 | E1 |
| `tests/e2e/test_notice_job_state.py` | 1 | 1 | 0 | 0 | E1 |
| `tests/e2e/test_recovery.py` | 2 | 2 | 0 | 0 | E1 |
| `tests/e2e/test_reserve.py` | 4 | 4 | 0 | 0 | E1 |
| `tests/e2e/test_review_r2_strand_e2e.py` | 2 | 2 | 0 | 0 | E1 |
| `tests/e2e/test_review_r3_strand_timing_e2e.py` | 2 | 2 | 0 | 0 | E1 |
| `tests/e2e/test_stop_bound.py` | 3 | 3 | 0 | 0 | E1 |
| `tests/e2e/test_suggestions_e2e.py` | 2 | 1 | 1 | 0 | E1 |
| `tests/e2e/test_suggestions_r1b_peer_e2e.py` | 8 | 8 | 0 | 0 | E1 |

</details>

**The 21 cases that did not pass, each skipped or expected to fail on its own terms:**

- **4 in `tests/unit/test_retention_ordinary_trees.py`** (`test_an_ordinary_tree_retires_without_an_unarchived_path`,
  all four link modes). The test builds a venv with `uv pip install --offline
  sortedcontainers==2.4.0` and skips when uv cannot. It skipped in U2, under
  the session's isolated HOME with no `UV_CACHE_DIR` to carry. Rerun in F1 with
  `UV_CACHE_DIR` set to uv's default cache, it skipped again. This uv reads
  its index from `simple-v25`, and the cache holds sortedcontainers' entry
  only in the older `simple-v21`. The brief forbids the network that would
  fill it, so these 4 cases remain unrun.
- **1 in `tests/unit/test_host_push_build.py`**: an opt-in benchmark
  (`SF_HOST_PUSH_BENCHMARK=1`), skipped by design.
- **2 xfailed in `tests/fake`**: the documented C-6.10 wait-kind case and the P3-6
  hung-mount case, both pre-existing.
- **16 in `tests/e2e/test_conversations.py`**: person-only requests and marker
  refusals that cannot run inside a Subfleet job (CI runs them).

Not run, and not briefed: `tests/frontend` (41 files), `tests/process` (9) and
`tests/live` (2, which this job would not run in any case).

### Status

All four decisions are done, and so is the merge's lint conflict. Every
briefed run finished (B1, B2, B3), as did all of `tests/unit` (with `tests/fake`
and `tests/e2e`): no failures. The 4 retention cases above are the only ones
unrun for want of something this job may not fetch.

The hold rules held: one pytest per hold, each through `suite_hold.sh`, at
most 4 workers, and the longest hold ran 12.5 minutes. P1 and P2 ran in
archives under scratch. Before this report, `pgrep -fl 'suite_hold.sh|pytest|lockf'`
listed only other jobs' processes: their own scratch folders, or none of
mine. A `pgrep` for this job's scratch folder found nothing.
