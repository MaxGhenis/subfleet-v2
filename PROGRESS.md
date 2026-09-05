# Routing lane progress

## State
Milestone 3 engine modules implemented and committed on `lane/routing`; daemon integration under verification.

## Done
- Confirmed the routing worktree and clean starting tree.
- Read acceptance clauses C-6.3–6.4, C-9, C-10.3–10.4, C-11, and milestone 3.
- Located the minimal pick, atomic decision persistence, store evidence APIs, and `why` endpoint.
- Finished the required plan, audit, and read-only v1 references; no v1 commands executed.
- Committed policy validation/aliases/hash (`0aca56c`), capacity view (`db96462`), scheduler (`9ed073c`, `be38387`), and rendering (`35716d0`).
- Policy/support tests: 81 passed. Capacity/render/scheduler tests: 73 passed.
- Initial in-process routing integration: 10 passed; socket end-to-end case skipped because process inspection is sandbox-restricted.

## Next
- Complete supervised admission probes and restart handling; a review caught the need for durable probe identity before releasing its launch gate.
- Verify FIFO waiting behavior and the same-model retry after promotion.
- Run routing and full test suites; compare sandbox failures with the starting tree.
- Commit each coherent step and write the final integrator report to `OUTPUT.md`.

## Seams and assumptions
- Keep the existing Decision dataclass and schema; additional scheduling facts belong in evaluation JSON.
- No output path was supplied, so the final report will be committed as `OUTPUT.md`.
- Optional `caps.max_active_attempts_per_parent` defaults to 1; descendant attempts share the bound.
- Loaded policy metadata: `_policy_hash`, `_policy_path`, default `headroom_floor`.
- The offline sync lacked dependency and build caches. Copied the existing v2 dependency cache into this lane, then installed with `uv sync --group dev --no-install-project`; tests use `uv run --no-sync` because `hatchling` remains unavailable offline.
- Existing CLI daemon-verb tests hit sandbox-denied Unix socket binding/process inspection (5 failures in a targeted 28-case run); no guard or hook was bypassed.
