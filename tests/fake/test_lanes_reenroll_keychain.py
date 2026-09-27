"""C-10.2 through the daemon: re-enrolling a keychain-token Claude lane (review of 664a6f19).

The unit tests exercise `enrollment_runner`; this drives `_enroll_lane_locked` with a
real `ClaudeAdapter`, so the daemon's own wiring is covered: the old all-calls lambda
fails here with the live TypeError, and a wiring with no fence fails the fenced count.
"""
import subprocess

from subfleet.adapters import registry
from subfleet.adapters.claude import ClaudeAdapter
from tests.fake.test_lanes_enroll import core  # noqa: F401 - the fixture
from tests.unit.test_claude_adapter import NOW, _enroll_stream, _Runner, profile_opener
from tests.unit.test_enrollment_runner import _info

REF = "claude-quota-max@axiom.org"


def test_c10_2_the_daemon_reenrolls_a_keychain_lane_with_only_the_turn_fenced(core, tmp_path, monkeypatch):
    daemon, _ = core
    runner = _Runner(stdout=_enroll_stream(info=_info()))
    monkeypatch.setitem(registry._factories, "claude", lambda: ClaudeAdapter(
        runner=runner, now=lambda: NOW, projects_dir=tmp_path, profile_opener=profile_opener()))
    first = daemon.dispatch("lanes", {"action": "enroll", "credential": REF})["enrolled"]
    daemon.store.update_lane(first["lane_id"], enabled=0)          # as auth-dead leaves it
    runner.calls.clear()
    fenced = []

    def turn(lane_id, holder, argv, *, cwd, env, timeout, **_):   # `_enrollment_turn`'s signature
        fenced.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, _enroll_stream(info=_info()), "")
    monkeypatch.setattr(daemon, "_enrollment_turn", turn)
    row = daemon.dispatch("lanes", {"action": "enroll", "credential": REF})["enrolled"]
    assert row["enabled"] and row["lane_id"] != first["lane_id"]
    assert len(fenced) == 1 and fenced[0][0] == "claude"
    assert runner.calls and all(c[1:2] == ["get"] or c[0].endswith("security") for c in runner.calls)
