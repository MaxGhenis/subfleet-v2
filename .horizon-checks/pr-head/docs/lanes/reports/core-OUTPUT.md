## Built

- `subfleet/store.py`: WAL/FULL SQLite, audited transactions, version checks, read-only access and typed CRUD.
- `subfleet/ids.py`: local-time job IDs, attempt IDs, UUID request IDs and canonical payload digests.
- `subfleet/procs.py`: boot/start identity checks, three-source containment and guarded signals.
- `subfleet/guardian.py`: detached provider supervision, durable receipts and a commit-before-launch pipe gate.
- `subfleet/salvage.py`: replayable temporary-index snapshots preserving HEAD, index and worktree.
- `subfleet/policy.py`: validated policy data and minimal eligible-lane selection.
- `subfleet/default_policy.json`: plan B model map, routing data and contract caps.
- `subfleet/credentials.py`: environment-only home/keychain credential resolution.
- `subfleet/adapters/registry.py`: lazy real adapters and registered fake factories.
- `subfleet/daemon.py`: singleton socket service, admission, recovery, cancellation, quarantine, retries, wall limits, frozen finalization, atomic acceptance/notices, serialized exports and retention scheduling.
- `subfleet/retention.py`: bounded pruning with evidence pins and leased removal of owned allocated worktrees.
- `tests/bin/fakeprov`: success, delay, limit, crash, ignored-TERM and escaped-session scenarios.
- `tests/fake_adapter.py`: injectable adapter with preamble-safe scenario settings.
- `tests/fake/test_crash_matrix.py`: SIGKILL hooks across reserved, starting, running, finalizing, terminal, notice, export and salvage boundaries.
- `tests/fake/test_daemon_contract.py`: named physical core acceptance tests, including writable kill-and-salvage.
- `tests/fake/test_state_contract.py`: deterministic admission, cancellation, lease, publication, retry and quarantine checks.
- `tests/fake/test_daemon_launch.py`: launch path, credential isolation and identity-failure checks without launching providers.
- `tests/fake/test_finalization_replay.py`: frozen adapter evidence, stale-attempt fencing and disk-full recovery.
- `tests/unit/`: store, digest, policy, credential, guardian, containment, salvage, retention and guard-interface checks.
- `tests/process/test_guardian_process.py`: physical guardian and containment checks with explicit capability skips.
- `uv.lock`: reproducible development dependencies, resolved from existing cached distributions.
- `PROGRESS.md`: committed state/done/next, with parallel-work details in `PROGRESS_STORE.md`, `PROGRESS_PROCESS.md` and `PROGRESS_FAKE.md`.

## Tests

```sh
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

**119 passed, 33 skipped; pytest wall time 3.33 seconds.** All 111 test functions have clause citations; `git diff --check` passed. Runtime remains standard-library-only.

The deterministic suite is within every C-20.2 time budget. Physical process-test budgets cannot be established while those cases are skipped.

## Clauses covered

Implemented and tested portions of C-1, C-2, C-3.1–C-3.5, C-4.1–C-4.6, C-5.1–C-5.8, C-6.1–C-6.7, C-7.1–C-7.4, C-8.1–C-8.4, C-9.6, C-10.1/C-10.5, C-11.1 and minimal C-11.2 eligibility, C-12.1/C-12.8, C-13.1–C-13.4, C-14.2/C-14.3 integration, C-15.1/C-15.3/C-15.4, C-16.1–C-16.4 and C-20.1/C-20.3/C-20.5. Process and crash behavior has deterministic coverage plus physical tests awaiting execution; milestone 1 physical acceptance is not claimed.

## Clauses not covered and why

- Physical C-4.2/C-5/C-20.3 acceptance: 29 real-daemon tests and four guardian/process tests skip because the sandbox denies `ps` and `sysctl kern.boottime`. Production inspection remains fail-closed. No permission bypass was attempted.
- Full C-11.2–C-11.6 routing/comparators/probes, real provider enrollment/classification/attestation, guard trust/isolation execution, and CLI/offline behavior belong to other lanes. The daemon consumes the Codex lane's preflight API; this interface is unit-tested here.
- C-6.4's optional `max_tokens_observed` is persisted but not enforced: the adapter seam currently provides no standardized token observations.
- C-15.2 hook/socket-push delivery and later timers/actions/release-soak gates are outside this milestone's core implementation. `ping` currently provides a socket liveness/echo response.
- Every required push failed with `Could not resolve host: github.com`. All work is committed locally on `lane/core`.

## Seam changes

None. `contracts.py`, `store_schema.sql`, `protocol.py` and `adapters/base.py` are unchanged. Other lanes' files and v1 were not modified.

## Open questions for the integrator

- Run all 33 skipped physical cases with permitted process inspection before accepting milestone 1.
- Push `lane/core` when DNS works, then integrate provider, guard and CLI lanes.
- Establish a token-observation field before enabling the optional observed-token cap.
- Retention conservatively pins salvage until a supplied callback proves the ref is retained elsewhere; decide which integration supplies that proof.
