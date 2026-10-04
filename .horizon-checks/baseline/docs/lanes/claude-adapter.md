# Lane brief: claude-adapter (Claude adapter, rate-limit stream sensor, attestation, fixtures)

You are building the Claude provider adapter for subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/claude-adapter`). subfleet v2 is one supervised daemon that dispatches delegated agent work across several Claude and Codex subscription accounts. Adapters translate between subfleet's vocabulary and one provider CLI; they return data and never touch the store or spawn the provider themselves. This adapter is milestone 2: it makes Claude lane capacity knowable by reading the `rate_limit_event` every headless run emits.

## Read first, in this order

1. `docs/acceptance-contract.md`, especially C-6.7, section 9 (classification and C-9.8), 10, 12 (C-12.4 to C-12.8), 14.3, 20, and milestone 2 in section 21. Cite clauses in every test docstring.
2. `subfleet/contracts.py` and `subfleet/adapters/base.py`: the interface you implement. Do not change names or semantics; if something blocks you, make the smallest additive change and list it under "Seam changes".
3. `docs/reports/experiment-0-rate-limit-event.md`: four real `rate_limit_event` payloads (two allowed, one rejected with `credits_required`, one allowed on an account the old table called exhausted). These are your first fixtures.
4. `docs/reference/claude-help.txt` (installed Claude Code 2.1.260 flags) and `docs/reference/VERSIONS.md`.
5. v1, read-only (never modify v1, never run its commands): `~/chief-of-staff/subfleet/bin/subfleet-claude` lines 480 to 640 (auth probe, transcript lookup by session uuid, `model_matches_requested`, `prefer_transcript_text`) and 780 to 840 (the launch line, `PERM_ARGS` per sandbox, headless block), `~/chief-of-staff/subfleet/subfleet/claude.py` (`probe_oauth_usage`, `transcript_limit_events`, `keychain_credentials`), `~/chief-of-staff/subfleet/subfleet/delegate.py` lines 95 to 130 (the headless and write preambles). Never run `grep -r` or `rg` over `~/chief-of-staff/state`, `~/.claude`, or any broad root; read specific files. Do not run `claude -p` yourself except the single live smoke test gated by `SUBFLEET_LIVE=1`, which you do not set.

## Scope: files you own

- `subfleet/adapters/claude.py`: class `ClaudeAdapter(Adapter)` with `provider = "claude"`.
  - Credential resolution: the daemon passes `credential_env` holding `CLAUDE_CODE_OAUTH_TOKEN` (keychain token lane) or `CLAUDE_CONFIG_DIR` (home lane). `enroll(credential)` receives only the reference; resolve a keychain token with `security find-generic-password -s <ref> -w` in a private helper, never log it, and for a home run under `CLAUDE_CONFIG_DIR`.
  - `enroll` (C-10.2): one Haiku turn, `claude -p "Reply with exactly: ok" --model claude-haiku-4-5-20251001 --output-format stream-json --verbose --max-turns 1`, read the `rate_limit_event` and the `system/init` event; account key is the email the daemon supplies in the credential reference (`claude-quota-<email>`), verified against the transcript's account when available; refuse when `system/init` never arrives (auth) with `AdapterError(code=5)`.
  - `probe` (C-9.8, C-11.4): the same Haiku turn, or the model the caller passes; returns `provider` readings for `five_hour` and `seven_day` with utilization as the fraction the event carries and `resets_at` converted from epoch seconds to ISO 8601 UTC; `status: rejected` returns an `admission-observed` reading for the requested model with no utilization and the event's `resetsAt` as the clock. Provide `probe_with_model(lane, credential_env, model_id)` as well.
  - `build_launch` (C-12.4, C-6.7): `claude -p --model <model id> --session-id <uuid4> --output-format stream-json --verbose`, permission flags per sandbox exactly as v1 builds them, prompt on stdin from `<attempt dir>/prompt.sent.md` (headless block prepended unless the marker `<!-- subfleet:headless -->` is present; write the file with temp-and-rename), `cwd` = job workdir, `env_add` = `credential_env` plus nothing else (the daemon adds `SUBFLEET_ATTEMPT` and `SUBFLEET_JOB`), `env_remove` = `("ANTHROPIC_API_KEY",)`, `raw_stream_path` = `<attempt dir>/stream.jsonl`, `native_session_id` = the uuid chosen. Record the transcript path you expect (`~/.claude/projects/<encoded workdir>/<uuid>.jsonl`, port v1's encoding) and its size at launch as `transcript_offset` in the Launch's notes for a resume.
  - `classify` (C-9.2 to C-9.5, C-9.8): authentication first (`system/init` present means the credential works; a 401 or organisation-block text means `auth-dead`); admission second (`rate_limit_event.status`); quota third (the windows). Map `credits_required` to `limited` with scope the requested model and reason `credits`; a session or weekly limit text with a reset clock to `limited` scope `account`; "out of usage credits. Switch to another model" to `limited` scope model. `transient` for 5xx, stream drops, overloaded. `cli-too-old` on the 400 version message. Return the readings the event yields and a `Closure` for `limited`. Keep the raw rc beside the class. Parse `overageStatus` independently and never treat it as admission evidence.
  - `attest` (C-12.5): locate the transcript by session uuid (exactly one match, else `unattested`), read assistant messages after the recorded offset, compare their `model` fields with the requested id; all equal is `attested`, any different is `mismatch` with the served model, none is `unattested`.
  - `deliverable` (C-12.6): the final assistant text within the attempt's transcript range (v1's `prefer_transcript_text` rule), else the `result` event's text from the stream; empty with rc 0 is class `unknown`.
  - `resume_launch`: `claude -p --resume <session id>` on the same lane with the same permission flags and model.
- `subfleet/adapters/claude_stream.py`: a pure parser for stream-json lines (`system/init`, `assistant`, `rate_limit_event`, `result`, `system/api_retry`), tolerant of unknown event types and truncated last lines, returning a typed summary the adapter uses. Unit-test it on every fixture.
- `tests/fixtures/claude/<case>/{stdout,stderr,rc,expected.json}` (C-12.7): from the experiment-0 payloads (wrap each in a minimal stream: `system/init`, `assistant`, `rate_limit_event`, `result`) and from real redacted v1 artifacts (newest 150 run directories under `~/chief-of-staff/state/subfleet/runs/` whose `meta.json` says `"provider": "claude"`; list with `ls -t | head -150`, read `meta.json`, `err.log`, and the raw output individually). Required cases: `success-allowed`, `rejected-credits-fable`, `allowed-on-table-exhausted-lane`, `limit-session-with-clock`, `limit-weekly-with-clock`, `limit-no-clock`, `auth-401`, `org-block`, `cli-too-old`, `stream-disconnect`, `model-downgrade` (assistant messages served by a different model than requested), `empty-result-rc0`. Redact tokens, cookies, `Authorization` values, and prompt text; keep emails. Mark synthetic cases `"synthetic": true`.
- `tests/bin/claude`: a Python fake that reads `SUBFLEET_FAKE_SCENARIO`, replays the matching fixture's stdout and stderr, exits with its rc, honours `--session-id` by writing a minimal transcript under a `CLAUDE_FAKE_PROJECTS_DIR` when set (so attestation tests run offline), and honours `SUBFLEET_FAKE_DELAY_S`.
- Tests: `tests/unit/test_claude_stream.py`, `tests/unit/test_claude_adapter.py` (every fixture to its `expected.json`), `tests/unit/test_claude_attest.py` (attested, mismatch, unattested, two-match ambiguity), `tests/process/test_claude_isolation.py` (C-14.4 with the fake: no `ANTHROPIC_API_KEY` reaches the child; stdout in the attempt dir), `tests/live/test_claude_live.py` skipped unless `SUBFLEET_LIVE=1`.

## Out of scope

`subfleet/store.py`, `subfleet/daemon.py`, `subfleet/procs.py`, `subfleet/credentials.py`, `subfleet/adapters/codex.py`, `subfleet/adapters/registry.py`, `subfleet/cli.py`, the scheduler. Other lanes build them. Your adapter must be testable without them: tests call adapter methods on fixture directories directly.

## Acceptance for this lane (milestone 2 rows in C-21)

- All four experiment-0 payloads parse: the three allowed ones to two `provider` readings each with fractions and ISO clocks; the rejected one to `limited`, scope `claude-fable-5-1`, reason `credits`, `clock_source: reported`, and no utilization reading.
- `model-downgrade` yields attestation `mismatch` naming the served model; a transcript that cannot be found yields `unattested`; two candidate transcripts yield `unattested` with the ambiguity in the evidence.
- No code path renders a percentage: readings carry fractions and labels only (rendering is the routing lane's job, and it must find nothing else to render).
- A limit fixture produces a `Closure` whose `until_at` equals the event's clock; a no-clock fixture produces `clock_source: guessed` at now plus 3600 s.
- `build_launch` output for each sandbox equals v1's argument order for the same inputs (write a test that reconstructs v1's line from `bin/subfleet-claude` and compares).
- `uv run pytest -q tests/unit/test_claude_stream.py tests/unit/test_claude_adapter.py tests/unit/test_claude_attest.py tests/process/test_claude_isolation.py` passes within the budgets of C-20.2.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step with a message naming the clauses implemented, and push after every commit: `git push -u origin lane/claude-adapter`. Never commit to `main`, never force-push, never rewrite history. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built (files with one line each); Tests (exact command, pass count, wall time); Clauses covered; Clauses not covered and why; Fixture provenance (real versus synthetic); Seam changes; Open questions for the integrator. No preamble.
