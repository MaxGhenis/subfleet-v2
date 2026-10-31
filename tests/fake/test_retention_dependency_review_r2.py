"""C-8.4: review r2 probes actual alternates changes and the job-directory boundary."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tracemalloc
import unicodedata

import pytest

from subfleet import dependencies, folders, procs, retention, retention_archive as rarch
from tests.unit.retention_world import World, git, snapshot
from tests.unit.test_retention_dependency_units import repo


def maintain(world):
    return retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                 holders=lambda *a, **k: {})


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(procs, "worktree_cwd_pids", lambda *a, **k: frozenset())
    w = World(tmp_path)
    yield w
    w.close()


def reader(world, depends, *, state="running", kind="dispatch"):
    world.store.add_job(job_id="borrower", request_id="borrower", payload_digest="d",
                        kind=kind, state=state, sandbox="read-only",
                        workdir=str(world.base / "outside"), prompt_path="/p")
    world.store.add_event("job.submitted", job_id="borrower", data={"depends": depends})


@pytest.mark.parametrize("change", ["replace", "rewrite"])
def test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads(world, monkeypatch, change):
    tree = world.job("owner")
    source = repo(tree / "object-source")
    commit = git(source, "rev-parse", "HEAD")
    borrower = world.base / "outside"
    git(world.base, "init", "--quiet", str(borrower))
    alternate = borrower / ".git" / "objects" / "info" / "alternates"
    alternate.write_bytes(b"# first\n# second\n")
    real = dependencies.open_regular
    changed = []

    @contextmanager
    def opening(path, *args, **kwargs):
        with real(path, *args, **kwargs) as stream:
            if str(path) != str(alternate):
                yield stream
                return

            class Changing:
                def readline(self, size=-1):
                    line = stream.readline(size)
                    if not changed:
                        changed.append(True)
                        new = os.fsencode(source / ".git" / "objects") + b"\n"
                        if change == "replace":
                            replacement = alternate.with_name("replacement")
                            replacement.write_bytes(new)
                            replacement.replace(alternate)
                        else:
                            alternate.write_bytes(new)
                    return line

                def __getattr__(self, name):
                    return getattr(stream, name)

            yield Changing()

    with monkeypatch.context() as patch:
        patch.setattr(dependencies, "open_regular", opening)
        recorded = dependencies.record(str(borrower))
    assert changed
    assert git(borrower, "cat-file", "-t", commit) == "commit"
    reader(world, recorded)
    result = maintain(world)
    assert "owner" not in result["pruned"], (recorded, result)
    assert tree.is_dir()
    assert git(borrower, "cat-file", "-t", commit) == "commit"
    assert recorded["incomplete"][0]["file"] == str(alternate)
    assert "borrower" in result["pin_reasons"]["owner"]
    assert str(alternate) in result["pin_reasons"]["owner"]


def test_c8_4_unlinked_open_alternates_is_incomplete(tmp_path, monkeypatch):
    alternate = tmp_path / ".git" / "objects" / "info" / "alternates"
    alternate.parent.mkdir(parents=True)
    alternate.write_bytes(b"# first\n# second\n")
    real = dependencies.open_regular
    removed = []

    @contextmanager
    def opening(path, *args, **kwargs):
        with real(path, *args, **kwargs) as stream:
            if str(path) == str(alternate):
                alternate.unlink()
                removed.append(True)
            yield stream

    monkeypatch.setattr(dependencies, "open_regular", opening)
    recorded = dependencies.record(str(tmp_path))
    assert removed and not alternate.exists()
    assert recorded["incomplete"][0]["file"] == str(alternate)


@pytest.mark.parametrize("stage", ["database", "full-pass", "already-exporting"])
def test_c8_4_legacy_incomplete_census_survives_terminal_export(world, monkeypatch, stage):
    tree = world.job("owner")
    borrower = world.base / "outside"
    alternate = borrower / ".git" / "objects" / "info" / "alternates"
    alternate.parent.mkdir(parents=True)
    alternate.write_bytes(b'"bad\\z"\n')
    world.store.add_job(job_id="borrower", request_id="borrower", payload_digest="d",
                        kind="dispatch", state="running", sandbox="read-only",
                        workdir=str(borrower), prompt_path="/p", out_path="/outside/result")
    world.store.add_event("job.submitted", job_id="borrower", data={})
    original = dependencies.legacy

    def finish_after_discovery(read):
        legacy = original(read)
        assert legacy.incomplete["borrower"][0]["file"] == str(alternate)
        world.store.acquire_lease("out:/outside/result", "borrower")
        world.store.update_job("borrower", state="succeeded")
        return legacy

    if stage == "already-exporting":
        world.store.acquire_lease("out:/outside/result", "borrower")
        world.store.update_job("borrower", state="succeeded")
        result = maintain(world)
        assert "owner" not in result["pruned"], result
        assert "dependency-incomplete" in result["pin_reasons"]["owner"]
        assert str(alternate) in result["pin_reasons"]["owner"]
        return

    if stage == "full-pass":
        monkeypatch.setattr(dependencies, "legacy", finish_after_discovery)
        result = maintain(world)
        assert "owner" not in result["pruned"], result
        assert "dependency-incomplete" in result["pin_reasons"]["owner"]
        assert str(alternate) in result["pin_reasons"]["owner"]
        return

    legacy = finish_after_discovery(world.store.query)
    with world.store.transaction():
        places = dependencies.live(world.store.query, legacy)
        assert "borrower" in places.incomplete
        pins = retention._pin_reasons(world.store, set(), None, root=world.root, legacy=legacy)
        assert "dependency-incomplete" in pins["owner"]
        assert str(alternate) in pins["owner"]
    assert tree.is_dir()


@pytest.mark.parametrize("recorded", [False, True])
def test_c8_3_null_job_output_preserves_lease_and_recorded_export_pins(world, monkeypatch, recorded):
    tree = world.job("owner")
    output = str(tree / "result")
    reader(world, {"out_path": output} if recorded else {}, state="succeeded")
    assert world.store.one("SELECT out_path FROM jobs WHERE job_id='borrower'")["out_path"] is None
    world.store.acquire_lease("out:/unrelated/result" if recorded else "out:" + output, "borrower")

    def forbidden(*a, **k):
        raise AssertionError("filesystem access in exporter transaction")

    with monkeypatch.context() as patch:
        for name in ("canonical", "spelling", "present", "identity"):
            patch.setattr(folders, name, forbidden)
        for name in ("record", "git_storage", "_alternates", "open_regular"):
            patch.setattr(dependencies, name, forbidden)
        with world.store.transaction():
            assert dependencies.dependents(world.store.query, str(tree), exclude="owner") == ["borrower"]
    result = maintain(world)
    assert "owner" not in result["pruned"], result
    world.store.release_leases("borrower")
    assert "owner" in maintain(world)["pruned"]


def test_c8_4_many_short_valid_lines_use_bounded_input_memory(tmp_path, record_property):
    base, target = tmp_path / "objects", tmp_path / "target"
    (base / "info").mkdir(parents=True)
    target.mkdir()
    alternate = base / "info" / "alternates"
    line = os.fsencode(target) + b"\n"
    with alternate.open("wb") as stream:
        for _ in range((2 * 1024 * 1024) // len(line) + 1):
            stream.write(line)
    errors = []
    tracemalloc.start()
    try:
        found = dependencies._alternates(str(base), incomplete=errors)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    record_property("alternates_bytes", alternate.stat().st_size)
    record_property("distinct_paths", len(found))
    record_property("traced_peak_bytes", peak)
    assert alternate.stat().st_size > 2 * 1024 * 1024
    assert found == [str(target)] and not errors
    # Only one distinct result; neither the full file nor repeated paths are retained.
    assert peak < 1536 * 1024, peak


@pytest.mark.parametrize("nodes", [5, 6], ids=["cycle-within-bound", "full-five-alternates"])
def test_c8_4_alternates_cycle_across_all_five_levels_matches_git(tmp_path, nodes):
    borrower = repo(tmp_path / "borrower")
    source = repo(tmp_path / "source")
    commit = git(source, "rev-parse", "HEAD")
    levels = [borrower / ".git" / "objects", *[tmp_path / f"objects-{i}" for i in range(1, nodes - 1)],
              source / ".git" / "objects"]
    for i, current in enumerate(levels):
        (current / "info").mkdir(parents=True, exist_ok=True)
        (current / "info" / "alternates").write_bytes(os.fsencode(levels[(i + 1) % len(levels)]) + b"\n")
    assert git(borrower, "cat-file", "-t", commit) == "commit"
    errors = []
    assert dependencies._alternates(str(levels[0]), incomplete=errors) == list(map(str, levels[1:]))
    assert not errors


@pytest.mark.parametrize("stage", ["census", "fence", "commit"])
@pytest.mark.parametrize("alias", ["case", "nfc", "greek"])
def test_c8_4_nested_job_directory_aliases_pin_at_every_stage(world, monkeypatch, alias, stage):
    owner = "Owner-é-ΐ"
    world.job(owner, worktree=False)
    directory = world.root / "jobs" / owner
    (directory / "nested" / "deeper").mkdir(parents=True)
    before = snapshot(directory)
    named = str(directory)
    if alias == "case":
        named = named.replace("Owner", "oWNER")
    elif alias == "nfc":
        named = unicodedata.normalize("NFC", named)
    else:
        named = named.replace("ΐ", "Ϊ\u0301")
    output = named + "/nested/deeper/result.md"

    def insert():
        reader(world, {"out_path": output})

    begin, final = rarch.Retirement.begin, rarch.Retirement.final_check

    def beginning(self, *args, **kwargs):
        if stage == "fence":
            insert()
        return begin(self, *args, **kwargs)

    def final_check(self):
        final(self)
        if stage == "commit":
            insert()
        self.ctx.pinned = lambda *_: None

    monkeypatch.setattr(rarch.Retirement, "begin", beginning)
    monkeypatch.setattr(rarch.Retirement, "final_check", final_check)
    if stage == "census":
        insert()
    result = maintain(world)
    assert owner not in result["pruned"], result
    assert snapshot(directory) == before
    if stage == "census":
        assert result["pin_reasons"][owner] == "job-directory-in-use"
    else:
        assert "job-directory-in-use" in result["deferred"][owner]


def test_c8_4_turn_job_directory_rule_keeps_its_precedence(world):
    world.job("owner", worktree=False)
    directory = world.root / "jobs" / "owner"
    nested = directory / "nested"
    nested.mkdir()
    reader(world, {"out_path": str(nested / "result")}, state="queued", kind="turn")
    world.store.update_job("borrower", workdir=str(nested))
    result = maintain(world)
    assert "owner" not in result["pruned"]
    assert result["pin_reasons"]["owner"] == "turn-folder"
