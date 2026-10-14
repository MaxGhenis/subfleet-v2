# Lane launch environment allowlist (D-ENV1)

Base: `release/217`, `3f50142ac1cc`. Reported on 2026-10-14 by the d1063 key-rotation session and verified by the hub. A shell-started daemon retained unrelated exported credentials, and provider children inherited them. This change selects names before constructing child environments. No credential store, secret-loader script, live store, live daemon or live process was inspected or changed. All test credentials are synthetic; no environment values are included in this report.

## Default allowlist

`subfleet/launch_env.py` owns the finite set. The first twenty names were observed in the working launchd daemon. Other names are exact additions with the reasons below. Neither `SUBFLEET_*` nor `LC_*` is accepted as a wildcard. Unknown names, including every unknown secret suffix, are excluded without needing a list of secret-store item names.

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

1. Parent values are selected only by exact membership in the default allowlist or policy `env.pass`. Selection never mutates the parent mapping. Presence, including an empty value, is retained.
2. Each launch supplies its own credential/configuration directory, OAuth credential when applicable, attempt/session/hook markers and push-bundle path separately. Those explicit additions override selected parent values. The guardian's Python import bootstrap is added after selecting provider names, so it does not become provider ambient authority.
3. `env.pass` is optional and must be a list of ASCII variable names matching `[A-Za-z_][A-Za-z0-9_]*`. No glob, whitespace, assignment or Unicode identifier is accepted. A policy name authorizes only that name, including when it has a secret suffix; it also overrides a default environment removal. Launch-owned values are explicit authority as well. Managed-setting rejection for isolated reviews remains in effect.
4. `daemon start` selects with the same builder and reads `env.pass` from the selected root's policy before spawning. It replaces the state-root variable with its own root. The install plist's explicit `EnvironmentVariables` are retained.
5. Every guardian provider exec selects again. The daemon authorizes its already-selected launch-owned and policy names using repeated `--env-pass NAME` arguments. No credential value is placed in argv or launch records. A directly invoked guardian has only the default allowlist unless names are deliberately provided.
6. `daemon.status.environment.extra_names` contains sorted names outside the default allowlist, never values. `doctor` asks the running daemon for this diagnostic even without `--live`; it warns on extras, passes on an empty list and reports unknown for an unavailable or older daemon. Explicitly passed names outside the default set are still reported for review.
7. Local Git/catalog/notification helpers use the default set and their own explicit variables; provider metadata and heal paths additionally receive the provider's explicit policy selection. CA selection remains the shared C-10.8 behavior.

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

Real-process coverage requires a host that permits the existing boot and process identity checks. No ownership bypass was added to make those tests pass. The nine skips comprise six CA-bundle daemon cases, two routing cases and one gate case. Other process, live-provider and end-to-end suites were not run; they would require additional host permissions or real credentials. There is no remaining implementation work identified by the completed checks.

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
