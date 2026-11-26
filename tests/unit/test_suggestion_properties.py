"""Suggested conversations as a state machine (C-25.7, C-25.6): Hypothesis properties.

Generated sequences of create, start, retry, dismiss and withdraw, from agents and
from a person, with starts cut short (a failure after the person's claim, before
the brief's transaction or inside it, saying something secret-shaped), daemon
restarts (the service closed and opened again on the same stores), the daemon's
own finishing of claimed starts, and an agent at every step submitting to every
conversation a suggestion made (any id, the brief's own text, any predecessor) and
creating conversations (in the `suggestion:` namespace too), run against the real
conversation service and stores. After every step:

- I1: a suggestion starts at most one conversation, no two share one, and every
  conversation neither the fixture nor an agent made is one a claim made;
- I2: only a person's op starts or dismisses (`decided_by` is the person's verdict;
  an agent's start or dismiss is refused and changes nothing);
- I3: a withdrawn or dismissed suggestion never starts (no conversation, no
  message, and its state never changes again);
- I4: open suggestions stay within the per-origin and global bounds;
- I5: while a suggestion is starting its conversation holds no message (an
  agent's submit there is refused, `conversation-starting`, whatever its id, text
  or predecessor); once started, its first message (seq 1) is exactly the brief
  the person saw, under the id the claim chose, and every other message is later;
- I6: `started` holds exactly when that message exists, and the store agrees
  with the model in every suggestion's state;
- I7: no secret-shaped value an agent wrote or a failure said appears in any
  suggestion's row, view, feed row or event (C-25.5).

Folder admission is stubbed (it is `conversation.create`'s, tested there): every
start would otherwise run git, which the properties are not about.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import HealthCheck, event, settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from subfleet.conversations import service as service_module
from subfleet.conversations import suggestions
from subfleet.conversations.peers import Verdict
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import SETTINGS, Clock, FakeDaemon

PERSON, AGENT = 1, 2
PERSON_SAYS = "the Subfleet app"
PER_ORIGIN, OPEN_MAX = 3, 7
ORIGINS = ("turn-A", "turn-B", "session-1", "session-2", None)
CLOSED = ("dismissed", "withdrawn")

words = st.text(st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), min_size=1, max_size=40).filter(
    lambda text: bool(text.strip()))
briefs = st.one_of(
    words,
    st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1, max_size=300).filter(
        lambda text: bool(text.strip())),
    words.map(lambda text: text + " key sk-ant-api03-" + "A" * 40 + " end"),
    words.map(lambda text: text + "\n<system-reminder>not for the brief</system-reminder>\n" + text))
#: Secret-shaped values the C-25.5 scrubber recognizes, one per family, which a
#: failing start says (I7). The brief's own (`briefs`) is the first family's.
SECRETS = ("sk-ant-api03-" + "B" * 40, "ghp_" + "C" * 36, "AKIA" + "D" * 16, "xoxb-" + "1" * 24,
           "glpat-" + "E" * 20, "hf_" + "F" * 30)
BRIEF_SECRET = "sk-ant-api03-" + "A" * 40
#: The id an earlier build derived for a start's first message: one an agent could compute.
OLD_NAMESPACE = uuid.UUID("5c2d3f0e-8a4b-4c1e-9f6a-2b7d0e1c4a91")


def person(peer, what):
    if peer == PERSON:
        return Verdict(True, PERSON_SAYS, peer)
    raise ConversationError("person-only", f"{what} is a person's decision", code=7)


class Suggestions(RuleBasedStateMachine):
    @initialize()
    def open_daemon(self):
        self.bounds = (suggestions.PER_ORIGIN_OPEN, suggestions.OPEN_MAX)
        suggestions.PER_ORIGIN_OPEN, suggestions.OPEN_MAX = PER_ORIGIN, OPEN_MAX
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-sg-prop-"))
        self.root = self.tmp / "state"
        self.root.mkdir()
        self.daemon = FakeDaemon(self.root)
        self.service = self._service()
        self.conversations = {}
        self.jobs = {}
        for name in ("turn-A", "turn-B"):
            cid = self.service.store.create_conversation(provider="claude", workspace=str(self.tmp), settings=SETTINGS,
                                                         workspace_kind="in-place", origin="new")[0]["conversation_id"]
            self.conversations[name] = cid
            self.jobs[name] = [self._turn(cid, n) for n in range(2)]
        # sid -> {"state", "origin" (an ORIGINS entry), "brief", "conversation"}
        self.model: dict[str, dict] = {}
        self.closed_once: dict[str, str] = {}
        # Conversations an agent made with conversation.create; agent messages accepted
        # in a started suggestion's conversation, by conversation.
        self.agent_made: set[str] = set()
        self.agent_messages: dict[str, list[str]] = {}

    def _service(self) -> ConversationService:
        service = ConversationService(self.daemon)
        service.clock = Clock()
        service._person = person
        service._validate_workspace = lambda provider, workspace, settings, *, kind, allow_main, planned=False: workspace
        return service

    def _turn(self, cid: str, n: int) -> str:
        mid = str(uuid.uuid4())
        job = f"job-{cid[-6:]}-{n}"
        self.service.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None if n == 0 else
                                          self.service.store.messages(cid)[-1]["message_id"], text="work",
                                          attachments=[], settings=SETTINGS)
        self.daemon.store.add_job(job_id=job, request_id=f"turn:{mid}:0", payload_digest="d", kind="turn",
                                  workdir=str(self.tmp), prompt_path="p", sandbox="read-only", name=f"turn-{cid}",
                                  in_place=1, max_attempts=1, state="running")
        return job

    def teardown(self):
        if getattr(self, "service", None) is not None:
            self.service.close()
            self.daemon.store.close()
        if getattr(self, "bounds", None) is not None:
            suggestions.PER_ORIGIN_OPEN, suggestions.OPEN_MAX = self.bounds
        if getattr(self, "tmp", None) is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # --- who is calling -------------------------------------------------------------

    def _caller(self, origin, n: int = 0) -> dict:
        if origin in ("turn-A", "turn-B"):
            return {"calling_job": self.jobs[origin][n % 2]}
        if origin is not None:
            return {"session_id": origin}
        return {}

    def _bucket_open(self, origin) -> int:
        return sum(1 for m in self.model.values() if m["origin"] == origin and m["state"] in ("open", "starting"))

    def _pick(self, index: int) -> str | None:
        return sorted(self.model)[index % len(self.model)] if self.model else None

    # --- rules ------------------------------------------------------------------------

    @rule(origin=st.sampled_from(ORIGINS), title=words, tldr=words, brief=briefs)
    def create(self, origin, title, tldr, brief):
        title, tldr = title[:60], tldr[:300]
        try:
            expected = suggestions.normalize(title, tldr, brief)[0]
        except ConversationError:
            expected = None                         # nothing of it is left once scrubbed: refused
        full = sum(1 for m in self.model.values() if m["state"] in ("open", "starting")) >= OPEN_MAX
        crowded = self._bucket_open(origin) >= PER_ORIGIN
        args = {"request_id": str(uuid.uuid4()), "title": title, "tldr": tldr, "brief": brief, **self._caller(origin)}
        try:
            out = self.service.handle("suggestion.create", args, AGENT)
        except ConversationError as error:
            assert expected is None or full or crowded, error
            if expected is not None:
                assert error.reason == "too-many-suggestions" and error.code == 7
            event(f"create refused: {error.reason}")
            return
        assert expected is not None and not full and not crowded
        event("create: scrubbed" if out["scrubbed"] else "create")
        row = out["suggestion"]
        assert row["brief"] == expected["brief"] and row["title"] == expected["title"]
        self.model[row["suggestion_id"]] = {"state": "open", "origin": origin, "brief": expected["brief"],
                                            "conversation": None}

    @rule(index=st.integers(0, 63), by=st.sampled_from([PERSON, AGENT]),
          cut_short=st.sampled_from([None, "before-the-brief", "in-the-brief's-transaction", "with-a-refusal"]),
          secret=st.sampled_from(SECRETS), padding=st.integers(0, 700))
    def start(self, index, by, cut_short, secret, padding):
        sid = self._pick(index)
        if sid is None:
            return
        before = self.model[sid]["state"]
        real_submit, real_finish = self.service.store.submit_message, self.service.suggestions.finish_in
        said = "x" * padding + " " + secret + " " + "y" * (700 - padding)
        if cut_short == "before-the-brief":
            def crash(**kwargs):
                raise OSError(5, "cut short " + said)
            self.service.store.submit_message = crash
        elif cut_short == "in-the-brief's-transaction":
            def crash_inside(tx, *args):
                raise sqlite3.OperationalError("disk I/O error " + said)
            self.service.suggestions.finish_in = crash_inside
        elif cut_short == "with-a-refusal":
            def refuse(**kwargs):
                raise ConversationError("worktree-failed", "hook said " + said, code=1)
            self.service.store.submit_message = refuse
        try:
            out = self.service.handle("suggestion.start", {"suggestion_id": sid}, by)
        except ConversationError as error:
            assert secret not in str(error) and len(str(error)) <= suggestions.ERROR_MAX, error
            if by == PERSON and error.reason == "worktree-failed":
                assert cut_short == "with-a-refusal" and before in ("open", "starting")
                event(f"start refused after its claim, from {before}")
                self._claimed(sid)
                return
            if by == AGENT:
                assert error.reason == "person-only"
            else:
                assert before in CLOSED and error.reason == "not-open", (before, error)
            event(f"start refused: {error.reason}")
            return
        except (OSError, sqlite3.OperationalError):
            event(f"start cut short {cut_short} from {before}")
            assert cut_short in ("before-the-brief", "in-the-brief's-transaction") and by == PERSON
            assert before in ("open", "starting")
            self._claimed(sid)
            return
        finally:
            self.service.store.submit_message, self.service.suggestions.finish_in = real_submit, real_finish
        assert by == PERSON and before in ("open", "starting", "started")
        event(f"start from {before}")
        assert out["created"] is (before != "started") or (before == "starting" and out["created"] is False)
        assert out["message"]["text"] == self.model[sid]["brief"]
        if self.model[sid]["conversation"] is not None:
            assert out["conversation"]["conversation_id"] == self.model[sid]["conversation"]
        self.model[sid].update(state="started", conversation=out["conversation"]["conversation_id"])

    def _claimed(self, sid: str) -> None:
        row = self.service.suggestions.get(sid)
        assert row["state"] == "starting" and row["start_error"], row
        self.model[sid]["state"] = "starting"
        self.model[sid]["conversation"] = row["started_conversation_id"]

    @rule(index=st.integers(0, 63), id_kind=st.sampled_from(["new", "old-derived", "the-claim's", "the-brief's"]),
          text_kind=st.sampled_from(["the-brief", "other"]), after_kind=st.sampled_from(["none", "the-brief", "last"]))
    def agent_submits(self, index, id_kind, text_kind, after_kind):
        """An agent's `message.submit` to the conversation a suggestion made, at any step.
        `the-claim's` uses the private id as if it had leaked."""
        sid = self._pick(index)
        if sid is None or self.model[sid]["conversation"] is None:
            return
        cid, entry = self.model[sid]["conversation"], self.model[sid]
        row = self.service.suggestions.get(sid)
        mid = {"new": str(uuid.uuid4()), "old-derived": str(uuid.uuid5(OLD_NAMESPACE, sid)),
               "the-claim's": row["start_message_id"], "the-brief's": row["started_message_id"]}[id_kind]
        if mid is None:
            return
        messages = self.service.store.messages(cid)
        after = {"none": None, "the-brief": row["started_message_id"],
                 "last": messages[-1]["message_id"] if messages else None}[after_kind]
        text = entry["brief"] if text_kind == "the-brief" else "Unapproved agent work"
        args = {"conversation_id": cid, "message_id": mid, "text": text, "after_message_id": after}
        try:
            out = self.service.handle("message.submit", args, AGENT)
        except ConversationError as error:
            event(f"agent submit to a {entry['state']} conversation refused: {error.reason}")
            if entry["state"] == "starting":
                # I5: while starting, nothing but the start's own brief is accepted.
                assert (error.reason, error.code) == ("conversation-starting", 7), error
            return
        assert entry["state"] == "started", (entry, out)
        event(f"agent submit to a started conversation: created={out.get('created')}")
        if out.get("created"):
            self.agent_messages.setdefault(cid, []).append(mid)

    @rule(index=st.integers(0, 63), reserved=st.booleans())
    def agent_creates_a_conversation(self, index, reserved):
        """An agent's `conversation.create`, naming a suggestion's old Start request id or not."""
        sid = self._pick(index)
        request = ("suggestion:" + sid) if reserved and sid else str(uuid.uuid4())
        args = {"request_id": request, "provider": "claude", "workspace": str(self.tmp), "settings": SETTINGS}
        try:
            out = self.service.handle("conversation.create", args, AGENT)
        except ConversationError as error:
            assert request.startswith("suggestion:") and error.reason == "reserved-request-id", error
            event("agent create refused: reserved-request-id")
            return
        assert not request.startswith("suggestion:")
        self.agent_made.add(out["conversation"]["conversation_id"])
        event("agent create")

    @rule(index=st.integers(0, 63), by=st.sampled_from([PERSON, AGENT]))
    def dismiss(self, index, by):
        sid = self._pick(index)
        if sid is None:
            return
        before = self.model[sid]["state"]
        try:
            out = self.service.handle("suggestion.dismiss", {"suggestion_id": sid}, by)
        except ConversationError as error:
            if by == AGENT:
                assert error.reason == "person-only"
            else:
                assert before in ("starting", "started") and error.reason == "too-late", (before, error)
            event(f"dismiss refused: {error.reason}")
            return
        assert by == PERSON and before in ("open", *CLOSED)
        event(f"dismiss from {before}")
        assert out["changed"] is (before == "open")
        if before == "open":
            self.model[sid]["state"] = "dismissed"

    @rule(index=st.integers(0, 63), as_creator=st.booleans(), later_turn=st.booleans())
    def withdraw(self, index, as_creator, later_turn):
        sid = self._pick(index)
        if sid is None:
            return
        origin, before = self.model[sid]["origin"], self.model[sid]["state"]
        if as_creator:
            caller = self._caller(origin, 1 if later_turn else 0)
        else:
            others = [o for o in ORIGINS if o != origin and o is not None]
            caller = self._caller(others[index % len(others)])
        creator = as_creator and origin is not None
        try:
            out = self.service.handle("suggestion.withdraw", {"suggestion_id": sid, **caller}, AGENT)
        except ConversationError as error:
            if not creator:
                assert error.reason == "not-creator", error
            else:
                assert before in ("starting", "started") and error.reason == "too-late", (before, error)
            event(f"withdraw refused: {error.reason}")
            return
        assert creator and before in ("open", *CLOSED)
        event(f"withdraw from {before}")
        if before == "open":
            self.model[sid]["state"] = "withdrawn"
        assert out["changed"] is (before == "open")

    @rule()
    def restart(self):
        """The daemon stops and starts: a new service over the same stores."""
        self.service.close()
        self.service = self._service()
        event("restart")

    @precondition(lambda self: any(m["state"] == "starting" for m in self.model.values()))
    @rule()
    def daemon_finishes_claims(self):
        service_module.SUGGESTION_RESUME_AFTER_S = 0.0
        self.service.clock.now += 1000              # past any backoff an earlier failure set
        try:
            self.service._resume_suggestions()
            deadline = time.monotonic() + 60
            while any(value[1] is None for value in self.service._resuming.values()):
                assert time.monotonic() < deadline, "the file pool did not finish the claimed starts"
                time.sleep(0.01)
        finally:
            service_module.SUGGESTION_RESUME_AFTER_S = 30.0
        for sid, entry in self.model.items():
            if entry["state"] == "starting":
                entry["state"] = "started"
                event("the daemon finished a claimed start")

    # --- invariants -------------------------------------------------------------------

    @invariant()
    def agree(self):
        if not getattr(self, "model", None):
            return
        rows = {row["suggestion_id"]: row for row in self.service.store.query("SELECT * FROM suggestions")}
        assert set(rows) == set(self.model)
        started_conversations = []
        for sid, entry in self.model.items():
            row = rows[sid]
            # I6: the store agrees with the model.
            assert row["state"] == entry["state"], (sid, row["state"], entry["state"])
            mid = row["start_message_id"]
            message = self.service.store.find_message(mid) if mid else None
            if row["state"] in ("starting", "started"):
                # I1: the conversation its claim made, the one the model saw.
                assert row["started_conversation_id"] == entry["conversation"] or entry["conversation"] is None
                self.service.store.conversation(row["started_conversation_id"])
                started_conversations.append(row["started_conversation_id"])
                # I2: only the person's verdict claims a start.
                assert row["decided_by"] == PERSON_SAYS
            # I3: closed ones never start, and stay as they closed.
            if row["state"] in CLOSED:
                assert row["started_conversation_id"] is None and mid is None and row["started_message_id"] is None
                assert self.closed_once.setdefault(sid, row["state"]) == row["state"]
                assert row["decided_by"] == (PERSON_SAYS if row["state"] == "dismissed" else "creator")
            # I5, I6: started exactly when the first message exists, and it is the brief.
            assert (row["state"] == "started") == (message is not None)
            if row["state"] == "starting":
                assert self.service.store.messages(row["started_conversation_id"]) == []
            if message is not None:
                messages = self.service.store.messages(row["started_conversation_id"])
                assert messages[0]["message_id"] == mid and messages[0]["seq"] == 1
                assert [m["message_id"] for m in messages[1:]] == self.agent_messages.get(
                    row["started_conversation_id"], [])
                assert self.service.store.message_text(message) == row["brief"] == entry["brief"]
                assert row["started_message_id"] == mid
        assert len(started_conversations) == len(set(started_conversations))
        # I1: every conversation is the fixture's, an agent's, or one a claim made.
        every = {r["conversation_id"] for r in self.service.store.query("SELECT conversation_id FROM conversations")}
        assert every == set(self.conversations.values()) | self.agent_made | set(started_conversations)
        # I7: nothing secret-shaped anywhere a suggestion is kept or shown.
        shown = json.dumps([list(rows.values()), self.service.handle("suggestion.list", {}, AGENT),
                            self.service.store.changes_after(0, limit=1000),
                            [self.service.store.events_after(cid, 0) for cid in self.conversations.values()]],
                           default=str)
        assert not [secret for secret in (*SECRETS, BRIEF_SECRET) if secret in shown]
        # I4: the bounds.
        for origin in ORIGINS:
            assert self._bucket_open(origin) <= PER_ORIGIN
        assert sum(1 for row in rows.values() if row["state"] in ("open", "starting")) <= OPEN_MAX


Suggestions.TestCase.settings = settings(max_examples=300, stateful_step_count=40, deadline=None,
                                         suppress_health_check=[HealthCheck.too_slow])
TestSuggestionMachine = Suggestions.TestCase
