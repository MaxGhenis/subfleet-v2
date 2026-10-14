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


## Round 5: Python 3.12 CI compatibility (assigned head `d49195394b6d`)

Read the supplied CI log: seven failures, 12,368 passed, 32 skipped and one
expected failure. All seven failing tests express legitimate production safety
or stubbed-credential expectations and remain unchanged.

### Causes and fixes

- The three folder-spelling failures enter `ConversationService._check_workspace`.
  Its guarded `CLAUDE_CONFIG_DIR` and `SUBFLEET_CODEX_APP_HOME` selections replaced
  the HOME-based protected paths. The first test happened to refuse on its Codex
  override, reporting `.codex` instead of `.claude`; the unlistable and sealed
  HOME tests never checked their fake Claude homes, so neither refused.
- The two permission variants and case-alias workspace-check failure use that
  same check. Their patched `Path.home()` was hidden by the session overrides,
  permitting a writable workspace containing the test's `.claude`.
- The policy credential test reaches `credentials.keychain_command` through
  `resolve_credential`. It checked `runner=None` before the caller selected the
  patched `subprocess.run`, incorrectly refusing a fully stubbed execution.

`protected_provider_homes()` first requires the existing guarded overrides.
During tests it also protects `.claude` and `.codex` beneath the supplied HOME,
putting those paths first so the original refusal and diagnostic survive.
The account's actual home is identified through the password database; an
identical home or a filesystem alias adds no implicit store paths. Alias
comparison inspects only the home directories themselves. An inaccessible
synthetic home still reaches the existing fail-closed spelling check. Ordinary
production selection is unchanged, and fake HOME never authorizes reader
fallback when a required override is absent.

All four credential execution sites now pass the selected execution callable
to the guard. This permits patched subprocess stubs while retaining refusal
for the native runner and native `security` commands. Adapters check their
captured runner, so a later module patch cannot authorize an already captured
native backend. The helper lookup, session baselines, sentinel lifetime,
configure/unconfigure restoration and registry are unchanged.

Two earlier refusal regressions now identify their recording callable as the
native-runner sentinel as well as the selected subprocess. Their old assumption
that any patched subprocess should be refused is obsolete: this round explicitly
requires stubs to work. All existing refusal assertions remain intact, including
zero backend calls; recording keeps the regression harmless under mutation.
The new round-five file adds eleven cases for account-home aliases, missing
reader overrides, five patched credential readers and captured-runner refusal.

### Validation

**0 pytest cases executed in round 5.** All four sequential foreground
acquisitions exited 75 (`already locked`) before pytest startup. No suite-lock
hold was acquired. These results do not validate the implementation at runtime;
all 28 selected files remain unrun for the hub.

| Requested group | Pytest cases executed | Lock wait | Status |
| --- | ---: | ---: | --- |
| Three CI-failing files: folder spelling, app cutover daemon, policy support | 0 | 50.32 s | Unrun: shared lock |
| Guard, round 3, round 4, round 5, module names, MCP resolver and launch | 0 | 50.40 s | Unrun: shared lock |
| `tests/unit/test_cli.py` | 0 | 50.22 s | Unrun: shared lock |
| Seventeen related HOME/credential/adapter/hooks modules below | 0 | 50.30 s | Unrun: shared lock |

The guard selection was `tests/unit/test_live_store_guard.py`,
`test_live_store_guard_round3.py`, `test_live_store_guard_round4.py`,
`test_live_store_guard_round5.py`, `test_test_module_names.py`,
`test_claude_mcp.py` and `test_claude_mcp_launch.py`, all under `tests/unit/`.
The three failing files were `tests/fake/test_folder_spelling.py`,
`tests/unit/test_app_cutover_daemon.py` and `tests/unit/test_policy_support.py`.

Searched Python test sources for HOME setters, direct environment assignments,
patched `Path.home`, and credential/home-path patches. Fifteen additional
matching modules were selected, plus the adapter and hooks suites affected by
this change or pending from round four. These seventeen files remain unrun:

- `tests/fake/test_unadmittable_pin_rereview.py`
- `tests/unit/test_folders.py`
- `tests/unit/test_agent_secret_credentials.py`
- `tests/unit/test_conversation_handoff.py`
- `tests/unit/test_timers_busy_read.py`
- `tests/unit/test_conversation_peers_attachments.py`
- `tests/unit/test_status_json.py`
- `tests/unit/test_conversation_catalog.py`
- `tests/unit/test_host_push_quarantine_restart.py`
- `tests/unit/test_timers_probe.py`
- `tests/unit/test_conversation_service.py`
- `tests/unit/test_conversation_catalog_lifecycle.py`
- `tests/unit/test_host_push.py`
- `tests/unit/test_conversation_fifo_reads.py`
- `tests/unit/test_soak_tools.py`
- `tests/unit/test_claude_adapter.py`
- `tests/unit/test_hooks.py`

The search also found `tests/canonical_identity_privacy.py` and
`tests/e2e/conftest.py`, both harness helpers rather than standalone test modules.
The retained guard regression verifies E2E HOME/registry/lifetime propagation.

All six changed Python files parse, all 28 selected test files exist, and
`git diff --check` passes. Each attempt used its own prescribed
`sf-r198f5-<pid>/` TMPDIR and fake HOME, `/usr/bin/lockf -t 50 -k` on the shared
lock, and `uv run pytest -q -p no:cacheprovider`. The 50-second queue bound
prevented indefinite waiting. The foreground supervisor would interrupt at
1,090 seconds and terminate within 1,120 seconds, keeping any hold below twenty
minutes without waiting for machine load. All four temporary roots were removed
with the required Python `shutil.rmtree` command. No task-owned runner remains.

### Delivery

- `543172766`: preserved implementation commit, including eleven new round-five
  regression cases and the two retained native-refusal regression adjustments.
- This report is committed separately after the four blocked validation attempts.

Commits use workspace-local `.git-local`, on `fix/test-live-store-guard` based on
assigned `d49195394b6d`, because the linked shared metadata is outside the writable
sandbox. Inspect with `git --git-dir=.git-local --work-tree=.`. Only named source,
test and report files are staged, with the requested co-author trailer. Python
3.12.14 and locked dependencies were prepared offline in ignored `.venv` from
workspace copies of cached packages. No remote, sub-agent, push, caller-checkout
write or personal-store access was used.

## Round 6: refuse by the binary, not the runner (reviewed head `5f4a5956a34f`)

Read `review-r6.md`, `review-r6-full.md` and the probes in `review-r6-evidence/`.
This round adopts the hub's decision, which is the reviewer's design.

### Cause

Round 5 passed `keychain_command` the callable it would execute. The guard
still refused only `None` or `_NATIVE_RUNNER`, so any object that handed the
call on to the real `subprocess.run` was a different object and passed. Such
objects include a recording pass-through, the suite's selective pass-through
idiom, a lambda, `functools.partial`, `Mock(wraps=)` and a `Popen`-based wrapper.
It then ran `security find-generic-password`. The production
`enrollment_runner` is such a wrapper, and `Daemon._enroll_lane_locked` installs
it around the adapter's native runner, so a re-enrolment reached `security` with
no test patch at all. No identity rule can tell a stub from a delegate.

### Fix

`subfleet/credentials.py` adds `install_keychain_exec_guard()`, a `sys.addaudithook`
hook that judges the program a launch would start:

- **Events:** `subprocess.Popen`, `os.posix_spawn` (which also serves
  `posix_spawnp`), `os.exec`, and `os.spawn`. `os.spawn` is native only on
  Windows; elsewhere `os.spawn*` forks, and the child's exec raises `os.exec`.
- **Finding the program:** a name with a directory is taken relative to the
  child's working directory (`Popen`'s `cwd`). A bare name is searched as
  `_posixsubprocess` searches it: the first executable regular file on the call's
  PATH (`os.get_exec_path(env)`), relative entries again under the child's
  working directory. `posix_spawn` events are checked both as `posix_spawnp`
  (the caller's PATH) and as a plain path.
- **Matching:** the program is the login keychain's reader when its real path,
  case- and Unicode-folded, is `/usr/bin/security`, or when it is the same file
  (device and inode). Folding catches `//usr/bin/security`, dot segments,
  `/USR/BIN/SECURITY` and `/usr/bin/ſecurity`; APFS starts all of these as the
  same file. The identity catches aliases no spelling reveals, such as
  `/.vol/<device>/<inode>`.
- **Refusal:** `KeychainExecRefused`, an `AdapterError` with code 7 and the early
  refusal's message and fix. Callers already report that error:
  `resolve_credential` and `_resolve_keychain_token` raise it,
  `_keychain_blob`, `home_login` and `_plan_from_keychain` return `None`, and the
  daemon maps it to `ProtocolError`. `Popen` closes its pipes before re-raising,
  so the refusal never hangs.
- **When it is active:** when the event fires, `under_pytest()` holds, or the
  pytest session that was live when the hook was installed still has its
  sentinel. The pin goes beyond the brief: a test that strips the markers from
  the session's own environment cannot switch the hook off, and cleanup's
  deletion of the sentinel still ends it.
- **Installation:** `tests/conftest.py::pytest_configure` calls it after
  setting the session markers. `subfleet.credentials` calls it at import when
  `SUBFLEET_UNDER_PYTEST` or `PYTEST_CURRENT_TEST` is present, which covers
  Python children such as test daemons. A second call adds no hook; it only pins
  a newly live session. The hook binds its helpers by default argument, so
  replacing the module's attributes cannot change it. Only the test stand-in
  registry is read at event time, and it can only add refusals.
- **Outside pytest:** nothing installs the hook, and an installed hook is inert
  without a live session. `keychain_command` still builds every spelling's argv
  there.

`keychain_command`'s identity check stays as the early refusal that explains
itself. For P3 it now uses the same file match instead of comparing strings, and
a bare `security` is still refused by name whatever PATH holds.
`home_paths.session_alive()` is the sentinel check `under_pytest()` already made,
factored out so the hook can check its pin.

The round-1 regressions (`test_keychain_after_undo_never_requests_native_command`
and `test_every_keychain_reader_refuses_default_backend`) again run the real
default backend. Neither `subprocess.run` nor `_NATIVE_RUNNER` is replaced. A
recording `security` comes first on PATH, and they assert it never ran and that
the refusal was code 7. Round 5's captured-runner test uses the same pattern and
drops its `_NATIVE_RUNNER` substitution. CI's stub tests still pass, because a
stub never launches.

`tests/keychain_standin.py` holds the recording stand-in and an independent
safety net. That net is a second audit hook, sharing no code with the guard. It
refuses and records any launch that still resolves to `/usr/bin/security`, and
each probe asserts it recorded nothing.

### Invariants

Stated and tested in `test_live_store_guard_round6.py` and, as properties, in
`test_keychain_exec_guard_properties.py` (Hypothesis, derandomized, no database):

1. While a session is live, no Python launch whose program is
   `/usr/bin/security` starts, by any spelling or alias, through any runner,
   wrapper or fixture undo. It raises `KeychainExecRefused` (code 7).
2. A launch is judged only by its program. A stub that never launches is never
   refused, and no other program is (every spelling of `/usr/bin/true`).
3. Differential: for any PATH built from a stand-in, the real directory,
   `/bin`, a missing directory, a non-executable `security` and a directory
   named `security`, the guard finds the same program as `shutil.which`, and
   refuses exactly when that is the real binary.
4. Outside a live session, the hook is absent (no markers) or inert (expired
   markers).
5. Installation is idempotent: one hook per process.

For the D-21 production root (`cacf61969`), stated and tested in
`test_live_store_guard_state_root.py`, two of them as properties:

6. Differential, under pytest with a test HOME: the development build is
   refused exactly when the daemon's root is the same file as `HOME/.subfleet`,
   for every spelling of HOME and root without case variants (see the case
   variants under the CI section).
7. Under pytest, no spelling of the account's own home leads to any access at or
   beneath its `.subfleet`. The request is refused, naming HOME.
8. Outside pytest, `production_state_root()` returns what release/217's
   expression returns, for all 21 HOME and override modes.
9. Without a development build named, the root is never asked.

### Regression evidence

`test_live_store_guard_round6.py` ports the reviewer's probes and registers the
stand-in with the guard, so each refusal it shows is the one the real binary
gets. Spellings of the real path are checked by name and by synthetic
`sys.audit` events, which reach every hook and start no program.

| Group | Cases | `5f4a5956a` export with these tests | Head |
| --- | ---: | --- | --- |
| p1a: delegating `subprocess.run` patch, 8 wrappers × 5 readers, plus `mock.patch.object(wraps=)` | 41 | 41 failed: the stand-in ran | passed |
| p1b: explicit delegating runner (`partial`, `Mock(wraps=)`, lambda) × 3 readers | 3 | 3 failed: the stand-in ran | passed |
| p1e: adapter built under a delegating patch, patch undone, then read | 1 | failed: the stand-in ran | passed |
| p1f: production `enrollment_runner` around a default adapter | 1 | failed: the stand-in ran | passed |
| p1g: daemon re-enrols a `claude-quota-…` lane with the default adapter | 1 | failed: the stand-in ran and the lane enrolled | passed: `ProtocolError`, code 7 |
| Markers stripped inside the session, native and delegating runners | 4 | 4 failed: the stand-in ran | passed |
| P3 early refusal: 7 spellings × 2 runners | 14 | 14 failed: argv built | passed |
| P3 at launch: 8 paths × `Popen`, `os.exec`, `os.posix_spawn` (synthetic) | 24 | 24 failed: no guard | passed |
| Bare names through the call's PATH or the child's working directory | 5 | 5 failed: no guard | passed |
| Launches that must stay allowed (missing program, `true`, stand-in first) | 3 | passed | passed |
| CI's stub shape with the stand-in guarded; control showing an unguarded stand-in runs | 2 | passed | passed |
| Idempotent install; Python child guarded from import; inert outside pytest and after cleanup | 4 | 4 failed: no guard API | passed |
| Replacing the module's helpers cannot weaken the hook | 1 | not replayed (added after the replay; uses the new API) | passed |
| **Total** | **104** | **98 failed, 5 passed** of the 103 replayed | **104 passed** |

The replay copied `tests/keychain_standin.py` and the round-6 file into a
`git archive` of `5f4a5956a` and ran them with this worktree's `.venv`
(`pythonpath = ["."]` imports the archived tree; every traceback names it).
All 50 failures of the wrapper, undo, enrolment and marker probes show the
stand-in's token in the returned value. The safety net recorded no launch of the
real binary in either tree. The five cases that pass at both heads are the
controls: they show what must keep working, not a defect.

This job re-ran that replay, because the first job's evidence was unverified. It
used a `git archive` of `5f4a5956a` with the current `tests/keychain_standin.py`
and round-6 file (111 cases), under one hold with a fake HOME. Result: 106
failed and 5 passed. The 106 are the 98 above, the helper-replacement case and
the seven layer cases below; those last eight need the new API. The same five
controls passed. The stand-in's token appears in 84 failure reports, and the
safety net never tripped.

`367096c25` adds seven cases (now 111 in the file), each showing one layer of
the file match with the other switched off. The folded spelling alone matches
doubled slashes, dot segments, `..`, case and the long s. The file identity
alone matches a case variant and the `/.vol` alias. The mutation check below
found that no earlier test isolated the fold.

### CI at `dbd9c4112`: the development build and the person's state root (D-21)

The hub merged release/217 (#195, #191, #194) into the branch. CI on Python
3.12 then failed one test, with 13,078 passing: #191's
`test_the_development_build_is_the_app_only_on_a_development_state_root`,
`DID NOT RAISE ConversationError`.

**Cause.** Under D-21, a daemon on any state root but `~/.subfleet` also
accepts the development build that `SUBFLEET_DEV_APP_EXECUTABLE` names. Round 1
(`25ae27f8f`) spelled that root in `ConversationService._app_executables` as
`provider_home() / ".subfleet"`, so no test would resolve the person's live
root. Under pytest, `provider_home()` returns `SUBFLEET_PROVIDER_HOME`, and the
session points that at its own `<session>/providers`. So the production root
became `<session>/providers/.subfleet`, a path no test uses, and every test root
counted as a development root. #191's test names its person by HOME. It sets
`HOME=tmp_path` and expects `tmp_path/.subfleet` to refuse the development
build; the build was accepted. Round 1 had hidden this from the older peers
D-21 test by setting `SUBFLEET_PROVIDER_HOME` beside HOME. The provider-home
seam is the base of legacy provider homes, not the person's home. Round 5 fixed
the same confusion in workspace admission (`protected_provider_homes()`).

**Fix (`cacf61969`).**

- **The root:** `home_paths.production_state_root()` resolves `HOME/.subfleet`
  exactly as release/217 does.
- **The account's own home:** under pytest, a HOME that is the account's own
  home is refused with a `RuntimeError` naming HOME, before anything beneath it
  is touched. That covers HOME unset, the same spelling, and the same directory
  by any spelling (`..`, a symlink, case).
- **`is_account_home()`** is the check `protected_provider_homes()` already
  made, factored out. Equal spellings decide without a stat. Otherwise it
  compares the home directories themselves, never a store beneath them.
- **When it asks:** `_app_executables` asks only when
  `SUBFLEET_DEV_APP_EXECUTABLE` is set. That variable joins `SAFE_ENV_REMOVALS`,
  so a value exported where pytest starts cannot widen every test service or
  make each one ask.
- **e2e:** the harness names its stand-in after configure and gives each daemon
  a synthetic HOME, so its daemons still accept the stand-in.
- **Outside pytest:** nothing changes. The parity child now also compares
  `production_state_root()` with release/217's
  `(Path.home() / ".subfleet").resolve()` across the 21 HOME and override modes.

So both guarantees hold. D-21 holds for every test that names its person: the
development build is refused on `HOME/.subfleet` and accepted elsewhere. D-ST1
holds because the person's live root is never resolved under pytest.

**Tests.** `test_live_store_guard_state_root.py` has 22 cases. In it, `pwd`
names a stand-in account, except in the test of the real account, which refuses
every access before it happens:

- The CI defect, with both HOME and the provider seam set.
- Six spellings of a test HOME (plain, trailing slash, `.`, `..`, symlink,
  missing) name the root.
- Seven spellings of the stand-in account's home (unset, same, trailing slash,
  `.`, `..`, symlink, case) are refused. Each runs with every access beneath its
  root trapped.
- The real account (unset, same, trailing slash) is refused with the live
  `~/.subfleet` trapped.
- Without a development build, the root is never asked.
- The removal in `SAFE_ENV_REMOVALS`.
- Property, differential: over spelled HOMEs and roots (doubled slashes, `.`
  segments, `<dir>/..` detours, symlink aliases), the development build is
  refused exactly when the root is the same file as `HOME/.subfleet`.
- Property: no spelling of the stand-in account's home reaches its root.

The consumer-refusal case for `_app_executables` now requires HOME. The peers
D-21 test names its HOME first, instead of setting the provider seam.

| Tree | `test_live_store_guard_state_root.py`, `test_person_boundary.py`, `test_conversation_peers_attachments.py` |
| --- | --- |
| `dbd9c4112` export with these tests | 24 failed, 139 passed. Four fail on behaviour: #191's test, the peers D-21 test, the provider-seam case and the differential property. The other 20 need the new function or the new removal. |
| `cacf61969` | 163 passed |

**The trap (`1ac28dd82`).** The mutation check removed the account-HOME refusal
(m14). The consumer-refusal case deletes HOME and had no trap, so the mutant's
`Path.resolve()` made one metadata-only `lstat` of the real `~/.subfleet` before
the test failed. Nothing was opened, read or written, and release/217's own
peers test makes the same lookup on every run. `tests/live_store_trap.py` now
holds the trap the state-root tests use:

- **What it refuses:** any stat, lstat, readlink, listing, `os.open`, `open`, or
  directory or file change at or beneath the given roots, before the call. It
  matches any case and paths through `..`.
- **How:** with `pytest.fail`, which `except Exception` cannot swallow.
- **Where:** every consumer-refusal case runs inside it, over the live stores the
  real account entry names: `~/.subfleet`, `~/.claude`, `~/.codex`,
  `~/.claude.json`, `~/chief-of-staff/state` and
  `~/Library/Application Support/Claude`.

Rerun with the trap, m14's case fails with `Failed: touched ~/.subfleet`, raised
before the call. C code that opens a path itself, such as `sqlite3.connect`, is
outside the trap.

**Case variants: reported, not changed.** `Path.resolve()` keeps case. On a
case-insensitive volume, a state root spelled `~/.SUBFLEET` is the production
store by file identity but not by resolved string, so the D-21 comparison
accepts the development build there.

- **Minimal case:** outside pytest, with `HOME=<tmp>/home`,
  `SUBFLEET_DEV_APP_EXECUTABLE` set, and root `<tmp>/home/.SUBFLEET`, which is
  the same directory as `<tmp>/home/.subfleet`. `_app_executables()` includes
  the development build, at release/217 and at this head alike.
- **Scope:** this predates D-ST1 and is outside this PR. The differential
  property leaves case variants out and says why.
- **A fix:** compare file identity, or `folders.spelling`, in `_app_executables`,
  as `tests/frontend/test_core_client.py` already requires of the app.

### Mutation check

Each mutant removes one element of the design, in an exported tree of the
named head. Each ran under one hold, never in this worktree. The D-21 mutants
ran `test_live_store_guard_state_root.py`, `test_person_boundary.py`,
`test_conversation_peers_attachments.py` and `test_live_store_guard.py`. The
exec-guard mutants ran `test_live_store_guard_round6.py`,
`test_keychain_exec_guard_properties.py`, `test_live_store_guard.py` and
`test_live_store_guard_round5.py`. At the head, those sets pass in full. m13 and
m14's first run used `e3b82c7af` (the code of `cacf61969`). m02–m04, m08, m15,
m16 and m14's second run used `1ac28dd82`. m01's second run and m05–m07 and
m09–m12 used `367096c25`.

| Mutant | Removed | Result | First failing test |
| --- | --- | --- | --- |
| m01 | case fold in `_fold` | survived at `1ac28dd82` (442 passed); killed at `367096c25`: 2 failed | `test_the_folded_spelling_matches_without_the_file_identity` |
| m02 | file identity | 5 failed | `test_native_binary_spellings_are_refused_early[volfs-none]` |
| m03 | session pin | 5 failed | `test_stripped_markers_keep_the_session_refusal` |
| m04 | child working directory | 2 failed, 2 errors | `test_launch_target_is_found_as_the_launch_finds_it[child-cwd]` |
| m05 | `Popen`'s own `env` PATH | 4 failed, 3 errors | `test_launch_target_is_found_as_the_launch_finds_it[call-path]` |
| m06 | install in `pytest_configure` | 84 failed, 5 errors | `test_delegating_subprocess_patch_cannot_reach_keychain` |
| m07 | install at `subfleet.credentials` import | 2 failed | `test_python_child_of_the_session_is_guarded_from_import` |
| m08 | early check by file (string compare instead) | 15 failed | `test_native_binary_spellings_are_refused_early` |
| m09 | executable check in the PATH search | 1 failed | `test_a_bare_name_is_found_as_which_finds_it` |
| m10 | idempotent install | 3 failed | `test_installing_again_adds_no_second_hook` |
| m11 | `os.posix_spawn` event | 9 failed | `test_native_binary_spellings_are_refused_at_launch[...-os.posix_spawn]` |
| m12 | code-7 `AdapterError` (raises `PermissionError`) | 56 failed | `test_delegating_subprocess_patch_cannot_reach_keychain` |
| m13 | HOME as the D-21 root (provider seam instead) | 10 failed | `test_the_provider_home_seam_does_not_name_the_production_root` and #191's test |
| m14 | account-HOME refusal | 12 failed | `test_the_accounts_own_home_is_refused_before_its_root_is_touched` |
| m15 | `samefile` in `is_account_home` (spelling only) | 4 failed | `test_the_accounts_own_home_is_refused_before_its_root_is_touched[parent-segment]` |
| m16 | `SUBFLEET_DEV_APP_EXECUTABLE` removal | 1 failed | `test_an_inherited_development_build_is_removed_for_the_session` |

m01 survived because, wherever a stat succeeds, the file identity also matches
every case variant. That is a redundancy, not a gap, but no test showed the
fold, so `367096c25` tests each layer alone. In m06's run the independent safety
net in `tests/keychain_standin.py` refused 32 launches that resolved to
`/usr/bin/security`, each before the exec. It is installed when the test
modules are collected, so the real binary never ran. The m14 run before
`1ac28dd82` is the one described under "The trap" above.

### Validation

Python 3.12.14, `.venv` synced offline from the uv cache. Every pytest ran in
the foreground, one per hold of the shared lock. Each used its own
`sf-r198f6-<pid>/` TMPDIR, removed with `shutil.rmtree`; `lockf -t 60` bounded the
queue, and a foreground supervisor would interrupt pytest (SIGINT) at 470
seconds. "Fake" HOME is a directory under that TMPDIR. "Real" is the account's
HOME, as in CI. Attempts that `lockf` turned away (exit 75) are not listed; each
was retried. One collect-only hold gave the per-file counts.

| Hold | Code | HOME | Files | Result |
| ---: | --- | --- | --- | --- |
| 1 | `cacf61969` | real | `test_live_store_guard_state_root.py`, `test_person_boundary.py`, `test_conversation_peers_attachments.py` | 163 passed |
| 2 | `dbd9c4112` export with the new tests | real | same three | 24 failed, 139 passed (above) |
| 3 | `cacf61969` | real | `test_live_store_guard.py`, rounds 3–6, `test_keychain_exec_guard_properties.py` | 472 passed |
| 4 | `cacf61969` | fake | CI's `test_folder_spelling.py` (7), `test_app_cutover_daemon.py` (13), `test_policy_support.py` (14); `test_test_module_names.py` (1); `test_agent_secret_credentials.py` (7); `test_claude_adapter.py` (170) | 212 passed |
| 5 | `cacf61969` | fake | `test_cli.py` | 199 passed |
| 6 | `cacf61969` | fake | `test_conversation_service.py` (97), `test_enrollment_runner.py` (7), `test_lanes_reenroll_keychain.py` (1), `test_lanes_reenroll.py` (19), `test_lanes_enroll.py` (15) | 139 passed |
| 7 | `cacf61969` | fake | `test_salvage_unindexable.py` (86), `test_soak_tools.py` (49), `test_workspace_transient.py` (24) | 159 passed |
| 8 | `cacf61969` | fake | `test_salvage_finalization.py` (47), `test_admission_liveness.py` (32), `test_admission_priority.py` (32), `test_unadmittable_pin_admission.py` (24) | 135 passed |
| 9 | `cacf61969` | fake | e2e `test_conversations.py` (44) | 29 passed, 15 skipped |
| 10 | `cacf61969` | fake | e2e titles (9), wakes (4), codex turn completed (3), guard and isolation (5), HTTP isolation (1), legacy cockpit (1) | 23 passed |
| 11 | `cacf61969` | fake | e2e milestone 1 Codex (9), milestone 2 Claude (7), notice job state (1), recovery (2) | 19 passed |
| 12 | `cacf61969` | fake | e2e reserve (4), review r2 strand (2), review r3 strand timing (2), stop bound (3) | 11 passed |
| 13 | `cacf61969` | fake | holds 1 and 3 together | 635 passed |
| 14 | `1ac28dd82` | fake | `test_live_store_guard_state_root.py`, `test_live_store_guard.py`, `test_person_boundary.py`, `test_conversation_peers_attachments.py`, `test_test_module_names.py` | 488 passed |
| 15 | `367096c25` | fake | the seven layer cases (`-k without_the`) | 7 passed |
| 16 | `367096c25` | real | `test_live_store_guard_state_root.py` (22), `test_person_boundary.py` (97), `test_conversation_peers_attachments.py` (44), `test_live_store_guard.py` (324), round 3 (22), round 4 (8), round 5 (11), round 6 (111), `test_keychain_exec_guard_properties.py` (3), `test_test_module_names.py` (1) | 643 passed |
| 17–34 | mutant trees | fake | as in the mutation check | every mutant killed |

`test_person_boundary.py` holds the person-boundary properties, and it passed in
holds 1, 13 and 16. The 14 files are the reviewer's pass-through-wrapper sweep;
two of them, `test_claude_adapter.py` and `test_agent_secret_credentials.py`, are
in hold 4. The 15 e2e skips are the person-only requests. `as_person` and
`as_terminal` skip them inside a Subfleet job by design ("run this test outside
Subfleet (CI runs it)"): the job's guardian is an ancestor of every process a
job starts. The 29 that ran start daemons whose environment names the
development stand-in, with a synthetic HOME, so they go through the new root on
every daemon start.

### Not covered

- **Non-Python children.** A Python audit hook sees only its own process's
  launches. A shell `-c` string, `env security`, a script whose interpreter is
  `security`, or a child that never imports `subfleet.credentials` is outside
  its reach. No test does any of these, and every production path builds the
  `security` argv itself. The reviewer found the same boundary.
- **Calls that bypass the audit events**, such as `_posixsubprocess.fork_exec`
  called directly or `ctypes`, and a copy of the binary under another path.
- **The full suite and Python 3.14** were not run here.
- **The 15 person-only e2e requests**, which skip inside a Subfleet job (above),
  and `tests/frontend/test_core_live.py`, whose daemon also names a development
  build, were not run here. CI runs them.
- **Case variants of the D-21 root** (above) are reported, not changed.

### Delivery

Round 6's commits, on a detached checkout of `fix/test-live-store-guard`:

- `a5953bf28`: refuse the keychain reader by binary, not runner, under pytest
  (the first job, recovered).
- `0d1851c7a`: the guard's invariants as Hypothesis properties (the first job,
  recovered).
- `dbd9c4112`: the hub's merge of release/217 (#195, #191, #194).
- `cacf61969`: the D-21 production root is named by the test's HOME, not the
  provider seam; CI's failure fixed.
- `e3b82c7af`: this section's draft, recovered from `fix6-uncommitted.patch`,
  with the round-5 docstring correction.
- `1ac28dd82`: the live-store trap, around every consumer-refusal case.
- `367096c25`: each layer of the keychain file match, tested alone.
- The commit that adds this text.

Only named source, test and report files are staged, with the requested
co-author trailer. No logs, JSON or bundles are staged. Commits went to the
shared repository's object store on a detached HEAD, which Subfleet keeps as a
salvage ref. The caller's checkout and its branch were not written. No remote,
push, sub-agent, or detached or background process was used. The only access to
a personal store was the one `lstat` under "The trap", and nothing touched the
keychain or `/usr/bin/security`.
