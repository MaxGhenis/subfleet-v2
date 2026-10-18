# Lane launch environment allowlist (D-ENV1)

Base: `release/217`, `3f50142ac1cc`. Reported on 2026-10-14 by the d1063 key-rotation session and verified by the hub. A shell-started daemon retained unrelated exported credentials, and provider children inherited them. This change selects names before constructing child environments. No credential store, secret-loader script, live store, live daemon or live process was inspected or changed. All test credentials are synthetic; no environment values are included in this report.

## Original exact-name allowlist

`subfleet/launch_env.py` owns the default selection. The table records round 1's finite set and its rationale. Round 2 additionally admitted noncredential `SUBFLEET_*` configuration by prefix. Round 3 replaces that with exact names, the harness's own prefixes and a substring credential-word backstop, and gives each env-kind lane credential to its own lane only (see "Round 3" at the end). Unknown `LC_*` names remain excluded.

| Name | Reason |
| --- | --- |
| HOME | Observed; provider settings, Git configuration and keyring lookup. |
| LOGNAME | Observed; account identity expected by Unix tools. |
| MKL_NUM_THREADS | Observed; numerical subprocess thread control. |
| NUMEXPR_NUM_THREADS | Observed; numerical subprocess thread control. |
| OMP_NUM_THREADS | Observed; numerical subprocess thread control. |
| OMP_WAIT_POLICY | Observed; OpenMP worker scheduling. |
| OPENBLAS_NUM_THREADS | Observed; numerical subprocess thread control. |
| OSLogRateLimit | Observed; macOS logging control. |
| PATH | Observed; provider, Git, gh and hook executable lookup. |
| PYTEST_XDIST_AUTO_NUM_WORKERS | Observed; child test worker budgeting. |
| SHELL | Observed; provider shell selection. |
| SSH_AUTH_SOCK | Observed; SSH agent socket path for Git authentication. |
| SSL_CERT_FILE | Observed; operator certificate bundle selection (C-10.8). |
| SUBFLEET_HOME | Observed; Subfleet state-root selection. |
| SUBFLEET_RETENTION_DORMANT | Observed; daemon retention configuration. |
| TMPDIR | Observed; child scratch storage. |
| USER | Observed; account identity expected by Unix tools. |
| VECLIB_MAXIMUM_THREADS | Observed; numerical subprocess thread control. |
| XPC_FLAGS | Observed; launchd/macOS process context. |
| XPC_SERVICE_NAME | Observed; launchd/macOS process context. |
| LANG | Addition; provider and shell text encoding and locale. |
| LC_ALL | Addition; complete locale override, also explicitly set by Git helpers. |
| LC_COLLATE | Addition; shell and tool sorting locale. |
| LC_CTYPE | Addition; Unicode/text classification and Python encoding. |
| LC_MESSAGES | Addition; tool diagnostic locale. |
| LC_MONETARY | Addition; locale-aware formatting. |
| LC_NUMERIC | Addition; locale-aware numeric formatting. |
| LC_TIME | Addition; locale-aware time formatting. |
| TERM | Addition; terminal capability detection for provider/tool output. |
| TZ | Addition; consistent provider/tool timezone; catalog sets its own override. |
| __CF_USER_TEXT_ENCODING | Addition; macOS CoreFoundation text encoding. |
| SSL_CERT_DIR | Addition; the second operator CA source supported by C-10.8. |
| HTTP_PROXY | Addition; explicit HTTP network routing for provider/tools. |
| HTTPS_PROXY | Addition; explicit HTTPS network routing for provider/tools. |
| ALL_PROXY | Addition; general proxy routing for provider/tools. |
| NO_PROXY | Addition; proxy bypass selection for provider/tools. |
| http_proxy | Addition; lowercase HTTP proxy spelling used by tools. |
| https_proxy | Addition; lowercase HTTPS proxy spelling used by tools. |
| all_proxy | Addition; lowercase general proxy spelling used by tools. |
| no_proxy | Addition; lowercase proxy bypass spelling used by tools. |
| SUBFLEET_CLAUDE_DIR | Addition; transcript and notification source directory. |
| SUBFLEET_CLAUDE_SETTINGS | Addition; hooks' explicit settings destination. |
| SUBFLEET_CODEX_GUARD_CACHE | Addition; guard preflight cache directory. |
| SUBFLEET_CODEX_HOMES | Addition; lane-home discovery configuration. |
| SUBFLEET_CODEX_UNIFIED_EXEC | Addition; detached and turn shell-tool safety switch. |
| SUBFLEET_DESKTOP_LOG | Addition; configured desktop log path for session helpers. |
| SUBFLEET_DEV_APP_EXECUTABLE | Addition; daemon development-app person boundary (D-PB1). |
| SUBFLEET_GIT_TIMEOUT_S | Addition; salvage/finalization Git deadline. |
| SUBFLEET_GUARD_TRUST | Addition; guard trust-file selection. |
| SUBFLEET_HOOK_COMMAND | Addition; installed hook command selection. |
| SUBFLEET_HOOK_TIMEOUT_S | Addition; hook deadline configuration. |
| SUBFLEET_NOTIFY_MODE | Addition; notification delivery configuration. |
| SUBFLEET_PROVIDER_QOS | Addition; guardian's provider QoS clamp. |
| SUBFLEET_RESET_INHIBIT | Addition; reset-credit inhibition marker path. |
| SUBFLEET_RETENTION_QOS | Addition; retention helper QoS clamp. |
| SUBFLEET_SESSION_STORE | Addition; mirror/revive session-store path. |
| SUBFLEET_TICKLE | Addition; restart-helper enablement. |
| CODEX_GUARD_PREFLIGHT_TIMEOUT | Addition; existing guard preflight deadline configuration. |

## Invariants and policy

1. Parent values are selected by the exact defaults, the noncredential Subfleet configuration rule, or exact policy `env.pass` names. Selection never mutates the parent mapping. Presence, including an empty value, is retained.
2. Each launch supplies its own credential/configuration directory, OAuth credential when applicable, attempt/session/hook markers and push-bundle path separately. Those explicit additions override selected parent values. The guardian imports Subfleet from its package working directory; it does not rewrite provider Python paths.
3. `env.pass` is optional and must be a list of ASCII variable names matching `[A-Za-z_][A-Za-z0-9_]*`. No glob, whitespace, assignment or Unicode identifier is accepted. A policy name authorizes only that name, including when it has a secret suffix. Mandatory launch removals apply last, including to selected policy additions, so pass-through cannot re-enable memory sources or other sandbox exclusions. Launch-owned additions override parent values subject to those removals. Managed-setting rejection for isolated reviews remains in effect.
4. `daemon start` selects with the same builder and reads `env.pass` from the selected root's policy before spawning. It replaces the state-root variable with its own root. The install plist's explicit `EnvironmentVariables` are retained.
5. Every guardian provider exec selects again. The daemon authorizes its already-selected launch-owned and policy names using repeated `--env-pass NAME` arguments. No credential value is placed in argv or launch records. A directly invoked guardian has only the default allowlist unless names are deliberately provided.
6. `daemon.status.environment.extra_names` contains sorted names outside the default allowlist, never values. `doctor` asks the running daemon for this diagnostic even without `--live`; it warns on extras, passes on an empty list and reports unknown for an unavailable or older daemon. Explicitly passed names outside the default set are still reported for review.
7. Local Git/catalog/notification helpers use the default selection, the owning operation/worker's exact policy names, and their own explicit variables; provider metadata and heal paths receive the provider's explicit policy selection. Scoped contexts reset on return and exception, and workers establish their own policy rather than depending on thread inheritance. Git location, pathspec, and trace removals remain mandatory. CA selection remains the shared C-10.8 behavior.

## Spawn audit

| Boundary | Environment construction |
| --- | --- |
| `cli.cmd_daemon_start` → daemon | `daemon_env`: shared allowlist, policy names, own root. |
| `Daemon._launch` → guardian | Shared provider selection; covers fresh detached jobs, resumes/revives, conversation turns and gate round jobs, including isolated reviews. Own attempt/root/job and push variables added separately. |
| `Daemon._execute_probe` → guardian | Same provider selection; admission, timer keepalive and other supervised probe turns. |
| `Daemon._enrollment_turn` → guardian | Selected adapter environment and own enrollment/probe markers. |
| `guardian.run_guardian`, stdin branch → provider | Independent selection with explicitly authorized names. |
| `guardian.run_guardian`, relay branch → provider | Same selection for conversation turns. |
| `ProviderEnvironment.build/assemble`, recovery | Shared selection for recovered launch records, classification/attestation, guard preflight and relaunch credential additions; no recovered parent environment snapshot is trusted. |
| `CodexAdapter._launch`, isolated inspection | Selected parent plus explicit credential environment, used for the inspection/refusal boundary. |
| `ClaudeAdapter.build_launch/resume_launch`, isolated/read-only inspection | Parent/credential names only for managed-settings refusal and memory exclusions; values are not copied for these checks. |
| `ClaudeAdapter._config_projects_dir` → transcript lookup | Uses the selected launch environment so a discarded ambient config directory cannot redirect recovery/finalization reads. |
| `conversations.launch.claude_launch` | Parent/credential names only for read-only turn exclusions. Codex and Claude turn records are executed through `_launch` above. |
| `ClaudeAdapter._run_turn` → provider | Shared selection for standalone enrollment, direct model probes and keepalive fallback. |
| `isolation.inspect_codex` → app-server | Shared selection plus own home; metadata only. |
| `isolation.inspect_codex` → MCP inventory | Same selected environment. |
| `guard.preflight._version` → Codex | Selected environment, preserving explicitly authorized policy names. |
| `guard.preflight._hooks_list` → app-server | Same selection plus scratch/lane home. |
| `claude_cards.heal_turn` → Claude | Shared provider selection plus own config directory. |
| `claude_cards.cli_version` → Claude | Shared provider selection; timer sensor supplies policy. |
| `doctor._run_full` → provider/tool checks | Default allowlist or an explicitly assembled provider environment. |
| `credentials.resolve_credential` → configured credential helper | Default allowlist; credential lookup behavior unchanged, no lookup performed during this task. |
| `ClaudeAdapter._resolve_keychain_token` / `_plan_from_keychain` / `_keychain_blob` → configured credential helper | Default allowlist; only test stand-ins exercised. |
| `salvage._git/_git_bytes/_add_all/_seeded/_record` → Git | `_git_env` starts from the shared allowlist; seeded/fallback indexes add their own index path. Existing Git location, trace and pathspec controls remain. This covers daemon workspace setup, salvage and finalization helpers and conversation diff. |
| `retention_git.run`, cat-file and archive batch helpers → Git | Shared allowlist plus retention's own Git/askpass controls. |
| `retention_holders.lsof_holders` → holder listing | Shared allowlist; host listings are replaced in tests. |
| `gate.merge.run_command` → Git/gh | Shared allowlist; gate provider rounds use `_launch`. |
| `conversations.service` workspace preparation → Git | Shared allowlist. |
| `sessions.handoff._run_git` → Git | Shared allowlist. |
| `conversations.wakes` → gh metadata helper | Shared allowlist. |
| `conversations.catalog.spawn_refresh` → catalog worker | Shared allowlist plus its own Python import path. |
| `conversations.catalog` → ps metadata checks | Shared allowlist and its own timezone override where required. |
| `sessions.nudge.spawn` → restart worker | Shared allowlist plus explicit root and package import path. |
| `cli.cmd_daemon_install` → launchctl | Shared allowlist; plist explicit environment unchanged. |
| `descriptors.kernel_open_files_ceiling` → sysctl | Shared allowlist for the install/doctor descriptor helper. |
| `compat.delegate` → historical front door | Shared allowlist if called directly; native compatibility mappings no longer delegate. |

Existing process-identity/census and host-push helpers already use their own restricted environments and do not inherit the daemon's full environment. The manually invoked `tools/login_table.status` provider helper already supplies only its own PATH, HOME and config directory; it inherits no ambient environment. The Codex shim execs its provider with the selected lane environment.

## Validation

The initial post-change run exposed two CLI test cases that omitted their temporary state-root environment variable. The sandbox rejected the fallback root write before any log opened; the daemon client was stubbed. The tests now set the temporary root explicitly. Other initial failures were an overbroad process spy (it recorded Git as well as guardians) and a mutation needing an import in a module that did not use `os`. Both harness issues were corrected.

The base launch paths failed the initial regression run: 45 failed, 4 passed (49 cases). All six fake-provider regressions failed on the base behavior. After the harness corrections, all 118 new tests passed, including 34 in-memory mutations killed by their named regressions. The first post-change run had 107 passes and seven harness failures; the corrected run had no failures. The adapter/guard group passed 2,117 tests; its one failing optional descriptor test requires a real `sysctl` kernel-ceiling read that this sandbox denies. The helper deliberately returns unknown on this denial. This was reproduced with no kernel value printed. All provider and environment tests in that group passed. The CLI/policy/helper group initially passed 753 with eight failures: one doctor test still expected inherited environment, and seven existing process-identity assertions cannot pass when the sandbox denies both boot and own-process inspection. The doctor assertion was updated to require the allowlist; its complete file then passed all 16 cases. The conversation/routing/state group passed 416 and skipped nine real-process cases through the existing identity-permission fixture. Both permission denials were reproduced by name/status only, with no identity or environment values printed.

Latest results across all 43 files: **3,405 passed, 8 environment-blocked failures, 9 skipped**. The eight remaining failures are the existing real-kernel descriptor assertion, five daemon-verbs integration assertions and two doctor lock assertions. The helper implementations deliberately degrade to unknown/refused without inspection authority. All new tests, provider adapters, policy, CLI, conversation launches and state-contract assertions passed.

| File | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| `tests/fake/test_codex_ca_bundle_fake.py` | 0 | 0 | 6 |
| `tests/fake/test_gate_end_to_end.py` | 10 | 0 | 1 |
| `tests/fake/test_launch_env_daemon.py` | 5 | 0 | 0 |
| `tests/fake/test_launch_env_providers.py` | 6 | 0 | 0 |
| `tests/fake/test_policy_sandbox.py` | 25 | 0 | 0 |
| `tests/fake/test_routing_end_to_end.py` | 14 | 0 | 2 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| `tests/unit/test_claude_adapter.py` | 170 | 0 | 0 |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 |
| `tests/unit/test_claude_turn.py` | 61 | 0 | 0 |
| `tests/unit/test_cli.py` | 199 | 0 | 0 |
| `tests/unit/test_codex_adapter.py` | 93 | 0 | 0 |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 0 | 0 |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 |
| `tests/unit/test_codex_turn.py` | 58 | 0 | 0 |
| `tests/unit/test_compat.py` | 1615 | 0 | 0 |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 |
| `tests/unit/test_conversation_service.py` | 97 | 0 | 0 |
| `tests/unit/test_daemon_verbs.py` | 15 | 5 | 0 |
| `tests/unit/test_descriptors.py` | 15 | 1 | 0 |
| `tests/unit/test_doctor.py` | 42 | 2 | 0 |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 |
| `tests/unit/test_gate_admission.py` | 20 | 0 | 0 |
| `tests/unit/test_gate_merge.py` | 59 | 0 | 0 |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 |
| `tests/unit/test_guard_trust.py` | 98 | 0 | 0 |
| `tests/unit/test_guardian.py` | 10 | 0 | 0 |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 0 | 0 |
| `tests/unit/test_launch_env_helpers.py` | 20 | 0 | 0 |
| `tests/unit/test_launch_env_mutations.py` | 34 | 0 | 0 |
| `tests/unit/test_launch_env_policy.py` | 26 | 0 | 0 |
| `tests/unit/test_policy.py` | 178 | 0 | 0 |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 |
| `tests/unit/test_salvage.py` | 31 | 0 | 0 |
| `tests/unit/test_sessions_handoff.py` | 112 | 0 | 0 |
| `tests/unit/test_sessions_nudge.py` | 44 | 0 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |

Each pytest process receives a unique scratch directory and Hypothesis database; the directory is removed after the hold. New properties use bounded deterministic settings (30 lane examples, 20 helper examples); no deep profile is used.

The fake-provider regressions exercise both providers' detached, turn and probe launch environments and a subprocess tool, emitting only a fixed success marker. The daemon tests intercept its real spawn sites. In-memory mutations replace individual subprocess environments with ambient inheritance and must be rejected by the corresponding named regression. No mutation writes a source file or starts another pytest process.

## Remaining validation

Real-process coverage requires a host that permits the existing boot and process identity checks. No ownership bypass was added to make those tests pass. The nine skips comprise six CA-bundle daemon cases, two routing cases and one gate case. Round 1 did not run the end-to-end, frontend or remaining process suites. The round-2 results below supersede that incomplete validation; the end-to-end harness uses synthetic credentials.

## Commits and handoff

The shared Git index is outside the sandbox's writable roots and rejected a checkout write. Commits therefore use the existing ignored `.git-local/` fallback, on `fix/launch-env-allowlist-217`, with the required co-author trailer. The local object store uses the base repository's objects as a read-only alternate. The hub can read the resulting commit chain with `git --git-dir=.git-local log` and import it from this workspace. Nothing was pushed and no history was rewritten.

Commit steps before this report:

- `0ff449f96`: initial launch/policy regressions, tests first.
- `5847de7a1`: helper and daemon spawn regressions.
- `7fa3d578e`: allowlist, policy, provider/guardian paths, diagnostics and contract.
- `bb4828879`: guard preflight child selection.
- `b882a1d95`: doctor environment expectation, all 16 cases passed.
- `fa5ec1cb9`: complete audit and validation report.
- The final documentation step repairs the test-table paragraph boundary. An ignored delivery bundle contains only the job commits, with the base commit as its prerequisite.


## Round 2: CI failure and review r1, resumed after cold boot

Restart baseline: `9231bd8486af`, preserving all 11 recovered commits after
`1a2287ca3`. The saved cold-boot patch contained only this report's invariant
edits and the acceptance-contract edits; it applied cleanly. The implementation
for configuration inheritance, mandatory removals, helper policy and guardian
bootstrap separation had not been committed before the restart. Those steps
are now complete. The recovered gate regression also needed a fake Git diff
response, the daemon-start regression needed its temporary `SUBFLEET_HOME`, and
the spawn oracle needed to distinguish the guardian-only settle setting.

### Cause and correction

- The exact-name list discarded noncredential `SUBFLEET_*` configuration,
  including fake-provider scenarios, reports, delays, and the live-test guard.
  Round 2 admits valid ASCII `SUBFLEET_*` names except launch/session markers
  and names containing an underscore-delimited credential word, case insensitive: the
  required `TOKEN`, `KEY`, `SECRET`, `PASSWORD` forms plus `APIKEY`, `PASSWD`,
  `CREDENTIAL`, `AUTHORIZATION`, `BEARER`, and `COOKIE`, including plurals and
  numbered variants. Unknown credential names still need exact
  policy authority. Tests cover these credential forms, mixed case, suffixes such as
  `_TOKEN_FILE`, and `SUBFLEET_TEST_TOKEN` across daemon/provider selection.
  The prefix rule accommodates supported runtime hooks without making a new
  configuration setting depend on a second, easily stale inventory.
- The e2e harness also needs nonprefix runtime authority. Its disposable policy
  now explicitly names `PYTHONPATH`, `CLAUDE_FAKE_PROJECTS_DIR`, both synthetic
  `E2E_CLAUDE_TOKEN_*` credentials, and its Git config/author/committer names.
  Production defaults do not implicitly admit those names. A regression builds
  the real harness policy and checks selection through daemon and provider.
- Review P2 memory isolation: mandatory `env_remove` entries now apply after
  ambient selection, exact policy names and launch additions. Detached, resumed,
  conversation and isolated Claude launches cannot restore `CLAUDE_MEMORY_STORES`.
  The round-1 property and six policy cases now require this corrected precedence.
- Review P2 helper policy: a scoped context carries exact policy names to local
  helpers in gate preview/dispatch, daemon dispatch/control work, and all daemon
  worker submissions, including push and conversation pools. The independent PR
  wake poller and timer cycle/lane/usage-reader pools establish their own policy
  too; they cannot depend on caller-thread context inheritance. Each worker sets
  its owning daemon's policy and resets it on return or exception. Provider
  builders retain explicit policy selection independent of helper context.
  Salvage/finalization retain their Git removals; retention now also drops
  inherited Git location, pathspec and trace settings before its own controls.
- Review P3 guardian bootstrap: every guardian already starts in the package
  directory. Removing the three redundant `PYTHONPATH` rewrites preserves absent,
  empty and policy-provided provider paths. The guardian removes its own
  `SUBFLEET_EXIT_SETTLE_S` even if explicitly authorized. Regressions exercise
  detached/turn/probe/enrollment launches through both guardian exec branches.
- Review P3 doctor: recovered commit `8b693d4d4` reports the invalid variable's
  name without its value. Its rendered-output regression remains present.
- CI tooling: recovered commit `3fe81efd2` binds an outcomes recorder's absolute
  path per pytest configuration, before fixtures can replace `os.environ` or
  change cwd. Passing and failing replacement-environment tests cover this CI
  tool change. The write-error tests now make the configured destination
  unwritable directly instead of trying to redirect it through the environment.

The #205 merge was compared against `73aff8cd3`: both provider `Popen` calls
retain explicit selected environments inside release's error/QoS/linger
structure. `daemon.spawn_guardian` forwards the selected mapping, cwd and gate
descriptor to `procs.spawn_disclaimed` or the Popen fallback. Two direct forwarding
regressions cover those branches; the existing census/responsibility tests
remain part of directory validation. The four round-1 daemon mutation cases
also needed retargeting from the pre-merge Popen call to the positional
environment argument of `_spawn_guardian`; the original 34-check total is retained.

### Validation and limitations

Before fixes, `tests/e2e` returned **2 passed, 95 skipped**: the existing
process-inspection fixture skips under the sandbox's ps/sysctl restrictions.
The initial recovered-regression run returned **37 failed, 91 passed**; after
repairing the recovered test harnesses and extending the bootstrap cases, the
remaining production defects returned **47 failed, 108 passed**. The first
configuration run exposed two more test-oracle gaps (temporary root and mandatory
daemon API-key removals), both corrected; its retention failure was the pending
helper-policy correction. The attempted fallback-root mkdir was denied before
any log or live state could be opened.

All pytest invocations use a unique temporary/Hypothesis directory, the shared
`lockf -k` lock without a wait timeout, one top-level pytest at a time, and the
`ci` Hypothesis profile. No deep profile, real credentials, live daemon/store,
remote Git operations or sub-agents are used. Scratch directories are removed
after each run; logs and evidence are not staged.

The first full fake-directory hold completed 687 tests in **19m11s** before
an orderly tool interrupt kept the hold below 20 minutes: **636 passed,
5 failed, 44 skipped, 2 xfailed**. The remaining tests continue in new holds.
Four failures require the sandbox-denied boot identity and signal inspection.
The fifth was an old preflight-call oracle omitting round 1's `env_pass` argument;
that assertion now verifies the exact credential/CA names as well as `env`.
Subsequent runs record completed node IDs and pause between tests after ten
minutes, continuing only their remaining nodes under a fresh shared-lock hold.

On the updated code, the original **118 tests passed**, including all **34
mutation checks**, in **58.51 s** under a separate lock hold. The full-directory
runs continue below; final directory totals remain pending those holds.

Historical controls against archived `1a2287ca3` produced **35 expected failures
in 24.81 s**. These cover all four review findings, daemon/harness configuration,
and the outcomes plugin crash with a replaced environment. The corrected tests
were overlaid on the archive; its production code remained unchanged. The
current Python 3.12 fake-directory rerun completed its first hold with **90
passed in 672.67 s**, then released the lock before continuing its 1,601 unrun
nodes. This full rerun supersedes the earlier partial Python 3.14 fake result.

The complete fake-directory traversal covered **1,691 nodes** in four holds of
672.67, 724.00, 658.45 and 552.60 seconds. Before the final fixture/oracle reruns,
it returned **1,585 passed, 31 failed, 73 skipped, 2 xfailed**. Five failures
depend on unavailable process inspection: the four daemon-stack cases stop at
boot identity, and the pending pin-notice finalization cannot establish empty
containment and remains inside the existing settle window.

The other 26 failures exposed two pre-existing test assumptions:

- Twenty-five maintenance-clock cases inherited the launching operator's
  `SUBFLEET_RETENTION_DORMANT` switch and consequently performed no retention.
  Their module now explicitly enables the behavior under test. Later holds
  also use a disposable Subfleet root and omit the launching job's markers and
  retention switch; no operator environment or live daemon is changed.
- The generated cold-lane oracle rejected two placements even when a successful
  admission probe proved that lane during the same pass. Its reduced sequence
  (submit, admit, failed-lane finish, submit) is retained as an explicit example.
  The assertion still restricts lanes that remain unproven, preserving its
  mutation checks. This changes the test oracle, not admission behavior.

The two complete corrected test files and the historical reduced-example
control are queued for their own locked validation after the directory runs.

The full preflight-launch file then passed **8 tests in 8.55 s**. The final e2e
directory invocation returned **2 passed, 95 skipped in 7.77 s**, with the same
existing inspection guard. The first unit partition covered **6,159 nodes**:
**6,113 passed, 23 failed, 15 skipped, 8 setup errors**. Every affected node
requires unavailable boot/process inspection or the kernel descriptor ceiling.
This includes importer assertions whose conservative fallback keeps ownership
unresolved when inspection fails. All launch-environment, helper-worker, plugin,
and original mutation regressions in that partition passed.

The second unit partition exposed ten stale rediscovery assertions indexing
unlisted `GIT_TRACE*` names in `daemon_env`. Its disposable fixture policy now
authorizes each trace name exactly, and the Git operation enters that policy's
helper scope, preserving the downstream mandatory-removal check. The configured
Trace2-target case also authorizes its synthetic Git config explicitly. The
complete file is queued for a separate locked rerun. Production Git suppression
was already correct; these failures were test-oracle mismatches.


### Continuation at `2a6dde5ff` (2026-10-15)

The release merge and all 27 recovered commits are preserved. This continuation
uses workspace-local `.git-local/` metadata on `fix/launch-env-allowlist-217`,
with the checkpoint as its parent and the shared object store read-only. Nothing
is pushed or rewritten. Its ignored validation runner runs in the shell's
foreground, with each pytest process under a separate shared-lock hold; it never
detaches. Test slices stop between tests after eight minutes, with a ten-minute
per-test timeout keeping a hold below twenty minutes. Scratch/Hypothesis storage
is unique to each hold and removed afterwards. No new holds start after 4h30.

The six failures reported at this checkpoint have these verified source causes:

| CI failures | Cause and correction |
| --- | --- |
| Both responsibility guardian cases waiting for `escaper.pid` | Their direct guardian argv did not authorize `ESCAPER` or their launch markers. Pass the fixture environment's names with `guardian_pass_args`, as daemon launches do. Assertions are retained. |
| Nested-setsid guardian census | The provider lost the attempt marker, so its escaped child could not be found by the raw-marker census. Authorize the selected fixture environment explicitly. |
| QoS provider marker assertion | Same omitted guardian authorization. The provider's selected fixture environment now supplies `--env-pass` names. |
| Claude doctor version environment | The old oracle compared against the finite round-1 constant even though round 2 admits noncredential Subfleet configuration. Production already loads the root policy and passes its exact `env.pass` names. Keep that code and replace the oracle with a deterministic, complete expected mapping for both providers: defaults, one configuration setting, exactly one authorized token, no neighboring/unlisted credentials or foreign attempt marker, and Codex's configured CA. |
| Hot mirror timer worker | No queueing regression: `Timers.tick` still chooses `_mirror` for both mirror names. The new `_run_worker` wrapper inserted a callback argument before the timer name, and the spy recorded that callback as the name. Update both spies' signatures; retain the assertion that both passes use the mirror pool and neither uses the cycle pool. |

Every real `python -m subfleet.guardian` launch found under `tests/` was audited:
`test_guardian_process`, `test_census_responsibility_process`,
`test_guardian_qos_process`, `test_guardian_relay`, and the embedded launcher in
`test_stop_alarm`. All now authorize their selected fixture environments. Other
matches are imports, synthetic process records, or intercepted daemon launches;
the direct `run_guardian` unit receipt test already authorizes its dummy token.

A new guarded real-process regression starts the guardian with the daemon's
`guardian_pass_args`, checks every provider launch marker, lets its provider exit,
and finds the escaped grandchild using only the raw-marker census (no group,
descendant or responsibility root supplied). `SUBFLEET_EXIT_SETTLE_S` remains the
intentional guardian-only exception to `LAUNCH_NAMES`; the test requires its
removal even when authorized. Process assertions have not been weakened and no
inspection bypass has been added.

Commits: `86e0e95b4` (guardian fixture authorization and real-process
regression), `85ac88d52` (doctor and timer oracles), `24e198b45` (initial
continuation report), and `9fde67ea4` (preserve the stop-alarm fixture's explicit
Python controls). The final documentation commit records the blocked validation
and complete unrun manifest.

Validation could not start. This checkout has no `.venv`, and both
`uv sync --locked` and `uv run --no-sync python --version` exit 2 because the
sandbox rejects opening `/Users/maxghenis/.cache/uv/sdists-v9/.git` with
`Operation not permitted`. The task requires that existing default cache; no
alternate cache, cleanup, permission bypass or substitute test environment was
used. A request to make that existing cache writable received no response during
the job. The first locked pytest invocation remained queued; after independently
confirming that its required uv setup could not work, it was stopped with SIGINT
(exit 130) before Python started. No lock timeout or unlocked pytest was used.
The foreground validation plan was never started. No pytest, runner or other
job-started work is left running, and job scratch storage is removed.

Static validation succeeded: all modified Python files parse, as do the embedded
stop-alarm launcher and the generated provider/escaper programs. `git diff --check`
passes. Production code is unchanged from `2a6dde5ff`.

| Current-run directory | Executed tests | Unrun files |
| --- | ---: | ---: |
| `tests/fake` (original environment regressions and two pending corrected files) | 0 | 4 |
| `tests/process` | 0 | 8 |
| `tests/e2e` (run separately by file) | 0 | 17 |
| `tests/unit` (includes rediscovery, focused fixes and the original mutation checks) | 0 | 298 |
| `tests/frontend` | 0 | 41 |
| **Total** | **0** | **368** |

The [complete unrun manifest](2026-10-15-launch-env-allowlist-unrun.md) names every
file. The previous report did not preserve the exact unit-partition manifest, so
the entire unit directory is listed for a conservative rerun. The original 118
passes, including 34 mutation checks, remain the previous job's last result;
**they were not revalidated here**. The guarded real-process regression and other
process suites still need a host with process-inspection authority; their guards
were not reached because uv failed before collection.

### Original CI failures, reconstructed by node ID

The Python 3.12 log contains 1,014 progress outcomes before its internal error.
Matching them to collection at `1a2287ca3` identifies all **70** failures:
**68 e2e cases**, the stale preflight oracle at position **717**, and the
merged guardian spawn oracle at position **1,014**. The last test replaces
`os.environ`; its assertion failure triggered the recorder's missing-key crash.
Both oracle mismatches are now corrected, and the recorder fix is a CI tool
change. The e2e configuration/hook diagnosis is verified by the harness-selection
regression and source reads, but these 68 individual runtime failures cannot
be independently reproduced here because the existing inspection fixture skips
them. A host CI rerun remains necessary to confirm them all.

<details>
<summary>All 70 failed nodes and their cause/fix groups</summary>

| CI position | Failed node | Cause/fix group |
| --- | --- | --- |
| 1 | `tests/e2e/test_codex_turn_completed_e2e.py::test_zero_exit_cut_off_turn_cannot_publish_output[cut-off-item-completed]` | E: harness configuration/runtime hooks dropped |
| 2 | `tests/e2e/test_codex_turn_completed_e2e.py::test_zero_exit_cut_off_turn_cannot_publish_output[cut-off-item-started]` | E: harness configuration/runtime hooks dropped |
| 3 | `tests/e2e/test_codex_turn_completed_e2e.py::test_zero_exit_cut_off_turn_cannot_publish_output[cut-off-error]` | E: harness configuration/runtime hooks dropped |
| 4 | `tests/e2e/test_conversation_titles_e2e.py::test_claude_names_the_conversation_after_its_first_reply_in_the_turn_process` | E: harness configuration/runtime hooks dropped |
| 5 | `tests/e2e/test_conversation_titles_e2e.py::test_a_followup_sent_while_the_title_is_asked_goes_at_once` | E: harness configuration/runtime hooks dropped |
| 6 | `tests/e2e/test_conversation_titles_e2e.py::test_a_followup_never_regenerates_the_title` | E: harness configuration/runtime hooks dropped |
| 7 | `tests/e2e/test_conversation_titles_e2e.py::test_a_rename_wins_over_a_title_already_in_flight` | E: harness configuration/runtime hooks dropped |
| 9 | `tests/e2e/test_conversation_titles_e2e.py::test_unavailable_claude_title_preserves_fallback_and_holds_the_turn_at_most_its_budget[title-error]` | E: harness configuration/runtime hooks dropped |
| 10 | `tests/e2e/test_conversation_titles_e2e.py::test_unavailable_claude_title_preserves_fallback_and_holds_the_turn_at_most_its_budget[title-null]` | E: harness configuration/runtime hooks dropped |
| 11 | `tests/e2e/test_conversation_titles_e2e.py::test_unavailable_claude_title_preserves_fallback_and_holds_the_turn_at_most_its_budget[title-timeout]` | E: harness configuration/runtime hooks dropped |
| 13 | `tests/e2e/test_conversation_wakes_e2e.py::test_turn_dispatches_run_ends_and_completion_starts_exactly_one_new_turn[False]` | E: harness configuration/runtime hooks dropped |
| 14 | `tests/e2e/test_conversation_wakes_e2e.py::test_turn_dispatches_run_ends_and_completion_starts_exactly_one_new_turn[True]` | E: harness configuration/runtime hooks dropped |
| 15 | `tests/e2e/test_conversation_wakes_e2e.py::test_cli_at_reinvokes_after_five_minutes` | E: harness configuration/runtime hooks dropped |
| 16 | `tests/e2e/test_conversation_wakes_e2e.py::test_cli_pr_event_uses_batched_gh_and_reinvokes` | E: harness configuration/runtime hooks dropped |
| 18 | `tests/e2e/test_conversations.py::test_a_claude_conversation_streams_completes_and_continues_in_the_same_session` | E: harness configuration/runtime hooks dropped |
| 19 | `tests/e2e/test_conversations.py::test_a_turns_session_hooks_see_the_daemons_markers_and_surface_no_ping` | E: harness configuration/runtime hooks dropped |
| 20 | `tests/e2e/test_conversations.py::test_a_job_a_turn_dispatched_reaches_the_next_turn_and_a_ping_does_not` | E: harness configuration/runtime hooks dropped |
| 21 | `tests/e2e/test_conversations.py::test_a_conversations_session_is_refused_by_resume_revive_and_the_sessions_kit` | E: harness configuration/runtime hooks dropped |
| 22 | `tests/e2e/test_conversations.py::test_a_tool_approval_is_a_persons_decision_and_reaches_the_provider_once` | E: harness configuration/runtime hooks dropped |
| 26 | `tests/e2e/test_conversations.py::test_multiple_question_answers_reach_the_provider_once_and_a_new_message_keeps_them_pending` | E: harness configuration/runtime hooks dropped |
| 28 | `tests/e2e/test_conversations.py::test_interrupt_ends_a_running_turn_with_the_providers_own_stop` | E: harness configuration/runtime hooks dropped |
| 29 | `tests/e2e/test_conversations.py::test_a_usage_limit_fails_over_with_a_labelled_continuation_never_a_resend` | E: harness configuration/runtime hooks dropped |
| 30 | `tests/e2e/test_conversations.py::test_a_turn_that_ends_without_a_result_blocks_the_conversation_until_a_person_decides` | E: harness configuration/runtime hooks dropped |
| 31 | `tests/e2e/test_conversations.py::test_a_codex_conversation_is_read_only_until_the_guard_is_proven_live` | E: harness configuration/runtime hooks dropped |
| 32 | `tests/e2e/test_conversations.py::test_a_codex_approval_and_limit` | E: harness configuration/runtime hooks dropped |
| 35 | `tests/e2e/test_conversations.py::test_a_daemon_restart_mid_turn_adopts_the_turn_and_sends_nothing_twice` | E: harness configuration/runtime hooks dropped |
| 36 | `tests/e2e/test_conversations.py::test_a_steer_joins_the_running_turn_once_without_its_own_job[claude]` | E: harness configuration/runtime hooks dropped |
| 37 | `tests/e2e/test_conversations.py::test_a_steer_joins_the_running_turn_once_without_its_own_job[codex]` | E: harness configuration/runtime hooks dropped |
| 38 | `tests/e2e/test_conversations.py::test_a_claude_steer_after_the_last_boundary_runs_in_the_same_process` | E: harness configuration/runtime hooks dropped |
| 39 | `tests/e2e/test_conversations.py::test_a_codex_steer_that_misses_returns_to_its_original_queue_place` | E: harness configuration/runtime hooks dropped |
| 41 | `tests/e2e/test_conversations.py::test_stopping_a_host_cancels_its_provider_queue_and_requeues_pending_steers` | E: harness configuration/runtime hooks dropped |
| 42 | `tests/e2e/test_conversations.py::test_a_daemon_restart_with_a_steer_in_flight_replays_without_resending` | E: harness configuration/runtime hooks dropped |
| 43 | `tests/e2e/test_conversations.py::test_a_worktree_conversation_runs_its_turns_in_its_own_worktree` | E: harness configuration/runtime hooks dropped |
| 46 | `tests/e2e/test_conversations.py::test_a_stubborn_turn_is_stopped_by_sigint_through_the_relay_on_the_policy_clock` | E: harness configuration/runtime hooks dropped |
| 47 | `tests/e2e/test_conversations.py::test_a_turn_that_ignores_every_stop_is_contained_on_the_policy_clock` | E: harness configuration/runtime hooks dropped |
| 48 | `tests/e2e/test_conversations.py::test_a_claude_turn_on_the_wrong_model_fails_model_mismatch` | E: harness configuration/runtime hooks dropped |
| 49 | `tests/e2e/test_conversations.py::test_a_message_read_but_never_acknowledged_is_delivery_unknown_until_a_person_resolves_it` | E: harness configuration/runtime hooks dropped |
| 50 | `tests/e2e/test_conversations.py::test_a_persons_stop_without_delivery_evidence_still_blocks_as_unknown` | E: harness configuration/runtime hooks dropped |
| 51 | `tests/e2e/test_conversations.py::test_fast_unavailable_is_readmitted_at_most_max_readmits_times_then_fails` | E: harness configuration/runtime hooks dropped |
| 52 | `tests/e2e/test_conversations.py::test_message_cancel_withdraws_a_queued_message_and_tombstones_an_unknown_one` | E: harness configuration/runtime hooks dropped |
| 53 | `tests/e2e/test_conversations.py::test_a_codex_thread_with_an_active_turn_is_an_external_writer` | E: harness configuration/runtime hooks dropped |
| 54 | `tests/e2e/test_conversations.py::test_a_person_who_resolves_a_claude_message_delivered_then_chooses_how_to_go_on` | E: harness configuration/runtime hooks dropped |
| 58 | `tests/e2e/test_conversations.py::test_a_claude_conversation_hands_off_to_codex_with_its_pending_messages` | E: harness configuration/runtime hooks dropped |
| 59 | `tests/e2e/test_conversations.py::test_a_codex_conversation_waiting_on_its_limited_lane_hands_off_to_claude` | E: harness configuration/runtime hooks dropped |
| 62 | `tests/e2e/test_guard_and_isolation.py::test_provider_environment_isolated_in_each_sandbox[read-only-astra-success-codex]` | E: harness configuration/runtime hooks dropped |
| 63 | `tests/e2e/test_guard_and_isolation.py::test_provider_environment_isolated_in_each_sandbox[read-only-haiku-success-allowed-claude]` | E: harness configuration/runtime hooks dropped |
| 64 | `tests/e2e/test_guard_and_isolation.py::test_provider_environment_isolated_in_each_sandbox[workspace-write-astra-success-codex]` | E: harness configuration/runtime hooks dropped |
| 65 | `tests/e2e/test_guard_and_isolation.py::test_provider_environment_isolated_in_each_sandbox[workspace-write-haiku-success-allowed-claude]` | E: harness configuration/runtime hooks dropped |
| 67 | `tests/e2e/test_legacy_cockpit.py::test_imported_history_reopens_and_its_session_continues_without_resending_it` | E: harness configuration/runtime hooks dropped |
| 69 | `tests/e2e/test_milestone1_codex.py::test_detached_cli_exit_does_not_end_provider_work` | E: harness configuration/runtime hooks dropped |
| 70 | `tests/e2e/test_milestone1_codex.py::test_kill_slow_job_interrupts_attempt_and_records_owner` | E: harness configuration/runtime hooks dropped |
| 71 | `tests/e2e/test_milestone1_codex.py::test_kill_dirty_allocated_worktree_publishes_salvage` | E: harness configuration/runtime hooks dropped |
| 77 | `tests/e2e/test_milestone2_claude.py::test_allowed_readings_percentages_and_raw_stream` | E: harness configuration/runtime hooks dropped |
| 78 | `tests/e2e/test_milestone2_claude.py::test_credits_rejection_closes_model_and_retry_explains_exclusion` | E: harness configuration/runtime hooks dropped |
| 79 | `tests/e2e/test_milestone2_claude.py::test_limit_without_clock_is_guessed_one_hour` | E: harness configuration/runtime hooks dropped |
| 80 | `tests/e2e/test_milestone2_claude.py::test_model_downgrade_persists_transcript_attestation` | E: harness configuration/runtime hooks dropped |
| 81 | `tests/e2e/test_milestone2_claude.py::test_a_credential_that_belongs_to_another_account_yields_no_capacity` | E: harness configuration/runtime hooks dropped |
| 82 | `tests/e2e/test_milestone2_claude.py::test_a_setup_token_lane_keeps_working_without_profile_scope` | E: harness configuration/runtime hooks dropped |
| 83 | `tests/e2e/test_milestone2_claude.py::test_empty_result_with_zero_rc_is_unknown` | E: harness configuration/runtime hooks dropped |
| 84 | `tests/e2e/test_notice_job_state.py::test_c9_2_c15_1_a_codex_job_cancelled_while_running_is_announced_cancelled` | E: harness configuration/runtime hooks dropped |
| 85 | `tests/e2e/test_recovery.py::test_running_job_survives_daemon_sigkill_once` | E: harness configuration/runtime hooks dropped |
| 87 | `tests/e2e/test_reserve.py::test_c11_7_opus_lands_on_the_lane_with_slack_and_the_reserved_lane_is_named` | E: harness configuration/runtime hooks dropped |
| 89 | `tests/e2e/test_reserve.py::test_c11_7_setup_token_lanes_stay_unmeasured_for_opus_while_fable_still_runs` | E: harness configuration/runtime hooks dropped |
| 91 | `tests/e2e/test_review_r2_strand_e2e.py::test_background_wait_outliving_the_result_does_not_swallow_the_wake[automatic]` | E: harness configuration/runtime hooks dropped |
| 92 | `tests/e2e/test_review_r2_strand_e2e.py::test_background_wait_outliving_the_result_does_not_swallow_the_wake[wake-me]` | E: harness configuration/runtime hooks dropped |
| 93 | `tests/e2e/test_review_r3_strand_timing_e2e.py::test_strand_timing[automatic]` | E: harness configuration/runtime hooks dropped |
| 94 | `tests/e2e/test_review_r3_strand_timing_e2e.py::test_strand_timing[wake-me]` | E: harness configuration/runtime hooks dropped |
| 97 | `tests/e2e/test_stop_bound.py::test_c5_8a_a_worker_that_never_returns_cannot_keep_the_lock` | E: harness configuration/runtime hooks dropped |
| 717 | `tests/fake/test_daemon_launch.py::test_c14_2_refused_preflight_is_recorded_in_the_attempt_directory_and_daemon_log` | P: old preflight oracle omitted env_pass |
| 1014 | `tests/fake/test_launch_env_daemon.py::test_actual_daemon_spawn_rejects_pollution[detached]` | G: merged guardian settle setting omitted from spawn oracle; recorder read replaced environ |


E: admit noncredential Subfleet configuration and explicitly authorize the
harness's nonprefix runtime hooks; preserve helper policy. P: assert the
preflight's exact `env_pass` credential/CA names. G: account for the merged
guardian-only settle setting, and bind the outcomes path before tests run.

</details>


## Round 3: review r2 (2026-10-16)

Base `1a4eb9c66`. Review r2 (`~/reviews/subfleet-hub/launch-env-2026-10-14/review-r2.md`)
requested changes: one P2 and three P3 findings. The hub's design decisions
(Max delegated Subfleet design) are implemented as given; the judgement calls
are listed under "Choices". No credential store, secret loader, live store,
live daemon or live process was read or changed, and every test value is a
dummy.

### Changes

1. **P2, the `SUBFLEET_` prefix admitted credentials.** `launch_env.allowed_name`
   no longer admits a prefix.
   - **Exact configuration names.** `CONFIG_NAMES` lists every `SUBFLEET_*`
     configuration name the package reads, plus `CODEX_GUARD_PREFLIGHT_TIMEOUT`.
     `tests/unit/test_launch_env_round3.py` generates the inventory: it parses
     every module (string constants and keyword names) and `bin/`'s shell
     reads, and fails on a `SUBFLEET_*` name that is neither listed, a launch
     marker, nor retired. It also fails on a listed name nothing reads, and
     shows the only name built at run time is compat's `CARPOOL_*` aliasing
     (no `CARPOOL_*` name is admitted).
   - **Retired names.** `RETIRED_NAMES`: `SUBFLEET_ALLOW_API_LANE` and
     `SUBFLEET_STATE_DIR` are read only to say they are ignored, so they are
     never inherited and doctor names them.
   - **Harness hooks.** `HARNESS_PREFIXES` (`SUBFLEET_FAKE_`, `SUBFLEET_E2E_`) and
     `HARNESS_NAMES` (`SUBFLEET_LIVE`, exact). A test shows no production module
     spells a name under them, and that every hook the fakes and the e2e, fake
     and live harnesses spell is admitted.
   - **Substring backstop.** `CREDENTIAL_WORDS`, matched anywhere in a name, in
     any case, refuse it whether or not it is listed or harness-shaped. The
     words are the hub's: TOKEN, KEY, SECRET, PASSWORD, PASSWD, PASSPHRASE, PASS,
     PWD, CREDENTIAL, CRED, AUTH, OAUTH, BEARER, COOKIE, JWT, PAT, PRIVKEY,
     WEBHOOK. Exact `env.pass` names are still explicit authority.
   - **Reviewed exceptions.** `CREDENTIAL_EXCEPTIONS` holds the four admitted
     names a word matches by accident, each holding a path, never a secret:
     `PATH` ("PAT"), `SSH_AUTH_SOCK` (the agent's socket; its keys stay in the
     agent), `SUBFLEET_FAKE_DIAGNOSTICS_PATH` and `SUBFLEET_FAKE_RELEASE_PATH`
     (the fake providers' report and release files). A test pins the set and
     checks each member trips a word and is otherwise admitted.
   - **Doctor.** `environment_report` lists every name the allowlist drops, so
     each unlisted `SUBFLEET_*` name now appears in `extra_names`, by name only.
2. **P3, env-kind credentials crossed lanes.** A lane's credential now reaches
   only that lane.
   - `credentials.env_references` names the daemon variables env-kind lanes use,
     from `Lane` objects or roster rows.
   - `ProviderEnvironment` takes `references`, and withholds every reference from
     both parent selection and `env.pass`. The daemon wires it to the store, read
     at each launch, and so do the timers' heal and version runs.
     `build_environment` gains `withheld` for this. A lane's own value arrives
     only through `resolve_credential`, as its `CLAUDE_CODE_OAUTH_TOKEN`
     addition.
   - `policy.check_lane_credentials` refuses an `env.pass` entry that is some
     lane's reference (exit 2, `env.pass[i]`). It runs in `daemon start` before
     the spawn, and at daemon startup after the roster is seeded.
     `_released_on_failure` gives back the lock, store and log.
   - `daemon start` gives the daemon process alone every reference named in
     `lanes.json` or the store (read-only), so an env-kind lane needs no policy
     entry.
   - `resolve_credential` refuses an env-kind reference the allowlist admits,
     because every launch inherits such a name.
   - The e2e harness's policy no longer passes `E2E_CLAUDE_TOKEN_*`. The fake
     Codex now reports which names it saw (never values), so the e2e isolation
     test checks that neither provider holds either reference.
3. **P3, stale comment.** `Launch.env_remove` now says mandatory removals apply
   last and win over the parent, policy names and `env_add`. It names
   `test_mandatory_removal_also_beats_selected_additions`, and a test reads the
   comment.
4. **P3, suggestion resume.** `_resume_start` enters `helper_policy` with the
   daemon's policy, so the file pool's `git worktree add` carries `env.pass`
   names. The pattern matches `wakes._poll_prs`.

Contract: C-10.5, C-11.1, C-12.3 and C-17.8 state these rules, with a dated
entry under "Changes in version 3".

Commits after `1a4eb9c66`, tests first:

- `d976c7a55`: the regressions, and the test oracles that change by design.
- `61abb0eaa`: exact Subfleet names, harness prefixes and the backstop (P2).
- `b38f6ebeb`: lane credentials reach only their own lane (P3).
- `968e1ca10`: the `env_remove` comment (P3).
- `59b1d7fcb`: the resumed suggestion start's helper policy (P3).
- `4395b629e`: the acceptance contract.
- `4499dd5f0` and `40d8e178d`: two test refinements, made after the control
  run (see below).
- This report.

### Choices

- **Retired names stay out of the allowlist.** The hub said to generate the
  list from the package's reads. `SUBFLEET_ALLOW_API_LANE` (`cli.py`) and
  `SUBFLEET_STATE_DIR` (`compat.py`) are read, but only to say they are ignored.
  They are classified as retired rather than admitted, so doctor flags a leftover.
- **Three names are added to the list:** `SUBFLEET_DAEMON_BIN` (the CLI's daemon
  override), `SUBFLEET_V1_BIN` (compat) and `SUBFLEET_NO_AUTOPICK`
  (`bin/codex`). Round 2's prefix admitted them; round 1's list lacked them.
  `SUBFLEET_HOME` and `SUBFLEET_RETENTION_DORMANT` also appear in
  `CONFIG_NAMES`, so the one list is complete. Round 1's oracle gains the three
  new names.
- **No `home_path(...)` helper exists in this package.** Names read through
  module constants (`SETTINGS_ENV`, `STORE_ENV`, `TIMEOUT_ENV` and others) are
  string constants, so the scan covers them.
- **The daemon-only list comes from the lane roster, not a new policy key.**
  `daemon start` reads `lanes.json` and the store. The launchd plist is
  unchanged: as before, an env-kind lane under launchd needs the variable
  added to the daemon's environment by hand.
- **The resolve guard is an addition.** It makes "never forwarded by the
  allowlist" hold for every spawn path, helpers included, not only for the
  daemon's provider launches.
- **Every reference is withheld, the lane's own included.** The provider gets
  its token as `CLAUDE_CODE_OAUTH_TOKEN`, so no provider needs the reference
  name. Withholding filters the parent, not additions, so a lane whose
  reference is literally `CLAUDE_CODE_OAUTH_TOKEN` still gets its own token.
- **Commits sit on a detached HEAD.** This job's worktree started detached at
  `1a4eb9c66`, and `fix/launch-env-allowlist-217` is checked out in the
  caller's worktree. Each commit descends from `1a4eb9c66`, so the branch can
  fast-forward to the last one. The shared Git index accepted the commits;
  no workspace-local metadata was needed. Nothing was pushed.

### Invariants

- **No credential word.** For every name *n*: `allowed_name(n)` implies that *n*
  is listed or harness-shaped, and that *n* is a reviewed exception or holds no
  credential word in any case. A bounded Hypothesis property covers prefix ×
  affixes × word × case (60 derandomized examples).
- **Reports and selection agree.** `environment_report` names exactly the
  parent names `allowed_name` rejects, and never a value.
- **No lane reference in any launch.** For every launch of lane *ℓ*,
  `names(launch) ∩ references(lanes) = ∅`. The only lane credential value in a
  Claude launch is *ℓ*'s own; a Codex launch holds none. This is checked
  through the daemon's own seams (`_credential_environment`,
  `_launch_environment`, `_relaunch_env`) for three channels: policy, the old
  prefix, and harness names.
- **Policy and lanes are disjoint.** For every policy *P* and roster *L*
  accepted by `daemon start` or daemon startup,
  `P.env.pass ∩ references(L) = ∅`.
- **The inventory is complete.** Every `SUBFLEET_*` name the package spells is
  classified (generated, not remembered).

### Control: the regressions at `1a4eb9c66`

The new and changed regressions were run against `git archive d976c7a55`, the
tests commit, whose production code equals `1a4eb9c66`. The run used the
workspace's Python 3.12 `.venv`, from the archive directory. Result:
**65 failed, 11 passed in 91.6 s**.

The control ran the tests as of `d976c7a55`. The two later test commits were
not re-run against `1a4eb9c66`; this is deduced from reading them:

- `4499dd5f0` narrows a staleness check on the inventory test. That test fails
  at `1a4eb9c66` before reaching the check, on `RETIRED_NAMES`.
- `40d8e178d` rewrites membership checks to print booleans, not mappings
  (`not in set(env)`, `not any(value == ...)`, `env.get(...)`). Each rewritten
  check is logically equivalent to the one it replaces.

- **All 15 of the review's names fail**, in one parametrized case: each
  survives `build_environment`.
- **38 more assertion failures:**
  - all 18 backstop words: round 2 applied no credential check to a listed name;
  - 7 harness and exact-name cases;
  - the 3 lane-crossing channels, where the Codex launch held both references;
  - doctor's baseline (2);
  - 8 single cases:
    - the real-case daemon start;
    - the unread-setting case;
    - daemon start holding both rosters' references;
    - the comment test;
    - the resume regression: the pooled `git worktree add` environment lacked
      `GIT_CONFIG_GLOBAL`;
    - round 2's harness test;
    - the fake-daemon `SUBFLEET_TEST_OAUTH` Codex launch;
    - the startup case, where a policy passing a lane credential started the
      daemon.
- **3 missing refusals:** `daemon start` with a passed reference (seed and store
  rosters) did not raise `PolicyError`, and an allowlisted reference resolved.
- **9 absent interfaces:** the new constants and
  `ProviderEnvironment(references=...)`.
- **The 11 that pass are expected to:** 9 exact-name cases where the old prefix
  gives the same answer, compat's construction guard, and a noncredential
  `env.pass` start.

### Validation

Each hold was one pytest process in the foreground, under its own hold of the
shared lock. Every hold used one worker, Python 3.12.14 (GIL), and its own
scratch `TMPDIR` and Hypothesis directory. A hold that reached 19 minutes would
have stopped with a `KeyboardInterrupt` and teardown; none did.

| Hold | Files | Result | Lock wait |
| --- | --- | --- | --- |
| Control | the new and changed regressions, on `git archive d976c7a55` | 65 failed, 11 passed (91.6 s), as expected | 12 min |
| A | the brief's holds 1, 2, 3, 5 and 6, plus the three contract parsers | 757 passed (312.2 s) | 12 min |
| B1 | `tests/e2e/test_conversations.py`, `test_milestone2_claude.py`, `test_guard_and_isolation.py` | 41 passed, 15 skipped (321.7 s) | 18 min |
| B2 | the other e2e files that set `SUBFLEET_FAKE_*`, the four other e2e files on the same harness policy, and the unit files that call `daemon_env` | 301 passed, 1 skipped (487.7 s) | 29 min |
| C | the 13 other test files that touch environment selection, found by grep | 1955 passed, 1 skipped (584.2 s) | 16 min |

Hold A, by file:

- `tests/unit/test_launch_env_allowlist.py`: 27 passed
- `tests/unit/test_launch_env_helpers.py`: 20 passed
- `tests/unit/test_launch_env_mutations.py`: 34 passed
- `tests/unit/test_launch_env_policy.py`: 26 passed
- `tests/unit/test_launch_env_round2.py`: 66 passed
- `tests/unit/test_launch_env_round3.py`: 67 passed
- `tests/fake/test_launch_env_daemon.py`: 34 passed
- `tests/unit/test_policy.py`: 178 passed
- `tests/unit/test_policy_support.py`: 14 passed
- `tests/unit/test_agent_secret_credentials.py`: 7 passed
- `tests/unit/test_doctor.py`: 44 passed
- `tests/unit/test_doctor_ca_bundle.py`: 16 passed
- `tests/unit/test_doctor_identity.py`: 13 passed
- `tests/unit/test_conversation_service.py`: 98 passed
- `tests/unit/test_suggestions.py`: 30 passed
- `tests/fake/test_suggestions_fake.py`: 1 passed
- `tests/fake/test_state_contract.py`: 37 passed
- `tests/unit/test_test_module_names.py`: 1 passed
- `tests/unit/test_invariants_index.py`: 25 passed
- `tests/unit/test_desktop_ledger.py`: 4 passed
- `tests/unit/test_retention_turn_contract.py`: 2 passed
- `tests/process/test_guardian_process.py`: 5 passed (process inspection was allowed)
- `tests/process/test_census_responsibility_process.py`: 8 passed

Hold B1, by file:

- `tests/e2e/test_conversations.py`: 29 passed, 15 skipped
- `tests/e2e/test_milestone2_claude.py`: 7 passed
- `tests/e2e/test_guard_and_isolation.py`: 5 passed

The 15 skips are lane-only: 10 person-only requests and 5 refused because the
lane carries Subfleet's markers. CI runs them outside a lane.

Hold B2, by file:

- `tests/e2e/test_conversation_wakes_e2e.py`: 4 passed
- `tests/e2e/test_legacy_cockpit.py`: 1 passed
- `tests/e2e/test_milestone1_codex.py`: 9 passed
- `tests/e2e/test_recovery.py`: 2 passed
- `tests/e2e/test_reserve.py`: 4 passed
- `tests/e2e/test_review_r2_strand_e2e.py`: 2 passed
- `tests/e2e/test_review_r3_strand_timing_e2e.py`: 2 passed
- `tests/e2e/test_stop_bound.py`: 3 passed
- `tests/e2e/test_suggestions_e2e.py`: 1 passed, 1 skipped (person-only, lane-only skip)
- `tests/e2e/test_codex_turn_completed_e2e.py`: 3 passed
- `tests/e2e/test_conversation_titles_e2e.py`: 9 passed
- `tests/e2e/test_http_isolation.py`: 1 passed
- `tests/e2e/test_notice_job_state.py`: 1 passed
- `tests/unit/test_cli.py`: 222 passed
- `tests/unit/test_daemon_verbs.py`: 20 passed
- `tests/unit/test_rediscovery_git_trace.py`: 17 passed

Hold C, by file. These are the test files outside the brief's list that
touch environment selection, credentials, the CARPOOL aliasing or a
`SUBFLEET_TEST_*` name:

- `tests/fake/test_launch_env_providers.py`: 6 passed (the brief's
  `tests/unit/test_launch_env*.py` glob misses this file)
- `tests/fake/test_daemon_launch.py`: 9 passed
- `tests/fake/test_detached_cwd_pins.py`: 18 passed
- `tests/unit/test_codex_ca_bundle.py`: 46 passed
- `tests/unit/test_codex_ca_bundle_provider.py`: 6 passed
- `tests/unit/test_compat.py`: 1615 passed
- `tests/unit/test_conversation_wakes.py`: 22 passed
- `tests/unit/test_guard_trust.py`: 98 passed
- `tests/unit/test_guardian.py`: 12 passed
- `tests/unit/test_stop_alarm.py`: 2 passed
- `tests/unit/test_timers_busy_read.py`: 115 passed
- `tests/process/test_guardian_qos_process.py`: 2 passed, 1 skipped (the
  `inherit` case: this lane's own process is clamped, as in review r2)
- `tests/process/test_guardian_relay.py`: 4 passed

**Hold layout.** The brief listed six holds. With about ten `lockf` waiters
(one waited 88 minutes) and lock waits of 12 to 33 minutes, I ran its holds 1,
2, 3, 5 and 6 as one pytest (hold A), and split the e2e files in two (B1, B2).
Hold C adds the files outside the brief's list that touch environment
selection. Each hold stayed one pytest process, one worker, under 20 minutes,
with a 19-minute bound enforced in-process. I stopped a queued hold 1 and the first
queued control while they waited; neither held the lock or had started
pytest.

### Not run

- **Lane-only skips:** 16 cases, 15 in `tests/e2e/test_conversations.py` (10
  person-only, 5 refused for the lane's markers) and 1 in
  `test_suggestions_e2e.py` (person-only). They need a terminal outside a lane,
  and CI runs them.
- **The QoS `inherit` case** (`test_guardian_qos_process.py`): this lane runs
  clamped.
- **The rest of the suite, never run:** every test file not named in holds A,
  B1, B2 and C. That includes the rest of `tests/unit`, `tests/fake` and
  `tests/process`, all of `tests/frontend` and `tests/live`, and Python 3.14.
  CI runs the full suite on 3.12 and 3.14 at the pushed head.
- **Live authentication and the live daemon:** off limits, and not touched.

### Cleanup

Every hold's scratch `TMPDIR` and Hypothesis directory were removed when its
run ended. The runner, logs, JUnit files, staged copies and the control
archive were in one scratch folder outside the repository; it is removed at
the end. No process of this job remains. The ignored `.venv` (Python 3.12)
remains; the bytecode caches the runs wrote are removed. Nothing was pushed.

## Round 4: #222 meets #198 (2026-10-16)

Base: the hub's merge `e5948f430` of `release/217` (`f305f6214`, with #198,
D-ST1, and #224) into this branch. Review r3
(`~/reviews/subfleet-hub/launch-env-2026-10-14/review-r3.md`) approved
`f694b8ffa` with three P3s. The hub's five integration decisions are
implemented as given, and the choices it left open are listed under
"Choices". No credential store, secret loader, live store, live daemon or
live process was read or changed, and every test value is a dummy.

### What failed at `e5948f430`, and which rule each test encodes

A locked run of the hold that failed (`tests/unit/test_live_store_guard*.py`,
`test_agent_secret_credentials.py`, `test_handler_refusals.py`) gave 39 failed
and 961 passed, the hub's count. With `test_every_subfleet_name_the_package_reads_is_listed`,
40 tests failed in all:

| Tests at `e5948f430` | Count | Rule encoded | Cause | Test changed? |
| --- | --- | --- | --- | --- |
| `test_collection_phase_is_isolated`, `test_configure_phase_is_isolated[override]`, `test_test_override_wins`, `test_undo_restores_session_safe_path` ×4, each `[adapter-projects]` | 7 | D-ST1: a resolver under the session uses the session's isolated `CLAUDE_CONFIG_DIR`, and undo restores it | the launch selection dropped the session's `CLAUDE_CONFIG_DIR`, so `_config_projects_dir` refused | no |
| `test_e2e_child_has_every_guard_variable` | 1 | D-ST1: an e2e child resolves the harness's own config directory | the same drop, in the child | no |
| `test_unconfigure_restores_environment_and_expires_captured_child` ×3, round 3's `test_captured_daemon_environment_expires_after_cleanup[test-call-*]` ×2 and `test_captured_daemon_environment_guards_a_live_session_child`, round 4's four marker-lifetime tests | 10 | D-ST1: a daemon environment captured in the session carries its markers and isolated paths, and expires with it | `cli.daemon_env` dropped every unlisted D-ST1 name | no |
| `test_outside_pytest_matches_release_217` ×21 | 21 | #198: production is release/217's, for every kind | the differential called the new `_config_projects_dir` on a namespace with no `provider_environment` | yes: decision 2, below |
| `test_every_subfleet_name_the_package_reads_is_listed` | 1 | #222: every `SUBFLEET_*` name the package spells is classified | 14 D-ST1 names were in no class | yes: it gains the test-session class (decision 1) |

The 18 tests that encode D-ST1's rule pass unchanged. The differential and the
classification test changed only where they encoded the old adapter shape and
the old three-class scheme.

### Decision 1: D-ST1's names in D-ENV1's scheme

`launch_env.TEST_SESSION_NAMES` is a fourth exact-name class beside
configuration, launch markers and retired names. It holds D-ST1's three
markers and every `home_paths.SAFE_HOME_PATHS` name that is not already
configuration: 15 names, derived from D-ST1's one registry so the two cannot
drift. The builder selects them **only while the parent is itself in a live
test session** (`home_paths.under_pytest`, which now takes the parent's
mapping): the marker with a sentinel whose identity still matches, a bare
marker, or `PYTEST_CURRENT_TEST`. So every child a test launches carries them,
and they expire with the session. Outside a session none is selected, and
production's selection is exactly the one review r3 approved.

Per name, with where it is set and read and why its value can hold no secret:

| Name | Class | Set by | Read by | Value |
| --- | --- | --- | --- | --- |
| `SUBFLEET_UNDER_PYTEST` | test session | `tests/conftest.py`, the e2e harness | `home_paths.under_pytest`; `credentials`' import-time guard | the flag `1` |
| `SUBFLEET_PYTEST_SESSION` | test session | `tests/conftest.py`; the e2e harness copies it | `home_paths.under_pytest`; the keychain exec guard's pins | the session's `mkdtemp` directory |
| `SUBFLEET_PYTEST_SESSION_ID` | test session | `tests/conftest.py`; copied likewise | `home_paths.session_alive` | `dev:ino:token`: the sentinel file's device and inode, and the 64-hex token written into that file. It proves a sentinel is the session's own. It authenticates to no service, and a process that holds it can only refuse more. |
| `SUBFLEET_CODEX_APP_HOME` | test session | `SAFE_HOME_PATHS` | `home_paths.codex_app_home` (test-only seam) | a path beneath an isolated test base |
| `SUBFLEET_DESKTOP_LOGIN` | test session | likewise | `home_paths.desktop_login_path` (test-only) | likewise |
| `SUBFLEET_PROVIDER_HOME` | test session | likewise | `home_paths.provider_home` (test-only) | likewise |
| `SUBFLEET_V1_STATE` | test session | likewise | `importer`, `lanes_transfer` (test-only) | likewise |
| `SUBFLEET_DELEGATE_STATE` | test session | likewise | `importer` (test-only) | likewise |
| `SUBFLEET_V1_ROSTER_DIR` | test session | likewise | `importer`, `lanes_transfer` (test-only) | likewise |
| `SUBFLEET_V1_LAUNCH_AGENTS` | test session | likewise | `lanes_transfer` (test-only) | likewise |
| `SUBFLEET_LAUNCHD_PLIST` | test session | likewise | `cli.launchd_plist_path` (test-only) | likewise |
| `SUBFLEET_HOST_HOME` | test session | likewise | `host_push._credential_paths`, for `host_credentials` and `_environment` (test-only) | likewise |
| `SUBFLEET_HOST_CONFIG` | test session | likewise | likewise (test-only) | likewise |
| `SUBFLEET_AGENT_SECRET` | test session; reviewed backstop exception (`SECRET`) | likewise | `credentials.keychain_command` (test-only) | the path of the session's stand-in `agent-secret` executable. It is only ever executed, never read as a credential. |
| `CLAUDE_CONFIG_DIR` | test session | likewise; the e2e harness sets its own | the Claude adapter (through the launch environment), `claude_mcp.global_config_path`, `home_paths.protected_provider_homes` | a path beneath an isolated test base. In production it stays a home lane's own addition, or `env.pass`. |
| `SUBFLEET_SESSION_STORE`, `SUBFLEET_DESKTOP_LOG`, `SUBFLEET_CLAUDE_DIR`, `SUBFLEET_HOME`, `SUBFLEET_CLAUDE_SETTINGS`, `SUBFLEET_RESET_INHIBIT` | configuration (`CONFIG_NAMES`, from round 3) | `SAFE_HOME_PATHS` in tests; operators in production | production readers, through `home_path` | paths |
| `CLAUDE_LANE_AGENT_SECRET` | none: dropped everywhere | D-ST1 removes it from the session | `credentials.keychain_command` in the daemon itself | a helper path, but `SECRET` and unlisted, so never inherited |
| `SUBFLEET_DEV_APP_EXECUTABLE` | configuration | D-ST1 removes it from the session | the daemon's development-app person boundary (D-PB1, `conversations/service.py`) | a path |

"Test-only" means `home_path(..., test_only=True)`: outside pytest the
override is ignored and the release default is used, so nothing in production
reads the value. `test_live_session_values_hold_no_credential` checks the live
values: the flag, the sentinel's identity format, and that every isolated path
is absolute and lies beneath the session's base or pytest's base temporary
directory.

### Decision 2: the outside-pytest differential, per kind

The child now builds a real `ClaudeAdapter`, whose `provider_environment` is a
`ProviderEnvironment`, in place of a bare namespace. Every mode (7) and home
spelling (3) still runs, and every kind is still compared:

| Kind | Decision |
| --- | --- |
| `sessions.mirror.store_dir`, `sessions.revive.session_store_dir`, `sessions.desktop.log_path`, `sessions.transcripts.claude_dir`, `notify_push.claude_dir`, `client.state_root`, `guard.preflight._state_root`, `hooks.settings_path`, `adapters.claude_mcp.global_config_path`, `doctor.check_never_rules` | still equal to release, unchanged. `claude_mcp` and `doctor` were never reached at `e5948f430`, because the loop stopped at the adapter; both pass now. |
| the four fixed homes and six legacy seams | still equal to release, unchanged |
| `adapters.claude._config_projects_dir` | **D-ENV1's intended change.** The expectation is now release's own function evaluated with the launch's selected environment as `os.environ`. The child also asserts that the selection holds no `CLAUDE_CONFIG_DIR`, and that the answer equals release whenever the daemon has no non-empty `CLAUDE_CONFIG_DIR`. The parent asserts the answer differs from release exactly in the `absolute`, `relative` and `tilde` modes with an ordinary HOME. With a literal `~` HOME, both may refuse alike, which depends on the Python version. |
| the same, under explicit authority | **equal to release.** With `env.pass: ["CLAUDE_CONFIG_DIR"]`, and with a home lane's own `CLAUDE_CONFIG_DIR` (`~/lane-home`) under either policy, the answer is exactly release's, in every mode. |

This found one difference that was not D-ENV1's intent. The merge's fallback
was `Path(env["HOME"]) / ".claude"`, where release used `Path.home()`. They
differ for an empty HOME (`.` against `/`) and, on Python 3.12, for a literal
`~` HOME: release refuses with "Could not determine home directory", and the
merge did not. `adapters.claude._launch_home` now spells the launch's HOME as
`Path.home()` does: a launch inherits HOME by name, so it is the daemon's own
value. On Python 3.12, the nine cases with a literal `~` HOME cover it. So does
`test_launch_home_is_spelled_as_release_spelled_it`, for those spellings and an
empty HOME, on every version.

### Decision 3: every other failure

None. Each D-ST1 failure was fixed in code by decision 1, and no D-ST1 test
changed.

### Decision 4: the keychain runner rule

**Kept, with one refinement.** A reader is handed the allowlisted environment
when no runner is supplied, as before. `credentials.native_runner` now also
counts `subprocess.run` itself passed as the runner, the same identity D-ST1's
`keychain_command` already treats as native. Any other runner, such as
#198's stand-ins, gets no `env` and builds its own.

- **Why not pass `env` to every runner:** #198's runners must keep working.
  Round 8's `test_credential_shell_runner_cannot_escape_to_emulated_system_path`
  wraps `subprocess.run(command, env={...}, **kwargs)`. An imposed `env` would
  raise `TypeError` before its exec guard could refuse.
- **Production:** no caller supplies a runner. `ClaudeAdapter()` is built bare
  by the registry, `cli`, `conversations.launch` and `conversations.classify`.
  `resolve_credential` is called with no runner from the timers and the
  daemon, and `_keychain_blob` only from `home_login`. So production's
  keychain reader always gets the allowlisted environment.
- **Shown:** `test_the_native_reader_gets_the_allowlisted_environment` runs a
  stand-in reader script for each of the three readers, with no runner and
  with `subprocess.run`. The script records only its environment's names: an
  unlisted lane variable, an unrelated token and `CLAUDE_LANE_AGENT_SECRET`
  are absent, and the markers are present.
  `test_a_supplied_stand_in_runner_builds_its_own_environment` shows a stand-in
  receives no `env`.

### Decision 5: review r3's P3s

- **P3-1.** `check_lane_credentials` also refuses an env-kind lane variable
  that some launch inherits without policy: `launch_env.inherited_name`, which
  covers the default allowlist, a harness hook or a test session's name. It
  runs at both callers: `daemon start` (exit 2, naming `credential_ref` and the
  variable, before any spawn, never a value) and daemon startup, after the
  roster is seeded, which releases its lock. The resolve guard uses the same
  predicate and stays the second line of defence for a lane added later.
- **P3-3.** `daemon.main` catches `PolicyError` and exits 2, printing the
  message to stderr, which is `daemon.log` under both `daemon start` and
  launchd. Any invalid policy now ends the same way. launchd's `KeepAlive`
  still restarts it.
- **P3-2.** `environment_report(references=...)` lists the lane variables the
  daemon holds apart, as `lane_credentials`, and leaves them out of
  `extra_names`. `daemon.status` passes the store's references. Doctor's
  allowlist row warns only on other names, and a new pass row, "daemon lane
  credential variables", lists the lane variables by name. It is `unknown`
  for an older or malformed daemon. Called without references,
  `environment_report` keeps its round-3 shape.
  `test_daemon_status_and_doctor_expose_only_extra_names` gains the new key,
  the one test changed for this.

### Choices

- **Session-scoped, not unconditional.** The hub preferred exact
  `CONFIG_NAMES` entries. Two names rule that out:
  - `CLAUDE_CONFIG_DIR` must reach a test's children, because D-ST1's tests
    require it and the adapter reads it from the launch environment. But D-ENV1
    deliberately keeps it out of production launches (C-12.4; hub decision 2).
  - `SUBFLEET_AGENT_SECRET` trips the backstop.
  A session-scoped class satisfies both, and leaves production's allowlist and
  doctor's report byte-for-byte as approved. The 13 harmless names could have
  been unconditional, but one rule for one registry is simpler, and every one
  of them is ignored outside pytest anyway.
- **Liveness, not the bare marker.** Selection follows `under_pytest(parent)`,
  so a daemon environment captured during a session and replayed after cleanup
  passes none of the names on. Its children are ordinary processes, as D-ST1's
  expiry tests require.
- **The parent decides.** `build_environment(environ=...)` checks the mapping
  it selects from, not the calling process, so a guardian's second selection
  and a captured mapping are judged by their own markers.
- **The backstop still applies to session names.** Only
  `SUBFLEET_AGENT_SECRET` trips a word, and it is a reviewed exception: a path,
  selected only in a live session, never read in production.
- **No contract-merge tool run.** The hub resolved the contract by hand at
  `e5948f430`. This round edits C-10.5, C-11.1, C-12.3, C-17.8 and C-20.6 in
  place.
- **Commits sit on a detached HEAD** at `e5948f430`'s descendants, in the
  shared Git index. Nothing was pushed.

### Invariants

- **A session changes only session names.** For every parent *p* and passed
  set *P*: `names(build(p, P)) = A(p) ∪ P(p) ∪ (S(p) if live(p))`. Here *A* is the
  production allowlist, *S* the session names present, and *live* is
  `under_pytest(p)`. Values are copied unchanged. `environment_report(p)`
  names exactly `p − A(p) − (S(p) if live(p))`. A bounded Hypothesis property
  covers four session states (none, bare, live, expired), random subsets of
  the allowlist, the session names and five others, and three passed names
  (60 derandomized examples).
- **Production is unchanged.** With no live session, the selection equals
  round 3's, for every parent: the property's `none` and `expired` states, and
  `test_outside_a_live_session_production_selection_is_unchanged`.
- **Every child of a live session carries the session.** The helper, the
  daemon (`cli.daemon_env`), both providers and the guardian's second
  selection hold every D-ST1 name with its value. The daemon's own
  `SUBFLEET_HOME` is the one exception.
- **No inherited lane variable.** For every roster accepted by `daemon start`
  or daemon startup: `references(L) ∩ {n : inherited_name(n)} = ∅`.
- **The projects folder.** For every mode and HOME spelling outside pytest:
  `new = release ∘ selected-environment`. It equals release under explicit
  authority.

## Round 5: review r4 (2026-10-18)

Base: `b27681e16`, with `origin/release/217` at `3fbc4b7c5` (#225 and #227)
merged first as `6477b8ec1`. That merge conflicted only in the contract's
change log, where both sides had added entries at the top: both are kept,
this branch's D-ENV1 entries first and release's #225 entries after them,
each block in its own order. No module this round touches changed on
release. Review r4 (`~/reviews/subfleet-hub/launch-env-2026-10-14/review-r4.md`)
asked for changes: one P2 and four P3s. The hub's five decisions are
implemented as given; the choices it left open are under "Choices". No
credential store, secret loader, live store, live daemon or live process was
read or changed, and every test value is a dummy.

### P2-1: a refused lane is never stored, and a stored one never wedges the daemon

**Check first.** Both callers now build the same list before anything is
written: `credentials.startup_lanes(roster, stored)` gives each env-kind
lane's (lane id, variable, source), where the source is `lanes.json` for
every roster row and `the store` for each stored lane that no roster row
repairs. `policy.check_lane_credentials(policy, path, lanes)` takes it:

- an `env.pass` entry naming a lane's credential variable raises, naming
  `env.pass[i]`, the variable and each such lane with its source (P3-1);
- a `lanes.json` row whose variable some launch inherits raises at
  `lanes.json`, key `credential_ref`, naming the lane, the variable and the
  fix;
- a stored lane whose variable some launch inherits is returned as (lane id,
  variable): it is held, never fatal.

Daemon startup now calls `Daemon._open_lanes`, which reads `lanes.json` once,
runs the check, then seeds that same roster (`_seed_lanes(roster)`), then
writes one `daemon.log` warning per held lane. Seeding the very rows that
were checked means the file cannot change between the check and the write.
`daemon start` runs the same check in `cli.daemon_env` and prints each held
lane's warning on stderr before it spawns.

**A stored lane is held.** Nothing about a held lane is stored or changed.
It is refused wherever a launch could start:

- **Admission.** `scheduler.judge_lane` refuses it as `credential-refused`,
  computed from the lane row's own `credential_kind` and `credential_ref`
  (both already in `LANE_FACTS`, so C-6.3's judged-as-it-was check holds).
  It is a standing refusal (`STANDING_REFUSALS`, `actions.UNUSABLE_REASONS`),
  so a job pinned to it is held `pin-unadmittable` under C-11.8. The notice
  reads "claude-9 names HOME, a variable every launch inherits, so it holds
  no credential of its own (D-ENV1); point it at a daemon variable off the
  launch allowlist in lanes.json, same lane id, provider and account, and
  restart the daemon". An unpinned job routes to the other lanes.
- **Resolution, the second line.** `resolve_credential` still refuses the
  variable (`credential-env-inherited`). Every probe, keepalive, heal and
  relaunch resolves its credential before it spawns, and the Claude
  adapter's enrolment refuses an env-kind credential outright, so none of
  them reaches a guardian on a held lane.
- **Status.** Both status views flag the lane `credential-refused=<variable>`.

The warning, in `daemon.log` and on `daemon start`'s stderr, reads "lane
claude-9 in the store names HOME, a variable every launch inherits, so it
cannot hold one lane's credential; the lane is held and nothing launches on
it; fix: store the lane's OAuth token under a daemon variable off the launch
allowlist, point the lane at it in lanes.json (same lane id, provider and
account) and restart the daemon". It holds names only, never a value.

**Recovery.** `credentials.reference_repair(stored, row)` gives a stored lane
a roster row's variable only when:

- the stored lane is env-kind and its variable is refused;
- the row has the same lane id, provider and account;
- the row is env-kind and its variable is not refused.

`Store.repair_lane_reference` then rewrites `credential_ref` and moves the
credential epoch on by one, guarded on the old variable, with a
`lane.reference_repaired` event (`from`, `to`, `epoch`). This is C-1.3's one
in-place binding. Such a lane never held a credential this release could
resolve, so nothing that was bound is rebound. Without such a row, a held
lane stays held, and it never blocks startup.

**A held lane's variable is no credential.** `credentials.env_references`
now leaves refused variables out. So the daemon is not handed one by name
(`cli.daemon_env`), `env.pass` may name one, and doctor's
`lane_credentials` omits it. `credentials.withheld_references`, which every
launch withholds, keeps a held lane's harness hook (production never reads
one, and round 3's `harness` channel test withholds it) but not a listed or
session name. Withholding those would take HOME or PATH from every other
lane, or a test child's session names.

### P3-1: the `env.pass` refusal names its entry, the variable and the lane

At both callers: "policy.json: env.pass[1]: E2E_LANE_NINE_TOKEN is the
credential variable of lane claude-9 (lanes.json), which reaches only its
own lane; remove it (the daemon holds each lane's credential without it)".
A store-only lane reads `(the store)`. C-11.1 says so.

### P3-2: a name popped between iterating and reading is skipped

`launch_env._present(parent, names)` reads each name and skips one that
raises `KeyError`. Both the session probe and the selection read through it.
The probe still reads only `SESSION_PROBE_NAMES`, and the selection reads
only names it has accepted. `ProviderEnvironment.additions` and its CA
sources use it too.

### P3-3: no withheld value is read

`ProviderEnvironment.additions` tests `name not in withheld` before
`name in environ`.

### P3-4: session names only with the marker

`_selected` decides `PYTEST_MARKER in probe and under_pytest(probe)`. A bare
`PYTEST_CURRENT_TEST` selects no session name, with or without a copied
session folder. `under_pytest` is unchanged for its other callers:
`credentials`' import-time guard, the keychain refusals and the home-path
refusals still count a bare `PYTEST_CURRENT_TEST` as a test process, which
only refuses more there.

The review's table, as the code now decides it (each row through helpers,
both providers, and the guardian's authorized and bare reselections):

| Parent | `under_pytest` | Session names selected |
| --- | --- | --- |
| marker, live sentinel | yes | all present |
| marker, deleted sentinel | no | none |
| marker, sentinel recreated with the same token on a new inode | no | none |
| marker, session with no identity | no | none |
| bare marker (`1` or `0`) | yes | all present |
| bare `PYTEST_CURRENT_TEST` | yes | **none** (round 4: all present) |
| marker, expired session, copied `PYTEST_CURRENT_TEST` | no | none |
| expired session, copied `PYTEST_CURRENT_TEST`, no marker | yes | **none** (round 4: all present) |
| none of these | no | none |

C-20.6's sentence now reads: "copied markers revive no isolation, even
beside a copied `PYTEST_CURRENT_TEST`. A copied `PYTEST_CURRENT_TEST`
without the marker selects none of the session's names either (C-12.3),
whether or not a session folder was copied with it; it only makes
`home_paths.under_pytest` count the child a test process, so the child
refuses personal fallbacks and keychain reads, which only refuses more."
Round 4's change-log line now says that it selected while `under_pytest`
decided the parent was in a live session, which included a bare
`PYTEST_CURRENT_TEST`, and that round 5 drops that case.

### Choices

- **Held, not disabled.** A held lane is not written to: its `enabled` flag,
  its readings and its closures stay as they were. The hold is computed from
  the lane's own row each time it is judged, so it ends the moment a repair
  rewrites the variable, and an operator's `enabled` stays the operator's.
- **The repair is narrow.** It needs a stored variable that is refused and a
  row for the same lane id, provider and account. A stored lane whose
  variable is fine still ignores `lanes.json`, as before. Moving the epoch on
  marks the new credential for the timers' revoked and heal bookkeeping,
  which key on it.
- **Startup checks a superset.** For `env.pass`, the check counts every
  `lanes.json` row's variable, including a row for a lane id already stored,
  which seeding skips. `daemon start` did this already, and the daemon now
  matches it.
- **Seeding takes the rows it checked.** `_seed_lanes(roster)` stores exactly
  the roster `_open_lanes` checked. `tests/unit/test_daemon_init_release.py`'s
  stand-in for `_seed_lanes` now accepts that argument. That is the one
  existing test outside this PR's files that this round changes.
- **The timers are unchanged.** A periodic probe of a held Claude lane fails
  at resolution, reads as `unknown` and backs off, as for any credential that
  cannot be resolved. Nothing is spawned, and no request is sent.
- **Doctor is unchanged.** `lane_credentials` lists credential variables. A
  held lane's variable is none, so doctor leaves it out. The lane shows in
  both status views as `credential-refused=<variable>`, and in `daemon.log`.

### Invariants

- **Startup.** For every `lanes.json` and store pair *(R, S)* and every
  policy *P* that loads, the daemon starts iff no row of *R* names a refused variable
  and `env.pass(P)` names no credential variable of *R* or of *S* after
  repair. No stored lane decides the outcome except through `env.pass`. A
  refused startup writes nothing to the store. A successful one stores
  exactly *S*, with repairs applied, plus each new row of *R*, so no refused
  row is ever newly stored. Every stored lane with a refused variable is
  refused by `scheduler.pin_unadmittable` as `credential-refused`, and its
  resolution raises. `daemon start` reaches the same verdict before the
  daemon runs. Bounded Hypothesis property:
  `test_bounded_property_stored_lanes_never_refuse_startup`, 25
  derandomized examples, each a real `Daemon` on a temporary root, over up
  to three roster and three stored lanes. Lane ids overlap, accounts differ,
  variables come from two credential and three refused names, and three
  `env.pass` names are drawn.
- **Selection.** For every parent *p*:
  `session_names(build(p)) = S(p)` if `PYTEST_MARKER ∈ p` and its session is
  live (or *p* has no session folder), else `∅`. This holds for helpers,
  both providers and the report, for `dict`, `MappingProxyType`, `ChainMap`
  and a custom `Mapping`, and the selection is the same for each mapping
  type. Bounded Hypothesis property:
  `test_bounded_property_session_names_iff_marker_and_live_session`, 120
  derandomized examples. It draws four marker values, seven session states
  (none, live, deleted, recreated, no identity, foreign identity, empty) and
  three `PYTEST_CURRENT_TEST` values, plus up to 15 session names and 10
  other names.
- **No withheld value is read; a popped name reads as absent.** Shown with
  strict and vanishing parents.

### Tests that changed because of a decision

| Test | Decision | What it encoded | Change |
| --- | --- | --- | --- |
| `test_launch_env_round4.py::test_bounded_property_a_session_changes_only_session_names` | P3-4 | Its `none` state with `PYTEST_CURRENT_TEST` drawn counted as live for selection, so it expected every session name present to be selected: round 4's bare-`PYTEST_CURRENT_TEST` rule. | Its `under_pytest` assertion is unchanged; selection now expects session names only when the marker is present too. |
| `test_daemon_init_release.py::test_a_failing_release_step_does_not_hide_why_construction_failed` | P2-1 | Its stand-in for `Daemon._seed_lanes` took no roster. | It takes the roster it is handed; it still raises, and the test is otherwise unchanged. |
| `test_launch_env_daemon.py::test_daemon_refuses_a_policy_passing_a_lane_credential`, `::test_daemon_startup_refuses_a_lane_variable_every_launch_inherits` | P2-1 | A docstring said the check ran after seeding, and a comment said the second start read the store. | Words only: the check runs before seeding, and the second start reads lanes.json again, because nothing was stored. |

No other existing test changed. Every D-ST1 test that ran (the main hold's
`test_live_store_guard*.py`, 546 of them) passed unchanged. None of them
relied on a bare `PYTEST_CURRENT_TEST` selecting session names: each one
that sets it alone checks `under_pytest`'s in-process refusals, which are
kept.

### Control: the regressions at the tests-only commit

`77840b88b` holds the tests and no fix. Its tree was exported, with
`3ad63b9d1`'s, `299e7b133`'s and `508e754d5`'s version of
`tests/fake/test_launch_env_daemon.py`, which were copied in before the run
began. The new regressions ran there under one hold
(`-k "round5 or review_r4 or never_refuse_startup"`): **29 failed and 19
passed** in 421 s.

- Every regression failed, each on the round-4 behaviour it targets:
  - a refused first start stored claude-9 and named no lane;
  - steps 2 and 3 exited 2;
  - the env.pass refusal named no variable and no lane, at both callers;
  - `KeyError` for a popped `PYTEST_CURRENT_TEST`, session identity or
    HOME;
  - the withheld value was read, for both providers, live session and not;
  - the bare-`PYTEST_CURRENT_TEST` rows selected session names;
  - both bounded properties found counterexamples, four for startup.
- The 19 that passed are not regressions:
  - the eight rows of the review's table that this round leaves unchanged;
  - ten rows showing `under_pytest` keeps its meaning;
  - the guard showing a bare `PYTEST_CURRENT_TEST` still refuses a personal
    fallback.

### A test defect that reached the live state root

`cli._root` ignores its arguments and resolves `SUBFLEET_HOME`. With no
session marker, the default is the person's `~/.subfleet`. Four of the new
unit tests replaced `os.environ` with a dict that had no `SUBFLEET_HOME` and
no marker:

- `test_daemon_start_warns_of_a_stored_refused_lane_and_starts`, three cases;
- `test_daemon_start_takes_a_fixed_lanes_json_row_for_a_stored_refused_lane`.

In the control run above, between 13:46 and 13:52 EDT, each of those four
ran round 4's `cmd_daemon_start` against `~/.subfleet`. The spawn,
`_daemon_alive` and `Client.lock_info` were stubbed. By the code, each call:

- ran `os.chmod(~/.subfleet, 0o700)` (afterwards the mode reads
  `drwx------`, the mode `daemon start` always sets);
- read `policy.json` and `lanes.json`, and read the store through
  `Offline`'s `mode=ro` connection;
- opened `daemon.log` for append and closed it without writing;
- built an environment in memory that was neither printed nor kept.

Nothing was spawned, no file's content was written, and no value was
printed. The other two CLI tests failed at their first assertion at that
commit, so they never reached `cmd_daemon_start`. The main hold ran after
the fix (`d68ec288a`): every replaced environment now names the test's own
root, and each test asserts the root `daemon start` resolves before calling
it. The fake tests' CLI steps had used the session's isolated
`SUBFLEET_HOME`, not the live root. They now set their own root.

### Mutations

Each fix has a mutant, run in memory by `tests/unit/test_launch_env_mutations.py`.
Each is killed by its named regression:

| Mutant | Killed by |
| --- | --- |
| seed `lanes.json` before the check (`Daemon._open_lanes` seeding first) | `test_review_r4_a_refused_first_start_stores_nothing`: "the check runs before lanes.json is seeded" |
| refuse the whole daemon on a stored lane (`check_lane_credentials` raising on a held lane) | `test_review_r4_a_removed_lane_is_held_and_never_blocks_startup`: launchd's start exits 2 |
| drop the `KeyError` tolerance (`_present` reading every name) | `test_a_name_popped_between_iterating_and_reading_is_skipped`: `KeyError` |
| put `name in environ` back first (`ProviderEnvironment.additions`, both providers) | `test_provider_environment_never_reads_a_withheld_value`: the withheld value is read |
| select on a bare `PYTEST_CURRENT_TEST` (`_selected`'s `session = under_pytest(probe)`) | `test_session_names_need_the_marker_and_a_live_session`, both changed rows |

All seven mutation cases pass, which means each mutant is killed. The
removed-lane case was rerun in the second hold, after its oracle's own fix
below.

### Validation

Every hold was one locked pytest with one worker, on Python 3.12.14 with the
GIL, from the job's own `.venv`, run as
`env -u PYTHONPATH .venv/bin/python -c '<pytest.main under a 1,140 s alarm>'`
(the brief's `uv run pytest`, without the uv cache lock). `TMPDIR` and the
Hypothesis store were in scratch. The lock queue held 10 to 16 waiters, and
each wait took between 30 and 70 minutes.

| Hold | Head | Files | Result | Time |
| --- | --- | --- | --- | --- |
| control | `77840b88b` export | round-5 regressions (above) | 29 failed, 19 passed | 421 s |
| 1 | `d68ec288a` | `test_launch_env_*` (allowlist 27, helpers 20, mutations 41, policy 26, round2 67, round3 67, round4 40, round5 39), fake daemon 49, fake providers 6, `test_claude_adapter` 170, `test_policy_support` 14; `test_live_store_guard` 324, round3 22, round4 8, round5 11, round6 112, round7 50, round7 mutations 19 | 1,108 passed, 4 failed; stopped by the alarm in `test_live_store_guard_round8` | 1,168 s at load 60 |
| 2 | `c0e60e0f3` | fake daemon 49, `test_launch_env_mutations` 41, `test_unadmittable_pin` 12, `test_daemon_init_release` 2, e2e `test_milestone2_claude` 7, process `test_claude_isolation` 15, `test_codex_isolation` 38, `test_guardian_process` 5, `test_state_contract` 37, `test_test_module_names` 1, `test_render` 20, `test_store_ids` 27, `test_scheduler` 97 | 351 passed | 211 s |
| 3 | `c0e60e0f3` | `test_policy` 178, `test_doctor` 44, `test_handler_refusals` 317, `test_agent_secret_credentials` 7, `test_live_store_guard_round8` 100, round8 mutations 4, `test_live_store_guard_state_root` 22, fake `test_lanes_enroll` 15, `test_lanes_reenroll` 19, `test_daemon_verbs` 20, `test_lanes_transfer` 79 | 805 passed | 62 s |

- **Hold 1's four failures** were steps 2 and 3 of the new daemon test, and
  the fault was in the test. Its stand-in for `cli.subprocess.Popen`
  replaced the process's own `subprocess.Popen` for the rest of the test.
  The test daemon's Git inspection at submit then got the stand-in's child
  and refused the job. `bbadfc780` undoes the stand-in before any daemon
  runs. In hold 2 the file passed whole (49), with the startup property's 25
  daemons, and the mutation file passed again (41).
- **Code under test.** No production module changed after `d68ec288a`;
  holds 2 and 3 ran the same code. In both, the one uncommitted tracked
  change was this report.
- **Brief coverage.** All six of the brief's runs are covered across the
  three holds. No `tests/fake/test_daemon_restart*.py` exists. Process
  inspection was allowed, and the e2e file ran all 7 cases with none
  skipped.
- **2,178 distinct tests passed** at the final code, counting each file once
  at its last run: 1,108 from hold 1, less the fake daemon file's 45 and the
  mutation file's 41, plus 351 and 805, with nothing failing.
- **No other import breaks.** A static pass found no code or test that
  imports a name this round removed, and every `from` import of the changed
  modules resolves.

**Not run:**
- Python 3.14.
- The full suite, a whole-suite `--collect-only`, `tests/frontend` and
  `tests/live`.
- The other `test_scheduler_*`, `test_doctor_*` and `test_render_*` files.
- `tests/e2e/test_conversations.py`.
- A control rerun of the four corrected CLI unit tests. At `77840b88b`,
  `cli.daemon_env` raises for the stored refused lane (in
  `test_daemon_start_warns_…`) or for the stored lane's inherited variable
  (in `test_daemon_start_takes_…`). So each returns 2 where the test needs
  0. That is read from the code; no run shows it.
- Live authentication and a real `launchctl kickstart`. The launchd path was
  run as `daemon.main`, which is what launchd runs.

### Commits

`6477b8ec1` (merge), `77840b88b`, `3ad63b9d1`, `299e7b133`, `508e754d5`,
`d68ec288a` and `bbadfc780` (tests), `37ba6cd9a` (P2-1, P3-1), `526f7da17`
(P3-2 to P3-4), `8f64e0eb8` (contract), and `87d4f1b7c`, `c0e60e0f3` and
this commit (report). All sit on a detached HEAD in the shared Git index.
Nothing was pushed.

### Cleanup

The scratch folder is removed, with both exports, the logs and the
Hypothesis store, and no `.hypothesis` folder was written in the
repository. The ignored `.venv` (14 MB) remains. No process of mine is
running, and no `lockf` is held or waiting.
