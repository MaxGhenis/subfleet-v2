# D-ST3: a pytest process never resolves the account's live roots, whatever os.environ holds

Branch `fix/st3-process-isolation-217`, cut from `origin/release/217` at `473fe2941`
(#222, the launch-env allowlist, merged as `f99da54b0`). The caller's checkout holds
that branch, so each job committed on a detached HEAD in its own worktree; Subfleet
keeps the commits as a salvage ref. Nothing was pushed. No live store, live daemon,
`~/.subfleet`, `~/.claude`, `~/.codex` or `~/chief-of-staff/state` file was opened,
written or listed, and no live process was signalled. The first job signalled only its
own lockf waiter; the second signalled nothing. No sub-agents.

**Two jobs.** The first job (`20261020-073517-st3-build-b`) wrote the commits up to
`d6e7fd601` and the body of this report. It was lost at about 15:25Z on 2026-10-20,
when the host lost power, so it never committed this report or reported its final runs.
A second job (`20261020-114751-st3-build-c`) finished the work. That job's first attempt
also ended without a final answer: Subfleet salvaged its worktree at 19:22Z, holding
only this report. It had run three holds at `d6e7fd601` but had not written their
results into the report. Its second attempt did four things:

- checked every item of the brief against the commits (below), and reran the `grep`
  and the fallback scan;
- found that `origin/release/217`, as the hub last fetched it, is still `473fe2941`,
  the commit this branch was cut from, so there was nothing to merge;
- ran the briefed files, then as much of the rest of `tests/unit` and `tests/fake` as
  the shared test lock allowed, all at `d6e7fd601` (Runs);
- committed this report.

Every count in Runs comes from the second attempt's own runs at `d6e7fd601`, except
one table, labelled as the first attempt's. The release run under "The cause, as
executed" is the first job's, and that section says so.

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
| (this report) | Report, committed by the second job |

## The brief's items, checked against the commits

| Item | Where | Status |
|---|---|---|
| #222 in the base (`git log --oneline origin/release/217 \| grep -m1 '#222'`) | `f99da54b0`, an ancestor of `473fe2941` | Done |
| 1. Process-level detection that never reads the environment | `81a672496`: `home_paths.pytest_loaded` | Done; why it cannot be spoofed or missed is under The fix |
| 1. No `subfleet/` code imports pytest | `grep -rn pytest subfleet/` finds only comments, strings and the `under_pytest`/`PYTEST_*` names (rerun by the second job); static test `test_no_subfleet_module_imports_pytest` | Done |
| 1. Folded into every live-root refusal | `81a672496`: `under_pytest()` is either rule for this process | Done |
| 2. One explicit seam, only while HOME is not the account's | `be8cbb767` (`production_process`, `waive_process_rule`), `81a672496` (`_process_rule_waived`, read again at every call) | Done |
| 2. Every test changed, and why | `70bad7117`, `d6e7fd601`; listed under Tests changed | Done |
| 3. Every fallback to the account's home under `subfleet/`, routed through the guarded functions | `81a672496` routes the two that were not (`compat.v1_binary`, `prune_decisions`); listed below | Done. The second job reran the scan (an AST walk of every `Path.home()`, `pwd.getpw*`, `expanduser`, `"HOME"` and `~`-literal site: 137 sites) and read each site outside the table. Each expands a value its caller supplied. |
| Regressions under `{}` and a PATH-only dict, with spies on `os.chmod`, `open` and `sqlite3.connect` (and more) | `be8cbb767`, `f45fdabab`, `d6e7fd601` | Done; at release they fail (the first job's run, below) |
| The same for `production_state_root`, `home_path` with each default, `protected_provider_homes`, `codex_app_home`, `desktop_login_path` | `be8cbb767` | Done |
| Property over arbitrary environment dicts (bounded Hypothesis) | `be8cbb767`, `6b07533ab` | Done; 150 derandomized examples, no database |
| Mutation: the process rule removed fails a named test | `test_live_store_guard_st3_mutations.py::...[no-process-rule]` | Done; all 8 mutants were killed at `d6e7fd601` (Runs) |
| Live store spied, never read or statted | the spies in `test_live_store_guard_st3.py` | Done |
| Contract amendment with change-log entry | `3e104df38` | Done |
| Briefed runs, then the whole of `tests/unit` and `tests/fake` | the second job | See Runs |
| Report | this file | Committed by the second job |

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
the second job's hold 1, every control passed and all eight mutants failed their named
tests (8 passed, Runs).

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

All runs are at `d6e7fd601`; there was no merge, because `origin/release/217` is the
branch's base. They used GIL CPython 3.14.7, pytest 9.1.1 and Hypothesis 6.168.1 with
`HYPOTHESIS_PROFILE=ci`. Each hold was one pytest process under the shared suite lock,
run with the brief's command (`lockf -k … uv run pytest -q -p no:cacheprovider`).
The briefed patterns matched 52 files: 16 `test_live_store_guard*`, 8 `test_launch_env*`,
2 `test_claude_mcp*` and 21 `test_host_push*`, plus the five named files. No
`test_home_paths*.py` or `test_credentials*.py` exists in `tests/unit`.

### The second attempt's holds

Status at 21:36Z: none has run yet. The second attempt queued its first hold at
20:08Z behind up to 11 other holds (load average 70 to 165). It then requeued it as
one larger hold (A2, below) at 20:52Z, and A2 is still waiting for the lock.

<!-- the second attempt's results replace this status as each hold ends -->

### The first attempt's holds (the same job and head; not counted above)

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
files it did not finish, and failed none. The first attempt never ran the remaining
328 files.
