"""Timed floors: independent translation of subfleet-disk-hold's three rules."""
from __future__ import annotations

import json
import math
import os

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.disk import DiskAdmission, GB, RULING_LIMIT, epoch, read_ruling
from subfleet.policy import disk_settings
from tests.unit.test_disk_admission import CLASSES, actions, stamp

from tests.disk_floor_model import Files, LOWER, RAISE, agent_rule, file, lowering, policy


numeric = st.one_of(st.integers(-30, 150), st.sampled_from(["30", "80.5", True, False, None,
                                                         "bad", "NaN", "Infinity", "-Infinity"]))
ruling = st.sampled_from(["Max", "  Max via popup  ", "", "  ", None, 17, False, True])
untils = st.one_of(st.integers(-200000, 200000).map(stamp), st.sampled_from([None, "bad", "2026-10-10T09:00:00"]))
document = st.one_of(
    st.fixed_dictionaries({}, optional={"floor_gb": numeric, "until": untils, "ruling": ruling,
                                       "release_margin_gb": numeric, "why": st.text(max_size=20),
                                       "drop_gb": numeric, "drop_window_min": numeric}),
    st.sampled_from([[], None, "text", 13]))
fake_file = st.one_of(st.none(), st.just(OSError("unreadable")),
                     st.tuples(document, st.integers(-200000, 200000)).map(lambda pair: file(*pair)),
                     st.just((b"not JSON", epoch(stamp(0)))))
PROPERTIES = settings(max_examples=400, deadline=None, derandomize=True)


@PROPERTIES
@given(lower=fake_file, raised=fake_file, now=st.integers(-100000, 200000),
       base=st.integers(0, 120), margin=st.integers(0, 12), minimum=st.integers(0, 40),
       hours=st.integers(1, 32), free=st.integers(0, 200))
def test_F1_agent_differential(lower, raised, now, base, margin, minimum, hours, free):
    cfg = policy(floor_gb=base, resume_margin_gb=margin, min_floor_gb=minimum, max_lower_h=hours)
    files = Files(lower, raised)
    expected_floor, expected_margin, source, reasons = agent_rule(disk_settings(cfg), files, now)
    gate = DiskAdmission("/fake/state", read_free=lambda path: free * GB, read_ruling=files)
    gate.begin_pass(cfg, [], stamp(now))
    gate.hold("background")  # Including the agent's infinities, never overflow.
    # Admission retains the oracle's numbers; only published evidence is text.
    assert gate.settings["floor_gb"] == expected_floor
    assert gate.settings["resume_margin_gb"] == expected_margin
    assert gate.snapshot["floor_gb"] == (expected_floor if math.isfinite(expected_floor) else str(expected_floor))
    assert gate.snapshot["resume_margin_gb"] == (expected_margin if math.isfinite(expected_margin) else str(expected_margin))
    assert gate.snapshot["floor_source"].startswith({"lower": "lowered", "override": "raised", "policy": "policy"}[source])
    assert {key for key in gate.snapshot if key.endswith(("_error", "_expired", "_refused"))} == reasons


def test_snapshot_sanitizes_nested_metadata_without_mutating_ruling():
    why = {"\ud800": [float("nan"), float("inf"), float("-inf"), "\udfff", "é 😀"]}
    gate = DiskAdmission("/fake/state", read_free=lambda path: 34 * GB,
                         read_ruling=Files(file(lowering(why=why))))
    gate.begin_pass(policy(), [], stamp(0))
    assert not gate.holding
    assert gate.snapshot["floor_why"] == {"\ufffd": ["nan", "inf", "-inf", "\ufffd", "é 😀"]}
    json.dumps(gate.snapshot, allow_nan=False, ensure_ascii=False).encode("utf-8")
    original = gate.floor_info["floor_why"]["\ud800"]
    assert math.isnan(original[0]) and original[1:3] == [float("inf"), float("-inf")]
    assert original[3:] == ["\udfff", "é 😀"]


@pytest.mark.parametrize("fields,prior_hold,holding,key", [
    ({"floor_gb": "1e999"}, False, True, "floor_gb"),
    ({"release_margin_gb": "Infinity"}, True, True, "resume_margin_gb"),
])
def test_nonfinite_floor_and_margin_keep_admission_arithmetic(fields, prior_hold, holding, key):
    gate = DiskAdmission("/fake/state", holding=prior_hold, read_free=lambda path: 100 * GB,
                         read_ruling=Files(file(lowering(**fields))))
    gate.begin_pass(policy(), [], stamp(0))
    assert math.isinf(gate.settings[key])
    assert math.isinf(gate.floor_info[key])
    assert gate.snapshot[key] == "inf"
    assert gate.snapshot[key + "_raw"] == next(iter(fields.values()))
    assert gate.holding is holding and gate.hold("background") is not None
    assert gate.hold("attended") is None and gate.hold("probe") is None


@PROPERTIES
@given(now=st.integers(-100000, 200000), base=st.integers(0, 200),
       lower=st.integers(20, 100), raised=st.integers(0, 200),
       margin=st.integers(0, 12), release=st.integers(0, 12), ttl=st.integers(1, 14 * 3600))
def test_F1_both_live_rulings_differential(now, base, lower, raised, margin, release, ttl):
    # Guarantee live pairs as well as the malformed/expired inputs above;
    # generated policy floors expose the agent's raw-raise precedence.
    cfg = policy(floor_gb=base, resume_margin_gb=margin)
    files = Files(file(lowering(lower, now + ttl, release_margin_gb=release), now - 3600),
                  file({"floor_gb": raised, "until": stamp(now + ttl)}, now))
    floor, expected_margin, source, reasons = agent_rule(disk_settings(cfg), files, now)
    gate = DiskAdmission("/fake/state", read_free=lambda path: 100 * GB, read_ruling=files)
    gate.begin_pass(cfg, [], stamp(now))
    assert gate.snapshot["floor_gb"] == floor and gate.snapshot["resume_margin_gb"] == expected_margin
    assert gate.snapshot["floor_source"].startswith("raised" if source == "override" else "lowered")
    assert not reasons


@PROPERTIES
@given(bad=st.one_of(st.just(OSError("denied")), st.binary(max_size=40).map(lambda b: b"\x00" + b),
                    st.sampled_from([[], None, {}, {"floor_gb": "bad", "until": stamp(50)}]).map(file)),
       name=st.sampled_from([LOWER, RAISE]), base=st.integers(0, 100), now=st.integers(0, 1000))
def test_F2_invalid_and_unreadable_never_change_policy(bad, name, base, now):
    files = Files()
    files.files[name] = (bad, epoch(stamp(0))) if isinstance(bad, bytes) else bad
    gate = DiskAdmission("/fake/state", read_free=lambda path: 100 * GB, read_ruling=files)
    gate.begin_pass(policy(floor_gb=base), [], stamp(now))
    gate.hold("priority")
    assert gate.snapshot["floor_gb"] == base
    assert gate.snapshot["resume_margin_gb"] == 5


@PROPERTIES
@given(floor=st.integers(-30, 100), written=st.integers(-100000, 100000),
       until=st.integers(-100000, 200000), now=st.integers(-100000, 200000),
       minimum=st.integers(0, 40), hours=st.integers(1, 32))
def test_F3_lower_bound_and_lifetime(floor, written, until, now, minimum, hours):
    gate = DiskAdmission("/fake/state", read_free=lambda path: 100 * GB,
                         read_ruling=Files(file(lowering(floor, until), written)))
    gate.begin_pass(policy(min_floor_gb=minimum, max_lower_h=hours), [], stamp(now))
    if gate.snapshot["floor_source"].startswith("lowered"):
        assert gate.snapshot["floor_gb"] >= minimum
        assert now < until <= min(now, written) + hours * 3600
    else:
        assert gate.snapshot["floor_gb"] == 40


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory", "oversize"])
def test_reader_rejects_special_and_oversize_files(tmp_path, kind):
    path = tmp_path / "ruling"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b" " * (RULING_LIMIT + 1))
    with pytest.raises(OSError):
        read_ruling(path)
    gate = DiskAdmission(tmp_path, read_free=lambda path: 100 * GB)
    gate.begin_pass(policy(lower_path=str(path), raise_path=None), [], stamp(0))
    assert gate.snapshot["floor_gb"] == 40 and "lower_error" in gate.snapshot


def test_reader_pairs_bytes_with_descriptor_mtime(tmp_path):
    path = tmp_path / "ruling"
    path.write_bytes(b"{}")
    os.utime(path, (epoch(stamp(7)), epoch(stamp(7))))
    assert read_ruling(path) == (b"{}", epoch(stamp(7)))


@pytest.mark.parametrize("written,until,source", [
    (0, 16 * 3600, "lowered"), (0, 16 * 3600 + 1, "policy"),
    (5 * 3600, 17 * 3600, "policy"),  # A future mtime cannot extend the cap from now.
])
def test_lower_duration_boundary_and_future_mtime(written, until, source):
    gate = DiskAdmission("/fake/state", read_free=lambda path: 100 * GB,
                         read_ruling=Files(file(lowering(until=until), written)))
    gate.begin_pass(policy(), [], stamp(0))
    assert gate.snapshot["floor_source"].startswith(source)


def test_deep_json_and_unreadable_mtime_are_errors():
    for lower in ((b"[" * 2000 + b"]" * 2000, epoch(stamp(0))),
                  (json.dumps(lowering()).encode(), float("nan"))):
        gate = DiskAdmission("/fake/state", read_free=lambda path: 100 * GB,
                             read_ruling=Files(lower))
        gate.begin_pass(policy(), [], stamp(0))
        assert gate.snapshot["floor_gb"] == 40 and "lower_error" in gate.snapshot


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "utf-8-sig"])
def test_agent_text_encoding_rejects_non_utf8_and_bom(encoding):
    gate = DiskAdmission("/fake/state", read_free=lambda path: 100 * GB,
                         read_ruling=Files((json.dumps(lowering()).encode(encoding), epoch(stamp(0)))))
    gate.begin_pass(policy(), [], stamp(0))
    assert gate.snapshot["floor_gb"] == 40 and "lower_error" in gate.snapshot


@pytest.mark.parametrize("change", ["ruling", "mtime", "raise"])
def test_mutation_floor_witnesses(change):
    files = Files(file(lowering(until=3 * 3600)))
    if change == "ruling":
        files.files[LOWER] = file(lowering(until=3 * 3600, ruling=""))
    elif change == "mtime":
        files.files[LOWER] = file(lowering(until=17 * 3600), 0)
    else:
        files = Files(raised=file({"floor_gb": 25, "until": stamp(3 * 3600)}))
    gate = DiskAdmission("/fake/state", read_free=lambda path: 100 * GB, read_ruling=files)
    # The mtime witness comes within 16 h of expiry later, but stays refused.
    gate.begin_pass(policy(), [], stamp(2 * 3600))
    assert gate.snapshot["floor_gb"] == 40


def test_latch_recomputed_at_start_and_expiry_even_without_jobs():
    files = Files()
    gate = DiskAdmission("/fake/state", read_free=lambda path: 34 * GB, read_ruling=files)
    gate.begin_pass(policy(), [], stamp(0))
    assert gate.hold("background")
    files.files[LOWER] = file(lowering(until=100))
    gate.begin_pass(policy(), [], stamp(1))
    assert gate.snapshot["floor_gb"] == 30 and gate.snapshot["resume_margin_gb"] == 0
    assert not gate.snapshot["holding"]
    gate.begin_pass(policy(), [], stamp(100))
    assert gate.snapshot["floor_gb"] == 40 and gate.snapshot["resume_margin_gb"] == 5
    assert gate.snapshot["holding"] and "lower_expired" in gate.snapshot


def test_drop_values_do_not_trigger_native_holds():
    files = Files(file(lowering(drop_gb=-100, drop_window_min=10)))
    gate = DiskAdmission("/fake/state", read_free=lambda path: 34 * GB, read_ruling=files)
    gate.begin_pass(policy(), [], stamp(0))
    assert gate.snapshot["floor_gb"] == 30
    assert gate.hold("background") is None


@pytest.mark.parametrize("fields", [{"drop_gb": "bad"}, {"drop_window_min": []}, {"drop_window_min": None}])
def test_malformed_drop_fields_reject_the_lowering_as_in_agent(fields):
    files = Files(file(lowering(**fields)))
    gate = DiskAdmission("/fake/state", read_free=lambda path: 34 * GB, read_ruling=files)
    gate.begin_pass(policy(), [], stamp(0))
    assert gate.snapshot["floor_gb"] == 40 and "lower_error" in gate.snapshot
    assert agent_rule(disk_settings(policy()), files, 0)[0] == 40


def test_disabled_rule_reads_no_ruling_files():
    def forbidden(path):
        raise AssertionError("disabled must not read files")
    gate = DiskAdmission("/fake/state", read_free=forbidden, read_ruling=forbidden)
    gate.begin_pass(policy(enabled=False), [], stamp(0))
    assert gate.hold("background") is None


@settings(max_examples=120, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sequence=actions, floor=st.integers(0, 80), margin=st.integers(0, 12),
       reserve=st.integers(1, 8))
def test_F5_null_paths_match_161_decisions(tmp_path, sequence, floor, margin, reserve):
    """Frozen #161 decision/expiry oracle, with both paths explicitly null."""
    now, free, latch, serial = 0, 39, False, 0
    budgets, attempts = {}, []
    cfg = policy(lower_path=None, raise_path=None, floor_gb=floor,
                 resume_margin_gb=margin, placement_reserve_gb=reserve)

    def forbidden(path):
        raise AssertionError("null paths must not read files")

    gate = DiskAdmission(tmp_path, read_free=lambda path: free * GB, read_ruling=forbidden)
    for action, value in sequence:
        if action == "free":
            free = value / 2
        elif action == "clock":
            now += value
        elif action == "end" and attempts:
            row = attempts[value % len(attempts)]
            row["finished_at"] = stamp(now)
            budgets.pop(row["attempt_id"], None)
        elif action == "restart":
            gate = DiskAdmission(tmp_path, read_free=lambda path: free * GB, holding=latch, read_ruling=forbidden)
        budgets = {aid: expires for aid, expires in budgets.items() if expires > now}
        # #161 does not evaluate its latch until a candidate reaches hold().
        gate.begin_pass(cfg, attempts, stamp(now))
        for klass in CLASSES if action == "submit" else ():
            expected = False
            if klass not in ("attended", "probe"):
                effective = free - reserve * len(budgets)
                latch = (latch and effective < floor + margin) or effective - reserve < floor
                expected = latch
            assert bool(gate.hold(klass)) == expected
            if not expected and klass not in ("attended", "probe"):
                aid = str(serial)
                serial += 1
                attempts.append({"attempt_id": aid, "state": "running", "kind": "dispatch",
                                 "reserved_at": stamp(now), "finished_at": None})
                gate.reserve(aid, stamp(now))
                budgets[aid] = now + 600
        assert gate.reserved_bytes == reserve * GB * len(budgets)


# Review expectations calculated independently from the external agent.
FIDELITY_CASES = [
 ('naive-local-live', {'floor_gb':30,'until':'2026-10-09T13:00:00','ruling':'Max'},None,0,30,0,None),
 ('naive-local-expired', {'floor_gb':30,'until':'2026-10-09T12:00:00','ruling':'Max'},None,0,40,5,'lower_expired'),
 ('until-exact-now-lower',lowering(until=0),None,0,40,5,'lower_expired'),
 ('until-exact-now-raise',None,{'floor_gb':80,'until':stamp(0)},0,40,5,'override_expired'),
 ('future-mtime-capped',lowering(until=17*3600),None,5*3600,40,5,'lower_refused'),
 ('future-mtime-within-cap',lowering(until=16*3600),None,5*3600,30,0,None),
 ('numeric-strings',lowering('30',release_margin_gb='2.5'),None,0,30,2.5,None),
 ('numeric-string-raise',None,{'floor_gb':'80.5','until':stamp(100)},0,80.5,5,None),
 ('lower-nan',lowering('NaN'),None,0,40,5,'lower_refused'),
 ('raise-nan',None,{'floor_gb':'NaN','until':stamp(100)},0,40,5,'override_expired'),
 ('lower-positive-infinity',lowering('Infinity'),None,0,math.inf,0,None),
 ('raise-positive-infinity',None,{'floor_gb':'Infinity','until':stamp(100)},0,math.inf,5,None),
 ('lower-negative-infinity',lowering('-Infinity'),None,0,40,5,'lower_refused'),
 ('raise-negative-infinity',None,{'floor_gb':'-Infinity','until':stamp(100)},0,40,5,None),
 ('numeric-ruling',lowering(ruling=17),None,0,30,0,None),
 ('object-ruling',lowering(ruling={'name':'Max'}),None,0,30,0,None),
 ('false-ruling',lowering(ruling=False),None,0,40,5,'lower_refused'),
 ('equal-raise',lowering(release_margin_gb=1),{'floor_gb':30,'until':stamp(100)},0,30,1,None),
 ('raise-below-policy',lowering(),{'floor_gb':35,'until':stamp(100)},0,35,5,None),
 ('bad-drop-gb',lowering(drop_gb='bad'),None,0,40,5,'lower_error'),
 ('bad-drop-window',lowering(drop_window_min=[]),None,0,40,5,'lower_error'),
 ('null-drop-window',lowering(drop_window_min=None),None,0,40,5,'lower_error'),
 ('null-drop-gb',lowering(drop_gb=None),None,0,30,0,None),
 ('nan-release-margin',lowering(release_margin_gb='NaN'),None,0,30,0,None),
 ('negative-release-margin',lowering(release_margin_gb=-4),None,0,30,0,None),
 ('infinite-release-margin',lowering(release_margin_gb='Infinity'),None,0,30,math.inf,None),
]

@pytest.mark.parametrize("name,lower,raised,written,floor,margin,reason", FIDELITY_CASES,
                         ids=[case[0] for case in FIDELITY_CASES])
def test_review_adversarial_fidelity(monkeypatch, name, lower, raised, written, floor, margin, reason):
    import time
    with monkeypatch.context() as zone:
        zone.setenv("TZ", "America/New_York")
        time.tzset()
        try:
            files = Files(file(lower, written) if lower is not None else None,
                          file(raised) if raised is not None else None)
            gate = DiskAdmission("/fake/state", read_free=lambda path: 100 * GB, read_ruling=files)
            gate.begin_pass(policy(), [], stamp(0))
            assert (gate.settings["floor_gb"], gate.settings["resume_margin_gb"]) == (floor, margin)
            assert gate.snapshot["floor_gb"] == (floor if math.isfinite(floor) else str(floor))
            assert gate.snapshot["resume_margin_gb"] == (margin if math.isfinite(margin) else str(margin))
            if reason:
                assert reason in gate.snapshot
            assert (gate.hold("background") is not None) == (100 - 1.5 < floor)
        finally:
            zone.undo()
            time.tzset()
