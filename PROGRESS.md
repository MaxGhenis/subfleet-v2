# Importer lane progress

Lane: `lane/importer` — v1 state import, lane ownership transfer, shadow compare.
Brief: `docs/lanes/importer.md`. Specification: `docs/migration.md` (import manifest).

## State

Built and green. `uv run pytest -q tests/unit/test_importer.py tests/unit/test_lanes_transfer.py`:
63 passed in ~9 s. Full suite: 856 passed, 5 skipped (one pre-existing timing flake in
`tests/fake/test_daemon_contract.py::test_c7_2_...`, which passes in isolation).

## Done

- Read order 1-4 of the brief; the v1 formats are recorded below.
- `subfleet/importer.py`: `ImportReport`/`StoreReport`, the manifest as data, per-store cursors
  in `events` of kind `import.cursor`, one function per `import` row, `scan_unmanifested`,
  a snapshot dry run, a refusal while a daemon holds `daemon.lock`, and `python -m subfleet.importer`.
- `subfleet/lanes_transfer.py` plus the `lanes` daemon op, two additive `LanesArgs` fields and
  two CLI flags: ownership flip, one `events` row, both rosters, a backup beside the v1 file,
  `--i-understand-v1-edit`, `--dry-run`.
- `tools/compare_decisions.py` and `docs/shadow-diffs/README.md`.
- `tests/unit/test_importer.py` (46), `tests/unit/test_lanes_transfer.py` (17),
  `tests/live/test_import_dry_run.py` (opt-in, `SUBFLEET_LIVE=1`).
- Seam change: `notices.job_id` is nullable, because the v1 outbox holds session messages that
  name a session and no run.
- C-3.3 pass: every v1 read, every `ps` call, every digest and every file publication happens
  outside the transaction that records the rows.

## Next

- The integrator runs the opt-in dry run against the real v1 state.

## v1 formats read (2026-09-05, read-only)

| Path | Shape |
|---|---|
| `~/chief-of-staff/subfleet/claude-accounts.json` | `{_comment, enrolled: {email: keychain item}, accounts: [email]}` |
| `~/chief-of-staff/subfleet/codex-accounts.json` | `{_comment, protected_account: {email, account_id}, auto_reset}`; homes are `~/.codex-<n>`, not listed |
| `~/.codex-<n>/auth.json` | `{auth_mode, OPENAI_API_KEY, tokens: {…, account_id}, last_refresh}` |
| `~/.claude.json` | `oauthAccount.emailAddress` names the desktop account |
| `S/runs/<id>/meta.json` | `id, family, model, lane, workdir, git_head_before/after, rc, started_at, finished_at, duration_s, original_out_path, out_path, session_id, transcript_path, codex_thread_id, codex_home, rollout_path, resumed_from, salvage_refs, routing_decision, caller{session_id,pid,…}, pid, launcher, notify` (+ `adopted_at`, `_source_paths`) |
| `S/notices/<session>.jsonl` | `{push{…}, pushed, rc, run_id, surfaced, surfaced_at, text, ts}` per line |
| `S/outbox.sqlite3` | `messages(sequence, message_id, session_id, request_digest, payload_digest, payload, status, created_at, updated_at, receipt)`; observed status `finished` with a non-empty receipt |
| `S/capacity-live-cache.json` | `{probed_at, accounts: [{family, id, email, five_hour{used_percent,reset_at,confidence}, weekly{…}, scoped_limits, learned_capacity, account_id, …}], roster_fingerprint}` |
| `S/claude-oauth-raw.json` | `{checked_at, raw: {five_hour{utilization,resets_at}, seven_day{…}, seven_day_<model>, limits: [{kind, percent, resets_at, scope{model{id,display_name}}}], …}}`; utilization is a percent 0–100 |
| `S/keepalive.json` | `{schema_version, updated_at, last_run{…}, lanes: {email: {last_outcome, last_checked_at, last_attempt_at, last_opened_at, auth_failed_at?, auth_code?}}}` |
| `S/reset-policy.json` | `{last_redeemed_at, lane, email, credit_id, last_redemptions: {home: iso}}` |
| `S/alerts.json` | `{latch key: {active, last_sent}}`, 152 keys |
| `S/native-workers.json` | `{"claude:<uuid>": {pid, broker_pid, account}}` |
| `S/tickles/<session>.json` | `{session_id, at, last_uuid, turn_uuid, restart_stubs, delivered, push{…}}` |
| `S/integration-events.salt` | 32 bytes |
| `D/cooldowns.json` | `{home path or email: {"*" or model id: iso until}}` |
| `D/decisions.jsonl` | one JSON object per line: `class, cmd, lane/home, model, overrides, signals matched, ts` (+ `task, tier, family, requested_model, routing_*, result, capacity` on recent lines) |
