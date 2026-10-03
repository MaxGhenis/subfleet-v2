"""C-4.7: a provider the host's shutdown ended is `transient`, retried, and never charged.

The rule and the attempt budget are pure functions of recorded facts
(`subfleet/host_shutdown.py`), which the daemon calls from finalization and
admission. The cases below replay the 2026-09-30 reboot from its recorded facts,
and the properties hold for every input:

- no attempt marked as a host shutdown is ever counted against `max_attempts`:
  every retry decision is the one the same history would reach with the
  shutdowns removed;
- with no host shutdown in a job's history, the decision is the one C-4.5 made
  before C-4.7 (`seq < max_attempts` and the class allows it): a differential
  check against that rule;
- a host-shutdown attempt is retried whatever its budget and class, until the
  job has had `RETRIES` of them, so a job that restarts its own host ends;
- a cancel always ends the job;
- the verdict is never given to an attempt the daemon signalled, one that ran
  in the current boot, one with a legacy boot timestamp, one a signal did not
  end, or one that ended outside both windows, and it is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

from hypothesis import given, settings
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
    assert judge(boot_at=later, daemon_stopping_at=at(ENDED_AT, 1)) is None          # ended before the stop
    assert judge(boot_at=later, daemon_stopping_at=ENDED_AT)["basis"] == "daemon-stopping"
    assert judge(boot_at=later, daemon_stopping_at=at(ENDED_AT, -hs.WINDOW_S))["basis"] == "daemon-stopping"
    assert judge(boot_at=later, daemon_stopping_at=at(ENDED_AT, -hs.WINDOW_S - 1)) is None


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


def test_c4_7_a_job_whose_host_keeps_shutting_down_ends():
    """C-4.7: the `RETRIES + 1`th host shutdown ends the job, so a job whose own
    work restarts the host cannot restart it forever; a cancel ends it at once."""
    shutdown = {hs.EVIDENCE_KEY: judge()}
    for before in range(hs.RETRIES):
        assert hs.retry_after(cancel=False, max_attempts=1, earlier=[shutdown] * before,
                              shutdown=True, eligible=False)
    assert not hs.retry_after(cancel=False, max_attempts=99, earlier=[shutdown] * hs.RETRIES,
                              shutdown=True, eligible=True)
    assert not hs.retry_after(cancel=True, max_attempts=99, earlier=[], shutdown=True, eligible=True)
    assert "3 times" in hs.cap_line(3)


# --- properties --------------------------------------------------------------

#: An attempt in a generated history: a host shutdown, or an ordinary attempt
#: whose class does or does not allow a retry (C-4.5's `eligible`).
ATTEMPT = st.one_of(st.just(("shutdown", False)), st.tuples(st.just("charged"), st.booleans()))
EVIDENCE_OF = {"shutdown": {hs.EVIDENCE_KEY: {"ended_at": ENDED_AT}}, "charged": {"classification": {}}}


def history_rows(kinds):
    return [{"seq": n, "evidence_json": json.dumps(EVIDENCE_OF[kind])} for n, kind in enumerate(kinds, 1)]


@settings(max_examples=400, deadline=None)
@given(history=st.lists(ATTEMPT, max_size=12), max_attempts=st.integers(1, 6), eligible=st.booleans(),
       shutdown=st.booleans(), cancel=st.booleans())
def test_c4_7_property_host_shutdowns_are_never_charged(history, max_attempts, eligible, shutdown, cancel):
    """C-4.7: every decision is the one the same history reaches with every host
    shutdown removed (for an ordinary attempt), or depends only on how many host
    shutdowns came before (for a host shutdown): none is ever charged."""
    rows = history_rows([kind for kind, _ in history])
    decided = hs.retry_after(cancel=cancel, max_attempts=max_attempts, earlier=rows,
                             shutdown=shutdown, eligible=eligible)
    if shutdown:
        shutdowns = sum(kind == "shutdown" for kind, _ in history)
        assert decided == (not cancel and shutdowns < hs.RETRIES)
    else:
        without = [row for row in rows if hs.marked(row) is None]
        assert decided == hs.retry_after(cancel=cancel, max_attempts=max_attempts, earlier=without,
                                         shutdown=False, eligible=eligible)
    assert hs.charged(rows) == sum(kind == "charged" for kind, _ in history)


@settings(max_examples=300, deadline=None)
@given(earlier=st.integers(0, 10), max_attempts=st.integers(1, 6), eligible=st.booleans(), cancel=st.booleans())
def test_c4_7_property_without_shutdowns_the_budget_is_c4_5s(earlier, max_attempts, eligible, cancel):
    """C-4.5, differential: with no host shutdown, `retry_after` is the rule the
    daemon applied before C-4.7, `not cancel and seq < max_attempts and eligible`."""
    rows = history_rows(["charged"] * earlier)
    seq = earlier + 1
    assert hs.retry_after(cancel=cancel, max_attempts=max_attempts, earlier=rows, shutdown=False,
                          eligible=eligible) == (not cancel and seq < max_attempts and eligible)


@settings(max_examples=300, deadline=None)
@given(outcomes=st.lists(ATTEMPT, min_size=1, max_size=30), max_attempts=st.integers(1, 6))
def test_c4_7_property_a_job_runs_its_budget_whatever_the_reboots(outcomes, max_attempts):
    """C-4.7, end to end over a job's life: attempts run in order until one is not
    retried. However the host shutdowns fall, the job runs at most `max_attempts`
    charged attempts and at most `RETRIES + 1` host-shutdown ones; it stops on a
    charged attempt only when that attempt's class forbids a retry or it is the
    `max_attempts`th charged one; it stops on a host shutdown only at the cap."""
    rows, charged, shutdowns = [], 0, 0
    for kind, eligible in outcomes:
        shutdown = kind == "shutdown"
        again = hs.retry_after(cancel=False, max_attempts=max_attempts, earlier=list(rows),
                               shutdown=shutdown, eligible=eligible)
        rows.append({"seq": len(rows) + 1, "evidence_json": json.dumps(EVIDENCE_OF[kind])})
        charged += not shutdown
        shutdowns += shutdown
        if not again:
            if shutdown:
                assert shutdowns == hs.RETRIES + 1
            else:
                assert not eligible or charged == max_attempts
            break
    assert charged <= max_attempts and shutdowns <= hs.RETRIES + 1
    assert hs.charged(rows) == charged


BOOTS = st.sampled_from([OLD_BOOT, NEW_BOOT, "0f0f0f0f-0000-4000-8000-000000000000", "1790732697", "", None])
TIMES = st.integers(-2 * 86400, 2 * 86400).map(lambda s: at(BOOT_AT, s))
RECEIPTS = st.fixed_dictionaries({
    "rc": st.one_of(st.none(), st.sampled_from([0, 1, 4, 124, 127, 130, 137, 143, -9, -15, -2])),
    "signal": st.one_of(st.none(), st.sampled_from([2, 9, 15])),
    "finished_at": st.one_of(TIMES, st.just(None), st.just("garbage")),
})


@settings(max_examples=600, deadline=None)
@given(kind=st.sampled_from(["dispatch", "revive", "turn", "gate-review"]),
       killed_by=st.sampled_from([None, "", "operator", "max_wall_s", "recovery"]),
       attempt_boot=BOOTS, current_boot=BOOTS, boot_at=st.one_of(st.none(), TIMES),
       receipt=RECEIPTS, stopping=st.one_of(st.none(), TIMES))
def test_c4_7_property_the_verdict_needs_every_condition(kind, killed_by, attempt_boot, current_boot,
                                                         boot_at, receipt, stopping):
    """C-4.7: a verdict is given exactly when every condition holds, and the same
    facts always give the same verdict."""
    facts = dict(kind=kind, killed_by=killed_by, attempt_boot=attempt_boot, current_boot=current_boot,
                 boot_at=boot_at, receipt=receipt, daemon_stopping_at=stopping)
    found = hs.verdict(**facts)
    assert found == hs.verdict(**facts)
    uuids = {OLD_BOOT, NEW_BOOT, "0f0f0f0f-0000-4000-8000-000000000000"}
    ended = receipt["finished_at"]
    in_window = False
    if ended not in (None, "garbage"):
        end = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        if boot_at:
            began = datetime.fromisoformat(boot_at.replace("Z", "+00:00"))
            in_window |= began - timedelta(seconds=hs.WINDOW_S) <= end <= began + timedelta(seconds=hs.CLOCK_SLACK_S)
        if stopping:
            stop = datetime.fromisoformat(stopping.replace("Z", "+00:00"))
            in_window |= stop <= end <= stop + timedelta(seconds=hs.WINDOW_S)
    expected = (kind != "turn" and not killed_by and hs.ended_by_signal(receipt) is not None
                and attempt_boot in uuids and current_boot in uuids and attempt_boot != current_boot
                and in_window)
    assert (found is not None) == expected
    if found is not None:
        assert found["boot_id"] == attempt_boot and found["next_boot_id"] == current_boot
        assert found["basis"] in {"next-boot", "daemon-stopping"}
        assert hs.detail(found).startswith("transient: host shutdown: ")
