# Tier preferences, 2026-10-09

Task chains accept a short model name or a non-empty preference list at each tier. `flatten_chain` slices from the job's tier before flattening, preserves order, and removes duplicates by first occurrence. Earlier models in a tier win whenever any of their lanes admits the job. Pins retain their existing behavior. C-11.1, C-11.2 and the version-3 changes entry specify the semantics. The default policy is unchanged; live `~/.subfleet` was neither read nor changed.

## Reader audit

| Changed reader | Location | Purpose |
| --- | --- | --- |
| Validation | `subfleet/policy.py:326` | Reject empty lists, unknown/retired aliases, repeated names and malformed members; preserve previous bad-input diagnostics. |
| Shared candidate construction | `subfleet/policy.py:25` | The single flattening function. |
| Pin provider | `subfleet/scheduler.py:176` | First eligible preference, including MCP filtering. |
| Demand models | `subfleet/scheduler.py:362` | Admission competition and probe demand. |
| Higher model scopes | `subfleet/scheduler.py:496` | Stranding calculations; preserve legacy models repeated after the current model by slicing before deduplication. |
| Route preparation | `subfleet/scheduler.py:588` | Evaluation, promotion, upward fallback, unadmittable checks and reservation rechecks. |
| Submit provider/model | `subfleet/daemon.py:1950` | Validate submission against the first eligible preference. |
| Conversation model defaults | `subfleet/conversations/service.py:345` | Read every preference in the hard-tier entry. |

All production `chains` reads were audited with `rg`. Other reads check task membership or validate policy structure. `evaluate` and `unadmittable` use `prepare`; `refused_for_good` reads evaluated models. Reservation rechecks use `prepare` too. Probe paths use `demand_models`, `pin_provider`, or evaluated decisions. `subfleet why` (`subfleet/cli.py:1191`, `subfleet/daemon.py:2861`) renders the recorded flattened decision. Human status (`subfleet/cli.py:337`) and JSON status (`subfleet/status_json.py:211`) render job/attempt rows, with no raw policy entry assumptions.

## Invariants and tests

| Invariant | Coverage |
| --- | --- |
| String-only candidates and full routing decisions equal release/217 | `tests/unit/test_scheduler_tier_preferences.py::test_string_chains_match_release217_candidates_and_routed_choice`: 300 generated policies/jobs, including tier permutations, policies without model priorities, pins, caps, exclusions and turns; also compares model scopes. The three chain-dependent evaluation functions are frozen verbatim from `6805af2dedd7` in `tests/fixtures/tier_preferences/release_217_chain_paths.py.txt`; unchanged lane-judging helpers are shared. |
| Upward-only, unique, first-preference order | `test_flattening_is_upward_unique_and_preserves_first_preference`: 300 generated mixed chains/tiers, checking preparation, evaluation and demand as well as flattening. `test_preference_examples_and_pins` adds examples, including repeated names across tiers. |
| Closed first model falls to the next preference before a higher tier | `test_closed_first_model_uses_next_in_same_tier_before_higher_tier`: 300 cases with 1–3 lanes per provider. `tests/fake/test_routing_tier_preferences.py` covers easy, standard and hard admission, durable decisions, why and status. |
| Valid mixed policies load; invalid lists fail | `tests/unit/test_policy_tier_preferences.py`: 250 valid mixes, 80 generated invalid lists, 12 invalid-input examples and both the unchanged default and supplied live chain shape. No live policy access. |
| Every indirect reader remains consistent | Scheduler examples cover standing refusals, MCP filtering and legacy stranding. 150 generated reservation rechecks compare complete decisions with fresh routing. Fake routing covers pins, probe reach and an actual admission probe on the same-tier Opus fallback; `test_codex_default_reads_preference_list_in_hard_tier` covers model defaults. |

## Mutations

Each mutant was applied to production source and tested in its own serial pytest process. Source and bytecode were restored afterward.

| Mutation | Killing test | Result |
| --- | --- | --- |
| Flatten without dedupe | `test_preference_examples_and_pins` | Killed |
| Flatten from tier 0 | `test_preference_examples_and_pins` | Killed |
| Ignore within-tier order (sort each list) | `test_preference_examples_and_pins` | Killed |
| Accept an empty list | `test_invalid_entries_keep_existing_diagnostics` | Killed |

## Verification and delivery

Final sweep: **410 passed, 2 skipped** plus one existing wall-clock assertion failure in `test_c6_4_second_unmeasured_job_waits_with_persisted_reason`: the recheck and current time both read `2026-10-09T12:26:17Z`. The assertion now compares with the admission start, since the recheck may become due while a slow pass finishes; its state and no-slot assertions are unchanged. The corrected focused rerun passed (**1 passed in 8.30 s**), for **411 passed and 2 skipped** across these final checks. The two skips require macOS boot/process identity, unavailable under this sandbox (`sysctl`/`ps`). All pytest runs are targeted and serial, without `-n`. Dependencies were installed with `uv sync --locked` (CPython 3.14.7, pytest 9.1.1, Hypothesis 6.168.1). The frozen baseline functions were checked against the commit with AST source extraction. The unchanged default policy has SHA-256 `ffb2b1fa834f0c07be03544428852f17c7628a1c8cb88d581a090e2cba8701e6`. The fresh test directory is under Darwin's user temp directory, outside HOME, with no component named `tmp`; it is removed after verification.

The workspace's shared git metadata is outside its writable roots. Delivery uses `.git-local`, branch `feat/tier-preferences`, with the required Co-Authored-By trailer on each commit. The verified bundle is `docs/reports/2026-10-09-tier-prefs.bundle`, with prerequisite `6805af2dedd760c2cd087e765ff9d9c016aebd47`. The bundle names `refs/heads/feat/tier-preferences`; its exact final report-inclusive head is listed by `git bundle list-heads` and in the delivery message. No push is performed.

Implementation commit: `bae70885b6e3d591ca3ad1bb90b6c8ddedda4aba`. Admission timing-test correction: `9a3a115c3a52aac5d8aaa00a35ea0f946d56c7b4`. This report is committed as its own final step. Each commit has `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` as its last message line.
