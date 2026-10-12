# Test isolation after fixture undo (D-ST1)

The 2026-10-12 incident let `monkeypatch.undo()` remove desktop overrides,
after which the mirror rewrote 55,279 copies in 635 personal session files.
Two independent defenses now cover implicit personal paths:

- `pytest_configure` installs safe values with `os.environ` before collection.
  Each target is nonexistent beneath a session temporary directory. The
  per-test fixture keeps its own base, test overrides win, and undo restores
  the session values. Session teardown removes the temporary base.
- `subfleet/home_paths.py` refuses personal fallbacks while
  `PYTEST_CURRENT_TEST` is present, naming the required override. Desktop
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
pytest marker and with fake HOME values. Seven cases cover absent, empty,
absolute, relative and tilde overrides, legacy MCP config, and a literal tilde
in HOME. Only overrides are expanded where defaults were already constructed
from HOME, preserving the original expansion behavior. Existing state-root,
provider and identity tests now explicitly select their isolated paths.

## Validation

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

## Commits and Git storage

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
