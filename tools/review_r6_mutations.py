"""PR #127 r6 mutations; each foreground check restores its changed source."""
import signal
import sys

from review_r5_mutations import run_case

TESTS = "tests/unit/test_review_r6_restart.py::"
MUTATIONS = [
    ("evaluate-before-replay", "service.py",
     "self._moot_blocks, self._replay_final_wakes,",
     "self._moot_blocks,",
     "test_tick_replays_all_recorded_finals_before_first_wake_evaluation"),
    ("ignore-unresolved-intents", "wakes.py",
     '    if tx.execute("SELECT 1 FROM final_wake_intents i JOIN messages m USING(message_id) "\n'
     '                  "WHERE m.conversation_id=? LIMIT 1", (cid,)).fetchone():\n'
     '        return False\n',
     "",
     "test_failed_replay_defers_only_the_conversation_with_an_intent"),
    ("skip-claim-recheck", "wakes.py",
     'if not eligible(tx, cid, data["now"]):', "if False:",
     "test_claim_rechecks_intents_committed_after_candidate_read"),
    ("replay-newest-first", "wakes.py",
     "ORDER BY m.conversation_id,m.seq", "ORDER BY m.conversation_id,m.seq DESC",
     "test_recorded_finals_replay_in_message_order_after_partial_registration"),
]


def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    selected = sys.argv[1:]
    assert all(name in {case[0] for case in MUTATIONS} for name in selected), selected
    cases = [case for case in MUTATIONS if not selected or case[0] in selected]
    return int(not all([run_case(case, tests=TESTS) for case in cases]))


if __name__ == "__main__":
    raise SystemExit(main())
