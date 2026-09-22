"""External overlay safety and exact staging; never uses installed state."""

import hashlib
import json
from pathlib import Path
import subprocess
import tomllib

import pytest

from subfleet import doctor
from subfleet.guard import preflight as guard
from tests.fake.guard import install_guard
from tools.stage_guard_overlay import stage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SUBFLEET_GUARD_TRUST", raising=False)


def test_missing_default_overlay_fails_closed_without_subprocess_or_files(tmp_path, monkeypatch):
    def no_process(*args, **kwargs):
        pytest.fail("missing overlay must refuse before resolving or invoking Codex")
    monkeypatch.setattr(guard.shutil, "which", no_process)
    root = tmp_path / "state"
    verdict = guard.preflight("codex")
    row = doctor.check_guard_preflight(root)
    assert verdict.code == 7 and verdict.kind == guard.TRUST
    assert row["status"] == doctor.FAIL
    assert row["detail"] in verdict.message
    assert "<state root>/guard" in row["fix"]
    assert not root.exists()


@pytest.mark.parametrize("bad", ["{", "[]", "null", '"a"', "{}", "non-string", "blank-version",
                                  "hash", "reference", "override", "hook-bytes", "non-executable"])
def test_doctor_and_preflight_reject_the_same_malformed_overlay(tmp_path, bad):
    root = tmp_path / "state"
    hook, pin = install_guard(root)
    trust = json.loads(pin.read_text())
    if bad in {"{", "[]", "null", '"a"', "{}"}:
        pin.write_text(bad)
    elif bad == "hook-bytes":
        hook.write_bytes(hook.read_bytes() + b"# altered\n")
    elif bad == "non-executable":
        hook.chmod(0o600)
    else:
        field, value = {
            "non-string": ("codex_version", 153), "blank-version": ("codex_version", " "),
            "hash": ("hooks_trust_hash", "sha256:changed"),
            "reference": ("reference_hook_path", "relative"), "override": ("override", "hooks={}"),
        }[bad]
        trust[field] = value
        pin.write_text(json.dumps(trust))
    before = {p.name: p.read_bytes() for p in (hook, pin)}
    row = doctor.check_guard_preflight(root)
    verdict = guard.preflight("nonexistent-codex", state_root=root)
    assert row["status"] == doctor.FAIL
    assert verdict.code == 7 and verdict.kind == guard.TRUST
    assert row["detail"] in verdict.message
    assert before == {p.name: p.read_bytes() for p in (hook, pin)}
    assert not (root / "guard-cache").exists() and not (root / "tmp").exists()


def test_selected_state_root_overrides_environment_and_doctor_reports_paths(tmp_path, monkeypatch):
    root = tmp_path / "selected root"
    hook, pin = install_guard(root)
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "missing"))
    assert guard.guard_paths(root) == (hook, pin)
    assert guard.load_guard(root)[:2] == (hook, pin)
    row = doctor.check_guard_preflight(root)
    assert row["status"] == doctor.PASS
    assert str(hook) in row["detail"] and str(pin) in row["detail"]
    assert "runtime trust requires preflight" in row["detail"]


def test_explicit_pair_wins_over_missing_default_and_bad_environment_pin(tmp_path, monkeypatch):
    hook, pin = install_guard(tmp_path / "explicit")
    monkeypatch.setenv("SUBFLEET_GUARD_TRUST", str(tmp_path / "missing-TRUST"))
    assert guard.load_guard(hook_path=hook, trust_path=pin)[:2] == (hook, pin)
    with pytest.raises(ValueError, match="unreadable"):
        guard.load_guard(hook_path=hook)


def test_stage_preserves_exact_bytes_and_old_launch_path(tmp_path):
    old_release = tmp_path / "old-release/subfleet"
    source_hook, source_pin = install_guard(old_release)
    old_override = guard.override_string(source_hook)
    old_bytes = {p.name: p.read_bytes() for p in (source_hook, source_pin)}
    root = tmp_path / "new state"
    destination = stage(source_hook.parent, root)
    assert stage(source_hook.parent, root) == destination
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == old_bytes
    assert {p.name: p.read_bytes() for p in (source_hook, source_pin)} == old_bytes
    assert guard.override_string(source_hook) == old_override
    assert guard.override_string(destination / source_hook.name) != old_override
    # An already-recorded launch still invokes the same existing executable.
    assert subprocess.run([str(source_hook), "--fixture-deny"]).returncode == 2
    assert (destination.stat().st_mode & 0o777) == 0o700
    assert ((destination / "TRUST").stat().st_mode & 0o777) == 0o600
    assert not list(root.glob(".guard-stage-*"))


def test_stage_never_overwrites_different_valid_overlay(tmp_path):
    source_hook, _ = install_guard(tmp_path / "source")
    root = tmp_path / "state"
    hook, pin = install_guard(root)
    hook.write_bytes(hook.read_bytes() + b"# separately reviewed local policy\n")
    trust = json.loads(pin.read_text())
    trust["hook_sha256"] = hashlib.sha256(hook.read_bytes()).hexdigest()
    pin.write_text(json.dumps(trust))
    before = (hook.read_bytes(), pin.read_bytes())
    with pytest.raises(ValueError, match="refusing to replace"):
        stage(source_hook.parent, root)
    assert before == (hook.read_bytes(), pin.read_bytes())


def test_stage_refuses_bad_source_without_creating_state(tmp_path):
    hook, _ = install_guard(tmp_path / "source")
    hook.write_bytes(b"changed")
    root = tmp_path / "state"
    with pytest.raises(ValueError, match="SHA-256"):
        stage(hook.parent, root)
    assert not root.exists()


def test_stage_does_not_mistake_old_release_symlink_for_external_copy(tmp_path):
    hook, pin = install_guard(tmp_path / "old-release/subfleet")
    root = tmp_path / "state"
    root.mkdir()
    (root / "guard").symlink_to(hook.parent, target_is_directory=True)
    before = (hook.read_bytes(), pin.read_bytes())
    with pytest.raises(ValueError, match="overlay symlink"):
        stage(hook.parent, root)
    assert before == (hook.read_bytes(), pin.read_bytes())
    assert (root / "guard").is_symlink()


def test_looping_overlay_symlink_is_a_refusal(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "guard").symlink_to(root / "guard")
    assert guard.preflight("codex").code == 7
    assert doctor.check_guard_preflight(root)["status"] == doctor.FAIL


def test_private_overlay_bytes_remain_the_original_reviewed_pair():
    repo = Path(__file__).resolve().parents[2]
    directory = repo / "private/guard"
    expected = {
        "never-rules-hook.sh": "a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db",
        "TRUST": "0b9d688e6b2ca3adefdb8ad465c0fad5e6e18dfa884bc2309ff4f506c2287bab",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
        assert not (repo / "subfleet/guard" / name).exists()
    guard.load_guard(hook_path=directory / "never-rules-hook.sh", trust_path=directory / "TRUST")
    assert subprocess.run(["/bin/bash", "-n", str(directory / "never-rules-hook.sh")]).returncode == 0


def test_distribution_allowlists_exclude_operator_files_and_test_fixtures():
    repo = Path(__file__).resolve().parents[2]
    targets = tomllib.loads((repo / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["packages"] == ["subfleet"]
    assert set(targets["sdist"]["include"]) == {"/subfleet", "/README.md", "/pyproject.toml", "/uv.lock"}
