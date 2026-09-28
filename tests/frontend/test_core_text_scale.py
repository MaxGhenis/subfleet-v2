"""Readable conversation text at one scale (C-29.13): app/Sources/TextScale.swift.

Invariants, for every scale including ones that are not numbers:
- the scale in use is within 1.2^-1..1.2^5 (83 %..249 %); a value outside is
  its nearest bound and one that is not a number is actual size;
- Bigger and Smaller step as Claude Code's ⌘+ and ⌘− zoom does, half a zoom
  level (×1.2^½) at a time: they land on a step, never go the wrong way, and
  undo each other between steps;
- every reading size grows with the scale, is at least 10 pt and a whole or
  half point; metadata (caption, footnote) stays smaller than body, body
  smaller than headings, and code between caption and body;
- the conversation column and the message bubble widen in proportion to body;
- at actual size, body is 16 pt: larger than the system's 13 pt body.
"""

from __future__ import annotations

import json
import math
import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import pytest

from tests.frontend.conftest import needs_swift, run_probe

pytestmark = needs_swift

FIXTURE_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
ORDER = ["footnote", "caption", "secondary", "body", "subheading", "heading", "title"]
STEPS = [1.2 ** (k / 2) for k in range(-2, 11)]
LOW, HIGH = STEPS[0], STEPS[-1]


def scale_probe(core_probe, tmp_path, command="text-scale", **payload) -> dict:
    path = tmp_path / f"{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(payload))
    return run_probe(core_probe, command, path)


def encode(value: float):
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return value


def test_c29_13_the_setting_its_steps_and_range(core_probe, tmp_path):
    out = scale_probe(core_probe, tmp_path)
    assert out["key"] == "conversationTextScale"
    assert out["actual"] == 1.0
    assert out["steps"] == pytest.approx(STEPS, rel=1e-12)
    assert 1.0 in out["steps"], "actual size is a step"
    assert out["range"] == pytest.approx([LOW, HIGH], rel=1e-12)
    assert out["minimum_point_size"] == 10
    # Claude Code at 158 % (where Max reads it) is five presses of ⌘+.
    assert out["steps"][out["steps"].index(1.0) + 5] == pytest.approx(1.5774, abs=1e-4)


def test_c29_13_bigger_smaller_and_actual_size(core_probe, tmp_path):
    values = [1.0, "nan", "inf", "-inf", 0, -3, 5, 1.3, LOW, HIGH, STEPS[3]]
    out = scale_probe(core_probe, tmp_path, values=values)["values"]
    got = dict(zip(map(str, values), out))
    assert got["1.0"]["clamp"] == 1.0 and got["1.0"]["is_actual"]
    assert got["1.0"]["bigger"] == pytest.approx(STEPS[3]) and got["1.0"]["smaller"] == pytest.approx(STEPS[1])
    for garbage in ("nan", "inf", "-inf"):
        assert got[garbage]["clamp"] == 1.0 and got[garbage]["is_actual"]
    assert got["0"]["clamp"] == pytest.approx(LOW) and got["-3"]["clamp"] == pytest.approx(LOW)
    assert got["5"]["clamp"] == pytest.approx(HIGH)
    assert got[str(LOW)]["smaller"] == pytest.approx(LOW) and not got[str(LOW)]["can_reduce"]
    assert got[str(HIGH)]["bigger"] == pytest.approx(HIGH) and not got[str(HIGH)]["can_enlarge"]
    # A stored value between steps (a hand edit) keeps its size and steps to its neighbours.
    assert got["1.3"]["clamp"] == 1.3 and not got["1.3"]["is_actual"]
    assert got["1.3"]["bigger"] == pytest.approx(1.2 ** 1.5) and got["1.3"]["smaller"] == pytest.approx(1.2)
    walk, value = [], 1.0
    for _ in range(12):
        value = scale_probe(core_probe, tmp_path, values=[value])["values"][0]["bigger"]
        walk.append(value)
    assert walk == pytest.approx(STEPS[3:] + [HIGH] * 2)


@settings(max_examples=40, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(st.lists(st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.floats(0.5, 3.0), st.sampled_from(STEPS)),
                min_size=1, max_size=40))
def test_c29_13_step_properties(core_probe, tmp_path, values):
    out = scale_probe(core_probe, tmp_path, values=[encode(v) for v in values])
    steps, (low, high) = out["steps"], out["range"]
    for value, got in zip(values, out["values"]):
        clamp = got["clamp"]
        assert low <= clamp <= high
        if math.isfinite(value):
            assert clamp == min(max(value, low), high)
        else:
            assert clamp == 1.0
        assert got["bigger"] in steps and got["smaller"] in steps
        assert got["bigger"] >= clamp and got["smaller"] <= clamp
        assert got["bigger"] > clamp or clamp == high
        assert got["smaller"] < clamp or clamp == low
        assert got["can_enlarge"] == (clamp < high - 0.001) and got["can_reduce"] == (clamp > low + 0.001)
    # Between steps, Bigger then Smaller (and the reverse) come back.
    there = scale_probe(core_probe, tmp_path, values=steps)["values"]
    back_down = scale_probe(core_probe, tmp_path, values=[v["bigger"] for v in there[:-1]])["values"]
    back_up = scale_probe(core_probe, tmp_path, values=[v["smaller"] for v in there[1:]])["values"]
    assert [v["smaller"] for v in back_down] == steps[:-1]
    assert [v["bigger"] for v in back_up] == steps[1:]


def test_c29_13_the_setting_persists_in_user_defaults(core_probe, tmp_path):
    suite = f"org.maxghenis.subfleet.probe.{uuid.uuid4().hex}"
    out = scale_probe(core_probe, tmp_path, suite=suite, stored=["big", True, 9.5, 0.1, 1.5, 1, None])["persisted"]
    assert out["missing"] == 1.0
    assert out["saved"] == 1.25 and out["raw_after_save"] == 1.25
    assert out["saved_too_large"] == pytest.approx(HIGH)
    # Not a number (a string, a Bool) reads as actual size; out of range as the nearest bound.
    assert out["stored"] == pytest.approx([1.0, 1.0, HIGH, LOW, 1.5, 1.0, 1.0])


def test_c29_13_reading_sizes_at_actual_size(core_probe, tmp_path):
    out = scale_probe(core_probe, tmp_path, scales=[1.0])
    [entry] = out["sizes"]
    assert entry["sizes"] == {"title": 24, "heading": 20, "subheading": 17, "body": 16, "secondary": 15, "code": 14.5,
                              "caption": 13, "footnote": 12}
    assert entry["sizes"]["body"] >= 13 + 3, "noticeably larger than the system's 13 pt body"
    assert entry["column"] == 896 and entry["bubble"] == 640, "the column the app had, at actual size"
    assert out["weights"] == {"title": "bold", "heading": "bold", "subheading": "semibold", "body": "regular",
                              "secondary": "regular", "code": "regular", "caption": "regular", "footnote": "regular"}


@settings(max_examples=30, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(st.lists(st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.floats(0.5, 3.0), st.sampled_from(STEPS)),
                min_size=2, max_size=40))
def test_c29_13_reading_size_properties(core_probe, tmp_path, scales):
    out = scale_probe(core_probe, tmp_path, scales=[encode(s) for s in scales])
    clamped = []
    for scale, entry in zip(scales, out["sizes"]):
        sizes = entry["sizes"]
        clamped.append((min(max(scale, LOW), HIGH) if math.isfinite(scale) else 1.0, entry))
        for style, size in sizes.items():
            assert size >= out["minimum_point_size"], style
            assert (size * 2) == int(size * 2), "whole or half points"
        ordered = [sizes[style] for style in ORDER]
        assert ordered == sorted(ordered), sizes
        assert sizes["caption"] < sizes["body"] < sizes["title"]
        assert sizes["caption"] <= sizes["code"] <= sizes["body"]
        assert entry["column"] == sizes["body"] * 56 and entry["bubble"] == sizes["body"] * 40
    # Every size and width grows (or stays) as the scale grows.
    clamped.sort(key=lambda pair: pair[0])
    for (_, smaller), (_, larger) in zip(clamped, clamped[1:]):
        for style in ORDER + ["code"]:
            assert smaller["sizes"][style] <= larger["sizes"][style], style
        assert smaller["column"] <= larger["column"]


def test_c29_8_code_blocks_show_more_lines_a_step_at_a_time(core_probe, tmp_path):
    pairs = [[40, 100], [40, 1000], [440, 1000], [840, 1000], [40, 40], [40, 41], [0, 10], [10, 5000]]
    out = scale_probe(core_probe, tmp_path, "code-expansion", **{"pairs": []})
    assert out["code_lines"] == 40 and out["step"] == 400
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(pairs))
    expanded = run_probe(core_probe, "code-expansion", path)["expanded"]
    assert expanded == [100, 440, 840, 1000, 40, 41, 10, 440]
    # From the first 40 lines, every line of a block of n lines is reachable in
    # ceil((n - 40) / 400) presses, and no press shows fewer lines.
    for total in (41, 400, 441, 5000):
        shown, presses = 40, 0
        while shown < total:
            path.write_text(json.dumps([[shown, total]]))
            [after] = run_probe(core_probe, "code-expansion", path)["expanded"]
            assert shown < after <= shown + 400
            shown, presses = after, presses + 1
        assert presses == math.ceil((total - 40) / 400)
