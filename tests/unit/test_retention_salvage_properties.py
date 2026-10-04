"""C-8.4/C-13.4: a salvage ref never vouches for bytes its snapshot omitted."""
from __future__ import annotations

import hashlib
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
