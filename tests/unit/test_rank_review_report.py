"""PR-body regressions: the replay and absent sensor must be described honestly."""
from pathlib import Path

REPORT = Path(__file__).resolve().parents[2] / 'docs/reports/2026-10-03-rank-earliest-reset.md'


def test_report_discloses_uncalibrated_replay_and_three_attempt_gain():
    text = REPORT.read_text()
    for fact in ('not calibrated', '232/3,188', '7.2773%', '714/3,188', '22.3965%',
                 '805/3,188', 'three attempts, 0.0941 percentage points',
                 'Claude worsens 155→159', 'Codex improves 77→70'):
        assert fact in text, fact


def test_report_limits_five_hour_results_to_the_observed_provider():
    assert 'five-hour column covers Claude only' in REPORT.read_text()


def test_report_discloses_missing_sensor_and_limits_freshness_claim():
    text = REPORT.read_text()
    for fact in ('no Claude freshness gain', 'zero successful Claude usage reads',
                 'HTTP 403', 'HTTP 429', '3,600-second Retry-After',
                 'Current Claude freshness comes from attempt-end events',
                 'metrics describe the historical implementation'):
        assert fact in text, fact
