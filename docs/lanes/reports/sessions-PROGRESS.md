# Sessions lane progress

Lane: `lane/sessions` — milestone 6: tickle, muster, revive, mirror, handoff as
clients of the job store. Brief: `docs/lanes/sessions.md`.

## State

Starting. Environment synced; recon of v1 behaviour and v2 seams under way.

## Done

- Read `docs/lanes/sessions.md`, the named clauses of `docs/acceptance-contract.md`
  (C-6.5, C-15.x, C-17.1, C-23.14, C-23.20, C-23.28, C-23.30-C-23.36, C-23.39,
  C-23.54, C-23.55), `docs/plan.md` open decisions 7 and 8, and
  `docs/plan-b-rev4.md` "Session continuity, kept beside the fleet".
- `uv sync --group dev` green.

## Next

- Finish the read of v1 (`tickle.py`, `handoff.py`, `bin/subfleet-mirror`,
  `notify.py`, the v1 CLI verbs and tests) and the v2 seams
  (`cli.py`, `compat.py`, `daemon.py`, `store.py`, `policy.py`, `hooks.py`,
  `timers.py`, `doctor.py`, the test harnesses).
- Build `subfleet/sessions/` (`registry.py`, `transcripts.py`, `nudge.py`,
  `revive.py`, `handoff.py`, `mirror.py`), the `subfleet-sessions` entry point,
  the CLI verbs, the compat cases, the policy keys, and the tests.
