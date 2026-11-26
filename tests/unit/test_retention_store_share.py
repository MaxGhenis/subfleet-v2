"""D-STORE-SHARE1: share archive bytes while keeping each source file's identity."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from subfleet import cli, retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from tests.unit.retention_world import Clock, World, git, inode_groups, snapshot, trust_temporary_directories

pytestmark = pytest.mark.retention_catch_up

DATA = b"identical contents kept in the archive\n"
STDOUT = "a1/stdout"
FROZEN = "a1/stream.frozen-" + "1" * 32 + ".jsonl"
STREAM = "a1/stream.jsonl"


class Crash(BaseException):
    pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()


def run(w: World, **kwargs):
    kwargs.setdefault("max_jobs", 0)
    kwargs.setdefault("max_bytes", 0)
    kwargs.setdefault("holders", lambda watches, **_: {})
    return retention.maintenance(w.store, w.root, **kwargs)


def building(w: World, job: str):
    return w.root / "retention" / job / "archive"


def archive(w: World, job: str):
    return w.root / "archive" / job


def progress(w: World, job: str):
    return {record["k"]: record for record in (
        json.loads(line) for line in (building(w, job) / rarch.PROGRESS).read_text().splitlines()
    )}


def read_manifest(w: World, job: str):
    return json.loads((archive(w, job) / "manifest.json").read_text())


def entries(manifest, tree):
    return {entry["p"]: entry for entry in manifest["trees"][tree]["entries"]}


def restore(w: World, job: str):
    dest = w.base / f"restore-{job}"
    rarch.restore(w.root, job, to=dest)
    return dest


def set_metadata(path, mode, mtime_ns):
    path.chmod(mode)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_identical_files_across_trees_share_a_store_and_restore_independently(world, monkeypatch, capsys):
    w = world
    wt = w.job("cross-tree", files={"job.txt": DATA})
    note = wt / "untracked.txt"
    note.write_bytes(DATA)
    job = w.root / "jobs" / "cross-tree"
    set_metadata(note, 0o640, 1600000000000000000)
    set_metadata(job / "job.txt", 0o755, 1600000001000000000)
    expected = snapshot(wt)["untracked.txt"], snapshot(job)["job.txt"]

    assert run(w)["pruned"] == ["cross-tree"]
    manifest = read_manifest(w, "cross-tree")
    store = entries(manifest, "worktree")["untracked.txt"]["store"]
    assert entries(manifest, "job")["job.txt"]["store"] == store
    assert {p.name for p in (archive(w, "cross-tree") / "files").iterdir() if p.read_bytes() == DATA} == {store}
    monkeypatch.setenv("SUBFLEET_HOME", str(w.root))
    assert cli.main(["retention", "restore", "cross-tree", "--check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]

    restored = restore(w, "cross-tree")
    worktree, job = restored / "worktree", restored / "job"
    assert (snapshot(worktree)["untracked.txt"], snapshot(job)["job.txt"]) == expected
    one, two = (worktree / "untracked.txt").stat(), (job / "job.txt").stat()
    assert one.st_ino != two.st_ino and one.st_nlink == two.st_nlink == 1


@pytest.mark.parametrize("pair_first", [False, True], ids=["single-first", "pair-first"])
def test_one_link_files_share_without_joining_a_hard_link_group(world, pair_first):
    w = world
    single, pair = ("z-single", "a-pair") if pair_first else ("a-single", "z-pair")
    w.job("hardlinks", worktree=False, files={single: DATA, pair: DATA})
    job = w.root / "jobs" / "hardlinks"
    other = pair + "-link"
    os.link(job / pair, job / other)
    set_metadata(job / single, 0o755, 1600000000000000000)
    set_metadata(job / pair, 0o640, 1600000001000000000)
    expected = snapshot(job)
    groups = inode_groups(job)
    pair_key = rfs.sig_key((job / pair).stat())

    assert run(w)["pruned"] == ["hardlinks"]
    archived = entries(read_manifest(w, "hardlinks"), "job")
    if pair_first:
        assert archived[single]["store"] == archived[pair]["store"]
    else:
        assert archived[single]["store"] != archived[pair]["store"]
    assert archived[pair]["store"] == archived[other]["store"] == archived[other]["hl"] == pair_key
    assert "hl" not in archived[single]

    restored = restore(w, "hardlinks") / "job"
    assert inode_groups(restored) == groups == [{pair, other}]
    assert snapshot(restored) == expected
    assert (restored / single).stat().st_nlink == 1
    assert (restored / single).stat().st_ino != (restored / pair).stat().st_ino


@pytest.mark.parametrize("step", ["fresh-clone", "discarded-clone", "shared-record"])
def test_a_crashed_share_resumes_with_the_same_totals(world, monkeypatch, step):
    w = world
    files = {"a-first": DATA, "b-second": DATA}
    job_id = "resume"
    w.job(job_id, worktree=False, files=files)
    job = w.root / "jobs" / job_id
    expected = snapshot(job)
    first_key = rfs.sig_key((job / "a-first").stat())
    duplicate_key = rfs.sig_key((job / "b-second").stat())
    original_clone = rfs.clone_or_copy
    original_note = rarch._Builder._note

    def clone_then_crash(src_fd, dst_dir_fd, name, check=None):
        method = original_clone(src_fd, dst_dir_fd, name, check)
        if name == duplicate_key:
            assert (building(w, job_id) / "files" / duplicate_key).exists()
            raise Crash()
        return method

    def crash_at_record(self, record):
        if record["k"] == duplicate_key and record.get("store"):
            assert not (self.files / duplicate_key).exists()
            if step == "shared-record":
                original_note(self, record)
            raise Crash()
        original_note(self, record)

    with monkeypatch.context() as once:
        if step == "fresh-clone":
            once.setattr(rfs, "clone_or_copy", clone_then_crash)
        else:
            once.setattr(rarch._Builder, "_note", crash_at_record)
        with pytest.raises(Crash):
            run(w)

    records = progress(w, job_id)
    cached = records.get(duplicate_key)
    if step == "shared-record":
        assert cached and cached["store"] == first_key
        # Model a stale unrecorded clone at the alias's own key. Accounting
        # must see only the blocks the original will actually give back.
        handle = os.open(w.root / "retention" / job_id / "job" / "b-second", rfs.O_FILE)
        files_fd = rfs.open_dir(building(w, job_id) / "files")
        try:
            original_clone(handle, files_fd, duplicate_key)
        finally:
            os.close(files_fd)
            os.close(handle)
    else:
        assert cached is None

    assert run(w)["pruned"] == [job_id]
    w.job("fresh", worktree=False, files=files)
    assert run(w)["pruned"] == ["fresh"]
    resumed, fresh = read_manifest(w, job_id), read_manifest(w, "fresh")
    assert resumed["totals"] == fresh["totals"]
    assert resumed["totals"].get("shared_bytes", 0) == len(DATA)
    assert resumed["totals"]["stored_files"] == 1
    assert entries(resumed, "job")["b-second"]["store"] == first_key
    assert {p.name for p in (archive(w, job_id) / "files").iterdir()} == {first_key}
    assert rarch.check_archive(w.root, job_id)["ok"]
    assert snapshot(restore(w, job_id) / "job") == expected


def test_a_shared_progress_record_keeps_its_store_without_the_original_owner(world, monkeypatch):
    w = world
    job_id = "only-alias"
    w.job(job_id, worktree=False, files={"a-owner": DATA, "b-alias": DATA})
    job = w.root / "jobs" / job_id
    owner_key = rfs.sig_key((job / "a-owner").stat())
    alias_key = rfs.sig_key((job / "b-alias").stat())
    original = rarch._Builder._note

    def note_then_crash(self, record):
        original(self, record)
        if record["k"] == alias_key and record.get("store"):
            raise Crash()

    with monkeypatch.context() as once:
        once.setattr(rarch._Builder, "_note", note_then_crash)
        with pytest.raises(Crash):
            run(w)
    assert progress(w, job_id)[alias_key]["store"] == owner_key
    (w.root / "retention" / job_id / "job" / "a-owner").unlink()

    assert run(w)["pruned"] == [job_id]
    manifest = read_manifest(w, job_id)
    assert entries(manifest, "job")["b-alias"]["store"] == owner_key
    assert manifest["totals"]["stored_files"] == 1
    assert manifest["totals"]["shared_bytes"] == 0
    assert {p.name for p in (archive(w, job_id) / "files").iterdir()} == {owner_key}
    assert (restore(w, job_id) / "job" / "b-alias").read_bytes() == DATA


def test_a_shared_store_that_fails_readback_preserves_both_sources(world, monkeypatch):
    w = world
    job_id = "bad-shared"
    w.job(job_id, worktree=False, files={"a-first": DATA, "b-second": DATA})
    job = w.root / "jobs" / job_id
    before = snapshot(job)
    first_key = rfs.sig_key((job / "a-first").stat())
    original = rfs.clone_or_copy

    def clone_then_spoil(src_fd, dst_dir_fd, name, check=None):
        method = original(src_fd, dst_dir_fd, name, check)
        if name == first_key:
            fd = os.open(name, os.O_WRONLY, dir_fd=dst_dir_fd)
            try:
                os.write(fd, b"X")
            finally:
                os.close(fd)
        return method

    clock, state = Clock(), retention.RetentionState()
    with monkeypatch.context() as once:
        once.setattr(rfs, "clone_or_copy", clone_then_spoil)
        result = run(w, clock=clock, state=state)
    manifest = json.loads((building(w, job_id) / "manifest.json").read_text())
    archived = entries(manifest, "job")
    assert archived["a-first"]["store"] == archived["b-second"]["store"]
    assert result["pruned"] == []
    assert result["deferred"][job_id].startswith("archive did not read back")
    assert not (building(w, job_id) / "files" / first_key).exists()
    assert not archive(w, job_id).exists()
    assert w.store.get_job(job_id) is not None
    assert snapshot(job) == before

    clock.advance(rarch.DEFER_ERROR_S + 1)
    assert run(w, clock=clock, state=state)["pruned"] == [job_id]
    assert rarch.check_archive(w.root, job_id)["ok"]
    assert snapshot(restore(w, job_id) / "job") == before


def test_a_late_write_to_one_shared_source_is_kept_in_conflicts(world, monkeypatch):
    w = world
    job_id = "shared-conflict"
    w.job(job_id, worktree=False, files={"a-first": DATA, "b-second": DATA})
    original = rarch.Retirement.publish
    changed = b"rewritten after publish"

    def publish_then_write(self):
        original(self)
        archived = entries(self.manifest(), "job")
        assert archived["a-first"]["store"] == archived["b-second"]["store"]
        (self.q_job / "a-first").write_bytes(changed)

    monkeypatch.setattr(rarch.Retirement, "publish", publish_then_write)
    result = run(w)
    assert result["pruned"] == [job_id]
    assert {c["job_id"] for c in result["conflicts"]} == {job_id}
    conflicts = w.root / "retention-conflicts" / job_id / "job"
    assert (conflicts / "a-first").read_bytes() == changed
    assert not (conflicts / "b-second").exists()
    assert not (w.root / "retention" / job_id).exists()
    restored = restore(w, job_id) / "job"
    assert (restored / "a-first").read_bytes() == (restored / "b-second").read_bytes() == DATA


@pytest.mark.parametrize("force_copy", [False, True], ids=["default", "force-copy"])
def test_shared_bytes_are_reported_in_archive_totals_events_and_cli(world, monkeypatch, capsys, force_copy):
    w = world
    job_id = "shared-totals"
    size = 64 << 10
    data = os.urandom(size)
    w.job(job_id, worktree=False, files={STDOUT: data, FROZEN: data})
    original_stat = (w.root / "jobs" / job_id / FROZEN).stat()
    own_key = rfs.sig_key(original_stat)
    measurements = []
    original_private = rfs.private_bytes

    def measure_private(parent, name, st):
        if st.st_ino == original_stat.st_ino and st.st_dev == original_stat.st_dev:
            measurements.append((building(w, job_id) / "files" / own_key).exists())
        return original_private(parent, name, st)

    monkeypatch.setattr(rfs, "FORCE_COPY", force_copy)
    monkeypatch.setattr(rfs, "private_bytes", measure_private)
    result = run(w)
    assert result["pruned"] == [job_id]
    summary = json.loads((archive(w, job_id) / "summary.json").read_text())
    assert summary["archived_bytes"] == size
    assert summary["stored_files"] == 1
    assert summary["shared_bytes"] == size
    assert summary["freed_bytes"] == 0
    assert summary["freed_disk_bytes"] >= size
    assert measurements == [False]
    if force_copy:
        assert summary["copied_bytes"] == size
        assert summary["copies"] == 1
    assert result["shared_bytes"] == result["pools"]["detached"]["shared_bytes"] == size
    for kind in ("retention.pruned", "retention.reclaimed"):
        events = [json.loads(e["data_json"]) for e in w.store.list_events(job_id) if e["kind"] == kind]
        assert [e["shared_bytes"] for e in events if e] == [size], kind
    monkeypatch.setenv("SUBFLEET_HOME", str(w.root))
    assert cli.main(["retention", "archives", "--json"]) == 0
    (listed,) = json.loads(capsys.readouterr().out)["archives"]
    assert listed["shared_bytes"] == size
    assert cli.main(["retention", "archives"]) == 0
    row, total = capsys.readouterr().out.splitlines()
    assert f"shared {size:,} B" in row
    assert f"{size:,} bytes shared" in total


@pytest.mark.parametrize("hard_link", [False, True], ids=["frozen-copy", "live-hardlink-and-frozen-copy"])
def test_attempt_stdout_and_streams_share_without_merging_inodes(world, hard_link):
    w = world
    job_id = "streams"
    w.job(job_id, worktree=False, files={STDOUT: DATA, FROZEN: DATA})
    w.attempt(job_id)
    job = w.root / "jobs" / job_id
    if hard_link:
        os.link(job / STDOUT, job / STREAM)
    before = snapshot(job)

    assert run(w)["pruned"] == [job_id]
    archived = entries(read_manifest(w, job_id), "job")
    assert archived[STDOUT]["store"] == archived[FROZEN]["store"]
    assert "hl" not in archived[FROZEN]
    assert {p.name for p in (archive(w, job_id) / "files").iterdir()} == {archived[STDOUT]["store"]}
    if hard_link:
        assert archived[STREAM]["hl"] == archived[STREAM]["store"] == archived[STDOUT]["store"]
    restored = restore(w, job_id) / "job"
    assert inode_groups(restored) == ([{STDOUT, STREAM}] if hard_link else [])
    assert (restored / FROZEN).stat().st_ino != (restored / STDOUT).stat().st_ino
    assert snapshot(restored) == before


def test_equal_size_files_with_different_bytes_keep_separate_stores(world):
    w = world
    job_id = "equal-size"
    w.job(job_id, worktree=False, files={"a-first": b"abc", "b-second": b"xyz"})
    assert run(w)["pruned"] == [job_id]
    archived = entries(read_manifest(w, job_id), "job")
    assert archived["a-first"]["store"] != archived["b-second"]["store"]
    assert archived["a-first"]["size"] == archived["b-second"]["size"]
    assert rarch.check_archive(w.root, job_id)["ok"]


def test_a_rewrite_that_would_share_defers_during_archive(world, monkeypatch):
    w = world
    job_id = "rewrite"
    w.job(job_id, worktree=False, files={"a-first": DATA, "b-second": DATA})
    job = w.root / "jobs" / job_id
    first_key = rfs.sig_key((job / "a-first").stat())
    duplicate_key = rfs.sig_key((job / "b-second").stat())
    original = rfs.clone_or_copy

    def clone_then_write(src_fd, dst_dir_fd, name, check=None):
        method = original(src_fd, dst_dir_fd, name, check)
        if name == duplicate_key:
            target = w.root / "retention" / job_id / "job" / "b-second"
            target.write_bytes(DATA)
            os.utime(target, ns=(1600000000000000000, 1600000000000000000))
        return method

    monkeypatch.setattr(rfs, "clone_or_copy", clone_then_write)
    result = run(w)
    assert result["pruned"] == []
    assert "changed while it was archived" in result["deferred"][job_id]
    assert (job / "b-second").read_bytes() == DATA
    records = progress(w, job_id)
    assert records[first_key]["store"] == first_key
    assert not records.get(duplicate_key, {}).get("store")


def test_a_restarted_regenerable_walk_counts_shared_stores_once(world, monkeypatch):
    w = world
    job_id = "regen-retry"
    wt = w.job(job_id)
    for name in (".dup-a", ".dup-b"):
        (wt / name).write_bytes(DATA)
    with (wt / ".gitignore").open("a") as rules:
        rules.write(".venv/\n")
    clone = wt / ".venv" / "lib" / "python3.14" / "site-packages" / "editable"
    clone.mkdir(parents=True)
    (wt / ".venv" / "pyvenv.cfg").write_text("home = /usr/local/bin\n")
    git(clone, "init", "--quiet")

    walks = []
    original = rfs.RegenerableWalk.__init__

    def count_walk(self, root_fd, clear, denied=frozenset()):
        walks.append(set(denied))
        original(self, root_fd, clear, denied)

    monkeypatch.setattr(rfs.RegenerableWalk, "__init__", count_walk)
    assert run(w)["pruned"] == [job_id]
    assert walks == [set(), {".venv/lib"}]
    manifest = read_manifest(w, job_id)
    archived = entries(manifest, "worktree")
    assert archived[".dup-a"]["store"] == archived[".dup-b"]["store"]
    seen = set()
    counts = dict.fromkeys(("stored_files", "archived_bytes", "shared_bytes"), 0)
    for label in ("worktree", "job", "admin"):
        for entry in manifest["trees"][label]["entries"]:
            store = entry.get("store")
            if not store:
                continue
            assert entry["sig"]["nlink"] == 1
            if store in seen:
                counts["shared_bytes"] += entry["size"]
            else:
                seen.add(store)
                counts["stored_files"] += 1
                counts["archived_bytes"] += entry["size"]
    assert {key: manifest["totals"][key] for key in counts} == counts


@pytest.mark.parametrize("field", ["sha256", "size"])
def test_readback_rejects_inconsistent_shared_entry_metadata(world, monkeypatch, field):
    w = world
    job_id = "bad-alias-metadata"
    w.job(job_id, worktree=False, files={"a-first": DATA, "b-second": DATA})
    job = w.root / "jobs" / job_id
    before = snapshot(job)
    original = rarch._Builder._verify_store

    def change_metadata(self, trees):
        archived = {entry["p"]: entry for entry in trees["job"]["entries"]}
        assert archived["a-first"]["store"] == archived["b-second"]["store"]
        archived["b-second"][field] = {"sha256": "0" * 64, "size": len(DATA) + 1}[field]
        original(self, trees)

    monkeypatch.setattr(rarch._Builder, "_verify_store", change_metadata)
    result = run(w)
    assert result["pruned"] == []
    assert result["deferred"][job_id].startswith("archive did not read back")
    assert not list((building(w, job_id) / "files").iterdir())
    assert snapshot(job) == before


@pytest.mark.parametrize("field", ["sha256", "size"])
def test_archive_check_rejects_inconsistent_shared_entry_metadata(world, field):
    w = world
    job_id = "bad-published-metadata"
    w.job(job_id, worktree=False, files={"a-first": DATA, "b-second": DATA})
    assert run(w)["pruned"] == [job_id]
    manifest = read_manifest(w, job_id)
    archived = entries(manifest, "job")
    assert archived["a-first"]["store"] == archived["b-second"]["store"]
    archived["b-second"][field] = {"sha256": "0" * 64, "size": len(DATA) + 1}[field]
    (archive(w, job_id) / "manifest.json").write_text(json.dumps(manifest))
    checked = rarch.check_archive(w.root, job_id)
    assert not checked["ok"]
    assert any("b-second" in problem for problem in checked["problems"])


@pytest.mark.documented_limitation
@pytest.mark.skipif(sys.platform != "darwin", reason="requires APFS clone xattr semantics")
def test_shared_restore_uses_the_stored_copys_xattrs(world):
    w = world
    job_id = "shared-xattrs"
    w.job(job_id, worktree=False, files={"a-first": DATA, "b-second": DATA})
    job = w.root / "jobs" / job_id
    attr = "org.subfleet.tests.archive-store-share"
    subprocess.run(["xattr", "-w", attr, "first xattr", job / "a-first"], check=True)
    subprocess.run(["xattr", "-w", attr, "second xattr", job / "b-second"], check=True)
    assert run(w)["pruned"] == [job_id]
    manifest = read_manifest(w, job_id)
    if manifest["totals"]["clones"] == 0:
        pytest.skip("this volume does not support clones")
    archived = entries(manifest, "job")
    assert archived["a-first"]["store"] == archived["b-second"]["store"]
    restored = restore(w, job_id) / "job"
    first = subprocess.check_output(["xattr", "-p", attr, restored / "a-first"])
    second = subprocess.check_output(["xattr", "-p", attr, restored / "b-second"])
    assert first == second == b"first xattr\n"
