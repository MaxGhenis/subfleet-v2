## Built

- `subfleet/policy.py`: validates policy keys with file/key errors, resolves retired aliases with a note, and hashes exact file bytes.
- `subfleet/capacity.py`: latest labeled readings, staleness, active closures, attempt counts, desktop refresh, ownership filtering, and reset ordering.
- `subfleet/scheduler.py`: upward chains, fixed pins, provider comparators, rejection evidence, slot caps, FIFO, parent bounds, and probe decisions.
- `subfleet/render.py`: labeled status tables and readable recorded decision walks.
- `subfleet/daemon.py`: atomic evaluator integration, persisted waiting decisions, full status, and guardian-supervised probes with recovery and quarantine.
- `tests/unit/test_policy.py`: policy validation, alias, and hash cases.
- `tests/unit/test_capacity_view.py`: evidence, desktop, ownership, staleness, and attempt-count cases.
- `tests/unit/test_scheduler.py`: named golden routing cases and admission regressions.
- `tests/unit/test_render.py`: provenance-safe percentages, stale markers, waterfall ordering, and decision rendering.
- `tests/fake/test_routing_end_to_end.py`: submission, promotion, pins, recorded `why`, status, probes, cancellation, and FIFO integration.
- `tests/fake/test_probe_recovery.py`: launch-gate ordering, recovery, containment, exception handling, and quarantine.
- `PROGRESS.md`: committed state/done/next tracking from the start.
- `OUTPUT.md`: this integrator report.

Implementation is committed on `lane/routing`; integration commit: `f73e5ab`.

## Tests

Environment:

```sh
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" UV_OFFLINE=1
```

Focused command:

```sh
uv run --no-sync pytest -q tests/unit/test_policy.py tests/unit/test_policy_support.py tests/unit/test_capacity_view.py tests/unit/test_scheduler.py tests/unit/test_render.py tests/fake/test_routing_end_to_end.py tests/fake/test_probe_recovery.py
```

**181 passed, 2 skipped; wall time 3.41 s.**

Full command:

```sh
uv run --no-sync pytest -q
```

**848 passed, 81 failed, 39 skipped; wall time 35.43 s.** The 81 failing test IDs exactly match the starting-tree baseline at `b209f09`: 74 CLI socket tests, 5 daemon-verb tests, and 2 offline process-identity tests. There are no newly failing IDs. The isolated baseline run had 677 passes, 81 failures, and 37 skips in 22.38 s.

The prescribed `uv sync --group dev` could not complete offline because its build cache lacks `hatchling`. Existing v2 dependency cache contents were copied into this lane; `uv sync --group dev --no-install-project` installed the test dependencies. No network or push was required. All new test docstrings cite clauses; `git diff --check` passes.

## Clauses covered

C-6.3–6.4, C-9.1, active-closure handling under C-9.6, C-10.3–10.4 desktop/ownership filtering, C-11.1–11.6, and evaluation exit semantics under C-17.3. Integration also exercises C-3.3, C-4.1, C-4.5–4.6, C-5.1–5.7, C-7.2, C-8.4, C-10.5, and C-19.1 with mocked process ownership where required. Plan amendments 11, 14, and 15 are reflected in routing, FIFO, and evidence rendering.

## Clauses not covered and why

- Real-process portions of C-5 and C-11.4–11.6 require an unrestricted run: this sandbox denies process inspection and Unix socket binding. The actual guardian-probe and socket/CLI routing tests remain present and skip here; mocked supervision and in-process routing pass.
- Classifier and enrollment internals under C-9.2–9.8 and C-10.2 remain with the adapter lane. Adapters, guardian implementation, CLI, sessions, gates, and timers were not modified.
- The existing CLI prints the promotion and chosen route; adoption of the richer server-rendered evidence walk remains a CLI integration seam.

## Seam changes

- No schema or shared-contract changes. Decision evaluation JSON adds candidate details, every rejection reason, capacity readings, capacity blocks, and the existing CLI's `rejected` alias.
- Optional `caps.max_active_attempts_per_parent` defaults to **1**. Loaded policy dictionaries carry `_policy_hash`, `_policy_path`, and default `headroom_floor`.
- Probes use `probe.state` events, unique `probe:<token>` lease holders, and temporary `lanes/<lane>/probes/<token>/` receipts. Completed receipts are removed; quarantined evidence and leases remain.
- Capacity snapshots expose `unavailable_lanes` and `reserved_probes` separately from attempt-based in-flight counts. `daemon.status` returns the full view plus rendered `status`; `why` adds rendered `text`.

## Open questions for the integrator

- Confirm the configurable per-parent concurrency default of 1 during integration.
- Have the CLI consume `why.text` and `daemon.status.status` for full evidence rendering.
- Rerun the full suite with Unix sockets and process inspection permitted before accepting the real-process milestone gates. Probe-specific operator force-release remains outside this lane's CLI scope; safe recovery releases only after verified containment.

No guard or hook rejection was bypassed. The reported syscall restrictions were left intact.
