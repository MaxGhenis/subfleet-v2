# Test isolation after fixture undo (D-ST1)

The 2026-10-12 incident let `monkeypatch.undo()` remove desktop overrides,
after which the mirror rewrote 55,279 copies in 635 personal session files.
Two independent defenses now cover implicit personal paths:

- `pytest_configure` installs safe values with `os.environ` before collection.
  Each target is nonexistent beneath a session temporary directory. The
  per-test fixture keeps its own base, test overrides win, and undo restores
  the session values. Session teardown removes the temporary base.
- `subfleet/home_paths.py` refuses personal fallbacks for live pytest sessions
  and standalone `SUBFLEET_UNDER_PYTEST` / `PYTEST_CURRENT_TEST` markers, naming the
  required override. The session marker is installed by the first configure hook
  and survives fixture teardown until unconfigure, covering collection and configure as well as test calls. Desktop
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


## Round 2 fixes (reviewed head `0f9eae1a0b3c`)

The complete external review was read. The earlier production parity and hub
counts are historical evidence, not validation of this round's new commit.

- Every native credential reader now builds its command through the keychain
  guard, including the default desktop profile reader after `undo()`, home
  OAuth fallback, subscription-plan reads, and daemon credential resolution.
  Either marker refuses native security reads without an explicit test runner
  or executable stand-in. The default adapter runner is distinguished from an
  explicitly injected runner, so replacing a captured backend cannot silently
  authorize a personal read. All four readers are in `KEYCHAIN_READERS`.
- The registry now contains fifteen isolated paths. The launchd installer and
  doctor share a guarded plist resolver; the optional `~/bin/agent-secret`
  lookup is guarded before its existence check. An inherited
  `CLAUDE_LANE_AGENT_SECRET` is removed for the session; explicit test helpers
  remain available. Both remaining review accesses are guarded rather than
  justified as harmless reads, because one writes a plist and the other can
  execute a credential helper.
- Configure saves each replaced or removed environment entry, including
  absence and empty values. Unconfigure restores those exact values, removes
  additions, deletes the temporary base, and tolerates repeated cleanup.
- Registry inspection resolves module imports, module-qualified attributes,
  aliases and relative imports, including `from .home_paths import home_path
  as hp`. Seven unregistered spellings and two registered spellings exercise
  the same checker used for production files.

### Captured environment copies (superseded by round 3)

The round-three fix below replaces this lifecycle choice and its regression.

Restoring `os.environ` cannot mutate a dictionary copied earlier by
`daemon_env` or a child's inherited environment. These snapshots deliberately
retain the marker and safe paths after cleanup. They remain test environments;
using one after cleanup still refuses missing overrides and cannot fall back to
personal stores. Removing its marker alone would be unsafe, since other test
variables remain in the snapshot.

An ordinary child started after unconfigure must obtain a fresh environment
(e.g. call `daemon_env` again). No change to `cli.daemon_env` is needed: it must
propagate isolation to children started during the session. No liveness check
expires markers in surviving test descendants. The regression replays the
review's capture-before/unconfigure/use-after input, verifies the captured
child stays isolated, and verifies a fresh ordinary child reads the fixture
login and production defaults under fake HOME. Missing, empty and preexisting
values all restore exactly.

### Round 2 validation

**0 pytest cases executed in round 2.** Eight foreground acquisitions,
including a final guard/module-name retry after offline dev dependencies were
installed, each queued for at most 55 seconds and exited 75 (`already locked`)
before pytest began. No suite-lock hold was acquired. All 26 requested files
below and all three pytest mutation selections remain unrun for the hub.

Each attempt used the prescribed shared lock, `uv run pytest -q`, its own
`sf-r198f2-<pid>/` TMPDIR and fake HOME. The foreground supervisor would interrupt
a runner at 1,090 seconds and finish termination within 1,120 seconds, keeping
any hold below twenty minutes. `shutil.rmtree` cleanup followed release. Dev
dependencies were installed offline from copies of the local cache into the
ignored workspace virtualenv. No network remote or personal store was used.

**60 direct baseline cases passed**, separate from pytest execution:

- 32 registry, undo, keychain, helper/plist and environment-lifecycle regressions;
- 7 existing agent-secret credential cases, including both helper sources,
  failed-helper refusal and native provider command construction with recording
  runners;
- 21 production-parity cases, **420 matching resolver outcomes**, with markers
  removed and fake HOME. The eleven frozen resolver sources also parse and are
  unchanged from the reviewed head.

The executed direct regressions use the committed test functions. Recording
backends make the keychain mutations safe; no native credential command runs.
Each mutation changes one function's code in memory and restores it afterward.

| Mutation | Direct regression functions | Direct result | Pytest result |
| --- | --- | --- | --- |
| Remove native keychain refusal | `test_keychain_after_undo_never_requests_native_command`, `test_every_keychain_reader_refuses_default_backend` | 12 expected assertion failures | Unrun: shared lock |
| Drop unconfigure environment restoration | `test_unconfigure_restores_environment_and_keeps_captured_child_isolated` | 3 expected environment-equality failures | Unrun: shared lock |
| Revert import/attribute resolution | `test_registry_rejects_unregistered_import_spellings`, `test_registry_counts_registered_import_spellings` | 8 expected assertion failures; direct spelling still passes | Unrun: shared lock |

These are direct mutation detections, not claims of failing pytest runs.
The 21 parity cases also remained passing with the unconfigure mutant. A replay
harness initially reused case directories; it was corrected to use a fresh
directory for every invocation, and cleanup mutation failures were confirmed
at the environment-restoration assertion. Production source mutations never
went to disk. Nine changed Python files parse and named-file `git diff --check`
passes. The complete keychain grep has 83 individually classified hits in the
updated path audit.

| Requested file | Pytest cases executed | Status |
| --- | ---: | --- |
| `tests/e2e/test_notice_job_state.py` | 0 | Unrun: shared lock |
| `tests/unit/test_agent_secret_credentials.py` | 0 | Unrun: shared lock |
| `tests/unit/test_claude_adapter.py` | 0 | Unrun: shared lock |
| `tests/unit/test_claude_identity.py` | 0 | Unrun: shared lock |
| `tests/unit/test_claude_usage.py` | 0 | Unrun: shared lock |
| `tests/unit/test_cli.py` | 0 | Unrun: shared lock |
| `tests/unit/test_codex_adapter.py` | 0 | Unrun: shared lock |
| `tests/unit/test_codex_probe.py` | 0 | Unrun: shared lock |
| `tests/unit/test_codex_shim.py` | 0 | Unrun: shared lock |
| `tests/unit/test_codex_turn.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_handoff_briefs.py` | 0 | Unrun: shared lock |
| `tests/unit/test_conversation_redact.py` | 0 | Unrun: shared lock |
| `tests/unit/test_daemon_fifo_reads.py` | 0 | Unrun: shared lock |
| `tests/unit/test_daemon_verbs.py` | 0 | Unrun: shared lock |
| `tests/unit/test_doctor.py` | 0 | Unrun: shared lock |
| `tests/unit/test_doctor_identity.py` | 0 | Unrun: shared lock |
| `tests/unit/test_gate_replay.py` | 0 | Unrun: shared lock |
| `tests/unit/test_live_store_guard.py` | 0 | Unrun: shared lock |
| `tests/unit/test_operator_notices.py` | 0 | Unrun: shared lock |
| `tests/unit/test_policy_support.py` | 0 | Unrun: shared lock |
| `tests/unit/test_sessions_handoff.py` | 0 | Unrun: shared lock |
| `tests/unit/test_soak_tools.py` | 0 | Unrun: shared lock |
| `tests/unit/test_test_module_names.py` | 0 | Unrun: shared lock |
| `tests/unit/test_timers_alerts.py` | 0 | Unrun: shared lock |
| `tests/unit/test_timers_probe.py` | 0 | Unrun: shared lock |
| `tests/unit/test_unadmittable_pin.py` | 0 | Unrun: shared lock |

### Round 2 delivery

- `67ba0c1b9`: native keychain protection, guarded plist/helper paths,
  environment restoration and qualified/aliased registry regressions.
- `2cabd3838`: updates this report and the access audit.
- The final documentation cleanup removes a trailing blank line and clarifies
  the baseline count; no source or test behavior changes.

The shared Git index rejected `index.lock` with `Operation not permitted`.
As authorized, both coherent steps are committed on `fix/test-live-store-guard`
in ignored workspace-local `.git-local`, based on assigned `0f9eae1a0b3c`.
Inspect with `git --git-dir=.git-local --work-tree=. log`. Shared Git metadata,
the caller's checkout, existing history and remote branches were untouched.
Only named source/test/report files were staged; no logs, JSON evidence or
bundles were staged. Both commits include the requested co-author trailer.
All task-owned runners and cache preparation commands have ended; no detached
runner or background build remains.

## Round 3 fixes (reviewed head `e54e4cab4e4a`)

Read the full round-three review, its executed merge probes, regression and
parity scripts, and the merged-path audit. This round addresses all three
findings on the assigned merged head:

- Host credential discovery and Git execution both resolve
  `SUBFLEET_HOST_HOME` and `SUBFLEET_HOST_CONFIG` through `home_path`.
  Configure and the per-test fixture install nonexistent locations from
  `SAFE_HOME_PATHS`, independently of removable HOME/XDG patches. Missing or
  empty locations refuse access during pytest. Git/SSH execution drops
  `SSH_AUTH_SOCK` during pytest; system/global Git configuration stays disabled.
  Explicit fixture credential configurations remain available.
- `SUBFLEET_RESET_INHIBIT` is now registered, replaced at configure, restored
  safely by fixture undo, and guarded at importer selection. An inherited hold
  cannot be read or copied into the state. An explicit hold retains precedence;
  an explicit provider home still locates its default hold when no environment
  override is supplied. Production #179 override behavior is preserved.
- Configure binds the marker to `SUBFLEET_PYTEST_SESSION`, the session temporary
  directory. `under_pytest()` checks that directory before accepting a captured
  session marker. Unconfigure removes it, so an ordinary child using an old
  `daemon_env()` dictionary resolves production Codex/login homes again. A copied
  `PYTEST_CURRENT_TEST` cannot revive an expired session. Active children retain
  the guard, and the E2E harness explicitly propagates the session lifetime.

The directory lifetime was chosen so `daemon_env()` can keep passing isolation
to children during collection, test calls and teardown. Stripping the marker
at capture would expose active children. Cleanup also restores the parent's
marker, lifetime, all registered paths and removed helper to their exact prior
values, including absent and empty values. Bare markers used by standalone
test harnesses continue to guard defaults.

### Round 3 regression evidence and parity

The three reviewer inputs are committed in
`tests/unit/test_live_store_guard_round3.py`. The first test separately checks
credential discovery and the intercepted agent-bearing launch; the captured
environment input separately retains and removes path overrides.

Before changing production code, **all five direct regression inputs failed
on `e54e4cab4e4a`**, at their intended assertions: personal helper settings,
ambient SSH socket, inherited hold read, and two stale-marker children. At the
fixed head, those inputs pass, alongside additional empty/missing refusals,
explicit test overrides, capture during test calls, and active-child guards.
These are direct calls to the committed tests, not pytest runner results.

| Direct validation | Passed |
| --- | ---: |
| New round-three regression cases | 22 |
| Existing exact parent restoration / expired-child cases | 3 |
| Actual registry coverage check, including all new variables | 1 |
| Existing unregistered import-spelling checks | 7 |
| E2E child with all registered paths and a live session lifetime | 1 |
| Test-module basename uniqueness | 1 |
| **Total direct regression cases** | **35** |

The registry check confirms **18 guarded path variables**, including both host
credential locations and the reset hold. Seven changed Python files parse;
named-file whitespace checks pass. An initial direct E2E invocation supplied an
unresolved Darwin TMPDIR spelling; the harness invocation was corrected to use
the resolved temporary path and the complete 34-case isolation replay passed.

Reused the reviewer's parity script without changing its comparisons, from an
ignored workspace-local copy. Against actual local `origin/release/217`,
`bd4c2cc8f23c8605b8dfac73c09f2fcf60b45266`, **21 cases / 672 unique outcomes
match**: 525 resolver/default outcomes and 147 importer path configurations.
This includes empty, absolute, relative and tilde reset overrides, explicit
home and explicit hold precedence. A separate replay of the actual release
host credential and environment functions also passed **5 cases / 10
byte-identical outcomes**, including absent/empty HOME/XDG, spaces, relative
XDG and a fake agent socket. No credential helper or transport was executed.

### Round 3 pytest status

The requested groups use separate foreground lock acquisitions, one pytest
process per hold, the prescribed `sf-r198f3-<pid>/` TMPDIR and a fake HOME.
Each queue wait is bounded at 50 seconds. The foreground supervisor interrupts
at 1,090 seconds and finishes termination within 1,115 seconds, keeping any
hold below twenty minutes. Temporary directories are removed with the required
Python `shutil.rmtree` command after the runner ends.

| Requested group | Pytest cases executed | Status |
| --- | ---: | --- |
| Reviewed-head round-three regression baseline | 0 | Shared lock, exit 75 |
| Both live-store guard files and module names | 0 | Shared lock, exit 75 |
| `tests/unit/test_importer.py` | 0 | Shared lock, exit 75 |
| All 21 `tests/unit/test_host_push*.py` files | 0 | Shared lock, exit 75 |
| `tests/unit/test_cli.py` | 0 | Shared lock, exit 75 |
| Eight reset-credit files listed below | 0 | Shared lock, exit 75 |

Reset-credit selection: `test_reset_credit_claim_timing.py`,
`test_reset_credit_demand.py`, `test_reset_credit_properties.py`,
`test_reset_credit_review_r1.py`, `test_reset_credit_review_r2.py`,
`test_reset_credit_review_r3.py`, `test_reset_credit_review_r3_mutants.py`,
and `test_timers_reset_credits.py`, all under `tests/unit/`.

**0 pytest cases executed in round 3.** All six acquisitions exited 75 before
pytest startup, after 50.3–50.5 seconds each; no suite-lock hold was acquired.
All 34 requested files remain unrun for the hub. Historical hub counts elsewhere in this
report do not validate this head. Offline dev dependencies and workspace entry
points are installed only in ignored `.venv`. Logs, copied probe scripts and
JSON evidence remain ignored under `.uv-cache`; none is staged.

### Round 3 delivery

- `8053545a0`: commit the reviewer-input regression tests before the fix.
- `cc95f0aef`: isolate host credentials and reset holds, expire captured markers,
  propagate the E2E session lifetime, and extend regressions.
- The following documentation commit records the completed results.

The shared Git metadata is outside the writable sandbox. As authorized, commits
are on `fix/test-live-store-guard` in workspace-local `.git-local`, based on the
assigned `e54e4cab4e4a`. Inspect with
`git --git-dir=.git-local --work-tree=. log`. Only named source, test and report
files are staged; all commits carry the requested co-author trailer. The caller
checkout, shared metadata and existing history are unchanged. No remote or
sub-agent was used, and nothing was pushed.
All task-owned runners and preparation commands have ended; nothing remains
detached or running.


## Round 4 fixes (reviewed head `bce9baea5b68`)

Read the full round-four review and its executed lifetime input. Configure now
exclusively creates a dedicated `.pytest-session` sentinel with a random
256-bit token. `SUBFLEET_PYTEST_SESSION_ID` records its device, inode and token;
`SUBFLEET_PYTEST_SESSION` retains the session base path. `under_pytest()` verifies
the opened sentinel's identity instead of directory existence. Store writes
cannot revive an expired capture, including a copied `PYTEST_CURRENT_TEST`.

Unconfigure deletes the sentinel before restoring the environment and removing
stores, so surviving or recreated directories cannot keep a marker active.
Restoration includes the new identity, preserving absent, empty and configured
originals. The E2E harness propagates both lifetime variables; `daemon_env()`
already preserves them. Bare standalone markers retain their guard behavior.
This supersedes the directory lifetime described in round three.

### Regression evidence

The reviewer input is committed in `tests/unit/test_live_store_guard_round4.py`.
Before changing production code, **both direct cases failed on `bce9baea5b68`**
at `settings write revived an expired session`, with and without a captured
current-test value. After the fix, the child writes retained synthetic settings,
recreates the old base, remains ordinary, selects fake HOME's login/account,
and resolves a missing session-store override beneath that fake HOME.

Six additional cases cover sentinel removal, a changed token with the same
inode, a replacement inode with the same token, missing identity, a surviving
store directory, and a fresh session at the same path. The replacement test
keeps the original inode allocated, avoiding reliance on inode reuse.

| Direct validation | Passed |
| --- | ---: |
| New round-four lifetime cases | 8 |
| All retained round-three cases | 22 |
| Retained guard and credential cases | 39 |
| E2E propagation and module-name uniqueness | 2 |
| Release-parity cases after the correction below | 21 |
| **Total unique direct cases** | **92** |

These are direct calls to committed tests, separate from pytest counts. The
round-three/round-four replay also verifies exact environment restoration after
each case. That check caught a new test fixture undo ordering issue; scoped
patches now restore the synthetic identity before unconfigure restores the
enclosing session. The complete final replay passes.

### Release-parity correction

The first acquired guard run produced **352 passed, 3 failed**. All failures
were retained parity cases with literal-tilde HOME and absolute, relative or
tilde `CLAUDE_CONFIG_DIR`. Python 3.12's `Path.home()` raises for that HOME.
Release evaluates it eagerly; the assigned head's MCP resolver skipped it when
an override existed. This MCP function was unchanged from `bce9baea5b68`, and
the frozen release body matches actual release commit
`bd4c2cc8f23c8605b8dfac73c09f2fcf60b45266`.

`global_config_path()` now preserves release's eager HOME evaluation outside
pytest, retaining guarded selection before implicit HOME access during active
tests. No parity assertion was weakened or skipped. All 21 parity cases pass
directly, and the full guard/MCP rerun passes below.

### Pytest validation

| Requested group | Final pytest result | Pytest duration |
| --- | ---: | ---: |
| Live-store guard, round 3, round 4, module names; MCP resolver and launch | **398 passed** | 29.51 s |
| `tests/unit/test_cli.py` | **185 passed** | 46.01 s |
| `tests/unit/test_hooks.py` | **0 executed; unrun: shared lock** | — |
| Reviewed-head regression baseline | 0 executed; shared lock; direct failures above | — |

The final guard group includes all 355 guard/module-name cases and 43 MCP cases,
superseding the earlier three parity failures. CLI ran at `e1544fda9`; the later
MCP correction only restores production HOME evaluation, and active pytest
selection is unchanged. **583 pytest cases passed in the final successful
runs.** Hooks still needs a host/hub run after three blocked acquisitions.

Every foreground attempt used its own `sf-r198f4-<pid>/` TMPDIR, fake HOME,
the prescribed shared lock, and `uv run pytest -q -p no:cacheprovider`.
Six acquisitions timed out after 50.2–50.3 seconds, before pytest startup: the
baseline, first guard group, three hooks attempts, and first guard/MCP rerun.
The acquired runs took 98.4 s (CLI), 78.1 s (initial guard), and 73.7 s (final
guard/MCP), including acquisition. Each hold was below twenty minutes, with
no wait for machine load. The supervisor would interrupt at 1,090 seconds and
finish termination by 1,115 seconds. Each temporary root was removed with the
required Python `shutil.rmtree` command. Six changed Python files parse, and
named-file whitespace checks pass.

### Delivery

- `ca41c6732`: reviewer-input regression committed before the production fix.
- `e1544fda9`: sentinel validation, cleanup, E2E propagation and extra cases.
- `868ad6ba6`: restore release MCP HOME evaluation for the retained parity cases.
- This report is delivered in a separate documentation commit.

The shared Git metadata is outside the writable sandbox. As authorized, commits
are on `fix/test-live-store-guard` in workspace-local `.git-local`, based on
assigned `bce9baea5b68`; inspect with
`git --git-dir=.git-local --work-tree=. log`. Only named source, test and report
files were staged. All commits include the requested co-author trailer.
Dependencies were prepared offline from cached packages in ignored `.venv`;
no logs, JSON evidence, dependency files or bundles were staged. Tests used fake
HOME and recording credential backends. No remote, sub-agent or push was used.
The caller checkout and shared metadata remain untouched. All task-owned
runners and preparation commands have ended.
