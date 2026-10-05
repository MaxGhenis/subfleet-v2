"""C-16.7 on the desktop line: the reserve grows with running conversation turns,
and the conversation ops that only read are dropped for a client that has left.

The desktop line serves the conversation ops (C-25.2) on the same socket as the
job ops. Each running turn's runner also holds descriptors of its own: its relay
connection to the guardian for the turn's whole life (`relay.RelayClient`,
C-26.4) and its stdout while it reads a chunk. These state the invariants the
connection cap and the read-only classification must keep, for every input.
"""

from __future__ import annotations

import resource

from hypothesis import given, settings, strategies as st

from subfleet import descriptors, protocol

INF = resource.RLIM_INFINITY

#: The daemon's own ops (C-16.2) that only read, as C-16.7 lists them. `ping`
#: reads only without text, `lanes` only when it lists; both are argument-dependent.
DAEMON_READS = {"list", "show", "wait", "readings", "why", "pick", "daemon.status", "notice.pending"}
#: The daemon's own ops that write something a caller relies on.
DAEMON_WRITES = {"submit", "kill", "notice.ack", "notice.mark", "gate.start", "gate.poll", "gate.continue",
                 "sessions", "operations"}
#: C-16.7: every conversation op (C-25.2) that only reads. `conversation.open` reads
#: only when it names a conversation; opened by native session it may create one.
CONVERSATION_READS = {"capabilities", "models.list", "conversation.list", "conversation.history",
                      "conversation.events", "conversation.watch", "conversation.runs", "message.status",
                      "approval.list", "approval.get", "turn.diff", "conversation.diff", "workspace.check"}
#: The conversation ops that write something a caller relies on: a conversation,
#: its title, a message, a steer, a stop, an answer, an attachment, a catalog run,
#: a handoff.
CONVERSATION_WRITES = {"conversation.create", "conversation.settings", "conversation.unblock",
                       "conversation.rename", "message.submit", "message.steer", "message.cancel",
                       "turn.interrupt", "message.resolve", "approval.respond", "attachment.add",
                       "catalog.refresh", "conversation.handoff"}


def test_c16_7_every_conversation_op_is_classified_once():
    """Completeness: a conversation op added to the protocol must be judged a read or
    a write here before this passes, since a read is dropped for a departed client."""
    ops = set(protocol.CONVERSATION_OPS)
    assert CONVERSATION_READS | CONVERSATION_WRITES | {"conversation.open"} == ops
    assert not CONVERSATION_READS & CONVERSATION_WRITES
    assert descriptors.READ_ONLY_CONVERSATION_OPS == CONVERSATION_READS


def test_c16_7_read_only_matches_the_classification_for_every_op():
    for op in CONVERSATION_READS | DAEMON_READS:
        assert descriptors.read_only(op, {}), op
    for op in CONVERSATION_WRITES | DAEMON_WRITES:
        assert not descriptors.read_only(op, {}), op
        assert not descriptors.read_only(op, {"conversation_id": "c1", "text": ""}), op


def test_c16_7_a_ping_reads_unless_its_text_has_more_than_whitespace():
    assert descriptors.read_only("ping", {})
    assert descriptors.read_only("ping", {"text": "   "})
    assert not descriptors.read_only("ping", {"text": "a notice"})


def test_c16_7_conversation_open_reads_only_when_it_names_a_conversation():
    assert descriptors.read_only("conversation.open", {"conversation_id": "c1"})
    assert not descriptors.read_only("conversation.open", {"native": {"provider": "claude", "session_id": "s"}})
    assert not descriptors.read_only("conversation.open", {"conversation_id": ""})
    assert not descriptors.read_only("conversation.open", {})


@given(op=st.sampled_from(sorted(CONVERSATION_READS | DAEMON_READS | CONVERSATION_WRITES | DAEMON_WRITES
                                 | {"ping", "lanes", "conversation.open"})),
       args=st.dictionaries(st.sampled_from(["text", "action", "conversation_id", "native", "wait_s"]),
                            st.one_of(st.none(), st.text(max_size=4), st.integers(0, 3))))
@settings(max_examples=400, deadline=None)
def test_c16_7_property_only_the_argument_dependent_ops_depend_on_their_arguments(op, args):
    """`read_only` is a function of the op alone, except for the three C-16.7 names:
    `ping` (a text that is more than whitespace records a notice, C-15.8), `lanes`
    (an action other than a listing) and
    `conversation.open` (by native session it may create a conversation)."""
    judged = descriptors.read_only(op, args)
    if op == "ping":
        # C-15.8: text that is only whitespace, or not a string, records nothing.
        assert judged == (not protocol.ping_writes(args))
    elif op == "lanes":
        assert judged == (args.get("action") in (None, "", "list"))
    elif op == "conversation.open":
        assert judged == bool(args.get("conversation_id"))
    else:
        assert judged == (op in CONVERSATION_READS | DAEMON_READS)


# --- the reserve and running turns ---------------------------------------------

def reference_cap(soft: int, turns: int) -> int:
    """The C-16.7 formula as the contract writes it."""
    if soft == INF:
        return 512
    return max(4, min(512, (soft - 64 - 2 * turns) // 2))


@given(soft=st.one_of(st.integers(0, 70_000), st.just(INF)), turns=st.integers(0, 2_000))
@settings(max_examples=2_000, deadline=None)
def test_c16_7_property_the_cap_leaves_the_reserve_and_each_running_turn_its_own(soft, turns):
    """For every soft limit and number of running turns:

    - the cap is within [CONNECTIONS_FLOOR, CONNECTIONS_CEILING];
    - it agrees with the contract's formula (differential);
    - once the limit leaves room for the floor, two descriptors per connection, the
      reserve and TURN_DESCRIPTORS per turn fit inside the limit;
    - no turns is #43's cap exactly.
    """
    cap = descriptors.max_connections(soft, live_turns=turns)
    assert descriptors.CONNECTIONS_FLOOR <= cap <= descriptors.CONNECTIONS_CEILING
    assert cap == reference_cap(soft, turns)
    reserve = descriptors.DESCRIPTOR_RESERVE + descriptors.TURN_DESCRIPTORS * turns
    if soft != INF and soft >= reserve + 2 * descriptors.CONNECTIONS_FLOOR:
        assert 2 * cap + reserve <= soft
    assert descriptors.max_connections(soft, live_turns=0) == descriptors.max_connections(soft)


@given(soft=st.integers(0, 70_000), turns=st.integers(0, 2_000), more=st.integers(1, 50),
       grow=st.integers(1, 5_000))
@settings(max_examples=1_000, deadline=None)
def test_c16_7_property_the_cap_falls_as_turns_start_and_rises_with_the_limit(soft, turns, more, grow):
    """Monotone: more running turns never raise the cap; a higher limit never lowers it."""
    cap = descriptors.max_connections(soft, live_turns=turns)
    assert descriptors.max_connections(soft, live_turns=turns + more) <= cap
    assert descriptors.max_connections(soft + grow, live_turns=turns) >= cap


def test_c16_7_turns_at_launchds_default_limit():
    assert descriptors.max_connections(256) == 96
    assert descriptors.max_connections(256, live_turns=3) == 93          # policy's max_active_turns
    assert descriptors.max_connections(65536, live_turns=40) == descriptors.CONNECTIONS_CEILING
    assert descriptors.max_connections(INF, live_turns=10**6) == descriptors.CONNECTIONS_CEILING
    assert descriptors.max_connections(256, live_turns=-5) == 96          # a count is never negative
