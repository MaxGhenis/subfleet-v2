"""C-4.7: an attempt the host's shutdown ended is retried and never charged.

A provider that the host killed as it shut down or restarted did not fail at its
work, so its attempt is `transient` and does not count against the job's
`max_attempts` (C-4.5). Everything here is a pure function of recorded facts: the
attempt's row and exit receipt, the boot the daemon runs in, and what the last
daemon of the attempt's boot wrote in `daemon.lock` as it stopped. The daemon
pins the verdict in `finalization.json` the first time it finalizes the attempt
(C-4.2), so a replay reaches the same class however many boots later it runs.

Incident: 2026-09-30, the host restarted at 01:44Z. The daemon logged
`stopping` at 01:44:23Z, fourteen running Claude attempts on claude-5 ended at
01:44:41Z with rc 143 (SIGTERM), and the next boot began at 01:44:57Z. Recovery
recorded eleven of them `unknown: rc 143, no classifying evidence`, which C-4.5
never retries, and three `limited` or `transient` from the agent's own prose,
each on its job's third and last attempt. All fourteen jobs ended `failed`, and
continuations were rebuilt by hand.
"""

from __future__ import annotations

import json
import signal
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .boot_identity import session_uuid

#: The two signals a host's shutdown ends processes with: SIGTERM first, and
#: SIGKILL for whatever has not exited by the deadline.
SIGNALS = {int(signal.SIGTERM): "SIGTERM", int(signal.SIGKILL): "SIGKILL"}
#: A provider that handles the signal exits 128 + its number (Claude Code exited
#: 143 in the incident); one that does not dies of it, which the guardian records
#: as rc -n and `signal` n (C-5.2). Both are the signal's end.
SIGNAL_RCS = {128 + number: name for number, name in SIGNALS.items()}

#: How close to the end of its boot the provider must have ended: no more than
#: this before the boot the daemon runs in began, or after the last daemon of the
#: attempt's boot began to stop.
WINDOW_S = 600
#: How far after the current boot began a receipt may say it was written.
#: `kern.boottime` moves when macOS corrects the wall clock (C-5.3: 2 s on this
#: host between 2026-09-30 and 2026-10-03), and the receipt's clock is the old
#: boot's.
CLOCK_SLACK_S = 120
#: Host shutdowns a job is retried after. One more ends it, still uncharged, so
#: that a job whose own work restarts the host cannot restart it forever.
RETRIES = 3

#: The attempt evidence key that marks a host-shutdown attempt. Only the daemon
#: writes it, so no provider text can claim it (C-9.2 keeps the adapter's verdict
#: as `provider_verdict`).
EVIDENCE_KEY = "host_shutdown"
#: The `daemon.lock` key carrying the last daemon of the previous boot forward.
PREVIOUS_BOOT_KEY = "previous_boot"
PREVIOUS_BOOT_FIELDS = ("boot_id", "pid", "proc_start", "version", "stopping_at")


def _parse(stamp: Any) -> datetime | None:
    """An ISO 8601 UTC stamp as the store and the receipts write one, else None."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def boot_stamp(seconds: int | None) -> str | None:
    """`kern.boottime` seconds as the store writes a time."""
    if seconds is None:
        return None
    return _stamp(datetime.fromtimestamp(seconds, timezone.utc))


def ended_by_signal(receipt: Mapping[str, Any]) -> str | None:
    """"SIGTERM" or "SIGKILL" when the receipt says either signal ended the provider."""
    rc, number = receipt.get("rc"), receipt.get("signal")
    if isinstance(number, int) and not isinstance(number, bool) and number in SIGNALS:
        return SIGNALS[number]
    if isinstance(rc, int) and not isinstance(rc, bool):
        if rc in SIGNAL_RCS:
            return SIGNAL_RCS[rc]
        if -rc in SIGNALS:
            return SIGNALS[-rc]
    return None


def previous_boot(record: Mapping[str, Any] | None, current_boot: str | None) -> dict | None:
    """The last daemon of the boot before `current_boot`, from the `daemon.lock`
    record the daemon found when it started (C-4.7, C-5.8).

    A record from another boot is that daemon. A record from this boot is a daemon
    that started after the reboot, and it carried the previous boot's daemon
    forward under `previous_boot`. Anything else is no record."""
    if not isinstance(record, Mapping):
        return None
    if record.get("boot_id") != current_boot:
        found: Any = record
    else:
        found = record.get(PREVIOUS_BOOT_KEY)
    if not isinstance(found, Mapping) or not session_uuid(str(found.get("boot_id") or "")):
        return None
    if found.get("boot_id") == current_boot:
        return None
    return {key: found.get(key) for key in PREVIOUS_BOOT_FIELDS}


def verdict(*, kind: str | None, killed_by: str | None, attempt_boot: str | None,
            current_boot: str | None, boot_at: str | None, receipt: Mapping[str, Any] | None,
            daemon_stopping_at: str | None) -> dict | None:
    """The evidence that the host's shutdown ended this attempt, or None (C-4.7).

    All of these must hold:

    * it is a detached job's attempt, not a conversation turn's (C-26.12);
    * the daemon never signalled it (`killed_by` is null: no operator's kill,
      wall limit or recovery, C-9.2);
    * its exit receipt says SIGTERM or SIGKILL ended the provider;
    * its guardian ran in another boot than the daemon does now, both named by
      their boot session UUIDs (C-5.3: a differing UUID means the boot ended;
      a legacy `kern.boottime` value decides nothing);
    * it ended as that boot ended: no more than `WINDOW_S` before the current
      boot began (and no more than `CLOCK_SLACK_S` after it), or within
      `WINDOW_S` after the last daemon of its boot began to stop
      (`daemon_stopping_at`, which the caller passes only for that boot).
    """
    if kind == "turn" or killed_by or not isinstance(receipt, Mapping):
        return None
    ended_by = ended_by_signal(receipt)
    old, new = session_uuid(str(attempt_boot or "")), session_uuid(str(current_boot or ""))
    if ended_by is None or not old or not new or old == new:
        return None
    ended = _parse(receipt.get("finished_at"))
    if ended is None:
        return None
    window, slack = timedelta(seconds=WINDOW_S), timedelta(seconds=CLOCK_SLACK_S)
    basis = None
    began = _parse(boot_at)
    if began is not None and began - window <= ended <= began + slack:
        basis = "next-boot"
    stopping = _parse(daemon_stopping_at)
    if basis is None and stopping is not None and stopping <= ended <= stopping + window:
        basis = "daemon-stopping"
    if basis is None:
        return None
    return {"ended_at": _stamp(ended), "signal": ended_by, "rc": receipt.get("rc"),
            "boot_id": old, "next_boot_id": new, "boot_at": boot_at,
            "daemon_stopping_at": daemon_stopping_at, "basis": basis}


def detail(evidence: Mapping[str, Any]) -> str:
    """The attempt's `outcome_detail` for a host shutdown."""
    up = evidence.get("boot_at") or "an unrecorded time"
    return (f"transient: host shutdown: the provider ended at {evidence['ended_at']} on "
            f"{evidence['signal']} (rc {evidence.get('rc')}) as the host shut down; it was up again at {up}. "
            "Not a failure of the work, and not counted against the job's attempts (C-4.7)")


def marked(evidence: Any) -> dict | None:
    """The host-shutdown evidence of an attempt (its row, its `evidence_json`, or
    that parsed), or None. Malformed evidence is no mark."""
    if not isinstance(evidence, (str, bytes, Mapping)) and callable(getattr(evidence, "keys", None)):
        evidence = {key: evidence[key] for key in evidence.keys()}      # an sqlite3.Row
    if isinstance(evidence, Mapping) and "evidence_json" in evidence:
        evidence = evidence["evidence_json"]
    if isinstance(evidence, (str, bytes)):
        try:
            evidence = json.loads(evidence or "{}")
        except ValueError:
            return None
    if not isinstance(evidence, Mapping):
        return None
    found = evidence.get(EVIDENCE_KEY)
    return dict(found) if isinstance(found, Mapping) else None


def retry_after(*, cancel: bool, max_attempts: int, earlier: Iterable[Any],
                shutdown: bool, eligible: bool) -> bool:
    """C-4.5, C-4.7: whether the job tries again after this attempt.

    `earlier` is every attempt of the job before this one (rows or evidence).
    Only attempts without a host-shutdown mark are charged, this one included,
    so a job tries again while fewer than `max_attempts` charged attempts have
    run and this attempt's class allows a retry (`eligible`, C-4.5's rule). A
    host-shutdown attempt is retried whatever its class and budget, until the
    job has had more than `RETRIES` of them. A cancel ends the job either way.
    """
    if cancel:
        return False
    marks = [marked(attempt) is not None for attempt in earlier]
    if shutdown:
        return marks.count(True) + 1 <= RETRIES
    return eligible and marks.count(False) + 1 < max_attempts


def charged(attempts: Iterable[Any]) -> int:
    """How many of these attempts count against the job's `max_attempts`."""
    return sum(1 for attempt in attempts if marked(attempt) is None)


def notice_line(attempts: Iterable[Mapping[str, Any]], max_attempts: int | None = None) -> str:
    """C-15.1: the notice's line for a job the host's shutdown interrupted, or ""."""
    found = [(attempt["seq"], evidence) for attempt in attempts if (evidence := marked(attempt)) is not None]
    if not found:
        return ""
    which = ", ".join(f"a{seq} at {evidence['ended_at']}" for seq, evidence in found)
    noun = "attempt" if len(found) == 1 else "attempts"
    budget = f" {max_attempts}" if max_attempts else ""
    return (f"host shutdown: the host shut down or restarted under {noun} {which}; that was not the work, "
            f"and {'it does' if len(found) == 1 else 'they do'} not count against the job's{budget} attempts (C-4.7)")


def cap_line(shutdowns: int) -> str:
    """The notice's line for a job ended by its `RETRIES + 1`th host shutdown."""
    return (f"the host shut down under this job {shutdowns} times; it is not retried again, in case its own "
            "work restarts the host (C-4.7). Resubmit it once the host is stable")
