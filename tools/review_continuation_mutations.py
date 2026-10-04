"""PR 127 review mutations; foreground children, bounded, always restored."""
import argparse
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    ("ci-environment-pins", "tests/unit/test_claude_conversation_mcp_scope.py",
     '        "SUBFLEET_TURN_JOB": "turn-job", "SUBFLEET_SESSION_ID": SESSION,\n', '',
     "tests/unit/test_claude_conversation_mcp_scope.py"),
    ("ci-ledger", "docs/desktop/ledger.json", '"C-24.10"', '"C-24.1"',
     "tests/unit/test_desktop_ledger.py::test_milestone_9_clauses_are_all_cited"),
    ("ci-history", "subfleet/conversations/history.py",
     'if path is None and conversation["provider"] == "claude" and conversation.get("workspace"):',
     'if False and path is None and conversation["provider"] == "claude" and conversation.get("workspace"):',
     "tests/unit/test_review_pr127_fixes.py::test_history_before_first_catalog_pass"),
    ("pr-first-poll-loop", "subfleet/conversations/wakes.py",
     'return False  # first observation establishes a baseline, never an event',
     'return True  # mutation: old completed snapshots are events',
     "tests/unit/test_review_pr127_pr_wakes.py::test_property_unchanged_watched_pr_never_wakes"),
    ("pr-batch-poison", "subfleet/conversations/wakes.py",
     'errors = body.get("errors") or []',
     'errors = body.get("errors") or []\n    if errors: raise ValueError("batch rejected")',
     "tests/unit/test_review_pr127_pr_wakes.py::test_partial_graphql_error_does_not_silence_other_conversations"),

    ('empty-run-wake', 'subfleet/conversations/wakes.py', 'if not undelivered:', 'if False and not undelivered:', 'tests/unit/test_review_pr127_fixes.py::test_already_announced_run_request_is_satisfied_without_a_wake'),
    ('fanout-per-run', 'subfleet/conversations/wakes.py', 'if j["job_id"] not in covered.get(cid, set())', 'if True', 'tests/unit/test_review_pr127_fixes.py::test_all_of_fanout_has_one_wake_and_one_throttle_charge'),
    ('historical-notice-repair', 'subfleet/conversations/wakes.py', '        rows = self.store.query("SELECT job_id,delivered_at FROM wake_notice_repairs LIMIT 500")', '        with self.store.transaction() as tx:\n            tx.execute("INSERT OR IGNORE INTO wake_notice_repairs SELECT w.job_id,m.created_at FROM wake_runs w JOIN messages m USING(message_id)")\n        rows = self.store.query("SELECT job_id,delivered_at FROM wake_notice_repairs LIMIT 500")', 'tests/unit/test_review_pr127_fixes.py::test_notice_repair_does_no_writes_for_already_delivered_history'),
    ('unindexed-notices', 'subfleet/store_schema.sql', 'CREATE INDEX IF NOT EXISTS notices_job ON notices(job_id, state);', '', 'tests/unit/test_review_pr127_fixes.py::test_notice_job_lookup_uses_an_index'),
    ('unpaced-completions', 'subfleet/conversations/wakes.py', 'scan_completions = self.now() >= self._next_completions', 'scan_completions = True', 'tests/unit/test_review_pr127_service.py::test_control_loop_paces_completion_scans'),
    ('old-upgrade-completions', 'subfleet/conversations/wakes.py', 'AND COALESCE(j.finished_at,j.created_at)>=? AND j.state', 'AND ? IS NOT NULL AND j.state', 'tests/unit/test_review_pr127_fixes.py::test_upgrade_does_not_automatically_announce_old_completions'),
    ('upgrade-drops-inflight-result', 'subfleet/conversations/wakes.py', 'AND COALESCE(j.finished_at,j.created_at)>=? AND j.state', 'AND j.created_at>=? AND j.state', 'tests/unit/test_review_pr127_fixes.py::test_upgrade_keeps_runs_that_complete_after_activation'),
    ('blocking-pr-poll', 'subfleet/conversations/service.py', 'self._moot_blocks, self.wakes.control_tick, self._dispatch', 'self._moot_blocks, self.wakes.tick, self._dispatch', 'tests/unit/test_review_pr127_service.py::test_pr_poll_does_not_hold_person_dispatch'),
    ('open-behind-file-ops', 'subfleet/conversations/service.py', 'return self.history_reads', 'return self.files', 'tests/unit/test_review_pr127_service.py::test_open_is_not_queued_behind_worktree_and_diff'),
    ('unpaced-moot-block', 'subfleet/conversations/service.py', 'if previous and previous[0] == c["blocked_at"]', 'if False and previous and previous[0] == c["blocked_at"]', 'tests/unit/test_review_pr127_service.py::test_moot_block_writer_and_catalog_checks_are_paced'),
    ('codex-session-lost', 'subfleet/conversations/launch.py', '*(() if spec.native_session_id else ("SUBFLEET_SESSION_ID",))', '*("SUBFLEET_SESSION_ID",)', 'tests/unit/test_review_pr127_service.py::test_resumed_codex_launch_preserves_its_own_session_marker'),
    ('wait-does-not-ack', 'subfleet/cli.py', '                        _ack_notices(client, job)', '                        pass', 'tests/unit/test_review_pr127_service.py::test_wait_receipt_ack_prevents_a_second_conversation_wake'),
    ('wait-omits-notices', 'subfleet/daemon.py', '                job["notices"] = self.store.query("SELECT * FROM notices WHERE job_id=? ORDER BY notice_id", (job["job_id"],))', '', 'tests/unit/test_review_pr127_service.py::test_wait_receipt_ack_prevents_a_second_conversation_wake'),
    ('empty-field-drops-pr', 'subfleet/conversations/wakes.py', 'or key in fields:\n', 'or key in fields or not value:\n', 'tests/unit/test_review_pr127_requests.py::test_final_request_forms_are_accepted'),
    ('closeout-drops-request', 'subfleet/conversations/wakes.py', 'if lines and top_level[-1] and re.fullmatch', 'if False and lines and top_level[-1] and re.fullmatch', 'tests/unit/test_review_pr127_requests.py::test_final_request_forms_are_accepted'),
    ('bad-line-drops-valid', 'subfleet/conversations/wakes.py', 'self.service.log.warning("wake request in final text of %s refused: %s", mid, exc)', 'self.service.log.warning("wake request in final text of %s refused: %s", mid, exc)\n                return', 'tests/unit/test_review_pr127_requests.py::test_bad_final_line_does_not_discard_the_valid_line'),
    ('timer-settlement-floor', 'subfleet/conversations/wakes.py', 'now=min(self.now(), validation_time)', 'now=self.now()', 'tests/unit/test_review_pr127_requests.py::test_timer_floor_is_checked_at_turn_start'),
    ('synthetic-prompt-boundary', 'subfleet/conversations/history.py', 'if owned is not None and kind == "user" and (row.get("uuid") in owned or _claude_prompt(row, blocks)):', 'if owned is not None and kind == "user" and any(b.get("type") == "text" for b in blocks):', 'tests/unit/test_review_pr127_history.py::test_synthetic_user_row_preserves_owned_turn'),
    ('hidden-wake-refusal', 'app/Sources/Timeline.swift', 'if data["phase"]?.string == "wake-refused", let detail', 'if false, data["phase"]?.string == "wake-refused", let detail', 'tests/frontend/test_review_pr127_timeline.py::test_invalid_wake_request_is_visible_in_the_timeline'),
    ('bulleted-final-line', 'subfleet/conversations/wakes.py', 'line = re.sub(r"^ {0,3}(?:[-*] )?", "", line)', 'line = line  # mutation: preserve bullet', 'tests/unit/test_review_pr127_requests.py::test_final_request_forms_are_accepted'),
    ('bold-final-line', 'subfleet/conversations/wakes.py', 'if line.startswith("**WAKE-ME:"):', 'if False and line.startswith("**WAKE-ME:"):', 'tests/unit/test_review_pr127_requests.py::test_final_request_forms_are_accepted'),
    ('fenced-request-injection', 'subfleet/conversations/wakes.py', 'normalized = _wake_line(line) if allowed else None', 'normalized = _wake_line(line)', 'tests/unit/test_review_pr127_requests.py::test_only_top_level_final_requests_are_accepted'),
    ('moot-block-report-overclaim', 'docs/reports/2026-10-04-continuation-pr-body.md', 'catalog transcript mtime', 'gained turns', 'tests/unit/test_review_pr127_report.py::test_moot_block_report_uses_the_implemented_mtime_criterion'),
    ('expanded-grammar-replays-timer', 'subfleet/conversations/wakes.py', 'request_id = legacy_ids.get(position, new_ids[position])', 'request_id = f"final:{mid}:{index}"', 'tests/unit/test_review_pr127_requests.py::test_expanded_grammar_replay_preserves_legacy_timer_identity'),
]


def run_case(case):
    name, relative, before, after, node = case
    (ROOT / "build/review/pytest").mkdir(parents=True, exist_ok=True)
    path = ROOT / relative
    original = path.read_text()
    assert original.count(before) == 1, (name, "anchor must be unique")
    try:
        path.write_text(original.replace(before, after))
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPYCACHEPREFIX", None)
        child = subprocess.Popen([sys.executable, "-m", "pytest", "-q", "--tb=short", node, "--basetemp", str(ROOT / "build/review/pytest" / name)],
                                 cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True)
        active = ROOT / "build/review/active-mutation.json"
        active.write_text(json.dumps({"pid": child.pid, "root": str(ROOT), "test": node, "mutation": name}))
        try:
            out, _ = child.communicate(timeout=180)
        except BaseException:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise
        finally:
            active.unlink(missing_ok=True)
        summary = out.strip().splitlines()[-1]
        killed = child.returncode == 1 and bool(re.search(r"\b[1-9][0-9]* failed\b", summary)) and "error" not in summary
        result = {"mutation": name, "test": node, "killed": killed,
                  "result": out.strip().splitlines()[-1], "output": out}
        print(json.dumps({k: v for k, v in result.items() if k != "output"}), flush=True)
        return result
    finally:
        path.write_text(original)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--report", default="docs/reports/2026-10-04-continuation-review-mutations.json")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    cases = [c for c in MUTATIONS if not args.only or c[0] in args.only]
    assert cases
    results = []
    report = ROOT / args.report
    for case in cases:
        result = run_case(case)
        results.append(result)
        previous = json.loads(report.read_text()) if report.exists() else []
        report.write_text(json.dumps([r for r in previous if r["mutation"] != result["mutation"]] + [result], indent=2) + "\n")
    return int(not all(r["killed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
