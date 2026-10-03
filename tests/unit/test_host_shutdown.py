"""C-4.7: a provider the host's shutdown ended is `transient`, retried, and not charged.

The rule and the attempt budget are pure functions of recorded facts
(`subfleet/host_shutdown.py`), which the daemon calls from finalization and
admission. The cases below replay the 2026-09-30 reboot from its recorded facts,
and the properties hold for every input:

- no host shutdown within a job's first `RETRIES` is ever counted against
  `max_attempts`: every retry decision for another attempt is the one the same
  history reaches with those shutdowns removed, and each of them is retried
  whatever the job's budget and its class;
- past `RETRIES` a host shutdown is charged like a transient, so a job that
  restarts its own host still ends, within `RETRIES + max_attempts` attempts,
  and no reboot alone ends a job while it has charged attempts left;
- with no host shutdown in a job's history, the decision is the one C-4.5 made
  before C-4.7 (`seq < max_attempts` and the class allows it): a differential
  check against that rule;
- a cancel always ends the job;
- the verdict is given exactly when every condition holds (an attempt the daemon
  did not signal, that a signal ended, from an earlier boot, ending in one of
  the two windows), and it is deterministic;
- in the boot it ended in, such an attempt is held, and only then.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

from hypothesis import event, given, settings
from hypothesis import strategies as st
import pytest

from subfleet import host_shutdown as hs


#: The incident's recorded facts (read-only, 2026-10-03): the attempts' boot from
#: `start.json`, the next boot's session UUID, `kern.boottime` (1790732697, which
#: had read 1790732699 on 2026-09-30: the clock moved it), the receipts' end, and
#: the old daemon's `stopping` line in `daemon.log`.
OLD_BOOT = "75469207-4043-4113-8e1f-b5469953a665"
NEW_BOOT = "fc616732-486d-4fb8-a95d-0aae48bfb501"
BOOT_AT = "2026-09-30T01:44:57Z"
ENDED_AT = "2026-09-30T01:44:41Z"
STOPPING_AT = "2026-09-30T01:44:23Z"
INCIDENT_RECEIPT = {"child_pid": 27977, "finished_at": ENDED_AT, "rc": 143, "signal": None,
                    "wall_s": 4069.652348}


def judge(**overrides):
    facts = dict(kind="dispatch", killed_by=None, attempt_boot=OLD_BOOT, current_boot=NEW_BOOT,
                 boot_at=BOOT_AT, receipt=INCIDENT_RECEIPT, daemon_stopping_at=STOPPING_AT)
    facts.update(overrides)
    return hs.verdict(**facts)


def stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def at(base: str, seconds: float) -> str:
    return stamp(datetime.fromisoformat(base.replace("Z", "+00:00")) + timedelta(seconds=seconds))


def test_c4_7_the_incident_receipts_are_a_host_shutdown():
    """C-4.7: the 2026-09-30 facts give the verdict on both grounds, and the detail
    says the host shut down and was up again, not that the work failed."""
    evidence = judge()
    assert evidence == {"ended_at": ENDED_AT, "signal": "SIGTERM", "rc": 143, "boot_id": OLD_BOOT,
                        "next_boot_id": NEW_BOOT, "boot_at": BOOT_AT, "daemon_stopping_at": STOPPING_AT,
                        "basis": "next-boot"}
    assert judge(boot_at=None)["basis"] == "daemon-stopping"
    detail = hs.detail(evidence)
    assert detail.startswith("transient: host shutdown: the provider ended at 2026-09-30T01:44:41Z on SIGTERM")
    assert "up again at 2026-09-30T01:44:57Z" in detail and "Not a failure of the work" in detail
    assert hs.boot_stamp(1790732697) == BOOT_AT


@pytest.mark.parametrize("killed_by", ["operator", "max_wall_s", "recovery"])
def test_c4_7_an_attempt_the_daemon_signalled_is_never_a_host_shutdown(killed_by):
    """C-4.7, C-9.2: `subfleet kill`, the wall limit and recovery set `killed_by`
    before they signal, and that decides it, whatever the boots and times say."""
    assert judge(killed_by=killed_by) is None


def test_c4_7_the_same_boot_or_a_legacy_boot_stamp_decides_nothing():
    """C-4.7, C-5.3: only two differing boot session UUIDs say the boot ended."""
    assert judge(current_boot=OLD_BOOT) is None
    assert judge(attempt_boot="1790732699") is None            # a legacy kern.boottime value
    assert judge(current_boot="1790732697") is None
    assert judge(attempt_boot=None) is None and judge(attempt_boot="") is None
    assert judge(attempt_boot=OLD_BOOT.upper())["boot_id"] == OLD_BOOT     # UUIDs compare canonically


@pytest.mark.parametrize("receipt,signal_name", [
    ({"rc": 143, "signal": None}, "SIGTERM"), ({"rc": 137, "signal": None}, "SIGKILL"),
    ({"rc": -15, "signal": 15}, "SIGTERM"), ({"rc": -9, "signal": 9}, "SIGKILL"),
    ({"rc": 1, "signal": 15}, "SIGTERM"),
    ({"rc": 0, "signal": None}, None), ({"rc": 1, "signal": None}, None), ({"rc": 4, "signal": None}, None),
    ({"rc": 130, "signal": None}, None), ({"rc": -2, "signal": 2}, None), ({"rc": 127, "signal": None}, None),
    ({"rc": True, "signal": None}, None), ({"rc": None, "signal": None}, None),
    ({"rc": "143", "signal": None}, None),
])
def test_c4_7_only_a_sigterm_or_sigkill_end_counts(receipt, signal_name):
    """C-4.7, C-5.2: 128 + n or the signal itself, for SIGTERM and SIGKILL only."""
    assert hs.ended_by_signal(receipt) == signal_name
    found = judge(receipt={**receipt, "finished_at": ENDED_AT})
    assert (found["signal"] if found else None) == signal_name


def test_c4_7_turns_and_unreadable_receipts_are_left_alone():
    """C-4.7, C-26.12: a conversation turn's end belongs to its conversation; a
    receipt with no end time cannot be placed at the end of its boot."""
    assert judge(kind="turn") is None
    assert judge(receipt=None) is None
    assert judge(receipt={**INCIDENT_RECEIPT, "finished_at": None}) is None
    assert judge(receipt={**INCIDENT_RECEIPT, "finished_at": "yesterday"}) is None
    assert judge(receipt={**INCIDENT_RECEIPT, "finished_at": "2026-09-30T01:44:41"}) is None   # no zone


def test_c4_7_the_two_windows_and_their_edges():
    """C-4.7: within `WINDOW_S` before the current boot began (and `CLOCK_SLACK_S`
    after), or within `WINDOW_S` after the last daemon of its boot began to stop."""
    receipt = lambda when: {**INCIDENT_RECEIPT, "finished_at": when}       # noqa: E731
    no_stop = dict(daemon_stopping_at=None)
    assert judge(receipt=receipt(at(BOOT_AT, -hs.WINDOW_S)), **no_stop)["basis"] == "next-boot"
    assert judge(receipt=receipt(at(BOOT_AT, -hs.WINDOW_S - 1)), **no_stop) is None
    assert judge(receipt=receipt(at(BOOT_AT, hs.CLOCK_SLACK_S)), **no_stop)["basis"] == "next-boot"
    assert judge(receipt=receipt(at(BOOT_AT, hs.CLOCK_SLACK_S + 1)), **no_stop) is None
    # Overnight: the host shut down at 01:44 and started again eight hours later.
    later = at(BOOT_AT, 8 * 3600)
    assert judge(boot_at=later)["basis"] == "daemon-stopping"
    assert judge(boot_at=later, daemon_stopping_at=None) is None
    assert judge(boot_at=later, daemon_stopping_at=ENDED_AT)["basis"] == "daemon-stopping"
    assert judge(boot_at=later, daemon_stopping_at=at(ENDED_AT, -hs.WINDOW_S))["basis"] == "daemon-stopping"
    assert judge(boot_at=later, daemon_stopping_at=at(ENDED_AT, -hs.WINDOW_S - 1)) is None
    # The provider may end before the daemon's stop is stamped: SIGTERM reaches
    # both at once, or the daemon held the attempt and was stopped meanwhile.
    assert judge(boot_at=later, daemon_stopping_at=at(ENDED_AT, hs.BEFORE_STOP_S))["basis"] == "daemon-stopping"
    assert judge(boot_at=later, daemon_stopping_at=at(ENDED_AT, hs.BEFORE_STOP_S + 1)) is None


def test_c4_7_a_held_attempt_always_fits_the_stop_window():
    """C-4.7: an attempt its daemon held (`SAME_BOOT_HOLD_S`) and then left to the
    next boot because it began to stop ended less than the hold before the stop,
    which `BEFORE_STOP_S` covers."""
    assert hs.BEFORE_STOP_S > hs.SAME_BOOT_HOLD_S
    stop = at(ENDED_AT, hs.SAME_BOOT_HOLD_S)
    assert judge(boot_at=at(BOOT_AT, 8 * 3600), daemon_stopping_at=stop)["basis"] == "daemon-stopping"


def test_c4_7_the_same_boot_holds_only_what_the_next_boot_could_judge():
    """C-4.7: in the boot it ended in, an attempt a signal from outside Subfleet
    ended waits `SAME_BOOT_HOLD_S`, and for good once the daemon is stopping;
    nothing else waits."""
    ended = datetime.fromisoformat(ENDED_AT.replace("Z", "+00:00"))
    hold = dict(kind="dispatch", killed_by=None, attempt_boot=OLD_BOOT, current_boot=OLD_BOOT,
                receipt=INCIDENT_RECEIPT, now=ended + timedelta(seconds=5), stopping=False)
    assert hs.held_in_boot(**hold)
    assert not hs.held_in_boot(**{**hold, "now": ended + timedelta(seconds=hs.SAME_BOOT_HOLD_S)})
    assert hs.held_in_boot(**{**hold, "now": ended + timedelta(days=3), "stopping": True})
    assert hs.held_in_boot(**{**hold, "hold_s": 1, "now": ended + timedelta(seconds=0.5)})
    for other in ({"current_boot": NEW_BOOT}, {"killed_by": "operator"}, {"killed_by": "offline-kill"},
                  {"kind": "turn"}, {"receipt": {**INCIDENT_RECEIPT, "rc": 1}}, {"receipt": None},
                  {"attempt_boot": "1790732699", "current_boot": "1790732699"}):
        assert not hs.held_in_boot(**{**hold, "stopping": True, **other}), other


def test_c4_7_previous_boot_is_the_last_daemon_of_the_boot_before():
    """C-4.7, C-5.8: `daemon.lock` as the daemon found it names that daemon when it
    ran in another boot, and otherwise the one a daemon of this boot carried forward."""
    old = {"pid": 2840, "boot_id": OLD_BOOT, "proc_start": "Tue Sep 29 12:00:00 2026", "version": "2.1.9",
           "stack_dumps": True, "stopping_at": STOPPING_AT}
    expected = {key: old[key] for key in hs.PREVIOUS_BOOT_FIELDS}
    assert hs.previous_boot(old, NEW_BOOT) == expected
    restarted = {"pid": 9, "boot_id": NEW_BOOT, "proc_start": "x", "version": "2.1.11",
                 "stopping_at": "2026-09-30T02:00:00Z", hs.PREVIOUS_BOOT_KEY: expected}
    assert hs.previous_boot(restarted, NEW_BOOT) == expected
    assert hs.previous_boot({**restarted, hs.PREVIOUS_BOOT_KEY: None}, NEW_BOOT) is None
    assert hs.previous_boot({**old, "stopping_at": None}, NEW_BOOT)["stopping_at"] is None
    for junk in (None, [], "text", {}, {"boot_id": "1790732699"}, {"boot_id": None},
                 {**restarted, hs.PREVIOUS_BOOT_KEY: {"boot_id": NEW_BOOT}}):
        assert hs.previous_boot(junk, NEW_BOOT) is None


def test_c4_7_marks_are_read_from_rows_json_and_parsed_evidence():
    """C-4.7: only the daemon's own evidence key marks an attempt; anything malformed is no mark."""
    evidence = {hs.EVIDENCE_KEY: judge(), "classification": {}}
    text = json.dumps(evidence)
    assert hs.marked(evidence) == hs.marked(text) == hs.marked({"evidence_json": text}) == judge()
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT 1 AS seq, ? AS evidence_json", (text,)).fetchone()
    assert hs.marked(row) == judge()
    for junk in (None, "", "{", "[]", "null", {"evidence_json": None}, {hs.EVIDENCE_KEY: "yes"},
                 {hs.EVIDENCE_KEY: None}, {"evidence_json": json.dumps({hs.EVIDENCE_KEY: [1]})}, 7):
        assert hs.marked(junk) is None


def test_c4_7_the_incident_jobs_would_have_retried():
    """C-4.5, C-4.7: two `limited` attempts and a third the reboot ended. Before,
    the third was the last (`seq < max_attempts` was false); now it is not charged."""
    limited = {"evidence_json": json.dumps({"classification": {}})}
    shutdown = {"evidence_json": json.dumps({hs.EVIDENCE_KEY: judge()})}
    assert hs.retry_after(cancel=False, max_attempts=3, earlier=[limited, limited], shutdown=True, eligible=False)
    # Its retry is the third charged attempt: a fourth would not run.
    assert not hs.retry_after(cancel=False, max_attempts=3, earlier=[limited, limited, shutdown],
                              shutdown=False, eligible=True)
    assert hs.charged([limited, limited, shutdown]) == 2
    line = hs.notice_line([{"seq": 1, **limited}, {"seq": 3, **shutdown}], 3)
    assert line == ("host shutdown: the host shut down or restarted under attempt a3 at 2026-09-30T01:44:41Z; "
                    "that was not the work, and it does not count against the job's 3 attempts (C-4.7)")
    assert hs.notice_line([limited]) == ""


def test_c4_7_past_the_first_shutdowns_they_are_charged_like_transients():
    """C-4.7: the first `RETRIES` host shutdowns are retried uncharged whatever the
    budget; later ones count against `max_attempts` as a transient does, so a job
    whose own work restarts the host still ends, and only by spending its
    budget. A cancel ends the job at once."""
    free = {hs.EVIDENCE_KEY: judge()}
    counted = {hs.EVIDENCE_KEY: {**judge(), "charged": True}}
    for before in range(hs.RETRIES):
        assert not hs.charged_now([free] * before)
        assert hs.retry_after(cancel=False, max_attempts=1, earlier=[free] * before,
                              shutdown=True, eligible=False)
    assert hs.charged_now([free] * hs.RETRIES)
    assert not hs.retry_after(cancel=False, max_attempts=1, earlier=[free] * hs.RETRIES,
                              shutdown=True, eligible=False)                   # its first charged attempt is its last
    assert hs.retry_after(cancel=False, max_attempts=2, earlier=[free] * hs.RETRIES,
                          shutdown=True, eligible=False)
    assert not hs.retry_after(cancel=False, max_attempts=2, earlier=[free] * hs.RETRIES + [counted],
                              shutdown=True, eligible=False)
    assert hs.charged([free] * hs.RETRIES + [counted]) == 1
    assert not hs.retry_after(cancel=True, max_attempts=99, earlier=[], shutdown=True, eligible=True)
    assert "counts against its attempts" in hs.detail({**judge(), "charged": True})
    rows = [{"seq": n + 1, hs.EVIDENCE_KEY: free[hs.EVIDENCE_KEY]} for n in range(hs.RETRIES)]
    rows.append({"seq": hs.RETRIES + 1, hs.EVIDENCE_KEY: counted[hs.EVIDENCE_KEY]})
    assert "past the first 3, a4 counted against the job's 2 attempts" in hs.notice_line(rows, 2)


# --- properties --------------------------------------------------------------

#: An attempt in a generated history: a host shutdown, or an ordinary attempt
#: whose class does or does not allow a retry (C-4.5's `eligible`).
ATTEMPT = st.one_of(st.just(("shutdown", False)), st.tuples(st.just("charged"), st.booleans()))
FREE = {hs.EVIDENCE_KEY: {"ended_at": ENDED_AT}}
COUNTED = {hs.EVIDENCE_KEY: {"ended_at": ENDED_AT, "charged": True}}
ORDINARY = {"classification": {}}


def simulate(kinds):
    """Rows for a history of attempts, each host shutdown marked as the daemon
    marks it: charged once `RETRIES` uncharged ones came before it."""
    rows = []
    for kind in kinds:
        evidence = ORDINARY if kind == "charged" else COUNTED if hs.charged_now(rows) else FREE
        rows.append({"seq": len(rows) + 1, "evidence_json": json.dumps(evidence)})
    return rows


@settings(max_examples=400, deadline=None)
@given(history=st.lists(ATTEMPT, max_size=12), max_attempts=st.integers(1, 6), eligible=st.booleans(),
       shutdown=st.booleans(), cancel=st.booleans())
def test_c4_7_property_host_shutdowns_are_never_charged(history, max_attempts, eligible, shutdown, cancel):
    """C-4.7: no host shutdown within a job's first `RETRIES` is charged. Every
    other decision is the one the same history reaches with those shutdowns
    removed, a later host shutdown deciding as an ordinary attempt whose class
    allows a retry (a transient); one of the first is always retried (barring a
    cancel)."""
    rows = simulate([kind for kind, _ in history])
    free = [row for row in rows if hs.exempt(row)]
    decided = hs.retry_after(cancel=cancel, max_attempts=max_attempts, earlier=rows,
                             shutdown=shutdown, eligible=eligible)
    if shutdown and len(free) < hs.RETRIES:
        assert decided == (not cancel)
    else:
        without = [row for row in rows if not hs.exempt(row)]
        assert decided == hs.retry_after(cancel=cancel, max_attempts=max_attempts, earlier=without,
                                         shutdown=False, eligible=eligible or shutdown)
    assert len(free) == min(hs.RETRIES, sum(kind == "shutdown" for kind, _ in history))
    assert hs.charged(rows) == len(rows) - len(free)


@settings(max_examples=300, deadline=None)
@given(earlier=st.integers(0, 10), max_attempts=st.integers(1, 6), eligible=st.booleans(), cancel=st.booleans())
def test_c4_7_property_without_shutdowns_the_budget_is_c4_5s(earlier, max_attempts, eligible, cancel):
    """C-4.5, differential: with no host shutdown, `retry_after` is the rule the
    daemon applied before C-4.7, `not cancel and seq < max_attempts and eligible`."""
    rows = simulate(["charged"] * earlier)
    seq = earlier + 1
    assert hs.retry_after(cancel=cancel, max_attempts=max_attempts, earlier=rows, shutdown=False,
                          eligible=eligible) == (not cancel and seq < max_attempts and eligible)


@settings(max_examples=300, deadline=None)
@given(outcomes=st.lists(ATTEMPT, min_size=1, max_size=30), max_attempts=st.integers(1, 6))
def test_c4_7_property_a_job_runs_its_budget_whatever_the_reboots(outcomes, max_attempts):
    """C-4.7, end to end over a job's life: attempts run in order until one is not
    retried. However the host shutdowns fall, the job runs at most `max_attempts`
    charged attempts and `RETRIES` uncharged ones, so at most
    `RETRIES + max_attempts` in all; it stops only when an attempt's class
    forbids a retry or its charged attempts reach `max_attempts`, never on an
    uncharged host shutdown."""
    rows = []
    for kind, eligible in outcomes:
        shutdown = kind == "shutdown"
        again = hs.retry_after(cancel=False, max_attempts=max_attempts, earlier=list(rows),
                               shutdown=shutdown, eligible=eligible)
        rows = simulate([*("shutdown" if hs.marked(row) else "charged" for row in rows), kind])
        if not again:
            last = rows[-1]
            assert not hs.exempt(last)
            assert hs.charged(rows) == max_attempts or (not shutdown and not eligible)
            break
    assert hs.charged(rows) <= max_attempts
    assert sum(hs.exempt(row) for row in rows) <= hs.RETRIES
    assert len(rows) <= hs.RETRIES + max_attempts


OTHER_BOOT = "0f0f0f0f-0000-4000-8000-000000000000"
#: Each condition is drawn satisfied far more often than not, so that most
#: examples miss a verdict by one condition or meet it: drawn independently and
#: evenly, under 1 % of examples reached a verdict (review of PR #119).
KINDS = st.sampled_from(["dispatch"] * 5 + ["revive", "gate-review", "turn"])
KILLED_BY = st.sampled_from([None] * 8 + ["", "operator", "max_wall_s", "recovery", "offline-kill"])
BOOT_PAIRS = st.sampled_from([(OLD_BOOT, NEW_BOOT)] * 8 + [
    (NEW_BOOT, OTHER_BOOT), (OLD_BOOT, OLD_BOOT), ("1790732697", NEW_BOOT), (OLD_BOOT, "1790732697"),
    ("", NEW_BOOT), (None, NEW_BOOT), (OLD_BOOT, None)])
RCS = st.sampled_from([143] * 6 + [137, -15, -9, 0, 1, 4, 124, 127, 130, -2, None])
SIGNALS = st.sampled_from([None] * 8 + [2, 9, 15])
ENDS = st.sampled_from(["time"] * 9 + [None, "garbage"])
#: Offsets from the boot's start and from the stop: mostly inside a window or
#: at its edges, some far away.
NEAR = st.one_of(st.integers(-hs.WINDOW_S - 200, hs.WINDOW_S + 200),
                 st.sampled_from([-hs.WINDOW_S, -hs.WINDOW_S - 1, hs.CLOCK_SLACK_S, hs.CLOCK_SLACK_S + 1,
                                  -hs.BEFORE_STOP_S, -hs.BEFORE_STOP_S - 1, hs.WINDOW_S, hs.WINDOW_S + 1, 0]),
                 st.integers(-2 * 86400, 2 * 86400))


@settings(max_examples=800, deadline=None)
@given(kind=KINDS, killed_by=KILLED_BY, boots=BOOT_PAIRS, has_boot=st.booleans(), from_boot=NEAR,
       has_stop=st.booleans(), from_stop=NEAR, rc=RCS, signal_number=SIGNALS, end=ENDS)
def test_c4_7_property_the_verdict_needs_every_condition(kind, killed_by, boots, has_boot, from_boot,
                                                         has_stop, from_stop, rc, signal_number, end):
    """C-4.7: a verdict is given exactly when every condition holds, and the same
    facts always give the same verdict."""
    attempt_boot, current_boot = boots
    ended = at(BOOT_AT, from_boot)
    boot_at = BOOT_AT if has_boot else None
    stopping = at(ended, -from_stop) if has_stop else None
    receipt = {"rc": rc, "signal": signal_number,
               "finished_at": ended if end == "time" else end}
    facts = dict(kind=kind, killed_by=killed_by, attempt_boot=attempt_boot, current_boot=current_boot,
                 boot_at=boot_at, receipt=receipt, daemon_stopping_at=stopping)
    found = hs.verdict(**facts)
    assert found == hs.verdict(**facts)
    event(f"verdict: {found['basis'] if found else 'none'}")        # both answers are drawn (review of #119)
    uuids = {OLD_BOOT, NEW_BOOT, OTHER_BOOT}
    in_window = end == "time" and (
        (has_boot and -hs.WINDOW_S <= from_boot <= hs.CLOCK_SLACK_S)
        or (has_stop and -hs.BEFORE_STOP_S <= from_stop <= hs.WINDOW_S))
    signalled = (signal_number in (9, 15) or rc in (137, 143, -9, -15))
    expected = (kind != "turn" and not killed_by and signalled
                and attempt_boot in uuids and current_boot in uuids and attempt_boot != current_boot
                and in_window)
    assert (found is not None) == expected
    if found is not None:
        assert found["boot_id"] == attempt_boot and found["next_boot_id"] == current_boot
        boot_fits = has_boot and -hs.WINDOW_S <= from_boot <= hs.CLOCK_SLACK_S
        assert found["basis"] == ("next-boot" if boot_fits else "daemon-stopping")
        assert hs.detail(found).startswith("transient: host shutdown: ")
