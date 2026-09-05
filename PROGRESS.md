# Importer lane progress

Lane: `lane/importer` — v1 state import, lane ownership transfer, shadow compare.
Brief: `docs/lanes/importer.md`. Specification: `docs/migration.md` (import manifest).

## State

Built, reviewed and green. `uv run pytest -q tests/unit/test_importer.py
tests/unit/test_lanes_transfer.py`: 79 passed in ~4 s. Full suite: 873 passed,
5 skipped.

## Done

- Read order 1-4 of the brief; the v1 formats are recorded below.
- `subfleet/importer.py`: `ImportReport`/`StoreReport`, the manifest as data, per-store cursors
  in `events` of kind `import.cursor`, one function per `import` row, `scan_unmanifested`,
  a snapshot dry run, `daemon.lock` held for the whole pass, `python -m subfleet.importer`.
- `subfleet/lanes_transfer.py` plus the `lanes` daemon op, two additive `LanesArgs` fields and
  two CLI flags: ownership flip, one `events` row, both rosters, a backup beside the v1 file,
  `--i-understand-v1-edit`, `--dry-run`, and a refusal while v1's launch agents can still
  reach a Codex home.
- `tools/compare_decisions.py` and `docs/shadow-diffs/README.md`.
- `tests/unit/test_importer.py` (55), `tests/unit/test_lanes_transfer.py` (24),
  `tests/live/test_import_dry_run.py` (opt-in, `SUBFLEET_LIVE=1`).
- Seam changes: `notices.job_id` nullable; `daemon._control` skips attempts flagged
  `imported_external`; `LanesArgs` gained `dry_run` and `confirm_v1_edit`.
- A 12-agent adversarial review of the lane against the manifest and the contract, and its
  confirmed findings fixed: external runs stay v1's, cursors keep what they did not finish,
  no v1 WAL database is opened in place, the compare script replays the caller's request
  rather than v1's answer.

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
