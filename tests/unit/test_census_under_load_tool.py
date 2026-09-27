"""tools/census_under_load.py, the 2026-09-27 reproduction (C-5.5, C-5.12), still runs and still keeps no ps text."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import census_under_load  # noqa: E402


def test_the_reproduction_reads_the_census_both_ways_and_prints_only_numbers(capsys):
    """Both readers read the real `ps -axEww` at least once; the summary is sizes, times, counts and exception types."""
    assert census_under_load.main(["--hogs", "1", "--seconds", "0.5"]) == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert set(summary["readers"]) == {"pipe", "socket"}
    for reader in summary["readers"].values():
        assert reader["n"] >= 1 and reader["chars_p50"] > 0
    assert "SUBFLEET_" not in out and "PATH=" not in out and "HOME=" not in out
