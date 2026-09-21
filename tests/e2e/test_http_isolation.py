"""Periodic fake-provider checks stay local even during long release benchmarks."""

import json


def test_periodic_codex_usage_uses_local_transport_and_keeps_lanes_enabled(e2e):
    harness = e2e
    harness.policy_update(lambda policy: policy.setdefault('timers', {}).update(probe_interval_s=1))
    try:
        harness.start()
        harness.until(lambda: len(harness.rows(
            "SELECT DISTINCT lane_id FROM readings WHERE lane_id LIKE 'codex-%' "
            "AND source='wham' AND label='provider'")) == 2, timeout=20)
        assert harness.rows("SELECT lane_id FROM lanes WHERE provider='codex' AND enabled=0") == []
        verdicts = harness.rows("SELECT data_json FROM events WHERE kind='timer.verdict' "
                               "AND lane_id LIKE 'codex-%' AND data_json!='{}'")
        assert verdicts and all(json.loads(row['data_json'])['probe_status'] == 'ok' for row in verdicts)
        assert 'real HTTP transport' not in harness.log_text()
    finally:
        harness.close()
