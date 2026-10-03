"""Notice delivery through the session hooks against the real daemon (C-15.2, C-15.3, C-23.26).

The hooks are run in process with a client that answers from `Daemon.dispatch`,
so every `notice.pending`, `notice.mark` and `notice.ack` goes through the
daemon's own handlers and store. No provider is launched: jobs end by `kill`
while queued, which writes their notice (C-15.1).

Incident: 2026-09-29. `notice.pending` returns service notices (jobless `ping`
messages: restart nudges, `subfleet ping`, timer alerts) with negated ids, and
`notice.mark` ran those ids against `notices`, matched nothing, and left every
service notice `pending`. Each SessionStart and UserPromptSubmit surfaced the
same ones again; 1,749 were pending that day, one of them a restart nudge from
two days before (service notice 1382, session 1cc3982f), printed under "1
detached run dispatched by this session finished while it was not running".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import itertools
import json
import re

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module
from subfleet import hooks, protocol
from tests.fake.test_state_contract import state_daemon  # noqa: F401 - the fixture


class InProcess:
    """The hooks' daemon client, answered by `Daemon.dispatch` in this process."""

    def __init__(self, daemon):
        self.daemon = daemon

    def call(self, op, args=None, **_):
        return self.daemon.dispatch(op, dict(args or {}))


def surface(daemon, root, session: str, event: str = "UserPromptSubmit") -> str:
    """Run one session hook; return the context it gave Claude ("" for none)."""
    stdout = io.StringIO()
    assert hooks.session_event(event, {"session_id": session, "hook_event_name": event},
                               root, client=InProcess(daemon), stdout=stdout) == 0
    printed = stdout.getvalue()
    return json.loads(printed)["hookSpecificOutput"]["additionalContext"] if printed else ""


def pending_ids(daemon, session: str) -> set[int]:
    return {row["notice_id"] for row in
            daemon.dispatch("notice.pending", {"session_id": session})["notices"]}


def service_row(daemon, wire_id: int) -> dict:
    assert wire_id < 0
    return daemon.store.one("SELECT * FROM service_notices WHERE notice_id=?", (-wire_id,))


def job_notice(daemon, harness, session: str) -> tuple[str, int]:
    """A cancelled job's notice for `session`: (job id, notice id)."""
    job_id = daemon.dispatch("submit", harness.submit_args(caller_session=session))["job_id"]
    daemon.dispatch("kill", {"job_id": job_id})
    row = daemon.store.one("SELECT notice_id FROM notices WHERE job_id=?", (job_id,))
    return job_id, row["notice_id"]


@pytest.fixture
def no_wake(monkeypatch):
    """SessionStart also spawns the sessions kit's worker (C-23.34); not here."""
    monkeypatch.setattr(hooks, "wake_worker", lambda *args, **kwargs: None)


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_c15_3_state_a_surfaced_service_notice_is_not_surfaced_again(state_daemon, no_wake, event):
    """C-15.3 a hook that printed a service notice marks it `surfaced`, and no later hook prints it."""
    daemon, harness = state_daemon
    notice_id = daemon.dispatch("ping", {"session_id": "nudged", "text": "this session restarted"})["notice_id"]
    assert notice_id < 0 and pending_ids(daemon, "nudged") == {notice_id}

    context = surface(daemon, harness.root, "nudged", event)
    assert context.startswith("subfleet: 1 message for this session:")
    assert "this session restarted" in context and "detached run" not in context
    row = service_row(daemon, notice_id)
    assert row["state"] == "surfaced" and row["transport"] == f"hook:{event}"
    assert row["offered_at"] and row["acknowledged_at"] is None

    assert pending_ids(daemon, "nudged") == set()
    assert surface(daemon, harness.root, "nudged", "UserPromptSubmit") == ""
    assert surface(daemon, harness.root, "nudged", "SessionStart") == ""
    assert service_row(daemon, notice_id)["state"] == "surfaced"


def test_c15_3_state_job_notices_take_the_path_they_always_took(state_daemon, no_wake):
    """C-15.3 a job notice is surfaced and marked as before; a service notice beside it gets its own header."""
    daemon, harness = state_daemon
    job_id, notice_id = job_notice(daemon, harness, "caller")
    message_id = daemon.dispatch("ping", {"session_id": "caller", "text": "a message"})["notice_id"]
    assert pending_ids(daemon, "caller") == {notice_id, message_id}

    context = surface(daemon, harness.root, "caller")
    runs, messages = context.split("\n\nsubfleet: 1 message for this session:\n\n")
    assert runs.startswith("subfleet: 1 detached run dispatched by this session finished")
    assert job_id in runs and runs.endswith("List: subfleet runs --mine · details: subfleet runs show <id>")
    assert messages.endswith("a message") and job_id not in messages
    notice = daemon.store.one("SELECT * FROM notices WHERE notice_id=?", (notice_id,))
    assert notice["state"] == "surfaced" and notice["transport"] == "hook:UserPromptSubmit"
    assert service_row(daemon, message_id)["state"] == "surfaced"
    assert surface(daemon, harness.root, "caller") == ""


def test_c15_3_state_marks_and_acks_are_session_scoped_for_service_notices(state_daemon):
    """C-15.3 a session can mark or acknowledge only its own service notices."""
    daemon, _ = state_daemon
    notice_id = daemon.dispatch("ping", {"session_id": "owner", "text": "for owner"})["notice_id"]
    daemon.dispatch("notice.mark", {"session_id": "intruder", "notice_ids": [notice_id],
                                    "state": "surfaced", "transport": "hook:UserPromptSubmit"})
    daemon.dispatch("notice.ack", {"session_id": "intruder", "notice_ids": [notice_id]})
    assert service_row(daemon, notice_id)["state"] == "pending"
    assert pending_ids(daemon, "owner") == {notice_id}


def test_c15_3_state_acknowledged_is_terminal_for_service_notices(state_daemon, monkeypatch):
    """C-15.3 a notice is acknowledged once: no later ack or mark moves it or restamps it."""
    daemon, _ = state_daemon
    clock = iter(f"2026-10-03T12:00:{second:02d}Z" for second in range(60))
    monkeypatch.setattr(daemon_module, "utcnow", lambda: next(clock))
    notice_id = daemon.dispatch("ping", {"session_id": "s", "text": "hello"})["notice_id"]
    daemon.dispatch("notice.ack", {"session_id": "s", "notice_ids": [notice_id]})
    first = service_row(daemon, notice_id)
    assert first["state"] == "acknowledged" and first["acknowledged_at"]

    daemon.dispatch("notice.ack", {"session_id": "s", "notice_ids": [notice_id]})
    for state in ("offered", "surfaced", "acknowledged"):
        daemon.dispatch("notice.mark", {"session_id": "s", "notice_ids": [notice_id],
                                        "state": state, "transport": "socket"})
    assert service_row(daemon, notice_id) == first


def test_c15_3_state_a_stale_offer_never_puts_a_surfaced_notice_back(state_daemon, no_wake):
    """C-15.3 an offer read before a hook surfaced the notice cannot make the next hook print it again.

    `notify_push.offer` and the PostToolUse hook mark `offered` from a
    `notice.pending` read; a session hook can surface the same rows between
    that read and the mark. `offered` is below `surfaced` on the ladder, so the
    mark leaves the row where the hook put it.
    """
    daemon, harness = state_daemon
    job_id, notice_id = job_notice(daemon, harness, "raced")
    message_id = daemon.dispatch("ping", {"session_id": "raced", "text": "raced message"})["notice_id"]
    read_before = sorted(pending_ids(daemon, "raced"))
    assert job_id in surface(daemon, harness.root, "raced")
    daemon.dispatch("notice.mark", {"session_id": "raced", "notice_ids": read_before,
                                    "state": "offered", "transport": "hook:PostToolUse"})
    assert daemon.store.one("SELECT state FROM notices WHERE notice_id=?", (notice_id,))["state"] == "surfaced"
    assert service_row(daemon, message_id)["state"] == "surfaced"
    assert surface(daemon, harness.root, "raced") == ""


def test_c15_3_state_a_pushed_notice_is_never_pending_again(state_daemon, no_wake):
    """C-15.3 with C-15.2 layer 4: a notice the socket push offered never goes back to `pending`.

    The push records a delivery as `offered` with transport `socket`. No mark
    returns a notice to `pending`, a later push of the same rows changes
    nothing a hook already did, and the next hook prints each one once.
    """
    daemon, harness = state_daemon
    job_id, notice_id = job_notice(daemon, harness, "pushed")
    message_id = daemon.dispatch("ping", {"session_id": "pushed", "text": "pushed message"})["notice_id"]
    ids = sorted(pending_ids(daemon, "pushed"))

    def states() -> list[str]:
        return [daemon.store.one("SELECT state FROM notices WHERE notice_id=?", (notice_id,))["state"],
                service_row(daemon, message_id)["state"]]

    daemon.dispatch("notice.mark", {"session_id": "pushed", "notice_ids": ids,
                                    "state": "offered", "transport": "socket"})
    assert states() == ["offered", "offered"]
    for state in ("pending", "queued", ""):
        with pytest.raises(protocol.ProtocolError, match="unknown notice state"):
            daemon.dispatch("notice.mark", {"session_id": "pushed", "notice_ids": ids, "state": state})
    daemon.dispatch("notice.mark", {"session_id": "pushed", "notice_ids": ids,
                                    "state": "offered", "transport": "socket"})
    assert states() == ["offered", "offered"]

    context = surface(daemon, harness.root, "pushed")
    assert job_id in context and "pushed message" in context
    assert states() == ["surfaced", "surfaced"]
    daemon.dispatch("notice.mark", {"session_id": "pushed", "notice_ids": ids,
                                    "state": "offered", "transport": "socket"})
    assert states() == ["surfaced", "surfaced"]
    assert surface(daemon, harness.root, "pushed") == ""


@pytest.mark.parametrize("bad", ["-3", 1.5, True, None])
def test_c15_3_state_notice_ids_are_integers(state_daemon, bad):
    """C-15.3 a notice id names a table by its sign, so anything but an integer is refused."""
    daemon, _ = state_daemon
    for op in ("notice.mark", "notice.ack"):
        with pytest.raises(protocol.ProtocolError, match="notice id must be an integer"):
            daemon.dispatch(op, {"session_id": "s", "notice_ids": [bad], "state": "surfaced"})


def stamp(age: timedelta) -> str:
    return (datetime.now(timezone.utc) - age).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_c23_26_state_service_notices_are_pruned_once_delivered_and_old(state_daemon, no_wake):
    """C-23.26 retention prunes `surfaced` and `acknowledged` service notices older than 14 days, never `pending` or `offered` ones."""
    daemon, harness = state_daemon
    with daemon.store.transaction() as tx:
        for state in daemon_module.NOTICE_LADDER:
            for days in (13, 15):
                tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) VALUES(?,?,?,?)",
                           ("aged", f"{state} {days}d", state, stamp(timedelta(days=days))))
    # One the hook surfaced through the real path, then aged past the window.
    surfaced_id = daemon.dispatch("ping", {"session_id": "hooked", "text": "seen"})["notice_id"]
    assert "seen" in surface(daemon, harness.root, "hooked")
    with daemon.store.transaction() as tx:
        tx.execute("UPDATE service_notices SET created_at=? WHERE notice_id=?",
                   (stamp(timedelta(days=15)), -surfaced_id))

    assert daemon._prune_service_notices() == 3
    assert sorted(row["text"] for row in daemon.store.query("SELECT text FROM service_notices")) == sorted([
        "pending 13d", "pending 15d", "offered 13d", "offered 15d", "surfaced 13d", "acknowledged 13d"])


# --- the property: no hook ever prints a notice a hook already printed --------

SESSIONS = st.sampled_from((0, 1))
OPS = st.one_of(
    st.tuples(st.just("ping"), SESSIONS),
    st.tuples(st.just("job"), SESSIONS),
    st.tuples(st.just("hook"), SESSIONS, st.sampled_from(("SessionStart", "UserPromptSubmit"))),
    st.tuples(st.just("ack"), SESSIONS, st.integers(0, 5)),
    st.tuples(st.just("offer"), SESSIONS),
    st.tuples(st.just("stale-offer"), SESSIONS),
)
EXAMPLES = itertools.count()


@settings(max_examples=60, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(ops=st.lists(OPS, max_size=14))
def test_c15_3_state_repeated_hooks_never_surface_a_notice_twice(state_daemon, no_wake, ops):
    """C-15.3 over any sequence of messages, job notices, hooks, acks and offers (fresh or stale):

    * a hook prints exactly its own session's `pending` and `offered` notices, and
      leaves none of them pending;
    * no notice is ever printed by a second hook;
    * job notices and service notices are counted under their own headers;
    * an acknowledged notice never changes state or acknowledgement time.

    One daemon serves every example, so each example has its own sessions.
    """
    daemon, harness = state_daemon
    example = next(EXAMPLES)
    sessions = {key: f"property-{example}-{key}" for key in (0, 1)}
    markers: dict[int, tuple[str, re.Pattern]] = {}         # wire id -> (session, its text)
    printed: set[int] = set()
    last_read: dict[int, list[int]] = {0: [], 1: []}        # what an offer read before the last hook
    acknowledged: dict[int, dict] = {}

    def row(wire_id: int) -> dict:
        return (service_row(daemon, wire_id) if wire_id < 0 else
                daemon.store.one("SELECT * FROM notices WHERE notice_id=?", (wire_id,)))

    for op in ops:
        kind, key = op[0], op[1]
        session = sessions[key]
        if kind == "ping":
            text = f"<message {example}.{len(markers)}>"
            wire_id = daemon.dispatch("ping", {"session_id": session, "text": text})["notice_id"]
            markers[wire_id] = (session, re.compile(re.escape(text)))
        elif kind == "job":
            job_id, wire_id = job_notice(daemon, harness, session)
            markers[wire_id] = (session, re.compile(re.escape(job_id) + r"(?![\w-])"))
        elif kind == "hook":
            before = pending_ids(daemon, session)
            last_read[key] = sorted(before)
            context = surface(daemon, harness.root, session, op[2])
            shown = {wire_id for wire_id, (_, pattern) in markers.items() if pattern.search(context)}
            assert shown == before, "a hook prints exactly this session's undelivered notices"
            assert not shown & printed, "a notice a hook printed is never printed again"
            printed |= shown
            assert pending_ids(daemon, session) == set()
            runs = sum(1 for wire_id in shown if wire_id > 0)
            messages = len(shown) - runs
            assert (f"subfleet: {runs} detached run" in context) == bool(runs)
            assert ("detached run" in context) == bool(runs)
            assert (f"subfleet: {messages} message" in context) == bool(messages)
        elif kind == "ack":
            own = sorted(wire_id for wire_id, (owner, _) in markers.items() if owner == session)
            if own:
                daemon.dispatch("notice.ack", {"session_id": session,
                                               "notice_ids": [own[op[2] % len(own)]]})
        elif kind == "offer":
            daemon.dispatch("notice.mark", {"session_id": session, "state": "offered",
                                            "notice_ids": sorted(pending_ids(daemon, session)),
                                            "transport": "socket"})
        else:
            daemon.dispatch("notice.mark", {"session_id": session, "state": "offered",
                                            "notice_ids": last_read[key], "transport": "socket"})
        for wire_id, was in acknowledged.items():
            assert row(wire_id) == was, "acknowledged is terminal"
        for wire_id in markers:
            current = row(wire_id)
            if current["state"] == "acknowledged":
                acknowledged.setdefault(wire_id, current)
            if wire_id in printed:
                assert current["state"] in ("surfaced", "acknowledged")
