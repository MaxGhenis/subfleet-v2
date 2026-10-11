# Invariant dispositions

Milestone 0 of `plan.md` requires that "every invariant has keep, replace, or drop with an
owner" before code that depends on it is accepted. This document is that adjudication: one row
for each of the 220 invariants in `reports/A-invariants.md`, saying where the behaviour lives in
subfleet v2, which clause owns it, which test layer can prove it, and which module carries it.
Where the clause is one `acceptance-contract.md` already defines it reads `C-x.y`; where the
contract has no clause for the rule it reads `P-23.<n>` and `invariant-gaps.md` proposes the text.

Nothing here is a new invariant. Every row restates a ledger row; where the wording is shortened
the meaning is unchanged. `reports/A-invariants.md` remains the evidence — it holds the v1
`path:line` and the incident that produced each rule.

`invariants.json` carries the same rows for machines, with the column names in snake case
(`v1 location` is `v1_location`, `contract clause` is `contract_clause`, `acceptance owner` is
`acceptance_owner`, `v2 module` is `v2_module`, `test name` is `test_name`).
`tests/unit/test_invariants_index.py` asserts the two files agree, that all 220 ids are present
exactly once, that every disposition is legal, that no row cites a `C-x.y` clause the contract does
not define or a `P-23.<n>` clause `invariant-gaps.md` does not propose, and that no row is left
unresolved.

## How to read a row

- **disposition** — `keep`: the rule survives unchanged, even where the mechanism moves out of the
  bash runners into the guardian, the daemon, or an adapter. `replace`: the rule itself changes,
  and the **replacement** column states the v2 rule. `drop`: the rule does not carry over, and
  **notes** says why.
- **contract clause** — the single clause whose text actually states this rule. A `C-x.y` value is a
  clause `acceptance-contract.md` already defines. A `P-23.<n>` value is a clause *proposed* in
  `invariant-gaps.md` and not yet in the contract: the row was `GAP` in version 1 of this file, and
  the proposal is the text the integrator would fold in as section 23. A clause about the same
  subject area is not coverage — a clause covers a row only when a test of that clause would fail if
  the invariant were violated — so a row carries a `P-23.<n>` rather than an adjacent `C-x.y`. Some
  **notes** still read "GAP" where they explain why no existing clause covers the row; that is
  history, and it stays true until section 23 lands.
- **acceptance owner** — `unit` when a fixture, a temp store, or a temp git repo can prove it;
  `fake` when it needs the daemon end to end against the fake provider CLIs in a temp
  `SUBFLEET_HOME`; `process` when it needs real processes, signals, process groups, or git
  plumbing; `live` when only a real provider, a real credential, or real host state can prove it
  (`tests/live/`, opt-in behind `SUBFLEET_LIVE=1`, per C-20.1).
- **v2 module** — the module that owns the rule, not one that calls it. Modules named by a lane
  brief or by the contract are milestone 1 to 3. Modules for milestones 4 to 8
  (`subfleet/notices.py`, `subfleet/hooks.py`, `subfleet/timers.py`, `subfleet/keepalive.py`,
  `subfleet/alerts.py`, `subfleet/actions.py`, `subfleet/sessions/*.py`, `subfleet/gate.py`,
  `subfleet/importer.py`) are proposed here, because no lane brief names them yet.
- **test name** — a proposed pytest function name, unique across the 220 rows. Per C-20.5 the test
  that takes it names its clause in the docstring.
- A `drop` row has nothing to accept, so its clause, owner, module, and test columns read `n/a`.

## What decided each disposition

The ledger's own verdict is the default: 214 `keep`, 4 `keep-simplified`, 2 `drop-with-reason`.
The four `keep-simplified` rows (44, 57, 58, 116) become `replace`. Plan B rev 4 fixes four more:
rows 125 to 127, the learned-capacity estimator, are replaced by `provider` readings from the
Claude stream event and the two usage endpoints; row 171, keepalive without a run directory, is
replaced by the readings table; row 133, the Codex weekly-reset ordering, is explicitly kept.

Fifteen rows depart from the ledger beyond that: 6, 7, 8, 32, 49, 81, 87, 88, 94, 107, 114, 140,
143, 147, and 212. Each carries `verdict change:` in **notes**, appears under Replaced or Dropped
below, and is listed again in the lane report. The test applied to each was: does the obligation
this row imposes still bind v2? If it binds and only the code carrying it moved, the row is a
`keep`. If v2 imposes a different obligation instead, it is a `replace` and the new obligation is
written out. If v2 imposes none — because plan B removed the sensor, the flag, or the file the rule
governs — it is a `drop`.

The contract binds milestones 1 to 3. Behaviour belonging to milestone 4 (hooks and notice
delivery), 5 (timers, keepalive, alerts, reset credits, the mirror), 6 (tickle, muster, revive,
handoff), 7 (gates), or 8 (compatibility removal, guard parity) is often covered only at interface
level by C-18.1 and C-19.1, or not at all. Those rows were `GAP` on purpose: an accurate map of
what the contract does not yet say is more useful to the integrator than a clause stretched to
cover it. They now carry a `P-23.<n>` clause proposed in `invariant-gaps.md`, marked with the
milestone that owes it.

| id | invariant | v1 location | class | disposition | replacement | contract clause | acceptance owner | v2 module | test name | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Snapshot dirty work to a salvage ref with a temp index and `commit-tree`, never touching HEAD, the branch, the real index, or the worktree. | `bin/subfleet-codex:495-530`, `bin/subfleet-claude:319-354` | work-salvage | keep | n/a | C-13.1 | unit | subfleet/salvage.py | test_salvage_commit_tree_leaves_head_index_and_worktree_untouched | 2026-07-12: a kill advanced a shared checkout's `main` onto WIP, twice; v2 refs live under `refs/subfleet-salvage/`. |
| 2 | Never salvage through `git stash`, because the stash stack is repo-global across worktrees. | `bin/subfleet-codex:19-20`, `bin/subfleet-guard-hook:338-344` | work-salvage | keep | n/a | C-13.1 | unit | subfleet/salvage.py | test_salvage_leaves_the_git_stash_stack_empty | 7/17 near-miss `feedback_never_stash_in_shared_worktrees`; C-13.1's temp-index mechanism excludes stash, which mutates the shared stack. |
| 3 | Arm salvage on every exit path, including interrupt and terminate, and normalise the reported exit code. | `bin/subfleet-claude:430-432`, `bin/subfleet-codex:253-255,544` | work-salvage | keep | n/a | C-13.1 | fake | subfleet/salvage.py | test_kill_and_sigterm_still_produce_a_salvage_ref | Kills and idle SIGTERMs must still salvage; C-5.6 finalizes after containment and C-17.3 now owns code normalisation (130 cancelled). |
| 4 | Run the guard preflight before salvage is armed, so a refused launch snapshots and pushes nothing. | `bin/subfleet-codex:479-493`, test `tests/test_guard.py:1091` | safety-guard | keep | n/a | C-14.2 | fake | subfleet/guard/preflight.py | test_guard_preflight_refusal_leaves_no_salvage_ref | 2026-08-19: guard-port design note; C-13.1 secondary, since salvage starts only after an attempt records its baseline at `reserved`. |
| 5 | Refuse a salvage target branch named `main` or `master` before anything else runs. | `bin/subfleet-codex:138-146` | safety-guard | keep | n/a | C-13.2 | fake | subfleet/daemon.py | test_writable_job_on_main_or_master_refused_at_admission | The v1 salvage push force-pushes WIP on every exit; C-6.5 gives the exit 7 refusal and C-13.2 keeps the never-push-to-`main` half. |
| 6 | State plainly, before review, that `-b` pushes on every exit including success. | `bin/subfleet-claude:76-79`, `bin/subfleet-codex:71-74` | UX-contract | drop | n/a | n/a | n/a | n/a | n/a | verdict change: v2's `run` has no `-b` or push-on-exit flag (C-17.2) and salvage writes a local ref (C-13.1), so the public-repo disclosure has no referent; C-13.2 still refuses any push of a salvage ref to `main` or `master`. |
| 7 | If the private caller prompt cannot be removed, skip salvage and push entirely. | `bin/subfleet-claude:418-429`, `bin/subfleet-codex:532-542` | safety-guard | replace | The daemon keeps the prompt at `jobs/<job id>/prompt.md` outside the worktree, so salvage no longer depends on cleaning up a caller prompt and runs unconditionally under C-13.1; what may be pushed is governed by C-13.2. | C-2.3 | unit | subfleet/salvage.py | test_salvage_is_unconditional_and_the_ref_excludes_the_job_prompt | verdict change: C-2.3 moves the prompt out of the worktree and C-13.1 never pushes; G-review row 7. A caller `-p` inside `-C` still enters the local ref. |
| 8 | Delete only an owned prompt whose basename is `delegate-prompt-*.md` and which is the exact `-p` file. | `bin/subfleet-claude:238-259`, `bin/subfleet-codex:224-243` | safety-guard | replace | The daemon copies the caller's prompt into `jobs/<job id>/prompt.md` and never deletes a caller file; C-2.1 allows no write outside the state root beyond workdirs, worktrees, salvage refs, and the `-o` export. | C-23.1 | fake | subfleet/daemon.py | test_daemon_never_deletes_the_caller_supplied_prompt_file | verdict change: inherited env markers do not prove ownership, and no v2 component deletes caller files, so the ownership check has no subject. |
| 9 | Detach only by re-exec under a real `setsid`, never a bare `nohup` in the caller's process group. | `bin/subfleet-claude:302-317`, `bin/subfleet-codex:151-165` | process-survival | keep | n/a | C-5.1 | process | subfleet/guardian.py | test_guardian_setsid_child_survives_caller_group_sigterm | 2026-08-23: an account switch plus the desktop app's 15-minute idle SIGTERM killed three dispatches; milestone 1 acceptance covers it. |
| 10 | Give the detached child its own private copy of the prompt and treat inherited lane markers as untrusted. | `bin/subfleet-claude:261-269,284-298` | safety-guard | keep | n/a | C-2.3 | unit | subfleet/daemon.py | test_attempt_reads_its_own_prompt_copy_not_the_callers_file | Ownership forgery through env; C-5.1 gives the child only `SUBFLEET_JOB` and `SUBFLEET_ATTEMPT`, and ownership is a store row, never a marker. |
| 11 | Refuse a launch whose prompt, out, err, raw, lane log, or marker paths alias each other. | `bin/subfleet-claude:270-278` | work-salvage | keep | n/a | C-2.3 | unit | subfleet/daemon.py | test_attempt_artifact_paths_never_alias_each_other_or_the_export | A truncating redirect would eat the prompt; C-2.3 fixes every artifact name, so only `-o` is caller-supplied and C-6.3 leases it. G-review row 11. |
| 12 | Clear stale attestation markers as soon as the paths validate, before any provider call. | `bin/subfleet-claude:280-282` | provenance/attestation | keep | n/a | C-12.5 | unit | subfleet/adapters/claude.py | test_attestation_verdict_never_inherited_from_a_previous_attempt | Markers must describe only the current output; per-attempt `a<seq>/` directories (C-2.3) replace persistent marker files. G-review row 12. |
| 13 | Resolve `-m` through subfleet's canonical model table before dispatch, never through the provider CLI's alias table. | `bin/subfleet-claude:184-191` | provenance/attestation | keep | n/a | C-1.6 | unit | subfleet/policy.py | test_short_model_name_resolves_through_policy_not_provider_alias | 2026-09-02: `fable` resolved to the retired Fable 5; C-11.1's `retired` map and the policy hash carry the alias table in v2. |
| 14 | Locate the served-model evidence by the exact session UUID transcript and require exactly one match. | `bin/subfleet-claude:612-672` | provenance/attestation | keep | n/a | C-12.5 | unit | subfleet/adapters/claude.py | test_claude_attestation_requires_exactly_one_transcript_for_the_session_uuid | Claude's cwd-to-project encoding is lossy; the fixture half is unit, the real-transcript half runs under `SUBFLEET_LIVE=1` (C-20.1). |
| 15 | Treat an ambiguous transcript as terminal, breaking immediately and never retrying into a positive attestation. | `bin/subfleet-claude:667-672`; test `tests/test_claude_lane_script.py:1116` | provenance/attestation | keep | n/a | C-12.5 | unit | subfleet/adapters/claude.py | test_ambiguous_transcript_is_terminal_unattested_with_no_retry | Attestation must fail closed; C-12.5's "never a false positive" owns it. G-review row 15. |
| 16 | Check every assistant message's model and require the session id on each, not just the latest row. | `bin/subfleet-claude:674-701`; test `tests/test_claude_lane_script.py:1152` | provenance/attestation | keep | n/a | C-12.5 | unit | subfleet/adapters/claude.py | test_attestation_checks_every_assistant_message_model_in_range | Mid-run classifier swap to Opus; C-12.5 states per-message `model` but not the per-message session id, which only the single-transcript match approximates; model-downgrade fixture (C-12.7). G-review row 16. |
| 17 | Write neither marker when the evidence is inconclusive, so a caller requiring a pinned model fails closed. | `bin/subfleet-claude:724-727`, `bin/subfleet-claude:38-40` | provenance/attestation | keep | n/a | C-12.5 | unit | subfleet/adapters/claude.py | test_inconclusive_evidence_yields_unattested_never_attested | Silent model downgrades; C-15.1 secondary, requiring the notice to carry `unattested` so a pinned caller sees the uncertainty. |
| 18 | On a model mismatch, record the downgrade and tell the caller to re-run or route to a non-Claude reviewer. | `bin/subfleet-claude:737-743` | provenance/attestation | keep | n/a | C-12.5 | unit | subfleet/adapters/claude.py | test_model_mismatch_records_served_model_and_downgrade_advice | Downgrade protocol; C-12.5 records the served model, but C-15.1's uncertainty list is `quarantined`, export failed, `unattested` only, so the re-run or re-route advice is an uncovered milestone 2 half. |
| 19 | Bound retries for delayed transcript persistence to a small default (4 tries, 1 s backoff) and no more. | `bin/subfleet-claude:178-179,719-722` | provenance/attestation | keep | n/a | C-23.40 | unit | subfleet/adapters/claude.py | test_transcript_persistence_wait_is_bounded_then_unattested | Transcripts land slightly after the run; no clause bounds that wait, so it is a milestone 2 Claude-adapter gap. G-review row 19. |
| 20 | Treat rc 0 with a bad or empty envelope as a failure, never as success. | `bin/subfleet-claude:815-826,895`, `bin/subfleet-codex:565,598` | UX-contract | keep | n/a | C-12.6 | unit | subfleet/adapters/base.py | test_rc_zero_with_empty_deliverable_classifies_unknown_not_ok | Empty deliverables reported as success; C-12.6 makes it class `unknown` and C-9.2 keeps the raw rc beside the class. G-review row 20. |
| 21 | Reserve exit codes 4 and 5 for text-classified limit and auth outcomes, and remap a raw provider rc of 4 or 5 to 1. | `bin/subfleet-claude:882-886,896` | capacity-truth | keep | n/a | C-17.3 | fake | subfleet/cli.py | test_raw_provider_rc_four_or_five_maps_to_one_not_limit_or_auth | No date; `C-17.3` reserves 4 and 5 but its remap sentence covers only a provider rc outside the table, so the 4/5 remap is a GAP the contract must state. |
| 22 | Never sleep on a hard account limit: re-pick the next candidate lane at once, and fail a pinned run fast with rc 4. | `bin/subfleet-claude:864-878` | routing-policy | keep | n/a | C-4.5 | fake | subfleet/daemon.py | test_hard_limit_repicks_next_candidate_without_sleeping | No date given; secondary `C-11.2` (pins never fall back) and `C-17.3` exit 4; only `transient` waits, 60 s, per `C-9.5`. |
| 23 | Confirm an auth-looking phrase against the live OAuth usage endpoint before declaring the lane's credential dead. | `bin/subfleet-claude:492-511,842-860` | identity | keep | n/a | C-9.3 | unit | subfleet/adapters/claude.py | test_auth_phrase_with_a_live_token_is_not_auth_dead | 2026-09-03: six live lanes parked 30 days on a phrase match; the core is provable on fixtures, the real 401 half belongs to `doctor --live`. |
| 24 | Keep an organisation block at auth-dead without a probe, since a valid token cannot help there. | `bin/subfleet-claude:20-23,845-848` | identity | keep | n/a | C-9.3 | unit | subfleet/adapters/claude.py | test_organisation_block_message_is_auth_dead_without_a_probe | 2026-08-23: seen on max@policybench.org; the opposite branch of row 23 — the block message is itself sufficient evidence. |
| 25 | Treat a "does not support this model" 400 as CLI-too-old naming `claude update`, with no lane rotation and no cooldown. | `bin/subfleet-claude:833-838`, `subfleet/tickle.py:71-88` | ops-hygiene | keep | n/a | C-9.2 | unit | subfleet/adapters/claude.py | test_model_not_supported_message_classifies_cli_too_old | 2026-09-02: a Homebrew cask frozen at 2.1.87 shadowed the real CLI; exit 6 per `C-17.3`, no retry per `C-4.5`, no closure because only `limited` writes one (`C-9.4`). Fixture premise for C-20.1's live suite: that an old CLI answers 400 with this message. |
| 26 | Resolve the Claude binary through an explicit override, then `~/.local/bin/claude`, then `PATH`. | `subfleet/paths.py:147-165`, `bin/subfleet-claude:160-171` | ops-hygiene | keep | n/a | C-23.21 | unit | subfleet/adapters/claude.py | test_claude_binary_resolution_prefers_local_bin_over_launchd_path | 2026-09-02: launchd `PATH` puts `/opt/homebrew/bin` first. The order is unchanged; only the override variable is renamed under the single `SUBFLEET_*` prefix (row 219). GAP, milestone 2: no clause states provider-binary resolution. Compare row 158. |
| 27 | Unset `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` in the lane child so work bills the subscription. | `bin/subfleet-claude:796`, `subfleet/keepalive.py:157-168` | routing-policy | keep | n/a | C-12.4 | process | subfleet/adapters/claude.py | test_claude_child_inherits_no_anthropic_api_key_or_auth_token | Subscription-only doctrine; `C-12.4` names only `ANTHROPIC_API_KEY`, so `C-14.4`'s isolation matrix must also assert `ANTHROPIC_AUTH_TOKEN`. |
| 28 | Make read-only mode fail closed even under `bypassPermissions` settings: plan mode, `--tools`, empty `--setting-sources`, safe mode, strict empty MCP, no Chrome, no slash commands. | `bin/subfleet-claude:769-792` | safety-guard | keep | n/a | C-12.4 | unit | subfleet/adapters/claude.py | test_read_only_claude_argv_fails_closed_against_bypass_permissions | Settings-driven escalation, no date; `C-12.4` delegates to v1's flag builder, so the whole flag set must be asserted argv by argv. |
| 29 | Require `-s read-only` and `-D` for `-I`, expose sources through `--add-dir`, and allow only Read, Glob and Grep. | `bin/subfleet-claude:135-141,778-791` | safety-guard | keep | n/a | C-23.2 | unit | subfleet/cli.py | test_isolated_review_requires_read_only_sandbox_and_a_review_root | Isolated review integrity, no date; `C-17.2`'s flag list has no `-I`/`-D`, so the isolated-review mode is unassigned to any milestone. |
| 30 | Refuse an isolated review that inherits `CLAUDE_CODE_MANAGED_SETTINGS_PATH` or the remote/mock settings vars rather than silently discarding operator policy. | `bin/subfleet-claude:143-152`; test `tests/test_claude_lane_script.py:446` | safety-guard | keep | n/a | C-23.3 | unit | subfleet/cli.py | test_inherited_managed_settings_path_refuses_the_submission | A parent could aim trusted hooks at the reviewed checkout; add to `C-6.5`'s refusal list (milestone 1); tied to the `-I` mode missing from `C-17.2`. |
| 31 | In read-only mode, unset the memory and CLAUDE.md inheritance variables before Claude initializes. | `bin/subfleet-claude:797-806` | safety-guard | keep | n/a | C-23.2 | unit | subfleet/adapters/claude.py | test_read_only_launch_env_drops_memory_and_claude_md_inheritance_vars | No date; `--add-dir` would otherwise load the reviewed repo's rules, and `C-12.4` names only the API-key removal, so these vars need enumerating. |
| 32 | Unset every `SUBFLEET_RUN_*` identity variable after recording the run so nested dispatches cannot inherit it. | `bin/subfleet-claude:747-749`, `bin/subfleet-codex:257-260` | provenance/attestation | replace | A nested submission never takes its identity from the inherited environment: the daemon mints a new job id for it, and an inherited `SUBFLEET_JOB` is usable only as `--parent`. | C-23.41 | fake | subfleet/daemon.py | test_nested_submission_mints_a_new_job_id_instead_of_inheriting_one | Id hijack by a child dispatch. Verdict change: C-5.1 and C-5.5 require `SUBFLEET_ATTEMPT` to stay in the child environment for containment, so v1's unset is impossible; GAP, milestone 1. Secondary C-7.3. |
| 33 | Record the lane's provider session id at launch, not only at finish. | `bin/subfleet-claude:760-766` | session-continuity | keep | n/a | C-12.2 | fake | subfleet/daemon.py | test_native_session_id_is_recorded_before_the_provider_exits | 2026-09-04: a fleet-wide notice reached three running lanes and overwrote their deliverables (commit 0117e9e); `C-12.4` pins `--session-id`. |
| 34 | Prefer the transcript's final assistant text when the JSON `.result` rendering is shorter, keeping the envelope copy beside it. | `bin/subfleet-claude:513-565` | work-salvage | keep | n/a | C-12.6 | unit | subfleet/adapters/claude.py | test_transcript_text_wins_over_a_shorter_stream_result_rendering | 2026-09-04: a 64,613 B out file silently lost 1,912 interior characters; the envelope copy stays as its own artifact role under `C-8.2`. |
| 35 | Call the accounting hook synchronously and let no hook failure change the run outcome. | `bin/subfleet-claude:578-592`; test `tests/test_claude_lane_script.py:557` | ops-hygiene | keep | n/a | C-23.22 | fake | subfleet/daemon.py | test_finalizing_accounting_failure_leaves_the_job_outcome_unchanged | No date; retries would overwrite err/raw before parsing, a hazard per-attempt dirs (`C-2.3`) remove, and `C-8.3` states the same rule for export. |
| 36 | Start the durable ledger record before lane selection and guard preflight. | `bin/subfleet-codex:175-205` | ops-hygiene | keep | n/a | C-4.1 | fake | subfleet/daemon.py | test_job_row_is_durable_before_lane_selection_and_guard_preflight | No date; pre-provider failures must leave a finalized job, `C-16.3` returns a job id at submit, and `C-6.3` records the decision at attempt admission. |
| 37 | Refuse to adopt a finished or unknown run id and start a fresh record instead. | `bin/subfleet-codex:181-194`, `subfleet/run_ledger.py:325-355` | provenance/attestation | keep | n/a | C-6.2 | fake | subfleet/daemon.py | test_unknown_request_id_creates_a_fresh_job_and_a_mismatch_is_refused | No date; a stale inherited id could hijack another record, and `C-4.3` carries the attempt-side half: a stale attempt is never accepted. |
| 38 | Add the worktree's git common dir to `sandbox_workspace_write.writable_roots`. | `bin/subfleet-codex:309-315` | work-salvage | keep | n/a | C-12.3 | unit | subfleet/adapters/codex.py | test_codex_writable_roots_include_the_worktree_git_common_dir | No date; external worktrees could not commit (`index.lock: Operation not permitted`), and `C-14.4`'s matrix proves the sandbox half at process level. |
| 39 | Never retry a Codex content-filter refusal; fail it fast and ask for a defensive prompt rewrite. | `bin/subfleet-codex:567-570` | routing-policy | keep | n/a | C-4.5 | fake | subfleet/daemon.py | test_content_filter_refusal_is_never_retried | 2026-09-01: rc 3 on security-flavoured audits; v1's exit 3 changes meaning in v2 (no lane, `C-17.3`), and the class comes from `C-9.2`. |
| 40 | Write a 15-minute lane cooldown immediately on a dispatch usage limit, then re-pick. | `bin/subfleet-codex:571-589` | capacity-truth | keep | n/a | C-9.4 | fake | subfleet/daemon.py | test_usage_limit_writes_a_closure_before_the_next_lane_is_picked | No date; the 15 min becomes the provider clock or a guessed now + 3600 s, and `C-11.2` then excludes the closed lane from candidates. |
| 41 | Re-run the guard preflight on a re-picked lane and stop rather than launch unguarded. | `bin/subfleet-codex:577-584` | safety-guard | keep | n/a | C-23.5 | fake | subfleet/guard/preflight.py | test_guard_preflight_reruns_before_a_repicked_lane_launches | No date given; C-14.2 scopes the preflight to the first Codex job, so key it per lane home and re-verify on a new lane (C-4.6). |
| 42 | Require the thread's original `-H CODEX_HOME` on a Codex thread resume and forbid an account override, because threads are home-local. | `bin/subfleet-codex:128-137`, `subfleet/resume_codex.py:121-130` | session-continuity | keep | n/a | C-12.3 | unit | subfleet/adapters/codex.py | test_codex_resume_requires_the_threads_original_home | Secondary C-4.6: an attempt never changes lane. v2 `resume <id>` carries no account flag, so v1's `-T`/`-A` conflict disappears. |
| 43 | Refuse an API-key `auth.json` home with exit 7 before any codex call and drop an exported `CODEX_API_KEY`. | `bin/subfleet-codex:275-307`, `subfleet/codex.py:91-134`, `bin/codex:48-52`, `subfleet/delegate.py:835-838` | routing-policy | keep | n/a | C-6.5 | fake | subfleet/adapters/codex.py | test_api_key_codex_home_refused_before_any_codex_call | 2026-09-04: lanes are subscription-only. Env scrub is C-12.3, enrolment refusal C-10.2, and the C-14.4 matrix asserts no inherited `CODEX_API_KEY`. |
| 44 | Duplicate the API-lane check in self-contained Python inside the runner so it holds without the wrapper. | `bin/subfleet-codex:283-300` | routing-policy | replace | One API-key-home check in `subfleet/adapters/codex.py`, consulted at enrolment, at admission (exit 7), and again in the launch builder; the bash duplicate goes with v1's runners. | C-6.5 | unit | subfleet/adapters/codex.py | test_codex_launch_builder_refuses_an_api_key_home_without_the_cli | keep-simplified: no runner or wrapper survives in v2, so one Python check serves enrolment (C-10.2), admission (C-6.5), and launch. |
| 45 | Disable apps, plugins, hooks, multi-agent, browser, memories, shell snapshots, history persistence, and web search for an isolated Codex review. | `bin/subfleet-codex:322-360` | safety-guard | keep | n/a | C-23.2 | unit | subfleet/adapters/codex.py | test_codex_launch_disables_hosted_tools_and_history_persistence | GAP, milestone 1: v1's `-I` isolated review has no v2 flag and C-12.3 enumerates argv without the hosted-tool disables; a read-only sandbox does not govern them. |
| 46 | Refuse an isolated Codex review when managed requirements or nonempty system or project config layers exist. | `bin/subfleet-codex:368-472` | safety-guard | keep | n/a | C-23.3 | unit | subfleet/guard/preflight.py | test_codex_isolation_refuses_managed_or_project_config_layers | GAP, milestone 1: C-14.2's preflight checks version, hash, and override only; unit-test the refusal against a recorded `config/read` layer response. |
| 47 | Re-prepare isolation before every attempt, since lane rotation changes the config and the inventory. | `bin/subfleet-codex:323-328` | safety-guard | keep | n/a | C-23.4 | unit | subfleet/adapters/codex.py | test_isolation_is_rebuilt_from_the_new_lane_on_every_attempt | C-12.1's `build_launch(job, attempt, lane, ...)` is per attempt; with C-4.6 (a new lane is a new attempt) no isolation state carries across lanes. |
| 48 | Never log Codex config while preparing isolation, because it can contain credential values. | `bin/subfleet-codex:366-367` | safety-guard | keep | n/a | C-10.5 | unit | subfleet/adapters/codex.py | test_codex_isolation_probe_never_logs_config_or_credentials | C-10.5 forbids logging a credential anywhere; Codex config layers can carry one, so the isolation probe's config output never reaches `lane.log` or `manifest.json`. |
| 49 | Arm the nine NEVER rules on every codex launch through the session-flags layer with the trust hash pinned. | `bin/subfleet-guard:115-132`, `bin/subfleet-codex:485-493` | safety-guard | replace | The guard override is armed on every `workspace-write` Codex launch with the trust hash pinned (C-14.3, C-14.1); a `read-only` Codex launch carries no override and relies on the sandbox. | C-14.3 | unit | subfleet/adapters/codex.py | test_codex_workspace_write_launch_carries_the_pinned_guard_override | 2026-08-08 load 47 and 2026-08-18 load 57 crawls, ported 2026-08-19. Verdict change: C-14.3 arms the override only on `workspace-write` launches, so a read-only Codex job loses the crawl rules; open question for the integrator. |
| 50 | Never write the lane's `CODEX_HOME`: probe a scratch home seeded with copies of `config.toml` and `hooks.json`. | `bin/subfleet-guard:237-250`, `docs/guard.md:81` | ops-hygiene | keep | n/a | C-23.23 | process | subfleet/guard/preflight.py | test_guard_preflight_probes_a_seeded_scratch_home_leaving_the_lane_home_untouched | 2026-08-18 23:53: the first preflight rewrote `~/.codex-5/config.toml` by personality migration. GAP, milestone 1: C-14.2 is silent on the scratch home; C-2.1 bars only homes outside `$SUBFLEET_HOME`. |
| 51 | Refuse to launch unless `hooks/list` reports the hook enabled and trusted. | `bin/subfleet-guard:314-328`, `bin/subfleet-codex:490` | safety-guard | keep | n/a | C-14.2 | fake | subfleet/guard/preflight.py | test_guard_preflight_refuses_launch_when_hooks_list_reports_untrusted | A codex upgrade must not silently skip the guard. Constant change: the refusal is exit 7 per C-17.3, not v1's exit 2. Fixture premise for C-20.1's live suite: that the installed CLI's `hooks/list` reports enabled and trusted. |
| 52 | Key the preflight cache on codex version, home, override, and seeded-config fingerprint, and prune markers after 30 days. | `bin/subfleet-guard:186-194,229-231,320-322` | safety-guard | keep | n/a | C-23.5 | unit | subfleet/guard/preflight.py | test_guard_preflight_cache_key_covers_version_home_override_and_seed_config | Implemented 2026-09-20 (`cache_key`, `seed_fingerprint`, `read_cached_verdict`, `write_cached_verdict`, `prune_cached_verdicts` in `subfleet/guard/preflight.py`; markers under `$SUBFLEET_HOME/guard-cache`, `SUBFLEET_CODEX_GUARD_CACHE` overrides): a `features.hooks=false` edit changes the fingerprint and re-verifies; a refusal is never cached; the hook bytes, TRUST pins, jq and the Codex version are checked on every call. |
| 53 | Bound the preflight: fail immediately if the app-server dies unanswered, and TERM then KILL the probe tree at the deadline. | `bin/subfleet-guard:196-210,262-286` | ops-hygiene | keep | n/a | C-23.23 | process | subfleet/guard/preflight.py | test_guard_preflight_terminates_then_kills_its_probe_tree_at_the_deadline | GAP, milestone 1: C-14.2 sets no deadline; the npm launcher forwards TERM so KILL alone orphans the binary; reuse C-5.6's escalation shape. |
| 54 | Fail open on malformed hook input, a missing `jq`, or a tool other than Bash or apply_patch. | `bin/subfleet-guard-hook:48-50,70-88` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_fails_open_on_malformed_input_missing_jq_or_other_tools | Preserved by C-14.1's byte-for-byte copy; the test drives the copied hook with fixture payloads so a broken guard never blocks all work. |
| 55 | Write guard telemetry first and fail-safe so a logging failure never changes the decision. | `bin/subfleet-guard-hook:104-112`; test `tests/test_guard.py:700` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_denial_survives_a_telemetry_write_failure | Preserved by C-14.1's byte-for-byte copy; the test points the log at an unwritable path and asserts the denial still stands. |
| 56 | Make the guard's `check` verb a dry run that never writes the real denial log. | `bin/subfleet-guard:175-176`; test `tests/test_guard_never_rules.py:1119` | ops-hygiene | keep | n/a | C-23.24 | unit | subfleet/guard/preflight.py | test_guard_check_dry_run_never_writes_the_real_denial_log | GAP, milestone 8 guard parity: C-17.1 lists no guard-check verb; C-19.1's rule that a dry run never acts is the analogue. |
| 57 | Keep `unscoped-search` as one shared region byte-identical with the Claude hook and never edit the Codex copy. | `bin/subfleet-guard-hook:16-23,364,627` | safety-guard | replace | v2 holds one hook file copied byte for byte from v1 and pinned by SHA-256 in `subfleet/guard/TRUST`; parity with the global Claude hook is a whole-file hash check and the v2 copy is never edited. | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_matches_the_upstream_claude_hook_byte_for_byte | keep-simplified: two hooks judging differently is the failure mode; the global Claude hook stays upstream (C-14.3) so parity is still asserted, not assumed. |
| 58 | Copy the seven Claude rule blocks byte for byte, pin the drift with tests, and edit the Claude hook first. | `bin/subfleet-guard-hook:5-15`, `docs/guard.md:99-102` | safety-guard | replace | The seven per-block byte-identity tests become one whole-file SHA-256 pinned in `subfleet/guard/TRUST`; rule edits land in the global Claude hook upstream and the file is re-copied wholesale. | C-14.1 | unit | subfleet/guard/TRUST | test_guard_hook_sha256_matches_the_pinned_trust_value | 2026-08-19 port contract. keep-simplified: one file hash replaces seven block-level drift tests; the pin must be updated in the same commit as a re-copy. |
| 59 | Allow no override for a blocked crawl: not an env var, not a magic comment, nothing in the command text. | `bin/subfleet-guard-hook:408-409` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_crawl_block_admits_no_override | 8/8 and 8/18 incidents; preserved by C-14.1's byte-for-byte copy, and the test replays each attempted override form. |
| 60 | Let any `-maxdepth` or `--max-depth` disarm the crawl rule. | `bin/subfleet-guard-hook:451` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_crawl_rule_disarmed_by_a_depth_bound | No date given; preserved by C-14.1's byte-for-byte copy, since depth-bounded searches are fine and must stay allowed. |
| 61 | Judge broad roots only in path position, after collapsing quoted strings while keeping searcher-bearing sub-scripts visible. | `bin/subfleet-guard-hook:414-443,460-498` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_judges_broad_root_only_in_path_position | 2026-08-19: false denials on `ls -1 /Users/... &#124; rg ...`; rule survives byte for byte under C-14.1, exercised as a subprocess against payload fixtures. |
| 62 | Deny `find .` or `rg pat .` when the payload cwd is itself a broad root. | `bin/subfleet-guard-hook:615-621` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_denies_dot_search_from_broad_root_cwd | Rationale: crawl by cwd. Preserved by the byte-identical hook copy; the cwd broad-root list is part of the pinned file. |
| 63 | Mirror Codex's own patch parser for hf-dest, including Rust `str::trim()` Unicode whitespace and case-folded path matching. | `bin/subfleet-guard-hook:121-203,244-251`, `docs/guard.md:27-28` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_hf_dest_parser_matches_codex_unicode_and_case_folding | 2026-08-20 adjudication: NBSP headers and APFS case aliases bypassed the scan; parser survives byte for byte under C-14.1. |
| 64 | Scan only decisive hf-dest bases: the payload cwd plus `cd` targets containing the repo name. | `bin/subfleet-guard-hook:237-245` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_bounds_hf_dest_bases_against_cd_flood | A 71 KB, 6000-`cd` command took about 72 s and timed the hook out into fail-open; test asserts a wall-clock bound. |
| 65 | Document the `write_stdin` PTY guard bypass and provide `SUBFLEET_CODEX_UNIFIED_EXEC=off` to close it. | `bin/subfleet-codex:60-69`, `docs/guard.md:146` | safety-guard | keep | n/a | C-23.6 | unit | subfleet/adapters/codex.py | test_codex_launch_can_disable_unified_exec_to_close_write_stdin_hole | 421 unguarded `write_stdin` calls in one sampled rollout; C-12.3 mirrors v1 argv but names no unified-exec switch, and section 14 omits the hole. Milestone 1. |
| 66 | Force a deterministic `/usr/bin:/bin` PATH prefix inside the hook. | `bin/subfleet-guard-hook:66-67` | safety-guard | keep | n/a | C-14.1 | unit | subfleet/guard/never-rules-hook.sh | test_guard_hook_forces_deterministic_path_prefix | Rationale: caller PATH must not change rule behaviour; test runs the pinned hook with a hostile PATH and shadowed binaries. |
| 67 | Block provider runners launched straight from a session's Bash tool and name the `subfleet run` replacement in the message. | `bin/subfleet-hook:40-68`, `subfleet/hooks.py:10-15` | process-survival | keep | n/a | C-23.54 | unit | subfleet/hooks.py | test_session_hook_blocks_direct_provider_launch_and_names_subfleet_run | 2026-08-23, observed three times; v1 already blocks bare `codex exec`, so the rule survives the bash runners' removal. Milestone 4. |
| 68 | Count only a runner in command position; `bash -n bin/subfleet-claude` or a heredoc mention is not a launch. | `bin/subfleet-hook:47-59` | UX-contract | keep | n/a | C-23.48 | unit | subfleet/hooks.py | test_session_hook_ignores_runner_mention_outside_command_position | Rationale: false blocks on inspection commands. Milestone 4; no clause states the PreToolUse hook's matching rules. |
| 69 | Let the runners' own `-d` pass, and accept `SUBFLEET_ATTACHED_OK=1` as the explicit one-off override. | `bin/subfleet-hook:46,61-67` | UX-contract | keep | n/a | C-23.48 | unit | subfleet/hooks.py | test_session_hook_allows_runner_d_flag_and_attached_ok_override | Rationale: detached launches already survive. Milestone 4; C-17.6 makes `run` detached by default inside a Claude session. |
| 70 | Make hook install idempotent, preserve other settings, and write a timestamped backup before any change. | `subfleet/hooks.py:16-19,92-102,132-163` | ops-hygiene | keep | n/a | C-23.25 | unit | subfleet/hooks.py | test_hook_install_is_idempotent_and_backs_up_user_settings | Rationale: `settings.json` is shared with the user. Milestone 4; C-2.1 does not yet except writes to `~/.claude/settings.json`. |
| 71 | Resolve the notice recipient by session id at finish time, never by the pid or socket captured at dispatch. | `subfleet/notify.py:21-26,138-172` | session-continuity | keep | n/a | C-15.1 | unit | subfleet/notices.py | test_notice_recipient_resolved_by_session_id_at_finish_time | Verified live 2026-08-23: dispatched from pid 78753, delivered to pid 76529. C-15.2 owns the transport resolution that follows. |
| 72 | Rank duplicate session-registry rows by live pid, then present socket, then newest start. | `subfleet/notify.py:143-172` | session-continuity | keep | n/a | C-23.30 | unit | subfleet/notices.py | test_duplicate_registry_rows_ranked_by_live_pid_then_socket_then_start | Rationale: restarts leave stale `<pid>.json` rows. Milestone 4; C-15.2 names delivery layers but not registry tie-breaking. |
| 73 | Put only metadata in a completion notice: paths, rc, and a first line, never the prompt or the output body. | `subfleet/notify.py:575-577,590-610` | safety-guard | keep | n/a | C-15.1 | unit | subfleet/notices.py | test_notice_body_carries_metadata_not_prompt_or_deliverable_text | Rationale: private prompts must stay on disk. C-15.1 enumerates the notice text exactly: ids, class, rc, paths, one summary line, uncertainty. |
| 74 | Declare the recipient's own permission class on the notice envelope, with `SUBFLEET_NOTIFY_MODE` as the override. | `subfleet/notify.py:28-33,341-352` | provenance/attestation | keep | n/a | C-23.42 | unit | subfleet/notices.py | test_notice_envelope_declares_recipient_permission_class | Rationale: inbox attestation contract. Milestone 4; C-15.1 lists notice text but no envelope or permission-class declaration. |
| 75 | Emit exactly one envelope per notice and neutralize a closing tag appearing inside the body. | `subfleet/notify.py:298-309` | UX-contract | keep | n/a | C-23.49 | unit | subfleet/notices.py | test_notice_emits_single_envelope_and_neutralizes_closing_tag | Rationale: the recipient parses only single-envelope messages. Milestone 4; the contract defines notice rows and states, not the wire envelope. |
| 76 | Refuse a lane session as a notify target and hide lane sessions from `subfleet sessions` unless forced. | `subfleet/notify.py:175-210,373-378`, `subfleet/lanes.py:1-10`; test `tests/test_notify.py:381` | session-continuity | keep | n/a | C-23.31 | unit | subfleet/notices.py | test_lane_session_refused_as_notice_target | 2026-09-04: the Sol to Astra routing broadcast turned two finished lanes' outputs into "Acknowledged". The session-listing half is milestone 6 and uncovered. |
| 77 | Park a notice when its session is not live and surface it at the next SessionStart or prompt. | `subfleet/notify.py:459-473`, `subfleet/cli.py:885-890` | session-continuity | keep | n/a | C-15.2 | fake | subfleet/notices.py | test_notice_parked_for_dead_session_surfaces_at_next_session_start | Rationale: detached runs outlive their sessions. C-15.3 supplies the `pending`/`surfaced` states; surfacing point is `subfleet/hooks.py`, milestone 4. |
| 78 | Serialize notice writes and keep exactly one notice row per run id. | `subfleet/notify.py:411-465` | ops-hygiene | keep | n/a | C-15.1 | unit | subfleet/notices.py | test_one_notice_row_per_job_under_concurrent_finishers | Rationale: concurrent finishers. Mechanism moves from file locks to the SQLite terminal transaction (C-3.2); the one-row rule is unchanged. |
| 79 | Skip the push while an inline `--attach` waiter is still alive, and fall through if that waiter died. | `subfleet/notify.py:622-631` | UX-contract | keep | n/a | C-23.50 | fake | subfleet/notices.py | test_push_skipped_while_attached_waiter_alive_and_resumes_when_it_dies | Rationale: duplicate reports; G-review flags this row. C-15.2 orders `wait` above the best-effort push; C-15.3 makes a repeat offer harmless. |
| 80 | Prune surfaced notices older than 14 days. | `subfleet/notify.py:494-520` | ops-hygiene | keep | n/a | C-23.26 | unit | subfleet/retention.py | test_surfaced_notices_pruned_after_fourteen_days | Rationale: file growth. C-8.4 covers job retention and protects unread notices only; the notice age prune is milestone 4, run by C-18.1's hourly pass. |
| 81 | Never let notify or ledger accounting failures fail a run. | `subfleet/cli.py:660-676` | ops-hygiene | replace | The notice row is written in the same transaction as the terminal job state and must succeed; delivery is layered and best effort, and a failed delivery leaves the notice `pending` or `offered` without changing the job's state or rc. | C-15.1 | fake | subfleet/notices.py | test_notice_delivery_failure_never_changes_job_outcome | Monitoring sidecar rule. Verdict change: v2 splits the rule — C-15.1 commits the notice row inside the terminal transaction, where it must succeed, and only delivery (C-15.2) stays best effort. Compare rows 35 and 129, which stay `keep`. |
| 82 | Cap the ledger at the newest 500 entries and 2 GiB, and never delete an entry that is still running. | `subfleet/run_ledger.py:21-22,589-617` | ops-hygiene | keep | n/a | C-8.4 | unit | subfleet/retention.py | test_retention_caps_500_jobs_and_2gib_and_spares_active | Disk growth versus live dispatches; `C-8.4` widens "still running" to active, quarantined, unread-notice, salvage-ref and gate-evidence jobs. |
| 83 | Create run directories 0700 and artifacts 0600. | `subfleet/run_ledger.py:54-67,84-125` | safety-guard | keep | n/a | C-2.3 | process | subfleet/daemon.py | test_job_dirs_are_0700_and_artifacts_0600 | Run dirs hold private prompts and outputs; the guardian must create attempt files and receipts under the same modes (`C-5.2`). G-review flags this row. |
| 84 | Treat `prompt.md` as an immutable start snapshot and never re-copy it at finish. | `subfleet/run_ledger.py:522-525` | provenance/attestation | keep | n/a | C-2.3 | fake | subfleet/daemon.py | test_prompt_md_written_once_and_never_rewritten_at_finish | A caller editing the source path would rewrite history; `C-6.7` fixes the bytes as sent, including any prepended preamble. G-review flags this row. |
| 85 | Make finish idempotent so a finished entry is never re-finalized. | `subfleet/run_ledger.py:518-519` | ops-hygiene | keep | n/a | C-4.2 | fake | subfleet/daemon.py | test_finalization_is_idempotent_for_a_finished_attempt | Double finishers; `C-4.2` requires every finalization step to check for its own completed output, and `C-4.3` accepts an attempt only once. |
| 86 | Refuse an `-o` path a live run is still writing and name the last writer. | `subfleet/delegate.py:286-315`, `subfleet/run_ledger.py:647-679`; test `tests/test_run_ledger.py:380` | work-salvage | keep | n/a | C-6.5 | fake | subfleet/daemon.py | test_output_lease_refuses_a_live_collision_and_names_the_holder | 2026-09-04 doc_076: a killed misroute and its pinned replacement shared a path (commit 24e81bf). C-6.5 states the refusal and the fix; v1's `--reuse-out` override is replaced by request-id idempotency (C-6.2), which C-17.2 does not list. |
| 87 | Mark a run orphaned when its pid is gone with no finish record, reap it as rc=-9, and leave pid-less entries alone. | `subfleet/run_ledger.py:726-728,841-867` | ops-hygiene | replace | An attempt whose guardian is gone with no `exit.json` is resolved by containment enumeration and finalized `lost` with no rc — never `succeeded` and never a synthetic rc; a `running` attempt always carries pid, boot id and proc start from `start.json`, and an attempt that never launched is recovered at its own boundary (`reserved-no-launch`) rather than reaped. | C-4.4 | process | subfleet/daemon.py | test_attempt_without_exit_receipt_finalizes_lost_without_rc | verdict change: `C-4.4` records `lost` with no rc instead of rc=-9, and `C-4.2` plus `C-5.2` give every attempt a recorded identity, so pid-less rows cannot occur. |
| 88 | Require a dead pid to persist for a grace period (30 s waiting, 60 s reaping) before calling a run orphaned. | `subfleet/run_ledger.py:796-838,841-848` | ops-hygiene | replace | The guardian writes `exit.json` by temp and rename before it exits, so a missing receipt with a dead guardian is decided by containment rather than by waiting; the only grace left is `start_grace_s` (10 s) at the `starting` boundary before containment runs. | C-4.2 | process | subfleet/daemon.py | test_start_grace_precedes_containment_at_the_starting_boundary | verdict change: the exit-to-receipt race is closed by receipt ordering (`C-5.2`), so v1's 30 s and 60 s graces become one 10 s `start_grace_s`. G-review flags this row. |
| 89 | Never let an in-flight run age out of `subfleet runs`; `last` bounds only finished rows. | `subfleet/run_ledger.py:761-788`; test `tests/test_run_ledger.py:450` | UX-contract | keep | n/a | C-23.51 | unit | subfleet/cli.py | test_running_jobs_never_age_out_of_last_n_listing | 2026-09-05: a 7 h lane fell below the newest-20 window and a poller lost it. `C-17.1` names `--last N` but no clause states its semantics; milestone 1. G-review flags this row. |
| 90 | Keep the rc token stable (RUNNING / ORPHANED / integer) and report transcript quiet only as a trailing note, never as a STALE verdict. | `subfleet/run_ledger.py:698-739,1007-1018` | UX-contract | keep | n/a | C-4.1 | unit | subfleet/cli.py | test_quiet_transcript_reports_a_note_not_a_stale_status | 2026-09-05 t1-R: 40 idle transcript minutes on a healthy lane (commits b0222a2, then c465456 and 57a851a). The token vocabulary changes — C-4.1's closed state set has no `ORPHANED` — but the obligation holds: no STALE verdict, and C-9.2 keeps the raw rc beside the class. |
| 91 | Signal the whole process group only when the pid leads its own group, never a group the caller belongs to. | `subfleet/run_ledger.py:927-938` | process-survival | keep | n/a | C-5.4 | process | subfleet/procs.py | test_signals_only_a_recorded_group_whose_leader_identity_matches | 2026-08-26: kill signalled the wrapper, not the tree, and a cross-session `pgrep &#124; kill -9` killed unrelated runs (2026-09-04); `C-5.1` setsid makes the guardian its own leader. |
| 92 | After SIGTERM wait the grace, escalate to SIGKILL, then report killed, escalated, or survived. | `subfleet/run_ledger.py:870-911`, `subfleet/cli.py:1320-1323`; test `tests/test_wait_kill.py:128` | process-survival | keep | n/a | C-5.6 | process | subfleet/procs.py | test_kill_escalates_sigterm_grace_then_sigkill_and_reports | 2026-09-04: kill printed "signalled" while the lane kept running; the constant becomes `term_grace_s` 15 s and v1's "survived" becomes `quarantined` (`C-5.7`). |
| 93 | Do not count a zombie as running. | `subfleet/run_ledger.py:914-924` | ops-hygiene | keep | n/a | C-5.5 | process | subfleet/procs.py | test_zombie_pids_are_not_counted_as_live | Unreaped children still answer `kill(pid, 0)`; `C-5.5` counts only live, non-zombie pids and reads `stat=` from `ps` for exactly this. G-review flags this row. |
| 94 | Take the Codex thread id from the first anchored `session id:` header in stderr with a bounded scan. | `subfleet/run_ledger.py:180-206`; test `tests/test_run_ledger.py:288` | provenance/attestation | replace | Read the Codex thread id from the `thread.started` event of the `codex exec --json` stream, never from stderr text that may quote another run's Codex log. | C-12.3 | unit | subfleet/adapters/codex.py | test_codex_thread_id_read_from_thread_started_event | verdict change: `C-12.3` makes the anchored stderr header and its bounded scan nonexistent; the anti-spoofing principle survives against a fixture that quotes another log. G-review flags this row. |
| 95 | Resolve historical Codex resume identity lazily from the saved `err.log` and rollout name, never rewriting old records. | `subfleet/run_ledger.py:949-980` | session-continuity | keep | n/a | C-23.32 | unit | subfleet/importer.py | test_legacy_codex_resume_identity_resolved_without_rewriting_record | Older entries lacked the fields; v2 records the thread id at launch, so only imported v1 rows need lazy in-memory resolution — cutover milestone, no clause. G-review flags this row. |
| 96 | Count in-flight runs from the ledger, never from `pgrep`. | `subfleet/run_ledger.py:983-995` | capacity-truth | keep | n/a | C-6.3 | fake | subfleet/daemon.py | test_in_flight_counted_from_leases_never_from_process_scan | 2026-09-04: a cross-session `pgrep -f gpt-6-astra &#124; kill -9`; the lane slot lease is the counter, `C-6.4` sets the caps and `C-16.4` forbids a handler blocking on `ps`. |
| 97 | Validate a run id as a single path component. | `subfleet/run_ledger.py:416-422` | safety-guard | keep | n/a | C-1.1 | unit | subfleet/ids.py | test_job_id_rejected_when_not_a_single_path_component | Path traversal into the state dir; `C-1.1` restricts the slug to `[a-z0-9-]` at most 40 characters, which forces a single component. G-review flags this row. |
| 98 | Detach by default inside a Claude session and return the run id immediately. | `subfleet/delegate.py:714-738,1114-1123` | process-survival | keep | n/a | C-17.6 | fake | subfleet/cli.py | test_run_defaults_to_detached_inside_a_claude_session | 2026-08-23 triple death; `C-17.6` also requires the four-line hint (out path, log path, wait command, status command) and v1's session detection. |
| 99 | Pre-create the ledger entry before the runner exists so the id is known up front. | `subfleet/delegate.py:1088-1113` | UX-contract | keep | n/a | C-6.3 | fake | subfleet/daemon.py | test_job_row_and_id_exist_before_the_guardian_launches | The caller must be able to name the run; `C-6.3` commits the `reserved` attempt before launch and `C-16.3` returns the job id on submit. |
| 100 | Add `-A` only when the lane was auto-picked, so pins stay pins and auto runs re-pick inside the runner. | `subfleet/delegate.py:1180-1181,1231-1232` | routing-policy | keep | n/a | C-11.2 | unit | subfleet/policy.py | test_pinned_lane_never_falls_back_while_auto_job_repicks | Identity pins are for attestation; the bash runner's `-A` re-pick becomes the daemon's next attempt (`C-4.5`), and `C-4.6` forbids re-picking inside one attempt. |
| 101 | Route only upward on exhaustion, and treat unknown telemetry as no evidence of exhaustion. | `subfleet/delegate.py:684-695,97-102` | routing-policy | keep | n/a | C-11.2 | unit | subfleet/policy.py | test_chain_walk_never_moves_down_and_unknown_is_not_exhaustion | Secondary C-11.3: lanes with no `provider` reading are "eligible but unmeasured", so an unmeasured lane is never treated as exhausted or skipped in the chain. |
| 102 | Fail fast with rc 3 and the earliest scoped reset when the quality floor has no lane, and never downgrade cross-family. | `subfleet/delegate.py:1020-1052` | routing-policy | keep | n/a | C-17.3 | fake | subfleet/cli.py | test_quality_floor_task_exits_three_naming_earliest_reset | Secondary C-11.2: v2 has no `fable` task class, so the floor is a single-model chain the upward walk can never leave. Exit 3 must name the earliest reset. |
| 103 | Overflow a sweep to Opus only when the Codex fleet is confirmed all-limited. | `subfleet/delegate.py:890-916` | routing-policy | keep | n/a | C-11.2 | unit | subfleet/policy.py | test_cross_provider_promotion_requires_every_lane_closed | The chain walk already promotes only on an empty candidate set; `--overflow` survives merely as a deprecated flag with a stderr note (C-17.2). |
| 104 | Never fall back from an exact `-m` pin. | `subfleet/delegate.py:853-858` | routing-policy | keep | n/a | C-11.2 | unit | subfleet/policy.py | test_model_pin_never_falls_back_to_another_model | C-11.2 states it verbatim: a `-m` pin evaluates one model and never falls back; a hard limit on a pinned lane is exit 4 (C-17.3). |
| 105 | Probe a blind lane live before picking it, and allow a still-blind lane only when it carries no other run. | `subfleet/delegate.py:423-496` | capacity-truth | keep | n/a | C-11.4 | fake | subfleet/daemon.py | test_unmeasured_lane_is_probed_and_capped_to_one_in_flight | 2026-09-03: five Opus lanes on unseeable accounts died mid-run. C-11.4 narrows the probe to writable or `hard` work (other jobs' first attempt is the probe); C-6.4 caps an unmeasured lane at one in flight. 2026-09-27: C-6.4 caps an unmeasured lane only when a policy sets `max_in_flight_unmeasured` (Max: "uncap everything and instead use prioritization"); the probe before writable or `hard` work stands, and a blind lane ranks after measured ones within its load band (C-11.3). |
| 106 | Cool a lane measured below the headroom floor to its own reset instead of dispatching to it. | `subfleet/delegate.py:481-487` | capacity-truth | keep | n/a | C-11.3 | unit | subfleet/policy.py | test_lane_below_headroom_floor_is_closed_until_its_reset | Secondary C-11.4: only a `limited` probe result closes the scope (C-9.4, C-9.6); a merely sub-floor reading fails C-11.3's eligibility test instead. Floor is `headroom_floor` 0.15. |
| 107 | Rank the desktop login last among Claude lanes, behind every other dispatchable lane including blind ones. | `subfleet/delegate.py:568-584`, `subfleet/capacity.py:1315-1322` | routing-policy | replace | The desktop-login lane is filtered out of the candidate set while Claude Code uses that login (C-10.3's registry signal) unless the job carries `--allow-desktop`; otherwise it is a candidate ranked after every other lane. | C-10.3 | unit | subfleet/policy.py | test_desktop_login_lane_is_never_a_candidate_without_allow_desktop | verdict change: the 2026-09-04 standing order says the handicap was not enough; C-10.3 excludes the lane outright, so v1's ranking term has no v2 counterpart. See C-11.6. 2026-09-27, Max: excluding it "makes sense if we're using sf thru the cc app but not if thru the sf app"; C-10.3 now refuses the lane only while Claude Code uses the login, and otherwise ranks it last, as v1 did. |
| 108 | Spend Fable-exhausted lanes first for non-Fable work. | `subfleet/delegate.py:553-566` | routing-policy | keep | n/a | C-23.37 | unit | subfleet/policy.py | test_model_stranded_lane_is_preferred_for_lower_model_work | 2026-08-26: stranded capacity versus shared windows. Milestone 3 gap: C-11.3's Claude comparator has no model-stranded term, and headroom ordering would rank such a lane last. |
| 109 | Accept the retired `sol` alias everywhere but dispatch Astra and say so on stderr. | `subfleet/delegate.py:59-72,155-165`, `subfleet/consensus.py:1247-1248`, `subfleet/handoff.py:618-626` | routing-policy | keep | n/a | C-17.2 | unit | subfleet/cli.py | test_retired_sol_alias_dispatches_astra_with_stderr_note | 2026-09-04: Opus for standard, Astra for hard. Secondary C-11.1's `retired` map covers gates and handoff; C-17.2 accepts the alias only through milestone 8. |
| 110 | Dispatch frontier Codex models at ultra reasoning effort. | `subfleet/delegate.py:55-58,1182` | routing-policy | keep | n/a | C-11.1 | unit | subfleet/default_policy.json | test_frontier_codex_model_launches_at_ultra_reasoning_effort | Effort is policy data (`models`: short name to id and optional effort); C-12.3 puts `-c model_reasoning_effort=ultra` on the argv. Codex's own default for the frontier model is low. |
| 111 | Default `build` to workspace-write and every other semantic task to read-only. | `subfleet/delegate.py:839` | safety-guard | keep | n/a | C-11.1 | unit | subfleet/default_policy.json | test_only_build_task_defaults_to_workspace_write_sandbox | 2026-09-02: review lanes must not write `-o` inside `-C`. C-6.1 now allows that case explicitly because the daemon writes the export, not the child. |
| 112 | Reject `-a` with a Codex model and `-H` with a Claude model, and let a `-H` pin with no `-m` imply the frontier Codex model. | `subfleet/delegate.py:821-823,831-834` | routing-policy | keep | n/a | C-6.1 | unit | subfleet/cli.py | test_lane_and_home_pins_reject_cross_provider_models | Secondary C-17.3: an inconsistent pin is exit 2. C-6.1 says only that pins are consistent; the `-H` implies frontier model rule needs a named default in `policy.json`. |
| 113 | Journal every routing decision with its normalized capacity inputs and scores. | `subfleet/delegate.py:968-1000` | provenance/attestation | keep | n/a | C-11.5 | unit | subfleet/policy.py | test_decision_record_journals_readings_closures_and_rejections | C-11.5 replaces v1's family scores with per-lane rejection reasons. It stores the record per attempt; a refusal that dispatches nothing still needs a place to keep it. |
| 114 | Transfer ownership of the merged temporary prompt to the detached runner. | `subfleet/delegate.py:1129-1131` | ops-hygiene | replace | The daemon writes `prompt.md` once into `jobs/<job id>/` as the exact bytes sent and nothing deletes it before retention; there is no temporary prompt and no ownership handover. | C-2.3 | fake | subfleet/daemon.py | test_prompt_md_is_written_once_and_outlives_the_submitter | Double deletion or a missing prompt. Verdict change: plan B drops the bash runners, so no temp prompt exists to hand over; C-2.3 makes `prompt.md` write-once instead. Secondary C-6.7: `prompt.md` is the prompt as sent. |
| 115 | On a hard limit, preserve the runner's precise reset and use the conservative fallback only when no future cooldown was recorded. | `subfleet/delegate.py:1256-1274` | capacity-truth | keep | n/a | C-9.4 | unit | subfleet/adapters/base.py | test_reported_reset_survives_the_guessed_limited_clock | Secondary C-9.6: a later closure extends but never shortens, so the guessed now plus 3600 s clock can never displace a live provider reset. |
| 116 | On an auth-dead result, cool the account and print the exact re-enrolment ritual. | `subfleet/delegate.py:1275-1277,106-107` | identity | replace | An `auth-dead` lane is disabled until `subfleet lanes enroll` rebinds the credential, instead of a 30-day cooldown; the CLI exits 5 and names that command as the fix. | C-23.44 | fake | subfleet/credentials.py | test_auth_dead_lane_is_disabled_until_reenrolment | Ledger verdict is keep-simplified. Row 23 makes rc 5 probe-confirmed, so 30 days is severe. Secondary C-17.3 exit 5; C-17.1 `lanes enroll` is the ritual. |
| 117 | Cap lane attempts at 3 before promoting or reporting exhaustion. | `subfleet/delegate.py:1211,1289-1301` | routing-policy | keep | n/a | C-4.5 | fake | subfleet/daemon.py | test_attempts_stop_at_max_attempts_before_promoting | The value is `max_attempts` 3 in `policy.json` caps (C-6.4); C-4.6 makes each new lane a new attempt, so the job cap is what bounds lane rotation. |
| 118 | Treat server responses as ground truth and render unknown as unknown, never fabricated. | `subfleet/capacity.py:1-7`, `subfleet/claude.py:118-123`, `README.md:55-58` | capacity-truth | keep | n/a | C-9.1 | fake | subfleet/cli.py | test_status_shows_unknown_when_no_provider_reading_exists | 2026-07-11: the app gauge lied for the whole incident. Plan B drops the learned estimator, so no source can fabricate a percentage; `status.json` (C-18.1) obeys the same rule. |
| 119 | Sanitize before caching: strip `_`-prefixed keys, `raw`, and any token fields. | `subfleet/capacity.py:307-318`; test `tests/test_codex.py:103` | safety-guard | keep | n/a | C-10.1 | unit | subfleet/store.py | test_reading_write_strips_raw_and_token_fields | G-review flags this row. Secondary C-10.5: the credential never reaches a log or `manifest.json`; C-12.7 demands the same redaction of the fixture corpus. |
| 120 | Record only whether an enrolled keychain service exists, never its value, and mark a missing secret `secret-missing` and undispatchable. | `subfleet/capacity.py:905-912,1183-1184,1260-1269` | identity | keep | n/a | C-10.1 | unit | subfleet/credentials.py | test_missing_keychain_item_marks_lane_undispatchable | G-review flags this row. C-10.1 owns storing a reference not a value; the undispatchable half needs C-11.2's filter. Real keychain reads are a `doctor --live` check. |
| 121 | Treat a live probe reading as fresh for 120 s and invalidate it when the enrolled roster changes. | `subfleet/capacity.py:25,915-940` | capacity-truth | keep | n/a | C-9.1 | unit | subfleet/store.py | test_provider_reading_goes_stale_after_reading_ttl | Stale roster after enrollment; the 120 s TTL survives as `reading_ttl_s` (C-6.4) and roster-fingerprint invalidation is subsumed by per-lane readings rows, a rebound lane getting a new id (C-1.3). |
| 122 | Merge ledger windows and cooldowns on every read so a completed run and a fresh hard limit are visible immediately. | `subfleet/capacity.py:1398-1429` | capacity-truth | keep | n/a | C-11.2 | fake | subfleet/policy.py | test_hard_limit_visible_to_the_next_routing_decision | Cache must never hide a fresh limit; closures are read inside the admission transaction (C-6.3) and listed in the decision record (C-11.5). Milestone 2 acceptance already runs this against fakes. |
| 123 | Extend a cooldown's expiry atomically and never shorten it. | `subfleet/capacity.py:577-594`; test `tests/test_capacity.py:180` | capacity-truth | keep | n/a | C-9.6 | unit | subfleet/store.py | test_closure_extends_until_and_never_shortens | Concurrent writers; the v1 file lock becomes one SQLite transaction with its events row (C-3.2). G-review lists row 123 as needing explicit treatment. |
| 124 | Normalize retired model pins onto the current model id for cooldown scopes, hard-limit records, picks, and revives, while a `[1m]` suffix survives on dispatch ids. | `subfleet/capacity.py:74-110,195-213,545-569`; test `tests/test_capacity.py:970` | capacity-truth | keep | n/a | C-11.1 | unit | subfleet/policy.py | test_retired_model_alias_normalized_before_closure_scope | 2026-09-02: `fable` pinned Fable 5 while the app ran 5.1. The `retired` map is policy data; the context suffix now lives in the provider model id (C-1.6) and closure scope uses that id (C-9.4). |
| 125 | Calibrate learned capacity only from account-scope hard limits, never from model-bucket limits. | `subfleet/capacity.py:466-486` | capacity-truth | replace | Record every limit with its scope (`account` or a model id) and never derive capacity from it; a model-scoped closure excludes only that model from the lane's candidates. | C-11.2 | unit | subfleet/policy.py | test_model_scoped_closure_leaves_other_models_eligible | Mixed-model totals made healthy models look exhausted; plan B drops the learned-capacity estimator (produced 4974%) and the scope is recorded on the closure (C-9.4). |
| 126 | Label a lane window `estimated` unless a learned capacity exists (`observed`) or a live reading is present (`live`). | `subfleet/capacity.py:1134-1152` | capacity-truth | replace | Label every reading from the closed set `provider`, `stale-provider`, `admission-observed`, `local-backoff`, `unknown`, and render a percentage only from a `provider` or `stale-provider` reading, marking a stale one stale. | C-9.1 | unit | subfleet/cli.py | test_no_percentage_rendered_without_a_provider_reading | Setup tokens get 403 from the usage endpoint, so they now render `unknown` rather than an estimate; `status.json` shares the rule (C-18.1). Milestone 2 acceptance. |
| 127 | Treat a keepalive marker as an exact observed five-hour reset until that window closes, then stop. | `subfleet/capacity.py:443-463,1147-1152`; test `tests/test_keepalive.py:441` | capacity-truth | replace | Record a keepalive as `admission-observed` evidence with remaining quota unknown; a reset clock or a percentage comes only from a `provider` reading. | C-18.1 | fake | subfleet/keepalive.py | test_keepalive_writes_admission_observed_without_reset_clock | It was the only known reset timestamp for a lane; plan B appendix replaces rows 125 to 127 with `provider` readings. `admission-observed` means quota unknown (C-9.1). Milestone 5. |
| 128 | Count each `message.id` once when summing transcript usage. | `subfleet/capacity.py:357-405` | capacity-truth | keep | n/a | C-23.15 | unit | subfleet/adapters/claude.py | test_transcript_usage_counts_each_message_id_once | Transcripts repeat updated assistant messages. GAP: C-6.4's `max_tokens_observed` implies token observation but no clause states message-id dedup. G-review flags row 128; milestone 2. |
| 129 | Never let usage accounting fail a run; record a parse failure as an error record instead. | `subfleet/capacity.py:627-675`; test `tests/test_capacity.py:125` | ops-hygiene | keep | n/a | C-23.22 | fake | subfleet/daemon.py | test_reading_parse_failure_records_event_and_job_still_succeeds | Monitoring sidecar rule. GAP: nearest is C-8.4 (probe results live in `readings` and `events`, not `jobs`) and C-8.3's export analogue, but no clause contains an accounting failure. Milestone 2. |
| 130 | Fall back to a one-hour limit when a hard limit carries no reset, never to immediate re-dispatch or a fabricated week. | `subfleet/capacity.py:647-655,28` | capacity-truth | keep | n/a | C-9.4 | unit | subfleet/adapters/base.py | test_limit_without_reset_clock_guesses_one_hour | A just-limited lane must not look free; C-9.4 fixes the guess at now + 3600 s marked `clock_source: guessed`. Shared by both adapters; fixture case 'hard limit without clock' (C-12.7). G-review flags row 130. |
| 131 | Classify quota windows by duration, never by their position in the payload. | `subfleet/codex.py:231-249`, `subfleet/capacity.py:735-744` | capacity-truth | keep | n/a | C-9.7 | unit | subfleet/adapters/codex.py | test_codex_window_keyed_by_duration_not_payload_position | 2026-07-11 primary was 5h; by 2026-07-25 primary was weekly with no 5h reported. Claude keys by `unifiedWindows` name instead (C-9.8). Fixture premise for C-20.1's wham probe: that Codex reports `window_minutes` 300 and 10080. |
| 132 | Require the later reset before a lane with both windows exhausted returns. | `subfleet/capacity.py:976-988`, `subfleet/claude.py:297-306` | capacity-truth | keep | n/a | C-11.3 | unit | subfleet/policy.py | test_lane_returns_only_after_the_later_of_two_exhausted_windows | One window resetting is not availability; C-11.3 requires every window under the floor, and closures extend but never shorten (C-9.6). G-review flags row 132. |
| 133 | Order Codex dispatch strictly by weekly reset ascending and keep in-flight counts and app-shadow flags display only. | `subfleet/capacity.py:1325-1335`, `subfleet/snapshot.py:409-420`; test `tests/test_pick.py:107` | routing-policy | keep | n/a | C-11.3 | unit | subfleet/policy.py | test_codex_lanes_ordered_by_seven_day_reset_ascending | 2026-08-22: drain the soonest-expiring window. Plan B keeps this as the Codex primary ordering key; in-flight never reorders Codex, but C-10.3 (row 138) overturns the display-only half of the shadow flag. Milestone 3. 2026-09-27: C-11.3's load band comes first, so in-flight counts reorder Codex lanes across bands only; within a band the weekly reset orders them as before. |
| 134 | Treat a just-past weekly reset clock as due now rather than demoting the lane. | `subfleet/capacity.py:694-702` | capacity-truth | keep | n/a | C-11.3 | unit | subfleet/policy.py | test_past_weekly_reset_clock_ranks_as_due_now | WHAM propagation lag; a past clock sorts first under C-11.3's soonest-first order and its closure has already expired by clock (C-9.6). |
| 135 | Exclude free-plan accounts from dispatch with an explicit verdict. | `subfleet/codex.py:200-203`, `subfleet/snapshot.py:81-88`; test `tests/test_app_home.py:176` | capacity-truth | keep | n/a | C-10.2 | unit | subfleet/adapters/codex.py | test_codex_free_plan_home_refused_at_enrolment | 2026-08-19: a lane logged in before its Pro upgrade ranked best at 0% of a 30-day window. The plan comes from the home's `auth.json` claims, so unit proves it; the endpoint half of C-10.2 is live. |
| 136 | Detect the same account bound in two homes, mark the non-canonical duplicate, and alert critical. | `subfleet/snapshot.py:166-179`, `subfleet/capacity.py:718-728`, `subfleet/watchdog.py:383-396` | identity | keep | n/a | C-23.45 | unit | subfleet/credentials.py | test_same_account_in_two_homes_marks_duplicate_lane | 2026-07-11: revoked refresh token from same-account-in-two-homes. GAP: C-1.4 defines the account key and C-10.1 the binding, but no clause states duplicate detection; alerting is C-18.1, milestone 5. |
| 137 | Never dispatch to `~/.codex`; observe it only for identity. | `subfleet/paths.py:15-37` | identity | keep | n/a | C-10.3 | unit | subfleet/adapters/codex.py | test_app_codex_home_observed_but_never_enrolled_as_lane | Since 2026-08-19 the app rewrites `~/.codex` on every sign-in and out; C-10.3 states the rule verbatim, so enrolment refuses it while the probe still reads its identity. |
| 138 | Treat app shadowing as metadata that does not change dispatch order but excludes a lane from automatic reset while an unshadowed candidate exists. | `subfleet/snapshot.py:181-200`, `subfleet/reset_policy.py:168-174` | identity | keep | n/a | C-23.46 | unit | subfleet/actions.py | test_reset_redemption_prefers_an_unshadowed_lane_over_a_shadowed_one | Observed 2026-08-04, 08-13, 08-17. GAP, milestone 5: C-10.3's `desktop` flag is the Claude login, not the Codex app shadow this row governs, and no clause states the reset-redemption preference. Compare rows 137 and 163. |
| 139 | Never write any auth store and never refresh a token in-process. | `subfleet/codex.py:14-18,59-83` | identity | keep | n/a | C-23.47 | unit | subfleet/credentials.py | test_enrolment_probe_never_writes_the_provider_auth_store | An unpersisted refresh rotation is the revocation trap. GAP: C-2.1 already forbids writing outside the state root, but no clause confines token refresh to the provider CLI; milestone 2. |
| 140 | Scan Codex rollouts incrementally with a per-file size and mtime cache, snapshotting sizes before grep. | `subfleet/codex.py:569-575,649-690` | ops-hygiene | replace | Take limit and revocation evidence only from the attempt's own directory, stream, and stderr, never from a sweep of a home's rollout tree; the rollout is located by the `thread.started` thread id for attestation alone. | C-12.1 | unit | subfleet/adapters/codex.py | test_codex_classifier_reads_only_the_attempt_directory | 2026-09-02: 72,799 stat calls per dispatch hung `subfleet run`. Verdict change: v2 has no rollout sweep, `classify(attempt_dir, exit_info)` is scoped to one attempt and the thread id comes from C-12.3. |
| 141 | Prune a Codex scan-cache entry only when its file is gone or is a week stale, never because it falls outside the current call's window. | `subfleet/codex.py:715-728` | ops-hygiene | keep | n/a | C-23.26 | unit | subfleet/adapters/codex.py | test_codex_scan_cache_prunes_only_missing_or_week_old_entries | GAP: no clause defines a Codex rollout scan cache; the rule applies wherever milestone 2 scans a home, and `~/.codex` is observed under C-10.3. |
| 142 | Gate a Claude lane on headroom in every probed window, the weekly window included. | `subfleet/claude.py:36-39,282-294` | capacity-truth | keep | n/a | C-11.3 | unit | subfleet/policy.py | test_claude_lane_ineligible_when_weekly_window_below_headroom_floor | C-11.3's worst-window eligibility covers the weekly window; the constant changes from v1's 5% `DEFAULT_MIN_HEADROOM` to `headroom_floor` 0.15. |
| 143 | Treat the statusline tap as terminal-TUI only, rank it by `updated_at`, and never treat it as current. | `subfleet/claude.py:594-599,4-11`; test `tests/test_snapshot_live.py:3` | capacity-truth | drop | n/a | n/a | n/a | n/a | n/a | Diagnosed 2026-08-12, last real capture 2026-07-22. Verdict change: plan B drops the statusline tap, so the rule governs a sensor v2 does not have; C-9.1's closed label set carries the principle and row 144 keeps the freshness ranking. |
| 144 | Headline the freshest usable reading and demote anything older than three hours to a labelled stale line. | `subfleet/claude.py:562-591` | capacity-truth | keep | n/a | C-9.1 | unit | subfleet/store.py | test_freshest_reading_wins_and_older_one_is_marked_stale | Constant changes: v1's 3 h `LIVE_STALE_AFTER_MIN` becomes `reading_ttl_s` 120 s (C-6.4); freshest-usable-then-mark-stale is unchanged. |
| 145 | Persist every 200 usage payload and re-parse it when the current probe fails. | `subfleet/claude.py:518-528` | capacity-truth | keep | n/a | C-9.1 | unit | subfleet/store.py | test_last_provider_payload_served_stale_when_probe_fails | Mechanism moves: the `claude-oauth-raw.json` sidecar becomes `readings` rows (C-8.4); the last `provider` reading is served as `stale-provider` when a probe fails. |
| 146 | Accept only live-confidence windows as desktop usage, and never let ledger estimates of lane traffic stand in for the login's usage. | `subfleet/claude.py:531-558`, `subfleet/capacity.py:1126-1129` | capacity-truth | keep | n/a | C-9.1 | unit | subfleet/cli.py | test_lane_token_estimates_never_render_as_the_login_percentage | Plan B drops the learned estimator, so only `provider` or `stale-provider` readings render a percentage; lane-token traffic is never the login's usage. |
| 147 | Label an activity-derived five-hour window `derived`, never as a server reading. | `subfleet/claude.py:778-799` | capacity-truth | replace | An activity-derived window is not a reading in v2: activity yields only an `admission-observed` label with no percentage, and window clocks come from the provider's `resetsAt` or a closure clock marked `clock_source: guessed`. | C-9.1 | unit | subfleet/adapters/claude.py | test_activity_derived_window_never_becomes_a_provider_reading | verdict change: C-9.1's closed label set has no `derived`; v1 was validated against the 2026-07-22 statusline observation. Guessed clocks follow C-9.4. |
| 148 | Read the Claude credential as one targeted keychain item, never as a keychain dump. | `subfleet/claude.py:384-390`, guard rule `bin/subfleet-guard-hook:283-289` | safety-guard | keep | n/a | C-10.1 | unit | subfleet/credentials.py | test_claude_credential_read_targets_one_keychain_item_never_a_dump | Core is the resolver's argv, so unit; the real keychain read is the live half. Secondary C-14.1: the copied hook blocks `dump-keychain -d`, which storms macOS prompts. |
| 149 | Never auto-login, and name the exact heal command in every alert. | `subfleet/watchdog.py:8,290-296,316-327` | UX-contract | keep | n/a | C-23.52 | unit | subfleet/alerts.py | test_auth_alert_names_the_login_command_and_never_logs_in | 2026-07-11 postmortem: logins are operator-only. GAP, milestone 5 alert bodies; C-17.3's exit-7 "names the rule and the fix" is the analogous CLI rule. |
| 150 | Allow exactly one automatic heal, a tiny `codex exec` turn that lets the CLI refresh and persist its own token, then re-probe. | `subfleet/watchdog.py:99-191`, `subfleet/codex.py:483-509` | identity | keep | n/a | C-23.47 | fake | subfleet/alerts.py | test_expired_codex_token_gets_exactly_one_refresh_turn_then_reprobe | 2026-08-12: `~/.codex-2` sat auth-suspect 14 h on a merely expired access token. GAP, milestone 5; the CLI persists `auth.json`, subfleet never writes it. |
| 151 | Latch `refresh token was revoked` until `auth.json` changes, and probe no further. | `subfleet/watchdog.py:131-138` | identity | keep | n/a | C-23.47 | unit | subfleet/alerts.py | test_refresh_token_revoked_latches_until_auth_json_changes | GAP, milestone 5 heal path; the latch key is `auth.json` `last_refresh`, the v2 credential epoch (C-10.1). C-9.3 sets the `auth-dead` evidence bar. |
| 152 | Attempt at most one refresh probe per home per cycle, spaced twenty minutes apart. | `subfleet/watchdog.py:32-35,139-141` | ops-hygiene | keep | n/a | C-23.27 | unit | subfleet/alerts.py | test_refresh_probe_spacing_limits_one_attempt_per_home_per_cycle | GAP, milestone 5: the heal probe is not the C-18.1 probe cycle, whose one-probe-per-idle-lane-per-window is the nearest interface rule. v1 spacing 20 min on a 30-min cycle. |
| 153 | Heal before persisting, so the snapshot, brief, history, and conditions all see post-heal verdicts. | `subfleet/watchdog.py:625-626` | ops-hygiene | keep | n/a | C-23.27 | fake | subfleet/timers.py | test_heal_runs_before_snapshot_and_condition_evaluation | GAP, milestone 5: heal precedes `status.json`, readings, and condition evaluation so no consumer reports mixed pre- and post-heal state. |
| 154 | Treat a cycle in which every Codex probe is a network error as offline and stay silent. | `subfleet/watchdog.py:274-281` | ops-hygiene | keep | n/a | C-23.27 | unit | subfleet/alerts.py | test_all_probes_network_error_yields_silent_offline_cycle | GAP, milestone 5. Ledger caveat: v1 lets non-silent scoped-limit conditions past the offline check, so the v2 test must assert a wholly silent cycle. |
| 155 | Alert on transition then at most every six hours, never re-alert `once` conditions, and warn at most daily on expiring capacity. | `subfleet/watchdog.py:31,477-488,707-720`; test `tests/test_app_home.py:126` | UX-contract | keep | n/a | C-18.1 | unit | subfleet/alerts.py | test_alert_repeats_on_transition_then_at_most_every_six_hours | C-18.1 states transition-then-6 h at interface level; `once` conditions and the daily expiring-capacity cap are milestone-5 detail beyond it. |
| 156 | Send a recovery notice only when no other condition for the same home is active. | `subfleet/watchdog.py:722-739` | UX-contract | keep | n/a | C-23.52 | unit | subfleet/alerts.py | test_recovery_notice_suppressed_while_another_condition_is_active | GAP, milestone 5: revoked to no-auth is a state change, not a recovery. v1's prefix list omitted conditions, so v2 must define recovery per condition. |
| 157 | Record only non-stale Claude readings in history. | `subfleet/watchdog.py:684-688` | capacity-truth | keep | n/a | C-9.1 | unit | subfleet/store.py | test_stale_claude_reading_is_not_recorded_as_a_history_point | A `stale-provider` reading is the same observation aged (C-9.1), so it is never inserted twice; C-8.4 keeps probe results in `readings` and `events`. |
| 158 | Resolve the codex binary explicitly so a stripped launchd PATH cannot break the call. | `subfleet/codex.py:454-480` | ops-hygiene | keep | n/a | C-23.21 | unit | subfleet/adapters/codex.py | test_codex_binary_resolved_absolutely_under_stripped_launchd_path | 2026-08-13: `.codex-3` latched failed for a week because launchd's PATH lacks `~/bin`; resolution order is `$SUBFLEET_CODEX_BIN`, PATH, then known install paths. |
| 159 | Judge mirror health from its per-pass state sidecar, tolerate a long in-flight pass, and cut off at thirty minutes. | `subfleet/claude.py:154-162,223-260`, `subfleet/paths.py:79-84`; test `tests/test_session_mirror.py:46` | ops-hygiene | keep | n/a | C-23.28 | unit | subfleet/sessions/mirror.py | test_mirror_heartbeat_reads_per_pass_sidecar_with_thirty_minute_cutoff | 2026-08-19 07:08: a quiet log produced a false "stalled"; an 8.5-minute pass was observed 2026-08-18. GAP, milestone 6. 2026-09-24: health is not reach. The app reads a session folder only when it loads it, so C-23.28 also requires the 2 s hot pass and the load-gap report (tests/unit/test_sessions_mirror_load_gap.py). 2026-09-25: the flag protocol is specified (docs/formal/MirrorFlags.tla), model-checked over every reachable state (tests/unit/test_mirror_flags_model.py), and the mirror is held to the model on random interleavings (tests/unit/test_mirror_flags_stateful.py). |
| 160 | Redeem at most one reset credit per evaluation, under a lock, and respect the minimum interval. | `subfleet/reset_policy.py:193-245,269-279,548-560` | capacity-truth | keep | n/a | C-18.1 | unit | subfleet/actions.py | test_at_most_one_reset_credit_redeemed_per_evaluation | 2026-08-22: batch redemption synchronized the fleet's weekly resets. C-18.1 now defers to C-23.16, which makes it demand-only: one credit, for one waiting job, on that job's own lane, never on a held lane, and none while a lane reset in the last week still has room or inside the interval, both read from the store (2026-09-23; on release/217 2026-10-10). C-19.1's `op_key` (account key plus credit id) replaces the flock. |
| 161 | Redeem a reset credit only when the server confirms `limit_reached` and a concrete `available` credit of type `codex_rate_limits` exists. | `subfleet/reset_policy.py:88-98,248-259`; tests `tests/test_reset.py:64,79` | capacity-truth | keep | n/a | C-23.16 | unit | subfleet/actions.py | test_redemption_requires_server_limited_status_and_concrete_credit | No date; do not spend a scarce gift on a guess. Milestone 5: `C-18.1` adopts v1's reset-credit rule set only at interface level, so the double gate itself is uncontracted. |
| 162 | Order redemption candidates by furthest-out weekly reset first, then lowest in-flight, then lowest lane number. | `subfleet/reset_policy.py:149-165` | routing-policy | keep | n/a | C-23.38 | unit | subfleet/actions.py | test_redemption_orders_candidates_by_furthest_weekly_reset | 2026-08-22: Max's rule. Milestone 5. Opposite direction to `C-11.3`, which orders Codex routing by soonest `seven_day` reset and forbids in-flight as a key. |
| 163 | Prefer an unshadowed lane for redemption and use a shadowed one only when no unshadowed concrete credit exists. | `subfleet/reset_policy.py:168-174,567-598` | identity | keep | n/a | C-23.46 | unit | subfleet/actions.py | test_redemption_prefers_unshadowed_lane_over_desktop_shadowed | Shadowed lanes can be revoked by the app; v2's equivalent flag is `C-10.3` `desktop`, which bars them outright, so milestone 5 must state this fallback explicitly. |
| 164 | Send a fresh UUID4 `redeem_request_id` with every consume. | `subfleet/codex.py:402-420` | capacity-truth | keep | n/a | C-23.16 | unit | subfleet/actions.py | test_consume_sends_fresh_redeem_request_id_each_call | Idempotency for an irreversible action; v2's durable double-spend guard is `op_key` (account key plus credit id) and the UUID rides in the action's request JSON. |
| 165 | Accept a consume as successful only for code `reset` with `windows_reset` greater than zero. | `subfleet/codex.py:423-432` | capacity-truth | keep | n/a | C-23.16 | unit | subfleet/actions.py | test_consume_success_requires_reset_code_and_positive_windows_reset | Upstream success enum. Milestone 5: `C-19.1` owns the `confirmed` state but states no success predicate. The wham endpoint client may sit in `subfleet/adapters/codex.py`. |
| 166 | Make the redemption audit record durable before starting the up-to-90 s propagation poll. | `subfleet/reset_policy.py:649-657` | provenance/attestation | keep | n/a | C-19.1 | unit | subfleet/actions.py | test_redemption_audit_row_durable_before_propagation_poll | The poll can be interrupted after an irreversible consume; `C-19.1` is stronger, writing the row `pending` before the remote call. Secondary `C-3.2`: one transaction plus an `events` row. |
| 167 | Treat a confirmed consume as authoritative while the usage endpoint is stale: set the reset clock to now plus seven days and keep the lane dispatchable. | `subfleet/reset_policy.py:452-528,669-694` | capacity-truth | keep | n/a | C-23.17 | unit | subfleet/actions.py | test_confirmed_consume_overrides_stale_usage_endpoint | Endpoint lag must not undo a real redemption; the confirmed consume is the later `provider` reading showing the window reset that `C-9.6` requires. Now+7d is a guessed clock (`C-9.4`). |
| 168 | Report fleet credits remaining as null whenever any lane's count is unreadable, never as a false undercount. | `subfleet/reset_policy.py:106-123`; test `tests/test_reset.py:198` | capacity-truth | keep | n/a | C-23.18 | unit | subfleet/actions.py | test_fleet_credits_remaining_null_when_any_lane_unreadable | Unknown is rendered as unknown. Milestone 5: `C-9.1` forbids a percentage without a `provider` reading but says nothing about aggregate credit counts. |
| 169 | Clear the lane's dispatch cooldown after a successful redemption. | `subfleet/reset_policy.py:661-667` | capacity-truth | keep | n/a | C-23.17 | unit | subfleet/actions.py | test_successful_redemption_clears_lane_dispatch_cooldown | The reset supersedes the 15-minute cooldown, a `local-backoff` closure (`C-9.1`). Milestone 5: `C-9.6` names no release for a local-backoff and forbids shortening. |
| 170 | List and consume only gifted entitlements, and provide no purchase or add-credit path. | `subfleet/codex.py:395-399`, `README.md:213-215` | safety-guard | keep | n/a | C-23.7 | unit | subfleet/actions.py | test_reset_credits_client_never_calls_purchase_path | Upsell CTAs are ignored by design. Milestone 5: `C-19.1`'s kind list has no purchase action, but no clause forbids the endpoint; assert a URL allowlist. |
| 171 | Bypass the full runner for keepalive: no salvage artifacts, no run directory, one compact usage marker. | `subfleet/keepalive.py:1-6,401-414` | ops-hygiene | replace | A keepalive runs outside the job path: it writes one `readings` row labelled `admission-observed` plus an `events` row, and creates no job row, no attempt directory, and no salvage artifact. | C-8.4 | fake | subfleet/keepalive.py | test_keepalive_writes_reading_without_job_directory | Mandated by plan B appendix A: the readings table replaces the run directory. `C-8.4` places keepalive results in `readings`/`events`; `C-18.1` sets the 5 h 05 m cadence and label. |
| 172 | Stamp the five-hour window at the moment the provider request is sent, not when the keychain read or thread wait began. | `subfleet/keepalive.py:189-199` | capacity-truth | keep | n/a | C-23.19 | unit | subfleet/keepalive.py | test_keepalive_window_stamped_at_request_send | The 5 h window starts at the request. Milestone 5: no clause fixes the reading's observed-at instant, which must be the send time, not the worker's queue time. |
| 173 | Skip a lane that made any request within the last five hours, recording it as `skipped-open`. | `subfleet/keepalive.py:341-353` | capacity-truth | keep | n/a | C-23.19 | unit | subfleet/keepalive.py | test_keepalive_skips_lane_with_request_inside_five_hour_window | Another ping only consumes usage. `C-18.1` gives the 5 h 05 m cadence; v2 must count any attempt on the lane, not only keepalives, as having opened the window. |
| 174 | Count a running lane entry as a recent request, and never count a session-less rc 5 as one. | `subfleet/keepalive.py:65-99` | capacity-truth | keep | n/a | C-23.19 | unit | subfleet/keepalive.py | test_running_attempt_counts_as_request_but_sessionless_rc5_does_not | A no-session rc=5 may mean no provider request happened. Milestone 5: `C-9.1`'s `admission-observed` needs a success or rejection, so an in-flight attempt needs its own rule. |
| 175 | Mark a 401/403 lane auth-dead, skip it without a request, log the detail at most daily, and clear the hold on re-enrolment. | `subfleet/keepalive.py:102-154,315-339`, `subfleet/cli.py:509-515` | identity | keep | n/a | C-23.44 | unit | subfleet/keepalive.py | test_keepalive_skips_auth_dead_lane_until_reenrollment | Repeated dead-token pings and alert spam. Milestone 5: `C-9.3` owns `auth-dead`; re-enrolment clears it via `C-1.3`'s new lane id; the daily log cadence is uncontracted. |
| 176 | Inspect 401/403 codes only after ruling out a successful response. | `subfleet/keepalive.py:232-247` | capacity-truth | keep | n/a | C-9.3 | unit | subfleet/adapters/claude.py | test_success_envelope_with_incidental_401_is_not_auth_dead | A success envelope can carry 401 in a duration field. `C-9.3` states the same guard for a limit phrase with a successful `system/init`; precedence is `C-9.2`. Add a `C-12.7` fixture. |
| 177 | Run keepalives with at most four workers and a 60 s per-lane timeout, writing state under a lock. | `subfleet/keepalive.py:28-29,274-285,363-386` | ops-hygiene | keep | n/a | C-23.29 | fake | subfleet/keepalive.py | test_keepalive_pass_bounds_workers_and_per_lane_timeout | Bounded concurrency for a launchd job. `C-16.4` gives queued workers with deadlines; v2 drops the flock for store transactions (`C-3.2`); put 4 and 60 s in `policy.json` caps. |
| 178 | Accept a 403 or authenticated 429 at enrolment as the expected inference-only scope, and reject a 401. | `subfleet/cli.py:492-495` | identity | keep | n/a | C-9.3 | unit | subfleet/adapters/claude.py | test_enrollment_accepts_scope_403_and_rejects_401 | Setup tokens cannot read usage. `C-9.3` covers 403-not-evidence and 401-auth-dead but not the authenticated 429. Fixture premise for C-20.1's Haiku probe: that a setup token gets 403 or 429 and never 401. |
| 179 | Nudge only from SessionStart sources `startup` and `resume`, never `compact` or `clear`. | `subfleet/tickle.py:70,293-295` | session-continuity | keep | n/a | C-23.33 | unit | subfleet/sessions/tickle.py | test_nudge_only_on_startup_and_resume_session_sources | Compaction is not a restart. Milestone 6: `C-15.2` mentions SessionStart only for surfacing notices, not for nudging a session. |
| 180 | Cap the age of an interruption eligible for a nudge at eight hours by default. | `subfleet/tickle.py:60,299-303` | session-continuity | keep | n/a | C-23.33 | unit | subfleet/sessions/tickle.py | test_nudge_skips_interruption_older_than_eight_hours | An abandoned turn is not resumed because a tab reopened. Milestone 6; the 8 h default belongs beside the caps in `policy.json`. |
| 181 | Nudge once per interruption point and enforce a per-session cooldown. | `subfleet/tickle.py:241-244,304-311` | session-continuity | keep | n/a | C-23.33 | unit | subfleet/sessions/tickle.py | test_nudge_once_per_interruption_point_with_session_cooldown | Milestone 6; no clause covers nudge dedupe or the per-session cooldown. Guards against restart storms; the cooldown constant belongs in `policy.json`. |
| 182 | Re-check the transcript after the nudge delay and skip when the real last turn has changed. | `subfleet/tickle.py:389-423` | session-continuity | keep | n/a | C-23.34 | unit | subfleet/sessions/tickle.py | test_nudge_skipped_when_last_turn_changed_during_delay | Milestone 6; GAP. The CLI's own `--resume` or a typed "." may already have continued the turn, so the worker re-reads before nudging. |
| 183 | Require additional transcript quiet before a manual sweep nudges a session. | `subfleet/tickle.py:419-423` | session-continuity | keep | n/a | C-23.34 | unit | subfleet/sessions/tickle.py | test_manual_sweep_requires_extra_transcript_quiet | Milestone 6; GAP. Outside `SessionStart` an interrupted tail can be a long tool call, so a manual sweep needs a longer quiet window. |
| 184 | Recognize the app's synthetic resume stub and judge the turn underneath it. | `subfleet/tickle.py:23-29,163-172` | session-continuity | keep | n/a | C-23.34 | unit | subfleet/sessions/tickle.py | test_synthetic_resume_stub_judged_by_underlying_turn | 2026-08-23: observed four times in one session. Milestone 6; the stub pattern belongs beside the transcript reader, with a fixture transcript. |
| 185 | Defer the hook's dedupe and cooldown verdicts to the worker, which re-decides against the fresh transcript. | `subfleet/cli.py:863-884` | session-continuity | keep | n/a | C-23.34 | unit | subfleet/hooks.py | test_hook_defers_dedupe_and_cooldown_to_worker_recheck | 2026-08-24: the stub lands about 0.7 s after the hook, so a hook-time "already nudged" blocked a fresh restart. Milestone 6; the hook is milestone 4 `subfleet/hooks.py`. |
| 186 | Make cross-tier revive an explicit choice: without `--model`, revive on the session's own recorded tier. | `subfleet/tickle.py:957-961`; test `tests/test_tickle.py:834` | routing-policy | keep | n/a | C-23.39 | unit | subfleet/sessions/tickle.py | test_revive_without_model_flag_keeps_recorded_tier | 2026-08-26 (Max): a Fable-grade session on Opus is worse than a parked one. Milestone 6; the tier comes from the session record, not a `policy.json` chain. |
| 187 | Never revive a headless lane run as a continuation. | `subfleet/tickle.py:1008-1014`; test `tests/test_tickle.py:361` | session-continuity | keep | n/a | C-23.31 | unit | subfleet/sessions/tickle.py | test_revive_never_continues_a_headless_lane_run | 2026-09-04: the sweep revived five dead `claude -p` lanes, burning windows with no reader. `C-15.4` states the prohibition for `wait`; the revive sweep is milestone 6. |
| 188 | Revive only sessions whose recorded permission mode is `bypassPermissions`. | `subfleet/tickle.py:1020-1027` | session-continuity | keep | n/a | C-23.35 | unit | subfleet/sessions/tickle.py | test_revive_requires_bypass_permissions_mode | Milestone 6; GAP. A revived run without `bypassPermissions` would deny its own tools, so the recorded mode is a candidate filter. |
| 189 | Probe a lane live before reviving rather than trusting the lane ledger's estimates. | `subfleet/tickle.py:685-735` | capacity-truth | keep | n/a | C-23.20 | fake | subfleet/sessions/tickle.py | test_revive_probes_lane_before_launch | 2026-08-25: three "healthy" lanes were out of Fable. Milestone 6; nearest clauses are `C-11.4` (probe before dispatch) and `C-9.1` (estimates gone from the label set). |
| 190 | Report a too-old Claude CLI once per target as a host fault, never as "no lane serves". | `subfleet/tickle.py:80-88,1044-1055`; test `tests/test_tickle.py:1098` | ops-hygiene | keep | n/a | C-17.3 | unit | subfleet/sessions/tickle.py | test_old_claude_cli_reported_as_host_fault_not_no_lane | 2026-09-02: launchd ran a cask at 2.1.87 while `~/.local/bin/claude` was 2.1.258. `unit` proves the core from a fixture stderr; real CLI version behaviour is the live half; dedupe is milestone 6. |
| 191 | Never list or revive a session the operator retired. | `subfleet/tickle.py:536,1004-1007`; test `tests/test_tickle.py:420` | session-continuity | keep | n/a | C-23.35 | unit | subfleet/sessions/tickle.py | test_retired_session_never_listed_or_revived | Milestone 6; GAP. Operator retirement must survive as a durable session flag that both the listing and the revive candidate filter honour. |
| 192 | Refresh the live-revive census under the pass lock before every launch and skip a session already running as a detached revive. | `subfleet/tickle.py:845-851,976-982,1058-1069` | process-survival | keep | n/a | C-23.55 | fake | subfleet/sessions/tickle.py | test_revive_census_refreshed_and_skips_running_twin | 2026-09-04: a headless revive twin ran alongside a live session and re-dispatched its lanes. Milestone 6; `C-6.3`/`C-6.5` lease machinery replaces the pass-lock census. |
| 193 | Bind approval to a caller-attested fingerprint and never infer it from a fresh read of the artifact. | `subfleet/consensus.py:268-301` | safety-guard | keep | n/a | C-23.8 | unit | subfleet/gate.py | test_gate_approval_bound_to_attested_fingerprint | Milestone 7; GAP. Fingerprint is `--expect-sha256` for a plan, `--expect-head` plus `--expect-base` for a PR; `C-19.1` keys a merge action by head sha. |
| 194 | Require exactly one sentinel-delimited JSON verdict, bound to the same revision, with no text outside it. | `subfleet/consensus.py:476-493` | safety-guard | keep | n/a | C-23.9 | unit | subfleet/gate.py | test_peer_verdict_requires_single_sentinel_json_block | Milestone 7; GAP. Guards verdict forgery and drift; parser fixtures belong beside the `C-12.7` corpus, redacted the same way. |
| 195 | Reject an approval that carries findings or notes, and a changes-requested with no finding. | `subfleet/consensus.py:518-523` | safety-guard | keep | n/a | C-23.9 | unit | subfleet/gate.py | test_verdict_approval_rejects_findings_and_empty_changes_request | Milestone 7; GAP. Approval must mean zero actionable findings and empty notes; changes-requested needs at least one finding. |
| 196 | Block the round if the artifact revision changed while the peer was reviewing. | `subfleet/consensus.py:1143-1148` | safety-guard | keep | n/a | C-23.8 | unit | subfleet/gate.py | test_gate_blocks_round_when_revision_changed_mid_review | Milestone 7; GAP. The captured revision is re-compared after the peer returns, so a moving target blocks rather than approves. |
| 197 | Require a positive Fable attestation and the absence of a downgrade marker before a Fable peer's verdict counts. | `subfleet/consensus.py:541-557,1151-1154`; test `tests/test_consensus.py:260` | provenance/attestation | keep | n/a | C-23.43 | unit | subfleet/gate.py | test_fable_peer_verdict_requires_attestation_without_downgrade | Milestone 7; GAP. `C-12.5` supplies the attestation and forbids false positives; the gate adds that an unattested or downgraded verdict never counts. |
| 198 | Run the peer read-only and isolated, from a neutral temporary directory outside the repository. | `subfleet/consensus.py:1073-1074,1101-1121` | safety-guard | keep | n/a | C-23.10 | unit | subfleet/gate.py | test_peer_round_runs_read_only_from_neutral_dir | Milestone 7; GAP. `C-2.4` refuses a `/tmp` workdir, so v2 allocates the neutral dir under `$SUBFLEET_HOME`; `C-14.4` proves the read-only sandbox itself. |
| 199 | Reserve each round under a live lease so abandoned output never counts as a new approval. | `subfleet/consensus.py:972-1006,1180-1191`; tests `tests/test_consensus.py:578,614` | safety-guard | keep | n/a | C-23.10 | fake | subfleet/gate.py | test_gate_round_lease_live_or_output_discarded | Milestone 7; GAP. In v2 the round lease is a store row, so a dead holder's late output is discarded instead of counted; prevents duplicate reviews. |
| 200 | Stop after four peer rounds by default. | `subfleet/consensus.py:31,1202-1204` | UX-contract | keep | n/a | C-23.53 | unit | subfleet/gate.py | test_gate_stops_after_four_peer_rounds | Milestone 7; GAP. Default four rounds (v1 `DEFAULT_MAX_ROUNDS`); the cap belongs beside the `C-6.4` caps in `policy.json`. |
| 201 | Preflight a merge for an open, non-draft PR with unchanged head and base, clean mergeability, and terminal green CI. | `subfleet/consensus.py:600-655` | safety-guard | keep | n/a | C-23.11 | unit | subfleet/gate.py | test_merge_preflight_requires_open_undrafted_unchanged_green_pr | GAP: milestone 7 gates. C-19.1 covers the action row only, not the merge preflight predicate; test it against captured PR JSON fixtures. |
| 202 | Merge with `--match-head-commit` pinned to the approved head. | `subfleet/consensus.py:848-858` | safety-guard | keep | n/a | C-23.11 | unit | subfleet/gate.py | test_merge_command_pins_match_head_commit | GAP: milestone 7. GitHub's head guard is the only atomic part, so v2 must keep `--match-head-commit` in the merge argv. |
| 203 | Verify the landing against the immutable merge commit's parents, not the moving base tip. | `subfleet/consensus.py:658-696` | safety-guard | keep | n/a | C-23.12 | unit | subfleet/gate.py | test_landing_verified_against_merge_commit_parents | GAP: milestone 7. Expected parents are the approved base, plus the approved head for method `merge`; the base branch moves after approval. |
| 204 | Report a post-merge mismatch as a mismatch; never retry and never auto-revert. | `subfleet/consensus.py:775-784`; test `tests/test_consensus.py:671` | safety-guard | keep | n/a | C-23.12 | unit | subfleet/gate.py | test_merged_revision_mismatch_reported_without_retry_or_revert | GAP: milestone 7. C-19.1 supplies the terminal `failed` state, but no clause states never-retry and never-auto-revert; a race must not be papered over. |
| 205 | Treat unknown merge-queue or auto-merge membership as "do not retry". | `subfleet/consensus.py:699-739`; test `tests/test_consensus.py:855` | safety-guard | keep | n/a | C-19.1 | unit | subfleet/gate.py | test_unknown_merge_queue_membership_blocks_retry | C-19.1's `unknown` is settled by a read of remote state, never by a retry; `subfleet/gate.py` reads queue and auto-merge membership. Incident: duplicate merges. |
| 206 | Let only the currently reserved action publish its result, and never overwrite a completion. | `subfleet/consensus.py:775-784`; test `tests/test_consensus.py:776` | safety-guard | keep | n/a | C-23.13 | unit | subfleet/actions.py | test_only_reserved_action_publishes_and_completion_is_never_overwritten | Unique `op_key` plus C-3.2's one-transaction-per-transition replace v1's file lock; the rule survives, the mechanism moves to the store. |
| 207 | Support merge and squash only, and refuse to verify a rebased landing. | `subfleet/consensus.py:37,690-693` | safety-guard | keep | n/a | C-23.12 | unit | subfleet/gate.py | test_rebased_landing_refused_only_merge_and_squash_supported | GAP: milestone 7. A rebased landing has no verifiable parent pair, so the gate cannot confirm it landed the approved revision. |
| 208 | Scrub credentials and encoded binary from a handoff while retaining ordinary code, commands, and tool output. | `subfleet/handoff.py:1-8,51-160` | safety-guard | keep | n/a | C-23.14 | unit | subfleet/sessions/handoff.py | test_handoff_scrubs_credentials_and_binary_but_keeps_code | GAP: milestone 6 handoff. C-10.5 is the analogous milestone-1 rule and covers lane credentials only; lossy rewrites destroy continuity. |
| 209 | Suppress the results of credential-reading tool calls (`agent-secret get`, keychain reads, `env`, `auth.json`). | `subfleet/handoff.py:100-110` | safety-guard | keep | n/a | C-23.14 | unit | subfleet/sessions/handoff.py | test_handoff_suppresses_credential_reading_tool_results | GAP: milestone 6 handoff. Suppression is by tool-input pattern, so secrets never reach the excerpt even unredacted. |
| 210 | Bound every handoff section with explicit character caps and keep the source transcript path as the durable record. | `subfleet/handoff.py:29-44,1-8` | session-continuity | keep | n/a | C-23.36 | unit | subfleet/sessions/handoff.py | test_handoff_sections_capped_and_source_transcript_path_recorded | GAP: milestone 6 handoff. Caps bound each section against unbounded context; the transcript path stays authoritative rather than a lossy rewrite. |
| 211 | Dispatch handoffs detached through `subfleet run` so they inherit routing, guard, salvage, ledger, and notices. | `subfleet/handoff.py:642-655` | process-survival | keep | n/a | C-23.54 | fake | subfleet/sessions/handoff.py | test_handoff_dispatches_detached_through_the_normal_submit_path | GAP: milestone 6 handoff. C-17.6 gives the detached default, but no clause requires handoffs to use the one submission path. |
| 212 | Create the handoff prompt with mode 0600 and unlink it after dispatch. | `subfleet/handoff.py:637-664` | safety-guard | replace | A handoff dispatches through the job API (row 211), so its prompt is the job's `jobs/<job id>/prompt.md` at mode 0600 inside the 0700 state root; it is written once and retained as the immutable record rather than unlinked, and retention (C-8.4) removes it. | C-2.3 | process | subfleet/sessions/handoff.py | test_handoff_prompt_created_private_and_unlinked_after_dispatch | Prompt privacy. Verdict change: the 0600 half is C-2.3; the unlink half is superseded because the prompt no longer sits in the system temp directory. Secondary C-8.4. |
| 213 | Pin a Codex resume to the recorded home, never invoking the picker or substituting an account. | `subfleet/resume_codex.py:1,180-196` | session-continuity | keep | n/a | C-12.3 | unit | subfleet/adapters/codex.py | test_codex_resume_pins_recorded_home_and_thread | C-12.3 names `codex exec resume <thread id>` on the same home; C-4.6 forbids an attempt changing lane. Codex thread indexes are home-local. |
| 214 | Park a resume with rc 3 when the recorded home is cooled, limited, or exhausted. | `subfleet/resume_codex.py:19,121-130` | session-continuity | keep | n/a | C-11.2 | fake | subfleet/policy.py | test_pinned_resume_on_closed_lane_parks_with_exit_three | C-11.2 pins never fall back; C-17.3 maps no candidate to exit 3 naming the earliest reset. `fake` proves park-not-migrate end to end. |
| 215 | Treat a capacity observation as advisory during resume; the pinned runner is the authority. | `subfleet/resume_codex.py:42-46` | capacity-truth | keep | n/a | C-11.3 | unit | subfleet/policy.py | test_unreadable_capacity_reading_does_not_block_pinned_resume | C-11.2's filter excludes only unexpired closures, so a missing or unreadable reading leaves the lane eligible but unmeasured; stale rows must not block. |
| 216 | Refuse to resume a source run that is still running, and close a pre-created entry the runner never adopted. | `subfleet/resume_codex.py:59-66,85-90,209` | ops-hygiene | keep | n/a | C-4.2 | fake | subfleet/daemon.py | test_resume_refused_while_source_running_and_unadopted_reservation_closed | C-4.2's `reserved-no-launch` recovery closes the unadopted attempt; refusing a resume of a non-terminal source is not enumerated in C-6.1 or C-6.5. |
| 217 | Make every path env-overridable so tests never touch real state. | `subfleet/paths.py:1,10-13` | ops-hygiene | keep | n/a | C-2.1 | fake | subfleet/contracts.py | test_state_root_env_override_keeps_tests_off_real_state | v2 collapses v1's many overrides into `$SUBFLEET_HOME`, resolved in the shared seam; tests run in a temp root (C-20.1) and write nothing outside it. |
| 218 | Keep `~/.claude` project transcript lookups to at most one directory below `projects`. | `subfleet/capacity.py:503-507` | ops-hygiene | keep | n/a | C-23.40 | unit | subfleet/adapters/claude.py | test_claude_transcript_lookup_bounded_to_one_level_below_projects | C-12.5 locates the transcript by session uuid under `~/.claude/projects/`; keep the glob one level deep, never an unbounded recursive walk. |
| 219 | Honour legacy `CARPOOL_*` variable names in every entry point. | `bin/subfleet-claude:81-86`, `bin/subfleet-codex:76-81`, `bin/subfleet-guard-hook:59-64`, `bin/codex:17-22`, `bin/subfleet-hook:23-28` | ops-hygiene | drop | n/a | n/a | n/a | n/a | n/a | 2026-08-23: the rename kept `CARPOOL_*` "for a transition week". Plan B drops `DELEGATE_*`, `CARPOOL_*`, and `CLAUDE_LANE_*`; one prefix only. |
| 220 | Keep lane cooldowns at `~/.local/state/delegate/cooldowns.json` via `DELEGATE_STATE_DIR`. | `subfleet/paths.py:61-63,143-144` | ops-hygiene | drop | n/a | n/a | n/a | n/a | n/a | C-2.1 forbids state outside `$SUBFLEET_HOME`; cooldowns become closure rows (C-9.6), so the ai-quota-era split path and its migration hazard disappear. |

## Gaps

The 92 rows that read `GAP` in version 1 are resolved in `invariant-gaps.md`, which holds the
proposed clause text for each, grouped by class, with the conflicts a fold-in would create.
None of the 92 turned out to be covered by an existing clause; all 92 carry a proposed clause,
55 clauses in total, and no row still reads `GAP`.

## Replaced

Twenty-one rows change obligation rather than mechanism. Each is quoted from
`reports/A-invariants.md` with the v2 rule that supersedes it and the authority for the change.
Eight were decided before this lane started — the four the ledger itself marks `keep-simplified`
(44, 57, 58, 116) and the four plan B rev 4 names in Appendix A (125 to 127, 171). The other
thirteen are verdict changes made here and listed again in the lane report.

### Prompts and ownership

#### 7 — skip salvage when the caller prompt cannot be removed

> If the private caller prompt cannot be cleaned up, skip salvage and push entirely. —
> `bin/subfleet-claude:418-429`, `bin/subfleet-codex:532-542`, "prompts must never reach a remote",
> safety-guard

**Replacement.** The daemon holds the prompt at `jobs/<job id>/prompt.md` (C-2.3), outside the
worktree salvage snapshots, so there is nothing to clean up and salvage is unconditional (C-13.1).
v1 traded a run's uncommitted work for prompt privacy; v2 needs no such trade. What may be pushed
is governed by C-13.2, which still refuses any push of a salvage ref to `main` or `master`.
Residual for the integrator: a caller who passes `-p` pointing at a file *inside* `-C` still gets
that file into the local salvage tree. Nothing refuses that today.

#### 8 — delete only an owned prompt

> Delete only an owned prompt whose basename is `delegate-prompt-*.md` and which is the exact `-p`
> file. — `bin/subfleet-claude:238-259`, `bin/subfleet-codex:224-243`, "inherited env markers do not
> prove ownership", safety-guard

**Replacement.** The daemon copies the caller's prompt into `jobs/<job id>/prompt.md` and never
removes or modifies a caller file. v1 needed an ownership test because it deleted; v2 does not
delete, so the test has no subject. C-2.1's write restriction expressly permits writes inside a
caller-named workdir, so it does not itself forbid the deletion. The clause is C-23.1 in
`invariant-gaps.md`.

#### 114 — hand the temporary prompt to the runner

> Transfer ownership of the merged temporary prompt to the detached runner. —
> `subfleet/delegate.py:1129-1131`, "double deletion or a missing prompt", ops-hygiene

**Replacement.** There is no merged temporary prompt and no handover. C-2.3 writes `prompt.md`
once, as the bytes actually sent including anything C-6.7 prepends, and nothing deletes it before
retention. The two failure modes the handover guarded against — deleting twice, or deleting the
file the child still needs — are removed rather than managed.

#### 212 — create the handoff prompt 0600 and unlink it

> Create the handoff prompt 0600 and unlink it after dispatch. — `subfleet/handoff.py:637-664`,
> "prompt privacy", safety-guard

**Replacement.** A handoff dispatches through the job API (row 211), so its prompt is the job's
`jobs/<job id>/prompt.md` at mode 0600 inside the 0700 state root (C-2.3). The 0600 half is kept
exactly; the unlink half is superseded, because the prompt no longer sits in the system temp
directory where deleting it was the only protection. It is retained as the immutable record and
removed by retention (C-8.4).

### Process identity and receipts

#### 32 — unset the run identity variables

> Unset every `SUBFLEET_RUN_*` identity variable after recording the run so nested dispatches cannot
> inherit it. — `bin/subfleet-claude:747-749`, `bin/subfleet-codex:257-260`, "id hijack by a child
> dispatch", provenance/attestation

**Replacement.** v2 cannot unset the marker: C-5.1 puts `SUBFLEET_ATTEMPT=<attempt id>` in the
child's environment on purpose, and C-5.5 enumerates processes by that marker as one of the three
containment sources. Removing it would break containment. The hijack is prevented at the other
end instead: the daemon mints every job id (C-1.1) and attempt id (C-1.2), a submission's identity
comes from its request id (C-1.5) and payload digest (C-6.2), and an inherited `SUBFLEET_JOB` is
usable only as `--parent` (C-7.3, C-17.2). The clause is C-23.41 in `invariant-gaps.md`.

#### 87 — mark a run orphaned and reap it as rc=-9

> Mark a run ORPHANED when its pid is gone with no finish record, reap as rc=-9, and leave pid-less
> entries alone. — `subfleet/run_ledger.py:726-728,841-867`, "`kill -9` skips the EXIT trap; older
> runners recorded no pid", ops-hygiene

**Replacement.** C-4.4: a job whose final attempt ends without an rc is `lost`, never `succeeded` —
and never a synthetic rc either. The `ORPHANED` token and the invented `-9` both go: an attempt
whose guardian is gone with no `exit.json` is resolved by containment (C-5.5) and finalized `lost`,
with the raw evidence kept beside it. There are no pid-less entries to leave alone, because a
`running` attempt always carries pid, boot id, and process start from `start.json` (C-5.2), and an
attempt that never launched is recovered at its own boundary as `reserved-no-launch` (C-4.2).

#### 88 — a grace period before declaring an orphan

> Require a dead pid to persist for a grace period (30 s waiting, 60 s reaping) before calling a run
> orphaned. — `subfleet/run_ledger.py:796-838,841-848`, "races between exit and finish write",
> ops-hygiene

**Replacement.** The race the graces covered is closed by ordering: C-5.2 has the guardian write
`exit.json` by temp file and rename *before* it exits, so a dead guardian with no receipt is not a
race but a fact, decided by containment rather than by waiting. One grace survives, at the one
boundary where a receipt is legitimately not yet written: `start_grace_s`, 10 s, at `starting`
(C-4.2).

### The guard

#### 44 — the duplicated API-lane check

> Duplicate the API-lane check in self-contained python inside the runner so it holds without the
> wrapper. — `bin/subfleet-codex:283-300`, "wrapper may be absent", routing-policy, keep-simplified

**Replacement.** The rule the duplication defended — never dispatch on an API key — is kept and
becomes single-sited. `CodexAdapter.enroll` refuses an API-key `auth.json` (C-10.2), admission
refuses an API-key home with exit 7 and the fix named (C-6.5), and `build_launch` removes
`CODEX_API_KEY` and `OPENAI_API_KEY` from the child environment (C-12.3). There is no wrapper to
be absent, so there is nothing to duplicate; the isolation matrix (C-14.4) asserts the child
inherited neither key.

#### 49 — arm the never-rules on every Codex launch

> Arm the nine NEVER rules on every codex launch through the session-flags layer with the trust hash
> pinned. — `bin/subfleet-guard:115-132`, `bin/subfleet-codex:485-493`, 2026-08-08 load 47 and
> 2026-08-18 load 57 crawls, safety-guard

**Replacement.** C-14.3 arms the override on every `workspace-write` Codex launch with the hash
pinned by C-14.1; a `read-only` Codex launch carries no override and relies on the sandbox. That
is narrower than v1, and deliberately flagged: the incidents behind this row were *crawls* —
load 47 and load 57 — which a read-only sandbox does not prevent, because reading is exactly what
a crawl does. See Open questions.

#### 57 — the shared `unscoped-search` region

> Keep `unscoped-search` as one shared region, byte-identical with the Claude hook; never edit the
> codex copy. — `bin/subfleet-guard-hook:16-23,364,627`, safety-guard, keep-simplified

**Replacement.** v2 holds one hook file, `subfleet/guard/never-rules-hook.sh`, copied byte for byte
from v1 and pinned by SHA-256 in `subfleet/guard/TRUST` (C-14.1). One file cannot drift from
itself, so the shared-region contract collapses into the file hash. What survives from v1 is the
prohibition on editing the copy: the pinned hash fails the moment anyone does.

#### 58 — the seven byte-identical rule blocks

> Copy the seven Claude rule blocks byte-for-byte and pin the drift with tests; edit the Claude hook
> first. — `bin/subfleet-guard-hook:5-15`, `docs/guard.md:99-102`, 2026-08-19, safety-guard,
> keep-simplified

**Replacement.** Same mechanism as row 57: seven block-level drift tests become one whole-file
SHA-256 pinned in `TRUST` (C-14.1). The "edit the Claude hook first" ordering survives as a
maintenance rule — rule changes land upstream in the global Claude hook and the file is re-copied
wholesale, with the pin updated in the same commit — and the decision-parity corpus v1 kept only in
its tests becomes milestone 8's parity suite (plan B, build sequence 8).

### Capacity truth

#### 125 — learned capacity from account-scope limits

> Calibrate learned capacity only from account-scope hard limits, never from model-bucket limits. —
> `subfleet/capacity.py:466-486`, capacity-truth

#### 126 — the `estimated` / `observed` / `live` labels

> Label lane windows `estimated` unless a learned capacity exists (`observed`) or a live reading is
> present (`live`). — `subfleet/capacity.py:1134-1152`, capacity-truth

#### 127 — the keepalive marker as an observed reset

> Treat a keepalive marker as an exact observed 5h reset until that window closes, then stop. —
> `subfleet/capacity.py:443-463,1147-1152`, capacity-truth

**Replacement for 125 to 127.** Plan B, "What gets dropped": "Learned-capacity estimator and
`estimated` percentages | produced 4974%; no sensor behind it". Appendix A: "Rows 125 to 127 are
replaced by `provider` readings from the stream event and the usage endpoints." There is no
estimator left to calibrate (125) and no `estimated` or `observed` label to assign (126): C-9.1
fixes a closed label set — `provider`, `stale-provider`, `admission-observed`, `local-backoff`,
`unknown` — and forbids rendering a percentage from anything but a `provider` or `stale-provider`
reading, with amendment 15 adding that admission-observed and local-backoff evidence render as
words. The calibration rule's purpose survives as scope discipline rather than arithmetic: a
`limited` outcome carries scope `account` or a model id (C-9.4) and a closure is keyed on that
scope (C-9.6), so a model-bucket limit can never make a healthy model look exhausted. A keepalive
no longer manufactures a reset clock (127); it is an `admission-observed` reading (C-18.1), which
records that the model was admitted and never claims remaining quota. Real reset clocks come from
the Claude `rate_limit_event`'s `resetsAt` and the Codex windows (C-9.8, C-9.7).

#### 147 — the `derived` window label

> Label an activity-derived 5h window `derived`, never as a server reading. —
> `subfleet/claude.py:778-799`, capacity-truth

**Replacement.** v2 derives no windows. `derive_five_hour_window` reconstructed a 5h window from
local session activity because there was no server sensor for a lane; experiment 0 supplied one,
and C-9.1's label set is closed with no `derived` member. The rule underneath — an estimate is
never presented as a server reading — is what C-9.1 now enforces for every source: session activity
produces at most `admission-observed` evidence, which renders as words and never as a percentage.

#### 171 — keepalive bypasses the run directory

> Bypass the full runner for keepalive: no salvage artifacts, no run directory, one compact usage
> marker. — `subfleet/keepalive.py:1-6,401-414`, "keepalives are not dispatches", ops-hygiene

**Replacement.** Plan B, Appendix A: "row 171 is replaced by the readings table." The distinction
the row protects — a keepalive is not a dispatch and must not litter the ledger — becomes
structural rather than a bypass. C-8.4: "Probe and keepalive results live in `readings` and
`events`, not in `jobs`." A keepalive creates no job, no attempt, and no job directory, so it has
no salvage path and no deliverable; it writes one `admission-observed` row (C-18.1). v1 needed an
explicit bypass because its only execution path was the full runner; v2 needs none because a
keepalive never enters that path.

### Routing and evidence

#### 94 — the Codex thread id from stderr

> Take the Codex thread id from the first anchored `session id:` header in stderr, with a bounded
> scan. — `subfleet/run_ledger.py:180-206`, "later tool output quotes other Codex logs",
> provenance/attestation

**Replacement.** C-12.3: the thread id is read from the `thread.started` event of the
`codex exec --json` stream. The anchored header and the bounded scan have no subject, because the
identity now arrives as one structured field rather than as text that later tool output can
imitate. The anti-spoofing principle is what carries over, and its test is a fixture whose stream
quotes another run's Codex log.

#### 107 — rank the desktop login last

> Rank the desktop login last among Claude lanes, behind every other dispatchable lane including
> blind ones. — `subfleet/delegate.py:568-584`, `subfleet/capacity.py:1315-1322`, standing order
> re-stated 2026-09-04, routing-policy

**Replacement.** C-10.3 removes the desktop lane from the candidate set outright; it is
dispatchable only when the job carries `--allow-desktop`. Ranking is not the mechanism any more,
so v1's ordering term has no counterpart. Plan amendment 14 records that "strictly last" already
overstated v1 — `delegate.py:576` sorts `not fable_stranded` ahead of `active` — and the ledger's
own rationale says the 10-point handicap was not enough. C-11.6's golden case is the check.

#### 140 — incremental rollout scans

> Scan Codex rollouts incrementally with a per-file size and mtime cache, snapshotting sizes before
> grep. — `subfleet/codex.py:569-575,649-690`, 2026-09-02: 72,799 stat calls per dispatch hung
> `subfleet run`, ops-hygiene

**Replacement.** There is no sweep to make incremental. C-12.1 scopes `classify(attempt_dir,
exit_info)` to one attempt, so limit and revocation evidence comes from that attempt's own
directory, stream, and stderr; the rollout tree is touched only by `attest`, which locates one file
by the thread id recorded at launch (C-12.5, C-12.3). The cache existed to make a fleet-wide scan
affordable, and the scan is gone.

### Auth and notices

#### 81 — accounting never fails a run

> Let notify or ledger accounting never fail a run. — `subfleet/cli.py:660-676`, "monitoring sidecar
> rule", ops-hygiene

**Replacement.** v2 splits the rule. The notice row is not a sidecar any more: C-15.1 writes it in
the same transaction as the terminal job state, where it must succeed or the transition does not
commit. Only *delivery* stays best effort — C-15.2's layers — and a failed delivery leaves the
notice `pending` or `offered` (C-15.3) without touching the job's state or rc. Rows 35 and 129, the
other two sidecar rules, stay `keep`: a hook failure and a usage-parse failure still change
nothing.

#### 116 — the 30-day rc=5 cooldown

> On rc=5, cool the account and print the exact re-enrollment ritual. —
> `subfleet/delegate.py:1275-1277,106-107`, "logins are Max-only", identity, keep-simplified
> (30 days is severe now that rc=5 is probe-confirmed, row 23)

**Replacement.** The re-enrolment ritual is kept verbatim; the 30-day clock is not. In v2
`auth-dead` is corroborated before it is declared — C-9.3 requires a 401 from a usage endpoint, an
explicit organisation block, or a refresh-token-revoked event, and a limit-looking phrase with a
successful `system/init` is `limited` instead. A corroborated `auth-dead` lane is held until its
credential changes, not until a timer expires, and the hold is released by re-enrolment or by
`subfleet lanes release` (C-17.1). A guessed month-long clock on evidence that is now definite
would park a live account for no reason, which is the 2026-09-03 incident behind row 23 in a slower
form. No clause states the hold or the ritual; C-23.44 in `invariant-gaps.md` proposes one, and
folds in row 175 so that nothing probes or pings the lane while the hold stands.

## Dropped

Four rows do not carry over. Two are the ledger's own `drop-with-reason` verdicts; two are verdict
changes recorded here and in the lane report, both because plan B removes the thing the rule
governs. A dropped row names no clause, owner, module, or test: its columns read `n/a`.

### 6 — disclose that `-b` pushes on every exit

> State plainly that `-b` pushes on every exit, including success, before review. —
> `bin/subfleet-claude:76-79`, `bin/subfleet-codex:71-74`; "public-repo exposure"

**Reason (verdict change; the ledger says keep).** The disclosure has no referent. v2's `run` has
no `-b` and no push-on-exit behaviour at all — C-17.2 enumerates `run`'s complete flag list and the
only three deprecated flags still accepted — and salvage writes a local ref under
`refs/subfleet-salvage/` (C-13.1). The hazard the warning existed for is removed rather than
documented. C-13.2 still refuses any push of a salvage ref to a branch named `main` or `master`, so
if a push-on-exit flag is ever added, this row comes back with it.

### 219 — the `CARPOOL_*` aliases

> Honour legacy `CARPOOL_*` variable names in every entry point. — `bin/subfleet-claude:81-86`,
> `bin/subfleet-codex:76-81`, `bin/subfleet-guard-hook:59-64`, `bin/codex:17-22`,
> `bin/subfleet-hook:23-28`; rename 2026-08-23, "for a transition week"

**Reason.** The ledger's own verdict: "four env generations (`DELEGATE_*`, `CARPOOL_*`,
`CLAUDE_LANE_*`, `SUBFLEET_*`) are pure debt in a rebuild; pick one prefix." Plan B drops the whole
family ("What gets dropped": "`CARPOOL_*`, `DELEGATE_*`, `CLAUDE_LANE_*` env, `~/.local/state/delegate`"),
amendment 1 confirms that milestone 8's compatibility removal covers exactly these env families
while every v1 *verb* spelling is permanent, and the transition week the aliases were written for
ended on 2026-08-30 by the README's own dating. v2 reads one prefix, `SUBFLEET_*`, plus the
provider-owned `CODEX_HOME`, `CODEX_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and
`CLAUDE_CODE_OAUTH_TOKEN` the adapters must set or clear.

### 220 — the `~/.local/state/delegate` cooldown path

> Keep lane cooldowns at `~/.local/state/delegate/cooldowns.json` via `DELEGATE_STATE_DIR`. —
> `subfleet/paths.py:61-63,143-144`; historical location from the ai-quota era

**Reason.** The ledger's own verdict: "move under the single state dir; the split path is a
migration hazard." C-2.1 makes `$SUBFLEET_HOME` the one state root and names the only three things
written outside it, and closures live in the store, not a JSON file (C-9.6, `store_schema.sql`).
The rule is not lost so much as satisfied by construction: there is no second state directory to
keep anything in. The v1 file is not abandoned — plan B's import step reads it explicitly,
"closures from `~/.local/state/delegate/cooldowns.json` with scope and source (unscoped legacy
holds stay conservative until a probe)".

### 143 — the statusline tap

> Treat the statusline tap as terminal-TUI only and rank it by `updated_at`, never as current. —
> `subfleet/claude.py:594-599,4-11`; test `tests/test_snapshot_live.py:3`; diagnosed 2026-08-12,
> last real capture 2026-07-22

**Reason (verdict change; the ledger says keep).** The rule governs a sensor v2 does not have.
Plan B, "What gets dropped": "Statusline tap | dead for the desktop app since 2026-07-22 (README)."
Keeping this row would put a rule about a nonexistent capture into the contract. What the rule
protects against is kept generically and more strongly: C-9.1 admits only five labels, renders a
percentage only from a `provider` or `stale-provider` reading, and marks a stale one stale — so no
local capture of any kind can be presented as current. The freshness-ranking half of the row
survives as row 144, which stays a keep.

## Counts

By disposition:

| disposition | rows |
|---|---|
| keep | 195 |
| replace | 21 |
| drop | 4 |
| **total** | **220** |

By class, with the disposition split, the rows the contract already covers, and the rows carrying a
clause proposed in `invariant-gaps.md`:

| class | rows | keep | replace | drop | `C-x.y` | `P-23.<n>` |
|---|---|---|---|---|---|---|
| safety-guard | 52 | 46 | 6 | 0 | 27 | 25 |
| capacity-truth | 40 | 35 | 4 | 1 | 28 | 11 |
| ops-hygiene | 33 | 25 | 6 | 2 | 15 | 16 |
| session-continuity | 20 | 20 | 0 | 0 | 6 | 14 |
| routing-policy | 19 | 17 | 2 | 0 | 16 | 3 |
| provenance/attestation | 16 | 14 | 2 | 0 | 12 | 4 |
| identity | 13 | 12 | 1 | 0 | 5 | 8 |
| UX-contract | 13 | 12 | 0 | 1 | 4 | 8 |
| work-salvage | 7 | 7 | 0 | 0 | 7 | 0 |
| process-survival | 7 | 7 | 0 | 0 | 4 | 3 |
| **total** | **220** | **195** | **21** | **4** | **124** | **92** |

The 92 proposed-clause rows become 55 clauses; `invariant-gaps.md` lists them by class with the
rows each covers. None of the 92 turned out to be covered by an existing clause under the test
"a clause covers a row only when a test of that clause fails if the invariant is violated".

By acceptance owner (surviving rows only):

| owner | rows |
|---|---|
| unit | 160 |
| fake | 45 |
| process | 11 |
| live | 0 |
| **total** | **216** |

By v2 module (surviving rows only, most-loaded first):

| module | rows |
|---|---|
| `subfleet/daemon.py` | 25 |
| `subfleet/adapters/claude.py` | 20 |
| `subfleet/policy.py` | 18 |
| `subfleet/adapters/codex.py` | 17 |
| `subfleet/gate.py` | 14 |
| `subfleet/actions.py` | 13 |
| `subfleet/sessions/tickle.py` | 13 |
| `subfleet/cli.py` | 12 |
| `subfleet/guard/never-rules-hook.sh` | 10 |
| `subfleet/notices.py` | 10 |
| `subfleet/guard/preflight.py` | 8 |
| `subfleet/alerts.py` | 7 |
| `subfleet/keepalive.py` | 7 |
| `subfleet/store.py` | 6 |
| `subfleet/credentials.py` | 5 |
| `subfleet/hooks.py` | 5 |
| `subfleet/sessions/handoff.py` | 5 |
| `subfleet/salvage.py` | 4 |
| `subfleet/adapters/base.py` | 3 |
| `subfleet/procs.py` | 3 |
| `subfleet/default_policy.json` | 2 |
| `subfleet/retention.py` | 2 |
| `subfleet/contracts.py` | 1 |
| `subfleet/guard/TRUST` | 1 |
| `subfleet/guardian.py` | 1 |
| `subfleet/ids.py` | 1 |
| `subfleet/importer.py` | 1 |
| `subfleet/sessions/mirror.py` | 1 |
| `subfleet/timers.py` | 1 |

