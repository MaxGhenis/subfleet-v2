"""Sends that are not going through: the outbox's half (C-29.12; the retries of C-28.3).

2026-09-27: a message read "Sending" for good. A directory sat at the name of
its image's stored copy, `attachment.add` answered exit 1, and the outbox,
which queues every exit 1 again, retried it silently and without end and
dropped the daemon's words and fix. The daemon on fix/attachment-followups
names that case (`copy-blocked`, exit 1, with a fix); the release line still
answers the generic "operation failed; inspect daemon status". The outbox
(app/Sources/Outbox.swift; docs/decisions/2026-09-27-app-send-failures.md) now:

- O1 counts the failed answers in a row (`consecutive`, from 1); Send now,
  Try again and a restart keep the count;
- O2 files each answer as the table says: `out-of-order` (exit 2) is queued
  again once its conversation is re-read; an exit-1 reason whose fix is the
  person's (`copy-blocked`) stops the send at once; exit 1, exit 69, no
  answer, or a refused endpoint queue it again with a backoff; anything else
  stops it, a refusal on its merits;
- O3 puts no cap on the tries: a send that fails transiently stays queued and
  goes again once it is due;
- O4 gives a notice (`SendNotice`): `needs-person` or `refused` for a stopped
  send, with Try again and Withdraw; `retrying` from the second failed answer
  in a row while it is queued or under way, with Send now while it is queued,
  and Withdraw;
- O5 marks the next message of the conversation `waiting` exactly while the
  one ahead of it has a notice;
- O6 keeps the fix and the count in its journal, and loads a journal written
  before they existed with a count of 1;
- O7 answers Send now and Try again: a stopped send is queued again, a queued
  one is due at once, and one under way or acknowledged is refused;
- O8 shows the daemon's words without their `<reason>: ` prefix, and takes off
  only that exact prefix;
- O9 asks the person first exactly for exit 1 with reason `copy-blocked`.

The property tests replay generated answers to one send through the probe's
`answers` command (the app's `Outbox`, no socket) and compare every step with
a reference written again from these rules (`Model`); one property per rule
then checks it on the probe's own output. The examples replay the incident and
the person's choices, most of them against the daemon's own service over a
socket.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import string
import tempfile

import pytest
from hypothesis import HealthCheck, event, given, settings, strategies as st

from subfleet.conversations.store import ConversationError
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness, ServiceServer
from tests.frontend.test_core_outbox import create_step, entries, ids, person_rows, run_steps

pytestmark = needs_swift

# CoreProbeScenarios.swift `runAnswers`: two messages of one conversation, the clock at `ProbeClock`.
FIRST = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"
CONVERSATION = "cv-probe"
T0 = 1_790_000_000.0
OPEN = ("queued", "sending", "failed")                # OutboxEntry.isOpen
PERSON_ACTION_REASONS = {"copy-blocked"}               # DaemonError.personActionReasons
NOTICE_AFTER = 2                                       # Outbox.noticeAfterFailures
LABELS = {"send-now": "Send now", "try-again": "Try again", "withdraw": "Withdraw"}
GENERIC = "operation failed; inspect daemon status"    # service.py `respond`, for an error it does not name
BLOCKED_FIX = "remove the directory at the stored copy's name, then add the image again"   # fix/attachment-followups
BUSY = ("the daemon is serving 512 connections", "try again shortly")                     # daemon.py `busy_answer`
WAITING = {"key": SECOND, "kind": "waiting", "status": "Waiting: a message above has not been sent", "detail": None,
           "reason": None, "fix": None, "then": None, "failures": 0, "actions": ["withdraw"], "labels": ["Withdraw"]}
SEND_NOW_REFUSED = {"kind": "outbox", "message": 'invalidState("only a queued or failed send is sent now")'}
# The answers that are not the daemon's (`scriptedOutcome`): each one's `DaemonClientError.summary`, and
# whether the outbox queues it again (`isRetryable`, or `endpointRefused`, which `classify` also retries).
CLIENT_ERRORS = {
    "timeout": ("message.submit had no answer within 15 s", True),
    "unavailable": ("The Subfleet daemon is not running at /tmp/sf/daemon.sock.", True),
    "transport": ("the daemon closed the connection without an answer", True),
    "malformed": ("unexpected answer: message.submit: not a protocol response", True),
    "too-large": ("the request is 2000000 bytes; the daemon takes at most 1 MiB", False),
    "endpoint-refused": ("The development build needs SUBFLEET_HOME set.", True),
}
OK = {"ok": True}
SLUG = frozenset(string.ascii_lowercase + string.digits + "-")


def daemon_answer(code: int, message: str, fix: str | None = None) -> dict:
    return {"daemon": {"code": code, "message": message, "fix": fix}}


def run_answers(core_probe, steps: list[dict], journal: dict | None = None) -> tuple[list[dict], dict]:
    """The probe's `answers` command: a row per step, and the journal it left. `journal`
    is on disk first, as an earlier run of the app left it."""
    with tempfile.TemporaryDirectory(prefix="sf-send-") as scratch:
        path = Path(scratch) / "outbox.json"
        if journal is not None:
            write_json(path, journal)
        result = run_probe(core_probe, "answers", path, write_json(Path(scratch) / "script.json", {"steps": steps}))
        written = json.loads(path.read_text())
    assert (result["first"], result["second"]) == (FIRST, SECOND)
    assert len(result["steps"]) == len(steps)
    return result["steps"], written


def rows_of(core_probe, steps: list[dict]) -> list[dict]:
    return run_answers(core_probe, steps)[0]


# MARK: - The reference


def reason_of(message: str) -> str | None:
    """`DaemonError.reason`: the slug before the first ": ", when it is lowercase
    letters, digits and hyphens and begins with a letter. The generated words are
    ASCII, where Swift's `isLowercase`, `isNumber` and `isLetter` mean these sets."""
    slug, colon, _ = message.partition(": ")
    if colon and slug and set(slug) <= SLUG and slug[0] in string.ascii_lowercase:
        return slug
    return None


def backoff(attempts: int) -> float:
    """`Outbox.backoff`: 0.5 s doubling to 30 s."""
    return min(30.0, 0.5 * 2.0 ** min(max(0, attempts - 1), 64))


def classify(answer) -> str:
    """The table (C-28.3, C-29.12): `ok`; `out-of-order`, queued once the chain is
    re-read; `needs-person`, stopped until Try again; `again`, queued with a backoff
    and no cap; `refused`, stopped, a refusal on its merits."""
    if answer == OK:
        return "ok"
    if isinstance(answer, dict):
        code, reason = answer["daemon"]["code"], reason_of(answer["daemon"]["message"])
        if code == 2 and reason == "out-of-order":
            return "out-of-order"
        if code == 1 and reason in PERSON_ACTION_REASONS:
            return "needs-person"
        return "again" if code in (1, 69) else "refused"
    return "again" if CLIENT_ERRORS[answer][1] else "refused"


def said(answer) -> tuple[int | None, str, str | None]:
    """What a failed answer says: its code, its message (`DaemonClientError.summary`), its fix."""
    if isinstance(answer, dict):
        daemon = answer["daemon"]
        return daemon["code"], daemon["message"], daemon.get("fix")
    return None, CLIENT_ERRORS[answer][0], None


def failure_view(failure: dict | None) -> dict | None:
    """The probe's projection of `OutboxFailure`: `count` is `consecutive` or 1; `detail`
    the message without an exact `<reason>: ` prefix; `needs_person` read from the message."""
    if failure is None:
        return None
    reason, message = failure["reason"], failure["message"]
    detail = message[len(reason) + 2:] if reason is not None and message.startswith(reason + ": ") else message
    return {**failure, "count": 1 if failure["consecutive"] is None else failure["consecutive"], "detail": detail,
            "needs_person": failure["code"] == 1 and reason_of(message) in PERSON_ACTION_REASONS}


def notice_rule(entry: dict) -> dict | None:
    """`Outbox.notice` written again from an entry's projection (C-29.12)."""
    failure = entry["failure"]
    if entry["state"] not in OPEN or failure is None:
        return None
    withdraw = ["withdraw"] if entry["kind"] == "message.submit" else []
    if entry["state"] == "failed":
        kind = "needs-person" if failure["needs_person"] else "refused"
        status = ("Not sent: this needs you first" if kind == "needs-person"
                  else "Not sent: the app cannot send it" if failure["code"] is None
                  else "Not sent: the daemon refused it")
        then = "When that is done, choose Try again." if kind == "needs-person" else None
        actions = ["try-again", *withdraw]
    elif failure["count"] >= NOTICE_AFTER:
        kind, then = "retrying", None
        status = f"Not sent yet: {failure['count']} tries failed; the app keeps trying"
        actions = (["send-now"] if entry["state"] == "queued" else []) + withdraw
    else:
        return None
    return {"key": entry["key"], "kind": kind, "status": status, "detail": failure["detail"], "reason": failure["reason"],
            "fix": failure["fix"], "then": then, "failures": failure["count"], "actions": actions,
            "labels": [LABELS[action] for action in actions]}


def notices_rule(entry: dict) -> dict:
    """`Outbox.notices` for the two messages: the second, which never fails, waits
    exactly while the first has a notice."""
    notice = notice_rule(entry)
    return {} if notice is None else {FIRST: notice, SECOND: WAITING}


class Model:
    """The first message's journal entry, its conversation's chain and the clock, kept
    as C-28.3 and C-29.12 say; each step gives the row the probe should print."""

    def __init__(self) -> None:
        self.state, self.attempts, self.last_after, self.waiting = "queued", 0, None, False
        self.next_at: float | None = None
        self.failure: dict | None = None
        self.receipt: dict | None = None
        self.chain_known, self.chain_last = True, None
        self.now, self.seq = T0, 0

    def begin(self) -> str | None:
        if self.state != "queued":
            return f'notSendable("{FIRST} is {self.state}")'
        if not self.chain_known:
            return f'notSendable("{FIRST} waits for its conversation or its predecessor")'
        self.last_after, self.state, self.attempts = self.chain_last, "sending", self.attempts + 1
        return None

    def finish(self, answer) -> None:
        kind = classify(answer)
        if kind == "ok":
            self.receipt = {"conversation_id": CONVERSATION, "created": True, "message_id": FIRST, "origin": "person",
                            "seq": self.seq, "state": "queued"}
            self.state, self.failure, self.next_at, self.waiting = "acknowledged", None, None, False
            self.chain_known, self.chain_last = True, FIRST
            return
        code, message, fix = said(answer)
        before = 0 if self.failure is None else (self.failure["consecutive"] or 1)
        again = kind in ("out-of-order", "again")
        self.failure = {"code": code, "reason": reason_of(message) if code is not None else None, "message": message,
                        "retryable": again, "fix": fix, "consecutive": before + 1}
        self.state, self.next_at = ("queued", self.now + backoff(self.attempts)) if again else ("failed", None)
        self.waiting = self.waiting or kind == "out-of-order"
        if self.waiting:
            self.chain_known = False

    def send_now(self) -> str | None:
        if self.state not in ("queued", "failed"):
            return SEND_NOW_REFUSED["message"]
        self.state, self.next_at = "queued", None
        return None

    def entry(self) -> dict:
        entry = {"key": FIRST, "kind": "message.submit", "order": 1, "conversation": CONVERSATION, "state": self.state,
                 "attempts": self.attempts, "last_after": self.last_after, "waiting_for_predecessor": self.waiting,
                 "next_attempt_at": self.next_at, "failure": failure_view(self.failure), "receipt": self.receipt,
                 "conversation_id": None, "text": "message 1"}
        entry["notice"] = notice_rule(entry)
        return entry

    def sendable(self) -> list[str]:
        due = (0 if self.next_at is None else self.next_at) <= self.now
        if self.state in OPEN:
            return [FIRST] if self.state == "queued" and due and self.chain_known else []
        return [SECOND] if self.chain_known else []

    def run(self, step: dict) -> dict:
        row: dict = {}
        error = None
        if "answer" in step:
            error = self.begin()
            if error is None:
                self.seq += 1
                self.finish(step["answer"])
                row["did"] = "answered"
        else:
            action = step["do"]
            if action == "send-now":
                error = self.send_now()
            elif action == "begin":
                error = self.begin()
            elif action == "know-chain":
                self.chain_known, self.chain_last, self.waiting = True, None, False
            elif action == "advance":
                self.now += step["seconds"]
            elif action == "reload" and self.state == "sending":       # its fate is unknown: it goes again
                self.state = "queued"
            if error is None:
                row["did"] = action
        if error is not None:
            row["error"] = {"kind": "outbox", "message": error}
        entry = self.entry()
        row.update(entry=entry, sendable=self.sendable(),
                   due_in=None if self.next_at is None else self.next_at - self.now, notices=notices_rule(entry))
        return row


def replay(steps: list[dict]) -> list[dict]:
    model = Model()
    return [model.run(step) for step in steps]


# MARK: - Generated answers

WORDS = st.text(st.sampled_from(list("abz XY:;-./")), max_size=24)
SLUGS = st.text(st.sampled_from(list("abcxyz019-")), min_size=1, max_size=10)
REASONS = ["copy-blocked", "copy-blocked", "out-of-order", "out-of-order", "copy-mismatch", "settings-mismatch",
           "person-only", "too-late"]
NOT_REASONS = ["Copy-blocked", "copy blocked", "copy_blocked", "1copy-blocked", "-out-of-order", " copy-blocked",
               "copy-blocked:", "out-of-order!"]
CODES = [1, 1, 1, 69, 2, 2, 3, 4, 7, 64, 65]
FIXES = [None, None, BLOCKED_FIX, "run `subfleet daemon status`", ""]


@st.composite
def daemon_messages(draw) -> str:
    """A refusal's words: `<reason>: <words>` as service.py writes them, a slug that is
    no reason, no space after the colon, words alone (": " may come later in them), the
    generic exit-1 words, or none."""
    words = draw(WORDS)
    form = draw(st.sampled_from(["reason"] * 4 + ["slug", "not-a-reason", "no-space", "words", "generic", "empty"]))
    if form == "reason":
        return f"{draw(st.sampled_from(REASONS))}: {words}"
    if form == "slug":
        return f"{draw(SLUGS)}: {words}"
    if form == "not-a-reason":
        return f"{draw(st.sampled_from(NOT_REASONS))}: {words}"
    if form == "no-space":
        return f"{draw(st.sampled_from(REASONS))}:{words}"
    return {"words": words, "generic": GENERIC, "empty": ""}[form]


@st.composite
def answers(draw):
    """An answer to one send: a receipt, the incident's `copy-blocked`, `out-of-order`,
    the generic exit 1, busy, any refusal (codes 1, 69, 2, 3, 4, 7, 64, 65), or no answer
    from the daemon at all."""
    kind = draw(st.sampled_from(["ok", "copy-blocked", "copy-blocked", "out-of-order", "out-of-order", "generic",
                                 "generic", "generic", "busy"] + ["daemon"] * 6 + list(CLIENT_ERRORS)))
    if kind == "ok":
        return OK
    if kind == "copy-blocked":
        return daemon_answer(draw(st.sampled_from([1, 1, 1, 2, 69])), f"copy-blocked: {draw(WORDS)}",
                             draw(st.sampled_from([BLOCKED_FIX, None])))
    if kind == "out-of-order":
        return daemon_answer(draw(st.sampled_from([2, 2, 2, 1, 3])), f"out-of-order: {draw(WORDS)}")
    if kind == "generic":
        return daemon_answer(1, GENERIC)
    if kind == "busy":
        return daemon_answer(69, *BUSY)
    if kind == "daemon":
        return daemon_answer(draw(st.sampled_from(CODES)), draw(daemon_messages()), draw(st.sampled_from(FIXES)))
    return kind


# Answers that never stop a send (O3): exit 1 but `copy-blocked`, 69, no answer, a refused endpoint.
transient_answers = st.one_of(
    st.sampled_from([daemon_answer(1, GENERIC), daemon_answer(69, *BUSY), "timeout", "unavailable", "transport",
                     "malformed", "endpoint-refused"]),
    st.builds(daemon_answer, st.sampled_from([1, 69]), daemon_messages(), st.sampled_from(FIXES)),
).filter(lambda answer: classify(answer) == "again")

STEP_WEIGHTS = {"answer": 10, "send-now": 2, "advance": 2, "know-chain": 2, "reload": 1, "begin": 1}


@st.composite
def scripts(draw, answer=answers(), weights: dict | None = None, max_steps: int = 30):
    """Steps for the `answers` command: answers to the first message's send, the
    person's Send now or Try again, the clock (in quarter seconds, so every time is
    exact), a restart, the conversation re-read, and a send left under way."""
    weights = {**STEP_WEIGHTS, **(weights or {})}
    kinds = [kind for kind, weight in weights.items() for _ in range(weight)]
    steps = []
    for _ in range(draw(st.integers(1, max_steps))):
        kind = draw(st.sampled_from(kinds))
        if kind == "answer":
            steps.append({"answer": draw(answer)})
        elif kind == "advance":
            steps.append({"do": "advance", "seconds": draw(st.integers(0, 130)) / 4})
        else:
            steps.append({"do": kind})
    return steps


DIFFERENTIAL = settings(max_examples=150, deadline=None, derandomize=True,
                        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
PROPERTY = settings(DIFFERENTIAL, max_examples=40)


def labels(steps: list[dict], rows: list[dict]) -> set[str]:
    """The cases a script reached, for the statistics and the coverage test."""
    out: set[str] = set()
    before: dict | None = None
    for step, row in zip(steps, rows):
        entry, notices, failure = row["entry"], row["notices"], row["entry"]["failure"]
        old = before["entry"] if before else None
        notice = notices.get(FIRST)
        if notice:
            out.add(f"a {notice['kind']} notice")
            if notice["kind"] == "retrying" and entry["state"] == "sending":
                out.add("a retrying send under way")
            if notice["kind"] == "refused" and failure["code"] is None:
                out.add("refused by the app")
        if SECOND in notices:
            out.add("the next message waits")
        if SECOND in row["sendable"]:
            out.add("the next message may go")
        if "answer" in step and row.get("did"):
            kind = classify(step["answer"])
            out.add(f"answered {kind}")
            if kind == "ok" and old and old["failure"]:
                out.add("sent after failures")
        if "error" in row:
            out.add(f"refused step {step.get('do', 'answer')}")
        if step.get("do") in ("send-now", "reload") and old:
            out.add(f"{step['do']} while {old['state']}")
            if step["do"] == "send-now" and old["state"] == "failed" and old["failure"]["needs_person"]:
                out.add("Try again after the person's fix")
        if step.get("do") == "know-chain" and old and old["waiting_for_predecessor"]:
            out.add("the chain re-read after out-of-order")
        if step.get("do") == "advance" and FIRST in row["sendable"] and before and FIRST not in before["sendable"]:
            out.add("a retry came due")
        if row["due_in"] == 30:
            out.add("the backoff at its cap")
        if failure and failure["detail"] != failure["message"]:
            out.add("the reason taken off the words")
        if failure and failure["reason"] is None and ": " in failure["message"]:
            out.add("words with a colon and no reason")
        if failure and failure["count"] >= 8:
            out.add("eight failed answers in a row")
        before = row
    return out


# MARK: - The differential property


@DIFFERENTIAL
@given(steps=scripts())
def test_c29_12_property_the_outbox_matches_the_reference_after_every_step(core_probe, steps):
    """C-28.3, C-29.12 (O1 to O9 at once): after every step the probe's first entry
    (state, attempts, backoff, failure with its count, detail, fix and needs-person,
    receipt, notice), what may be sent, seconds until the retry, and every notice (the
    second message's `waiting` included) equal the reference's."""
    rows = rows_of(core_probe, steps)
    for index, (step, row, expected) in enumerate(zip(steps, rows, replay(steps))):
        assert row == expected, (index, step)
    for label in sorted(labels(steps, rows)):
        event(label)


# MARK: - One property per rule, on the probe's own output


@PROPERTY
@given(steps=scripts())
def test_c28_3_o1_property_the_count_is_the_failed_answers_in_a_row(core_probe, steps):
    """C-28.3, C-29.12 (O1): a failed answer counts one more than the failure before it
    (one after a receipt or at the start); no other step changes the failure: not Send
    now or Try again, a restart, the clock, a re-read chain, or a send left under way."""
    rows = rows_of(core_probe, steps)
    run, before = 0, None
    for step, row in zip(steps, rows):
        failure = row["entry"]["failure"]
        if row.get("did") == "answered":
            run = 0 if step["answer"] == OK else run + 1
            if run == 0:
                assert failure is None and row["entry"]["state"] == "acknowledged"
            else:
                assert (failure["consecutive"], failure["count"]) == (run, run)
        else:
            assert failure == before, step
        before = failure


@PROPERTY
@given(steps=scripts())
def test_c28_3_o2_property_each_answer_is_filed_as_the_table_says(core_probe, steps):
    """C-28.3, C-29.12 (O2): after each answer the state, whether the outbox tries again,
    the backoff (0.5 s doubling to 30 s, from the attempts so far), whether the chain is
    re-read first, and the failure's code, reason, words and fix follow from the answer
    alone; nothing failed goes again at once."""
    rows = rows_of(core_probe, steps)
    now, begun, answered = T0, 0, 0
    for step, row in zip(steps, rows):
        entry = row["entry"]
        now += step.get("seconds", 0)
        begun += row.get("did") in ("answered", "begin")
        assert entry["attempts"] == begun
        if row.get("did") != "answered":
            continue
        answered += 1
        kind = classify(step["answer"])
        if kind == "ok":
            assert (entry["state"], entry["failure"], entry["next_attempt_at"]) == ("acknowledged", None, None)
            assert entry["receipt"]["seq"] == answered and row["sendable"] == [SECOND]
            continue
        code, message, fix = said(step["answer"])
        failure, again = entry["failure"], kind in ("out-of-order", "again")
        assert (failure["code"], failure["reason"], failure["message"], failure["fix"]) == (
            code, reason_of(message) if code is not None else None, message, fix)
        assert failure["retryable"] is again and entry["state"] == ("queued" if again else "failed")
        assert entry["next_attempt_at"] == (now + backoff(begun) if again else None)
        assert entry["waiting_for_predecessor"] is (kind == "out-of-order")
        assert row["sendable"] == []
        event(kind)


@PROPERTY
@given(steps=scripts(answer=transient_answers, weights={"answer": 6, "know-chain": 0, "begin": 0}, max_steps=60))
def test_c28_3_o3_property_transient_failures_never_stop_the_send(core_probe, steps):
    """C-28.3 (O3): with only exit 1 (not `copy-blocked`), exit 69, no answer and a
    refused endpoint, however many, the send stays queued, the outbox keeps trying,
    it may go exactly when it is due, and its notice is `retrying` from the second."""
    rows = rows_of(core_probe, steps)
    failed = 0
    for step, row in zip(steps, rows):
        entry = row["entry"]
        failed += "answer" in step
        assert entry["state"] == "queued" and "error" not in row
        assert (entry["failure"] or {}).get("count", 0) == failed
        assert entry["failure"] is None or entry["failure"]["retryable"] is True
        assert row["sendable"] == ([FIRST] if row["due_in"] is None or row["due_in"] <= 0 else [])
        kinds = {key: notice["kind"] for key, notice in row["notices"].items()}
        assert kinds == ({FIRST: "retrying", SECOND: "waiting"} if failed >= NOTICE_AFTER else {})
    event(f"failed answers: {'10 or more' if failed >= 10 else 'under 10'}")


@PROPERTY
@given(steps=scripts())
def test_c29_12_o4_property_the_notice_follows_from_the_entry(core_probe, steps):
    """C-29.12 (O4): the first message's notice, on its entry and among the notices, is
    `Outbox.notice` of the entry the probe printed; `retrying` exactly when two or more
    answers in a row failed and it is queued or under way; a stopped send always has one."""
    for row in rows_of(core_probe, steps):
        entry, failure = row["entry"], row["entry"]["failure"]
        notice = notice_rule(entry)
        assert entry["notice"] == notice and row["notices"].get(FIRST) == notice
        retrying = entry["state"] in ("queued", "sending") and failure is not None and failure["count"] >= NOTICE_AFTER
        assert (notice is not None and notice["kind"] == "retrying") is retrying
        assert (notice is not None and notice["kind"] in ("needs-person", "refused")) is (entry["state"] == "failed")


@PROPERTY
@given(steps=scripts())
def test_c29_12_o5_property_the_next_message_waits_exactly_while_the_first_has_a_notice(core_probe, steps):
    """C-29.12 (O5): the second message, which never failed, is `waiting`, with Withdraw
    only, exactly while the first shows a notice; otherwise it has none."""
    for row in rows_of(core_probe, steps):
        assert set(row["notices"]) <= {FIRST, SECOND}
        assert row["notices"].get(SECOND) == (WAITING if FIRST in row["notices"] else None)


@PROPERTY
@given(steps=scripts(weights={"reload": 5, "begin": 2}))
def test_c28_3_o6_property_a_restart_keeps_the_failure_its_fix_and_its_count(core_probe, steps):
    """C-28.3 (O6): the journal read again after a restart gives the same entry, failure
    (fix and count included) and backoff; only a send that was under way is queued again."""
    rows = rows_of(core_probe, steps)
    for step, before, row in zip(steps[1:], rows, rows[1:]):
        if step.get("do") != "reload":
            continue
        kept = {**before["entry"], "state": "queued" if before["entry"]["state"] == "sending" else before["entry"]["state"]}
        kept["notice"] = notice_rule(kept)
        assert row["entry"] == kept and row["due_in"] == before["due_in"]
        event(f"reload while {before['entry']['state']}")


@PROPERTY
@given(steps=scripts(weights={"send-now": 6, "begin": 2}))
def test_c29_12_o7_property_send_now_and_try_again(core_probe, steps):
    """C-29.12 (O7): Send now on a queued send, or Try again on a stopped one, leaves it
    queued and due at once, its failure kept until the next answer, and sendable unless
    its conversation is re-read first; on a send under way or acknowledged it is refused
    and changes nothing."""
    rows = rows_of(core_probe, steps)
    for step, before, row in zip(steps[1:], rows, rows[1:]):
        if step.get("do") != "send-now":
            continue
        old = before["entry"]
        if old["state"] in ("queued", "failed"):
            expected = {**old, "state": "queued", "next_attempt_at": None}
            expected["notice"] = notice_rule(expected)
            assert row.get("did") == "send-now" and row["entry"] == expected
            assert row["sendable"] == ([] if old["waiting_for_predecessor"] else [FIRST])
        else:
            assert row["error"] == SEND_NOW_REFUSED and row["entry"] == old
        event(f"send-now while {old['state']}")


@PROPERTY
@given(steps=scripts(weights={"send-now": 4, "know-chain": 3}))
def test_c29_12_o8_property_the_detail_is_the_words_without_their_reason(core_probe, steps):
    """C-29.12 (O8): a failure keeps the answer's words whole; its detail, and every
    notice's, is those words without `<reason>: ` when the daemon's words carry a reason,
    and the words themselves otherwise (an answer that is not the daemon's has no reason)."""
    rows = rows_of(core_probe, steps)
    for step, row in zip(steps, rows):
        failure = row["entry"]["failure"]
        if row.get("did") == "answered" and failure is not None:
            code, message, _ = said(step["answer"])
            assert failure["message"] == message
            assert failure["reason"] == (reason_of(message) if code is not None else None)
            if failure["reason"] is None:
                assert failure["detail"] == message
            else:
                assert failure["reason"] + ": " + failure["detail"] == message
                event("a reason taken off")
        for notice in row["notices"].values():
            assert notice["detail"] == (None if notice["kind"] == "waiting" else failure["detail"])


@PROPERTY
@given(steps=scripts(weights={"send-now": 4, "know-chain": 3}))
def test_c29_12_o9_property_only_exit_1_copy_blocked_needs_the_person(core_probe, steps):
    """C-28.3, C-29.12 (O9): an answer needs the person, and stops the send with a
    `needs-person` notice, exactly when it is exit 1 with reason `copy-blocked`."""
    rows = rows_of(core_probe, steps)
    for step, row in zip(steps, rows):
        if row.get("did") != "answered" or step["answer"] == OK:
            continue
        code, message, _ = said(step["answer"])
        asks = code == 1 and reason_of(message) == "copy-blocked"
        notice = row["notices"].get(FIRST) or {}
        assert row["entry"]["failure"]["needs_person"] is asks
        assert (row["entry"]["state"] == "failed" and notice.get("kind") == "needs-person") is asks
        event(f"needs the person: {asks}")


# The cases the properties must reach to say anything, each in at least this many scripts of a batch.
REACHED = 3
CASES = [
    "a retrying notice", "a needs-person notice", "a refused notice", "a retrying send under way",
    "refused by the app", "the next message waits", "the next message may go", "answered ok", "answered again",
    "answered out-of-order", "answered needs-person", "answered refused", "sent after failures",
    "refused step answer", "refused step send-now", "send-now while queued", "send-now while failed",
    "send-now while sending", "send-now while acknowledged", "Try again after the person's fix", "reload while sending",
    "reload while failed", "the chain re-read after out-of-order", "a retry came due", "the backoff at its cap",
    "the reason taken off the words", "words with a colon and no reason", "eight failed answers in a row",
]


def test_c29_12_the_generated_scripts_reach_every_case():
    """C-29.12: the properties are not vacuous. Over a batch drawn as the differential
    property draws its own (the same generator and settings), every case comes up in
    several scripts. The rows are the reference's, which that property holds equal to
    the probe's at every step, so no probe runs here."""
    seen: Counter = Counter()

    @DIFFERENTIAL
    @given(steps=scripts())
    def collect(steps):
        for label in labels(steps, replay(steps)):
            seen[label] += 1

    collect()
    thin = {case: seen[case] for case in CASES if seen[case] < REACHED}
    assert not thin, (thin, dict(seen))


# MARK: - Examples: the person's choices, one send at a time


def test_c28_3_o3_fifty_generic_exit_1_answers_leave_the_send_queued(core_probe):
    """C-28.3, C-29.12 (O3, O4, O5): the release line's generic exit 1, fifty times: the
    send stays queued with no cap, backing off to 30 s; its notice shows from the second
    answer with the daemon's words and Send now, and the message behind it waits. Due
    again, it goes, and a receipt clears it all."""
    generic = daemon_answer(1, GENERIC)
    rows = rows_of(core_probe, [{"answer": generic}] * 50 + [{"do": "advance", "seconds": 30}, {"answer": OK}])
    for count, row in enumerate(rows[:50], 1):
        entry = row["entry"]
        assert entry["state"] == "queued" and entry["attempts"] == count and row["sendable"] == []
        assert entry["failure"] == {"code": 1, "reason": None, "message": GENERIC, "retryable": True, "fix": None,
                                    "consecutive": count, "count": count, "detail": GENERIC, "needs_person": False}
        assert row["due_in"] == backoff(count)
    assert rows[0]["notices"] == {}                             # one failure is usually gone at the first backoff
    assert rows[1]["notices"][SECOND] == WAITING
    assert rows[49]["due_in"] == 30 and rows[49]["notices"][FIRST] == {
        "key": FIRST, "kind": "retrying", "status": "Not sent yet: 50 tries failed; the app keeps trying",
        "detail": GENERIC, "reason": None, "fix": None, "then": None, "failures": 50,
        "actions": ["send-now", "withdraw"], "labels": ["Send now", "Withdraw"]}
    assert rows[50]["sendable"] == [FIRST]
    sent = rows[51]
    assert sent["entry"]["state"] == "acknowledged" and sent["entry"]["failure"] is None
    assert sent["notices"] == {} and sent["sendable"] == [SECOND]


def test_c29_12_o7_send_now_on_queued_failed_sending_and_acknowledged(core_probe):
    """C-29.12 (O7): Send now makes a queued send due at once; Try again queues a refused
    one again, due at once, its failure kept until the next answer (so it reads as
    retrying); both are refused while the send is under way or once it is acknowledged."""
    person_only = daemon_answer(7, "person-only: widening a conversation's permissions is the person's")
    rows = rows_of(core_probe, [
        {"answer": "timeout"}, {"do": "send-now"},              # 0, 1: queued, then due at once
        {"answer": person_only}, {"do": "send-now"},            # 2, 3: refused, then Try again
        {"do": "begin"}, {"do": "send-now"},                    # 4, 5: under way: refused
        {"do": "reload"},                                       # 6: the app restarted: queued again
        {"answer": OK}, {"do": "send-now"},                     # 7, 8: acknowledged: refused
    ])
    assert rows[0]["due_in"] == 0.5 and rows[0]["sendable"] == []
    assert rows[1]["did"] == "send-now" and rows[1]["due_in"] is None and rows[1]["sendable"] == [FIRST]
    assert rows[1]["entry"]["failure"] == rows[0]["entry"]["failure"]
    refused = rows[2]["notices"][FIRST]
    assert (refused["kind"], refused["status"], refused["actions"]) == (
        "refused", "Not sent: the daemon refused it", ["try-again", "withdraw"])
    assert (refused["reason"], refused["detail"], refused["failures"]) == (
        "person-only", "widening a conversation's permissions is the person's", 2)
    again = rows[3]
    assert again["entry"]["state"] == "queued" and again["due_in"] is None and again["sendable"] == [FIRST]
    assert again["entry"]["failure"] == rows[2]["entry"]["failure"]
    assert (again["notices"][FIRST]["kind"], again["notices"][FIRST]["actions"]) == ("retrying", ["send-now", "withdraw"])
    under_way = rows[4]
    assert under_way["entry"]["state"] == "sending" and under_way["entry"]["attempts"] == 3
    assert under_way["notices"][FIRST]["actions"] == ["withdraw"] and under_way["sendable"] == []
    assert rows[5]["error"] == SEND_NOW_REFUSED and rows[5]["entry"] == under_way["entry"]
    assert rows[6]["entry"]["state"] == "queued" and rows[6]["notices"][FIRST]["actions"] == ["send-now", "withdraw"]
    assert rows[7]["entry"]["state"] == "acknowledged" and rows[7]["sendable"] == [SECOND]
    assert rows[8]["error"] == SEND_NOW_REFUSED and rows[8]["entry"] == rows[7]["entry"]
    assert rows == replay([
        {"answer": "timeout"}, {"do": "send-now"}, {"answer": person_only}, {"do": "send-now"}, {"do": "begin"},
        {"do": "send-now"}, {"do": "reload"}, {"answer": OK}, {"do": "send-now"}])


def test_c29_12_o9_only_exit_1_copy_blocked_needs_the_person(core_probe):
    """C-28.3, C-29.12 (O9): `copy-blocked` stops the send for the person only with exit 1
    and the exact reason; the same words with another code, casing, spacing or reason
    are refused on their merits or tried again, as their code says."""
    cases = [
        (1, "copy-blocked: a directory is in the way", "needs-person"),
        (2, "copy-blocked: a directory is in the way", "refused"),
        (69, "copy-blocked: a directory is in the way", "retrying"),
        (1, "Copy-blocked: a directory is in the way", "retrying"),
        (1, "copy-blocked:a directory is in the way", "retrying"),
        (1, "copy blocked: a directory is in the way", "retrying"),
        (1, "copy-blocked-2: a directory is in the way", "retrying"),
        (1, "copy-mismatch: the stored copy does not match; try again", "retrying"),
        (1, "the stored copy: copy-blocked: a directory", "retrying"),
    ]
    steps = []
    for code, message, _ in cases:
        steps += [{"answer": daemon_answer(code, message, BLOCKED_FIX)}, {"do": "send-now"}]
    rows = rows_of(core_probe, steps)
    for (code, message, kind), row in zip(cases, rows[::2]):
        failure, notice = row["entry"]["failure"], row["notices"][FIRST]
        assert (failure["needs_person"], notice["kind"]) == (kind == "needs-person", kind), (code, message)
        assert notice["fix"] == BLOCKED_FIX
    assert rows == replay(steps)


# MARK: - Examples: journals an older app wrote

SETTINGS = {"auto_continue": True, "effort": None, "fast": False, "model": "opus[1m]", "permission": "ask"}


def journaled(key: str, order: int, conversation: str, state: str, attempts: int, failure: dict | None = None,
              **extra) -> dict:
    """A `message.submit` entry as Outbox.swift encodes it (sorted keys, nil left out)."""
    entry = {"attempts": attempts, "conversation": conversation, "createdAt": "2026-09-26T21:40:00.000Z", "key": key,
             "kind": "message.submit", "message": {"attachments": [], "settings": SETTINGS, "text": f"message {order}"},
             "order": order, "state": state, "waitingForPredecessor": False, **extra}
    if failure is not None:
        entry["failure"] = failure
    return entry


def test_c28_3_o6_a_journal_from_before_fix_and_consecutive_loads_with_count_1(core_probe):
    """C-28.3 (O6): a journal the app wrote before C-29.12, whose failures have no `fix`
    and no `consecutive` (Outbox.swift at 9b564fd6), loads: each failure counts as one,
    so a queued send shows nothing yet and a refused one shows its refusal. The next
    failed answer counts two; the count and the daemon's fix are journaled, and read
    back after a restart."""
    older = "00000000-0000-4000-8000-000000000003"
    journal = {"chains": {CONVERSATION: {}}, "nextOrder": 4, "version": 1, "entries": [
        journaled(FIRST, 1, CONVERSATION, "queued", 3, {"code": 1, "message": GENERIC, "retryable": True},
                  nextAttemptAt=T0 + 2),
        journaled(SECOND, 2, CONVERSATION, "queued", 0),
        journaled(older, 3, "cv-older", "failed", 1,
                  {"code": 7, "message": "person-only: widening a conversation's permissions is the person's",
                   "reason": "person-only", "retryable": False}),
    ]}
    copy_blocked = daemon_answer(1, "copy-blocked: a directory is in the way", BLOCKED_FIX)
    steps = [{"do": "advance", "seconds": 0}, {"answer": daemon_answer(1, GENERIC)}, {"do": "send-now"},
             {"answer": copy_blocked}, {"do": "reload"}]
    rows, written = run_answers(core_probe, steps, journal)
    loaded = rows[0]
    assert loaded["entry"]["failure"] == {"code": 1, "reason": None, "message": GENERIC, "retryable": True, "fix": None,
                                          "consecutive": None, "count": 1, "detail": GENERIC, "needs_person": False}
    assert loaded["entry"]["attempts"] == 3 and loaded["due_in"] == 2 and loaded["entry"]["notice"] is None
    assert loaded["notices"] == {older: {
        "key": older, "kind": "refused", "status": "Not sent: the daemon refused it",
        "detail": "widening a conversation's permissions is the person's", "reason": "person-only", "fix": None,
        "then": None, "failures": 1, "actions": ["try-again", "withdraw"], "labels": ["Try again", "Withdraw"]}}
    counted = rows[1]["entry"]
    assert (counted["failure"]["consecutive"], counted["notice"]["kind"], counted["attempts"]) == (2, "retrying", 4)
    blocked = rows[3]["entry"]
    assert (blocked["state"], blocked["failure"]["consecutive"], blocked["failure"]["fix"]) == ("failed", 3, BLOCKED_FIX)
    assert rows[4]["entry"] == blocked and rows[4]["notices"] == rows[3]["notices"]
    first, _, kept = sorted(written["entries"], key=lambda entry: entry["order"])
    assert first["failure"] == {"code": 1, "consecutive": 3, "fix": BLOCKED_FIX,
                                "message": "copy-blocked: a directory is in the way", "reason": "copy-blocked",
                                "retryable": False}
    assert kept["failure"] == journal["entries"][2]["failure"]          # never answered again: still as it was


def test_c29_12_o8_the_detail_loses_only_an_exact_reason_prefix(core_probe):
    """C-29.12 (O8): the words lose `<reason>: ` only when they begin with exactly the
    failure's reason, a colon and a space; a journaled reason the words do not begin
    with takes nothing off them."""
    cases = {
        "cv-a": ("copy-blocked", "copy-blocked: a directory is in the way", "a directory is in the way"),
        "cv-b": ("copy-blocked", "copy-blocked:a directory is in the way", "copy-blocked:a directory is in the way"),
        "cv-c": ("copy-blocked", "a directory is in the way", "a directory is in the way"),
        "cv-d": ("copy", "copy-blocked: a directory is in the way", "copy-blocked: a directory is in the way"),
        "cv-e": ("copy-blocked", "copy-blocked: ", ""),
    }
    keys = {cid: f"00000000-0000-4000-8000-00000000001{n}" for n, cid in enumerate(cases)}
    journal = {"chains": {}, "nextOrder": len(cases) + 1, "version": 1, "entries": [
        journaled(keys[cid], n + 1, cid, "failed", 1, {"code": 3, "message": message, "reason": reason,
                                                        "retryable": False, "consecutive": 1})
        for n, (cid, (reason, message, _)) in enumerate(cases.items())]}
    notices = run_answers(core_probe, [{"do": "advance", "seconds": 0}], journal)[0][0]["notices"]
    assert {cid: notices[keys[cid]]["detail"] for cid in cases} == {cid: detail for cid, (_, _, detail) in cases.items()}


# MARK: - Examples: against the daemon's own service

PNG = b"\x89PNG\r\n\x1a\n" + bytes(56)


@pytest.fixture
def daemon():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-sf-", dir="/tmp")))
    server = ServiceServer(harness)
    yield harness, server
    server.close()
    harness.close()


def blocked_image(harness, tmp_path) -> tuple[dict, Path]:
    """The 2026-09-27 incident: an image the app staged, and a directory at the name of
    its stored copy in the daemon's state root, which `attachment.add` cannot copy over."""
    path = tmp_path / "staged.png"
    path.write_bytes(PNG)
    digest = hashlib.sha256(PNG).hexdigest()
    blocker = harness.store.subdirectory("attachments") / f"{digest}.png"
    blocker.mkdir()
    return {"path": str(path), "sha256": digest, "media_type": "image/png", "bytes": len(PNG)}, blocker


def name_the_blocked_copy(harness, blocker: Path) -> None:
    """The daemon's answer on fix/attachment-followups (a673b64c), not yet on the release
    line: the copy blocked by a directory is `copy-blocked`, exit 1, with its fix, where
    the release line answers the generic exit 1 (service.py `respond` writes
    `<reason>: <words>` for both)."""
    add = harness.service.op_attachment_add

    def op_attachment_add(args, peer):
        try:
            return add(args, peer)
        except IsADirectoryError:
            raise ConversationError("copy-blocked", f"a directory is in the way of the stored copy at {blocker}",
                                    code=1, fix=BLOCKED_FIX) from None

    harness.service.op_attachment_add = op_attachment_add


def test_c28_3_o3_the_release_lines_blocked_copy_is_retried_and_shown(core_probe, tmp_path, daemon):
    """C-28.3, C-29.12 (O3, O4, O5, O7) on the release line: `attachment.add` fails with
    the generic exit 1 while a directory blocks the copy. The send stays queued and goes
    again after its backoff; from the second failure its conversation shows it with the
    daemon's words and Send now, which sends it at once; the message behind it waits.
    Nothing reaches the daemon's store."""
    harness, server = daemon
    m1, m2 = ids(2)
    staged, _ = blocked_image(harness, tmp_path)
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-blocked"),
        {"do": "submit", "conversation": "@draft:req-blocked", "message_id": m1, "text": "this image",
         "staged": [staged]},
        {"do": "submit", "conversation": "@draft:req-blocked", "message_id": m2, "text": "and this"},
        {"do": "pump"}, {"do": "notices"},
        {"do": "advance", "seconds": 1}, {"do": "pump"}, {"do": "notices"},
        {"do": "send-now", "key": m1}, {"do": "sendable"}, {"do": "pump"},
    ])
    results = out["results"]
    assert results[3]["report"]["acknowledged"] == ["req-blocked"] and results[3]["report"]["retrying"] == [m1]
    assert results[4]["notices"] == {}
    assert results[6]["report"]["retrying"] == [m1]
    assert results[7]["notices"] == {
        m1: {"key": m1, "kind": "retrying", "status": "Not sent yet: 2 tries failed; the app keeps trying",
             "detail": GENERIC, "reason": None, "fix": None, "then": None, "failures": 2,
             "actions": ["send-now", "withdraw"], "labels": ["Send now", "Withdraw"]},
        m2: {**WAITING, "key": m2}}
    assert results[9]["keys"] == [m1] and results[10]["report"]["retrying"] == [m1]
    entry = entries(out)[m1]
    assert entry["state"] == "queued" and entry["attempts"] == 3
    assert entry["failure"] == {"code": 1, "reason": None, "message": GENERIC, "retryable": True, "fix": None,
                                "consecutive": 3, "count": 3, "detail": GENERIC, "needs_person": False}
    assert [call for call in out["calls"] if "conversation.create" not in call] == ["attachment.add answered"] * 3
    cid = entries(out)["req-blocked"]["conversation_id"]
    assert person_rows(harness, cid) == []


def test_c29_12_o9_the_copy_blocked_incident_asks_the_person_and_try_again_sends_it(core_probe, tmp_path, daemon):
    """C-29.12 (O4, O5, O7, O9), the incident as the daemon names it: `copy-blocked`
    stops the send at its first failure and asks the person, with the daemon's words,
    its fix, and Try again; the message behind it waits, and nothing goes on its own.
    Try again before the fix fails the same way; once the directory is gone, Try again
    sends both messages in order."""
    harness, server = daemon
    m1, m2 = ids(2)
    staged, blocker = blocked_image(harness, tmp_path)
    name_the_blocked_copy(harness, blocker)
    journal = tmp_path / "support" / "outbox.json"
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-incident"),
        {"do": "submit", "conversation": "@draft:req-incident", "message_id": m1, "text": "this image",
         "staged": [staged]},
        {"do": "submit", "conversation": "@draft:req-incident", "message_id": m2, "text": "and this"},
        {"do": "pump"}, {"do": "notices"},
        {"do": "advance", "seconds": 60}, {"do": "sendable"}, {"do": "pump"},
        {"do": "send-now", "key": m1}, {"do": "pump"}, {"do": "notices"},
    ], journal)
    results = out["results"]
    assert results[3]["report"]["acknowledged"] == ["req-incident"] and results[3]["report"]["failed"] == [m1]
    asked = {"key": m1, "kind": "needs-person", "status": "Not sent: this needs you first",
             "detail": f"a directory is in the way of the stored copy at {blocker}", "reason": "copy-blocked",
             "fix": BLOCKED_FIX, "then": "When that is done, choose Try again.", "failures": 1,
             "actions": ["try-again", "withdraw"], "labels": ["Try again", "Withdraw"]}
    assert results[4]["notices"] == {m1: asked, m2: {**WAITING, "key": m2}}
    assert results[6]["keys"] == [] and results[7]["report"]["sent"] == []
    assert results[9]["report"]["failed"] == [m1] and results[10]["notices"][m1] == {**asked, "failures": 2}
    entry = entries(out)[m1]
    assert (entry["state"], entry["next_attempt_at"], entry["failure"]["retryable"], entry["failure"]["code"]) == (
        "failed", None, False, 1)
    assert out["calls"][1:] == ["attachment.add answered"] * 2
    blocker.rmdir()                                             # the person's fix
    cid = entries(out)["req-incident"]["conversation_id"]
    out = run_steps(core_probe, tmp_path, server, [
        {"do": "send-now", "key": m1}, {"do": "pump"}, {"do": "notices"},
    ], journal)
    assert out["results"][1]["report"]["acknowledged"] == [m1, m2] and out["results"][2]["notices"] == {}
    assert out["calls"] == ["attachment.add answered", f"message.submit {m1} after=null answered",
                            f"message.submit {m2} after={m1} answered"]
    assert entries(out)[m1]["failure"] is None and entries(out)[m1]["attempts"] == 3
    assert [(r["message_id"], r["after_message_id"]) for r in person_rows(harness, cid)] == [(m1, None), (m2, m1)]


def test_c29_12_withdraw_takes_a_send_the_daemon_never_had_and_stops_one_it_has(core_probe, tmp_path, daemon):
    """C-29.12, D-22: Withdraw on a send's notice. Never sent, it is gone with no
    receipt and no call. Sent but never received (the incident's blocked copy), the
    daemon is asked, and a tombstone keeps a late copy out; the message behind it
    stops waiting and goes. Received with its answer lost, it is stopped where the
    daemon holds it."""
    harness, server = daemon
    never, blocked, behind, landed = ids(4)
    staged, _ = blocked_image(harness, tmp_path)
    server.faults[("message.submit", landed)] = "drop"
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-withdraw"), {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-withdraw", "message_id": never, "text": "never sent"},
        {"do": "withdraw-send", "key": never},                                                           # 3
        {"do": "submit", "conversation": "@conv:req-withdraw", "message_id": blocked, "text": "this image",
         "staged": [staged]},
        {"do": "submit", "conversation": "@conv:req-withdraw", "message_id": behind, "text": "behind it"},
        {"do": "pump"}, {"do": "advance", "seconds": 1}, {"do": "pump"}, {"do": "notices"},              # 6 to 9
        {"do": "withdraw-send", "key": blocked}, {"do": "notices"}, {"do": "sendable"}, {"do": "pump"},  # 10 to 13
        {"do": "submit", "conversation": "@conv:req-withdraw", "message_id": landed, "text": "landed"},
        {"do": "pump"}, {"do": "notices"}, {"do": "withdraw-send", "key": landed},                        # 15 to 17
    ])
    results = out["results"]
    assert results[3]["result"] == {"outcome": "withdrawn", "receipt": None}
    assert not any(never in call for call in out["calls"])
    assert (results[9]["notices"][blocked]["kind"], results[9]["notices"][behind]["kind"]) == ("retrying", "waiting")
    tomb = results[10]["result"]
    assert tomb["outcome"] == "withdrawn" and tomb["receipt"]["origin"] == "tombstone"
    assert (tomb["receipt"]["state"], tomb["receipt"]["state_reason"]) == ("cancelled", "withdrawn-before-receipt")
    assert results[11]["notices"] == {} and results[12]["keys"] == [behind]
    assert results[13]["report"]["acknowledged"] == [behind] and entries(out)[behind]["last_after"] is None
    assert results[15]["report"]["retrying"] == [landed] and results[16]["notices"] == {}   # one failure: no notice
    stopped = results[17]["result"]
    assert stopped["outcome"] == "stopped" and stopped["receipt"]["state"] == "cancelled"
    assert entries(out)[blocked]["state"] == "withdrawn" and entries(out)[landed]["state"] == "acknowledged"
    cid = entries(out)["req-withdraw"]["conversation_id"]
    assert [(r["message_id"], r["origin"], r["state"]) for r in person_rows(harness, cid)] == [
        (blocked, "tombstone", "cancelled"), (behind, "person", "queued"), (landed, "person", "cancelled")]
