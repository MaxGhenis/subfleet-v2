"""C-24.7, C-26.5, C-26.9: the turn runner's clocks come from policy.

The runner's timer step is driven directly with a controlled clock; nothing is
sent (the relay is never reached), so these check only what each step queues.
"""

from __future__ import annotations

import pytest

from subfleet.conversations.runner import Clocks, TurnRunner
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.turn import TurnSpec

MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make_runner(tmp_path):
    store = ConversationStore(tmp_path / "state")
    contained: list[str] = []

    def make(clocks: Clocks):
        clock = Clock()
        spec = TurnSpec(provider="claude", message_id=MID, text="hi", model_id="opus[1m]", permission="ask",
                        native_session_id=None, new_session_id=SID)
        runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                            conversation_id="cv-x", attempt_dir=tmp_path / "a1",
                            control_socket=str(tmp_path / "none.sock"), on_outcome=lambda r: None,
                            on_contain=contained.append, clocks=clocks, clock=clock)
        return runner, clock, contained

    yield make
    store.close()


def queued(runner) -> list[str]:
    return [frame.tag for frame in runner.outbox]


def test_the_stop_escalation_follows_the_policy_clocks(make_runner):
    """C-24.7: SIGINT, then closing stdin, then containment, each at its configured delay
    after the stop request, and nothing before it."""
    runner, clock, contained = make_runner(Clocks(sigint_after_s=1, close_after_s=2, contain_after_s=3))
    runner.stop_at = clock.now
    clock.now += 0.99
    runner._timers()
    assert queued(runner) == [] and contained == []
    clock.now += 0.02                      # 1.01 s
    runner._timers()
    assert queued(runner) == ["signal:int"]
    clock.now += 1.0                       # 2.01 s
    runner._timers()
    assert queued(runner) == ["signal:int", "close"] and contained == []
    clock.now += 1.0                       # 3.01 s
    runner._timers()
    runner._timers()
    assert contained == ["job/a1"]         # once


def test_the_default_clocks_are_the_contract_defaults(make_runner):
    """C-24.7, C-26.5, C-26.9: 10, 20 and 30 s after a stop; 135 s after the terminal event;
    3600 s for an approval."""
    assert Clocks() == Clocks(sigint_after_s=10, close_after_s=20, contain_after_s=30, after_result_s=135,
                              approval_wait_s=3600)
    runner, clock, contained = make_runner(Clocks())
    runner.stop_at = clock.now
    clock.now += 9.9
    runner._timers()
    assert queued(runner) == []
    clock.now += 0.2
    runner._timers()
    assert queued(runner) == ["signal:int"]


def test_a_process_outliving_its_terminal_event_is_stopped_after_the_configured_time(make_runner):
    """C-26.5: SIGINT `after_result_s` after the terminal event, containment
    `contain - sigint` seconds later."""
    runner, clock, contained = make_runner(Clocks(sigint_after_s=1, close_after_s=2, contain_after_s=4,
                                                  after_result_s=5))
    runner.ended_at = clock.now
    clock.now += 4.9
    runner._timers()
    assert queued(runner) == []
    clock.now += 0.2
    runner._timers()
    assert queued(runner) == ["signal:int:late"] and contained == []
    clock.now += 3.1
    runner._timers()
    assert contained == ["job/a1"]


def test_an_unanswered_approval_stops_the_turn_after_the_configured_wait(make_runner):
    """C-26.9, IR-8: the turn is stopped with reason approval-timeout; the approval is not answered."""
    runner, clock, contained = make_runner(Clocks(approval_wait_s=2))
    runner.approval_seen["perm-1"] = clock.now
    clock.now += 1.9
    runner._timers()
    assert runner.commands.empty()
    clock.now += 0.2
    runner._timers()
    assert runner.commands.get_nowait() == ("interrupt",) and runner.stop_reason == "approval-timeout"
