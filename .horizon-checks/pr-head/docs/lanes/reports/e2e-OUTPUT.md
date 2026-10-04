## Proved

These are deterministic seam checks and adapter-to-fake process checks. No real-daemon acceptance case executed in this sandbox.

- C-4.5: retry admission persists the excluded lane in the job returned by `show`.
- C-5.3: daemon/guardian and CLI process timestamps agree despite ambient timezone and locale.
- C-6.2: the payload digest uses exact submitted prompt bytes; identical requests deduplicate.
- C-6.7: original prompts remain intact; write preparation, headless text, sent prompts, and retry checkpoints remain separate.
- C-8.2: finalization records both providers’ raw streams, launch metadata, and sent prompts.
- C-10.5: Claude environment references resolve without invoking `security` or persisting token values.
- C-11.5: `why` renders the actual policy decision’s exclusion reason.
- C-12.2: saved/reloaded `Launch.notes` remain equal and feed Claude classification with the attempt identity.
- C-14.2: changed TRUST refuses before execution; the refusal and fix survive finalization into CLI wait output. The executable fake also passes the real metadata preflight against unchanged reviewed TRUST.
- C-17.3: terminal limits/authentication/version failures map to contract job exit codes while attempts retain raw provider rc.
- C-20.5: all 17 E2E test functions cite clauses; parametrization produces 21 cases.

Final focused validation: **405 passed, 1 deselected in 12.64 seconds**, using `uv run pytest` over the prompt/launch/state/finalization regressions, both adapter suites, credential/guard/process-identity checks, the real-policy formatter regression, and both provider process suites. The detached-grandchild Codex process case was excluded; no containment proof is claimed. `git diff --check` passed. Runtime dependencies remain standard-library-only.

## Seam fixes

| File | Symptom and fix | Commit |
|---|---|---|
| `subfleet/credentials.py`, `contracts.py`, `store_schema.sql` | Claude fake lanes required the keychain; add environment credential references and permit them in fresh stores. | `84a373b` |
| `subfleet/guard/preflight.py` | The real daemon could not select a copied TRUST; add an environment fallback while preserving explicit-path precedence. | `8248222` |
| `subfleet/cli.py` | `why` expected `rejected`, while policy emitted `rejections`; consume the canonical field with compatibility fallback. | `13b4bdf` |
| `subfleet/daemon.py` | Raw streams, launches, and sent prompts were missing from artifacts; freeze stdout as the raw stream after containment and register the files. | `2c9dafd` |
| `subfleet/daemon.py` | Retry exclusions affected selection but were absent from job JSON; persist their union during admission. | `0783dd5` |
| `subfleet/daemon.py`, `cli.py` | Guard refusal details disappeared from `run --wait`; retain prelaunch errors, include the latest attempt, and render failed-attempt details. | `54e5049` |
| `subfleet/daemon.py` | Provider rc 1 escaped as the CLI code for limits/authentication/version failures; map terminal job codes to 3/4/5/6 and retain raw attempt rc. | `16cb410` |
| `tests/bin/codex` | The executable fake lacked guard metadata RPCs and the requested slow/dirty behavior; add version/`hooks/list`, delayed success, and opt-in dirty bytes. | `c13b605` |
| `subfleet/procs.py` | Local-time daemon identities disagreed with the UTC CLI and could falsely mark a live daemon stale; normalize inspection environment. | `5523bad` |
| `subfleet/daemon.py`, `adapters/codex.py` | Preambles changed the stored prompt and digest, and Codex had no sent-prompt file; preserve original bytes, record preparation separately, and atomically capture actual Codex input. | `cb144ad` |

Each production fix has a separate commit. The new regressions reproduced the artifact, exclusion, formatter, refusal, exit-code, timezone, and prompt failures before their fixes.

## Seam changes

- `Credential.kind="env"`: for Claude, `ref` names an environment variable whose value becomes `CLAUDE_CODE_OAUTH_TOKEN`. Fake lanes explicitly seed account identities.
- `SUBFLEET_GUARD_TRUST`: selects TRUST when `preflight(..., trust_path=...)` is not explicitly supplied.
- Terminal `wait` job objects include an optional `attempt` row for outcome details.
- Writable-job manifests optionally include `prepared_prompt_path`; original `prompt.md` and attempt `prompt.sent.md` remain distinct.
- Test-only Codex additions: `slow`, `SUBFLEET_FAKE_DIRTY=1`, version and metadata RPC replies. The E2E fixture uses existing daemon constructor hooks through test-only startup observers for publication auditing and the starting crash boundary.

## Not proved and why

**Milestones 1 and 2 remain unaccepted.** With `/usr/sbin`, `/sbin`, and `/bin` reachable, the final requested command produced:

```text
uv sync --group dev
Checked 6 packages in 1ms

uv run pytest -q tests/e2e
21 skipped in 0.12s
wall time: 0.54s
```

The sandbox denied `/bin/ps` and `sysctl kern.boottime`. An exploratory broader CLI unit selection also failed at AF_UNIX `bind` with `PermissionError(1)` (1 failed, 4 passed). No permission, guard, or hook refusal was bypassed. An all-skipped exit 0 is not acceptance, and the under-90-second executed-suite budget remains unmeasured.

Consequently real CLI/daemon recovery, kill/salvage, notices, export rename, offline operation, and Claude evidence persistence remain physically unproved. The fakes cannot prove actual filesystem sandbox enforcement or installed-provider trust. Broader C-21 gates such as escaped-descendant quarantine and offline kill still need their existing physical suites.

Initial PyPI DNS failure was resolved for local setup by copying existing dependency caches into this worktree’s ignored cache and syncing offline. The final ordinary `uv sync --group dev` succeeded.

## Open questions for the integrator

- Run all 21 E2E cases with permitted process inspection and Unix sockets; require executed passes and a wall time below 90 seconds. Run the existing containment/crash gates before milestone 1 signoff.
- Existing databases retain the old credential-kind CHECK: provide a schema migration before using `env` with those stores. Environment credentials support explicitly seeded identities; interactive enrollment does not infer an account from an environment-variable name.
- Stream publication and unchanged saved `Launch.notes` now have deterministic evidence. Confirm their real-daemon success and mismatch paths in the permitted run.
- All changes are committed locally on `lane/e2e`; no push was attempted. Push from the integrator’s environment.

