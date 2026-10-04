## Built

Implemented native `subfleet gate pr|plan|continue` and the `subfleet-gate` console entry on `lane/gates`, replacing v1 delegation. Added caller-attested revisions, strict sentinel verdicts, three redacted v1 certificate replay archives, daemon-owned gate state/events, isolated `gate-review` jobs with admission leases, four-round stopping, and typed merge actions through an injectable runner.

Merge coverage includes immediate preflight, pinned head, immutable-parent landing verification, unknown-outcome reconciliation, holder fencing, operation-key deduplication, and interrupted result publication. No real GitHub command or v1 command was run. Runtime additions use the standard library. Implementation and progress were committed throughout; usage and constraints are in `docs/gates.md`.

Production acceptance remains incomplete: isolated Astra provenance and an unrestricted full-suite run need integration work below.

## Tests (command, count, time)

All final commands used `UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" UV_NO_SYNC=1`.

| Command | Result | Time |
| --- | --- | --- |
| `uv run pytest -q tests/unit/test_gate_*.py tests/fake/test_gate_end_to_end.py` | 194 passed, 1 skipped | 4.51 s |
| `uv run pytest -q tests/unit/test_compat.py` | 1,536 passed | 6.85 s |
| `uv run pytest -q --tb=short` | 2,881 passed, 117 failed, 64 skipped | 75.43 s |

All three archived v1 certificates replay identically. Every gate test function has a clause docstring. The process-backed daemon/CLI integration test skips because this sandbox denies process identity inspection; the fixture peer child-process test passes.

The full suite is not green here. Review classified all 117 failures: 105 socket restrictions (104 explicit bind denials, one inferred from the same startup stub with suppressed stderr), five denied guard scratch writes, and seven denied process-inspection expectations (`ps`/`sysctl`). No remaining implementation regression was identified. The full log is `/tmp/gates-full-suite-final.log`.

The prescribed `uv sync --group dev` failed on DNS downloading pytest; offline sync lacked hatchling. Final tests used already-installed locked dependencies copied into this lane's ignored virtualenv, with syncing disabled. Re-run normal installation and tests outside this sandbox.

## Clauses covered

C-17.1 (native syntax/exits), C-17.2 and C-6.5 (isolated review options/refusals), C-19.1 (typed actions), C-23.2–4 (fresh read-only isolation), C-23.8 (exact approval), C-23.9 (strict verdict), C-23.10 (neutral directory and round lease), C-23.11–13 (preflight, landing, holder result), C-23.43 (attestation), and C-23.53 (stopping cap). Journal/projection and artifact tests also exercise C-3.2–3.3 and C-8.1–8.2.

## Exit codes (the table preserved)

| Code | v1 meaning preserved |
| --- | --- |
| 0 | Agreement/completion |
| 1 | Operational error |
| 2 | Invalid input |
| 3 | Changes requested |
| 4 | Blocked/invalid review, including changed head or base |
| 5 | Failed, unverified, or queued merge action |

## Compat cases added

Ten explicit `gates-v2-*` cases cover the CLAUDE.md and v1 README commands: plan approval; PR merge/squash; Fable dry-run; plan continuation; PR continuation with response; CLAUDE response-only continuation syntax; `--max-rounds 0`; Fable account pin/exclusion; and the `sol` alias. Existing gate cases now map to native code. `-I`/`-D` cases map to native review isolation. All six gate exits are tested through the main CLI handler.

## Seam changes

- Schema 3 adds job `isolated_review`, `review_root`, and `round_lease`; ordinary request digests remain unchanged. Offline readers use the shared schema version.
- Added typed gate protocol requests, daemon filesystem-worker dispatch, startup action recovery fencing, and round leases inside existing attempt admission. Terminal job cleanup preserves the distinct round holder until consumption.
- Added fresh Claude/Codex isolation arguments and lane-home checks. Default policy adds `caps.gate_max_rounds=4`.
- Merge actions reuse existing `Actions.claim/publish`; reset-credit behavior is unchanged. Unknown settlements are additive `action.reconciled` events, preserving terminal rows.
- Fake peer adapter/daemon registration exists only under tests.

## Contract questions

No clause text or meaning was changed. Current local v1 defaults to unlimited rounds and permits some explicit same-head failed-merge retries. C-23.53 and C-23.13 require the implemented cap and immutable terminal operation behavior. The accepted `--max-rounds 0` spelling therefore uses the policy cap. Since the merge key excludes base/method, a base-only update or recovered CI cannot retry a terminal action at the same head; changing this requires a contract decision. The v1 JSON wire spelling `changes_requested` remains unchanged.

## Open questions for the integrator

- Supply authentic served-model provenance for isolated Codex ephemeral runs. Installed Codex 0.153.3 suppresses the persisted rollouts used by the adapter's attestation; available authentic exec fixtures provide no replacement evidence. Astra rounds therefore remain unattested and stop at the cap. Requested argv/startup headers are deliberately not accepted as provenance. Fable mismatch/unattested/downgrade cases also fail closed.
- Re-run installation, the process-backed gate test, and the full suite where sockets/process inspection and required scratch paths are permitted. Packaging entry-point installation could not be validated by normal sync here.
- Review the schema/admission/isolation seams when integrating other lanes. Complementary main-family selection remains caller-attested as in v1; optional `--main-model` validates an explicit family.
- One optional broad local Codex source search was refused by the unscoped-search guard. It was not retried or bypassed. No push was attempted; the integrator owns publishing `lane/gates`.
