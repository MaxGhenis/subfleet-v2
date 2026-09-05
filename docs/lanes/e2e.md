# Lane brief: e2e (milestone 1 and 2 acceptance through the real CLI and daemon)

You are proving milestones 1 and 2 of subfleet v2 end to end in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/e2e`). Every component exists and has unit tests: the daemon (`subfleet/daemon.py`), the store, the guardian, the Codex and Claude adapters, the routing engine, and the CLI. What does not exist is a test that runs the real `subfleet` CLI against the real `subfleetd` with both adapters wired to fake provider executables. That is this lane. Bugs you find at the seams you fix, minimally, and list.

## Read first, in this order

1. `docs/acceptance-contract.md` section 21 (milestone acceptance rows for 1 and 2) and sections 6, 8, 9, 12, 15, 16, 17.
2. `tests/fake/conftest.py` and `tests/fake/run_daemon.py` (how the fake-adapter tests start a daemon), `tests/fake_adapter.py`, `tests/bin/codex`, `tests/bin/claude`, `tests/bin/fakeprov` (the fake providers and their `SUBFLEET_FAKE_SCENARIO` values), `tests/fixtures/codex/*/expected.json`, `tests/fixtures/claude/*/expected.json`.
3. `subfleet/credentials.py` (how a `Credential` becomes environment variables), `subfleet/adapters/registry.py`, `subfleet/policy.py` and `subfleet/default_policy.json`, `subfleet/cli.py` (verbs), `subfleet/client.py`.
4. `docs/lanes/reports/*-OUTPUT.md`: each lane's open questions for the integrator; several are seam questions this lane will answer by running the pieces together (for example whether the daemon publishes the Claude raw stream `stream.jsonl` as an artifact, and whether `Launch.notes` reaches `classify` and `attest` unchanged).

## Scope: files you own

- `tests/e2e/conftest.py`: a fixture that builds a temporary `SUBFLEET_HOME` under `/tmp` (AF_UNIX path limit) with: `policy.json` (copy of the default), lanes for `codex-1` and `codex-2` (each a temp `CODEX_HOME` containing a subscription-style `auth.json` the Codex adapter's `enroll` accepts) and `claude-1` and `claude-2` (keychain-token credentials); a `bin/` directory placed first on `PATH` with `codex` and `claude` symlinked to `tests/bin/codex` and `tests/bin/claude`; a running `subfleetd --foreground --state-root <home>` subprocess; and a `cli(*argv)` helper that runs `python -m subfleet.cli` with `SUBFLEET_HOME` set and returns rc, stdout, stderr. Credentials: if `subfleet/credentials.py` can only resolve a keychain item through `security`, add an additive credential kind `env` whose reference names an environment variable, use it for the fake lanes, and list it under "Seam changes"; never call `security` in tests.
- `tests/e2e/test_milestone1_codex.py`: `subfleet run -m astra -C <workdir> -p prompt.md -o out.md --wait` succeeds and prints the job id; `runs` lists it; `runs show <id> --out` prints the deliverable; the `-o` file equals the deliverable and was published by rename; the notice row exists for the caller session; `runs show <id> --json` shows `attestation` and the artifacts (deliverable, stdout, stderr, raw stream, launch); `kill` on a `slow` scenario ends `interrupted` with `killed_by`, and on a writable job with a dirty fake worktree writes a salvage ref; `--dry-run` and `--why` dispatch nothing; a `/tmp` workdir without `--allow-tmp` exits 7; a second `run` with the same `--request-id` returns the same job id; a differing payload with the same id exits 2; `daemon stop` then `runs`, `runs show`, `status` work offline and `run` exits 69 naming `subfleet daemon start`.
- `tests/e2e/test_milestone2_claude.py`: `run -m haiku` with the `success-allowed` scenario records two `provider` readings from the fake's `rate_limit_event` (fractions, ISO clocks) and `status` renders them as percentages; the `rejected-credits-fable` scenario on `claude-1` yields `limited` with the reported clock, a closure on the model scope, and the retry lands on `claude-2` with `claude-1` in the job's exclusions (`runs show --json`); `limit-no-clock` yields a `guessed` closure of now plus 3600 s; `model-downgrade` yields attestation `mismatch` and the served model in the row; `empty-result-rc0` is class `unknown`; no percentage is rendered for a lane with only `admission-observed` readings; `why <id>` prints the decision with the exclusion reason.
- `tests/e2e/test_guard_and_isolation.py`: with `subfleet/guard/TRUST` overridden to a wrong hash (copy the file, edit, point the daemon at it through whatever mechanism `subfleet/guard/preflight.py` exposes; add an env override if there is none and list it), a Codex `run` is refused with exit 7 and a message naming the fix; the fake providers record their environment, and the recorded environment has no `CODEX_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` and does have `SUBFLEET_ATTEMPT` and `SUBFLEET_JOB` (C-14.4 as far as fakes can prove it).
- `tests/e2e/test_recovery.py`: SIGKILL the daemon while a `slow` job runs, restart it, and the job finishes `succeeded` with one attempt and one notice; SIGKILL during `starting` recovers per C-4.2.
- Fixes to `subfleet/*` where a seam is broken. Keep each fix minimal and separate (one commit per fix, message naming the clause and the symptom). Do not restructure modules.

## Out of scope

New features, the routing engine's internals (a routing lane is finishing them on `lane/routing`; if `main` has not merged it yet, pin models with `-m` and skip `--task` cases), sessions, gates, timers, the importer.

## Acceptance for this lane

`uv run pytest -q tests/e2e` passes in under 90 s on this Mac with `/usr/sbin` and `/bin` reachable, and every test docstring cites a clause. Run with the sandbox's `ps` and `sysctl` available; if `sysctl` is not found, `export PATH=/usr/sbin:/sbin:$PATH`.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q tests/e2e
```

Standard library only at runtime. Commit after every coherent step with a message naming the clauses proved or the seam fixed. Your sandbox may have no network; if `git push` fails on DNS, do not retry; the integrator pushes `lane/e2e` from outside. Never commit to `main`, never force-push, never rewrite history. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Proved (clauses, one line each); Seam fixes (file, symptom, fix, commit); Seam changes (additive API you had to add); Not proved and why; Open questions for the integrator. No preamble.
