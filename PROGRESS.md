# Importer lane progress

Lane: `lane/importer` — v1 state import, lane ownership transfer, shadow compare.
Brief: `docs/lanes/importer.md`. Specification: `docs/migration.md` (import manifest).

## State

Read the manifest, the binding contract sections (C-1, C-2, C-3, C-9, C-10, C-11, C-15,
C-18, C-19, C-20), plan amendments 8 and 12, the v2 seams (`store.py`,
`store_schema.sql`, `contracts.py`, `policy.py`, `cli.py`, `client.py`, `daemon.py`,
`protocol.py`), and every v1 format the manifest names (read-only, individual files;
the newest 20 `runs/*/meta.json` for the run schema). Implementing.

## Done

- Read order 1–4 of the brief complete; v1 formats recorded below.
- `PROGRESS.md` created (this file).

## Next

1. `subfleet/importer.py`: report dataclasses, cursors, one function per `import` row.
2. `subfleet lanes transfer` (daemon op + thin CLI, both rosters, backup, `--dry-run`).
3. `tools/compare_decisions.py` (shadow-week diff, never shipped in the package).
4. `tests/unit/test_importer.py`, `tests/unit/test_lanes_transfer.py`.
5. Opt-in `SUBFLEET_LIVE=1` dry run against the real v1 state.

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
