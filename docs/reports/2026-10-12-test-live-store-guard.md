# Test isolation after fixture undo (D-ST1)

The 2026-10-12 incident let `monkeypatch.undo()` remove desktop overrides,
after which the mirror rewrote 55,279 copies in 635 personal session files.
Two independent defenses now cover implicit personal paths:

- `pytest_configure` installs safe values with `os.environ` before collection.
  Each target is nonexistent beneath a session temporary directory. The
  per-test fixture keeps its own base, test overrides win, and undo restores
  the session values. Session teardown removes the temporary base.
- `subfleet/home_paths.py` refuses personal fallbacks while
  `SUBFLEET_UNDER_PYTEST` or `PYTEST_CURRENT_TEST` is present, naming the
  required override. The session marker is installed by the first configure hook
  and survives teardown, covering collection and configure as well as test calls. Desktop
  store/log, transcripts, notifications, Claude config/MCP/settings/login,
  Codex desktop catalog/auth readers, and legacy provider discovery use it.
  C-23.28 gains one incident-dated sentence specifying both defenses.

The daemon state root has the same exposure: client, daemon/importer entry
points and guard scratch-root resolution now protect `SUBFLEET_HOME` with
both layers. Claude adapter homes protect `CLAUDE_CONFIG_DIR`. Historically
fixed Codex app, Claude login and provider-base defaults have test-only seams
that are ignored outside pytest. The service's protected-home checks and
production state-directory identity comparison also use the guard before
resolving personal paths. Codex lane homes already require an explicit lane
home or credential reference; preflight without a Codex home uses fresh
scratch state instead of the desktop home. Explicit paths retain precedence.

Production parity tests compare eleven original resolver bodies frozen from
the assigned `release/217` snapshot, `24be965c5ce8`, in children without the
pytest marker and with fake HOME values. Twenty-one cases cover seven override/configuration modes across ordinary,
space-containing and literal-tilde HOME spellings. They compare a returned value
or an exception type and message, including legacy MCP configuration. Only overrides are expanded where defaults were already constructed
from HOME, preserving the original expansion behavior. Existing state-root,
provider and identity tests now explicitly select their isolated paths.

## Original validation (before review round 1)

- All 27 changed Python files compile; all eleven frozen resolver bodies parse.
- `git diff --check` passes. Both temporary mutations were restored exactly.
- **0 pytest cases executed.** Eight lock acquisitions (seven 60-second waits,
  one 600-second wait) all exited 75, `already locked`, before pytest started.
  The 104 added regression cases, including 77 production-parity comparisons,
  remain unexecuted. There were no successful suite-lock holds.

Mutation attempts were also blocked before pytest started:

| Removed layer | Targeted test in `tests/unit/test_live_store_guard.py` | Cases | Result |
| --- | --- | ---: | --- |
| Session assignments in `pytest_configure` | `-k undo_restores_session_safe_path` | 32 | Not executed; restored |
| Resolver refusal branch | `-k missing_override_refuses_fallback` | 16 | Not executed; restored |

Exactly these test files could not run:

- `tests/unit/test_test_module_names.py`
- `tests/unit/test_live_store_guard.py`
- `tests/unit/test_sessions_cli.py`
- `tests/unit/test_sessions_mirror.py`
- `tests/unit/test_sessions_revive_reads.py`
- `tests/unit/test_sessions_revive.py`
- `tests/unit/test_sessions_mirror_load_gap.py`
- `tests/unit/test_sessions_mirror_fifo.py`
- `tests/unit/test_sessions_mirror_safety.py`
- `tests/unit/test_sessions_transcripts.py`
- `tests/unit/test_notify_push.py`
- `tests/unit/test_daemon_verbs.py`
- `tests/unit/test_claude_mcp.py`
- `tests/unit/test_claude_adapter.py`
- `tests/unit/test_doctor.py`
- `tests/unit/test_hooks.py`
- `tests/unit/test_conversation_catalog.py`
- `tests/unit/test_conversation_catalog_lifecycle.py`
- `tests/unit/test_conversation_handoff.py`
- `tests/unit/test_conversation_service.py`
- `tests/unit/test_desktop_account_cache.py`
- `tests/unit/test_capacity_desktop.py`
- `tests/unit/test_conversation_peers_attachments.py`

The original `git grep -l SUBFLEET_SESSION_STORE tests/` test modules are
included above; `tests/conftest.py` and `tests/sessions_fixtures.py` are helper
files used by those modules. Every attempted command used foreground
`/usr/bin/lockf -k` on `/private/tmp/claude-501/subfleet-suites.lock`, its own
prescribed TMPDIR, and Python `shutil.rmtree` cleanup. No personal stores or
live processes were inspected or modified. Dependency setup finished before
lock attempts; no pytest runner or build remains running.

## Original commits and Git storage

Implementation commits on `fix/test-live-store-guard`:

- `902c4d2e1`: session baselines, shared guard, resolvers, regressions and contract.
- `c0aa2c7d8`: existing state/provider fixtures updated for guarded defaults.
- `15561a305`: preserve production expansion exactly; add literal-tilde parity case.
- `25ae27f8f`: guard the state-directory identity comparison and protected-home consumers.

The shared index refused `index.lock` with `Operation not permitted`; changing
the checkout's `.git` marker is also prohibited. Commits therefore live in
workspace-local `.subfleet-job-git`, based on `24be965c5ce8`. Inspect or import
with `git --git-dir=.subfleet-job-git --work-tree=.`. Only named source, test,
contract and report files are staged. The caller checkout and shared
branch/index are unchanged; history was not rewritten and no Git network
remote was used. This report is committed separately.


## Fix round 1

Read all of `review-r1-full.txt` and addressed its four findings:

- Configure installs `SUBFLEET_UNDER_PYTEST=1` before creating the session base,
  with `tryfirst=True`. The resolver checks either marker by membership; an empty
  marker still guards defaults. Neither session values nor the marker are restored
  to personal values at teardown.
- The thirteen guard variables now live in `home_paths.SAFE_HOME_PATHS`, used by
  configure, the per-test fixture and e2e. Four new test-only seams cover v1 state,
  delegate state, roster and v1 launch-agent discovery. Importer, legacy cockpit,
  importer CLI and transfer defaults pass the guard before reading. The original
  import-time Path defaults and explicit argument precedence remain intact outside
  pytest; CLI defaults retain those Path objects until guarded resolution.
- E2E children receive every registered path plus the session marker, beneath
  their own fake home. State points at the harness root, and both Claude home
  variables point at its existing fake transcript directory.
- Parity compares values or exception type/message. The literal-tilde HOME cases
  pass on Python 3.14.7, including when both versions raise.

The guard file now defines 253 pytest cases, including actual module-import
collection snapshots, direct configure-hook checks with `Path.home` forbidden,
80 undo cases across twenty resolver forms, missing and empty override refusals,
real legacy defaults and e2e child propagation. No nested pytest is invoked.

The [path audit](2026-10-12-test-live-store-guard-path-audit.md) lists every one
of the 235 remaining hits from the requested `git grep` with its safety reason.
Captured legacy defaults perform no I/O and are guarded at use. Explicit
caller/credential/workspace/policy paths retain precedence. Remaining launch-agent
installation/doctor and optional ~/bin executable lookup are identified as outside
desktop/session/account stores. No implicit `~/.subfleet-v1` path remains.

### Round 1 verification

Independent direct probes used fake HOME trees only, under the prescribed TMPDIR:

| Probe | Checks passed |
| --- | ---: |
| Production outcomes: 20 resolver/default forms × 7 modes × 3 HOME spellings | 420 |
| Frozen release/217 bodies AST-matched to `24be965c5ce8` | 11 |
| Static registry check covers every production guard variable | 13 variables |
| Configure resolver override/refusal checks | 40 |
| Import-time resolver snapshots with no test-current marker | 20 |
| Actual configure + autouse fixture + MonkeyPatch undo restoration | 80 |
| Real legacy import and transfer with omitted defaults | 2 |
| E2E child sees registered guard variables and reads isolated desktop login | 13 variables |

All six changed Python files parse, and named-file `git diff --check` passes.
These direct probes are separate from pytest counts.

Each mutation replaces one function's code in memory, removes exactly one AST
statement, and leaves source files unchanged. The session mutant removes the
actual `os.environ.update()` call rather than a bracket-assignment regex.

| Mutation | Regression target | Selected pytest cases | Direct probe mismatches detected | Pytest result |
| --- | --- | ---: | ---: | --- |
| Remove configure session update | `test_undo_restores_session_safe_path` | 80 | 80 | Unrun: shared lock |
| Remove resolver refusal | `test_missing_override_refuses_fallback` | 20 | 20 | Unrun: shared lock |
| Remove configure marker | `test_collection_phase_is_isolated` | 20 | 20 | Unrun: shared lock |

The direct mismatches demonstrate which assertions detect the removed defenses;
there is no claim that the mutation pytest cases ran or failed.

**0 pytest cases executed in round 1.** Eight foreground acquisitions each
waited at most 55 seconds for the shared lock and exited 75, `already locked`,
before pytest started. This includes the three mutations, the guard suite, the
requested unit files, importer/transfer, and the daemon e2e file. There were no
successful lock holds; all 43 files below remain for the hub to run.

Every attempted runner was foreground, with one pytest command per prospective
hold, a distinct `sf-r198f1-<pid>/` TMPDIR, fake HOME, and `shutil.rmtree` cleanup.
The bounded runner would interrupt after 1090 seconds including queue time and
allow 30 seconds for cleanup, keeping any hold below twenty minutes. No personal
store or existing live process was inspected, no remote was used, and no runner
or build remains running.

### Round 1 pytest counts per file

| File | Cases executed | Status |
| --- | ---: | --- |
| `tests/e2e/test_notice_job_state.py` | 0 | Unrun: shared lock |
| `tests/unit/test_capacity_desktop.py` | 0 | Unrun: shared lock |
| `tests/unit/test_claude_adapter.py` | 0 | Unrun: shared lock |
| `tests/unit/test_claude_mcp.py` | 0 | Unrun: shared lock |
| `tests/unit/test_claude_mcp_launch.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_catalog.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_handoff.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_handoff_briefs.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_peers_attachments.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_service.py` | 0 | Unrun: shared lock |
| `tests/unit/test_daemon_verbs.py` | 0 | Unrun: shared lock |
| `tests/unit/test_desktop_account_cache.py` | 0 | Unrun: shared lock |
| `tests/unit/test_doctor.py` | 0 | Unrun: shared lock |
| `tests/unit/test_hooks.py` | 0 | Unrun: shared lock |
| `tests/unit/test_importer.py` | 0 | Unrun: shared lock |
| `tests/unit/test_lanes_transfer.py` | 0 | Unrun: shared lock |
| `tests/unit/test_live_store_guard.py` | 0 | Unrun: shared lock |
| `tests/unit/test_mirror_flags_faults.py` | 0 | Unrun: shared lock |
| `tests/unit/test_mirror_flags_model.py` | 0 | Unrun: shared lock |
| `tests/unit/test_mirror_flags_stateful.py` | 0 | Unrun: shared lock |
| `tests/unit/test_notify_push.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_cli.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_handoff.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_handoff_excerpts.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_headless_properties.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_fifo.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_hot_health.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_inventory.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_load_gap.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_safety.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_mirror_write_churn.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_nudge.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_registry.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_revive.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_revive_reads.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_transcripts.py` | 0 | Unrun: shared lock |
| `tests/unit/test_test_module_names.py` | 0 | Unrun: shared lock |

### Round 1 delivery

- `69700e940`: collection marker, central registry, legacy defaults, e2e
  isolation, configure/collection/undo regressions and outcome parity.
- `31d9945e2`: ensure every production guard variable is in the shared registry.
- This report and the individual path audit are delivered in the following
  documentation commit.

The assigned checkout is detached at `a5df7f27b223`; the shared index refused
`index.lock` with `Operation not permitted`. As authorized, the fix and this
report/audit are committed on `fix/test-live-store-guard` in ignored workspace-local
`.git-local`, based on that assigned head. Inspect with
`git --git-dir=.git-local --work-tree=. log`. Only the six named Python files and
two named report files are staged. The caller checkout, shared metadata and
existing history remain unchanged. All commits carry the required co-author
trailer. No logs, JSON evidence, dependency files or bundles are staged.
