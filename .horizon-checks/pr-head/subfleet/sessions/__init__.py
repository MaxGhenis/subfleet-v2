"""The sessions kit: tickle, muster, revive, mirror, and handoff (milestone 6).

In v1 these lived inside the dispatcher and multiplied its state. On 2026-09-04
a headless revive produced a second live instance of a session — "the twin" —
and on 2026-09-05 the same thing happened to the integrator of this rebuild. In
v2 they are a separate entry point, `subfleet-sessions`, reached through the
permanent verb `subfleet sessions` (C-17.1). They read Claude Code's transcripts
and session registry and they submit jobs through the daemon like any other
client: a nudge is a notice row written by the `ping` op, a revive is a job of
kind `revive` whose `session:<id>:revive` lease is taken in the admission
transaction (C-23.55), and a handoff is an ordinary `run` submission (C-23.54).

Nothing in this package writes to the store, opens a session's inbox socket, or
launches a provider. The daemon owns all three.
"""

from __future__ import annotations

__all__ = ["handoff", "mirror", "nudge", "registry", "revive", "transcripts"]
