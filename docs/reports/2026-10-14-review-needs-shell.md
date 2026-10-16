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

Base: `a0d24b46c1a3`, including the hub's merge of #181. The shared Git index is still read-only; round-4 commits use workspace-local `.git-local` metadata on `fix/review-needs-shell-217`, preserving the base's ancestry. No remote or caller checkout is written.

The baseline hold reproduced all nine reported failures in 215.63 seconds:

| File | Passed | Failed |
| --- | ---: | ---: |
| `tests/unit/test_handler_refusals.py` | 314 | 1 |
| `tests/fake/test_review_needs_shell.py` | 21 | 2 |
| `tests/fake/test_review_launch_permissions.py` | 9 | 6 |

The AST refusal check identifies two constructors without a public reason: the unknown pinned model and the unsatisfiable pinned shell requirement. Both need handler templates and checked details, including value-free fallbacks. The eight notice failures expect capability/publication evidence in the first line even though round 3 moved it to its own line. Comparing `f3b287771` with `a0d24b46c` confirms #181 changed neither `subfleet/render.py` nor these two fake test files. Production rendering already follows C-15.1 and needs no change.

Tests-first commit `fdaffe9e5` checks the exact first-line header independently of the renderer, capability/publication evidence on the second line, unchanged deliverable/output bytes, public wire refusals, and malformed or credential-shaped detail fallbacks. The focused regression run is queued on the shared lock; implementation and final validation results will be recorded below.

All holds use one foreground pytest process, the shared lock without a wait timeout, `env -u SUBFLEET_RETENTION_DORMANT`, no pytest cache, and scratch Hypothesis storage with the bounded `ci` profile. The sandbox cannot write uv's default cache, so `UV_CACHE_DIR` points inside the permitted task scratch directory. Dependencies installed successfully there. The conservative cutoff for starting holds is 2026-10-16 07:27 UTC (4 h 30 m after checkout creation).

Verification status: baseline complete; focused regression, final green groups, and all 17 `tests/e2e/test_*.py` files pending. Scratch data is retained until final cleanup; no logs, XML, JSON evidence, or bundles are staged.
