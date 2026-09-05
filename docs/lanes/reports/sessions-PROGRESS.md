# Sessions lane progress

Lane: `lane/sessions` — milestone 6: tickle, muster, revive, mirror, handoff as
clients of the job store. Brief: `docs/lanes/sessions.md`.

## State

Built and green. `uv run pytest -q tests/unit/test_sessions_*.py
tests/fake/test_sessions_end_to_end.py`: 202 passed in 6.9 s. Full suite:
3035 passed, 5 skipped in 148 s. An adversarial review of the lane against the
contract and against v1 is the last step.

## Done

- Read order 1-4 of the brief: the named clauses, `docs/plan.md` open decisions
  7 and 8, `docs/plan-b-rev4.md` "Session continuity, kept beside the fleet",
  the v2 seams, and v1's `tickle.py`, `handoff.py`, `bin/subfleet-mirror`,
  `notify.py`, its CLI verbs and its tests.
- `subfleet/sessions/`: `transcripts.py` (the turn classifier, the app's resume
  stub, the headless-lane heuristic, the cold scan), `registry.py` (C-23.30's
  ranking, duplicate detection, C-23.31's two lane signals), `nudge.py` (tickle
  and muster), `revive.py` (off by default for desktop-owned sessions),
  `handoff.py` (the scrub list, the suppression list, the caps),
  `mirror.py` (a full port of `bin/subfleet-mirror` v5.0 with the merge base in
  the state root and health from a per-pass sidecar), `client.py`, `cli.py`.
- Daemon seams, all additive: a `sessions` op for the three durable facts the
  kit cannot write itself; the `session:<id>:revive` lease taken in the
  admission transaction, with a conflict skipping the job and a submit-time
  refusal; `resume_launch` for a job of kind `revive`; `probe_required`
  unconditional for a revive.
- `subfleet sessions [list|continue|tickle|muster|revive|mirror|retire|
  unretire|handoff]`, `subfleet handoff`, and the `subfleet-sessions` console
  entry; compat maps `tickle`/`muster`/`revive` onto `sessions continue
  --scope ...`, `sessions`/`handoff` straight through, and `mirror` onto
  `sessions mirror` — no longer refused, because v2 owns the mirror now.
- Additive `sessions.*` policy keys with their own validator, a 60 s mirror
  timer on its own worker, a `doctor` row read from the sidecar, and the
  SessionStart wake in `hooks.py`.
- Tests: `tests/sessions_fixtures.py` plus six unit files and one fake-daemon
  file, 202 tests. Three bugs the tests found are fixed and described in the
  commits: the store's audit event shadowing the record it audited, a policy
  validator refusing the documented "off" value, and an unguarded wake call in
  the SessionStart hook.

## Next

Integrator: see `OUTPUT.md` for the seam changes, the contract questions, and
the two open questions (the revive job's `caller_session`, and the module names
`docs/invariants.json` still records as `subfleet/sessions/tickle.py`).
