# D-TMP1: private temporary storage per provider attempt

Base: `c0759bd658d6b55a656117badf3ccfa8271eab43`. Work began at 2026-10-22 07:41 UTC. All changes are confined to the assigned worktree. Shared Git metadata is unwritable, and the assigned branch is already checked out by the caller, so commits use workspace-local `.git-tmp1` metadata on `fix/attempt-tmpdir-217`. No history was rewritten and nothing was pushed.

Each launch allocates an exclusive `subfleet-attempt-<safe-id>-<random>` directory with mode 0700 under the user's canonical Darwin temp root. The Darwin SDK defines `_CS_DARWIN_USER_TEMP_DIR` as 65537; Python uses that numeric constant when its `confstr_names` lacks the spelling. Production does not derive this root from the daemon's inherited TMPDIR. Test daemons use isolated fixture or test-session scratch. The daemon records the path, creation time, uid, device and inode before releasing the guardian's launch gate. A marker binds each allocation to its store, preventing one store's sweep from reclaiming another store's live work. Only TMPDIR is added or replaced; no other environment variable is introduced. The same path reaches guard preflight, the guardian, provider and its children. Retries, resumes, turns, capacity probes and enrollment all use this lifecycle.

Cleanup follows finalization and verified-empty containment. Both the terminal state and the census check are required at the deletion boundary. Quarantine, unknown containment, live members, salvage errors/skipped content and an explicit `tmpdir_keep` retain scratch. The deletion check requires the canonical temp parent, exact prefix, owning uid, directory type, no symlink, and the recorded device/inode. A known allocation remains identifiable from its durable evidence if a provider removes the discovery marker; unknown allocations require their store-bound marker. Descriptor-relative `shutil.rmtree` supplies its own symlink-swap protection. Nested symlinks are unlinked without traversing their targets. Deletion errors are logged and never fail finalization.

A probe's `contained` state is intermediate: classification may still need its scratch. Only `completed` probes are reclaimed, and enrollment cleanup follows consumption of the adapter result. Cleanup measurements merge into the current evidence after the census, preserving identities and any retention hold the census recorded. Only `FileNotFoundError` counts as an absent allocation; permission failures retain it for retry.

Startup and retention's cadence sweep leftovers, including while archive retention is dormant. Known terminal attempts get a fresh census; unknown allocations from this store must be older than 24 hours. Foreign-store, live, quarantined and salvage-held folders remain, as do unmarked or unverifiable unknown folders. Known folders whose marker was removed are reconciled from durable allocation identity without weakening the census gate. Successful cleanup commits `tmpdir.removed_at`. Until then retention pins the attempt's database evidence, including if physical deletion succeeded but receipt publication was interrupted. Retention's transactional pin checks read SQL evidence only. A provider may remove its own scratch; cleanup reconciles an absent allocation after containment.

The existing provisional disk placement reservation covers anticipated worktree and TMPDIR growth together. Already allocated scratch reduces filesystem free space on that volume, so an additional reservation for those same bytes would count them twice. Remaining scratch is included in the job's measured retention bytes and recorded at final cleanup. Attended turns and probes retain their existing admission exemption. Probe scratch measurements are saved in their durable probe records. As with worktrees, a disk policy pointed at a different volume measures that configured volume; this change does not add another volume policy.

Codex's installed 0.159.0 executable was inspected without a model request, real home or real credentials. `debug prompt-input` with `sandbox_mode="workspace-write"`, dummy credentials, an isolated CODEX_HOME and a dummy provider URL emitted the private TMPDIR in its writable roots. Repeating it with `sandbox_workspace_write.exclude_tmpdir_env_var=true` removed that root. This directly confirms that Codex derives it from the child environment. No explicit Codex directory flag is needed under the default policy. An actual nested Seatbelt write probe was attempted and refused by the enclosing workspace sandbox (`sandbox-exec: sandbox_apply: Operation not permitted`); it is not claimed as a successful execution probe.

Claude's detached writable jobs use bypass permissions. Its conversation turns also support ask and acceptEdits. All writable Claude launch builders now explicitly add the private scratch directory with `--add-dir`, preserving their existing approval modes. Read-only jobs and probes keep their existing restricted tool surface. The installed Claude 2.1.284 CLI's help and local launch code were inspected with an isolated config and dummy credentials; no model request was sent.

The required invariants and their witnesses are:

| Invariant | Test witnesses |
| --- | --- |
| 1. Two attempts never share TMPDIR. | Distinct live daemon launches, colliding sanitized ids, repeated ids, and the generated lifecycle sequences. |
| 2. TMPDIR exists, is 0700 and user-owned when the provider starts. | All four daemon spawn seams check existence, mode and uid. |
| 3. Nonempty/uncertain census and quarantine prohibit deletion. | Finalization quarantine regression, deletion predicate matrix, sweep guards and generated lifecycle sequences. |
| 4. The sweep cannot remove outside-prefix folders, symlinks or live attempts. | Prefix and symlink tests, live/quarantine/salvage sweep matrix, ownership/replacement checks and generated sequences. |
| 5. Probes and conversation turns receive private scratch. | Explicit turn, capacity-probe and enrollment regressions. |

Hypothesis generates at most 40 examples of at most 35 operations (start, finalize, quarantine, sweep), with five possible attempt identities. It uses the ci profile and isolated storage. Mutation tests execute altered production code in memory and require a named lifecycle assertion to fail: shared-folder reuse, omitted census check, omitted prefix check, and symlink-following validation. Production files are not rewritten by the mutation tests.

The four mutation witnesses use these named assertions:

| Mutation | Named assertion that must fail |
| --- | --- |
| Reuse one folder | `test_attempt_names_are_unique_even_after_path_sanitizing` |
| Remove without census check | `test_cleanup_requires_terminal_empty_unretained_attempt` |
| Omit sweep prefix check | `test_sweep_requires_prefix_even_with_valid_creation_record` |
| Follow a symlink | `test_sweep_never_follows_symlink` |

The test-session root also survives fixture undo and replacement of `os.environ`. Each fixture daemon uses a sibling temporary directory, so it cannot alter tests that assert exact contents of their own `tmp_path`.

<!-- TMP1_VALIDATION -->
## Validation

The tests-first base run failed all 20 initial cases. The corrected focused hold passed **271 cases and skipped one** real enrollment guardian case because process inspection is unavailable in this sandbox. All **55 initial D-TMP1 cases passed**, including the bounded property check and all four mutation witnesses. A later marker-removal follow-up adds three cases and extends the existing retention case. The initial broader focused hold had five fixture/journal failures; its complete rerun passed after fixture isolation and journal-transition assertions were corrected.

Required `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py`: **38 passed** (37 and 1 respectively). All pytest invocations used the host suite wrapper, one pytest at a time, one worker, no pytest cache, isolated TMPDIR/Hypothesis storage, and the ci profile. Broad holds had a 19-minute execution guard; slot waits are separate from execution time.

Counts below use the latest result of each test case, avoiding double counting reruns and preserving cases completed before an interrupted resume. Partial modules count only finished cases. The complete file inventory appears below, including every unrun module. This is not a clean full-suite result.

| Directory | Passed | Skipped | Failed | Errors | Modules seen | Partial | Unrun modules |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 10070 | 15 | 9 | 0 | 245/331 | 2 | 86 |
| `tests/fake` | 174 | 1 | 0 | 0 | 9/107 | 0 | 98 |

Final post-marker-removal focused hold: 312 passed, 1 skipped. **All 58 new D-TMP1 cases passed**, including all four mutation witnesses and the marker-removal regressions. Their latest per-case results appear below.

| Hold | Passed | Skipped | Failed | Errors | Pytest seconds | Wrapper wall seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `focused` | 266 | 0 | 5 | 0 | 192.04 | 3486.22 |
| `focused-rerun` | 271 | 1 | 0 | 0 | 49.83 | 1096.75 |
| `contract` | 38 | 0 | 0 | 0 | 6.9 | 484.6 |
| `unit-01` | 3516 | 0 | 0 | 0 | 329.77 | 1508.41 |
| `unit-remaining-01` | 2859 | 9 | 5 | 0 | 645.27 | 1248.36 |
| `unit-remaining-02` | 3387 | 6 | 3 | 0 | 424.9 | 645.81 |
| `unit-remaining-03` | 249 | 0 | 1 | 0 | 1149.52 | — |
| `fake-retention-all` | 42 | 0 | 0 | 0 | 1147.09 | 1555.01 |
| `fake-focused-final` | 312 | 1 | 0 | 0 | 406.21 | 1266.0 |

`unit-remaining-03` was interrupted by its execution guard after 19m10s, with its interrupt stack in Git-index copying in unchanged salvage code; the cause of the long runtime was not established. The post-pytest measurement runner was interrupted and reaped after the wrapper exited. The first queued `unit-retention-all` request was cancelled before pytest started so retention and fake files could share one bounded hold. Neither cancellation counts as a completed validation run. The queued broad `fake-unit-finish` request was also cancelled before pytest started to prioritize the final marker-removal focused hold. Broad runs preceded that follow-up; the final focused hold rechecked all affected launch, guardian, census, retention, probe and contract files.

Observed failures in the latest per-case runs:

- `tests/unit/test_daemon_qos_tools.py::test_thread_priorities_reads_every_thread_of_a_clamped_process` — PermissionError: [Errno 1] Operation not permitted: '/bin/ps'
- `tests/unit/test_descriptors.py::test_c16_6_the_kernel_ceiling_is_read_on_macos` — assert (False) + where False = isinstance(None, int)
- `tests/unit/test_doctor.py::test_daemon_lock_row_fails_on_a_lock_whose_process_is_gone` — AssertionError: assert ('unknown' == 'fail' - fail + unknown)
- `tests/unit/test_doctor.py::test_daemon_lock_row_passes_against_a_live_daemon` — AssertionError: assert 'unknown' == 'pass' - pass + unknown
- `tests/unit/test_hooks.py::test_the_hook_command_with_the_daemons_markers_spawns_nothing_and_surfaces_only_jobs` — PermissionError: [Errno 1] Operation not permitted: '/bin/ps'
- `tests/unit/test_importer.py::test_a_live_claude_process_outside_subfleet_keeps_history_out_and_fences_nothing[True]` — AssertionError: assert 'session-held-by-legacy-owner' == 'history' - history + session-held-by-legacy-owner
- `tests/unit/test_importer.py::test_states_map_from_rc` — AssertionError: assert 'running' == 'lost' - lost + running
- `tests/unit/test_importer.py::test_the_daemon_never_adopts_an_external_run` — AssertionError: assert ['20260905-10...0400-live/a1'] == ['20260905-100400-live/a1'] At index 0 diff: '20260905-100300-lost/a1' != '20260905-100400-live/a1' Left contains one more item: '20260905-100400-live/a1' Use -v to get more diff
- `tests/unit/test_review_pr127_service.py::test_open_is_not_queued_behind_worktree_and_diff` — TimeoutError

The native process/QoS/doctor/kernel-ceiling failures expose denied `/bin/ps`, sysctl or unavailable process identities. Importer liveness assertions also fail under conservative unknown identity; that connection is an inference until compared at the base. The one-second conversation-open timeout occurred in an unchanged test; its cause was not isolated. These failures were not masked or used to weaken production checks.

A baseline comparison of the eight process-inspection/importer failures was prepared but has not run. No baseline failure claim is made for those eight cases.

## Scratch measurement

This build lane supplies the measured workload: one Python development environment, dependency cache, recording-provider fixtures, contract checks and broad unit/fake runs. It is a build-lane sample, not a production survey. A foreground runner periodically walked its single `sf-tmp1` scratch tree without following symlinks, summing regular-file contents and `st_blocks * 512`. Pauses between walks were initially 5 seconds, then 15 seconds; walk time adds to that interval. Peaks are observed lower bounds. APFS clones can share blocks, so the block sum is not unique physical allocation. The measurement includes accumulated test fixtures and the offline baseline source copy.

Largest observed scratch sample: **1,430,910,989 bytes** of contents (1.431 GB), **1,556,500,480 bytes** by block sum (1.557 GB), during `fake-retention-all-measurement` (2 completed samples).

The configured default placement reservation is 1.5 GB (`subfleet/policy.py`). This is an anticipated-growth budget, not a directory quota. The observed block sum is a sizing input; periodic samples and shared APFS blocks prevent treating it as a guaranteed upper bound.

| Component / observation | Contents bytes | Block-sum bytes |
| --- | ---: | ---: |
| Initial development venv | 13,726,189 | 15,601,664 |
| Initial uv cache | 10,223,025 | 12,025,856 |
| Initial whole scratch | 35,334,717 | 40,517,632 |
| `focused-measurement` observed peak | 106,911,804 | 120,532,992 |
| `focused-rerun-measurement` observed peak | 178,336,166 | 200,171,520 |
| `contract-measurement` observed peak | 197,060,030 | 220,450,816 |
| `unit-01-measurement` observed peak | 446,182,642 | 489,115,648 |
| `fake-focused-final-measurement` observed peak | 476,708,882 | 547,033,088 |
| `unit-remaining-01-measurement` observed peak | 885,011,420 | 948,162,560 |
| `unit-remaining-02-measurement` observed peak | 1,354,000,544 | 1,473,609,728 |
| `fake-retention-all-measurement` observed peak | 1,430,910,989 | 1,556,500,480 |

The largest observed block sum exceeds the default 1.5 GB reservation by 56,500,480 bytes. The sample is a sizing input, not an upper bound. One provisional reservation covers anticipated combined growth; already allocated scratch reduces filesystem free space. Remaining scratch is counted in retention measurement; the existing configured volume/free-space policy is unchanged.

## Delivery and constraints

Setup deviation: the required uv setup sync downloaded dependencies before offline mode was enabled, contrary to the no-network rule. The default cache was unwritable, so setup used the permitted scratch cache. Subsequent uv invocations set `UV_OFFLINE=1`. This checkout was not fetched and no branch was pushed.

Commits use workspace-local `.git-tmp1` metadata because the shared Git index is unwritable. The implementation and regression steps, oldest first:

No further hold requests were started after 12:10 UTC. The existing final focused hold began at 12:09:48 UTC and finished with 312 passed and 1 skipped; all 58 new D-TMP1 cases passed. The controller stopped before requesting the prepared baseline comparison. Whole-directory validation remains incomplete: 184 modules are unrun and two are partial. Required pgrep returned 3 because global process listing is unavailable here; all recorded runner, wrapper and pytest PIDs were absent after completion. The earlier housekeeping pass completed and was reaped.

- `5753ebaf2` test: specify private attempt TMPDIR lifecycle (D-TMP1)
- `50f17eaa4` test: require Claude writable modes to include attempt scratch
- `8de7d98b8` fix: isolate provider attempt temporary storage (D-TMP1)
- `6a9c10c95` test: keep attempt scratch isolated after fixture undo
- `c87dc5672` test: pin temporary root baseline for the pytest session
- `4a69cda20` test: retain scratch when allocation inspection is denied
- `bfeb3f623` fix: distinguish missing scratch from failed inspection
- `07e6b5a5b` test: preserve census evidence and retention changes during cleanup
- `2b676e67b` fix: merge scratch measurements with current census evidence
- `3e2b73724` test: retain probe scratch until classification is finalized
- `c3807c604` fix: reclaim probe scratch only after finalization completes
- `0a8de118f` test: isolate attempt scratch beside per-test working directories
- `dea5c9e02` test: check probe transitions across scratch evidence records
- `80d8a62a7` test: reclaim evidenced scratch after its marker is removed
- `724b12cfd` fix: trust durable allocation identity after marker removal
- `eccc6d7d3` docs: record TMPDIR design and validation cutoff status

The final report/PR-body commit follows these steps. No push was performed. Scratch logs, JSON evidence, archives and bundles are not staged.

Completion check (2026-10-22 12:26 UTC): the prescribed scratch cleanup finished, and `sf-tmp1` is absent. Thirteen generated workspace cache directories were moved into scratch before removal; no `__pycache__`, `.pytest_cache` or `.hypothesis` remains in the worktree. The final required pgrep check returned 3; process listing is unavailable in this sandbox. All previously recorded runner, wrapper and pytest PIDs were absent, and every foreground command was reaped.

## Per-file results

| File | Passed | Skipped | Failed | Errors | Status |
| --- | ---: | ---: | ---: | ---: | --- |
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | Run |
| `tests/fake/test_admission_held_waiters.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_latency.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_lease_waiters.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_liveness.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_priority.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_ramp.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_ramp_retention.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_route_isolation.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_stateful.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_visibility.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_attempt_tmpdir_daemon.py` | 15 | 0 | 0 | 0 | Run |
| `tests/fake/test_batch_label.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity_fix2.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity_fix3.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity_fix4.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity_fix5.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_capacity_steer_daemon.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_capacity_wait_backoff.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_census_escapers_daemon.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_census_recycled_pid_daemon.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_codex_ca_bundle_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_codex_turn_completed_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_conversation_pools.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_conversation_wakes_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_crash_matrix.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_daemon_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_daemon_descriptors.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_daemon_launch.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_daemon_stacks.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_descriptor_exhaustion.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_detached_cwd_pins.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_detached_retention_fence.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_enrollment_guardian.py` | 0 | 1 | 0 | 0 | Run |
| `tests/fake/test_fake_provider.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_finalization_replay.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_folder_spelling.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_frozen_stream_nonregular.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_gate_end_to_end.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_identity_rollback.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_idle_cost.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_interactive_claude_titles.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_interactive_steer.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lane_fault.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lane_pilot.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lanes_enroll.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lanes_reenroll.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lanes_reenroll_keychain.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_launch_env_daemon.py` | 49 | 0 | 0 | 0 | Run |
| `tests/fake/test_launch_env_providers.py` | 6 | 0 | 0 | 0 | Run |
| `tests/fake/test_launch_gates_above_stdio.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lost_acknowledgement.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_main_refusal_write_dir.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_mcp_job_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_native_maintenance_startup.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_notice_delivery.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_notice_header.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_picker_native.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_policy_sandbox.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_probe_cancel.py` | 24 | 0 | 0 | 0 | Run |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | 0 | Run |
| `tests/fake/test_probe_turn.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_prompt_seam.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_provider_active_cap_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_provider_cap_review_evidence.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_provider_qos.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_record_secrecy.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_request_routing.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_reserve_authorization.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_reserve_authorization_validation.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_reset_credit_incident.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_resume_cli.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_resume_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 | Run |
| `tests/fake/test_review130_ab.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_review_r185_adversarial.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_route_view.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_routing_end_to_end.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_routing_tier_preferences.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_salvage_adversarial.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_salvage_finalization.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_sessions_end_to_end.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | Run |
| `tests/fake/test_state_fixture_processes.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_status_payload.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_suggestions_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_tier_hold_by_demand.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_timers_busy_admission.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_timers_end_to_end.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_listing.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_cwd.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_cwd_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_job_directory.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_job_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_quarantine.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_trees.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_wait_reasons.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_unadmittable_pin_admission.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_unadmittable_pin_rereview.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_unadmittable_pin_review.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_workspace_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_workspace_enospc.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_workspace_transient.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_writable_fanout.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_writer_retention_fence.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_guardian.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_lifecycle.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_mutations.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_retention.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_sandboxes.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_capacity_view.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_counted.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_procargs.py` | 52 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 | Run |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 0 | 0 | 0 | Run |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_adapter.py` | 170 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_cards.py` | 96 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_identity.py` | 30 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_stream.py` | 60 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_turn.py` | 61 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_usage.py` | 28 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_cli.py` | 222 | 0 | 0 | 0 | Run |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | Run |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_adapter.py` | 172 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_probe.py` | 55 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_shim.py` | 31 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_turn.py` | 58 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 | Run |
| `tests/unit/test_compat.py` | 1615 | 0 | 0 | 0 | Run |
| `tests/unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_history.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_service.py` | 98 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_store.py` | 32 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_titles.py` | 89 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_wakes.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_connections.py` | 31 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_integration.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_qos_tools.py` | 3 | 0 | 1 | 0 | Run |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_settle.py` | 61 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_verbs.py` | 15 | 5 | 0 | 0 | Run |
| `tests/unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 | 0 | 0 | Run |
| `tests/unit/test_descriptors.py` | 15 | 0 | 1 | 0 | Run |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 | Run |
| `tests/unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_disk_admission.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_disk_floor.py` | 60 | 0 | 0 | 0 | Run |
| `tests/unit/test_doctor.py` | 42 | 0 | 2 | 0 | Run |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 | 0 | Run |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_empty_provider_results.py` | 334 | 0 | 0 | 0 | Run |
| `tests/unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_failed_keep.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_filesystem_identity_agreement.py` | 37 | 3 | 0 | 0 | Run |
| `tests/unit/test_folders.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_forget_paced.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_fresh_until.py` | 1 | 0 | 0 | 0 | Run |
| `tests/unit/test_frozen_provider_streams.py` | 217 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_admission.py` | 24 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_merge.py` | 59 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_replay.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_revision.py` | 16 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_service.py` | 25 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_verdict.py` | 45 | 0 | 0 | 0 | Run |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_guard_trust.py` | 98 | 0 | 0 | 0 | Run |
| `tests/unit/test_guardian.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 | 0 | Run |
| `tests/unit/test_handler_refusals.py` | 317 | 0 | 0 | 0 | Run |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_hidden_environment.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_hook_cost.py` | 3 | 0 | 0 | 0 | Run |
| `tests/unit/test_hooks.py` | 66 | 0 | 1 | 0 | Run |
| `tests/unit/test_hooks_native_guard.py` | 18 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push.py` | 118 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_build.py` | 10 | 1 | 0 | 0 | Run |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_export.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_mutations.py` | 24 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_names.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_storage_retry_budget.py` | 60 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_validated_top.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_importer.py` | 145 | 0 | 3 | 0 | Run |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 | Run |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | Run |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 0 | 0 | 0 | Run |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_lanes_transfer.py` | 79 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_helpers.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_mutations.py` | 41 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_policy.py` | 26 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_round2.py` | 67 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_round3.py` | 67 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_round4.py` | 40 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_round5.py` | 39 | 0 | 0 | 0 | Run |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_legacy_hold.py` | 50 | 6 | 0 | 0 | Run |
| `tests/unit/test_limit_from_model_text.py` | 95 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard.py` | 324 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round10.py` | 60 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_state_root.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_lockwatch.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 | Run |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 | Run |
| `tests/unit/test_notice_rendering.py` | 21 | 0 | 0 | 0 | Run |
| `tests/unit/test_notify_push.py` | 31 | 0 | 0 | 0 | Run |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 | Run |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_operator_notices.py` | 79 | 0 | 0 | 0 | Run |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 | 0 | 0 | Run |
| `tests/unit/test_person_boundary.py` | 97 | 0 | 0 | 0 | Run |
| `tests/unit/test_person_boundary_identity.py` | 25 | 0 | 0 | 0 | Run |
| `tests/unit/test_picker.py` | 39 | 0 | 0 | 0 | Run |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy.py` | 178 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 | Run |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_procs.py` | 47 | 0 | 0 | 0 | Run |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 0 | 0 | Run |
| `tests/unit/test_provider_active_cap.py` | 50 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_217.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_restore.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 | Run |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 | Run |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 | Run |
| `tests/unit/test_relay.py` | 24 | 0 | 0 | 0 | Run |
| `tests/unit/test_render.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_render_quota_projection.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_demand.py` | 68 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_properties.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_resource_identity.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 | Run |
| `tests/unit/test_retention_archive.py` | 4 | 0 | 0 | 0 | Partial; interrupted |
| `tests/unit/test_retention_archive_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_case_alias.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_nested_leases.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_ordinary_trees.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_qos.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_readback.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_rediscovery.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_regenerable.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_regenerable_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_salvage_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_shared_folders.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_sweep_race.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_turn_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_turn_workdirs.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_turns.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_report.py` | 1 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_requests.py` | 17 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_service.py` | 7 | 0 | 1 | 0 | Run |
| `tests/unit/test_review_r2_repro.py` | 31 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r3_measure.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r5_repro.py` | 17 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 | 0 | 0 | Run |
| `tests/unit/test_route_check.py` | 24 | 0 | 0 | 0 | Run |
| `tests/unit/test_salvage.py` | 6 | 0 | 0 | 0 | Partial; interrupted |
| `tests/unit/test_salvage_optional_transient.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_salvage_unindexable.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_priority_callers.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_split.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_tier_preferences.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_turns.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_weekly.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scoped_stream_capacity.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scrub_key_order.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_cli.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_handoff.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_handoff_excerpts.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_headless_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_fifo.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_hot_health.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_inventory.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_load_gap.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_safety.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_write_churn.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_nudge.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_registry.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_revive.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_revive_reads.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_transcripts.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_settle_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_soak_tools.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_status_json.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_steer_invariants.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_stop_alarm.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_stop_watchdog.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_contention_queries.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_ids.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_migration_2.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_readers.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_snapshot_plans.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggest_cli.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggest_hint.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestion_error_boundaries.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestion_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestion_secret_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestion_structural_errors.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_r3_channels.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_review_boundaries.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_review_multiline.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_review_response.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | Run |
| `tests/unit/test_tick_prefilter.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_alerts.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_busy_read.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_daemon.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_keepalive.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_probe.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_reset_credits.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_retention.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_title_review_regressions.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_turn_attachment_replay.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_turn_overlap.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_turn_replay.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_turn_runner.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_ultracode_effort.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_unadmittable_pin.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_wait_hub.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_wake_cli.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_weekly_rank_vs_base.py` | 0 | 0 | 0 | 0 | Unrun |
