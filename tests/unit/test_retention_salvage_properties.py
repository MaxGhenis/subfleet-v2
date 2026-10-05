"""C-8.4/C-13.4: a salvage ref never vouches for bytes its snapshot omitted."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import retention, retention_archive as rarch
from subfleet.salvage import salvage
from tests.unit.retention_world import Clock, World, git, snapshot


@settings(max_examples=20, deadline=None, database=None)
@example(skipped=True, present=True, archive="missing", data=b"private work")
@example(skipped=True, present=True, archive="corrupt", data=b"private work")
@example(skipped=True, present=True, archive="verified", data=b"private work")
@example(skipped=False, present=True, archive="missing", data=b"ignored work")
@example(skipped=False, present=False, archive="missing", data=b"gone")
@given(skipped=st.booleans(), present=st.booleans(),
       archive=st.sampled_from(["missing", "corrupt", "verified"]),
       data=st.binary(min_size=1, max_size=256))
def test_retention_prunes_only_when_unbundled_unarchived_bytes_are_absent(skipped, present, archive, data):
    # All repositories, stores and mutations are private to this example.
    with tempfile.TemporaryDirectory(prefix="retention-salvage-") as temporary, pytest.MonkeyPatch.context() as patch:
        base = Path(temporary)
        w = World(base)
        try:
            wt = w.job("job")
            (wt / "src" / "main.py").write_bytes(b"provider progress\n")
            root = wt / ("nested repo " if skipped else "out")
            root.mkdir()
            if skipped:
                git(root, "init", "--quiet")
            path = root / "private.bin"
            path.write_bytes(data)
            result = salvage(wt, w.head(), 1, timestamp="2026-10-04T12:00:00Z")
            assert result is not None and bool(result.skipped) == skipped
            w.store.add_artifact(w.attempt("job"), "salvage", result.ref,
                                 hashlib.sha256(result.commit.encode()).hexdigest(), 0)
            # Independent git oracle: these bytes are absent from the accepted
            # salvage. Both unindexable and ignored files need a byte archive.
            rel = str(path.relative_to(wt))
            assert not git(w.repo, "ls-tree", "-r", "--name-only", result.commit, "--", rel)
            if not present:
                path.unlink()
            before = snapshot(wt)
            original = rarch._Builder._file

            def archive_file(self, entry, *args, **kwargs):
                original(self, entry, *args, **kwargs)
                if entry["p"] != rel:
                    return
                if archive == "missing":
                    entry.pop("store")
                elif archive == "corrupt":
                    copy = self.files / entry["store"]
                    # Same size: readback must check bytes, not just size.
                    copy.write_bytes(bytes([data[0] ^ 1]) + data[1:])

            patch.setattr(rarch._Builder, "_file", archive_file)
            outcome = retention.maintenance(w.store, w.root, max_jobs=0, clock=Clock(),
                                            holders=lambda watches, **_: {},
                                            salvage_referenced_elsewhere=lambda artifact: True)
            safe = not present or archive == "verified"
            assert outcome["pruned"] == (["job"] if safe else []), outcome
            if safe:
                assert outcome["protected"] == [] and not wt.exists()
                assert rarch.check_archive(w.root, "job")["ok"]
                # Import into an independent object store: neither the source's
                # loose objects nor a caller's vouch can satisfy the oracle.
                fresh = base / "fresh"
                git(base, "clone", "--quiet", str(w.remote), str(fresh))
                git(fresh, "fetch", "--quiet", str(w.root / "archive" / "job" / "commits.bundle"),
                    "+refs/*:refs/restored/*")
                git(fresh, "cat-file", "-e", result.commit + "^{commit}")
                rarch.restore(w.root, "job", to=base / "restored", repository=fresh)
                restored = base / "restored" / "worktree"
                # The registration's root gitfile is rewritten on restore;
                # every other entry, including the nested object store, survives.
                assert {p: e for p, e in snapshot(restored).items() if p != ".git"} == {
                    p: e for p, e in before.items() if p != ".git"}
            else:
                assert outcome["protected"] == ["job"] and w.store.get_job("job") is not None
                reason = "unarchived path" if archive == "missing" else "archive did not read back"
                assert outcome["deferred"]["job"].startswith(reason + ":"), outcome
                assert rel in outcome["deferred"]["job"]
                assert snapshot(wt) == before and path.read_bytes() == data
                assert str(wt) in git(w.repo, "worktree", "list", "--porcelain")
                assert w.store.list_leases() == []
        finally:
            w.close()


KINDS = ["file", "empty", "hardlink", "symlink"]


def _tamper_saved(retirement, rel, archive):
    """After an interrupted final check: lose or corrupt `rel`'s archived copy."""
    import json
    manifest = retirement.manifest()
    entry = next(e for e in manifest["trees"]["worktree"]["entries"] if e["p"] == rel)
    name = entry.get("store")
    if name is None:
        return                                   # a link: no bytes to lose
    copy = retirement.building / "files" / name
    if archive == "missing":
        entry.pop("store")
        (retirement.building / "manifest.json").write_text(json.dumps(manifest))
    elif archive == "corrupt":
        data = copy.read_bytes()
        copy.write_bytes(bytes([data[0] ^ 1]) + data[1:] if data else b"\x01")


@settings(max_examples=24, deadline=None, database=None)
@example(kind="empty", resume=False, skipped=True, present=True, archive="missing", data=b"x")
@example(kind="hardlink", resume=False, skipped=True, present=True, archive="missing", data=b"x")
@example(kind="symlink", resume=False, skipped=True, present=True, archive="missing", data=b"x")
@example(kind="file", resume=True, skipped=True, present=True, archive="missing", data=b"x")
@example(kind="file", resume=True, skipped=True, present=True, archive="verified", data=b"x")
@example(kind="file", resume=True, skipped=True, present=True, archive="corrupt", data=b"x")
@given(kind=st.sampled_from(KINDS), resume=st.booleans(), skipped=st.booleans(), present=st.booleans(),
       archive=st.sampled_from(["missing", "corrupt", "verified"]),
       data=st.binary(min_size=1, max_size=256))
def test_every_kind_of_entry_and_a_resume_keep_unarchived_bytes(kind, resume, skipped, present, archive, data):
    """The property above over every entry type, and over a restart between the
    archive and its final check (the recovery path that skips the builder)."""
    with tempfile.TemporaryDirectory(prefix="retention-salvage-") as temporary, pytest.MonkeyPatch.context() as patch:
        base = Path(temporary)
        w = World(base)
        try:
            wt = w.job("job")
            (wt / "src" / "main.py").write_bytes(b"provider progress\n")
            root = wt / ("nested repo " if skipped else "out")
            root.mkdir()
            if skipped:
                git(root, "init", "--quiet")
            if kind == "empty":
                data = b""
            path = root / "private.bin"
            if kind == "symlink":
                (root / "target.bin").write_bytes(data)
                os.symlink("target.bin", path)
            else:
                path.write_bytes(data)
                if kind == "hardlink":
                    # Sorts first, so it is stored and `path` is archived as its link ("hl").
                    os.link(path, root / "private-link.bin")
            result = salvage(wt, w.head(), 1, timestamp="2026-10-04T12:00:00Z")
            assert result is not None and bool(result.skipped) == skipped
            w.store.add_artifact(w.attempt("job"), "salvage", result.ref,
                                 hashlib.sha256(result.commit.encode()).hexdigest(), 0)
            rel = str(path.relative_to(wt))
            assert not git(w.repo, "ls-tree", "-r", "--name-only", result.commit, "--", rel)
            if not present:
                path.unlink()
            before = snapshot(wt)
            run = dict(max_jobs=0, clock=Clock(), holders=lambda watches, **_: {},
                       salvage_referenced_elsewhere=lambda artifact: True)
            if resume:
                def interrupt(self):
                    raise rarch.Interrupted("restart before the final check")

                with pytest.MonkeyPatch.context() as once:
                    once.setattr(rarch.Retirement, "final_check", interrupt)
                    first = retention.maintenance(w.store, w.root, **run)
                assert first["pruned"] == [] and first.get("interrupted"), first
                retirement = rarch.Retirement(rarch.Context(w.root, w.store), "job")
                assert retirement.state == "archived"
                if present:
                    _tamper_saved(retirement, rel, archive)
                patch.setattr(rarch.Retirement, "archive",
                              lambda *a, **k: pytest.fail("recovery of an archived journal does not rebuild"))
            else:
                original = rarch._Builder._file

                def archive_file(self, entry, *args, **kwargs):
                    original(self, entry, *args, **kwargs)
                    if entry["p"] != rel:
                        return
                    if archive == "missing":
                        entry.pop("store")
                    elif archive == "corrupt":
                        copy = self.files / entry["store"]
                        copy.write_bytes(bytes([data[0] ^ 1]) + data[1:] if data else b"\x01")

                patch.setattr(rarch._Builder, "_file", archive_file)
            outcome = retention.maintenance(w.store, w.root, **run)
            # A link's bytes are its target, which the manifest holds.
            safe = not present or archive == "verified" or kind == "symlink"
            assert outcome["pruned"] == (["job"] if safe else []), (kind, resume, archive, outcome)
            if safe:
                assert outcome["protected"] == [] and not wt.exists()
                assert rarch.check_archive(w.root, "job")["ok"]
                fresh = base / "fresh"
                git(base, "clone", "--quiet", str(w.remote), str(fresh))
                rarch.restore(w.root, "job", to=base / "restored", repository=fresh)
                restored = base / "restored" / "worktree"
                assert {p: e for p, e in snapshot(restored).items() if p != ".git"} == {
                    p: e for p, e in before.items() if p != ".git"}
            else:
                assert outcome["protected"] == ["job"] and w.store.get_job("job") is not None
                if not resume:
                    reason = "unarchived path" if archive == "missing" else "archive did not read back"
                    assert outcome["deferred"]["job"].startswith(reason + ":"), outcome
                assert snapshot(wt) == before and path.read_bytes() == data
                assert str(wt) in git(w.repo, "worktree", "list", "--porcelain")
                assert w.store.list_leases() == []
        finally:
            w.close()
