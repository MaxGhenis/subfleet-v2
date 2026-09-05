## Built

- `subfleet/adapters/codex.py`: standard-library subscription enrollment/probe, launch/resume builders, classifier, deliverable capture, and attempt-aware model attestation.
- `subfleet/contracts.py`: existing additive `Launch.lane_id` field retained to identify closure lanes.
- `subfleet/guard/never-rules-hook.sh`: byte-for-byte v1 hook; SHA-256 `a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db`.
- `subfleet/guard/TRUST`: pinned hook bytes, Codex 0.153.3, normalized hook identity hash, and exact v1 override.
- `subfleet/guard/preflight.py`: bounded scratch-home trust preflight; code 7 and a fix on file, version, hash, override, RPC, or workdir mismatch.
- `subfleet/guard/__init__.py`: guard package.
- `tests/fixtures/codex/`: twelve redacted cases with streams, rc, expected outcomes, and documented provenance.
- `tests/bin/codex`: Python replay provider with delay, final-message output, and detached-grandchild scenario.
- `tests/unit/test_codex_adapter.py`: 68 cases for classification, launch/resume, deliverables, and model attestation.
- `tests/unit/test_codex_probe.py`: 50 offline HTTP/enrollment cases, including weekly-only windows and identity fallbacks.
- `tests/unit/test_guard_trust.py`: 35 integrity, override-parity, and fake preflight cases.
- `tests/process/test_codex_isolation.py`: 28 replay, environment, redaction, and detached-child cases.
- `uv.lock`: development dependency lock restored from the prior attempt; runtime dependencies remain empty.
- `PROGRESS.md`: committed state/done/next record maintained throughout the resumed work.
- `OUTPUT.md`: final integrator report.

## Tests

Environment:

```sh
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
```

`uv sync --group dev` succeeded. Final acceptance command:

```sh
uv run pytest -q tests/unit/test_codex_adapter.py tests/unit/test_codex_probe.py tests/unit/test_guard_trust.py tests/process/test_codex_isolation.py
```

**181 passed in 3.70 s; measured wall time 3.86 s** (`/usr/bin/time -p`). These four files are all current test modules in this worktree: 153 unit and 28 process cases. Even the combined run is below both C-20.2 limits. `git diff --check` and an AST audit of every test function's clause docstring passed.

The restored baseline passed 97 tests. New regressions demonstrated failures before fixes for classification evidence, policy aliases on resume, historic attestation, and split identity claims. No real Codex execution, live HTTP probe, or v1 command ran.

## Clauses covered

- C-1.4, C-1.6, C-1.7: account identity, policy alias separation, UTC clocks.
- C-9.1–C-9.7: adapter reading labels, authentication/admission/quota precedence, raw rc/signal, scoped closures, reported/guessed clocks, transients, duration-based windows. C-9.6 coverage is closure construction.
- C-10.2, C-10.5: API-key/free-plan refusal, usage probing, environment-only credentials.
- C-12.1–C-12.3, C-12.5–C-12.8: provider-independent adapter interface, launch/resume, model evidence, final text, corpus, and fake provider.
- C-14.1, C-14.2, Codex portion of C-14.3: exact hook copy and override; fake-validated fail-closed preflight; writable launches require an override.
- Environment/argument portions of C-14.4; applicable layout, time budgets, and docstring rules in C-20.1, C-20.2, C-20.5.

## Clauses not covered and why

- C-14.2 installed CLI trust measurement and doctor/daemon wiring: provider execution was prohibited, and those integration modules belong to other lanes. Preflight is implemented and validated against fake app-server responses.
- C-14.4 actual filesystem write enforcement: the fake receives sandbox flags but implements no OS sandbox. The process tests prove API-key removal and attempt-directory stream capture, not the complete write-isolation matrix.
- C-9.6 persistence, extension, and expiration; C-4.5 retry scheduling; Claude behavior and broader release/crash gates: outside this adapter lane's ownership.

## Fixture provenance

Real: `success`, from v1 run `20260905-111854-fix-06-lane-g-r2`. Its rc, thread id, and redacted final deliverable were independently checked against the source. JSONL envelopes are normalized because v1 did not request JSON output.

Synthetic: `limit-with-clock`, `limit-no-clock`, `model-scoped-limit`, `credits-rejection`, `auth-401`, `refresh-token-revoked`, `cli-too-old`, `content-filter`, `stream-disconnect`, `model-at-capacity`, `spawn-fail`. Every expected file marks this explicitly. The recorded search of the newest 150 v1 directories found 29 Codex-family runs and no qualifying provider failure; metadata used `family`, not `provider`. Tokens, cookies, authorization values, and personal absolute paths are absent; account emails are retained. The saved wham test payload is also explicitly synthetic.

## Seam changes

- Retained the prior attempt's optional final field `Launch.lane_id: str | None = None`; builders populate it so limited outcomes identify their lane. No existing field names or method signatures changed.
- Raw rc/signal remain in `Outcome.evidence`. Preflight exposes `PreflightResult` and optional testable home/workdir/path/deadline arguments without changing `preflight(codex_bin)` usage.
- Native resume attestation reads existing `start.json.started_at` and `exit.json.finished_at`; it returns `unattested` when clocks are missing or model evidence falls in ambiguous boundary seconds. No receipt schema change.
- Enrollment readings initially have an empty lane id because lane allocation follows enrollment; the integrator must bind them to the allocated lane.

## Open questions for the integrator

- Wire preflight with the actual lane home/workdir before executable jobs and supply its checked override. Record the installed CLI trust and full OS sandbox measurements before cutover.
- Preserve `Launch.lane_id` when persisting/reconstructing launches, preserve guardian receipt clocks, and strip all three provider API-key variables at the process boundary before applying the adapter's removals.
- Remote delivery is blocked: every resumed commit was followed by `git push -u origin lane/codex-adapter`, and each failed with `Could not resolve host: github.com`. Push the retained local commits once networking is available. No guard/hook refusal occurred; no bypass was attempted.
