# Lane brief: gates (milestone 7: main/peer agreement gates as clients of the job store)

You are building the gates of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/gates`). A gate is subfleet's most distinctive feature: a main agent approves an exact revision (a PR head and base, or a plan file's hash), one pinned read-only peer of a different model family reviews the same revision, and only their agreement authorises an already-permitted action such as a merge. v1 implements this in `consensus.py` behind the `subfleet gate` verb. v2 keeps every semantic and moves the peer run into the job store: each peer round is a job of kind `gate-review`, and the merge is a typed action with the states the contract gives it.

## Read first, in this order

1. `docs/acceptance-contract.md`: C-17.1 (`gate [pr|plan|continue]` is a permanent verb with its 0 to 5 exit codes), C-19 (actions), C-6.5 and C-17.2 (`-I`, `-D`, isolated review), and the carried-forward clauses C-23.2 to C-23.4 (isolated review), C-23.8 (a gate approves an exact revision), C-23.9 (what counts as a peer verdict), C-23.10 (where a peer round runs and what reserves it), C-23.11 to C-23.13 (what a merge requires, how a landing is verified, only the holder publishes an action's result), C-23.43 (an unattested round is not a verdict), C-23.53 (a gate stops). Cite clauses in every test docstring.
2. `docs/plan.md` amendment 5 (external actions) and `docs/plan-b-rev4.md` "Gates".
3. `subfleet/daemon.py` (submit, the `actions` table helpers if any, `_prepare_route` and how a job pins a model and lane), `subfleet/store.py`, `subfleet/contracts.py` (`ActionState`), `subfleet/cli.py`, `subfleet/compat.py` (which currently delegates `gate` to the v1 binary; your work replaces that), `subfleet/adapters/claude.py` and `codex.py` (attestation: a Fable round must be attested), `subfleet/timers.py` if present (the reset-credit action is the other `actions` user; share the action state machine helpers).
4. v1, read-only (never modify, never run its commands): `~/chief-of-staff/subfleet/subfleet/consensus.py` (all of it: `_expected_revision`, `_assert_expected`, the round loop, the verdict parser with its sentinel, the fingerprint, `_perform_merge` with `--match-head-commit`, the landing verification, the `unknown` status, the four-round cap, the state file layout), `~/chief-of-staff/subfleet/tests/test_consensus.py`, the gate section of `~/.claude/CLAUDE.md` "Model routing" (the exact command lines agents type: `subfleet gate plan <file> --peer astra --main-approve --expect-sha256 <hash>`, `subfleet gate pr <pr> --peer astra --main-approve --expect-head <sha> --expect-base <sha> --on-agreement merge --merge-method <merge|squash>`, `--dry-run`, `subfleet gate continue <gate-id> --main-approve --response <file>`). For fixtures, copy three redacted gate state directories from `~/chief-of-staff/state/subfleet/gates/` (list them with `ls -t | head -5`; read each file individually; strip any token or cookie).

## Scope

- `subfleet/gate/` package with console entry `subfleet-gate` and the `subfleet gate pr|plan|continue` verb wired into `cli.py` (remove `gate` from `compat.py`'s delegation and add compat cases for every gate command line in CLAUDE.md and the v1 README).
  - `revision.py` (C-23.8): the caller-attested revision (`--expect-head` and `--expect-base` as full OIDs, or `--expect-sha256` for a plan), never inferred from a fresh read; the fingerprint; the rule that a changed revision blocks the round.
  - `round.py` (C-23.9, C-23.10, C-23.43): each peer round submits a job of kind `gate-review` pinned to the peer model with `-I -D <review root>` isolation, a copied input bundle in a neutral directory under the state root, `read-only` sandbox, and a round lease `gate:<gate id>:round:<n>` taken in the admission transaction so abandoned output never counts; the verdict is exactly one sentinel-delimited JSON object bound to the same revision; an approval carrying findings, or a changes-requested with none, is rejected; a Fable round requires attestation `attested` and no downgrade marker; unattested is not a verdict.
  - `certificate.py`: the gate state and certificate written under `$SUBFLEET_HOME/gates/<gate id>/` in a layout that replays v1's state files (the acceptance test below), with `events` rows for every transition.
  - `merge.py` (C-19, C-23.11 to C-23.13): the merge is an `actions` row with `op_key` = repository plus PR plus approved head sha; `pending` before any remote call; preflight requires an open, non-draft PR with unchanged head and base and clean mergeability and passing required checks; the merge call pins `--match-head-commit <approved head>`; `merge` and `squash` only; the landing is verified against the merge commit's parents, never the moving base tip; a mismatch is reported as a mismatch and never retried or reverted; a timeout is `unknown` until a read of the PR settles it; only the lease holder publishes the result. `gh` is called through an injectable runner so tests never touch GitHub.
  - `cli` behaviour: exit codes 0 to 5 exactly as v1 (read them from `consensus.py` and preserve their meanings), `--dry-run` prints the fingerprint and dispatches nothing, `--on-agreement merge` requires `--main-approve` on the same fingerprint, `continue` re-enters with a new expected fingerprint and an optional `--response <file>`, and the four-round cap stops the gate (C-23.53).
- Tests: `tests/unit/test_gate_revision.py`, `test_gate_verdict.py` (every malformed-verdict case: two objects, missing sentinel, wrong revision, approval with findings, changes-requested without findings, unattested Fable), `test_gate_merge.py` (fake `gh`: preflight failures, head moved between preflight and merge, ambiguous response leading to `unknown` then settled, mismatch never retried), `test_gate_replay.py` (the three redacted v1 gate state fixtures replay to the same certificate content), and `tests/fake/test_gate_end_to_end.py` (a `gate plan` round against the fake daemon whose fake peer replays a verdict fixture; the round lease refuses a concurrent second round; `--dry-run` submits nothing).

## Out of scope

The sessions kit, the importer, timers other than sharing the action helpers, the daemon's core. Do not change existing clause meanings; if one must change, say exactly what and why in your final message.

## Acceptance for this lane

- Every gate command line in `~/.claude/CLAUDE.md` and the v1 README parses (compat cases green) and reaches the new code with v1's exit codes.
- A changed head between approval and merge blocks the merge with exit code 4 as v1 does (confirm the code from `consensus.py` and preserve it).
- The three v1 gate state fixtures replay to the same certificate.
- A Fable peer round whose attestation is `mismatch` or `unattested` is not a verdict and the gate says so.
- `uv run pytest -q tests/unit/test_gate_*.py tests/fake/test_gate_end_to_end.py` passes in under 30 s; the full suite stays green.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step. Your sandbox may have no network; if `git push` fails on DNS, do not retry; the integrator pushes `lane/gates` from outside. Never commit to `main`, never force-push. Never call `gh` against a real repository from this lane. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built; Tests (command, count, time); Clauses covered; Exit codes (the table you preserved); Compat cases added; Seam changes; Contract questions; Open questions for the integrator. No preamble.
