"""Timed floors: independent translation of subfleet-disk-hold's three rules."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.disk import DiskAdmission, GB, RULING_LIMIT, epoch, read_ruling
from subfleet.policy import disk_settings
from tests.unit.test_disk_admission import CLASSES, actions, stamp

LOWER, RAISE = "/fake/lower", "/fake/raise"


class Files:
    def __init__(self, lower=None, raised=None):
        self.files = {LOWER: lower, RAISE: raised}
        self.reads = []

    def __call__(self, path):
        self.reads.append(path)
        value = self.files[path]
        if value is None:
            raise FileNotFoundError(path)
        if isinstance(value, Exception):
            raise value
        return value


def file(value, written=0):
    return json.dumps(value).encode(), epoch(stamp(written))


def lowering(floor=30, until=3600, ruling="Max", **extra):
    return {"floor_gb": floor, "until": stamp(until), "ruling": ruling, **extra}


def agent_rule(cfg, files, seconds):
    """Oracle copied independently from floor_gb, lower_override, pass_once.

    Return floor, margin, winning source and ignored reason codes. Only the
    agent's top-level non-object crash is totalized into an error here.
    """
    now = datetime.fromisoformat(stamp(seconds).replace("Z", "+00:00"))
    lo, raised, reasons = None, None, set()
    for name, path in (("lower", cfg["lower_path"]), ("override", cfg["raise_path"])):
        if path is None:
            continue
        try:
            data, mtime = files(path)
            ov = json.loads(data)
            until = datetime.fromisoformat(str(ov["until"]).replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.astimezone()
            floor = float(ov["floor_gb"])
            if name == "lower":
                ruling = str(ov.get("ruling") or "").strip()
                margin = max(0.0, float(ov.get("release_margin_gb", 0.0)))
                # Agent validates these, although the native rule ignores them.
                if ov.get("drop_gb") is not None:
                    float(ov["drop_gb"])
                float(ov.get("drop_window_min", 10.0))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            reasons.add(name + "_error")
            continue
        if until <= now or (name == "override" and floor != floor):
            reasons.add(name + "_expired")
            continue
        if name == "lower":
            written = datetime.fromtimestamp(mtime, timezone.utc)
            if ((until - min(now, written)).total_seconds() > cfg["max_lower_h"] * 3600
                    or not ruling or not floor >= cfg["min_floor_gb"]):
                reasons.add("lower_refused")
                continue
            lo = (floor, margin)
        else:
            raised = floor
    floor = cfg["floor_gb"] if raised is None else max(cfg["floor_gb"], raised)
    margin = cfg["resume_margin_gb"]
    source = "override" if raised is not None and raised > cfg["floor_gb"] else "policy"
    if lo is not None:
        if raised is not None and raised > lo[0]:
            floor, source = raised, "override"
        else:
            floor, margin = lo
            source = "lower"
    return floor, margin, source, reasons


def policy(**settings):
    return {"admission": {"disk": {"enabled": True, "lower_path": LOWER, "raise_path": RAISE, **settings}}}


numeric = st.one_of(st.integers(-30, 150), st.sampled_from(["30", "80.5", True, False, None,
                                                         "bad", "NaN", "Infinity", "-Infinity"]))
ruling = st.sampled_from(["Max", "  Max via popup  ", "", "  ", None, 17, False, True])
untils = st.one_of(st.integers(-200000, 200000).map(stamp), st.sampled_from([None, "bad", "2026-10-10T09:00:00"]))
document = st.one_of(
    st.fixed_dictionaries({}, optional={"floor_gb": numeric, "until": untils, "ruling": ruling,
                                       "release_margin_gb": numeric, "why": st.text(max_size=20),
                                       "drop_gb": st.integers(0, 20), "drop_window_min": st.integers(1, 20)}),
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
    assert gate.snapshot["floor_gb"] == expected_floor
    assert gate.snapshot["resume_margin_gb"] == expected_margin
    assert gate.snapshot["floor_source"].startswith({"lower": "lowered", "override": "raised", "policy": "policy"}[source])
    assert {key for key in gate.snapshot if key.endswith(("_error", "_expired", "_refused"))} == reasons


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


def test_drop_fields_are_ignored_including_malformed_values():
    files = Files(file(lowering(drop_gb="ignored", drop_window_min=[])))
    gate = DiskAdmission("/fake/state", read_free=lambda path: 34 * GB, read_ruling=files)
    gate.begin_pass(policy(), [], stamp(0))
    assert gate.snapshot["floor_gb"] == 30


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
