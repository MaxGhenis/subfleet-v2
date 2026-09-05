# Lane brief: codex-adapter (Codex adapter, classifier, guard prerequisites, fixtures)

You are building the Codex provider adapter for subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/codex-adapter`). subfleet v2 is one supervised daemon that dispatches delegated agent work across several Claude and Codex subscription accounts. Adapters translate between subfleet's vocabulary and one provider CLI; they return data and never touch the store or spawn the provider themselves.

## Read first, in this order

1. `docs/acceptance-contract.md`, especially sections 9 (classification), 10 (lanes and credentials), 12 (adapters), 14 (guard prerequisites), 20 (tests). Cite clauses (`C-x.y`) in every test docstring.
2. `subfleet/contracts.py` and `subfleet/adapters/base.py`: the interface you implement. Do not change names or semantics; if something blocks you, make the smallest additive change and list it under "Seam changes" in your final message.
3. v1, read-only, for the behaviour to port (never modify v1, never run its commands, never launch `codex` yourself): `~/chief-of-staff/subfleet/bin/subfleet-codex` (argument construction, sandbox, `-c hooks=` override, usage-limit and content-filter regexes, transient classes, salvage branch refusal), `~/chief-of-staff/subfleet/subfleet/codex.py` (`probe_wham`, `api_key_login`, `classify_windows`, `scan_rollout_signals`, reset credits), `~/chief-of-staff/subfleet/bin/subfleet-guard-hook` and `~/chief-of-staff/subfleet/bin/subfleet-guard` (the never-rules hook and its trust preflight), `~/chief-of-staff/subfleet/docs/guard.md`, `~/chief-of-staff/subfleet/tests/test_guard.py` and `test_guard_never_rules.py` (what parity means). Never run `grep -r` or `rg` over `~/chief-of-staff/state` or any broad root; read specific files.
4. Codex CLI reference for the installed version (codex-cli 0.153.3): `docs/reference/codex-exec-help.txt` and `docs/reference/codex-help.txt` are the captured `--help` outputs. Do not run `codex exec` yourself in any form; `codex --version` is the only codex command you may run. The flags you rely on are `--json`, `--output-last-message`, `--sandbox`, `-m`, `-c`, and `codex exec resume`.

## Scope: files you own

- `subfleet/adapters/codex.py`: class `CodexAdapter(Adapter)` with `provider = "codex"`.
  - `enroll` (C-10.2, C-1.4): read `<home>/auth.json`; refuse an API-key login (port `api_key_login`) and a free plan with `AdapterError(code=7, fix=...)`; derive the account key from the token claims when present, else the email; probe usage.
  - `probe` (C-9.7): port `probe_wham` using only the standard library (`urllib.request`), classify windows by duration into `five_hour`, `seven_day`, or the minute count; return `provider` readings with utilization as a fraction in [0, 1] and `resets_at` in ISO 8601 UTC; on network failure return an empty tuple (no exception) and let the caller keep older readings.
  - `build_launch` (C-12.3): `codex exec --json -m <model id>`, `-c model_reasoning_effort=<effort>` when given, `--sandbox <read-only|workspace-write>`, `-c hooks=<guard override>` when `guard_override` is given, `--output-last-message <attempt dir>/last.md`, prompt on stdin (`stdin_path`), `cwd` = job workdir, `env_add` = `credential_env` (which carries `CODEX_HOME`) plus `SUBFLEET_ATTEMPT` and `SUBFLEET_JOB` set by the daemon, `env_remove` = `("CODEX_API_KEY", "OPENAI_API_KEY")`, `raw_stream_path` = `<attempt dir>/stream.jsonl` (the daemon redirects stdout there).
  - `classify` (C-9.2 to C-9.7): parse the JSONL stream for `thread.started` (thread id into `native_session_id`), `turn.failed` and `error` events, and stderr for v1's regexes; precedence authentication, admission, quota; `limited` carries scope, clock, and evidence and returns a `Closure`; `transient` for 5xx, stream disconnects, "model at capacity"; `content-filter` never retried; `cli-too-old`; `unknown` when rc is 0 but no deliverable exists. Keep the raw rc beside the class.
  - `attest` (C-12.5): served model from the rollout under `<home>/sessions/**` located by thread id; `unattested` when not found; `mismatch` when a different model served.
  - `deliverable` (C-12.6): `last.md` when present and non-empty, else the last `item.completed` agent message from the stream.
  - `resume_launch`: `codex exec resume <thread id>` on the same home with the same sandbox and guard override.
- `subfleet/guard/never-rules-hook.sh`: byte-for-byte copy of v1's `bin/subfleet-guard-hook`. `subfleet/guard/TRUST`: the pinned SHA-256 of that file plus the Codex hooks trust hash and override string v1's preflight checks. `subfleet/guard/preflight.py`: `preflight(codex_bin) -> PreflightResult` porting `subfleet-guard preflight` (C-14.2), and `override_string(hook_path) -> str` producing the `-c hooks=...` value exactly as v1 builds it.
- `tests/fixtures/codex/<case>/{stdout,stderr,rc,expected.json}` (C-12.7): build the corpus from real, redacted v1 artifacts. Sources: the newest run directories under `~/chief-of-staff/state/subfleet/runs/` whose `meta.json` has `"provider": "codex"` (list with `ls -t ~/chief-of-staff/state/subfleet/runs | head -150`, then read each `meta.json`, `err.log`, and `raw.jsonl` or equivalent individually). Redact tokens, cookies, `Authorization` values, and absolute prompt text; keep account emails. Required cases: `success`, `limit-with-clock`, `limit-no-clock`, `credits-rejection`, `auth-401`, `refresh-token-revoked`, `cli-too-old`, `content-filter`, `stream-disconnect`, `model-at-capacity`, `spawn-fail`. Where no real artifact exists, synthesise one and mark `"synthetic": true` in `expected.json`.
- `tests/bin/codex`: a Python fake that reads `SUBFLEET_FAKE_SCENARIO` and replays the matching fixture's stdout and stderr and exits with its rc, honouring `--output-last-message` and `SUBFLEET_FAKE_DELAY_S` (C-12.8).
- `tests/unit/test_codex_adapter.py`, `tests/unit/test_codex_probe.py` (with a stubbed HTTP layer), `tests/unit/test_guard_trust.py` (C-14.1: SHA-256 of the copied hook equals `TRUST`; C-14.2: preflight refuses a mismatched hash), `tests/process/test_codex_isolation.py` (C-14.4 with the fake: the child sees no `CODEX_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`, and stdout lands in the attempt directory).

## Out of scope

`subfleet/store.py`, `subfleet/daemon.py`, `subfleet/guardian.py`, `subfleet/procs.py`, `subfleet/cli.py`, `subfleet/adapters/claude.py`, `subfleet/adapters/registry.py`. Another lane builds them concurrently. Your adapter must be testable without them: tests call the adapter methods on fixture directories directly.

## Acceptance for this lane

- Every fixture case classifies to its `expected.json` (class, scope, clock source, closure presence, native session id).
- The experiment-0 style rejection payload for Codex (a `turn.failed` with a usage-limit message and a reset time) yields `limited` with `clock_source: reported`.
- `enroll` refuses an API-key `auth.json` and a free plan with code 7 and a fix line.
- `probe` on a saved wham payload returns two `provider` readings with fractions and ISO clocks; on a payload with only a weekly window returns one, keyed by duration (C-9.7).
- Guard trust tests pass; the override string equals what v1 builds for the same hook path.
- `uv run pytest -q tests/unit/test_codex_adapter.py tests/unit/test_codex_probe.py tests/unit/test_guard_trust.py tests/process/test_codex_isolation.py` passes within the budgets of C-20.2.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step with a message naming the clauses implemented, and push after every commit: `git push -u origin lane/codex-adapter`. Never commit to `main`, never force-push, never rewrite history. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built (files with one line each); Tests (exact command, pass count, wall time); Clauses covered; Clauses not covered and why; Fixture provenance (which cases are real, which synthetic); Seam changes; Open questions for the integrator. No preamble.
