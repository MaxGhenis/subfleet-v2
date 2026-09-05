# Lane brief: routing (policy evaluation, comparators, capacity view, `why`, `status` rendering)

You are building the routing engine of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/routing`). subfleet v2 is one supervised daemon that dispatches delegated agent work across several Claude and Codex subscription accounts. Routing is data (`policy.json`) evaluated by one engine that answers three questions per lane and model: does authentication work, can this model accept a request now, and how much quota remains. This is milestone 3. The core daemon (`subfleet/store.py`, `subfleet/daemon.py`, `subfleet/policy.py` minimal) already exists on `main`; you replace its minimal pick with the full engine.

## Read first, in this order

1. `docs/acceptance-contract.md` sections 6.3, 6.4, 9, 10.3, 10.4, 11, and milestone 3 in section 21. Cite clauses in every test docstring.
2. `subfleet/contracts.py` (`Reading`, `Closure`, `Decision`, `ReadingLabel`, defaults), `subfleet/policy.py` and `subfleet/default_policy.json` as the core lane left them, `subfleet/store.py` (read the public methods for readings, closures, leases, lanes), `subfleet/daemon.py` (find where the minimal pick is called; you will replace that call with `scheduler.evaluate`).
3. `docs/plan-b-rev4.md` sections "Capacity truth", "Ordering when capacity is unknown", "Classification precedence", "Routing as data"; `docs/plan.md` amendments 11, 14, 15.
4. `docs/reports/D-surface.md` (the surface audit, for golden cases) and `docs/plan-b-rev4.md` "What I watched it do this morning" (the 06:31 to 06:33 log that C-11.6 turns into a test).
5. v1, read-only (never modify, never run its commands): `~/chief-of-staff/subfleet/subfleet/delegate.py` lines 420 to 600 (`_blind_lane_filter`, `_capacity_candidates`, the sort key at 568 to 584) and 640 to 700 (`select_semantic_model`, `_model_capacity_state`), `~/chief-of-staff/subfleet/subfleet/capacity.py` lines 1180 to 1330 (`_claude_rows`, `_best_dispatchable`, `_codex_dispatch_score`), `~/chief-of-staff/subfleet/README.md` lines 90 to 130 and 540 to 560 (documented ordering rules). Never run `grep -r` or `rg` over `~/chief-of-staff/state` or any broad root.

## Scope: files you own

- `subfleet/policy.py` (extend, keep the core lane's functions): schema validation of `policy.json` per C-11.1 with an error naming file and key; `retired` alias resolution with a stderr note; `policy_hash`.
- `subfleet/capacity.py`: the capacity view over the store: latest reading per (lane, scope, window) with its label, `stale-provider` when older than `READING_TTL_S`, active closures, in-flight counts from `attempts` (never from `ps`), `desktop` flags refreshed from `~/.claude.json` `oauthAccount` (C-10.3), `owner` filter (C-10.4). Pure functions over rows so tests need no daemon.
- `subfleet/scheduler.py`: `evaluate(policy, view, job) -> Decision` per C-11.2 to C-11.5: walk the chain upward from the tier; candidates per model with every rejection reason recorded (`excluded`, `desktop`, `owner-v1`, `closed:<scope>:<until>`, `no-slot`, `disabled`, `below-floor`); comparators per C-11.3 (Codex: weekly reset ascending then lane id, in-flight never reorders; Claude: worst-window headroom descending, then in-flight, then lane id); unmeasured lanes after measured ones as "eligible but unmeasured"; pins evaluate one model or one lane and never fall back; `probe_required(decision, job)` per C-11.4 (writable or tier `hard` on an unmeasured lane). Anti-starvation per plan amendment 11: FIFO within tier, one concurrency bound per parent, `waiting` jobs carry reason and `next_check_at`.
- `subfleet/render.py`: the `status` table (lanes, latest readings with labels, closures, running jobs) and the `why` output (the decision as a readable walk). A percentage is rendered only from a `provider` or `stale-provider` reading and a stale one is marked; `admission-observed`, `local-backoff`, and `unknown` render as words (C-9.1, plan amendment 15). Codex weekly-reset waterfall shown as the primary order.
- Wire-up: replace the daemon's minimal pick with `scheduler.evaluate`, persist the `Decision` in `decisions` per attempt, serve `why` from it, and run the probe job when `probe_required`. Keep the change to `daemon.py` small and commented.
- Tests: `tests/unit/test_policy.py` (validation errors name the key; hash stable), `tests/unit/test_capacity_view.py` (staleness, closures, in-flight from attempts), `tests/unit/test_scheduler.py` (golden cases below), `tests/unit/test_render.py` (no percentage without a provider reading; stale marked), `tests/fake/test_routing_end_to_end.py` (a submitted `--task research --tier standard` job with the desktop lane excluded and every Opus lane closed lands on Astra with the recorded reason; `why` prints it).

## Golden cases (write each as a named test)

- **C-11.6**: lanes as at 06:33 on 2026-09-05: desktop account excluded by `-x`; the only Opus-eligible lanes are blind (unmeasured) with one in-flight attempt each and `max_in_flight_unmeasured` 1; Astra lane measured with headroom. Expected: `opus: no candidate lanes after exclusions; promoted` and chosen model `astra`.
- Pinned `-m opus` in the same state: `Exit.NO_LANE` with the earliest reset in the reason; no promotion.
- Pinned `-a max@rules.foundation`: that lane or `NO_LANE`; never another lane.
- Codex: two lanes at 60% and 40% weekly, resets tomorrow and in six days: the fuller lane resetting tomorrow wins (C-11.3).
- Codex above the floor on any window is ineligible even with a soon reset.
- Claude: model-scoped closure on `claude-fable-5-1` leaves Opus eligible on the same lane; account-scoped closure removes both.
- `owner: v1` lane never a candidate; `desktop` lane a candidate only with `allow_desktop`.
- Unmeasured lane takes one slot; a second job waits with `wait_reason: capacity` and a `next_check_at`.
- `retired: {"sol": "astra"}` resolves with a note; an unknown model is exit 2 naming the key.
- Fable-only chains (`authored-prose`, `strategy`, `adjudication`) never leave the Claude provider.

## Out of scope

Adapters, the guardian, the CLI, sessions, gates, timers. Do not change `store_schema.sql`; if you need a column, record it as a seam change and use `decision_json` in the meantime.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step with a message naming the clauses implemented. Your sandbox has no network, so do not try to push; the integrator pushes `lane/routing` from outside when you finish. Never commit to `main`, never force-push, never rewrite history. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built (files with one line each); Tests (exact command, pass count, wall time); Clauses covered; Clauses not covered and why; Seam changes; Open questions for the integrator. No preamble.
