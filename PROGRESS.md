# PROGRESS — lane/claude-adapter

Milestone 2: the Claude provider adapter, the rate-limit stream sensor, attestation,
and the fixture corpus. Clause numbers are `docs/acceptance-contract.md`.

## State

Reading and reconnaissance complete; implementation starting.

## Done

- Read `docs/acceptance-contract.md` (C-6.7, §9, §10, C-12.4..C-12.8, C-14.3, §20, §21),
  `subfleet/contracts.py`, `subfleet/adapters/base.py`, `docs/reports/experiment-0-rate-limit-event.md`,
  `docs/reference/claude-help.txt`, `docs/reference/VERSIONS.md`.
- Read v1 (read-only): `bin/subfleet-claude` (auth probe, `prefer_transcript_text`,
  `model_matches_requested`, `check_model`, the launch line and `PERM_ARGS`, the
  text classifier), `subfleet/claude.py` (`keychain_credentials`, `probe_oauth_usage`,
  `transcript_limit_events`), `subfleet/capacity.py` (`record_lane_run`,
  `_resolve_transcript`), `subfleet/util.py` (`parse_reset_clock`), `subfleet/delegate.py`
  (preambles).
- Established the `~/.claude/projects/` directory encoding empirically: `/`, `.` and `_`
  each become `-`, case preserved (verified against a transcript's own `cwd` field).
- Recovered the authoritative stream-json event schemas from the installed
  Claude Code 2.1.260 binary's embedded validators (`system/init`, `assistant`,
  `rate_limit_event` + `rate_limit_info`, `result` success and error variants,
  `system/api_retry`, and the assistant/result `error` enum).

## Next

1. `subfleet/adapters/claude_stream.py` — pure stream-json parser.
2. Fixtures under `tests/fixtures/claude/`.
3. `subfleet/adapters/claude.py` — the adapter.
4. `tests/bin/claude` — the fake provider.
5. Unit, process, and live tests.
