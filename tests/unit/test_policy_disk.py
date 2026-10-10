import copy
import json

import pytest

from subfleet.policy import DEFAULT_POLICY_PATH, DISK_DEFAULTS, PolicyError, disk_settings, load_policy


@pytest.mark.parametrize("section", [None, {}, {"enabled": True}, {"path": "/Volumes/work"},
                                    {"lower_path": "/rulings/lower", "raise_path": "/rulings/raise"},
                                    {"min_floor_gb": 0, "max_lower_h": 0.5}])
def test_disk_defaults_and_partial_config(tmp_path, section):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    if section is None:
        raw.pop("admission")
    else:
        raw["admission"]["disk"] = section
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    assert load_policy(path)["admission"]["disk"] == {**DISK_DEFAULTS, **(section or {})}
    assert disk_settings({}) == DISK_DEFAULTS
    assert DISK_DEFAULTS["enabled"] is False


@pytest.mark.parametrize("key,value", [
    ("enabled", 1), ("enabled", "true"), ("floor_gb", -1), ("floor_gb", True),
    ("floor_gb", float("nan")), ("resume_margin_gb", -1), ("resume_margin_gb", float("inf")),
    ("placement_reserve_gb", 0), ("placement_reserve_gb", -1), ("placement_reserve_gb", "1.5"),
    ("reserve_ttl_s", 0), ("reserve_ttl_s", False), ("reserve_ttl_s", float("inf")),
    ("path", ""), ("path", "  "), ("path", 1), ("typo", 1),
    ("lower_path", "lower.json"), ("lower_path", ""), ("lower_path", 1),
    ("raise_path", "../raise.json"), ("raise_path", "  "), ("raise_path", False),
    ("min_floor_gb", -1), ("min_floor_gb", True), ("min_floor_gb", float("nan")),
    ("max_lower_h", 0), ("max_lower_h", False), ("max_lower_h", float("inf")),
])
def test_invalid_disk_setting_names_exact_key(tmp_path, key, value):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"]["disk"][key] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == f"admission.disk.{key}"


@pytest.mark.parametrize("value", [None, [], True, "enabled"])
def test_disk_section_must_be_an_object(tmp_path, value):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"]["disk"] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == "admission.disk"


def test_default_objects_are_not_shared_between_loads(tmp_path):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"].pop("disk")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    first = load_policy(path)
    first["admission"]["disk"]["enabled"] = True
    assert load_policy(path)["admission"]["disk"] == copy.deepcopy(DISK_DEFAULTS)


@pytest.mark.parametrize("key,value", [("placement_reserve_gb", 1e-10), ("placement_reserve_gb", 0.0009),
                                       ("floor_gb", 1e308), ("resume_margin_gb", 1e7), ("min_floor_gb", 2e6)])
def test_disk_sizes_outside_whole_byte_bounds_are_refused(tmp_path, key, value):
    """C-6.17 (review of #161, P3s): a reserve that rounds to nothing, or a size that
    overflows whole-byte arithmetic, is refused at load with the key named."""
    policy = json.loads(DEFAULT_POLICY_PATH.read_text())
    policy.setdefault("admission", {})["disk"] = {"enabled": True, key: value}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(PolicyError, match=f"admission.disk.{key}"):
        load_policy(path)


@pytest.mark.parametrize("key", ["floor_gb", "resume_margin_gb", "placement_reserve_gb", "min_floor_gb"])
@pytest.mark.parametrize("value", [1_000_001, 1e308, 10**400],
                         ids=["one-pb-plus", "huge-float", "huge-int"])
def test_review_large_size_rejected_with_key(tmp_path, key, value):
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    raw["admission"]["disk"][key] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == f"admission.disk.{key}"
