"""D-TMP1: cleanup authority, orphan age and bounded lifecycle sequences."""

import json
import os
from pathlib import Path
import shutil
import tempfile

from hypothesis import given, settings, strategies as st
import pytest


def manager(tmp_path):
    from subfleet.attempt_tmp import AttemptTemp
    root = tmp_path / "user-temp"
    root.mkdir(exist_ok=True)
    return AttemptTemp(tmp_path / "store", root=root)


def subject(attempt_id="job/a1", state="succeeded", **evidence):
    return {"attempt_id": attempt_id, "state": state, "evidence_json": json.dumps(evidence)}


def test_attempt_names_are_unique_even_after_path_sanitizing(tmp_path):
    scratch = manager(tmp_path)
    records = [scratch.create(name) for name in ("job/a1", "job-a1", "../a1", "job/a1")]
    assert len({record["path"] for record in records}) == len(records)
    assert all(Path(record["path"]).parent == scratch.root for record in records)


@pytest.mark.parametrize("state,empty,keep", [
    ("finalizing", True, False), ("running", True, False),
    ("quarantined", True, False), ("succeeded", False, False),
    ("failed", True, True),
])
def test_cleanup_requires_terminal_empty_unretained_attempt(tmp_path, state, empty, keep):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    attempt = subject(state=state, **({"salvage_error": "held"} if keep else {}))
    assert not scratch.remove(record, attempt, verified_empty=empty)
    assert Path(record["path"]).is_dir()


def test_clean_terminal_attempt_is_removed(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    (Path(record["path"]) / "file").write_bytes(b"x" * 4096)
    assert scratch.remove(record, subject(), verified_empty=True)
    assert not Path(record["path"]).exists()


def test_sweep_requires_prefix_even_with_valid_creation_record(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    outside = scratch.root / "unrelated"
    Path(record["path"]).rename(outside)
    scratch.sweep(lambda _: None, lambda _: True, now=10**12)
    assert outside.is_dir()


def test_sweep_never_follows_symlink(tmp_path, monkeypatch):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    target = scratch.root / "outside-prefix"
    Path(record["path"]).rename(target)
    Path(record["path"]).symlink_to(target, target_is_directory=True)
    removed = []
    monkeypatch.setattr(shutil, "rmtree", lambda path, **kwargs: removed.append(path))
    scratch.sweep(lambda _: None, lambda _: True, now=10**12)
    assert removed == []
    assert target.is_dir()


def test_sweep_defers_live_quarantined_salvage_and_nonempty(tmp_path):
    scratch = manager(tmp_path)
    states = {name: subject(name, state, **evidence) for name, state, evidence in [
        ("live", "running", {}), ("q", "quarantined", {}),
        ("salvage", "failed", {"salvage_skipped": {"count": 1}}),
        ("busy", "succeeded", {}), ("dead", "failed", {})]}
    records = {name: scratch.create(name) for name in states}
    scratch.sweep(states.get, lambda a: a["attempt_id"] != "busy", now=10**12)
    assert {name for name, record in records.items() if Path(record["path"]).exists()} == set(states) - {"dead"}


def test_unknown_attempt_waits_24_hours_and_foreign_store_is_untouched(tmp_path):
    from subfleet.attempt_tmp import AttemptTemp
    scratch = manager(tmp_path)
    record = scratch.create("unknown")
    foreign = AttemptTemp(tmp_path / "other-store", root=scratch.root).create("other-live")
    scratch.sweep(lambda _: None, lambda _: True, now=record["created_at"] + 86399)
    assert Path(record["path"]).exists()
    scratch.sweep(lambda _: None, lambda _: True, now=record["created_at"] + 86401)
    assert not Path(record["path"]).exists()
    assert Path(foreign["path"]).exists()


def test_removal_error_is_logged_and_sweep_retries(tmp_path, monkeypatch, caplog):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    original = shutil.rmtree
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("dummy")))
        assert not scratch.remove(record, subject(), verified_empty=True)
    assert "TMPDIR" in caplog.text
    scratch.sweep(lambda _: subject(), lambda _: True)
    assert not Path(record["path"]).exists()


@settings(max_examples=40, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(["start", "finalize", "quarantine", "sweep"]),
                         st.integers(0, 4), st.booleans()), min_size=1, max_size=35))
def test_generated_attempt_lifetimes_preserve_isolation_and_cleanup_authority(steps):
    # Each example owns a fresh tree; no generated operation reaches host scratch.
    with tempfile.TemporaryDirectory(prefix="attempt-model-") as directory:
        scratch = manager(Path(directory))
        states, records, empties, all_paths = {}, {}, {}, set()
        protected = scratch.root / "not-an-attempt"
        protected.mkdir()
        link = scratch.root / "subfleet-attempt-symlink"
        link.symlink_to(protected, target_is_directory=True)
        for operation, index, empty in steps:
            name = f"job-{index}/a1"
            if operation == "start" and name not in states:
                states[name], empties[name] = subject(name, "running"), False
                records[name] = scratch.create(name)
                path = records[name]["path"]
                assert path not in all_paths
                all_paths.add(path)
            elif name in states and operation in {"finalize", "quarantine"}:
                states[name]["state"] = "succeeded" if operation == "finalize" else "quarantined"
                empties[name] = empty
                scratch.remove(records[name], states[name], verified_empty=empty)
            elif operation == "sweep":
                scratch.sweep(states.get, lambda a: empties[a["attempt_id"]])
            for key, row in states.items():
                if row["state"] in {"running", "quarantined"} or not empties[key]:
                    # Do not resurrect already removed attempts in the model.
                    if Path(records[key]["path"]).exists():
                        assert Path(records[key]["path"]).is_dir()
            assert protected.is_dir() and link.is_symlink()
