# Routing lane progress

## State
Milestone 3 routing implementation complete and committed on `lane/routing`; final report published in `OUTPUT.md`.

## Done
- Confirmed the routing worktree and clean starting tree.
- Read acceptance clauses C-6.3–6.4, C-9, C-10.3–10.4, C-11, and milestone 3.
- Located the minimal pick, atomic decision persistence, store evidence APIs, and `why` endpoint.
- Finished the required plan, audit, and read-only v1 references; no v1 commands executed.
- Committed policy validation/aliases/hash (`0aca56c`), capacity view (`db96462`), scheduler (`9ed073c`, `be38387`), and rendering (`35716d0`).
- Policy/support tests: 81 passed. Capacity/render/scheduler tests: 73 passed.
- Initial in-process routing integration: 10 passed; socket end-to-end case skipped because process inspection is sandbox-restricted.
- Integrated the evaluator with atomic attempt/decision reservation, persisted capacity-wait explanations, and full status snapshots.
- Added requested-model read-only probes through gated guardians, durable probe identities/receipts, verified containment, restart recovery, and quarantine. Completed probe files are removed; uncertain probes retain their lease and evidence.
- Fixed promoted transient retries to preserve the model, FIFO overtaking during recheck waits, unsupported-provider lane pins, missing capacity evidence, and null admission reset clocks.
- Final focused suite: 181 passed, 2 skipped in 3.41 s.
- Final full suite: 848 passed, 81 failed, 39 skipped in 35.43 s. The 81 failing test IDs exactly match the initial-tree baseline (677 passed, 81 failed, 37 skipped in 22.38 s): 74 CLI socket cases, 5 daemon-verb cases, 2 offline process-identity cases. No new failing IDs.
- All six new test files have clause citations in every test docstring; `git diff --check` passes.

## Next
- Integrator: rerun the full suite with process inspection and Unix sockets available, including real guardian probe and socket/CLI routing cases.
- Integrator: adopt the richer `why.text` / `daemon.status.status` renderings in the CLI when that lane is integrated.
- Integrator: use `OUTPUT.md` for final commands, measurements, coverage, and seams.

## Seams and assumptions
- Keep the existing Decision dataclass and schema; additional scheduling facts belong in evaluation JSON.
- No output path was supplied, so the final report will be committed as `OUTPUT.md`.
- Optional `caps.max_active_attempts_per_parent` defaults to 1; descendant attempts share the bound.
- Loaded policy metadata: `_policy_hash`, `_policy_path`, default `headroom_floor`.
- Decision evaluations add `candidate_details`, all rejection reasons, `capacity_readings`, `capacity_blocks`, and the existing CLI's `rejected` alias. Store schema and shared contracts are unchanged.
- Probe supervision uses `probe.state` events, unique `probe:<token>` lease holders, and temporary `lanes/<lane>/probes/<token>/` receipts; `unavailable_lanes` and `reserved_probes` keep probe reservations separate from attempt counts.
- The offline sync lacked dependency and build caches. Copied the existing v2 dependency cache into this lane, then installed with `uv sync --group dev --no-install-project`; tests use `uv run --no-sync` because `hatchling` remains unavailable offline.
- Existing CLI daemon-verb tests hit sandbox-denied Unix socket binding/process inspection (5 failures in a targeted 28-case run); no guard or hook was bypassed.
