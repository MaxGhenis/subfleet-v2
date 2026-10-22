# D-SH1: review launch permissions, capability and publication

The round-2 design at `~/reviews/subfleet-hub/shell-downgrade-2026-10-14/design-r2.md` supersedes round 1's default. The original evidence was 466 successful read-only Claude reviews during October 7–14, 314 prompts asking for commands, 407 deliverables reporting no shell, and two Track B verdicts caused solely by inability to run tests. Fleet ops' addendum establishes that Codex read-only can run commands but cannot write even temporary files, so it cannot run pytest either.

## Round 1 retained

- `--needs-shell` / `--no-needs-shell` and boolean batch `needs_shell` override optional task policy `requires`. Only the distinct requirement `shell` is valid. Resolved requirements persist across retries, recovery and native resume; explicit choices affect idempotency.
- Admission filters incompatible launches before ranking and again at reservation. Exhausted candidates wait with an explanatory `why`; later compatible chain entries remain eligible. Incompatible explicit pins fail at submit with exit 7 and fixes naming workspace-write or a Codex model. Competing demand uses the same requirement.
- The daemon's policy model map is authoritative for `-m`, including sol61, luna and custom names. Empty pins are rejected. Retired sol still maps to astra with its note.
- Round 1's boolean shell evidence is superseded by capability below. Its shipped review shell requirement is removed; shell remains opt-in.

Round-1 commits: `e7bb5fb15`, `3ef714a79`, `205ff2fff`, `8eb36a490`, `bde120c1e`, `da07a2758`, `b5165a845`, `05869349d`, `8d96571ac`. The starting merge `a2a3e8f2cc1e` includes release #205. The source was already present in this checkout; no unpublished staging tail needed recovering. Round 1 left verification unfinished; the verification below covers its retained behavior too.

After the 2026-10-15 cold boot, verification resumed from recovered head `473fc30e4`. The five recovered round-2 commits (`a643b05d1`, `497b13787`, `98c4e84c8`, `3680dcbdf`, `473fc30e4`) were read and preserved. The first resumed fake-suite run found one incomplete step: gate host-push submissions reached the generic dispatch-only refusal before the review publication refusal. The review check now runs first for both review tasks and gate rounds.

## Round 2 implementation and invariants

The shipped policy grants `permissions.review = "worktree-write"` and sets no `requires`. This is a policy permission, never a provider sandbox. At submission it resolves to workspace-write only for a new job worktree cut from a committed Git repository. In-place, non-git, isolated reviews and gate rounds resolve directly to read-only. This does not produce a writable result and downgrade it under d261. Explicit workspace-write outside Git remains refused, now with the required read-only escape. Existing writer admission and worktree ownership rules apply to writable reviews.

Attempts record `capability = none | commands | writable` at reservation and confirm it when constructing the launch. Claude read-only is none; Codex read-only is commands; either writable provider is writable. Schema 9 migrates known version-8 shell flags using the stored job sandbox and leaves unknown historical evidence unknown. The deprecated has_shell column remains for additive schema compatibility but new attempts use capability. Historical attempts never receive a fictitious publish denial.

Generic Store SQL reads retain SQLite's raw integer values. Boolean launch flags are converted only by job/attempt accessors and RPC record serialization, so raw SQL aggregates and retention's archive snapshot/recheck keep their original values and identical JSON bytes. Show, wait, list and status retain their existing JSON boolean fields.

Completion notices, runs show, its JSON and wait summaries expose the evidence. A none launch says it could not run any command; commands says nothing could be written, temp files included, so pytest could not run. The status response and text count distinct run ids with any restricted attempt reserved in the last 24 hours. Deliverable and `-o` bytes remain unchanged.

Review tasks and every gate round receive the same launch-scoped publishing guard on both providers, including resumes. Git/gh wrappers have embedded targets and policy, placed beside the attempt outside the job worktree. Wrapped git refuses push even with config overrides or marker removal. An inherited Git push URL rewrite and empty GIT_ALLOW_PROTOCOL transport allowlist also deny absolute-path pushes and explicit remote push URLs, even with protocol config overrides. Local Git reads still execute; remote Git transports, including fetch, are unavailable. gh allows pr view/diff/checks and field-free API GETs, and fails closed on write verbs, non-GET methods, fields, aliases, extensions and unknown commands. The never-rules hook is untouched. Explicit host-push submission for a review is refused. Attempts and notices say `publish: denied`.

The wrapper layer was chosen because both providers inherit the launch environment and it avoids provider-specific hook parsing or changes to the pinned never-rules hook. It is a command-surface restriction, not an OS boundary against hostile arbitrary code. Code discarding its environment and selecting alternate or absolute binaries can bypass it; raw HTTPS using a token found on disk is also outside its boundary. It must not be represented as token revocation or an immutable host security boundary.

The #222 branch `fix/launch-env-allowlist-217` was read locally, initially at `1a2287ca300414b361406c92a3c50c8451be4dc1` and again after recovery at `7c13d347186823e2d6baaf1859db17e377ccc66b`. Its `build_environment` applies explicit launch additions after ambient filtering; `guardian_pass_args(env)` carries selected names across the guardian boundary. This change puts PATH, GIT_ALLOW_PROTOCOL and the exact GIT_CONFIG_COUNT/KEY_n/VALUE_n entries in Launch.env_add, using that mechanism rather than broadening ambient inheritance. At merge, keep the daemon's final guard application before #222 captures guardian_pass_args, and retain both adapters' additions. No new ambient allowlist entry or SUBFLEET prefix grant is needed.

Contract clauses C-11.1, C-11.2, C-12.3, C-12.4, C-15.1 and C-17.2 and version history describe the change.

## Verification

Every pytest invocation uses its own foreground lockf hold, no lock timeout, the ci profile and temporary Hypothesis storage. There is only one pytest process at a time. The writable-worktree regression invokes pytest in-process to exercise its tmp_path fixture without starting a second pytest process. All Git targets are disposable local file:// repositories and gh is fake. No provider, live store or GitHub endpoint is used.

Latest normal file results combine the recovered passing checks with resumed verification: **1,488 distinct passes, three sandbox skips and one existing expected failure**, with no remaining ordinary failures. Every requested file was invoked; the three unexecuted process cases are named below. All three mutations were caught by named regressions.

Tests-first commit `a643b05d1` produced 45 expected failures against the round-1 head. Implementation commit `497b13787` includes the policy, evidence, launch guard and contract. The first implementation check found the old notice invariant parser, a repeated in-process pytest module basename and an existing stale migration-version expectation; these fixtures were corrected without changing deliverable semantics.

The focused corrected run passed 107 tests in 123.82 seconds: review launch permissions 11, publish guard 33, retained needs-shell 23, migration 15 and policy sandbox 25. After hardening, the unit run completed in 190.93 seconds with 923 passes and three failures. All launch, CLI and adapter tests passed. The resumed unit check completed in 11.63 seconds with 156 passes and the same two retention failures. It confirms all 46 policy-ramp cases after the hash correction in `3680dcbdf`, 97 scheduler cases and the module-name check.

The isolated `3f50142ac` baseline passed all 14 policy-support cases in 8.87 seconds. The two retention failures were a round-1 regression: generic `Store.query`/`one` coerced the new integer flags to booleans, while retention's transaction recheck read raw SQLite integers. JSON bytes therefore differed (`false` versus `0`) for unchanged rows, and retention deferred pruning. Tests-first commit `c8d7483fc` covers raw SQL aggregates, identical archive snapshot bytes through both read paths, and boolean job/attempt accessors with and without pooled readers. Fix `eaa9b86b9` restores raw SQL values and converts flags only in the domain accessors; `789504973` preserves boolean fields in RPC records. The verification passed all 14 policy-support cases, all 17 migration/flag cases, and the review/state regressions, including strict JSON boolean assertions.

That broader Store hold completed in 510.31 seconds: 292 passed and 91 failed during idle-daemon construction because macOS boot identity is unavailable. All 91 failures were confined to the SQL contention/read-pool unit files. Fixture fix `26fe2a763` supplies synthetic boot/start identities; the real SQLite queries, transactions and concurrency checks remain intact, and no operating-system identity guarantee is claimed. Their full-file rerun passed all 238 cases in 51.36 seconds. Production identity checks and the three process-backed skips below are unchanged.

| Unit file (`tests/unit/`) | Passed | Failed | Verification |
| --- | ---: | ---: | --- |
| test_policy.py | 186 | 0 | recovered |
| test_policy_disk.py | 57 | 0 | recovered |
| test_policy_ramp.py | 46 | 0 | resumed |
| test_policy_support.py | 14 | 0 | Store fix verified |
| test_policy_tier_preferences.py | 15 | 0 | recovered |
| test_cli.py | 207 | 0 | recovered |
| test_codex_adapter.py | 93 | 0 | recovered |
| test_claude_adapter.py | 172 | 0 | recovered |
| test_review_publish_guard.py | 66 | 0 | recovered |
| test_store_migration_2.py | 17 | 0 | Store fix verified |
| test_offline.py | 33 | 0 | recovered |
| test_notice_rendering.py | 21 | 0 | Store fix verified |
| test_test_module_names.py | 1 | 0 | Store fix verified |
| test_scheduler.py | 97 | 0 | resumed |
| test_store_contention_queries.py | 212 | 0 | SQL fixture verified |
| test_store_properties.py | 3 | 0 | Store fix verified |
| test_store_ids.py | 10 | 0 | Store fix verified |
| test_store_snapshot_plans.py | 4 | 0 | Store fix verified |
| test_store_readers.py | 26 | 0 | SQL fixture verified |

The first resumed fake run completed in 71.62 seconds: 182 passed, three skipped and one expected failure, with the gate publication-refusal ordering regression failing. Commit `16aebc2b8` moves that refusal ahead of generic dispatch validation. The focused permission rerun passed all 15 cases in 7.85 seconds, including both providers' writable worktrees and the gate refusal. Including the bounded stateful machines and corrected notice expectations, latest file results establish 208 distinct passing fake tests, three skips and one expected failure:

| Fake file (`tests/fake/`) | Passed | Other |
| --- | ---: | --- |
| test_review_launch_permissions.py | 15 | — |
| test_review_needs_shell.py | 23 | — |
| test_policy_sandbox.py | 25 | — |
| test_routing_end_to_end.py | 14 | 2 skipped |
| test_gate_end_to_end.py | 10 | 1 skipped |
| test_admission_properties.py | 27 | 1 expected failure |
| test_admission_priority.py | 32 | — |
| test_state_contract.py | 37 | — |
| test_admission_stateful.py | 2 | ci profile, both machines |
| test_notice_header.py | 23 | — |

The stateful/notice hold completed in 315.05 seconds: both state machines passed under the ci profile, while ten notice assertions still expected a header without capability evidence. Commit `244c7cf17` explicitly expects the complete Codex commands-only sentence for attempts, retaining bare headers for jobs that never reserved an attempt. The focused rerun passed all 23 notice cases in 24.86 seconds. Previously passing unit files are retained from the recovered run; only unresolved checks and coverage relevant to new fixes were repeated.

Three process-backed cases could not run because C-5.3's macOS boot identity is unavailable under this sandbox: `test_routing_end_to_end.py::test_c11_5_socket_submission_and_cli_why_print_recorded_walk`, `test_routing_end_to_end.py::test_c11_4_generic_adapter_probe_classifies_a_real_tiny_fake_turn`, and `test_gate_end_to_end.py::test_plan_peer_process_finalizes_through_real_daemon`. Their files were invoked; these cases remain skipped. The existing expected failure is `test_admission_properties.py::test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again`: C-6.10's wait key still includes the kind as well as the verdict. Neither limitation is represented as a pass.

The regression replay at `3f50142ac` completed in 22.06 seconds: 66 expected failures and 14 passing controls. All ten selected launch-permission/status cases failed; Git push cases failed in all 12 provider/sandbox/resume/gate combinations because unprotected local pushes succeeded; 40 gh write cases failed because writes reached the fake gh. Twelve permitted gh reads and two direct daemon model-routing controls passed. The retained shell-wait case and three unsatisfiable-pin cases failed as expected too. Thus every specified round-2 regression is red at the requested base, with working read controls and no collection errors.

Mutation: changing the shipped `permissions.review` back to `read-only` was killed by `tests/unit/test_policy.py::test_d_sh1_default_reviews_write_only_in_job_worktrees_and_shell_is_opt_in` (one expected failure, 5.45 seconds). The mutation runs in an isolated copy and is restored before the next check.

Mutation: dropping `protect_launch`'s publication block was killed by `tests/unit/test_review_publish_guard.py::test_review_launch_real_git_push_local_file_is_denied` and `::test_review_gh_write_denied_reads_reach_fake`: 52 expected failures and 12 passing read controls in 17.56 seconds. All 12 Git launch combinations actually pushed to their disposable local bare repositories with the block removed; all 40 fake-gh write cases reached the fake. The 12 permitted reads continued to pass.

Mutation: making `launch_has_shell` always return true was killed by `tests/fake/test_review_needs_shell.py::test_hard_review_waits_for_codex_then_runs_with_shell` (one expected failure, 3.01 seconds): it wrongly admitted the read-only Claude fallback while Codex was full. All three mutated files were restored and checked byte for byte against the final implementation. The completed test holds stayed below 20 minutes; the longest was the 510.31-second Store verification.

## Delivery and remaining work

The shared Git index is not writable in this sandbox. All round-2 commits use workspace-local `.git-local` metadata on `fix/review-needs-shell-217`, with original ancestry and the required co-author trailer. Files in the assigned worktree contain the changes. No caller checkout write, live policy edit, history rewrite, network Git remote access or GitHub call occurred. Publication controls use only disposable local file:// bare repositories.

Both rounds' requested verification is complete apart from the three explicitly skipped process cases and the existing expected failure. No test wrapper remains running, temporary verification copies were removed with `shutil.rmtree`, and no `.hypothesis` directory remains. No evidence logs, JSON evidence or bundles were staged.

The hub owns live policy installation and notifying Fleet ops, and must retain the documented #222 merge seam. The publishing layer's command-surface boundary and disabled remote Git transports remain explicit limitations above.


## Round 3: CI at `3e37f8017`

The three supplied failures were confirmed in the local Python 3.12 shard-2 log (2,571 passes, 13 skips, three failures). No additional shard logs for this head were present in the supplied review directory. No network remote was contacted.

1. Capability text had been appended inside `render.notice_header`, changing C-15.1's first-line format. `notice_header` now returns only the contracted header. Shared `notice_intro` adds known capability and publication evidence on the following line; the daemon and hook fallback both use it. The e2e header assertion remains unchanged and now checks the separate capability line. Unit regressions cover all capabilities and publication-only evidence; fake notice expectations check the complete lines, and the independent invariant no longer permits capability suffixes in the header.
2. The prune proofs still asserted schema 7. They now compare the store and report with `SCHEMA_VERSION`, retaining exact table, foreign-key, trigger, reader, partition, backup and restore checks. Prune already used `SELECT *` fingerprints, whole-row decision dumps and a complete `VACUUM INTO` copy, so the four launch columns needed no special copying or partitioning code. Its preflight now also compares every expected column against the current schema DDL before opening a writer or creating backups. A regression enumerates every ADD COLUMN migration, removes that column from a disposable current-version store, and requires refusal without changing the store. New migration columns therefore enter the regression automatically. Separate cases perturb each launch column and require its hash to change and its bytes to survive pruning and the backup copy; the partition/restore property also seeds nondefault launch evidence.
3. The D-SH1 migration numbers remain 8 and 9. Their only numeric definitions are `SHELL_REQUIREMENT_MIGRATION` and `LAUNCH_CAPABILITY_MIGRATION` in `subfleet/store.py`; migration keys and the capability backfill use those constants, and `SCHEMA_VERSION` follows the capability migration. Migration tests reconstruct previous schemas by reversing the migration table's ADD COLUMN statements, rather than removing literal SQL declarations or inserting literal version numbers. A regression simulates a competing migration taking the first slot and checks the capability backfill after both D-SH1 steps move.

### Renumbering at the release merge

If #131 merges first, change the two constants in `subfleet/store.py` to 9 and 10 and retain #131's migration at its allocated number. No D-SH1 migration keys, backfill condition, schema assertion, test fixture SQL, or test filenames need edits. `SCHEMA_VERSION` follows automatically. `store_schema.sql` already describes the resulting columns and does not need a numbering edit. This report's historical version descriptions may be updated for clarity.

### Notice reader audit

- `subfleet/hooks.py`: `render_pending` delivers the complete multiline text; `job_summary` now shares `notice_intro`; offline hook reads return the text unchanged. SessionStart, UserPromptSubmit and PostToolUse keep their existing delivery and marking behavior.
- `subfleet/cli.py`: `format_notices` deliberately displays the first line only and has a new multiline regression. `format_job` displays full notice text. `wait_jobs` / `_wait_summary` render structured job/attempt fields rather than parsing notice text.
- `subfleet/offline.py` and `subfleet/store.py`: show/list/notices readers retain the full text; notice fingerprints hash the full text. `subfleet/notify_push.py::offer` forwards the complete text without parsing it. Import and retention retain the text as row data. Daemon append-only notice annotations already start new lines.
- Repository hook scripts and entry points (`bin/`, `private/guard/never-rules-hook.sh`, shell scripts under `tools/`, and workflow files): no reader of the C-15.1 text header. The guard hook parses command input, independently of completion notices.
- `app/Sources/`: no C-15.1 header parser. `TaskNotification` parses Claude's separate XML task-notification format, and Timeline/UIConversationRecovery use that structured result.
- `tests/fake/notice_invariant.py` parses the first line independently of production rendering and now rejects an appended capability as part of the out value.

### Round-3 verification status

Blocked before collection: the exact locked `uv run` attempt acquired the shared lock, then stopped before pytest collection because the sandbox cannot write `/Users/maxghenis/.cache/uv/sdists-v9/.git`. `uv sync` and `uv run --no-sync` fail at the same default-cache initialization. The default cache was not redirected or cleaned. A copy of an existing uv-built environment is local to this workspace; missing locked dependencies were copied read-only from the default uv cache. Permission to invoke that environment's Python directly under the same shared lock has been requested and is pending. No pytest result is claimed. The user has not authorized the fallback, so it was not run.

All changed Python files pass AST parsing, and `git diff --check` passes. Regression-only commits precede implementation commits. The red regression replay at `3e37f8017` and the green suite remain unexecuted; the prepared source copy was removed during cleanup.

Every requested file remains unrun (zero executed; no collection occurred):

| File | Executed | Status |
| --- | ---: | --- |
| `tests/unit/test_prune_217.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/unit/test_notice_rendering.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/fake/test_notice_header.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/unit/test_cli.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/unit/test_offline.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/unit/test_store_migration_2.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/unit/test_store_readers.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/unit/test_store_properties.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/fake/test_state_contract.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/fake/test_review_needs_shell.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/fake/test_review_launch_permissions.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/unit/test_review_publish_guard.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_codex_turn_completed_e2e.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_conversation_titles_e2e.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_conversation_wakes_e2e.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_conversations.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_guard_and_isolation.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_http_isolation.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_legacy_cockpit.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_milestone1_codex.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_milestone2_claude.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_notice_job_state.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_recovery.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_reserve.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_review_r2_strand_e2e.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_review_r3_strand_timing_e2e.py` | 0 | unrun: uv cache sandbox restriction |
| `tests/e2e/test_stop_bound.py` | 0 | unrun: uv cache sandbox restriction |

Round-3 implementation commits are `e43108c15` (notice lines), `7798a5b2d` (prune column validation), and `006e6af96` (migration constants). Regression commits are `32218fc70`, `92f844876`, `42c76110d` and fixture refinement `c94009ac3`. All use the required co-author trailer. The shared index and `.git` pointer are read-only, so commits live in workspace-local `.git-local` metadata on `fix/review-needs-shell-217`, preserving ancestry at `3e37f8017`. Nothing was pushed. No sub-agent, live store, daemon, policy, or caller checkout was modified. No test runner remains running, and no `.hypothesis` directory remains. Temporary test storage and the regression source copy were removed with `shutil.rmtree`; no logs, JSON evidence or bundles were staged.

## Round 4: public refusal integration and executed verification

Base: `a0d24b46c1a3`, including the hub's merge of #181. The shared Git index is read-only, so commits use `.git-local` metadata on `fix/review-needs-shell-217`, preserving the base's ancestry. Nothing was pushed or written to the caller checkout.

The baseline reproduced all nine reported failures in 215.63 seconds (344 passed):

| File | Passed | Failed |
| --- | ---: | ---: |
| `tests/unit/test_handler_refusals.py` | 314 | 1 |
| `tests/fake/test_review_needs_shell.py` | 21 | 2 |
| `tests/fake/test_review_launch_permissions.py` | 9 | 6 |

Causes and fixes:

- **One refusal-vocabulary failure, two constructors:** unknown pinned models and unsatisfiable pinned shell requirements had no public reason. Commit `1c05d7803` adds `unknown-pinned-model` and `needs-shell`, templates, and model/sandbox details. Model names must match a bounded shape and pass the existing credential check; sandbox values come from `Sandbox`. Both templates have fallbacks that name no value and retain their repairs. Direct callers keep the raw diagnostics and policy model list; public messages use checked details.
- **Eight stale notice assertions:** the Opus cases (2) and read-only policy review cases (6) still expected capability/publication evidence in the first line after round 3 moved it to its own line. Comparing `f3b287771` with `a0d24b46c` confirms #181 changed neither the renderer nor those two fake test files. Tests now check the exact C-15.1 header, capability/publication on line two, and unchanged deliverable/output bytes. Production rendering already matched the design.
- **Two additional state-contract failures:** the acceptance/cancellation parameters still unpacked header/summary/retained output without the capability line, counting the attempt summary as retained output. Their state and acceptance assertions passed. Commit `85d98603d` reads the separate capability line, removes permissive header-suffix handling, and checks unchanged deliverable bytes for both outcomes. All 37 state cases then passed.

Tests-first commit `fdaffe9e5` adds wire/detail regressions and corrects the eight notice cases. Its focused red run was 18 failed, 8 passed, 338 deselected in 71.87 seconds: the eight corrected notice cases passed while the missing public reasons failed. Credential cases use lowercase tokens that fit the model-name shape, exercising credential rejection too. The full three-file green rerun passed 368 in 106.03 seconds. Notice/CLI passed 312 in 127.80 seconds. Store/state initially passed 112 and failed the two stale assertions in 223.31 seconds; the unchanged four unit files retain their 77 passes, and the full state rerun passed 37 in 35.64 seconds. Refusal/publication passed 396 in 60.48 seconds. The e2e notice assertions were also checked in source and already follow the separate-line design; the remaining two-line fake notice case has no attempt and correctly has no capability line.

Final results below are the latest full-file outcomes, without counting red runs or retries twice: **1,192 passed, 105 skipped, zero unresolved failures. All 32 requested files were invoked; no files remain unrun.** The 1,190 unit/fake cases passed. E2e contributed two passing stop-lock observer cases using owned child processes. All 105 other e2e cases skipped at the existing C-5.3 fixture: `macOS boot identity is unavailable` (permitted ps/sysctl inspection is required). Their daemon/process behavior was not exercised in this sandbox.

| File | Passed | Skipped |
| --- | ---: | ---: |
| `tests/unit/test_handler_refusals.py` | 330 | 0 |
| `tests/fake/test_review_needs_shell.py` | 23 | 0 |
| `tests/fake/test_review_launch_permissions.py` | 15 | 0 |
| `tests/unit/test_notice_rendering.py` | 26 | 0 |
| `tests/fake/test_notice_header.py` | 23 | 0 |
| `tests/unit/test_cli.py` | 230 | 0 |
| `tests/unit/test_offline.py` | 33 | 0 |
| `tests/unit/test_prune_217.py` | 27 | 0 |
| `tests/unit/test_store_migration_2.py` | 21 | 0 |
| `tests/unit/test_store_readers.py` | 26 | 0 |
| `tests/unit/test_store_properties.py` | 3 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 |
| `tests/unit/test_review_publish_guard.py` | 66 | 0 |
| `tests/e2e/test_codex_turn_completed_e2e.py` | 0 | 3 |
| `tests/e2e/test_conversation_titles_e2e.py` | 0 | 9 |
| `tests/e2e/test_conversation_wakes_e2e.py` | 0 | 4 |
| `tests/e2e/test_conversations.py` | 0 | 44 |
| `tests/e2e/test_guard_and_isolation.py` | 0 | 5 |
| `tests/e2e/test_http_isolation.py` | 0 | 1 |
| `tests/e2e/test_legacy_cockpit.py` | 0 | 1 |
| `tests/e2e/test_milestone1_codex.py` | 0 | 9 |
| `tests/e2e/test_milestone2_claude.py` | 0 | 7 |
| `tests/e2e/test_notice_job_state.py` | 0 | 1 |
| `tests/e2e/test_recovery.py` | 0 | 2 |
| `tests/e2e/test_reserve.py` | 0 | 4 |
| `tests/e2e/test_review_r2_strand_e2e.py` | 0 | 2 |
| `tests/e2e/test_review_r3_strand_timing_e2e.py` | 0 | 2 |
| `tests/e2e/test_stop_bound.py` | 2 | 1 |
| `tests/e2e/test_suggestions_e2e.py` | 0 | 2 |
| `tests/e2e/test_suggestions_r1b_peer_e2e.py` | 0 | 8 |

Every pytest invocation used its own foreground shared-lock hold, without a lock-wait timeout, `env -u SUBFLEET_RETENTION_DORMANT`, no pytest cache, one worker, scratch Hypothesis storage, and the bounded `ci` profile. The default uv cache is not writable in this sandbox; all runs used `UV_CACHE_DIR` inside the permitted task scratch directory. Dependencies installed there successfully. The longest reported pytest phase was 223.31 seconds; lock waits were separate (the stop-bound invocation waited about 41 minutes, then reported 13.23 seconds of pytest execution). The last invocation was queued at 06:47 UTC and completed at 07:03 UTC, before the conservative 07:27 UTC cutoff for starting holds.

Task scratch storage, including the uv cache and Hypothesis data, was removed with the required `shutil.rmtree` command; absence of both the scratch directory and root `.hypothesis` was checked. Every foreground test runner exited. No logs, XML, JSON evidence, or bundles are staged. All commits carry the required co-author trailer. No shared Git metadata, live service/state, or caller checkout was modified; nothing was pushed, and no sub-agent was used.

## Round 5: CI notice positions, conditional permissions, and schema expectations

Base: `6421af9b7856`. Work began at 07:34 UTC on 2026-10-16; no new test hold may start after the conservative 12:04 UTC cutoff. The shared Git index is read-only (the attempted named-file add failed with `Operation not permitted`), so commits use workspace-local `.git-local` metadata on `fix/review-needs-shell-217`, preserving the base ancestry. The caller checkout and shared metadata remain untouched; nothing is pushed.

The hub's corrected notice decision supersedes the line-two interpretation in rounds 3 and 4: the C-15.1 header stays first, the summary stays second, every later body line keeps its position, and recorded capability/publication evidence goes last. Tests-first commits `714348099` and `aa7903b07` pin this order for daemon notices, hook fallback, retained output, salvage/retry notices, and later push/export annotations. Commit `dfe4a141f` adds regressions for handoff submission/dry run in committed, unborn and non-git repositories, explicit overrides and wildcard permissions, and each scheduler permission reader (demand, lane refusal and model reason).

Source audit: `cli.format_notices` reads only the first-line header; `hooks.render_pending` passes through complete notice text and `hooks.job_summary` builds the fallback. The daemon's `_notice`, `_export_error` and `_push_job` write or annotate notices. Offline/store/protocol notice readers retain the text; conversation wakes use structured job rows. App `TaskNotification` parses its own XML fields and `Timeline` displays notice text, without indexing a C-15.1 summary. Searches of the package, hooks, CLI, app, bin and tools found no production summary reader using a numeric line index or header/summary unpacking. The unrelated `lanes_transfer._indent_of` slice reads roster formatting. All indexed test readers were audited: notice-header, notice-rendering, review-needs-shell, review-launch-permissions, state-contract, salvage-finalization, workspace-transient, record-secrecy (through the salvage helper), and the e2e notice-job-state assertion. The salvage/workspace helper checks capability is final before exposing the unchanged body to existing assertions.

The initial hold installed dependencies in task scratch and stopped during collection because a test import edit was misindented. Commit `7a6d974a1` corrects that edit; every unit/fake Python source then passed AST parsing. The corrected tests-first red run on CPython 3.14.7 freethreaded completed **103 failed, 378 passed in 371.61 seconds**. Those failures cover the notice layout and later annotations, handoff/scheduler policy resolution, and the three stale schema/policy assertions.

Implementation commits:

- `6175077b0`: one complete notice renderer puts capability last in the daemon and hook fallback. Later export/push annotations go before that footer; the summary and all other preceding lines retain their order. C-15.1's contradictory header/line-two descriptions are corrected.
- `49f5efcfb`: handoff and the scheduler's demand, lane-refusal and model-reason paths call `policy.resolve_sandbox`. A handoff probes committed Git state only for conditional authority; scheduler routing uses the job's recorded commit and isolation/in-place/gate context. The daemon already delegates its conversion to the same resolver; its remaining raw permission read only decides whether a Git probe is needed.
- `66aa7602c` and `4df38b232`: source inspection found that the new handoff Git probe could otherwise escape the CLI's error handler on a transient Git failure. Tests precede the correction. It now returns an actionable operational handoff error before prompt staging/submission, omits private Git output, and retains the explicit read-only escape. Explicit sandbox choices do not need this conditional probe.
- `70314d9ed`: the schema-six host-push test follows `SCHEMA_VERSION`; the authorization migration fixture expects `needs_shell` and proves its default is false. The provider-cap test still checks exact input-byte preservation and its actual SHA-256, without pinning a prior release's policy hash. Other literal schema values belong to deliberately historical fixtures or the independent conversation-store schema.

The first green focused hold used CPython 3.12.14 and passed **656 tests in 298.66 seconds**, with **304 seconds of lock occupancy**, no failures or skips:

| File | Passed |
| --- | ---: |
| `tests/fake/test_notice_header.py` | 24 |
| `tests/fake/test_policy_sandbox.py` | 25 |
| `tests/fake/test_record_secrecy.py` | 44 |
| `tests/fake/test_review_launch_permissions.py` | 15 |
| `tests/fake/test_review_needs_shell.py` | 23 |
| `tests/fake/test_salvage_finalization.py` | 47 |
| `tests/fake/test_state_contract.py` | 37 |
| `tests/fake/test_workspace_transient.py` | 24 |
| `tests/unit/test_notice_rendering.py` | 35 |
| `tests/unit/test_policy.py` | 186 |
| `tests/unit/test_policy_sandbox_resolution.py` | 42 |
| `tests/unit/test_policy_support.py` | 14 |
| `tests/unit/test_sessions_handoff.py` | 140 |

Latest recorded results at the cutoff (report written 12:06 UTC on 2026-10-16): **13,260 passed, 97 skipped, two xfailed, five pre-fix platform-test failures and zero errors** in the latest complete result for each of **401 / 401 scoped files**. The scope is all 400 fake/unit files plus the edited e2e notice file. The last two fixture corrections are committed but unverified because their queued replay never acquired the lock before the cutoff. Repeated executions are counted once. When a hold stopped early, only files whose complete collected tests finished their required phases are credited.

| Hold | Passed | Skipped | Xfailed | Failed/errors | Lock seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| focus-1 | 656 | 0 | 0 | 0 | 304 |
| focus-2 | 816 | 1 | 0 | 0 | 179 |
| full-01 | 1962 | 44 | 1 | 0 | 346 |
| small-01 | 491 | 20 | 1 | 0 | 309 |
| diag-02 | 1754 | 8 | 0 | 3 | 714 |
| finish-01 | 1065 | 4 | 0 | 1 | 347 |
| complete-01 (recovered complete files) | 3046 | 11 | 0 | 1 | 1161 |
| last-01 | 2181 | 4 | 0 | 2 | 653 |
| final-01 | 1900 | 5 | 0 | 5 | 859 |

All verification holds used CPython 3.12.14. 61 latest file outcomes still rely on complete phase records rather than a completed pytest session; their rows identify complete phase records.


One earlier parallel hold reported three failures and stalled near completion. It occupied the lock from 09:04:33 to 09:25:03 UTC: **20 minutes 30 seconds**, exceeding the requested bound by 30 seconds. Direct inspection/signalling of the owned controller was denied; the tool interrupt ended the execution and the master PID was confirmed gone. No complete JUnit document survived, so none of its partial passes are credited. The diagnostic replay classified and reproduced all three failures; later complete-file replay cleared them. All other recorded completed holds are shown above.

A later hold was interrupted at 11:09:42 UTC after **19 minutes 21 seconds**, with 3,069 of 3,070 cases finished and one bounded scheduler property still running. Its controller was confirmed gone. Per-phase records recovered complete results for 62 files; no partial result for `tests/unit/test_unadmittable_pin.py` is credited. The table labels this as recovered complete-file evidence rather than a completed pytest run. The unfinished file and the hook replay were moved to the front of the remaining plan, with four workers and one-node scheduling chunks to distribute slow properties earlier.

The wider sweep found additional test-fixture prerequisites. Importer synthetic live/lost records depended on real host process inspection, which this sandbox denies. Tests-first commit `9e04065a5` adds deterministic fixture and fail-closed regressions; `3d60a3aff` supplies identities only for two reserved synthetic PIDs, delegates other PIDs to the real inspector, and guards only the real-child ownership-marker branch. The complete importer replay passed 149 and skipped one; production importer behavior stays fail-closed. The QoS tool test also assumed an existing `/bin/ps` could execute. Commit `cb2655495` probes `ps -M` permission before starting the owned clamped child; the actual thread-count and priority assertions remain active when inspection is permitted. The hook command test likewise completed its notice-delivery assertions before its real worker census hit denied `/bin/ps` execution. Commit `9bb0c2f00` adds a narrow permission prerequisite to that census, retaining the actual no-worker assertion wherever permitted. The doctor table tests had the same dependency for synthetic live/dead lock identities. Tests-first commit `7f3e787ff` preserves UNKNOWN for denied inspection; `ddf43cd9b` supplies deterministic boot/start probes to the live/dead fixtures and asserts the recorded PID is inspected, while exercising the real identity comparison and leaving production behavior unchanged. The final broad hold found four stack-dump probes with undeclared boot/process prerequisites and one kernel-ceiling probe assuming sysctl access. Commit `700d45b8a` guards real-child signal checks and supplies a synthetic identity only to the handler-order unit, preserving its actual registration/lock assertions. Commit `4a5379362` independently probes sysctl permission before comparing the descriptor helper with the kernel value; an available probe still requires the helper to return that exact positive integer.

The phase-recovered files and final fixture corrections were selected for a final verification hold. It remained queued through the cutoff and did not start; the exact unrun replay paths are listed below.

The hub failed-test list was reread and contained no additional CI failures beyond those addressed here. Each foreground pytest invocation waits for the shared lock without a wait timeout, removes `SUBFLEET_RETENTION_DORMANT`, uses bounded Hypothesis ci settings and task-local uv/Hypothesis storage, and starts at most four workers. No new hold is requested at or after 12:04 UTC; the acquired-lock runner also checks that cutoff before starting pytest.

Skip and expected-failure reasons across the recorded results:

- 4: Offline wheel-matrix tests: sortedcontainers 2.4.0 is absent from the scratch uv cache.
- 1: C-5.3 e2e requires permitted ps/sysctl inspection: macOS boot identity is unavailable
- 73: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- 1: C-6.10: the wait is keyed on its kind as well as its verdict, so a job that alternates between lease-held and slot-kept records one verdict again on every alternation (not fixed here: the probe-pending key has no decision to key on)
- 1: P3-6: filesystem identity has no wall-clock bound for a hung mount
- 1: cross-version differential needs the CPython 3.14 oracle source
- 1: explicit local-repo benchmark
- 1: hdiutil create Case-sensitive HFS+: hdiutil: create failed - Device not configured
- 1: hdiutil create ExFAT: hdiutil: create failed - Device not configured
- 1: hdiutil create HFS+: hdiutil: create failed - Device not configured
- 1: hook no-worker check requires permitted process inspection
- 1: kern.ipc.somaxconn is 0: no room above the old queue of 64
- 11: phase recovery: prerequisite reason unavailable without JUnit
- 1: sandbox denies process inspection; bounded turn unit tests cover this seam

Files never invoked: none. **The final 63-file replay did not acquire the lock before the 12:04 UTC cutoff and was cancelled; its foreground runner exited 130 without starting pytest.** Two files have committed fixture/prerequisite corrections awaiting post-fix replay: `tests/fake/test_daemon_stacks.py` (four pre-fix failures) and `tests/unit/test_descriptors.py` (one pre-fix failure). The other 61 files have complete per-test phase evidence from the interrupted hold, but their planned completed-session verification was not run. The five failures in the totals above therefore describe the last pre-fix executions, not a post-fix result. All 16 files explicitly requested in the five initial regression groups passed: **1,307 passed, zero skipped and zero failures**. No new hold was started at or after the cutoff.

### Unrun replays at the cutoff

| File | Unrun work |
| --- | --- |
| `tests/fake/test_daemon_stacks.py` | Post-fix whole-file replay |
| `tests/unit/test_descriptors.py` | Post-fix whole-file replay |
| `tests/unit/test_retention_salvage_properties.py` | Completed-session verification of complete phase records |
| `tests/unit/test_stop_watchdog.py` | Completed-session verification of complete phase records |
| `tests/fake/test_detached_retention_fence.py` | Completed-session verification of complete phase records |
| `tests/unit/test_compat.py` | Completed-session verification of complete phase records |
| `tests/unit/test_store_properties.py` | Completed-session verification of complete phase records |
| `tests/unit/test_retention_regenerable_properties.py` | Completed-session verification of complete phase records |
| `tests/unit/test_sessions_mirror_safety.py` | Completed-session verification of complete phase records |
| `tests/unit/test_turn_overlap.py` | Completed-session verification of complete phase records |
| `tests/unit/test_retention_worktrees.py` | Completed-session verification of complete phase records |
| `tests/unit/test_stop_alarm.py` | Completed-session verification of complete phase records |
| `tests/fake/test_lane_pilot.py` | Completed-session verification of complete phase records |
| `tests/unit/test_scheduler_tier_preferences.py` | Completed-session verification of complete phase records |
| `tests/unit/test_legacy_hold.py` | Completed-session verification of complete phase records |
| `tests/unit/test_review130_pass.py` | Completed-session verification of complete phase records |
| `tests/unit/test_conversation_catalog_lifecycle.py` | Completed-session verification of complete phase records |
| `tests/unit/test_salvage.py` | Completed-session verification of complete phase records |
| `tests/unit/test_conversation_steer_service.py` | Completed-session verification of complete phase records |
| `tests/unit/test_host_push_metadata.py` | Completed-session verification of complete phase records |
| `tests/unit/test_census_responsibility_failures.py` | Completed-session verification of complete phase records |
| `tests/unit/test_operator_hold_closures.py` | Completed-session verification of complete phase records |
| `tests/unit/test_round10_refusal_context.py` | Completed-session verification of complete phase records |
| `tests/unit/test_suggestion_secret_properties.py` | Completed-session verification of complete phase records |
| `tests/unit/test_suggestions_review_boundaries.py` | Completed-session verification of complete phase records |
| `tests/unit/test_host_push_export.py` | Completed-session verification of complete phase records |
| `tests/unit/test_store_readers.py` | Completed-session verification of complete phase records |
| `tests/unit/test_review_r3_measure.py` | Completed-session verification of complete phase records |
| `tests/fake/test_native_maintenance_startup.py` | Completed-session verification of complete phase records |
| `tests/unit/test_sessions_cli.py` | Completed-session verification of complete phase records |
| `tests/fake/test_provider_qos.py` | Completed-session verification of complete phase records |
| `tests/unit/test_descriptors_desktop.py` | Completed-session verification of complete phase records |
| `tests/unit/test_host_push_validated_top.py` | Completed-session verification of complete phase records |
| `tests/unit/test_guardian_qos.py` | Completed-session verification of complete phase records |
| `tests/unit/test_codex_ca_bundle.py` | Completed-session verification of complete phase records |
| `tests/unit/test_status_json.py` | Completed-session verification of complete phase records |
| `tests/unit/test_prune_decision_fingerprints.py` | Completed-session verification of complete phase records |
| `tests/unit/test_lanes_transfer.py` | Completed-session verification of complete phase records |
| `tests/unit/test_desktop_in_use.py` | Completed-session verification of complete phase records |
| `tests/fake/test_main_refusal_write_dir.py` | Completed-session verification of complete phase records |
| `tests/fake/test_status_payload.py` | Completed-session verification of complete phase records |
| `tests/unit/test_continuation_r2.py` | Completed-session verification of complete phase records |
| `tests/unit/test_sessions_revive_reads.py` | Completed-session verification of complete phase records |
| `tests/unit/test_sessions_revive.py` | Completed-session verification of complete phase records |
| `tests/unit/test_policy_disk.py` | Completed-session verification of complete phase records |
| `tests/fake/test_enrollment_guardian.py` | Completed-session verification of complete phase records |
| `tests/fake/test_finalization_replay.py` | Completed-session verification of complete phase records |
| `tests/unit/test_doctor_ca_bundle.py` | Completed-session verification of complete phase records |
| `tests/unit/test_prune_decision_backup_verification.py` | Completed-session verification of complete phase records |
| `tests/unit/test_mcp_cli_contract.py` | Completed-session verification of complete phase records |
| `tests/unit/test_codex_turn.py` | Completed-session verification of complete phase records |
| `tests/unit/test_picker.py` | Completed-session verification of complete phase records |
| `tests/unit/test_review_pr127_pr_wakes.py` | Completed-session verification of complete phase records |
| `tests/fake/test_routing_tier_preferences.py` | Completed-session verification of complete phase records |
| `tests/unit/test_daemon_qos_tools.py` | Completed-session verification of complete phase records |
| `tests/fake/test_picker_native.py` | Completed-session verification of complete phase records |
| `tests/unit/test_conversation_state_reads.py` | Completed-session verification of complete phase records |
| `tests/unit/test_guardian.py` | Completed-session verification of complete phase records |
| `tests/unit/test_salvage_optional_transient.py` | Completed-session verification of complete phase records |
| `tests/unit/test_claude_identity.py` | Completed-session verification of complete phase records |
| `tests/unit/test_pr124_review_r2_fixes.py` | Completed-session verification of complete phase records |
| `tests/unit/test_scoped_stream_capacity.py` | Completed-session verification of complete phase records |
| `tests/unit/test_store_snapshot_plans.py` | Completed-session verification of complete phase records |

Commits before this final report commit (all carry the requested coauthor trailer):

- `714348099` test: preserve notice body positions before final capability evidence
- `aa7903b07` test: keep capability last after host push annotations
- `dfe4a141f` test: resolve conditional permissions in handoff and shell routing
- `7a6d974a1` test: correct local imports in workspace notice regressions
- `d63b97789` docs: record round-five regressions, reader audit and validation checkpoint
- `6175077b0` fix: append capability after the complete notice body
- `49f5efcfb` fix: resolve conditional policy permissions before handoff and shell routing
- `70314d9ed` test: follow the current store schema and policy bytes
- `66aa7602c` test: cover conditional handoff Git probe failures and explicit escapes
- `4df38b232` fix: report conditional handoff probe errors without exposing Git output
- `53f61070a` docs: record round-five fixes and 656 green focused checks
- `784467bed` docs: record focused checks, full-sweep progress and interrupted hold
- `9e04065a5` test: cover importer fixture identities with unavailable process inspection
- `3d60a3aff` test: isolate synthetic importer identities from host inspection
- `cb2655495` test: guard real QoS measurement when process inspection is denied
- `33ac652cd` docs: record importer replay and bounded full-sweep diagnostics
- `9bb0c2f00` test: guard the hook worker census when ps execution is denied
- `23b81c735` docs: record complete-file recovery before the final two holds
- `7f3e787ff` test: keep doctor lock identity unknown when inspection is denied
- `ddf43cd9b` test: make doctor lock identities independent of host inspection
- `700d45b8a` test: declare process-inspection prerequisites for stack-dump probes
- `4a5379362` test: require sysctl permission for the real kernel ceiling probe

- `34844dc46` docs: finalize round-five fixes, per-file counts and cutoff status

- `07df2803d` docs: verify scratch cleanup after restoring the read-only fixture

### Round-five per-file counts

| File | Passed | Skipped | Xfailed | Failed/errors | Latest evidence / pending work |
| --- | ---: | ---: | ---: | ---: | --- |
| `tests/e2e/test_notice_job_state.py` | 0 | 1 | 0 | 0 | focus-2 |
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_admission_priority.py` | 32 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_admission_properties.py` | 27 | 0 | 1 | 0 | small-01 |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_admission_route_isolation.py` | 90 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_admission_stateful.py` | 2 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_batch_label.py` | 9 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_canonical_identity.py` | 24 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_canonical_identity_fix3.py` | 17 | 0 | 1 | 0 | full-01 |
| `tests/fake/test_canonical_identity_fix4.py` | 11 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_canonical_identity_fix5.py` | 13 | 1 | 0 | 0 | small-01 |
| `tests/fake/test_capacity_wait_backoff.py` | 30 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_census_escapers_daemon.py` | 0 | 4 | 0 | 0 | small-01 |
| `tests/fake/test_census_recycled_pid_daemon.py` | 8 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_codex_ca_bundle_fake.py` | 0 | 6 | 0 | 0 | small-01 |
| `tests/fake/test_codex_turn_completed_fake.py` | 105 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_conversation_pools.py` | 2 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_conversation_wakes_fake.py` | 4 | 0 | 0 | 0 | small-01 |
| `tests/fake/test_crash_matrix.py` | 0 | 9 | 0 | 0 | small-01 |
| `tests/fake/test_daemon_contract.py` | 0 | 21 | 0 | 0 | full-01 |
| `tests/fake/test_daemon_descriptors.py` | 12 | 1 | 0 | 0 | final-01 |
| `tests/fake/test_daemon_launch.py` | 9 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_daemon_stacks.py` | 1 | 1 | 0 | 4 | final-01; pre-fix failures, post-fix replay unrun |
| `tests/fake/test_descriptor_exhaustion.py` | 0 | 2 | 0 | 0 | finish-01 |
| `tests/fake/test_detached_cwd_pins.py` | 18 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_detached_retention_fence.py` | 89 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_enrollment_guardian.py` | 0 | 1 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_fake_provider.py` | 13 | 0 | 0 | 0 | final-01 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_folder_spelling.py` | 7 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_frozen_stream_nonregular.py` | 32 | 0 | 0 | 0 | final-01 |
| `tests/fake/test_gate_end_to_end.py` | 10 | 1 | 0 | 0 | finish-01 |
| `tests/fake/test_identity_rollback.py` | 5 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_idle_cost.py` | 0 | 2 | 0 | 0 | last-01 |
| `tests/fake/test_interactive_claude_titles.py` | 6 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_interactive_steer.py` | 13 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_lane_fault.py` | 20 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_lane_pilot.py` | 24 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_lanes_enroll.py` | 15 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_lanes_reenroll.py` | 19 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_lanes_reenroll_keychain.py` | 1 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_launch_gates_above_stdio.py` | 0 | 9 | 0 | 0 | full-01 |
| `tests/fake/test_lost_acknowledgement.py` | 0 | 3 | 0 | 0 | full-01 |
| `tests/fake/test_main_refusal_write_dir.py` | 5 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_mcp_job_contract.py` | 12 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_native_maintenance_startup.py` | 41 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_notice_delivery.py` | 15 | 0 | 0 | 0 | final-01 |
| `tests/fake/test_notice_header.py` | 24 | 0 | 0 | 0 | focus-1 |
| `tests/fake/test_picker_native.py` | 3 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_policy_sandbox.py` | 25 | 0 | 0 | 0 | focus-1 |
| `tests/fake/test_probe_cancel.py` | 24 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_probe_turn.py` | 43 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_prompt_seam.py` | 8 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_provider_active_cap_fake.py` | 60 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_provider_cap_review_evidence.py` | 5 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_provider_qos.py` | 0 | 2 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_record_secrecy.py` | 44 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_request_routing.py` | 3 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_reserve_authorization.py` | 12 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_reserve_authorization_validation.py` | 11 | 0 | 0 | 0 | final-01 |
| `tests/fake/test_reset_credit_incident.py` | 19 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_resume_cli.py` | 0 | 1 | 0 | 0 | diag-02 |
| `tests/fake/test_resume_contract.py` | 23 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_review130_ab.py` | 12 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_review_launch_permissions.py` | 15 | 0 | 0 | 0 | focus-1 |
| `tests/fake/test_review_needs_shell.py` | 23 | 0 | 0 | 0 | focus-1 |
| `tests/fake/test_review_r185_adversarial.py` | 1 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_route_view.py` | 8 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_routing_end_to_end.py` | 14 | 2 | 0 | 0 | diag-02 |
| `tests/fake/test_routing_tier_preferences.py` | 5 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_salvage_adversarial.py` | 8 | 0 | 0 | 0 | final-01 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_sessions_end_to_end.py` | 27 | 6 | 0 | 0 | full-01 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | focus-1 |
| `tests/fake/test_state_fixture_processes.py` | 4 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_status_payload.py` | 2 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/fake/test_suggestions_fake.py` | 1 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_tier_hold_by_demand.py` | 27 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_timers_busy_admission.py` | 1 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_timers_end_to_end.py` | 6 | 1 | 0 | 0 | diag-02 |
| `tests/fake/test_turn_listing.py` | 9 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_turn_retention_cwd.py` | 6 | 0 | 0 | 0 | final-01 |
| `tests/fake/test_turn_retention_cwd_properties.py` | 1 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_turn_retention_job_directory.py` | 2 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_turn_retention_job_properties.py` | 1 | 0 | 0 | 0 | final-01 |
| `tests/fake/test_turn_retention_quarantine.py` | 10 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_turn_trees.py` | 12 | 0 | 0 | 0 | final-01 |
| `tests/fake/test_turn_wait_reasons.py` | 7 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_unadmittable_pin_admission.py` | 24 | 0 | 0 | 0 | full-01 |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_unadmittable_pin_review.py` | 7 | 0 | 0 | 0 | diag-02 |
| `tests/fake/test_workspace_contract.py` | 8 | 0 | 0 | 0 | last-01 |
| `tests/fake/test_workspace_enospc.py` | 1 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_workspace_transient.py` | 24 | 0 | 0 | 0 | focus-1 |
| `tests/fake/test_writable_fanout.py` | 28 | 0 | 0 | 0 | finish-01 |
| `tests/fake/test_writer_retention_fence.py` | 23 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_capacity_view.py` | 20 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_census_procargs.py` | 52 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_ci_retry_known_flakes.py` | 70 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_claude_adapter.py` | 172 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_claude_cards.py` | 96 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_claude_identity.py` | 30 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_claude_stream.py` | 60 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_claude_turn.py` | 61 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_claude_usage.py` | 28 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_cli.py` | 230 | 0 | 0 | 0 | focus-2 |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_codex_adapter.py` | 93 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_codex_probe.py` | 55 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_codex_shim.py` | 31 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_codex_turn.py` | 58 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_compat.py` | 1615 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_conversation_history.py` | 22 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_conversation_service.py` | 98 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_conversation_store.py` | 32 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_conversation_titles.py` | 89 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_conversation_wakes.py` | 21 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_daemon_connections.py` | 30 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_daemon_integration.py` | 7 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_daemon_qos_tools.py` | 3 | 1 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_daemon_settle.py` | 43 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_daemon_verbs.py` | 15 | 5 | 0 | 0 | full-01 |
| `tests/unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_descriptors.py` | 15 | 0 | 0 | 1 | final-01; pre-fix failures, post-fix replay unrun |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_disk_admission.py` | 27 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_disk_floor.py` | 60 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_doctor.py` | 45 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_empty_provider_results.py` | 334 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_filesystem_identity_agreement.py` | 10 | 3 | 0 | 0 | final-01 |
| `tests/unit/test_folders.py` | 23 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_forget_paced.py` | 2 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_fresh_until.py` | 1 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_frozen_provider_streams.py` | 48 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_gate_admission.py` | 24 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_gate_merge.py` | 59 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_gate_replay.py` | 23 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_gate_revision.py` | 16 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_gate_service.py` | 23 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_gate_verdict.py` | 45 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_guard_trust.py` | 98 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_guardian.py` | 12 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_handler_refusals.py` | 330 | 0 | 0 | 0 | focus-2 |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_hook_cost.py` | 3 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_hooks.py` | 66 | 1 | 0 | 0 | last-01 |
| `tests/unit/test_hooks_native_guard.py` | 18 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_host_push.py` | 120 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_host_push_build.py` | 10 | 1 | 0 | 0 | last-01 |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_host_push_export.py` | 10 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_host_push_mutations.py` | 24 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_host_push_names.py` | 13 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_host_push_storage_retry_budget.py` | 60 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_host_push_validated_top.py` | 8 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_importer.py` | 149 | 1 | 0 | 0 | finish-01 |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_lanes_transfer.py` | 79 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_legacy_hold.py` | 50 | 6 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_limit_from_model_text.py` | 95 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_lockwatch.py` | 20 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_notice_rendering.py` | 35 | 0 | 0 | 0 | focus-1 |
| `tests/unit/test_notify_push.py` | 31 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_operator_notices.py` | 79 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_person_boundary.py` | 97 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_person_boundary_identity.py` | 25 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_picker.py` | 39 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_policy.py` | 186 | 0 | 0 | 0 | focus-1 |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_policy_sandbox_resolution.py` | 42 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_procs.py` | 47 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_provider_active_cap.py` | 50 | 0 | 0 | 0 | focus-2 |
| `tests/unit/test_prune_217.py` | 27 | 0 | 0 | 0 | focus-2 |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_prune_restore.py` | 4 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_relay.py` | 11 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_render.py` | 20 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_render_quota_projection.py` | 23 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_reset_credit_demand.py` | 68 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_reset_credit_properties.py` | 11 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_resource_identity.py` | 10 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_retention_archive.py` | 109 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_retention_archive_properties.py` | 2 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_retention_case_alias.py` | 56 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_retention_nested_leases.py` | 27 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_retention_ordinary_trees.py` | 0 | 4 | 0 | 0 | diag-02 |
| `tests/unit/test_retention_qos.py` | 8 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 | 0 | finish-01 |
| `tests/unit/test_retention_rediscovery.py` | 17 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_retention_regenerable.py` | 26 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_retention_regenerable_properties.py` | 1 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_retention_shared_folders.py` | 62 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_retention_sweep_race.py` | 13 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_retention_turn_contract.py` | 2 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_retention_turn_workdirs.py` | 1 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_retention_turns.py` | 7 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_review_pr127_report.py` | 1 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_review_pr127_requests.py` | 17 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_review_pr127_service.py` | 8 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_review_publish_guard.py` | 66 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_review_r2_repro.py` | 31 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_review_r3_measure.py` | 6 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_review_r5_repro.py` | 17 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_round10_refusal_context.py` | 41 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_route_check.py` | 24 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_salvage.py` | 31 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_salvage_optional_transient.py` | 15 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_salvage_unindexable.py` | 86 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_scheduler.py` | 97 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_scheduler_split.py` | 3 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_scheduler_tier_preferences.py` | 9 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_scheduler_turns.py` | 10 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_scheduler_weekly.py` | 33 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_scoped_stream_capacity.py` | 17 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_sessions_cli.py` | 142 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_sessions_handoff.py` | 140 | 0 | 0 | 0 | focus-1 |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_sessions_headless_properties.py` | 5 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_sessions_mirror.py` | 41 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 4 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_sessions_mirror_fifo.py` | 49 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_sessions_mirror_hot_health.py` | 6 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 16 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 22 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_sessions_mirror_inventory.py` | 8 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_sessions_mirror_load_gap.py` | 85 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_sessions_mirror_safety.py` | 28 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_sessions_mirror_write_churn.py` | 3 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_sessions_nudge.py` | 44 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_sessions_registry.py` | 24 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_sessions_revive.py` | 45 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_sessions_revive_reads.py` | 10 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_sessions_transcripts.py` | 71 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_soak_tools.py` | 49 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_status_json.py` | 56 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_steer_invariants.py` | 248 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_stop_alarm.py` | 1 | 1 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_stop_watchdog.py` | 26 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_store_contention_queries.py` | 212 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_store_ids.py` | 27 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_store_migration_2.py` | 21 | 0 | 0 | 0 | focus-2 |
| `tests/unit/test_store_properties.py` | 3 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_store_readers.py` | 26 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_store_snapshot_plans.py` | 4 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_suggest_cli.py` | 7 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_suggest_hint.py` | 10 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_suggestion_error_boundaries.py` | 22 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_suggestion_properties.py` | 1 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_suggestion_secret_properties.py` | 7 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_suggestion_structural_errors.py` | 40 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_suggestions.py` | 29 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 30 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_suggestions_r3_channels.py` | 14 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_suggestions_review_multiline.py` | 6 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_suggestions_review_response.py` | 3 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_tick_prefilter.py` | 30 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_timers_alerts.py` | 17 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_timers_busy_read.py` | 115 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_timers_daemon.py` | 4 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_timers_keepalive.py` | 14 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_timers_probe.py` | 35 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_timers_reset_credits.py` | 47 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_timers_retention.py` | 20 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_title_review_regressions.py` | 4 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_turn_attachment_replay.py` | 13 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_turn_overlap.py` | 10 | 0 | 0 | 0 | complete-01; complete phase records, completed-session replay unrun |
| `tests/unit/test_turn_replay.py` | 33 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_turn_runner.py` | 76 | 0 | 0 | 0 | diag-02 |
| `tests/unit/test_ultracode_effort.py` | 39 | 0 | 0 | 0 | final-01 |
| `tests/unit/test_unadmittable_pin.py` | 12 | 0 | 0 | 0 | last-01 |
| `tests/unit/test_wait_hub.py` | 44 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_wake_cli.py` | 2 | 0 | 0 | 0 | full-01 |
| `tests/unit/test_weekly_rank_vs_base.py` | 26 | 0 | 0 | 0 | full-01 |

Cleanup verified: task scratch, uv cache and Hypothesis storage were removed. The first pass left a deliberately read-only test-fixture directory; its owner write permission was restored inside task scratch before the final removal. No workspace `.hypothesis` remains. All owned pytest and queued test runners exited; the final queued invocation never started pytest. Nothing was pushed. Commits remain in workspace-local `.git-local` metadata because the shared Git index is read-only.

The final comparison with the base removed one extra blank line at the end of the new policy-resolution test file. Its code is unchanged; no additional pytest invocation was started after the cutoff. The complete diff passes the whitespace check.


## Round 6: the publish boundary in real shells, the caller's uncommitted work, and review r1b's P3s

Review r1b (`~/reviews/subfleet-hub/shell-downgrade-2026-10-14/review-r1b.md`) requested changes at `d03832397`. Round 6 follows the hub's decisions in `brief-fix6.md`. Commits are on a detached HEAD in this job's worktree (the shared Git directory was writable this round, so no `.git-local` was needed). Nothing was pushed.

### Base merge

`origin/release/217` at `3fbc4b7c5` (#207, #225, #227) is merged in `72aca94a5`. Two files conflicted:

- `docs/acceptance-contract.md`: the version 3 change log keeps both sides' entries. C-11.1 keeps this branch's `worktree-write` text, since release did not change it. C-11.2 keeps this branch's shell-requirement text and gains release's capacity-steer sentence (C-9.5) after "walks that list in order".
- `subfleet/scheduler.py` `demand_models`: the shell filter runs first, then release's live capacity-steer filter, so a job's demand drops both shell-less launches and steered models. `evaluate` had merged cleanly with both filters.

### Commits

| Commit | Change |
|---|---|
| `72aca94a5` | merge release/217 |
| `84e2d049c` | P2 gh flag clusters; P3-7 review reads; rule moved to `subfleet/publish_rule.py` |
| `799c9f08f` | P1 PreToolUse publish rule in both providers |
| `5e88e2bc4` | P3-1 `GIT_CONFIG_PARAMETERS` |
| `be23a51dc` | P2 the caller's uncommitted work in a policy review's worktree; P3-8 review preamble |
| `52efb5327` | P3-2 capability recorded at launch |
| `4a1802a7e` | P3-3 no capability line for writable attempts; P3-4 offline JSON |
| `f3172a62e` | P3-5 all 192 sandbox resolutions |
| `a52140437` | P3-9 indexed 24-hour query |
| `8fd1338f8` | P3-6 real process identity in importer and doctor tests |
| `f352eb0c1` | wire templates for the three new refusal reasons (C-25.7) |

### P1: a layer that doesn't depend on PATH order

Mechanism (a), a job-scoped command rule in each provider's own pre-execution hook, is feasible for both providers, so fallback (b) (a job `ZDOTDIR`) was not built. The rule is `subfleet/publish_rule.py`, standard library only. It judges the command text before any shell starts:

- **What it refuses:** every `gh` call that is not a known read; `gh api` with a non-GET method, fields, `--input`, `graphql` or `--`; and `git push` (`push`, `send-pack`, `http-push`, or a `-c` alias for one) as a second layer behind git's environment.
- **What it looks through:** wrappers (`env`, `command`, `exec`, `xargs`, `sudo`, `timeout`, `nice`, `time`, `nohup`), `sh|bash|zsh -c`, `eval`, command and process substitution (quoted or not, and in unquoted heredocs), `find -exec`, shell input given by a pipe or heredoc, and command words that only expand at run time (`$g pr comment 7`).
- **Fail closed:** text it cannot tokenize is refused when it names gh or git push.
- **What it cannot see:** gh run from another language (`python -c`, a script on disk), and raw HTTPS with a token found on disk. C-12.4 says so.

**Claude.**
- **How the rule reaches the launch:** a writable review launch passes `--settings` with a PreToolUse hook that has no matcher, so it sees every tool call with a `command`. The hook runs `<attempt dir>/publish-bin/review-publish-hook`, a script that embeds the interpreter (`-I`) and the package path.
- **A second path:** `subfleet hook PreToolUse` applies the same rule wherever the launch marker `SUBFLEET_PUBLISH_DENIED=1` is set. A hook runs in the provider's environment, which the job cannot change.
- **Read-only launches:** a read-only Claude launch has no Bash tool, and its `--safe-mode` turns hooks off, so it gets no hook.
- **Evidence:**
  - Conversations already pass `--settings <json>` to the installed Claude in production (`subfleet/conversations/claude_turn.py:151`).
  - `docs/reference/claude-hooks.md` records that hook entries merge across settings levels, and gives the deny JSON this hook prints.
  - This session itself runs with permissions bypassed. Its never-rules PreToolUse hook denied two of my Bash commands (each contained a `git stash` string), and neither ran. So a PreToolUse deny holds under bypass on this host's Claude.
- **Not executed:** a real Claude lane launch firing the `--settings` hook, since a model call needs the network.

**Codex.**
- **How the rule reaches the launch:** a review's `-c hooks=` override gains a second PreToolUse group beside the never-rules guard: matcher `Bash`, status `review publish guard`. It runs `<state root>/publish-guard/review-publish-hook`, a stable path so the trust hash and the cached preflight verdict survive from launch to launch. The group has its own trusted state, taken with the guard's identity hash.
- **Verification:** the guard preflight runs hooks/list with that override. It requires the publish hook exactly once, enabled, trusted, with that hash, and gives a `trust` refusal otherwise. The Codex adapter also refuses a writable review whose override lacks the hook (`codex-publish-hook-required`).
- **Evidence:**
  - Real Codex 0.159.0 `app-server` hooks/list, run with a scratch `CODEX_HOME` and network denied by a `sandbox-exec` profile, listed the second group at key `/<session-flags>/config.toml:pre_tool_use:1:0`. It was enabled and trusted, with `currentHash` equal to our identity hash (`sha256:bf3a2b57…`; pinned in `test_publish_hook_identity_hash_matches_codex_0159_hooks_list`).
  - The 2026-09-24 live probe (`docs/desktop/reviews/2026-09-24-live-probes.md`) shows a trusted PreToolUse hook in this same override blocking `exec_command`.
  - The deny output shape is the guard's own I/O contract.
- **Not executed:** a real Codex model turn.
- **Guard and TRUST unchanged:**
  - The never-rules guard's bytes, its key and hash, and TRUST are unchanged.
  - v1's plain override is byte-identical. I compared the old and new `override_string` for three paths, including quote and backslash spellings.
  - So no TRUST pin moves, and nothing under `~/.subfleet` needs restaging.
- **Which launches carry it:**
  - Every non-isolated Codex review launch carries the publish hook, read-only included.
  - Isolated reviews and gate rounds carry no hooks override at all, as before, and they are read-only.
- **Network:**
  - Subfleet gives a writable Codex launch network only through `sandbox_workspace_write.network_access=true` (d260; the adapter's comment records "Could not resolve host" without it, verified live 2026-09-25).
  - Those launches turn unified exec off (C-23.6), so every shell command they run passes through the hooks.

**Regressions.**
- **What `test_gh_writes_are_refused_in_each_providers_real_shell` runs:**
  - For Codex `zsh -c` and `zsh -lc`, and Claude `zsh -c`, `zsh -lc` and the snapshot shape (`source <snapshot> && eval <command>`), it takes the hooks out of the real launch argv and runs them with the provider's payload.
  - When they allow, it runs the command through that shell.
  - A recording `gh` sits at the Homebrew position, put first by a scratch `ZDOTDIR`'s `.zshenv` and `.zprofile` (after `/etc/zprofile`'s `path_helper`) and by the snapshot.
- **What it checks:**
  - All 10 writes are refused, including the review's `gh pr comment 7 --body published`, the `-iXPOST` and `-ifbody=` clusters, `zsh -lc '…'`, `$(…)` and `$g`.
  - `gh pr view 7`, `gh pr checks 7`, `gh run view 1 --log-failed` and an API GET reach the recorder.
  - The harness checks that each write, run past the hook, does reach the recorder. That reproduces the finding.
- **`test_git_push_is_refused_by_the_hook_as_a_second_layer`** covers four push shapes on both providers.
- **Failing at the base:** at `d03832397` sources, 13 of these 15 tests fail. The other 2 are non-review guards, which pass at the base too.

### P2: gh flag clusters

A single-dash cluster containing `f`, `F` or `X` is refused, except exactly `-XGET`, and `-X GET` as two words. `--method`, `--field`, `--raw-field` and `--input` are judged by name. The review's four executed inputs (`api -ifbody=published …`, `api -iXPOST …`, `api -iX POST …`, `api -iFbody=@file …`) and four more cluster shapes are regressions on both providers. 10 of the 16 cases (5 inputs on both providers) fail at `d03832397`; `-Xget`, `-HAccept:x -f` and `--method PATCH` were already refused there. GET clusters (`-XGET`, `-X GET`, `-i`, `--method=GET`, `--paginate -q`) still read.

### P2: a review sees what its caller sees

- **Which reviews:** a review whose sandbox comes from a `worktree-write` policy entry and resolves to workspace-write.
- **At submit:** `subfleet/uncommitted.py` plans two things:
  - the caller's uncommitted tracked changes, from `git diff --binary <head>` with options that ignore the caller's diff config;
  - untracked, non-ignored files, from `git ls-files --others --exclude-standard`.
- **Applicability check:** submit checks that the patch applies to `<head>`, in a scratch index under the state root (`<state root>/tmp`, C-2.1), and keeps the copy in `jobs/<id>/uncommitted/`.
- **When the worktree is cut:** the patch is applied and the files copied in, before the attempt's baseline is taken, so salvage still holds only the job's own changes. Ignored files are never copied.
- **The bound:** at most **1,000 paths** (changed tracked plus untracked) and **16 MiB** of patch and file bytes together.
- **Fallback:** the review runs read-only in place, decided at submit before the job exists, so no sandbox is lowered later (d261). Causes:
  - either bound exceeded;
  - a patch that does not apply;
  - a changed submodule;
  - an untracked nested repository. Git lists only files, symlinks and nested repositories as untracked (never a FIFO or a socket, which are simply not copied), so a nested repository is the only untracked path that reaches this fallback (corrected in round 7, #221 review r2 P3-2).
- **Edge cases:**
  - A file that vanishes between plan and copy fails the submit (`uncommitted-copy`), and the caller retries.
  - A kept copy that does not apply at the cut fails that launch (`uncommitted-apply`).
  - An explicit `-s workspace-write` job keeps today's rule.
- **What the job is told:**
  - The prompt note says the workspace holds commit `<head>` plus the caller's uncommitted changes and untracked files as of submit, and how many files.
  - The finish notice adds, before the capability line, `the worktree included the caller's uncommitted changes as of submit: 2 files (1 tracked, 1 untracked)`, or after a fallback `ran read-only in the caller's checkout: its uncommitted changes could not be copied into a worktree (<reason>)`.
  - `runs show` prints the same line, and its JSON carries `uncommitted`, from the daemon and offline alike.
- **Regressions** (fake daemon, the review's probe of an edited `tracked.txt`, an untracked `new_module.py`, and an excluded `ignored.log`):
  - the worktree has both and not the ignored file, and the caller's `git status` is unchanged;
  - three fallback causes (bytes, files, nested repository) each run read-only in the caller's directory with `commands` capability;
  - an explicit `-s workspace-write` job sees `baseline`;
  - a clean checkout copies nothing.
  - All 6 fail at the pre-fix sources.

### Review r1b's P3s

1. **`GIT_CONFIG_PARAMETERS`** carries the push URL rewrite, and no denial variable matches Codex's `*KEY*`, `*SECRET*` or `*TOKEN*` excludes. Real `codex sandbox` 0.159.0 with `ignore_default_excludes=false`: the old form failed `git log` with rc 128; the new form keeps `git log` working, and the push is refused (`transport 'subfleet-publish-denied' not allowed`). The regression strips the excluded names and runs absolute git. Both cases fail at the base.
2. **Capability is recorded at launch** (`Daemon._record_launch`), never at reservation. The pinned notice (`test_notice_header.py`) is now `[header, "reserved-no-launch"]`, and the 24-hour line counts nothing until a launch is recorded. State-only fixtures that stand for a launched attempt record the launch as `_launch` does (`tests/fake` `launched()`). `notice_without_capability` strips launch evidence only when it is present, and still requires it to be last.
3. **The capability line** appears only for `none` and `commands`. A writable review says `publish: denied` alone, and a build says nothing. C-12.4 and C-15.1 match. Three cases fail at the base.
4. **Offline `show` and `list`** go through `store.shell_flags`. The none, commands and writable probe cases compare daemon and offline JSON. All 3 fail at the base (`1` against `true`).
5. **All 192 sandbox combinations** are now enumerated with `itertools.product`. The mutant that ignores `in_place` fails at `('review', 'policy', True, True, False, False)`.
6. **Real process identity.** The importer's `v1` fixture uses real identity (this process, and a reaped child) wherever `ps` and `sysctl` report it, and fakes it only where `process_inspection_available` would skip. The denied-kernel test forces the faked path. Doctor keeps its faked lock rows and gains one real case each.
7. **gh reads:** `run view|list|watch` (including `--log-failed`), `issue view|list|status`, `pr list|status`, `repo view`, `release view|list`, `workflow view|list`, `label list` and `search`. Writes such as `run rerun`, `workflow run`, `issue close` and `auth token` stay refused.
8. **A writable review gets a review preamble.** It may run tests and write scratch in its workspace, and must not commit, push or publish. A build keeps the write preamble.
9. **The 24-hour query** is `reserved_at >= ? AND reserved_at <= ?`, with bounds in the stored spelling (`store.restricted_window`), and searches `attempts_reserved`. On 100,000 synthetic attempts over 90 days, with 20,000 jobs and 545 of them in the window, best of 20: **21.11 ms before** (`SCAN attempts`) and **1.84 ms after** (`SEARCH attempts USING INDEX attempts_reserved (reserved_at>? AND reserved_at<?)`). The test pins the plan and the window's edges.
10. **Install order:** see the install steps below.
11. **`GIT_ALLOW_PROTOCOL=""`** also disables `git fetch` for every review. It is kept, and C-12.4 states that D-SH1 round 6 accepts it knowingly: a review reads the checkout and history it was given.

### Mutations

Each mutation was applied under its own lock hold, then the files were restored byte for byte.

| Mutation | Caught by |
|---|---|
| Drop the hook rule (`payload_refusal` returns None) | 17 failed: `test_gh_writes_are_refused_in_each_providers_real_shell` (5), `test_git_push_is_refused_by_the_hook_as_a_second_layer` (8), `test_pre_tool_use_denies_publishing_in_a_publish_denied_launch` (4) |
| Accept clusters again (old prefix checks) | 15 failed: `test_gh_flag_clusters_that_real_gh_sends_as_writes_are_refused` (10), `test_gh_writes_are_refused_in_each_providers_real_shell` (5) |
| Skip the uncommitted copy (`uncommitted.apply` returns) | 1 failed: `test_policy_review_worktree_sees_the_callers_uncommitted_and_untracked_files` |
| Capability line back for writable jobs | 7 failed: `test_writable_attempts_carry_no_capability_sentence` (3), `test_push_result_precedes_the_final_capability_line` (2), `test_opus_attempt_and_final_notice_line_report_shell`, `test_policy_review_worktree_sees_the_callers_uncommitted_and_untracked_files` |
| `resolve_sandbox` ignores `in_place` (P3-5) | `test_resolved_sandbox_table` |

### Tests

**Setup:** Python 3.14.7 (free-threaded) venv in the worktree, `PYTHONPATH` unset. Each run was one pytest process under its own hold of the shared lock, one worker (no xdist in this project), Hypothesis ci profile, scratch under `$(getconf DARWIN_USER_TEMP_DIR)sf-r221f6-s1/`. The briefed holds ran at `8fd1338f8` (hold 7 with the `f352eb0c1` refusal templates in place) and all passed with no skips. Per-file counts:

| Hold | File | Passed |
|---|---|---:|
| 1 | `tests/fake/test_review_needs_shell.py` | 23 |
| 1 | `tests/fake/test_review_launch_permissions.py` | 25 |
| 1 | `tests/unit/test_review_publish_guard.py` | 137 |
| 2 | `tests/unit/test_hooks.py` | 75 |
| 2 | `tests/unit/test_hooks_native_guard.py` | 18 |
| 2 | `tests/unit/test_guard_trust.py` | 105 |
| 2 | `tests/unit/test_guard_overlay.py` | 22 |
| 3 | `tests/unit/test_policy.py` | 186 |
| 3 | `tests/unit/test_policy_disk.py` | 57 |
| 3 | `tests/unit/test_policy_ramp.py` | 46 |
| 3 | `tests/unit/test_policy_sandbox_resolution.py` | 42 |
| 3 | `tests/unit/test_policy_support.py` | 14 |
| 3 | `tests/unit/test_policy_tier_preferences.py` | 15 |
| 3 | `tests/fake/test_policy_sandbox.py` | 25 |
| 4 | `tests/unit/test_cli.py` | 230 |
| 4 | `tests/unit/test_offline.py` | 33 |
| 4 | `tests/unit/test_notice_rendering.py` | 40 |
| 4 | `tests/fake/test_notice_header.py` | 24 |
| 5 | `tests/unit/test_codex_adapter.py` | 172 |
| 5 | `tests/unit/test_claude_adapter.py` | 172 |
| 6 | `tests/unit/test_store_migration_2.py` | 21 |
| 6 | `tests/unit/test_store_readers.py` | 27 |
| 6 | `tests/unit/test_scheduler.py` | 97 |
| 7 | `tests/unit/test_sessions_handoff.py` | 145 |
| 7 | `tests/unit/test_policy_sandbox_resolution.py` | 42 |
| 7 | `tests/unit/test_host_push.py` | 120 |
| 7 | `tests/unit/test_handler_refusals.py` | 338 |
| 7 | `tests/fake/test_salvage_finalization.py` | 47 |
| 7 | `tests/unit/test_provider_active_cap.py` | 50 |
| 8 | `tests/unit/test_importer.py` | 151 |
| 8 | `tests/unit/test_doctor.py` | 47 |
| 8 | `tests/process/test_claude_isolation.py` | 15 |
| 9 | `tests/fake/test_state_contract.py` | 37 |
| 9 | `tests/unit/test_test_module_names.py` | 1 |

**Totals:** hold 1, 185; hold 2, 220; hold 3, 385; hold 4, 327; hold 5, 344; hold 6, 145; hold 7, 742; hold 8, 213; hold 9, 38. That is 2,599 passed, 0 failed, 0 skipped.

**Hold 7's first run:** it found 1 failure, `test_handler_refusals`: the three new refusal reasons had no wire template. `f352eb0c1` adds them, and the rerun is the 742 above.

**Extra holds** (files outside the brief that touch the changed code), all passed:

- **x1, 244:** `tests/fake/test_daemon_launch.py`, `test_detached_cwd_pins.py`, `test_fake_provider.py`, `test_prompt_seam.py`, `test_workspace_transient.py`, `test_workspace_contract.py`; `tests/unit/test_daemon_integration.py`, `test_host_push_intake.py`, `test_outcome_unknown.py`, `test_prune_217.py`, `test_round10_refusal_context.py`.
- **x2, 201:** `tests/fake/test_gate_end_to_end.py` and `tests/unit/test_gate_*.py`.

**Outside pytest:**
- `codex app-server` hooks/list and `codex sandbox` 0.159.0, with scratch homes;
- real git 2.55 against local file:// remotes;
- the query timing.

**Not run:**
- `tests/e2e` and the full suite (not in the brief);
- real provider launches: a Claude lane firing the `--settings` hook, and a Codex model turn firing the publish hook (both need the network);
- `gh` against any host.

### Install steps (also in the PR body)

1. **Upgrade the daemon before the live policy edit.** release/217's `load_policy` rejects `worktree-write`, so a daemon still on release/217 would refuse that policy. Install the release containing this PR through the normal backed-up installation, and restart the daemon.
2. **No guard restaging.** `never-rules-hook.sh` and TRUST are unchanged; the publish hook is a separate group with its own trusted state. Verify:
   - `cmp private/guard/never-rules-hook.sh "$HOME/.subfleet/guard/never-rules-hook.sh"`;
   - `cmp private/guard/TRUST "$HOME/.subfleet/guard/TRUST"`;
   - `subfleet doctor` passes its guard rows.
3. **Tell Fleet ops, then edit the live policy:** `permissions.review = "worktree-write"`. Restart the daemon, which reads policy only at start.
4. **First Codex review:**
   - The daemon writes `<state root>/publish-guard/review-publish-hook` (mode 555, inside the state root).
   - The lane's preflight runs hooks/list once for the extended override, and the verdict is cached as before.
   - Check that `<state root>/jobs/<id>/a1/launch.json`'s `hooks=` argument names `pre_tool_use:1:0` and that hook path, and that `guard-preflight.json` says `verified` (or `cached`).
   - A `trust` refusal there means this Codex build does not trust the second group. The launch then fails closed and never runs without the rule.
5. **First writable Claude review:** check that `launch.json`'s argv has `--settings` naming `publish-bin/review-publish-hook`. `subfleet runs show <id>` then shows `publish: denied` under the attempt.
6. **Merging #222 later:** the explicit launch additions are now `PATH`, `GIT_ALLOW_PROTOCOL`, `GIT_CONFIG_PARAMETERS` and `SUBFLEET_PUBLISH_DENIED` (`GIT_CONFIG_COUNT/KEY_n/VALUE_n` are gone). They still enter through `Launch.env_add`, so no ambient allowlist entry is needed.

### PR body for round 6

> **D-SH1 round 6.** Reviews in their own git worktree can run their tests, and every review is denied publishing in each provider's real shell. Fixes #221 review r1b.
>
> - **Publish boundary (P1).** zsh startup put Homebrew ahead of the PATH wrappers, so gh writes reached the real gh. A job-scoped PreToolUse command rule now refuses them before any shell starts:
>   - Claude gets it through `--settings`, and `subfleet hook PreToolUse` applies it under the launch marker.
>   - Codex gets it as a second trusted hook group beside the never-rules guard, which the guard preflight verifies with hooks/list.
>   - The guard and TRUST are unchanged.
>   - git push stays refused by the environment, and the rule adds a second layer.
> - **gh (P2, P3-7).** Flag clusters with `f`, `F` or `X` are refused except `-XGET` and `-X GET`. Reads: pr view/diff/checks/list/status, issue view/list/status, run view/list/watch, api GETs and more.
> - **The caller's work (P2).** A policy review's worktree gets the caller's uncommitted and untracked files as of submit (bound: 1,000 paths and 16 MiB). Otherwise the review runs read-only in place, and its notice says so.
> - **Evidence (P3).**
>   - Capability is recorded at launch.
>   - A capability line appears only for `none` and `commands`; writable reviews say `publish: denied`.
>   - Offline JSON matches the daemon's.
>   - The 24-hour line is an indexed range (21.11 ms to 1.84 ms on 100,000 rows).
>   - git config travels in `GIT_CONFIG_PARAMETERS`.
>   - Writable reviews get a review preamble.
> - **Accepted:** `GIT_ALLOW_PROTOCOL=""` also disables `git fetch` in every review (C-12.4).
> - **Install order:** upgrade the daemon first, because release/217's `load_policy` rejects `worktree-write`. Then tell Fleet ops, set `permissions.review = "worktree-write"` in the live policy, and restart. No TRUST change. Verification steps are in `docs/reports/2026-10-14-review-needs-shell.md`, round 6.
>
> 🤖 Generated with [Claude Code](https://claude.com/claude-code)

### Wider sweep after the briefed holds

To catch regressions outside the brief, the rest of `tests/fake` and most of `tests/unit` ran at `2070ddecb`, one hold per chunk, before the 4 h 30 m cutoff.

**`tests/fake`: all 106 files ran.**
- **Earlier holds:** 13 files, in the briefed holds and x1 and x2.
- **The other 93, in chunks:**
  - 1,567 passed, 2 xfailed, 1 failed (below);
  - by chunk: fake-0 379 plus 1 xfailed, fake-15 329 plus 1 xfailed, fake-30 167, fake-40 149, fake-50 208, fake-60 110, fake-70 97, fake-80 128.

**`tests/unit`: 237 of 311 files ran.**
- **Earlier holds:** 37 files.
- **In chunks, 200 more:** 7,423 passed and 1 skipped (`test_host_push_build.py:289`, an explicit local-repo benchmark). By chunk: unit-0 1,193; unit-40 2,620; unit-80 935; unit-120 1,862; unit-160 813.
- **The longest hold** was unit-160 at 18 min 20 s.

**The one failure: `tests/fake/test_admission_stateful.py::TestAdmissionPassProviderCaps`, in fake-0. It predates this PR.**
- **Profile:** that run used the file's `dev` profile (random seeds, 40 examples, 30 steps). The file picks `ci` only when `HYPOTHESIS_PROFILE` or `CI` is set; CI sets `HYPOTHESIS_PROFILE: ci`, and these holds did not set it.
  - Under `ci` (derandomized, as CI runs it), the file passes 11 of 11 at `2070ddecb`.
  - With CI's `--hypothesis-seed=314159`, it also passes 11 of 11.
  - Only this file and the two `test_census_responsibility_*` files choose a profile from the environment, and none of them is in the briefed holds.
- **The minimal example:**
  - one Codex lane, `providers.codex.max_active_attempts = 1`, and two reviews;
  - the first review's attempt ends `transient`, then the fleet is quiet for 2 minutes;
  - the next pass reserves the first review's retry (`a2`), and the second review's hold still names the first as `behind-older-job`;
  - the oracle then reports "held behind …, which is no longer pending".
- **Release/217 shows the same:** replayed step for step on release/217 itself (`3fbc4b7c5`, before any #221 change), it gives the same assertion with the same holds. So the defect is release/217's FIFO hold reason (C-6.9, C-6.4a). It looks like a hold that is not revisited when the job it names is placed later in the same pass, but I did not read the pass to confirm that.
- **Which trees failed:** random runs failed at the round-6 trees (`52efb5327`, `2070ddecb`) and passed at older ones (`72aca94a5`, `be23a51dc`, and `52efb5327` with the old reservation INSERT). On a random-seed profile, that is not evidence that round 6 caused it.
- **Follow-up for the hub:** an admission fix on release/217. It is not part of #221.

**Unrun:**
- 74 `tests/unit` files, ran out of time before the cutoff: `test_review_r3_measure.py` through `test_weekly_rank_vs_base.py` alphabetically (the `review_r3`, `route_check`, `salvage*`, `scheduler_*` splits, `sessions_*`, `settle`, `soak`, `status_json`, `steer`, `stop_*`, `store_*` others, `suggest*`, `tick`, `timers_*`, `title`, `turn_*`, `ultracode`, `unadmittable_pin`, `wait_hub`, `wake_cli` and `weekly_rank` files);
- `tests/e2e`, `tests/frontend`, and `tests/process` apart from `test_claude_isolation.py`.

**Cleanup:**
- The scratch probe directory, `git archive` exports, hold logs, pytest `TMPDIR` and Hypothesis storage are removed at the end of the job, along with the worktree's bytecode and its `.venv`.
- No `.hypothesis` folder was left, and nothing of this job is left running.

## Round 7: ordinary shell, the copy's marker, the pinned launch, and review r2's P3s

Review r2 (`~/reviews/subfleet-hub/shell-downgrade-2026-10-14/review-r2.md`) requested changes at `f62fdec4a`: round 6 closed r1b's findings, but the publish rule passed spellings that C-12.4 did not list as limits. Round 7 follows the hub's decisions in `brief-fix7.md`. The commits are on a detached HEAD in this job's worktree; Subfleet keeps them as a `refs/subfleet-salvage/` ref, and `fix/review-needs-shell-217` was not moved. The shared Git directory was writable, so no workspace-local metadata was needed.

**Base:** `origin/release/217` is at `d72597b5c` locally, which `f62fdec4a` already merges, so there was nothing to merge. No fetch was made (the job may not reach a network remote), so this is the local remote-tracking ref.

### Commits

| Commit | Change |
|---|---|
| `d728c8a83` | Tests first: the round-7 rule regressions (127 failed, 92 passed at `f62fdec4a`). Its message says the corpus has 77 commands; it has 69. |
| `437e7581d` | P2 and P3-5: the rule covers ordinary shell; the fallback; gh under another name; credential exposure; `gh api` read as gh's flag parser reads it; C-12.4's exact limits |
| `79c3b746d` | P3-1: the copy's `applied` marker, and a recut when it is missing; P3-2: the FIFO and socket wording |
| `42f94f123` | P3-3: the git denial pinned in Codex's `shell_environment_policy.set`; P3-4: the git-dependency loss stated; P3-6: `--setting-sources user` on writable Claude reviews |
| `ea2624f9c` | P3-7: the installed publish hook is self-tested before launch |
| `11514aa70` | P3-9: the workspace note says where an ignored file may be read |
| `917e178c1`, `df8563dff` | This report section, and the wider sweep |
| `60d12dc6e` | The self-test refusal's fix points at daemon.log: `subfleet doctor` does not inspect the publish hook |

### P2: each spelling review r2 executed, and its verdict

Each was rebuilt as the review ran it: the hooks taken from a real review launch's argv (Codex's `-c hooks=` publish group, Claude's `--settings`), then the command through Codex `zsh -c` and `zsh -lc` and Claude `zsh -c`, `zsh -lc` and its snapshot shape, with a recording `gh` (and a real `git`) at the Homebrew position, put first by a scratch `ZDOTDIR` and the snapshot. "Reached" is where the recording `gh` ran once the hooks were skipped, which reproduces the finding on this host.

| Spelling | Reached without the hooks | Round 7 | What refuses it |
|---|---|---|---|
| `r() { "$@"; }; r gh pr comment …` | 5 of 5 | refused | `"$@"` is a parameter-only command word and the text names gh; the fallback also judges the `gh` after `r` |
| `c='gh pr comment …'; eval "$c"` | 5 of 5 | refused | the assignment value is shell text naming a write (fallback); `eval "$c"` is parameter-only and the text names gh |
| `c='…'; sh -c "$c"` | 5 of 5 | refused | the same |
| `c='…'; ${=c}` | 5 of 5 | refused | the same |
| `bash <(echo 'gh pr comment …')` | 5 of 5 | refused | a process substitution given to a shell: what it prints is read as the script |
| `source <(echo '…')` | 5 of 5 | refused | the same, for `source` (and `.`) |
| `bash <<< 'gh pr comment …'` | 5 of 5 | refused | a here-string into a shell that reads standard input |
| `echo '…' \| xargs -I{} sh -c '{}'` | 5 of 5 | refused | `xargs` into a shell: its input is shell input |
| `arch -arm64 gh …` | 5 of 5 | refused | fallback |
| `/usr/bin/lockf -k f gh …` | 5 of 5 | refused | fallback |
| `taskpolicy -b gh …` | 5 of 5 | refused | fallback |
| `env -S 'gh …'` | 5 of 5 | refused | `env -S` and `--split-string` values are split into words |
| `UV_OFFLINE=1 uv run --no-project gh …` | `zsh -lc` only here (review r2: 4 of 5). The snapshot's PATH has no uv; why `zsh -c` did not reach gh here was not investigated | refused | fallback |
| `repeat 1 gh …` | 5 of 5 | refused | zsh's `repeat N` is a wrapper |
| `<brew>/g[h] pr comment …` | 5 of 5 | refused | a glob or brace command word is a run-time word; a gh command follows |
| `>/dev/null gh …` | 5 of 5 | refused | redirections are skipped |
| `- gh …` | Claude snapshot only, as review r2 saw; why the other shapes did not run it was not investigated | refused | zsh's `-` is a wrapper |
| `$(command -v gh) -R o/r pr comment …` | 5 of 5 | refused | gh's `-R`/`--repo`/`--hostname` are skipped before judging a run-time word |
| `ln -sf <brew>/gh ./g && ./g pr comment …` | 5 of 5 | refused | copying gh under another name |
| `GIT_EDITOR='gh pr comment …' git commit` | 5 of 5 | refused | the assignment value is shell text (fallback) |
| `git config alias.pc '!gh …' && git pc` | 5 of 5 | refused | git config values are shell text |
| `watch -n 1 -e gh …` | not run (watch needs a terminal) | refused | `watch` joins its operands as shell text; the fallback also |
| `parallel ::: 'gh …'` | not run (GNU parallel is not installed) | refused | parallel's template and arguments are shell text, and with neither it reads standard input |
| `$(command -v gh) cache delete --all` | 5 of 5 | refused | a run-time word followed by a known gh write |
| `$(command -v gh) auth token` | 5 of 5 | refused, `credential` | credential exposure, with its own reason and deny text |

Beyond the review's list, 58 more forms of the hub's decision are refused at the rule (`test_the_decisions_other_forms_are_refused_by_the_rule`): `bash < <(…)`, `. <(…)`, zsh's `=(…)`, `sh <<<"…"`, `bash -s <<<`, `source /dev/stdin <<<`, `"$c"`, `eval $c`, `2>&1 gh …`, `{fd}>…`, `cat <<< x` followed by a newline and a write (the heredoc pattern no longer mistakes `<<<` for a heredoc), `env -S"…"`, `--split-string=`, `- - gh`, `{gh,}`, `g?` and `*` command words, `--repo`, `--repo=` and `--hostname` before a run-time word, `ln -s "$(command -v gh)"`, `cp`, `install`, `mv`, `cat …/gh > g`, `dd if=…/gh`, `GIT_SEQUENCE_EDITOR`, `export EDITOR=`, `git -c core.editor=`, `git config core.pager`, `git config alias.x '!gh'` then `git x pr comment`, nested unnamed runners, functions defined with `function`, `docker run`, `ssh`, `watch '…'`, `parallel` with a template or from standard input, `xargs -0 bash -c`, `uv run sh -c`, `fd -x sh -c`, `find -exec sh -c`, and run-time words before `api -XPOST` or `--method PATCH`.

### The fallback

Where the rule cannot name the command (a runner it does not know, a shell function, a value git runs), it reads every word of the simple command:

- a word equal to `gh` or ending in `/gh`, after quote removal and without a leading `!` or `=`, starts a gh call made of the words after it;
- a run-time word (a parameter, a substitution, a glob or brace word) followed, after gh's global flags, by a gh command starts one too;
- a word equal to `git` or ending in `/git` starts a git call, judged by `git_publishes`;
- an assignment value (`NAME=value`, anywhere in the command), the value of `-c`, `-e` or `--exec` (not for `grep`, `rg`, `sed`, `awk`, `perl`, `python…`, `ruby`, `node`, `jq` and the like, whose values are patterns or code), and a git config value (`git -c name=value`, `git config <name> <value>`, a `!` alias) are read as shell text when they hold more than one word.

A gh call found this way is judged positively (`gh_writes`): it is refused only for a known gh verb that is not a read (the subcommand lists of gh 2.100.0, read offline), `gh api` that does not read, `gh copilot` (an agent that can run gh itself), or credential exposure. A word gh does not know is not a write, so `grep gh file`, `ls ghost`, `echo gh`, `which gh git` and `git log --grep gh` pass. Where the rule can name `gh` as the command it still judges fail closed, as before.

A command word, or text given to `eval` or a shell, that is only parameters (`$c`, `${=c}`, `"$c"`, `"$@"`, `$c$d`) is refused when the command text names gh anywhere, quotes and backslashes removed. A path built from a parameter (`"$VIRTUAL_ENV/bin/python" -m pytest`) is not parameter-only and passes.

### What remains outside the rule

C-12.4 now states exactly this, and nothing else is claimed:

- code in another language (`python -c`, `perl -e`, `node -e`, `sed`'s and `awk`'s own commands);
- a script on disk, however it is run;
- git that discards its environment where the rule cannot see the push (from another language or a script on disk). Review r2's one landed push, `unset GIT_ALLOW_PROTOCOL GIT_CONFIG_PARAMETERS; r() { "$@"; }; r git push …`, is now refused by the rule, which sees the `git` word;
- a raw HTTPS call with a token found on disk;
- a gh binary given another name outside the command text, or by a tool the rule does not name;
- a gh alias or extension name (gh keeps them in its own config, `co` among the defaults) reached through a runner the rule does not know, since the fallback refuses only the verbs it knows;
- a command assembled at run time from pieces that never spell gh and never put a gh verb after a run-time word (`a=$(printf 'g%s' h); b='pr comment 7'; eval "$a $b"`);
- a zsh alias: review r2 saw `alias g=gh; g pr comment` run no gh in any of the five command paths.

`env -S` performs no substitution, globbing or `$VAR` expansion, so a run-time word inside it never runs gh; the property test leaves those draws out.

### git push in every new form

Against a local bare repository, in each of the five command paths, with a real `git` at the Homebrew position (`test_git_push_stays_refused_in_every_new_form`):

| Form | The hooks | Past the hooks |
|---|---|---|
| function runner | refused | refused by the git environment |
| here-string | refused | refused |
| process substitution | refused | refused |
| `arch` | refused | refused |
| `{git,} push` | refused | refused |
| leading redirection | refused | refused |
| `env -S` | refused | refused |
| a variable with `eval` | refused | refused |
| `python3 -c` | allowed (another language) | refused |
| a script on disk | allowed | refused |
| `unset …; r() { "$@"; }; r git push` | refused (new) | review r2 saw it land |

### Credential exposure

`gh auth token`, `gh auth status -t` or `--show-token`, `--show-token` on any call, and `gh auth status` with a flag beyond `-a`/`--active` and `-h`/`--hostname` (`--json` is refused because this job could not verify offline that it masks the token) are refused with reason kind `credential` and their own deny text: "A review never prints a GitHub credential: gh auth token, gh auth status -t and --show-token would copy the token into the review's output. gh auth status alone still works." `gh auth status` alone reads. Only the recording `gh` ran these; the real `gh auth token` never ran.

### P3-5: reads

Real `gh` 2.100.0, with a dummy token against closed `localhost:9` (`GH_DEBUG=api`, scratch `GH_CONFIG_DIR`), sent:

| Input | Request |
|---|---|
| `api -X GET -f q=repo:o/r search/issues` | `GET /api/v3/search/issues?q=repo%3Ao%2Fr` |
| `api -iX GET -f q=x search/issues` | `GET …?q=x` |
| `api -X=GET repos/o/r` | `GET` |
| `api -HX-GitHub-Api-Version:2022-11-28 repos/o/r` | `GET` |
| `api -q.foo repos/o/r` | `GET` |
| `api -X GET -F q=x`, `-X GET -fq=x`, `--method GET --raw-field q=x` | `GET …?q=x` |
| `api -Xget repos/o/r` | `GET` |
| `api -fq=x repos/o/r` | `POST` |
| `api -iXPOST repos/o/r` | `POST` |
| `api -X GET -X POST repos/o/r` | `POST` (the last method wins) |

The rule now reads `gh api` flags as pflag does and allows exactly the GET ones; `--input`, `graphql`, `--` and an undefined flag stay refused. Three round-6 expectations changed with it: `-iX GET` and `-Xget` move from the cluster writes to the reads, and `-XGET --raw-field=a=b` is now a read. `gh --version`, `gh version` and `gh auth status` read.

### Review r2's P3s

1. **P3-1, a worktree without its copy.** `uncommitted.apply` clears `jobs/<id>/uncommitted/applied`, applies, then writes it naming the worktree. Admission discards and recuts a worktree whose job has a copy and no matching marker; nothing has run there, since the job has no attempt. The reviewer's case (`git worktree add --detach`, then `_admit()`) now gets the caller's `tracked.txt` and `new_module.py`.
2. **P3-2, FIFOs and sockets.** Wording only, in `uncommitted.py`, C-6.6 and this report's round-6 bullet: git lists only files, symlinks and nested repositories as untracked, so a nested repository is the only untracked path that reaches the fallback.
3. **P3-3, the pinned denial.** Every Codex review launch passes `-c shell_environment_policy.set.GIT_ALLOW_PROTOCOL=""`, `…GIT_CONFIG_PARAMETERS="…"` and `…SUBFLEET_PUBLISH_DENIED="1"`; Claude launches carry them in the launch environment. With real `codex sandbox` 0.159.0, a scratch `CODEX_HOME`, `sandbox_mode="workspace-write"` and `shell_environment_policy.inherit="core"`, the push to a bare repository inside the workspace landed without the pins and was refused with them (`fatal: transport 'subfleet-publish-denied' not allowed`, then `transport 'file' not allowed`). The test runs that case and skips where codex 0.159 is not installed.
4. **P3-4, git dependencies.** C-12.4's accepted fetch loss now says a review cannot install a git-sourced dependency. The denial is unchanged.
5. **P3-5, reads.** Above.
6. **P3-6, project settings.** A writable Claude review launch passes `--setting-sources user` before `--settings`. Whether `--settings` still loads was shown from references, not a Claude run: the installed Claude Code 2.1.284's own source selection (`fs()`) adds `flagSettings` (the `--settings` source) and `policySettings` to whatever `--setting-sources` names, and the repo's `docs/reference/claude-help.txt` says "managed settings and --settings still apply" under `--restricted`. The P3-7 self-test runs the hook command itself; it does not show that Claude loads it.
7. **P3-7, the self-test.** Before a publish-denied launch starts, the daemon reads the publish hooks the launch's argv installs, feeds each a PreToolUse payload for `gh pr comment 1 --body x` in the launch's environment with its C-5.1 markers, and refuses the launch (`publish-hook-self-test`, wire template "the review publish hook did not deny a gh write when it was tested before launch") unless each answers with the deny JSON. A pass is cached for the daemon's run, keyed by the command and the SHA-256 of the script, its interpreter (hashed once per file identity) and the two modules it imports; a failure is tried again at the next launch. The verdicts are in `launch.json` under `publish_hook_self_test`.
   - **Cost**, measured on this host at load 10 to 11: the first test in a daemon run took 280 ms (it hashes the 18 MB interpreter); a new hook command took 183.5 ms median (127.5 to 265.4 ms, 14 runs); a cached pass took 0.26 ms median (1.9 ms max, 300 runs).
   - A Codex review's hook is one stable path per state root, so after the first launch it is cached. A Claude review's hook lives in its attempt directory, so each Claude review launch pays the uncached cost, about 0.18 s.
8. **P3-8, the install order in the PR body.** The hub's; nothing here.
9. **P3-9, the workspace note.** Both notes now say that an ignored file the task names (one under `.diag/`) is not in the workspace and may be read in the caller's checkout at the same relative path; writing there is still forbidden.

### Mutations

Each was applied to the committed source, run, and restored from `HEAD`:

| Mutation | Result |
|---|---|
| Drop the fallback | caught: 29 failed, among them `test_each_spelling_review_r2_saw_reach_gh_is_refused_by_the_rule[arch]`, `[lockf]`, `[taskpolicy]`, `[uv run]`, `[GIT_EDITOR value]`, the five real-shell tests and the property |
| Drop the `<<<` rule (`<<<` no longer a redirection) | caught: 13 failed, among them `…[here-string into bash]`, the five real-shell tests and the property |
| Drop the marker check | caught: `test_a_worktree_cut_without_its_copy_is_cut_again` (`'baseline\n' == 'edited by the caller\n'`) |
| Drop the self-test | caught: `test_the_installed_publish_hook_is_run_before_launch` and the three `test_a_publish_hook_that_does_not_deny_refuses_the_launch` cases |

The new tests, run against the `f62fdec4a` source: 151 failed, 118 passed. The real-Codex case fails there because the unpinned push lands (`* [new branch] HEAD -> probe`).

### Tests

**Setup:** Python 3.14.7 free-threaded from the worktree's `.venv`, `PYTHONPATH` unset, one pytest process per hold of the shared suite lock (no xdist), `--timeout=300`, Hypothesis bounded (the new properties are derandomized with 400 examples each; no `deep` profile). The longest hold, including its wait for the lock, took about 17 min (the ninth `tests/unit` chunk below).

**The six briefed holds, at `11514aa70`:** 2,096 passed, none failed or skipped.

| Hold | File | Passed |
|---|---|---:|
| 1 | `tests/fake/test_review_needs_shell.py` | 23 |
| 1 | `tests/fake/test_review_launch_permissions.py` | 34 |
| 1 | `tests/unit/test_review_publish_guard.py` | 135 |
| 1 | `tests/unit/test_publish_rule_round7.py` | 235 |
| 2 | `tests/unit/test_hooks.py` | 75 |
| 2 | `tests/unit/test_hooks_native_guard.py` | 18 |
| 2 | `tests/unit/test_guard_trust.py` | 105 |
| 2 | `tests/unit/test_guard_overlay.py` | 22 |
| 3 | `tests/unit/test_policy.py` | 186 |
| 3 | `tests/unit/test_policy_disk.py` | 57 |
| 3 | `tests/unit/test_policy_ramp.py` | 46 |
| 3 | `tests/unit/test_policy_sandbox_resolution.py` | 42 |
| 3 | `tests/unit/test_policy_support.py` | 14 |
| 3 | `tests/unit/test_policy_tier_preferences.py` | 15 |
| 3 | `tests/fake/test_policy_sandbox.py` | 25 |
| 4 | `tests/unit/test_cli.py` | 230 |
| 4 | `tests/unit/test_offline.py` | 33 |
| 4 | `tests/unit/test_notice_rendering.py` | 40 |
| 4 | `tests/fake/test_notice_header.py` | 24 |
| 4 | `tests/unit/test_handler_refusals.py` | 340 |
| 5 | `tests/unit/test_codex_adapter.py` | 172 |
| 5 | `tests/unit/test_claude_adapter.py` | 172 |
| 5 | `tests/process/test_claude_isolation.py` | 15 |
| 6 | `tests/fake/test_state_contract.py` | 37 |
| 6 | `tests/unit/test_test_module_names.py` | 1 |

`test_review_publish_guard.py` has 135, two fewer than round 6's 137: two cluster cases (each run for both providers) moved to the GET reads list (P3-5). `test_handler_refusals.py` has 340, two more, for the new `publish-hook-self-test` reason. The only change after `11514aa70` is this report and a comment in `test_publish_rule_round7.py`.

**An extra hold** over the other callers of the launch path and the users of the uncommitted copy: 331 passed (`test_daemon_launch` 9, `test_workspace_contract` 8, `test_workspace_transient` 24, `test_main_refusal_write_dir` 5, `test_prompt_seam` 8, `test_detached_cwd_pins` 18, `test_launch_gates_above_stdio` 9, `test_salvage_finalization` 47, `test_record_secrecy` 44, `test_mcp_job_contract` 12, `test_retention_regenerable` 26, `test_host_push_quarantine_restart` 8, `test_salvage_unindexable` 86, `test_store_readers` 27).

**A second extra hold** over the isolation, gate, resume and compat tests, since isolated Codex reviews also get the new `-c` pins: 2,033 passed (`tests/process/test_codex_isolation.py` 38, `test_gate_admission` 24, `test_gate_end_to_end` 11, `test_resume_contract` 23, `test_compat` 1,615, `test_protocol_secrecy` 322).

**The wider sweep, at `917e178c1`,** with `HYPOTHESIS_PROFILE=ci` as CI runs it:
- **all 106 `tests/fake` files**, in 8 chunks: 1,849 passed and 2 xfailed, none failed (by chunk: 359 and 1 xfailed, 331 and 1 xfailed, 218, 207, 268, 255, 90, 121). Round 6's one failure there, `test_admission_stateful.py` under its random `dev` profile, did not recur under `ci`;
- **the other 286 `tests/unit` files** (all 312 with the holds above), in 12 chunks: 8,896 passed and 1 skipped (`test_host_push_build.py`'s explicit local benchmark), none failed (by chunk: 624, 766, 649, 877, 467 and the skip, 1,184, 706, 598, 379, 955, 1,120, 571);
- **the other 6 `tests/process` files:** 33 passed and 1 environment-conditional skip.

**Other runs:** the new tests at the `f62fdec4a` source (151 failed, 118 passed); the four mutation holds (370 run, 29 failed; 235, 13; 34, 1; 34, 4); and three development holds of the rule tests (127 failed and 92 passed at base; 349 passed and 5 failed, a test that never created its directory; 424 passed and 1 failed, the transport assertion fixed in `ea2624f9c`).

**Outside pytest:**
- the whole property space (10 writes, 7 spellings of gh, 40 forms nested two deep: 224,000 commands), all refused once `env -S` draws that run no gh are left out;
- real `gh` 2.100.0 against closed `localhost:9` with a dummy token, for the P3-5 table;
- real `codex sandbox` 0.159.0 for P3-3, by hand and in the test;
- the installed Claude Code 2.1.284 binary's own text, for P3-6 (read, not run);
- the self-test's cost, timed directly.

**Not run:**
- `tests/e2e`, `tests/frontend` and `tests/live`, and the suite as one process (every `tests/unit`, `tests/fake` and `tests/process` file ran, in holds);
- a real provider turn: a Claude lane loading `--settings` under `--setting-sources user`, or a Codex turn firing the publish hook;
- `gh` against any real host; the real `gh auth token`;
- the lane configs and the live PR body, which are off limits or need the network.

### Install checks added by round 7

The round-6 install steps stand. Nothing needs restaging: the guard file, TRUST and the Codex `hooks=` override are unchanged, so the preflight's cached verdicts stay valid. After the upgrade:
- the first review's `launch.json` carries `notes.publish_hook_self_test` with `ok: true` (daemon.log: `publish hook self-test … ok=True`);
- a Codex review's argv carries the three `shell_environment_policy.set.…` overrides;
- a writable Claude review's argv carries `--setting-sources user` before `--settings`.

## Round 8: release with #222 and after, CI's two failures, and the trust refusal's advice

Round 8 (`brief-fix8.md`) ran as two builds. The first, `20261019-081357-pr221-fix8b`, merged `origin/release/217` (#222, #231) and made five commits, then ended `transient` at 14:03Z on a provider server error while the host was off its network. Its last words were "All 466 pass. Committing the refusal fix, then investigating the 3.12 `test_daemon_connections` failure"; the refusal fix was committed, and the investigation hadn't started. The second, `20261020-024213-pr221-fix8c` (`brief-fix8c.md`), merged release again, checked the first build's work against `brief-fix8.md`, did CI item 2 and ran the tests. Both builds committed on a detached HEAD in their job worktrees, and Subfleet keeps their work under `refs/subfleet-salvage/`. `fix/review-needs-shell-217` was not moved. The shared Git directory was writable, so no workspace-local metadata was needed. Nothing was fetched or pushed.

### Commits

| Commit | Change |
|---|---|
| `55e3f9472` | Merge `origin/release/217` at `16b24033a` (#222 D-ENV1, #231 D-SCR1) |
| `f7e877b38` | Tests first: a review launch's pins pass #222's selection and its guardian; the copy's git selects its environment |
| `2e7c51add` | `LAUNCH_NAMES` lists `SUBFLEET_PUBLISH_DENIED`; `uncommitted._check_applies` gives git `build_environment({"GIT_INDEX_FILE": …})`, not the daemon's whole environment; C-12.3 and C-12.4 amended |
| `3260ac89e` | CI item 1: the e2e fake Codex lists and trusts every group the override's `state` trusts; `tests/e2e/conftest.py` puts the session's keychain recorder on PATH before `/usr/sbin`; `test_review_publish_e2e.py` runs a policy review (the shipped default) on both providers |
| `d2dc1c734` | Tests first: an untrusted publish hook gets its own verdict, reason and advice |
| `36a89279f` | Preflight kind `publish` with `_PUBLISH_FIX`, mapped to `guard-preflight-publish` with its own refusal template; C-14.2 amended |
| `94e011600` | Merge `origin/release/217` at `473fe2941` (#235 D-ADM3, #236 relay stop bound, #234 D-KILL2, #226 D-ST1) |
| `27dcbe4d3`, then the commit after it | This report section, then the whole-suite sweep |

### The merges

**First, `55e3f9472`, with `16b24033a`:** four files conflicted, and each kept both sides, as the hub's notes in `brief-fix8.md` said:
- **`subfleet/daemon.py`:** `_guard_override(..., env=None, env_pass=(), publish_command=None)`. Release's `options["env_pass"] = tuple(env_pass)` stays inside `if env is not None:`, and the publish block follows it. The attempt launch's call site passes `env_pass=credential_env` and `publish_command=(publish_hook(self.root) if publishing_denied(spec) and lane.provider == "codex" else None)`. The probe and conversation-turn call sites pass `env_pass` and no publish hook, since neither is a review.
- **`subfleet/guard/preflight.py`:** `preflight(..., env=None, env_pass=(), publish_command=None)`. Release's `env_pass` feeds `build_environment` and the preflight's Codex calls; this branch's `publish_command` feeds `override_string` and the hooks/list check. They share no line.
- **`subfleet/sessions/handoff.py`:** `from ..contracts import DEFAULT_CAPS, SCRUB_MAX_CHARS, Exit`, `from ..launch_env import build_environment`, `from ..policy import resolve_sandbox`, `from ..protocol import POLICY_SANDBOX, SubmitArgs`, `from ..salvage import SalvageError, git_head`; no `Sandbox`.
- **`docs/acceptance-contract.md`:** C-11.1, C-12.3 and C-12.4 merged by hand; both change logs kept.
- **`assemble`:** `self._provider_environment.assemble(credential_env)` calls it on the instance, the same way release's own call sites do since release made it an instance method. All five `assemble` calls in `daemon.py` go through `self._provider_environment`, as release's five do; three of them feed the guard override (probe, attempt, conversation turn).

**The contract merge, checked sentence by sentence.** Each clause was split into sentences at the merge base `d72597b5c`, at this branch's `7578a7cf2`, at release's `16b24033a` and at the merge:

| Clause | New sentences, this branch | New sentences, release | Missing from the merge |
|---|---:|---:|---:|
| C-11.1 | 7 | 10 | 0 |
| C-12.3 | 3 | 23 | 0 |
| C-12.4 | 46 | 4 | 0 |

No sentence that one side removed came back, and none that both sides kept was dropped. `2e7c51add` then amended C-12.3 and C-12.4 to match the merged code (below).

**Second, `94e011600`, with `473fe2941`:** one file conflicted, `docs/acceptance-contract.md`, and only in its change log, where both sides had added entries at the head. Every entry from both sides is kept: this branch's round-7 and round-8 entries first, then release's C-20.6, C-5.5, C-26.4 and C-6.9 to C-6.11 entries. Release's blank line before C-23.14 stays, and no clause body conflicted. `subfleet/daemon.py` and `tests/unit/test_sessions_handoff.py` merged cleanly:
- release's daemon hunks are D-ADM3's predecessor recheck in the admission pass and D-KILL2's `counted_identities` in the census; neither touches a launch, the guard override or the connection path;
- release's handoff test hunks are the CPU-scaling ratio constant and its docstring, which don't overlap this branch's sandbox tests.

`94e011600`'s message says "the request pool this branch changes". That is a slip: this branch changes no request pool, reader or dispatch path (CI item 2 below).

This branch shares no other non-test file with release's changes since `16b24033a` (`credentials.py`, `relay.py`, the CI workflow, `tools/`). Release's C-20.6 change to the test exec guard (#226) reaches this branch's publish-hook self-test, which runs `/bin/sh -c`. The briefed holds, `tests/e2e/test_review_publish_e2e.py` and the whole `tests/e2e` hold below ran after the merge.

### Round 8's items, checked against the commits

| `brief-fix8.md` item | State | Where |
|---|---|---|
| 1. The merge's code resolutions (`_guard_override`, `preflight`, `handoff` imports, `assemble`) | Done | `55e3f9472`; re-read at `94e011600` |
| 1. C-11.1, C-12.3 and C-12.4 merged with every rule from both; both change logs | Done | `55e3f9472`; the sentence check above |
| 1. Each merged clause says what the merged code does | Done | `2e7c51add` amended C-12.3 (the copy's git selects) and C-12.4 (the pins are launch additions) |
| 2. `LAUNCH_NAMES` for this PR's launch-owned names | Done | `2e7c51add`. `SUBFLEET_PUBLISH_DENIED` is the only `SUBFLEET_*` name this PR's source adds (a grep of every added line under `subfleet/` against release) |
| 2. `GIT_ALLOW_PROTOCOL` and `GIT_CONFIG_PARAMETERS` not withheld | Confirmed, no change needed | Neither matches `CREDENTIAL_PATTERN` or Codex's `KEY`/`SECRET`/`TOKEN` excludes, and neither is ambient (`allowed_name` is false for both), so a parent's value is never selected. They are `Launch.env_add` values, which `build_environment` adds after selection, and the guardian passes every name of the daemon-built environment. `test_review_publish_env_round8.py` asserts all of this for both providers and every review mode |
| 2. All of `tests/unit/test_launch_env*.py` | Pass | Hold 2 below |
| 3.1 The e2e harness | Done | `3260ac89e` |
| 3.1 Other e2e tests that submit reviews | Run | The `tests/e2e` hold below |
| 3.1 The trust refusal's own advice | Done | `d2dc1c734`, `36a89279f` |
| 3.2 The 3.12 connections failure | Done here; not this PR's | Below |

### CI item 1: `test_c9_5_a_refused_hard_review_runs_next_on_its_chains_next_model`

**Cause.** The brief put it down to the shipped `review: worktree-write` default. The first build found otherwise: that review is submitted `-s read-only`. Every Codex review launch, read-only included, carries the publish hook as the override's second trusted group (C-12.3, C-14.2). The fake app-server's hooks/list answered with the guard's entry only, so the preflight refused "did not list the review publish hook". CI's text came out as "refused the writable job (trust)" because a publish refusal was then kind `trust`, whose template assumes a writable job.

**Fix.** `3260ac89e` makes `tests/fake/interactive_codex.py` list every group the override's `state` trusts, each enabled and trusted with its own hash, the same way it lists the guard. With trust fixed, the launch then stopped at round 7's publish-hook self-test: its `/bin/sh -c` was refused by D-ST1's exec guard (`KeychainExecRefused`), because the harness's PATH reached a system directory before the session's keychain recorder. `tests/e2e/conftest.py` now puts the recorder first. Only a live test session arms that guard. `tests/e2e/test_review_publish_e2e.py` runs a policy review, the shipped default, on Codex and on Claude, end to end.

**The advice.** `d2dc1c734` and `36a89279f` give an untrusted publish group its own preflight kind, `publish`, and reason, `guard-preflight-publish`. Its template says the never-rules guard verified but Codex did not trust the review's publish hook. Its fix points at the attempt's `guard-preflight.json`, notes that doctor doesn't check this hook, and suggests a Claude model meanwhile. It no longer says "the writable job" or "rerun subfleet doctor". C-14.2 is amended, with its change-log entry.

### CI item 2: `test_c16_7_a_stopping_daemon_does_not_count_its_own_shutdown_as_departures` on 3.12

**This PR doesn't change the connection path.** At `94e011600`, these functions are identical, compared by AST source segment, to `origin/release/217`'s:
- `serve_forever`, `_hold_connection`, `_read_connection` and `_connection`;
- `_decode`, `_start_request`, `_respond` and `_run_worker`;
- `close`, `_descriptor_status`, `_count_connection`, `_refuse`, `_end_stream` and `max_connections`;
- `LOOKUP_OPS`.

`dispatch` differs in three places:
- `list`'s rows and `show`'s attempts gain `shell_flags`;
- `show` gains `uncommitted`;
- `daemon.status` gains `restricted_runs_24h`, one store query.

`Daemon.__init__` gains `_hook_self_tests`. No pool, executor or reader is added or changed.

**What the test waits for, and why it can time out.** The test does the following, in order:
1. It replaces the request pool with one worker.
2. It sends a blocking `readings` request.
3. It waits until `connections == 1`.
4. It sends three `daemon.status` requests and waits 5 s for the pool's queue to hold 3.

`connections` is counted in `_hold_connection` when the connection is admitted, before its reader thread starts, let alone reads. So step 3 doesn't mean the blocker's request has reached the worker. If a reader thread the scheduler runs late lets one `daemon.status` reach the single worker first, that request runs, the blocker takes the worker after it, and the queue never again holds 3.

This PR's `daemon.status` change can't cause that. The handler runs only once a request reaches the worker, and a slower handler would make a queue of 3 more likely, not less.

**Reproduced.** A scratch plugin (`-p`, not committed) delayed only the blocker's `readings` by 0.5 s between its read and its pool. Nothing else in the daemon or the test changed. The unchanged test then failed 5 of 5 runs on 3.12, at the same place as CI: `until(lambda: service.requests._work_queue.qsize() == 3)` at `:647`, "condition not reached in time" at `:78`. CI's timing matches too: its call took 5.18 s and its teardown 5.03 s, because the blocker's `release.wait(10)` ran out, its timer never having started.

**Repeated.** 50 runs on Python 3.12.14 in one hold, at load average 48 on 18 CPUs: 50 passed, 1.5 to 2.6 s each (hold 5).

**Verdict:** a race in the test's own ordering, not this PR. The test is unchanged, as the brief says. A fix for whoever owns C-16.7's tests would be to have the `readings` stand-in set an event when it starts, and wait for that event instead of `connections == 1`.

### Tests

**Setup:**
- Python 3.14.7, GIL build, in `.venv`, and Python 3.12.14 in `.venv312`, both synced `--locked --group dev`, with `PYTHONPATH` unset.
- One pytest process per hold of the shared suite lock, through the brief's `/usr/bin/lockf -k` command, run with `uv run --no-sync`. pytest runs in-process with a 1,140 s alarm, so each hold stays under 20 minutes.
- No xdist. `HYPOTHESIS_PROFILE=ci` and `SUBFLEET_LIVE=0`, as CI sets them.
- The job's `SUBFLEET_JOB`/`SUBFLEET_ATTEMPT` markers are left set, so person-only e2e tests skip.
- Load averaged 48 to 150 on 18 CPUs. Lock waits ran up to 39 minutes.

**The briefed holds, at `94e011600`:**

| Hold | Python | File | Passed |
|---|---|---|---:|
| 1 | 3.14 | `tests/fake/test_review_needs_shell.py` | 23 |
| 1 | 3.14 | `tests/fake/test_review_launch_permissions.py` | 34 |
| 1 | 3.14 | `tests/unit/test_review_publish_guard.py` | 135 |
| 1 | 3.14 | `tests/unit/test_publish_rule_round7.py` | 235 |
| 2 | 3.14 | `tests/unit/test_launch_env_allowlist.py` | 27 |
| 2 | 3.14 | `tests/unit/test_launch_env_helpers.py` | 20 |
| 2 | 3.14 | `tests/unit/test_launch_env_mutations.py` | 41 |
| 2 | 3.14 | `tests/unit/test_launch_env_policy.py` | 26 |
| 2 | 3.14 | `tests/unit/test_launch_env_round2.py` | 67 |
| 2 | 3.14 | `tests/unit/test_launch_env_round3.py` | 67 |
| 2 | 3.14 | `tests/unit/test_launch_env_round4.py` | 40 |
| 2 | 3.14 | `tests/unit/test_launch_env_round5.py` | 39 |
| 2 | 3.14 | `tests/fake/test_daemon_launch.py` (the brief's `tests/unit/` path doesn't exist) | 9 |
| 2 | 3.14 | `tests/unit/test_handler_refusals.py` | 344 |
| 3 | 3.14 | `tests/unit/test_hooks.py` | 75 |
| 3 | 3.14 | `tests/unit/test_hooks_native_guard.py` | 18 |
| 3 | 3.14 | `tests/unit/test_guard_trust.py` | 105 |
| 3 | 3.14 | `tests/unit/test_guard_overlay.py` | 22 |
| 4 | 3.14 | `tests/unit/test_codex_adapter.py` | 172 |
| 4 | 3.14 | `tests/unit/test_claude_adapter.py` | 172 |
| 4 | 3.14 | `tests/process/test_claude_isolation.py` | 15 |
| 5 | 3.12 | `tests/e2e/test_capacity_steer_e2e.py` | 2 |
| 5 | 3.12 | `tests/unit/test_daemon_connections.py`, with the C-16.7 stopping test run 50 times | 79 (29 + 50) |
| 6 | 3.14 | `tests/fake/test_state_contract.py` | 37 |
| 6 | 3.14 | `tests/unit/test_test_module_names.py` | 1 |

None failed or skipped. Hold 6 ran inside the `tests/e2e` hold, to save a lock wait.

**All 19 `tests/e2e` files, on 3.14 (hold 7):** 135 passed and 16 skipped, none failed:

| File | Passed | Skipped |
|---|---:|---:|
| `test_capacity_steer_e2e.py` | 2 | |
| `test_codex_turn_completed_e2e.py` | 3 | |
| `test_conversation_titles_e2e.py` | 9 | |
| `test_conversation_wakes_e2e.py` | 4 | |
| `test_conversations.py` | 29 | 15 |
| `test_guard_and_isolation.py` | 5 | |
| `test_http_isolation.py` | 1 | |
| `test_legacy_cockpit.py` | 1 | |
| `test_milestone1_codex.py` | 9 | |
| `test_milestone2_claude.py` | 7 | |
| `test_notice_job_state.py` | 1 | |
| `test_recovery.py` | 2 | |
| `test_reserve.py` | 4 | |
| `test_review_publish_e2e.py` | 4 | |
| `test_review_r2_strand_e2e.py` | 2 | |
| `test_review_r3_strand_timing_e2e.py` | 2 | |
| `test_stop_bound.py` | 3 | |
| `test_suggestions_e2e.py` | 1 | 1 |
| `test_suggestions_r1b_peer_e2e.py` | 8 | |

The 16 skips are person-only requests and marker refusals, which can't come from inside a Subfleet job ("run this test outside Subfleet (CI runs it)"). `test_capacity_steer_e2e.py` passed on both Pythons; CI had failed it on both.

**The demonstration hold** (3.12, the scratch late-blocker plugin): the C-16.7 stopping test failed 5 of 5, as described above. That is the expected result, not a regression.

**The demonstration plugin**, kept here because its scratch file is removed at cleanup. Run with `-p` and the C-16.7 stopping test's node id:

```python
def pytest_collection_modifyitems(session, config, items):
    from subfleet.daemon import Daemon
    real = Daemon._start_request

    def late(self, conn, write_lock, req, arrived, peer):
        if req.op == "readings":
            time.sleep(0.5)          # the blocker's reader, run late by the scheduler
        return real(self, conn, write_lock, req, arrived, peer)

    Daemon._start_request = late
```

### The whole-suite sweep (holds 8 to 11)

Each hold ran this PR's changed test files first, then `tests/fake`, then `tests/unit`. The last hold took every file left and was stopped by its 1,140 s alarm. All four ran on 3.14 at `94e011600`.

| Hold | Lock wait | Ran | Files | Passed | Failed |
|---|---:|---:|---:|---:|---:|
| 8 | 13 min | 18 min 44 s | 17 fake, 36 unit (every changed test file not already run) | 1,724 | 0 |
| 9 | 23 min | 9 min 11 s | 29 fake | 631 (and 2 skipped) | 2 |
| 10 | 33 min | 10 min 22 s | 57 fake (the 2 failures' files first), 3 unit | 832 | 0 |
| 11 | 28 min | 19 min 10 s, stopped | 3 fake, 94 unit (the last cut part way) | 2,746 | 0 |

**The two failures in hold 9 were timing, not this PR:**
- `tests/fake/test_census_escapers_daemon.py::test_d_kill2_a_setsid_lockf_that_outlives_a_cancel_holds_the_attempt[rooted]`, at 09:42:34Z;
- `tests/fake/test_codex_ca_bundle_fake.py::test_fake_codex_and_its_tool_inherit_ca_environment[default]`, at 09:43:33Z.

Each test's kept state root (`/tmp/sf-failed/…`) shows the same thing:
- The attempt was quarantined "start grace expired without a receipt" within a second of `attempt.starting`. The fake daemon's start grace is 0.65 s (`tests/fake/run_daemon.py`).
- The guardian had not yet left its parent's process group (pgid 38842 in both) and was in state `U` (uninterruptible wait).
- The CA test's fake provider still exited 0, with a `turn.completed` line, in 0.16 s. The job was already `lost`.

The start-grace deadline is set after the guardian is spawned. This PR's launch additions (`protect_launch`, the publish-hook self-test, `_record_launch`) all run before the spawn, and the self-test runs only for publish-denied launches. Both attempts were `publish_denied=0` and not reviews. Neither test file changed in this PR.

Hold 10 reran both whole files first: 9 of 9 and 6 of 6 passed. The two quarantines a minute apart, with guardians in `U`, look like a short host I/O stall.

**Per directory, each file's last full run, at `94e011600`:**
- **`tests/fake`:** all 108 files, 1,914 passed and 2 skipped, none failed. The skips are `test_admission_properties.py` and `test_canonical_identity_fix3.py`, one each.
- **`tests/e2e`:** all 19 files, 97 passed and 16 skipped (hold 7).
- **`tests/unit`:** 151 of 330 files, 5,729 passed, none failed or skipped.
  - `test_retention_archive.py` was cut part way by hold 11's alarm, after 65 passed.
  - 178 files are unrun.
- **`tests/process`:** 1 of 9 files (`test_claude_isolation.py`, 15 passed). The other 8 are unrun: `test_census_hidden_environment_process`, `test_census_raw_markers_process`, `test_census_responsibility_process`, `test_codex_isolation`, `test_guardian_process`, `test_guardian_qos_process`, `test_guardian_relay`, `test_pipe_above_stdio`.

**Why `tests/unit` is unfinished:** the four sweep holds waited 13 to 33 minutes each for the shared lock; at 10:20Z its holder was another session's whole-`tests/unit` run. At load 50 to 80, local runs took about three times CI's per-file seconds. The fourth hold got the lock at 11:03:50Z, eight minutes before this job's 4 h 30 m line (11:12Z), and no hold was started after it.

**The 178 unrun `tests/unit` files**, plus the rest of `test_retention_archive.py`:
`test_admission_uncapped`, `test_approval_resolution`, `test_capacity_steer_mutations`, `test_capacity_steer_route`, `test_census_counted`, `test_census_procargs`, `test_census_recycled_pid`, `test_census_responsibility_failures`, `test_census_responsibility_identity`, `test_census_responsibility_races`, `test_ci_retry_known_flakes`, `test_ci_shard_plugin`, `test_claude_cards`, `test_claude_identity`, `test_claude_large_stream`, `test_claude_mcp`, `test_claude_stream`, `test_claude_synthetic_error`, `test_codex_ca_bundle`, `test_codex_probe`, `test_codex_shim`, `test_codex_turn_completed`, `test_compat`, `test_continuation_r2`, `test_conversation_connections`, `test_conversation_diff`, `test_conversation_fifo_reads`, `test_conversation_handoff_briefs`, `test_conversation_history`, `test_conversation_reconcile`, `test_conversation_redact`, `test_conversation_state_reads`, `test_conversation_titles`, `test_conversation_waits`, `test_cpu_scaling`, `test_daemon_init_release`, `test_daemon_integration`, `test_daemon_log_scrubbing`, `test_daemon_refusals_cli`, `test_daemon_verbs`, `test_descriptor_review_fixes`, `test_descriptors_desktop`, `test_desktop_in_use`, `test_doctor_ca_bundle`, `test_doctor_identity`, `test_empty_provider_results`, `test_folders`, `test_forget_paced`, `test_frozen_provider_streams`, `test_gate_replay`, `test_gate_revision`, `test_gate_service`, `test_gate_verdict`, `test_guardian_qos`, `test_held_waiter_holdback`, `test_hidden_environment`, `test_host_push_admission`, `test_host_push_build`, `test_host_push_config_cost`, `test_host_push_copy`, `test_host_push_mutations`, `test_host_push_retention`, `test_host_push_storage_recovery`, `test_host_push_store_interleavings`, `test_host_push_trees`, `test_host_push_validated_top`, `test_invariants_index`, `test_keychain_exec_guard_properties`, `test_lane_answers`, `test_lease_waiter_selection`, `test_limit_from_model_text`, `test_live_store_guard`, `test_live_store_guard_round10`, `test_live_store_guard_round10_mutations`, `test_live_store_guard_round3`, `test_live_store_guard_round4`, `test_live_store_guard_round5`, `test_live_store_guard_round6`, `test_live_store_guard_round7`, `test_live_store_guard_round7_mutations`, `test_live_store_guard_round8`, `test_live_store_guard_round8_mutations`, `test_live_store_guard_round9`, `test_live_store_guard_round9_mutations`, `test_live_store_guard_state_root`, `test_lockwatch`, `test_native_operations`, `test_person_boundary`, `test_person_boundary_identity`, `test_placement_ramp`, `test_policy_disk`, `test_policy_tier_preferences`, `test_procs`, `test_protocol_secrecy`, `test_prune_decision_fingerprints`, `test_quota_projection`, `test_reap_liveness`, `test_rediscovery_git_trace`, `test_render`, `test_render_quota_projection`, `test_reset_credit_claim_timing`, `test_reset_credit_review_r1`, `test_reset_credit_review_r2`, `test_reset_credit_review_r3`, `test_reset_credit_review_r3_mutants`, `test_resource_identity`, `test_retention_archive_properties`, `test_retention_ordinary_trees`, `test_retention_qos`, `test_retention_regenerable_properties`, `test_retention_salvage_properties`, `test_retention_shared_folders`, `test_retention_worktrees`, `test_review_pr127_history`, `test_review_pr127_pr_wakes`, `test_review_pr127_requests`, `test_review_pr127_service`, `test_review_r3_measure`, `test_review_r3_repro`, `test_review_r4_repro`, `test_review_r5_repro`, `test_review_r6_probes`, `test_review_r6_restart`, `test_round10_refusal_context`, `test_route_check`, `test_salvage`, `test_salvage_optional_transient`, `test_salvage_unindexable`, `test_scheduler_priority_callers`, `test_scheduler_split`, `test_scheduler_tier_preferences`, `test_scheduler_turns`, `test_scheduler_weekly`, `test_scoped_stream_capacity`, `test_scrub_key_order`, `test_sessions_cli`, `test_sessions_handoff_excerpts`, `test_sessions_headless_properties`, `test_sessions_mirror`, `test_sessions_mirror_cache_coherence`, `test_sessions_mirror_fifo`, `test_sessions_mirror_hot_health`, `test_sessions_mirror_inventory`, `test_sessions_mirror_load_gap`, `test_sessions_mirror_safety`, `test_sessions_mirror_write_churn`, `test_sessions_nudge`, `test_sessions_registry`, `test_sessions_revive_reads`, `test_sessions_transcripts`, `test_settle_properties`, `test_steer_invariants`, `test_stop_alarm`, `test_stop_watchdog`, `test_store_contention_queries`, `test_store_properties`, `test_store_snapshot_plans`, `test_suggest_cli`, `test_suggest_hint`, `test_suggestion_error_boundaries`, `test_suggestion_properties`, `test_suggestion_secret_properties`, `test_suggestion_structural_errors`, `test_suggestions_r1b_adversarial`, `test_suggestions_r3_channels`, `test_suggestions_review_boundaries`, `test_suggestions_review_multiline`, `test_suggestions_review_response`, `test_timers_alerts`, `test_timers_daemon`, `test_title_review_regressions`, `test_turn_attachment_replay`, `test_turn_overlap`, `test_turn_runner`, `test_ultracode_effort`, `test_unadmittable_pin`, `test_wait_hub`, `test_wake_cli`

**Not run:**
- the files above, `tests/frontend` and `tests/live`;
- the suite as one process, and `pytest --collect-only` over the whole tree (`test_test_module_names.py`, the duplicate-basename guard, passed);
- a real provider turn;
- the person-only e2e tests, which skip inside a Subfleet job; CI runs them.

**Hold 11 also printed one warning**, at collection, from release's test code: `PytestRemovedIn10Warning` for a `zip` passed to `parametrize` in `tests/unit/test_codex_turn_completed.py`. It is not a failure, and that file is not this PR's.

## Round 9: one judgment decides gh on every path, and command names in any case

Round 9 (`brief-fix9.md`) answers review r3c (`review-r3c.md`, REQUEST_CHANGES: one P2 and two P3s). It ran as `20261021-211630-pr221-fix9` on a detached HEAD in its job worktree. Its base is `31b1ceb65`, which merges `origin/release/217` at `a26d08336` (#237, a C-16.7 test race fix that touches only `tests/unit/test_daemon_connections.py`) into `8d5330fa5`. This job's first attempt made that merge, and Subfleet kept it under `refs/subfleet-salvage/detached-20261022T070045Z-a1`, whose tree `31b1ceb65` equals. `fix/review-needs-shell-217` was not moved. The shared Git directory was writable, so no workspace-local metadata was needed. Nothing was fetched or pushed.

### Commits

| Commit | Change |
|---|---|
| `4045a3d3b` | P3: each guard wrapper call gets 60 s (`test_review_publish_guard.py:61`) and each round-7 push form's shell run 120 s (`test_publish_rule_round7.py:314`) |
| `ff2981af8` | Tests first: `tests/unit/test_publish_rule_round9.py` (regressions, the four invariants' properties, real hooks and shells, the PATH wrapper, offline differentials against real gh) |
| `8af90e3a6` | The credential property inserts value flags only where a gh call can have them |
| `93d3a3c66` | The fix: `_gh_path`; `gh_writes` returns the direct path's verdict; aliases and `ls` as data; `gh_read` skips flags before the verb; `auth git-credential` is a credential call. C-12.4 amended |
| `84986ac8d` | Tests first: command names in another case |
| `1eac2d15c` | Command names compared in any case. C-12.4 amended |
| `14ddf7139` | The real-shell test reaches gh through a glob word, not a brace word, which zsh refuses in command position |
| the commit after it | This section |

### P2: the fallback and run-time words refused only the verbs they could name

**Cause.** `_fallback` and `_runtime` judged gh with `gh_writes` (`publish_rule.py:327-347` at `8d5330fa5`). It refused a call only when the word right after the command (past leading `-R`, `--repo` or `--hostname`) was a verb listed in `GH_SUBCOMMANDS` that `gh_read` didn't accept. Everything else passed. So three things gh itself accepts got through `arch`, `$(command -v gh)`, a glob or brace word and a variable, though the direct path refused each:
1. gh's built-in aliases (`pr new`, `cs delete`), which `GH_SUBCOMMANDS` didn't list;
2. a flag before the verb (`gh pr -R o/r comment 7`): the word after `pr` was `-R`;
3. the hidden `gh auth git-credential get`, which prints `password=<token>`. `gh_credential` didn't name it, so even the direct path refused it with the `publish` wording.

**Fix.** One predicate, `gh_read`, decides gh everywhere:
- **Finding the command.** `_gh_path(args)` finds the command and subcommand the way gh's parser (cobra) finds them:
  - it reads level by level, from the first word each time;
  - at each level it takes the first word that is not a flag or a flag's value;
  - a flag without `=` takes the next word as its value, unless gh reads it as a switch at that level: `--help` everywhere, and `--version` only at the top (gh's `-h` is `--hostname`, never help);
  - a cluster such as `-Ro/r` takes no value, and `--` ends the search.
- **One verdict.** Once `_gh_path` reaches one of gh's commands (`GH_SUBCOMMANDS`, aliases included), or the call would print the credential, `gh_writes` returns `_gh_verdict`, the direct path's judgment: refused unless `gh_read` accepts the whole argument list, with the `credential` wording when `gh_credential` names it.
  - A first word gh doesn't know as a command is not judged there. That keeps C-12.4's stated limit for config aliases and extensions, so `grep gh file` and `echo gh` pass.
  - `_runtime` returns early only for a read; any other gh call gets `gh_writes`'s verdict.
- **Data, not code paths:**
  - `GH_SUBCOMMANDS` gains every alias gh 2.100.0's own `--help` lists, for each command and each subcommand, read offline on 2026-10-22. That is the review's nine subcommand aliases and six command aliases, plus `agents`, `agent-tasks`, `extensions`, `skill show`, `pr co` and every `ls`.
  - The command aliases are commands of their own (`GH_COMMAND_ALIASES`), with their command's subcommands.
  - So are `co` (the alias gh writes into a new config, `pr checkout`), cobra's `help`, and the hidden commands found by name: `credits`, `repo credits`, `repo garden` and `auth git-credential`.
  - `GH_READS` gains `ls` wherever it has `list`: `pr`, `issue`, `run`, `workflow`, `release` and `label`.
- **Flags before the verb.** `gh_read` skips `-R`/`--repo`/`--hostname`, each with its value, before the verb as well as before the command (P3, below). Any other flag there still makes a call no read.
- **Credentials.**
  - `gh_read` refuses any call `gh_credential` names, so the PATH wrapper now refuses `gh pr view 7 --show-token` too.
  - `gh_credential` finds `auth token`, `auth git-credential` and `auth status` with `_gh_path`, whatever flags come before the subcommand. Real gh ran `auth token` for `gh auth -h h token` and `gh auth -u x token`.
  - The credential deny text names `gh auth git-credential`.

**Beyond the review's three gaps.** Checking the fix against real gh turned up two more:
- **A verb's own flag before the command.** Real gh ran `pr comment` for `gh --body x pr comment 7` and `gh -b x pr comment 7`, and `api` with `--method POST` for `gh --method POST api repos/o/r`. The old fallback passed these, because `--body` stopped `_without_globals`; `_gh_path` finds the command past them.
- **Aliases the review didn't list:** `agents`, `agent-tasks`, `extensions`, `skill show`, `pr co` and `co`.

**Real gh's parser, offline.** Real gh 2.100.0 ran with a scratch `GH_CONFIG_DIR` and `HOME` and no token. Outside pytest it also ran under `sandbox-exec` with the network, DNS and the keychain denied. `gh <args> --help` names the command gh found and makes no request: its USAGE line names it, or, for an unknown subcommand, the error names the parent gh looked it up in. On all 80 argument lists `test_the_rule_finds_the_command_real_gh_finds` probes, `_gh_path` finds what gh finds. Some of them:

| `gh …` | gh found | the rule found |
|---|---|---|
| `pr -R o/r view 7` | `pr view` | `pr view` |
| `--body x pr comment 7` | `pr comment` | `pr comment` |
| `pr -b x comment 7` | `pr comment` | `pr comment` |
| `--method POST api repos/o/r` | `api` | `api` |
| `issue -R o/r new` | `issue create` | `issue new`, its alias |
| `cs ls` | `codespace list` | `cs ls`, its aliases |
| `auth -h h token` | `auth token` | `auth token` |
| `auth --hostname h git-credential get` | `auth git-credential` | `auth git-credential` |
| `--version pr comment 7` | `pr` | `pr` |
| `pr --web view 7` | `pr` | `pr` |
| `-h pr comment 7` | none (`unknown command "comment" for "gh"`) | none |
| `-- pr view 7` | none | none |

The first version of `_gh_path` read all the words in one pass, with `--version` a switch everywhere, and found `pr comment` for `gh --version pr comment 7`. The differential caught it: cobra reads the leading flags again at each level, and below the top level `--version` takes a value. `_gh_path` now reads each level from the start. Both readings refused that call.

`test_gh_subcommands_holds_every_command_subcommand_and_alias_real_gh_lists` reads every command's and subcommand's `--help` the same way. It checks that `GH_SUBCOMMANDS` and `GH_COMMAND_ALIASES` hold every command, subcommand and alias gh lists. It took 64 s here.

**The review's executed inputs**, each now a regression in `test_each_input_review_r3c_executed_is_refused` and `test_gh_auth_git_credential_is_refused_with_the_credential_wording`:

| Input | r3c's verdict | Now |
|---|---|---|
| `arch -arm64 gh pr new --title t --body b` | allowed | refused, `gh pr new` |
| `$(command -v gh) pr new --title t --body b` | allowed | refused, `gh pr new` |
| `{g,}h pr new -t t -b b` | allowed | refused, `gh pr new` |
| `arch -arm64 gh pr -R o/r comment 7 --body x` | allowed | refused, `gh pr comment` |
| `$(command -v gh) pr -R o/r comment 7 --body x` | allowed | refused, `gh pr comment` |
| `/opt/homebrew/bin/g? pr -R o/r comment 7 --body x` | allowed | refused, `gh pr comment` |
| `g=gh; $g pr -R o/r comment 7 --body x` | allowed | refused, `gh pr comment` |
| `arch -arm64 gh pr --repo=o/r comment 7 --body x` | allowed | refused, `gh pr comment` |
| `arch -arm64 gh auth git-credential get` | allowed | refused, `credential` wording |
| `gh auth git-credential get` | refused, `publish` wording | refused, `credential` wording |
| `gh pr new --title t --body b`, `$(command -v gh) cs delete --all`, `arch -arm64 gh pr comment 7 -b x` | refused | refused |

**Through the real command paths.** `test_each_input_is_denied_by_the_real_hooks_and_reaches_gh_past_them` takes 12 writes (the review's inputs and the new forms) and 5 reads. It runs them through the PreToolUse hooks a real review launch carries, in each provider's shells (Codex `zsh -c` and `zsh -lc`; Claude `zsh -c`, `zsh -lc` and its snapshot shape). Every one is denied with the right wording. With the hooks skipped, each shell runs the recording gh at the Homebrew position, which shows the input really reaches gh. The read forms are allowed by the hooks and reach the recorder once each.

One harness row failed in hold 1, in all five shells: `{gh,} pr new`. zsh parses a brace word in command position as a group and stops with a parse error, so it never ran gh. The review's `{g,}h pr new` fails in zsh the same way, though the rule should and does refuse it. `14ddf7139` uses `<brew>/g[h] pr new` there, which zsh expands to the recorder, and hold 2 passed all five shells.

**The PATH wrapper** (layer 3) already judged with `gh_read`. `test_the_path_wrapper_judges_gh_with_the_same_predicate` runs it on aliases, flags before the verb, git-credential, `--show-token` and the new reads.

### P3: the read form

`gh pr -R o/r view 7` and `gh pr --repo o/r view 7` are reads now, on every path. So are `--repo=o/r` and `-Ro/r` there, repeated flags, `-R` before the command, and `ls` where gh has it. gh reads each the same way (the table above). `test_reads_with_a_flag_before_the_verb_pass_on_every_path` covers 18 read forms through all 36 spellings.

### P3: two timeouts

`tests/unit/test_review_publish_guard.py:61` gives each wrapper call 60 s (it was 10 s), and `tests/unit/test_publish_rule_round7.py:314` gives each push form's shell run 120 s (it was 60 s), as the hub decided. The 23 ids review r3c reran had failed only on `TimeoutExpired` at load 150 to 250. `:151`, the round-7 spellings' shell run, keeps its 60 s, as the brief names only `:314`; it didn't fail in r3b's hold.

### Beyond review r3c: command names in any case

**Cause.** macOS's default APFS volume is case-insensitive, so the shell finds gh for `GH`, git for `GIT` and bash for `BASH`. On this host zsh and bash both ran the recording gh for `GH pr comment 7`. The rule compared command names case-sensitively, so it allowed:
- `GH pr comment 7` and `/opt/homebrew/bin/GH pr comment 7`;
- `arch -arm64 GH pr new`;
- `GIT push`, which the git pins would still refuse, since `GIT` runs the same git with the same environment;
- `BASH <<< 'gh pr comment 7'`, whose here-string it never read;
- `cp /opt/homebrew/bin/GH ./g`.

**Fix.** `_name` lowers the command name before every comparison. `_names_gh_binary` and the fallback's gh and git words do the same, and `GH_WORD` and `_mentions_publish` match in any case. Lowering only adds judgments: a builtin spelled in capitals (`EVAL`) is judged as the builtin, and a text searcher in capitals (`GREP`) keeps its pattern. `GH_TOKEN` and `GH_PAGER` still don't name gh. Tests: `test_a_command_name_in_another_case_is_judged_as_the_command` (19 forms), a credential pair, the reads that still pass, a property over every casing of gh with and without a path, and the real hooks in both providers on a case-insensitive filesystem (skipped on a case-sensitive one). C-12.4 states it, with its own change-log entry.

This is not in the review. It is the same class as its P2, a spelling that reaches gh past the rule, and it lies in the same code. The two commits (`84986ac8d`, `1eac2d15c`) stand alone if the hub would rather take it separately.

### The invariants

For every argument list `a` given to gh. Each is a Hypothesis property in `test_publish_rule_round9.py`: 300 examples, derandomized, no database.

1. **One judgment.** Wherever `a` reaches one of gh's commands as gh's parser finds them, reads, or would print the credential, every spelling gives the same verdict, and it is `gh_read`'s: none for a read, `credential` when `gh_credential` names the call, `publish` otherwise. The spellings (`test_one_judgment_decides_gh_on_every_path`):
   - **the direct path and the known wrappers (16):** `gh`, a path, `=gh`, `env`, `env X=1`, `command`, `nohup`, `sudo -u x`, `timeout 5`, `nice -n 5`, `exec`, `-`, `noglob`, a leading redirection, `cd . &&` and `true;`;
   - **the unknown runners (8):** `arch`, `lockf`, `taskpolicy`, `uv run`, `caffeinate … lockf`, a path through `arch`, `ssh` and `docker run`;
   - **the run-time words (12):** `$(command -v gh)` bare, quoted and in backquotes; `"$GH"`; `${GH:-gh}`; `{gh,}`; `{g,}h`; `g[h]`; `g?`; `g=gh; $g`; and two behind unknown runners;
   - **the argv forms.**
2. **Fail closed.** Such an `a` that `gh_read` doesn't accept is refused on every path, and any `a` it doesn't accept is refused on the direct path (`test_every_gh_call_gh_read_does_not_accept_is_refused`).
3. **Reads pass.** Every call in `GH_READS`, `ls` included, is allowed on every path with `-R`/`--repo` in any of its forms before the command, before the verb or after it, inserted only between words that are not a flag and its value (`test_every_read_passes_with_global_flags_where_gh_accepts_them`).
4. **Credentials.** `auth token`, `auth git-credential`, `auth status -t` or `--show-token`, and `--show-token` on any call are refused on every path with the `credential` wording. Value flags may come first, with their values, before the command or before `auth`'s verb (`test_credentials_are_refused_whatever_value_flags_come_first`, and 18 forms through all 36 spellings in `test_credential_calls_are_refused_everywhere_with_the_credential_wording`).

**The generator** (`gh_argument_lists`) draws:
- the command, from `GH_SUBCOMMANDS` (aliases included) and four unknown words;
- the verb, from that command's subcommands, its reads and the unknown words;
- up to three operands;
- up to three flags, each inserted anywhere, splitting another flag from its value included. They come from gh's global value flags (`-R`, `--repo`, `--hostname`, each form), verb flags gh accepts before the verb (`--body x`, `-b x`, `--json title`, `-X POST`, `--method GET`, `-f q=x`, `-h h`, `-u x`), and flags given alone (`--web`, `-t`, `--show-token`, `--help`, `--version`, `--`).

**Outside the domain, as C-12.4 states.** A first word gh doesn't know as a command (`arch -arm64 gh myalias 7`, `grep gh file`) is judged only on the direct path. `test_an_unknown_first_word_after_an_unknown_runner_is_not_judged_documented_limitation` is marked `documented_limitation`.

**An over-refusal this round accepts, stated in C-12.4.**
- **What:** a run-time word followed by a gh command word is judged as gh, whatever program it names. So `$HOME/.local/bin/uv run pytest` is refused as `gh run pytest`, and `"$GIT" status` as `gh status`.
- **Before:** only a known non-read verb after the word was refused, so `run pytest` passed.
- **What still passes:** spelled out, `uv run` and `git status`; and `"$VIRTUAL_ENV/bin/python" -m pytest` and `$PYTHON -m pytest`.
- **Why not read the word's last path component as the program** (`…/uv` is not gh): a splitting expansion can put gh first. That covers `${=x}/uv` in zsh, and every unquoted parameter in bash.

**A remaining edge, not new this round.** A run-time word whose value carries the start of a gh call, followed by words that read, is judged by those words alone. For example, `$(printf 'g%s pr comment --body' h) pr view` would run `gh pr comment --body pr view`, a comment on the PR of branch `view`. It belongs to C-12.4's stated limit, a command assembled at run time from pieces that never spell gh: the substitution's text doesn't spell gh. Where the text does spell gh (`c='gh pr comment --body'`), the assignment is judged and refused.

### Mutations

All four ran in one hold, one pytest process after another, each over `test_publish_rule_round9.py` (without the gh-tree differential) and three round-7 rule tests. Each was undone with `git checkout -- subfleet/publish_rule.py`, and the driver checked the file matched the commit again.

| Mutant | Failed | Named tests that failed (count of ids) |
|---|---:|---|
| (a) restore round 8's name-only `gh_writes` on the fallback and run-time paths | 210 | `test_a_flag_before_the_verb_is_refused_on_every_path` (154), `test_gh_aliases_and_hidden_verbs_are_refused_on_every_path` (42), `test_each_input_is_denied_by_the_real_hooks_and_reaches_gh_past_them` (5), `test_each_input_review_r3c_executed_is_refused` (3), `test_a_run_time_word_before_a_gh_command_word_is_judged_as_gh` (3), and the properties for invariants 1 and 2 |
| (b) drop the flag skipping: `gh_read` takes the word after the command as the verb, and `_gh_path` stops at the first flag | 744 | `test_reads_with_a_flag_before_the_verb_pass_on_every_path` (360), `test_credential_calls_are_refused_everywhere_with_the_credential_wording` (288), `test_a_flag_before_the_verb_is_refused_on_every_path` (84), the real hooks (5), the PATH wrapper (2), the real-gh differential, the properties for invariants 3 and 4, and round 7's credential test |
| (c) drop `git-credential` from `gh_credential` | 153 | `test_credential_calls_are_refused_everywhere_with_the_credential_wording` (144), the real hooks (5), `test_gh_auth_git_credential_is_refused_with_the_credential_wording` (2), the invariant-4 property, and the case credential test |
| (d) compare command names case-sensitively again | 20 | `test_a_command_name_in_another_case_is_judged_as_the_command` (15), the real hooks on this filesystem (2), the case credential test (2), the casing property |

Under (a), 3 of the review's 11 executed inputs fail: the flag-before-the-verb forms through `arch` and a glob word. With the aliases now in `GH_SUBCOMMANDS`, the old name-only judgment catches `pr new` and `cs delete`. The same forms after `$(command -v gh)` or `$g` stay refused because the text names gh and the word is only a parameter or a substitution.

### Tests

**Setup:**
- **Python:** CPython 3.14.7, GIL build, in a scratch venv synced `--locked --group dev` through the hub's setup wrapper, with `PYTHONPATH` unset.
- **Workers:** for the whole-suite sweep only, pytest-xdist 3.8.0 and execnet 2.1.2 were installed into that venv from the uv cache, offline, through the same wrapper, for `-n 4 --dist loadfile`. The briefed holds ran in one process, as CI's shards do.
- **The lock:** each hold ran through `suite_hold.sh`, calling the venv's `python -m pytest`, or `runpytest.py`, which runs pytest in-process with a 1,140 s alarm. No hold reached the alarm.
- **Environment:** `SUBFLEET_LIVE=0`, and Hypothesis storage in scratch. The job's `SUBFLEET_JOB`/`SUBFLEET_ATTEMPT` markers stayed set, as in round 8.
- **Host:** load averaged 10 to 120 on 18 CPUs. Lock waits ran 0 to 19 minutes. The second slot was closed at times: first by load over 120, then by free disk at 49 GB, under the 50 GB floor.
- **One lost hold.** The first attempt at hold 2 ran `uv run --no-sync`. uv waited 300 s for the shared cache lock (`~/.cache/uv/.lock`, held by another job's sync) and exited 2 before pytest started. Every later hold called the venv's python directly.

**Holds:**

| Hold | Head | What | Result |
|---|---|---|---|
| 1 | `1eac2d15c` | briefed group 1, with `test_publish_rule_round9.py` | 3,136 passed, 5 failed: the brace-word harness row in all five shells (above), fixed in `14ddf7139` |
| 2 | `14ddf7139` | briefed group 2, with hold 1's 5 failed ids | 226 passed |
| 3 | `14ddf7139` | briefed group 3 | 38 passed |
| M | `14ddf7139` | the four mutants, one pytest after another | each caught (above); the source restored |
| S1 | `14ddf7139` | all 108 `tests/fake` files, `-n 4` | 1,913 passed, 2 xfailed, 1 failed (below) |
| S2 | `14ddf7139` | unit chunk 1 (78 files), with `test_routing_end_to_end.py` again | 5,556 passed, 1 skipped |
| S3 | `14ddf7139` | unit chunks 2 and 3 (174 files) | 7,100 passed, 4 skipped |
| S4 | `14ddf7139` | unit chunk 4 (79 files) | 3,675 passed |
| 1b | `14ddf7139` | briefed group 1 again, with `tests/e2e/test_review_publish_e2e.py` | 3,145 passed |

The unit chunks split `tests/unit` by CI's per-file seconds (`.github/test-shards.json`), counting 25 s for each file the map doesn't list yet.

**Each briefed file, at `14ddf7139`.** None failed or skipped.

| File | Passed | Hold |
|---|---:|---|
| `tests/unit/test_publish_rule_round7.py` | 236 | 1b |
| `tests/unit/test_review_publish_guard.py` | 135 | 1b |
| `tests/fake/test_review_needs_shell.py` | 23 | 1b |
| `tests/unit/test_publish_rule_round9.py` (new) | 2,747 | 1b |
| `tests/e2e/test_review_publish_e2e.py` (not briefed: the publish hook end to end on both providers) | 4 | 1b |
| `tests/unit/test_daemon_integration.py` | 7 | 2 |
| `tests/fake/test_review_launch_permissions.py` | 34 | 2 |
| `tests/unit/test_hooks.py` | 75 | 2 |
| `tests/unit/test_guard_trust.py` | 105 | 2 |
| `tests/fake/test_state_contract.py` | 37 | 3 |
| `tests/unit/test_test_module_names.py` | 1 | 3 |

Most of `test_publish_rule_round9.py`'s 2,747 are its parametrized cases: each of 26 alias and hidden-verb calls, 12 flag-first calls, 18 read forms and 18 credential calls through all 36 spellings.

### The whole-suite sweep

Every file in `tests/unit` and `tests/fake` ran, at `14ddf7139`, in four workers.
- **`tests/fake`, all 108 files:** 1,913 passed, 2 xfailed (`test_admission_properties.py` and `test_canonical_identity_fix3.py`, one each, as in round 8), and 1 failed.
  - **The failure:** `test_routing_end_to_end.py::test_c11_5_socket_submission_and_cli_why_print_recorded_walk`. The fake daemon didn't answer within the harness's 5 s start wait (`tests/fake/conftest.py:88`). Its kept log stops after its two startup lines. It started at 09:45Z, with three other workers busy and load near 30.
  - **The rerun:** in S2, all 16 of the file's tests passed.
  - **Not this round's:** it failed in `daemon.start()`, before it submitted its research job. This round changes no daemon code, only `subfleet/publish_rule.py`. The PR leaves the test file unchanged; its one change to `tests/fake/conftest.py` removes the policy's `requires` key before the daemon starts, which adds nothing to the start.
- **`tests/unit`, all 331 files** (330 and the new one): 16,315 passed, 5 skipped, none failed.
  - `test_retention_ordinary_trees.py` skipped 4: `sortedcontainers==2.4.0` is not in this host's uv cache, an environment condition.
  - `test_host_push_build.py` skipped 1, an explicit local-repo benchmark.
- **Warnings:** release's `PytestRemovedIn10Warning` for a `zip` passed to `parametrize` in `tests/unit/test_codex_turn_completed.py`, as in round 8.

The sweep ran four workers per process, so it is not CI's single-process shard run. Its one failure was a start-time bound under that load, and the rerun passed.

### Not run

- **Other test directories:** the rest of `tests/e2e` (only `test_review_publish_e2e.py` ran), `tests/process`, `tests/frontend` and `tests/live`.
- **Python 3.12**, which CI also runs.
- **The suite as one process**, and `pytest --collect-only` over the whole tree. The duplicate-basename guard, `test_test_module_names.py`, passed.
- **Real providers:** no real provider turn, and no real gh beyond offline `--help`.
- **The sweep families review r3c didn't reach,** apart from command names in another case: here-strings, process substitution into `source`, leading redirections, `${=c}`, `"$@"` and `$*` through the new forms; the name-splitting family; wrappers other than those in the invariants' spellings; copies, `alias`, `function` and `hash -p`.
