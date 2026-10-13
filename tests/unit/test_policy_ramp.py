import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet.policy import (
    DEFAULT_POLICY_PATH, RAMP_DEFAULTS, PolicyError, admission_settings, load_policy,
    policy_hash, ramp_settings,
)
from subfleet.ramp import PlacementRamp


def test_shipped_policy_preserves_pre_ramp_raw_hash_and_disabled_defaults():
    # A daemon without policy.json copies these bytes, whose hash enters every
    # admission decision even when the ramp is absent or disabled (C-6.18 R6).
    # release/217's bytes after #190 added `providers.codex.ca_bundle` and #179 turned
    # `reset_credits` off; this PR leaves them unchanged.
    before = "29e76475f03eb5f27b7ca83a0a321e1d296c4701a6fd33733b104d5d7844d2a2"
    assert "ramp" not in json.loads(DEFAULT_POLICY_PATH.read_bytes())["admission"]
    assert policy_hash(DEFAULT_POLICY_PATH) == before
    loaded = load_policy(DEFAULT_POLICY_PATH)
    assert loaded["_policy_hash"] == before
    assert loaded["admission"]["ramp"] == RAMP_DEFAULTS


@pytest.mark.parametrize("shape", ["bundled", "admission-absent", "operator-tuned"])
def test_policy_without_ramp_hashes_its_bytes_as_release_217_loader(tmp_path, shape):
    """R6: release/217 (4c6a8a9c) hashed a policy as `sha256(raw)` of the bytes it
    parsed, and `policy_hash(path)` as `sha256(path.read_bytes())`. Neither the
    ramp defaults the loader now supplies nor the file's own path may enter it."""
    raw = DEFAULT_POLICY_PATH.read_bytes()
    if shape != "bundled":
        data = json.loads(raw)
        if shape == "admission-absent":
            data.pop("admission")
        else:
            data["caps"]["max_active_attempts"] = 3
            data["admission"]["priority_callers"] = ["chosen"]
        raw = json.dumps(data, indent=1).encode()
    assert "ramp" not in json.loads(raw).get("admission", {})
    digests = set()
    for root in ("first", "second"):            # two state roots, as two daemons have
        path = tmp_path / root / "policy.json"
        path.parent.mkdir()
        path.write_bytes(raw)
        loaded = load_policy(path)
        assert path.read_bytes() == raw         # loading writes nothing back
        assert loaded["admission"]["ramp"] == RAMP_DEFAULTS
        digests |= {loaded["_policy_hash"], policy_hash(path)}
    assert digests == {hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize("section", [None, {}, {"enabled": True}, {"per_minute": 3},
                                    {"window_s": 120},
                                    {"enabled": True, "per_minute": 60, "window_s": 1},
                                    {"enabled": True, "per_minute": 1, "window_s": 60},
                                    {"per_minute": 10 ** 30, "window_s": 6}])
def test_ramp_defaults_and_partial_config(tmp_path, section):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    if section is None:
        raw.pop("admission")
    else:
        raw["admission"]["ramp"] = section
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    expected = {**RAMP_DEFAULTS, **(section or {})}
    loaded = load_policy(path)
    assert loaded["admission"]["ramp"] == expected
    assert ramp_settings(loaded) == expected
    assert admission_settings(loaded)["ramp"] == expected
    assert ramp_settings({}) == RAMP_DEFAULTS
    assert admission_settings({})["ramp"] == RAMP_DEFAULTS
    assert RAMP_DEFAULTS == {"enabled": False, "per_minute": 12, "window_s": 60}


@pytest.mark.parametrize("key,value", [
    ("enabled", 1), ("enabled", 0), ("enabled", None), ("enabled", "true"),
    ("per_minute", 0), ("per_minute", -1), ("per_minute", True),
    ("per_minute", 12.0), ("per_minute", 1.5), ("per_minute", "12"),
    ("per_minute", None), ("per_minute", float("nan")), ("per_minute", float("inf")),
    ("window_s", 0), ("window_s", -1), ("window_s", False),
    ("window_s", 60.0), ("window_s", 0.5), ("window_s", "60"),
    ("window_s", None), ("window_s", float("nan")), ("window_s", float("-inf")),
    ("starts_per_minute", 12), ("window", 60),
])
def test_invalid_ramp_setting_names_exact_key(tmp_path, key, value):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"].setdefault("ramp", dict(RAMP_DEFAULTS))[key] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == f"admission.ramp.{key}"


@settings(max_examples=150, deadline=None, derandomize=True)
@example(per_minute=12, window_s=1, enabled=True)      # allowance 0.2 would admit 1 a second
@example(per_minute=1, window_s=1, enabled=False)      # refused even while disabled
@example(per_minute=1, window_s=30, enabled=True)
@example(per_minute=7, window_s=90, enabled=True)
@example(per_minute=60, window_s=1, enabled=True)
@example(per_minute=12, window_s=5, enabled=True)
@example(per_minute=1, window_s=120, enabled=True)
@example(per_minute=12, window_s=60, enabled=True)
@given(per_minute=st.integers(1, 60), window_s=st.integers(1, 300), enabled=st.booleans())
def test_loader_accepts_exactly_whole_allowances_and_each_admits_exactly_that(
        tmp_path_factory, per_minute, window_s, enabled):
    """C-6.18: a loaded window allows `per_minute * window_s / 60` ordinary starts, a
    whole number. A fractional one is refused naming `window_s`, because the
    pre-placement comparison would admit its ceiling (12/minute over 1 s: 60/minute)."""
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"]["ramp"] = {"enabled": enabled, "per_minute": per_minute, "window_s": window_s}
    path = tmp_path_factory.mktemp("ramp-allowance") / "policy.json"
    path.write_text(json.dumps(raw))
    whole = per_minute * window_s % 60 == 0
    if not whole:
        with pytest.raises(PolicyError) as caught:
            load_policy(path)
        assert caught.value.key == "admission.ramp.window_s"
        assert "multiple of 60" in str(caught.value)
        return
    loaded = load_policy(path)
    if not enabled:
        return
    gate, now = PlacementRamp(), datetime(2026, 10, 10, 14, tzinfo=timezone.utc).isoformat()
    gate.begin_pass(loaded, [], now)
    starts = 0
    while gate.hold("background", now) is None:
        gate.reserve(str(starts), now)
        starts += 1
    assert starts == gate.snapshot["limit"] == per_minute * window_s // 60      # R1, exact


def test_large_whole_allowance_is_accepted_and_large_fractional_one_refused(tmp_path):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    path = tmp_path / "policy.json"
    raw["admission"]["ramp"] = {"enabled": True, "per_minute": 10 ** 400, "window_s": 6}
    path.write_text(json.dumps(raw))
    assert load_policy(path)["admission"]["ramp"]["per_minute"] == 10 ** 400
    raw["admission"]["ramp"]["window_s"] = 1
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == "admission.ramp.window_s"


BASE = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
#: Rates and windows the loader accepts, including subminute and longer windows.
WHOLE = [(per_minute, window_s) for per_minute in range(1, 21)
         for window_s in (1, 5, 10, 15, 20, 30, 40, 45, 60, 90, 120, 300)
         if per_minute * window_s % 60 == 0]


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


@settings(max_examples=150, deadline=None, derandomize=True)
@given(quota=st.sampled_from(WHOLE), n=st.integers(1, 60), data=st.data())
def test_R2_from_a_window_already_holding_up_to_its_allowance(quota, n, data):
    """C-6.18: with up to K = per_minute * window_s / 60 starts already in the window,
    n jobs place by ceil(n / K) * window_s: ceil(n / per_minute) minutes for the
    60-second window, and at most window_s more than that for any window."""
    per_minute, window_s = quota
    allowance = per_minute * window_s // 60
    cfg = {"admission": {"ramp": {"enabled": True, "per_minute": per_minute, "window_s": window_s}}}
    ages_ms = data.draw(st.lists(st.integers(0, window_s * 1000 - 1), max_size=allowance))
    ledger = [{"attempt_id": f"prior-{serial}", "reserved_at": at(-age / 1000), "kind": "dispatch"}
              for serial, age in enumerate(ages_ms)]
    now, placed = 0.0, 0
    while True:
        gate = PlacementRamp()                  # every pass reconstructs from the ledger
        gate.begin_pass(cfg, ledger, at(now))
        while placed < n and (hold := gate.hold("background", at(now))) is None:
            gate.reserve(str(placed), at(now))
            ledger.append({"attempt_id": str(placed), "reserved_at": at(now), "kind": "dispatch"})
            placed += 1
        if placed == n:
            break
        # The first pass once a slot opens; `_control` runs one every tick.
        now = (datetime.fromisoformat(hold["next_slot_at"].replace("Z", "+00:00")) - BASE).total_seconds()
    bound = math.ceil(n / allowance) * window_s
    assert now <= bound                                                 # R2, prefilled window
    assert bound <= math.ceil(n / per_minute) * 60 + window_s
    if window_s == 60:
        assert bound == math.ceil(n / per_minute) * 60


@pytest.mark.parametrize("value", [None, [], True, "enabled", 12])
def test_ramp_section_must_be_an_object(tmp_path, value):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"]["ramp"] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == "admission.ramp"


def test_ramp_default_objects_are_not_shared_between_loads(tmp_path):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"].pop("ramp", None)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    first = load_policy(path)
    first["admission"]["ramp"]["enabled"] = True
    assert load_policy(path)["admission"]["ramp"] == copy.deepcopy(RAMP_DEFAULTS)


def test_raw_ramp_settings_merge_without_mutating_defaults():
    raw = {"admission": {"ramp": {"per_minute": 7}}}
    settings = ramp_settings(raw)
    assert settings == {"enabled": False, "per_minute": 7, "window_s": 60}
    settings["window_s"] = 1
    assert raw == {"admission": {"ramp": {"per_minute": 7}}}
    assert RAMP_DEFAULTS["window_s"] == 60
