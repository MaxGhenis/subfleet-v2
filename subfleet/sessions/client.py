"""The kit's one connection to the daemon.

Everything the sessions kit makes durable — a nudge record, a retirement flag, a
notice, a job — goes through here, because the daemon owns the store (C-3.4) and
the dispatch path (C-23.54). Nothing in this package touches `state.sqlite3`, and
nothing opens a session's inbox socket: a nudge is a notice row written by the
`ping` op, and `subfleet/notify_push.py` is the daemon-side layer that later
tries to push it (C-15.2 layer 4).

`Sessions` is deliberately a thin object so every module above it can be tested
against a recording double instead of a live daemon.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ..client import Client, DaemonError, DaemonUnavailable
from ..protocol import PingArgs, ProtocolError, SessionsArgs, SubmitArgs

#: What a daemon older than this verb answers. `protocol.decode_request` raises
#: it before any handler runs, so it is a version signal, not a failure of the
#: request (compare `cmd_lanes`'s transfer check).
UNKNOWN_OP = "unknown op"


class SessionsUnsupported(RuntimeError):
    """The daemon is older than the `sessions` op and cannot record the kit's facts."""

    fix = "subfleet daemon stop && subfleet daemon start"


def _asdict(value: Any) -> dict[str, Any]:
    return dataclasses.asdict(value)


class Sessions:
    """`subfleet sessions`' view of the daemon."""

    def __init__(self, client: Client):
        self.client = client

    # --- durable facts (C-23.33, C-23.35, C-23.55) ---------------------------

    def state(self, session_ids: list[str] | None = None) -> dict[str, Any]:
        """Retirement, last nudge, and revive lease per session; plus lane ids."""
        return self._call("sessions", _asdict(SessionsArgs(
            action="state", session_ids=list(session_ids or []))))

    def record_nudge(self, session_id: str, *, dedupe_key: str | None,
                     cooldown_s: float | None, kind: str = "nudge",
                     detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """Reserve the nudge before delivering it.

        The reservation comes first on purpose. C-23.33 says a session is nudged
        at most once per interruption point, so the failure this ordering can
        produce is a recorded nudge that never went out — and that heals by
        itself, because the app writes a fresh resume stub (and therefore a
        fresh dedupe key) on the next restart. The other ordering produces two
        nudges for one interruption, which nothing heals.
        """
        return self._call("sessions", _asdict(SessionsArgs(
            action="nudged", session_id=session_id, dedupe_key=dedupe_key,
            cooldown_s=cooldown_s, kind=kind, detail=dict(detail or {}))))

    def retire(self, session_id: str, reason: str | None = None) -> dict[str, Any]:
        return self._call("sessions", _asdict(SessionsArgs(
            action="retire", session_id=session_id, reason=reason)))

    def unretire(self, session_id: str) -> dict[str, Any]:
        return self._call("sessions", _asdict(SessionsArgs(
            action="unretire", session_id=session_id)))

    # --- delivery and dispatch (C-15.2, C-23.54) -----------------------------

    def ping(self, session_id: str, text: str) -> dict[str, Any]:
        """One notice row for `session_id`; the delivery ladder does the rest."""
        return self.client.call("ping", _asdict(PingArgs(text=text, session_id=session_id)))

    def submit(self, args: SubmitArgs) -> dict[str, Any]:
        return self.client.call("submit", _asdict(args), request_id=args.request_id)

    # --- internals -----------------------------------------------------------

    def _call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """An unknown op is a version signal, not a failed request.

        `decode_request` rejects an op outside `protocol.OPS` before any handler
        runs, and the daemon answers `ok: false` with code 2, so a daemon older
        than this verb is indistinguishable from bad arguments unless the text
        is read. Reporting it as "older than the sessions kit" is the difference
        between a restart and an hour of confusion (compare `cmd_lanes`, which
        makes the same check for `lanes transfer`).
        """
        try:
            return self.client.call(op, args)
        except DaemonError as exc:
            if UNKNOWN_OP in str(exc):
                raise SessionsUnsupported(
                    f"this daemon does not answer `{op}`; it is older than the "
                    "sessions kit") from exc
            raise


__all__ = ["Sessions", "SessionsUnsupported", "DaemonError", "DaemonUnavailable"]
