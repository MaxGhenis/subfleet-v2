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
