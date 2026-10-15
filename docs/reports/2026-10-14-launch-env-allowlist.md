# Lane launch environment allowlist (D-ENV1)

Base: `release/217`, `3f50142ac1cc`. Reported on 2026-10-14 by the d1063 key-rotation session and verified by the hub. A shell-started daemon retained unrelated exported credentials, and provider children inherited them. This change selects names before constructing child environments. No credential store, secret-loader script, live store, live daemon or live process was inspected or changed. All test credentials are synthetic; no environment values are included in this report.

## Original exact-name allowlist

`subfleet/launch_env.py` owns the default selection. The table records round 1's finite set and its rationale. Round 2 additionally admits noncredential `SUBFLEET_*` configuration, with credential-word and launch-marker exclusions described below. Unknown `LC_*` names remain excluded.

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
  worker submissions, including push and conversation pools. Each worker sets
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
Subsequent runs record completed node IDs and pause between tests after four
minutes, continuing only their remaining nodes under a fresh shared-lock hold.

Final directory totals and the historical CI failure mapping follow once those
holds finish.


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
