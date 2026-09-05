# Lane brief: core (daemon, store, guardian, containment, salvage, recovery)

You are building the core of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/core`). subfleet v2 is one supervised daemon that dispatches delegated agent work across several Claude and Codex subscription accounts and keeps every run as a durable row it owns.

## Read first, in this order

1. `docs/acceptance-contract.md`: the binding specification. Cite its clauses (`C-x.y`) in every test docstring.
2. `subfleet/contracts.py`, `subfleet/store_schema.sql`, `subfleet/protocol.py`, `subfleet/adapters/base.py`: the seams shared with the other lanes. Do not change their names or semantics. If one blocks you, make the smallest additive change and list it in your final message under "Seam changes".
3. `docs/plan.md` for the reasoning; `docs/plan-b-rev4.md` sections "Core model", "Architecture", "Notices and waiting" for the design narrative.
4. v1, read-only, for behaviour to preserve (never modify v1, never run its commands): `~/chief-of-staff/subfleet/subfleet/run_ledger.py` (ledger, kill_run, salvage refs), `~/chief-of-staff/subfleet/bin/subfleet-claude` lines 300 to 440 (setsid, salvage(), on_exit trap), `~/chief-of-staff/subfleet/subfleet/notify.py` (notice rows), `~/chief-of-staff/subfleet/subfleet/paths.py`. Never run `grep -r` or `rg` over `~/chief-of-staff/state` or any broad root; read specific files.

## Scope: files you own

- `subfleet/store.py`: `Store(path)` opening SQLite per C-3.1, applying `store_schema.sql`, recording `schema_version`; a `transaction()` context manager; typed CRUD for lanes, jobs, attempts, artifacts, notices, decisions, leases, closures, readings, events. Every state change writes an `events` row in the same transaction (C-3.2). Read-only open mode for other processes (C-3.4). Refuse a newer schema (C-3.5).
- `subfleet/ids.py`: job id per C-1.1, attempt id per C-1.2, payload digest per C-6.2 (canonical JSON, SHA-256), request id generation per C-1.5.
- `subfleet/procs.py`: boot id via `sysctl -n kern.boottime`, process start via `ps -p <pid> -o lstart=`, `same_process(pid, boot_id, proc_start)` (C-5.3), containment enumeration returning a `Containment` result with the three pid sets and an `unverifiable` flag (C-5.5), `signal_group(pgid, sig)` guarded by identity (C-5.4). Treat state `Z` as not live.
- `subfleet/guardian.py` with console entry `subfleet-guardian` (C-5.1, C-5.2): `setsid`, write `start.json`, spawn the provider with stdout and stderr redirected and stdin from the prompt file when given, wait, write `exit.json`, exit with the child's rc. Receipts via temp file and rename. The guardian receives argv and paths; secrets arrive only in its environment and pass through to the child.
- `subfleet/salvage.py` (C-13.1 to C-13.3): temporary index, `commit-tree` with the baseline as parent, `refs/subfleet-salvage/<branch>-<utc>-a<seq>`, tree-hash comparison against the baseline, refuse a writable job whose workdir is on `main` or `master`.
- `subfleet/policy.py`: `load_policy(path)` and `policy_hash(path)` for C-11.1, plus `subfleet/default_policy.json` written from plan B rev 4's "Routing as data" example with the caps from C-6.4. Full chain evaluation and comparators (C-11.2 to C-11.6) belong to a later lane; you need only the model map, the caps, and a minimal pick: pinned lane, else the first eligible lane of the model's provider (enabled, `owner: v2`, not `desktop` unless allowed, not excluded, no active closure for `account` or the model id, free slot).
- `subfleet/adapters/registry.py`: `get_adapter(provider) -> Adapter`, importing `subfleet.adapters.codex` and `subfleet.adapters.claude` lazily, and `register(provider, factory)` so tests inject a fake adapter.
- `subfleet/credentials.py`: resolve a `Credential` to the environment variables the adapter expects (`CODEX_HOME` for a home; `CLAUDE_CODE_OAUTH_TOKEN` from `security find-generic-password -s <ref> -w` for a keychain token). The value never enters logs, rows, or `manifest.json` (C-10.5).
- `subfleet/daemon.py` with console entry `subfleetd`: singleton lock (C-5.8); socket server per C-16 on a thread pool that never blocks on processes or `ps` (C-16.4); handlers for `submit` (C-6.1 to C-6.7, including the headless marker and write template prepend), `list`, `show`, `wait` (server-side long poll capped at 60 s), `kill` (C-7), `lanes`, `readings`, `why` (returns the stored decision), `notice.pending`, `notice.ack`, `ping`, `daemon.status`; the control loop that admits attempts (C-6.3), spawns them through the guardian, watches receipts, finalizes (classify, attest, deliverable, artifacts with fsync-rename per C-8.1, salvage, export, notice in one transaction with the terminal state per C-4.3 and C-15.1), retries per C-4.5, and enforces `max_wall_s`. Recovery on start per the C-4.2 table, including containment and quarantine. `--foreground` and `--state-root` flags; logging to `daemon.log`.
- `subfleet/retention.py`: the maintenance pass per C-8.4, outside transactions.
- `tests/bin/fakeprov`: a Python fake provider that reads `SUBFLEET_FAKE_SCENARIO` (`ok`, `slow`, `rc4-limit-with-clock`, `rc1-crash-after-output`, `nested-setsid`, `ignore-sigterm`, `spawn-fail` via a missing executable) and `SUBFLEET_FAKE_DELAY_S`; `nested-setsid` spawns a `setsid` grandchild that sleeps 30 s and writes its pid to `$SUBFLEET_FAKE_MARKER` (C-12.8).
- `tests/fake_adapter.py`: an `Adapter` that launches `tests/bin/fakeprov`, classifies rc 0 as `ok`, rc 4 as `limited` with the clock the fake prints, and returns the fake's stdout as the deliverable.
- Tests under `tests/unit/`, `tests/process/`, `tests/fake/` per C-20.1, within the budgets of C-20.2.

## Out of scope (other lanes own these)

`subfleet/cli.py`, `subfleet/client.py`, `subfleet/offline.py` (CLI lane); `subfleet/adapters/codex.py`, `subfleet/adapters/claude.py`, `subfleet/guard/`, provider fixtures (adapter lanes); `docs/invariants.md`. Do not create them. The daemon must run end to end with `tests/fake_adapter.py` alone.

## Acceptance for this lane (milestone 1 core rows in C-21)

Write these as named tests and make them pass:

- `C-6.2`: repeated request id with the same digest returns the same job id; a different digest is rejected with code 2.
- `C-6.3`: two concurrent submits for a one-slot lane admit exactly one attempt; the other waits with `wait_reason: capacity`.
- `C-5.2`, `C-4.2 running`: a fake job survives the submitting client disconnecting; the daemon SIGKILLed during `running` re-adopts the attempt on restart and finalizes it with the right rc.
- `C-4.2 starting`: daemon SIGKILLed before `start.json` exists recovers per the table (receipt appears later: `running`; never appears: release and retry).
- `C-5.6`: `kill` on the `ignore-sigterm` scenario escalates, verifies containment, finalizes with `killed_by`.
- `C-5.6`, `C-5.5`: `kill` on `nested-setsid` ends `quarantined`, not released, and `show` prints the surviving pid; `kill --force-release` records the override event.
- `C-7.2`: cancel committed before acceptance leaves the job `cancelled` and the attempt `interrupted` even when the provider then exits 0; acceptance first makes `kill` report already finished.
- `C-7.3`: cancelling a parent cancels its non-independent children.
- `C-8.1`, `C-8.3`: deliverable and `-o` export published by temp-fsync-rename; an export to an unwritable directory leaves the job `succeeded` with `export_error`.
- `C-13.1`, `C-13.2`: salvage on a temp repo writes the ref and leaves HEAD, index, and worktree untouched; a writable job on `main` is refused with code 7.
- `C-4.3`, `C-15.1`: terminal state and notice row land in one transaction (assert by crashing between them is impossible: test that a SIGKILL right after the terminal state still shows the notice on restart).
- `C-5.8`: a second daemon exits 69; a stale lock from a dead pid is taken over.
- `C-20.3`: a crash-matrix test parametrised over the boundaries in C-4.2 plus export and notice, using a hook that SIGKILLs the daemon at the boundary.
- `C-16.1`: a malformed request line gets an error response with code 2 and does not kill the connection handler.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step with a message naming the clauses implemented, and push after every commit: `git push -u origin lane/core`. Never commit to `main`, never force-push, never rewrite history. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built (files with one line each); Tests (the exact command and the pass count, and the wall time); Clauses covered; Clauses not covered and why; Seam changes; Open questions for the integrator. No preamble.
