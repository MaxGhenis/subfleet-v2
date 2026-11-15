# Codex callers of subfleet run (D-CODEX-CALLER1)

Implemented Codex caller detachment and namespaced session ownership while preserving marked lane jobs. All 44 new cases and 435 required checks passed; all three mutants were killed. Three timing failures in the focused parallel run passed in a one-process retry. Commits remain blocked by the sandbox.

Started 2026-11-14 21:04:20Z at adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83 on feat/codex-caller-217, initially clean. Deadline 2026-11-15 00:04:20Z; no new tests after 23:44:20Z. Sandbox Git index-lock denial prevents commits.

## Source evidence: Codex 0.159.0

GitHub Contents API reads at rust-v0.159.0 found CODEX_THREAD_ID: the common tool launcher injects it after filtering without frontend conditions, covering desktop, CLI and codex exec. It identifies the stable caller thread. Source attribution, like Claude's markers, is not authentication against deliberate spoofing/inheritance.

Quoted paths/lines below are under [codex-rs at rust-v0.159.0](https://github.com/openai/codex/tree/rust-v0.159.0/codex-rs):

- protocol/src/shell_environment.rs:6–7:
  `pub const CODEX_SESSION_ID_ENV_VAR: &str = "CODEX_SESSION_ID";`
  `pub const CODEX_THREAD_ID_ENV_VAR: &str = "CODEX_THREAD_ID";`

- core/src/unified_exec/process_manager.rs:1444–1453:

```rust
let local_policy_env = create_env(shell_environment_policy, /*thread_id*/ None);
let mut env = local_policy_env.clone();
env.insert(
    CODEX_THREAD_ID_ENV_VAR.to_string(),
    context.session.thread_id.to_string(),
);
inject_session_env(&mut env, context.session.session_id());
```

- protocol/src/shell_environment.rs:150–153:
  `env_map.insert(CODEX_THREAD_ID_ENV_VAR.to_string(), thread_id.to_string());`

- core/src/session/session.rs:59–60,644–646: `pub(crate) thread_id: ThreadId,` and `self.thread_id`. Initialization:852–860 generates a new identity or retains `(InitialHistory::Resumed(resumed_history), None) => resumed_history.conversation_id,`; lines 1766–1767 retain it in Session. protocol/src/thread_id.rs:13–18,27–31,76–79 defines its UUID/display.

- core/src/tools/handlers/unified_exec/exec_command.rs:439–445 routes both lifetimes through the manager; core/src/unified_exec/oneshot.rs:72–74 calls `manager.exec_command_inner(request, &context, Some(&mut completion))`; manager lines 503–530 reach the common launcher.

Other exports: CODEX_CI=1 (process_manager.rs:103,129–133) has no identity. CODEX_SESSION_ID is a root-scoped identity shared with descendant threads (exec_env.rs:40–49; session.rs:649–651); CODEX_VERSION reports the build version. CODEX_THREAD_ID identifies the caller thread. These are Codex exports, not native Claude's. No clone/cache/live env-value inspection.

## Changes and audit

- in_codex_session detects nonblank CODEX_THREAD_ID outside existing SUBFLEET_ATTEMPT/SUBFLEET_JOB markers. launch_mode preserves prior overrides/Claude behavior; Codex detaches with its own reason, --attach waits inline.

- session_id prefers Claude, codex:<thread>, Subfleet fallback; marked jobs preserve exact old whitespace/selection. Native Claude UUIDs cannot equal qualified Codex ids.

- Existing caller_session storage and generic callers (run/resume, --mine, wake/suggest, ack, kill audit, ping, sessions-kit attribution) inherit it; help/refusals name both. No new record keys/refusal kinds.

- Qualified conversation/runs/wake ownership resolves only Codex native bindings; raw legacy/parent ownership stays. Real CLI/transport/dispatch/store integration uses synthetic completion.

- STRIPPED_ENV gains CODEX_THREAD_ID metadata; launch_env already excludes it/CODEX_SESSION_ID/CODEX_CI, no ambient allowance. Codex injects its own identity; explicit pass-through/turn removals unchanged.

- C-17.6/C-15.8/C-24.10/C-26.3 and no-PR changelog entries amended.

Audit unchanged: in_claude_session; hooks.payload_session/Claude docs; Claude UUID generation; HEAL_ENV_REMOVE (allowlist drops Codex); sessions/cli.py's direct Claude id for native transcript selection; native canonicalizers/leases; exact-match Claude notify_push transport. Generic session_id consumers above count Codex. Codex notices use wait/runs/show.

## Invariants and current results

All 44 new tests passed in the completed focused run:

- test_codex_caller.py: none/Claude/Codex/both × marker combinations × 16 flags × 13 overrides = 3,328 combinations. Hypothesis: 300 synthetic marked environments match frozen release functions; separate property proves qualified Codex/native Claude UUID separation. Regressions cover each marker, whitespace/fallback/nonmarkers, env exclusion, refusals/help and ping.

- Ten test_codex_caller_wakes.py cases cover qualified lookup/suggestion, provider isolation, legacy raw callers, canonicalization, explicit/automatic wake and runs view.

- test_codex_caller_cli.py: real run/runs --mine/wait --mine isolate two Codex threads and same-raw-id Claude, persisting caller_session.

tests/fixtures/codex_caller_release217.py freezes session_id/launch_mode/in_claude_session. Extraction verified full base cli.py SHA-256 a635c61b8d17e073958586bdde7dbd3370773d05460a38e5a40c3c25946fb2fb; joined AST source hashes to 0a9607457ad53e54401971ac08326e6fc0a0a10c9620e1c8c66ce7c269203d02. Tests verify provenance/fixture integrity without Git, not absent original-file bytes.

.codex-caller-mutants.py makes one pristine copy per mutant: drop launch detection → test_c17_6_codex_defaults_to_detached; drop marker exclusion → test_c5_1_codex_lane_retains_release217_behavior (both markers); drop prefix → identity regression/property. It checks import origin/expected assertions/no setup errors; records digests/JUnit/commands. Preparation/AST passed; all three isolated mutants were killed by assertions, with no setup errors (details below).

Broad selected run: interrupted exit 130 after many failures, no final traceback/summary. Focused retry completed: 2,453 passed, 3 failed, 1 skipped; all 44 new Codex cases passed. The original controller and both recorded workers have exited. Final validation details follow below.

## Build command/results ledger

BASE=/private/tmp/cc-build/codex-caller-217-20261114. login:false after discovery. Static supplied OpenAI Docs skill instructions were read; no personal account state inspected.

Checkout: date -u '+%Y-%m-%dT%H:%M:%SZ', pwd, git status --short, git branch --show-current, git rev-parse HEAD, scoped rg --files: 0/clean assigned base; repeats unchanged. du -sh .venv:12M.
TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)", UV_CACHE_DIR=$TMPDIR/uv-cache; uv sync --group dev:2/shared sdists-v9/.git permission denied. Create BASE/use BASE/uv-cache; repeat:0/10 resolved,9 checked.
Tests: uv run --no-sync --with 'pytest-xdist>=3.6', fresh --basetemp, <=4 workers; later runs unset inherited marker names. BASE/build_guard.py isolates scratch/failed roots and disables only others' failed-state prune; HOME/store guards remain.
Edits via apply_patch/Python; initial compileall, nine-file AST parse, repeated git diff --check:0. git show adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83:subfleet/cli.py | shasum -a 256 matched; Python asserted hash/extracted AST fixture:0.
Safety: exclude test_claude_v1_parity.py and test_compat.py::{test_no_v1_flag_is_unaccounted_for,test_no_v1_mirror_flag_is_unaccounted_for}; they read real chief-of-staff. Disabling prune avoids others' PIDs/state. Audit found147 existing modules.
git add REPORT; git commit -m 'docs: verify Codex tool caller environment at 0.159.0':128 each. Named add implementation/contract/unit conftest/three tests/fixture; git commit -m 'feat: treat Codex tool callers as agent sessions':128 each. /Users/maxghenis/subfleet-v2/.git/worktrees/codex-caller/index.lock outside writable roots; continued per instruction. No history/remote writes.
Ownership: /usr/sbin/lsof -Fpcf -- BASE/selected-tests-2.log via subprocess.run(timeout=20):timeout/reaped/no data. lsof -w -n -P -b -a -d 1 -Fpf -- same:1/no holders. Own daemon.lock/procs.Darwin.process checked owned workers/parent UID/kernel identity/cwd, no argv/env. Wrong process_table import:1/corrected procs. interrupt_owned_pytest.py prepared/AST-valid; wrong standalone import failed, corrected identity-verified SIGINT was denied by sandbox (PermissionError); tool-session Ctrl-C ended the broad run with exit130. Later Darwin checks verified its recorded controller identity absent and both old workers absent. Own PID manifest/tail/date/scoped find/Python monitored own temps.
Delegates: runner creation/AST checks/reviews:0/no tests. Process-backed integration skipped on inspection denial; revised state_daemon transport test has synthetic completion and own guarded /tmp/sf-codex-* teardown. Report appends/static scans:0.

Ledger aliases: s/=subfleet/; c/=s/conversations/; k/=s/sessions/; u/,f/,x/,e/,v/,p/=tests/{unit,fake,fixtures,e2e,frontend,process}/. REPORT=this file; CONTRACT=docs/acceptance-contract.md. R=rg -n; L=rg -l; [xN]=repeat; S(file:ranges)=sed -n 'range' file. All0 except listed failures; truncated output prompted narrower reads.

Ledger tokens expand literally: @CS@=CLAUDE_CODE_SESSION_ID, @CT@=CODEX_THREAD_ID, @SA@=SUBFLEET_ATTEMPT, @SJ@=SUBFLEET_JOB, @SD@=SUBFLEET_RUN_DETACH, @IC@=in_claude_session, @CA@=caller_session, @SI@=session_id, @CN@=canonical_native, @LM@=launch_mode, @TCS@=test_conversation_service.py, @M@=core/src/unified_exec/process_manager.rs, @E@=core/src/tools/handlers/unified_exec/exec_command.rs, @SE@=protocol/src/shell_environment.rs, @SS@=core/src/session/session.rs.

Repository S reads:

```
s/cli.py: 100,325p, 1590,1730p, 1855,1925p, 2580,2660p, 2770,2820p, 920,970p, 2040,2090p, 2210,2250p, 135,155p, 289,330p, 1050,1125p, 1620,1645p, 1690,1775p, 3500,3610p, 1470,1625p, 3610,3655p, 3650,3674p, 3720,3732p, 135,170p, 260,350p, 1445,1472p, 1690,1778p, 2880,2910p, 3830,3885p, 900,990p
s/launch_env.py: 1,155p, 270,340p, 1,95p
s/hooks.py: 1,72p, 180,225p, 98,174p, 1,75p
k/cli.py: 275,318p, 420,452p, 640,678p
s/claude_cards.py: 525,563p, 563,630p
CONTRACT: 636,665p, 775p, 1,41p
u/test_claude_v1_parity.py: 1,180p
u/test_launch_env_allowlist.py: 1,85p
c/service.py: 960,1045p, 2705,2730p, 365,410p, 50,65p, 965,1040p
c/wakes.py: 396,423p, 240,290p, 1,55p, 122,160p, 1,220p, 395,451p, 255,292p
tests/conftest.py: 1,150p, 350,465p, 1,220p, 398,408p, 360,399p, 93,111p
u/test_compat.py: 220,290p
e/conftest.py: 1,160p
e/test_milestone1_codex.py: 1,105p
e/test_milestone2_claude.py: 1,90p
p/test_provider_shell_environment.py: 1,110p, 105,170p
p/test_codex_isolation.py: 1,100p
tests/failed_keep.py: 1,170p
c/store.py: 220,248p, 560,577p, 246,286p, 200,247p, 214,247p, 2034,2070p
u/test_conversation_wakes.py: 1,120p
u/@TCS@: 1,125p, 125,195p
k/handoff.py: 2740,2768p
s/adapters/claude.py: 1398,1423p
s/notify_push.py: 1,100p, 1,145p
u/test_codex_caller.py: 1,190p, 1,180p, 1,300p
u/conftest.py: 1,190p, 1,60p, 60,145p
f/test_codex_caller_cli.py: 1,190p, 1,130p, 1,125p, 1,195p
u/test_codex_caller_wakes.py: 1,240p
f/conftest.py: 1,180p, 254,305p, 214,222p
f/test_daemon_contract.py: 1,150p
f/test_resume_cli.py: 1,100p
f/test_turn_listing.py: 1,160p
f/run_daemon.py: 1,110p
u/test_test_module_names.py: 1,150p
f/test_state_contract.py: 1,90p, 205,265p, 15,80p
s/home_paths.py: 1,110p
s/daemon.py: 4238,4277p, 4890,4965p, 1090,1320p, 3340,3450p, 12365,12555p, 12562,12610p, 12665,12755p, 5400,5470p, 2100,2162p, 12785,12900p, 12895,12980p, 2950,3015p
u/test_daemon_connections.py: 1,160p
s/client.py: 1,240p, 295,420p, 510,650p, 650,735p
s/compat.py: 1,90p
s/store.py: 844,874p
```

Repository searches:

```
rg --files -g AGENTS.md -g '*contract*' -g '*launch_env*' -g '*cli*' -g '*invariant*' -g '*handler_refusal*'
R '@IC@|@SI@\(|@CS@|CLAUDECODE' subfleet
R 'STRIPPED_ENV|@SJ@|@SA@|Claude|C-17\.6|C-17|session' s/launch_env.py CONTRACT
R '^\- \*\*C-(15\.[238]|17\.[56]|26\.13|23\.34)' CONTRACT
L '@LM@|@IC@|@SI@\(|\-\-mine' tests
R 'conversation\.wake|def wake|def .*wake|@CA@|@SI@' s/conversations | head -100
R 'CLAUDECODE|@CS@|@LM@|@IC@|def test.*mine|\-\-mine' u/test_cli.py u/test_hooks.py f/test_notice_delivery.py u/test_offline.py f/test_turn_listing.py u/test_claude_v1_parity.py
R 'CLAUDE|STRIPPED_ENV|select_environment|CODEX_|SUBFLEET_SESSION_ID|retired|config' u/test_launch_env_round3.py u/test_launch_env_allowlist.py u/test_launch_env_policy.py
R '@SI@|by_session|@CA@|session_key' c/wakes.py c/suggestions.py c/service.py | head -150
R 'def test.*launch|@LM@|@IC@|@SD@|outside a Claude' u/test_cli.py u/test_claude_v1_parity.py u/test_review_r2_repro.py
R 'bound_session\(' subfleet u/test_wake_cli.py u/test_suggest_cli.py tests/fake
R 'def _caller|bound_session|@CN@|@CA@|@SI@' c/suggestions.py
L '@LM@|@IC@|@SI@|--mine' tests -g 'test_*.py'
R '(~/(\.subfleet|\.claude|\.codex|chief-of-staff)|Path\.home\(|pwd\.getpwuid|\.expanduser\(|SUBFLEET_LIVE|pytestmark|live)' tests/conftest.py f/conftest.py u/conftest.py u/test_cli.py u/test_hooks.py u/test_offline.py u/test_compat.py u/test_outcome_unknown.py u/test_review_r2_repro.py u/test_claude_adapter.py f/test_notice_delivery.py f/test_turn_listing.py
R '^\- \*\*C-(12\.[34]|25\.7|27\.[0-9]+|26\.3)' CONTRACT
R '_v1_option_map|^def test_' u/test_compat.py
R '^\- \*\*C-26\.7' CONTRACT
R 'wake|calling session' CONTRACT | tail -15
R '^def |^class |\.read_text\(|Path\(' v/conftest.py
R '\.subfleet|\.claude|\.codex|chief-of-staff|Path\.home|getpwuid' e/dev_app.py e/test_legacy_cockpit.py e/test_conversations.py p/test_claude_isolation.py
R '^def |completion|register|runs|bound_session|native_session' u/test_conversation_wakes.py | head -100
R 'from \.store import' c/service.py
R '^    def by_native' c/store.py
R 'def create_conversation|validate_settings' c/store.py | head -12
R 'HEAL_ENV_REMOVE' subfleet tests -g '*.py'
R 'LAUNCH_NAMES|LOCAL_TOOL_NAMES|forbidden|refus' s/provider_env.py s/launch_env.py | head -50
R '@CA@|@SI@|push|codex' s/notify_push.py | head -75
R '@SI@\(' subfleet | wc -l
rg --files --hidden -g AGENTS.md -g '!\.git'
R 'launched_by_subfleet|@SJ@|@SA@' s/hooks.py s/launch_env.py s/identity.py s/sessions s/cli.py
R '@CA@|@SI@' c/wake.py s/notices.py s/notifications.py
R 'read_text|is_file|exists|expanduser|home|pwd|Popen|subprocess|kill\(' v/conftest.py v/helpers.py e/dev_app.py
R -A 100 -B 15 'def _subfleet_launch|def in_codex_session|def @SI@|def @LM@' s/cli.py
R 'test_c17_6_codex_defaults_to_detached|test_c5_1_codex_lane_retains_release217_behavior|prefix|never.*claude|codex.*ident' tests/unit tests/fake
R 'addopts|pythonpath|testpaths' pyproject.toml
R -A 90 -B 15 'build_guard|scope|pytest_sessionfinish|pytest_runtest_teardown' tests/conftest.py
R -A 55 -B 5 'Unchanged|unchanged|C-17.6|C-5.1|C-15.8|wake|notice' REPORT
R -A 10 -B 3 'def by_native|def canonical_uuid' c/store.py
R -A 45 -B 8 'def launched_by_subfleet|@CS@|CLAUDECODE|@IC@|@SI@\(' s/hooks.py s/notify.py s/launch_env.py
R 'LAUNCH_MARKERS|@IC@|@SI@\(|@CS@|CLAUDECODE' s/cli.py s/notify s/hooks.py s/launch_env.py
R 'def .*|class |subprocess|run_cli|--mine|@CA@' f/conftest.py f/test*mine* f/test*run* f/test*wait* 2>/dev/null
R 'def |class |subprocess|run_cli|--mine|@CA@' f/conftest.py f/test_run.py f/test_wait.py f/test_runs.py tests/conftest.py
R -- '--mine|subprocess.run|PYTHONPATH|CLI' tests/fake
R 'def @SI@|def @LM@|def run\(|def runs\(|def wait\(|mine|@CA@' s/cli.py
R 'no_agent|@CS@|CLAUDECODE|@SD@|@SJ@|@SA@' tests/conftest.py u/test_test_module_names.py
R 'prompt|parent' s/cli.py | head -25
R 'def list_jobs|@CA@' s/store.py | head -25
R 'def _jobs_from_wait|def _job_id' s/cli.py
R 'codex|@CT@|STRIPPED_ENV|@SI@\(' s/cli.py tests/fixtures u/test_codex_caller.py s/launch_env.py
R -A25 -B5 'def launched_by_subfleet' s/hooks.py
R -A22 -B5 'def _list|def list\(|def wait\(' s/handlers/jobs.py
R 'def list|def wait' s/handlers* s/jobs* s/service* 2>/dev/null
R -A20 -B10 '@SA@|SAFE_HOME_PATHS' tests/conftest.py
R 'LAUNCH_MARKERS' s/hooks.py
R 'def .*list|def .*wait|"list"|"wait"' s/daemon.py | head -35
R 'def list_jobs|@CA@|jobs.*list|def wait' s/job_service.py s/job_ledger.py s/service.py s/daemon.py s/launch_env.py s/home_paths.py
R -A25 -B5 'def ping|def cmd_ping|def cmd_notices|in_codex_session' s/cli.py
R 'cli|def test.*attach|--attach|@SD@' s/hooks.py u/test_cli.py u/test_codex_caller.py
R 'def cmd_run|client =|_client\(' s/cli.py | head -35
L 'codex:|qualified|@CT@' u/test*wake* f/test*wake* s/sessions 2>/dev/null
R 'qualified|@CN@|by_native|native_thread|bound_session|@SI@' c/service.py c/store.py c/wakes.py
R 'uppercase|upper\(|canonical|parent.*turn|parent_name|_completions|raw.*caller|bound_session' u/test_conversation_wakes.py u/test_codex_caller_wakes.py u/@TCS@
R 'def .*serve|def .*handle|def .*connection|def start|def run|socket\.socket|bind\(|accept\(' s/daemon.py
R 'dispatch.*connection|_connection|_serve|_handle' f/test_state_contract.py u/test*daemon* | head -55
R 'def _notice|def _notify|def update_job|def set_job' s/daemon.py s/store.py
R 'verify_lock|proc_status|boot_id|proc_start|_ident' s/client.py s/daemon.py | head -80
R 'compat.dispatch|def dispatch|dispatch' s/compat.py s/__main__.py s/cli.py pyproject.toml
R 'verify_lock|def _.*holder|def _.*lock|proc_status\(' s/client.py
R 'caller_instance|instance =' s/cli.py s/daemon.py
R 'def .*_check|notice_mismatches|accepted_attempt' f/notice_invariant.py
```

Other read/review/static commands:

```
pwd [x2]
python3 -c 'print("audit-running")'
git diff --check -- c/store.py c/service.py c/wakes.py u/test_codex_caller_wakes.py CONTRACT
git diff --stat -- c/store.py c/service.py c/wakes.py u/test_codex_caller_wakes.py CONTRACT
git diff -- c/store.py c/service.py c/wakes.py
python3 -c 'import ast; from pathlib import Path; ast.parse(Path(".codex-caller-mutants.py").read_text()); print("mutation runner syntax: valid")'
cat x/codex_caller_release217.py [x2]
git diff adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83 -- s/cli.py s/notify.py s/launch_env.py s/hooks.py
git diff adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83 -- c/store.py c/service.py c/wakes.py
git diff --stat adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83 -- subfleet
git diff --check -- f/test_codex_caller_cli.py [x2]
git diff -- s/cli.py s/launch_env.py tests/conftest.py
git diff --stat
git diff -- k/wake.py k/cli.py k/discovery.py k/models.py k/store.py
git diff -- c/service.py c/store.py c/wakes.py
cat u/test_codex_caller_wakes.py
tail -100 REPORT
```

Exceptions: AGENTS.md rg --files: 1/absent; build_guard|scope|pytest_sessionfinish|pytest_runtest_teardown in tests/conftest.py: 1; no_agent/Claude/job-marker search in tests/conftest.py + u/test_test_module_names.py: 1. Guessed-path rg errors (2; absent zsh globs refused before rg): s/identity.py, c/wake.py, s/notices.py, s/notifications.py, v/helpers.py, s/notify.py, s/notify, f/test_run.py, f/test_wait.py, f/test_runs.py, s/handlers/jobs.py, s/handlers*, s/jobs*, s/service*, s/job_service.py, s/job_ledger.py, f/test*mine*. Root also corrected absent s/resource_identity.py, tests/suite_lock.py, p/conftest.py, run/wait/runs guesses and .env*/.pytest* globs; no files created.
Python scans: safety regex over test_*.py ran twice (first completion uncollected; repeat 0/147 modules). Contract scan for wake/conversation.wake: 0, C-24.10/C-25.2/C-25.7. Synthetic UUID/name/C-24.10 refinements, four-file AST parse and report appends: 0.
Source API loops are consolidated by exact path/outcome/count; post-fetch rg/nl/sed/head inspections selected the quoted symbols/lines above. Source macros: GET(path)=gh api 'repos/openai/codex/contents/codex-rs/<path>?ref=rust-v0.159.0' --jq '.content' | base64 --decode; LIST(path)=same URL with --jq '.[].name'. No clone/cache. ls REPORT before creation: 1; apply_patch created it.

```
GET(core/src/exec.rs) 0 [x1]
GET(core/src/exec_env.rs) 0 [x2]
GET(@SE@) 0 [x1]
GET(core/src/tools/runtimes/shell.rs) 404; decode warning; pipeline 0 [x1]
GET(core/src/tools/runtimes/unified_exec.rs) 0 [x1]
LIST(core/src/tools/handlers) 0 [x1]
LIST(core/src/tools/runtimes) 0 [x1]
LIST(core/src/session) 0 [x1]
GET(core/src/tools/handlers/unified_exec.rs) 1 no matches [x1], 0 [x1]
GET(@SS@) 0 [x1]
GET(core/src/session/startup.rs) 1 no matches [x1]
LIST(core/src/tools/handlers/unified_exec) 0 [x1]
LIST(core/src/unified_exec) 0 [x1]
GET(@E@) 0 [x3]
GET(@M@) 0 [x4]
GET(protocol/src/thread_id.rs) 0 [x1]
GET(core/src/tools/handlers/shell_spec.rs) 0 [x1]
GET(core/src/unified_exec/process_manager_tests.rs) 0 [x1]
GET(core/src/unified_exec/oneshot.rs) 0 [x1]
GET(core/src/tools/registry.rs) 0 [x1]
LIST(core/src/tools) 0 [x1]
GET(core/src/tools/router.rs) 1 no matches [x1]
```

Later triage, all 0: S(c/store.py:575,617p); S(u/test_codex_caller_wakes.py:1,180p); S(u/test_conversation_service.py:125,160p); R 'class WakeEngine|def __init__|automatic-since|def add_job' c/wakes.py s/store.py; S(c/wakes.py:176,224p). No shared fixture fault found; no edits/tests.

Compaction: Python read REPORT bytes/headings; S(REPORT:1,235p;235,540p;540,840p); Python extracted/deduplicated command blocks/counts and rewrote only REPORT. Reads/extraction passed; one JS formatting parse failed before reaching shell. Helper bodies are described by path/action.


## Final validation ledger

All following pytest commands used `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=BASE/uv-cache`, `PYTHONPATH=BASE:.`, `PYTHONUNBUFFERED=1`; inherited CODEX_THREAD_ID/CODEX_SESSION_ID/CODEX_CI names were unset without reading values. `P` means `uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p build_guard`. Each basetemp was fresh.

1. `P -n 4 --basetemp=BASE/new-tests-1 tests/unit/test_codex_caller.py tests/fake/test_codex_caller_cli.py -q`: 33 passed, 1 skipped in75.02s (exit0). The initial process-backed integration skipped because native inspection was unavailable; it was replaced by the production-socket-handler fixture before the next completed run.
2. Broad selector `rg -l 'launch_mode|in_claude_session|session_id|--mine' tests -g 'test_*.py' | sort` wrote BASE/selected-tests.txt (150 files). `uv run --no-sync --with 'pytest-xdist>=3.6' python BASE/run_selected.py` invoked P with `-n4 --basetemp=BASE/selected-tests-2 --timeout=180 --ignore=tests/unit/test_claude_v1_parity.py -k 'not test_no_v1_flag_is_unaccounted_for and not test_no_v1_mirror_flag_is_unaccounted_for' -q -rs` and selected files. Interrupted by tool Ctrl-C at57%, exit130, numerous F/E markers and no final traceback/summary. Bare session_id had included many unrelated data-field references. No pass claim for this run.
3. Corrected selector `rg -l 'launch_mode|in_claude_session|session_id\(|--mine' tests -g 'test_*.py'`: P `-n 4 --basetemp=/private/tmp/cc-build/c17-n3 --ignore=tests/unit/test_claude_v1_parity.py -k 'not test_no_v1_flag_is_unaccounted_for and not test_no_v1_mirror_flag_is_unaccounted_for' -q -rs` over fake/{test_codex_caller_cli,test_notice_delivery,test_turn_listing}.py and unit/{test_claude_adapter,test_cli,test_codex_caller,test_codex_caller_wakes,test_compat,test_hooks,test_offline,test_outcome_unknown,test_review_r2_repro,test_sessions_handoff}.py. Exit1: 2,453 passed, 3 failed, 1 skipped in1147.80s. All 44 new cases passed, including real CLI run/runs --mine/wait --mine. Failures: test_cli.py::test_ping_open_pipe_is_bounded[empty] measured1.05892s against0.75s; test_sessions_handoff.py::test_scrubber_work_scales_linearly_for_long_ordinary_identifiers[a.] and[a-] each hit their subprocess60s timeout. These failures exercise unchanged timing/scrubber paths; no baseline comparison was run. test_hooks.py:341 skipped native ps inspection after its hook output/mark assertions passed.

Additional command results: repeated `tail -N BASE/{focused-3,selected-tests-2,gates-4}.log`, `date -u`, traceback `rg`, `wc -c`:0 except no-match rg1. `rg --files BASE/failed-state`:2 (absent). Scoped own-worker/controller Darwin/Python checks:0, exited identities confirmed; first mistaken subfleet.process_table import:1, corrected subfleet.procs. `sed` reads of the interrupt helper, mutant runner, new CLI/wake tests, test fixtures, hooks/CLI/compat/client/daemon, notice invariant, and invariant-index/name tests:0; guessed tests/invariants_index.py:1 absent. Targeted Claude/refusal/launch marker/STRIPPED_ENV searches and `git status --short`, `git diff --stat`, `git diff --check`:0. One broad contract rg output was truncated; no live caller environment values were inspected or recorded. Tests never wrote a new event/record key, so no new-key exact-comparison audit was needed.

Independent second review commands: `git diff BASE_COMMIT -- subfleet/cli.py subfleet/conversations/{store,service,wakes}.py`; narrowed `rg -n -A35 -B10` for caller identity, binding, completions and launch markers; `sed` wakes395:445/service2720:2745/cli275:338; caller/binding searches across conversations, sessions/cli and notify_push:0, no actionable issue. No delegated tests. Report compaction/read/deduplication commands passed after four size-guard exits1 and one JS parse error before shell; formatting inserted12 blank lines and final diff-check passed.


4. `P -n 4 --basetemp=/private/tmp/cc-build/c17-g4 -q --tb=short tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py`: exit0, 435 passed in251.59s. This includes the required refusal checks after the new CLI refusal wording.


## Per-use Claude/session audit

The final four-pattern search found36 matches; line numbers are current source. All CLI/session-kit attribution consumers now inherit Codex identity centrally.

| Path:lines | Decision |
|---|---|
| cli.py:140,143,149 | session_id qualifies Codex, prefers Claude; marked jobs retain exact legacy expression. |
| cli.py:165,211 | wake and suggestion creator/origin use qualified caller. |
| cli.py:287,290,292,295,296 | in_claude_session definition/docs/body stay specifically Claude. |
| cli.py:327 | Existing Claude launch branch stays; following Codex branch is separate. |
| cli.py:974,2258 | Run and resume submission record qualified caller. |
| cli.py:1653,1656,1727,1730 | wait/runs --mine resolve Codex; refusals name both providers. |
| cli.py:1922,2100,2646 | Notice acknowledgement, cancellation attribution, ping inbox count Codex. |
| cli.py:2813–2814 | Existing Claude stripping retained; CODEX_THREAD_ID added. |
| sessions/cli.py:306,439,660 | Nudge/cold-handoff/handoff caller attribution counts Codex. |
| sessions/cli.py:664 | Native Claude handoff source selection stays Claude-only. |
| sessions/handoff.py:2710,2740,2747,2755 | Native Claude UUID validation and explicit/current/discovered transcript selection stay unchanged. |
| conversations/service.py:2039 | Provider-specific native handoff canonicalization stays; Codex already has a branch. |
| hooks.py:35,199 | Claude hook documentation/environment fallback stays; no Codex hook protocol exists here. |
| adapters/claude.py:1415 | Native Claude UUID generation stays. |
| claude_cards.py:549,550 | Claude heal exclusions stay; launch allowlist excludes Codex identity already. |
| launch_env.py:51 | Comment explains ambient exclusion of caller identity. |

Additional changed help/reason/default sites: cli.py:4,146,161,2643,333–334,3662,3687. Provider-qualified binding, runs and explicit/automatic wake discovery changed as described above. notify_push.py stays Claude-only with exact socket-session matching. Audit commands: `rg -n 'in_claude_session|session_id\(|CLAUDE_CODE_SESSION_ID|CLAUDECODE' subfleet`; `rg -n 'CODEX_THREAD_ID|inside a Codex|Claude or Codex|default.*Claude|this session.*jobs|the Claude or Codex' subfleet/cli.py subfleet/launch_env.py`; `sed -n '2705,2765p' subfleet/sessions/handoff.py`: all exit0.


5. `uv run --no-sync --with 'pytest-xdist>=3.6' python .codex-caller-mutants.py`: exit0. The runner made three independent subfleet copies, asserted their original cli.py digests match, proved the imported cli.__file__ points into each copy, and ran `python -c BOOTSTRAP COPY BASE WORKSPACE -p build_guard -o pythonpath=COPY -n 0 --basetemp=BASE/mutants/NAME/pytest-temp --junitxml=BASE/mutants/NAME/junit.xml -q tests/unit/test_codex_caller.py::TEST`. Each mutant had exactly its expected assertion failures and zero setup errors:

| Isolated mutation | Killing test | Result |
|---|---|---|
| Drop Codex launch branch | test_c17_6_codex_defaults_to_detached | pytest exit1; 1 assertion failure |
| Drop shared lane exclusion | test_c5_1_codex_lane_retains_release217_behavior | pytest exit1; 2 assertion failures, SUBFLEET_JOB and SUBFLEET_ATTEMPT |
| Drop codex: prefix | test_c17_6_codex_identity_cannot_equal_a_native_claude_identity | pytest exit1; 1 assertion failure |

The JUnit verifier required these exact case names/counts and AssertionError evidence. No working production source was mutated. `rg --files BASE/mutants` for log/result names and log tails passed; Python digest/result verification passed. Report append/replace Python commands passed.


The three pristine mutant source digests were identical: 26174905a5829c91013061ae9a5c446d95bfd3d9ea664b7a4d628696d2362713. Killing test durations were18.69s/16.96s/32.20s respectively.

6. Follow-up for the three focused-run failures: `P -n 0 --basetemp=/private/tmp/cc-build/c17-r5 -q --tb=short 'tests/unit/test_cli.py::test_ping_open_pipe_is_bounded[]' 'tests/unit/test_sessions_handoff.py::test_scrubber_work_scales_linearly_for_long_ordinary_identifiers[a.]' 'tests/unit/test_sessions_handoff.py::test_scrubber_work_scales_linearly_for_long_ordinary_identifiers[a-]'`: exit0, 3 passed in72.21s. No source/test change preceded the retry. It ran after the required checks and mutants, before23:44:20Z; no more test runs were started.

Not run: the full suite (as instructed); the real-home-reading parity module and two compatibility inventory tests (forbidden by live-state rules); a completed result for the oversized broad selector; native ps assertions skipped by the existing hook test; provider/admission/guardian survival in the synthetic-completion CLI integration. The completed focused selection covered every safe file matching function calls/--mine plus all new tests. The initially skipped process-backed integration was replaced and then passed via real CLI/socket/daemon dispatch/store.


## Cleanup and final review

All test processes had returned before cleanup; the interrupted broad controller and its recorded workers were separately confirmed exited. Guarded Python `shutil.rmtree` required each exact direct child of /private/tmp/cc-build to resolve to itself and not be a symlink. It removed each folder and all descendants:

- /private/tmp/cc-build/codex-caller-217-20261114 (uv cache, scratch, isolated state, logs, owned-process manifests and all three mutation copies/basetemps)
- /private/tmp/cc-build/c17-n3
- /private/tmp/cc-build/c17-g4
- /private/tmp/cc-build/c17-r5

The empty /private/tmp/cc-build parent was removed with rmdir. The guarded workspace-only .codex-caller-mutants.py helper was unlinked. Cleanup exited0. Fixture finalizers removed their short /tmp/sf-* roots; the new /tmp/sf-codex-* fixture uses guarded rmtree. Failed-state retention was redirected into BASE, so this build left no /tmp/sf-failed/<pid> tree requiring separate deletion. No live state or unowned process was inspected/changed.

Final read-only `git diff --check`, branch/head checks, `git diff --numstat`, `git status --short`, report pending-token/size scans, `cat REPORT` and own-root existence checks passed. The branch remained feat/codex-caller-217 and HEAD remained the starting commit. Report read/append/format edits passed. clock reads and bounded15/20/30-second tool sleeps were used while awaiting owned runs/cleanup; no background work was started. No test was started after23:31:09Z.


Final commit attempt: `git add -- docs/acceptance-contract.md docs/reports/2026-11-14-codex-caller.md subfleet/cli.py subfleet/conversations/service.py subfleet/conversations/store.py subfleet/conversations/wakes.py subfleet/launch_env.py tests/unit/conftest.py tests/fake/test_codex_caller_cli.py tests/fixtures/codex_caller_release217.py tests/unit/test_codex_caller.py tests/unit/test_codex_caller_wakes.py` and `git commit -m 'feat: treat Codex sessions as Subfleet callers'` both exited128: unable to create /Users/maxghenis/subfleet-v2/.git/worktrees/codex-caller/index.lock, Operation not permitted. No commits were created; all12 deliverable files remain as worktree changes. No history was rewritten and nothing was pushed. This is a filesystem sandbox denial, not an approval request. Final branch/head assertions passed. Work and report completed within the three-hour limit.

Head built: adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83
