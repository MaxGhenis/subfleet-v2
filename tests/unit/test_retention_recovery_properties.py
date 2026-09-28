"""C-8.4: cancellation, crash recovery, and transaction pins preserve jobs.

Every example owns a new temporary state directory and repository. These tests
never inspect the operator's state or invoke a daemon.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import retention
from subfleet.store import Store


def git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, capture_output=True,
        text=True,
    ).stdout.strip()


@contextmanager
def case(*, owned=False, payload=b"job output"):
    with TemporaryDirectory(prefix="retention-property-") as temporary:
        temporary_root = Path(temporary).resolve()
        root = temporary_root / "state"
        root.mkdir()
        repository = temporary_root / "repository"
        worktree = None
        if owned:
            repository.mkdir()
            git(repository, "init", "-b", "main")
            (repository / "tracked").write_bytes(b"preserved baseline")
            (repository / "second").write_bytes(b"another baseline file")
            git(repository, "add", ".")
            git(repository, "-c", "user.name=Retention test", "-c",
                "user.email=retention@example.invalid", "commit", "-m", "baseline")
            worktree = root / "worktrees" / "job"
            git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
        with Store(root / "state.sqlite3") as store:
            store.add_job(
                job_id="job", request_id="job", payload_digest="digest", kind="dispatch",
                workdir=str(repository if owned else root),
                worktree=str(worktree) if worktree else None, prompt_path="/prompt",
                sandbox="workspace-write" if owned else "read-only", state="succeeded",
                finished_at="2026-01-01T00:00:00Z",
            )
            if owned:
                store.update_job("job", workdir_head=git(repository, "rev-parse", "HEAD"))
            directory = root / "jobs" / "job"
            directory.mkdir(parents=True)
            (directory / "stdout").write_bytes(payload)
            yield store, root, repository, worktree, directory


def retention_leases(store):
    return [lease for lease in store.list_leases() if lease["holder"].startswith("retention:")]


@settings(max_examples=45, deadline=None, database=None)
@given(stop=st.integers(min_value=1, max_value=55), payload=st.binary(max_size=100))
def test_checkpoint_interruption_leaves_job_intact_or_committed_without_lease(stop, payload):
    """Every generated cooperative interruption returns an intact or pruned job."""
    with case(payload=payload) as (store, root, _, _, directory):
        original = retention._checkpoint
        checkpoints = 0

        def interrupt(cancel, deadline):
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == stop:
                raise retention._Interrupted("deadline")
            original(cancel, deadline)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(retention, "_checkpoint", interrupt)
            result = retention.maintenance(store, root, max_jobs=0)

        if store.get_job("job") is None:
            assert "job" in result["pruned"]
            assert not directory.exists()
            assert not retention_leases(store)
            assert any(event["kind"] == "retention.pruned" for event in store.list_events("job"))
        else:
            assert (directory / "stdout").read_bytes() == payload

        # Recovery must run even when count and byte pressure have disappeared.
        retention.maintenance(store, root, max_jobs=100, max_bytes=10**9)
        assert not retention_leases(store)
        if store.get_job("job") is not None:
            assert (directory / "stdout").read_bytes() == payload


@settings(max_examples=16, deadline=None, database=None)
@given(transaction_check=st.integers(min_value=1, max_value=2),
       payload=st.binary(min_size=1, max_size=100))
def test_pin_appearing_inside_either_delete_transaction_restores_intact_job(transaction_check, payload):
    """Selection and final deletion both re-check pins inside the transaction."""
    with case(payload=payload) as (store, root, _, _, directory):
        checks = 0

        def pins():
            nonlocal checks
            if store.connection.in_transaction:
                checks += 1
            return {"job"} if checks >= transaction_check else set()

        result = retention.maintenance(store, root, max_jobs=0, pins=pins)

        assert checks >= transaction_check
        assert result["pruned"] == []
        assert "job" in result["protected"]
        assert store.get_job("job") is not None
        assert (directory / "stdout").read_bytes() == payload
        assert not retention_leases(store)


class SimulatedProcessDeath(BaseException):
    """Bypass ordinary error handling, as SIGKILL would."""


@settings(max_examples=8, deadline=None, database=None)
@given(after_rename=st.integers(min_value=1, max_value=2), pinned=st.booleans())
def test_restart_recovers_each_atomic_rename_before_considering_budget(after_rename, pinned):
    """A crash between directory renames is recoverable, including a new pin."""
    with case(owned=True) as (store, root, repository, worktree, directory):
        original = Path.rename
        renamed = 0

        def die_after_rename(source, destination):
            nonlocal renamed
            result = original(source, destination)
            if source in (worktree, directory):
                renamed += 1
                if renamed == after_rename:
                    raise SimulatedProcessDeath()
            return result

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "rename", die_after_rename)
            try:
                result = retention.maintenance(store, root, max_jobs=0)
            except SimulatedProcessDeath:
                pass
            else:
                pytest.fail(f"did not reach rename checkpoint {after_rename}: {result!r}")

        assert store.get_job("job") is not None
        assert retention_leases(store)
        # Even before restart, every byte still exists in exactly one original
        # or journaled location. SIGKILL may interrupt the two atomic renames,
        # but cannot leave partially unlinked live job data.
        target = root / "trash" / "job"
        assert (target / "manifest.json").is_file()
        for source, retired, files in (
            (worktree, target / "worktree", {
                "tracked": b"preserved baseline", "second": b"another baseline file",
            }),
            (directory, target / "job", {"stdout": b"job output"}),
        ):
            locations = [path for path in (source, retired) if path.exists()]
            assert len(locations) == 1
            for name, contents in files.items():
                assert (locations[0] / name).read_bytes() == contents
        # Reopen the database to ensure recovery does not need an in-memory scan.
        store.close()
        with Store(root / "state.sqlite3") as restarted:
            if pinned:
                restarted.add_notice("job", "newly needed", "session")
            result = retention.maintenance(restarted, root, max_jobs=100, max_bytes=10**9)
            assert not retention_leases(restarted)
            if pinned:
                assert restarted.get_job("job") is not None
                assert (directory / "stdout").read_bytes() == b"job output"
                assert (worktree / "tracked").read_bytes() == b"preserved baseline"
                assert (worktree / "second").read_bytes() == b"another baseline file"
                assert str(worktree) in git(repository, "worktree", "list", "--porcelain")
            else:
                assert result["pruned"] == ["job"], result
                assert restarted.get_job("job") is None
                assert not worktree.exists()
                assert not directory.exists()


@pytest.mark.parametrize("pin_kind", [None, "notice", "explicit"])
@pytest.mark.parametrize("missing_gitfile", [False, True])
def test_legacy_half_deleted_retention_lease_recovers_without_pressure(pin_kind, missing_gitfile):
    """A prior git-remove timeout cannot strand the row or defeat a new pin."""
    with case(owned=True) as (store, root, _, worktree, directory):
        (worktree / "tracked").unlink()
        if missing_gitfile:
            (worktree / ".git").unlink()
        assert store.acquire_lease(f"worktree:{worktree}", "retention:job")
        if pin_kind == "notice":
            store.add_notice("job", "still needed", "session")

        result = retention.maintenance(
            store, root, max_jobs=100, max_bytes=10**9,
            referenced_job_ids={"job"} if pin_kind == "explicit" else (),
        )

        assert not retention_leases(store)
        if pin_kind is not None:
            assert result["pruned"] == []
            assert store.get_job("job") is not None
            assert (worktree / "second").read_bytes() == b"another baseline file"
            assert (directory / "stdout").read_bytes() == b"job output"
        else:
            assert result["pruned"] == ["job"], result
            assert store.get_job("job") is None
            assert not worktree.exists()
            assert not directory.exists()


@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_legacy_recovery_lease_does_not_authorize_discarding_surviving_edits(change):
    """Old deletion intent only explains missing files, never new user content."""
    with case(owned=True) as (store, root, _, worktree, directory):
        (worktree / "tracked").unlink()
        unpreserved = worktree / ("second" if change == "tracked" else "new-output")
        unpreserved.write_bytes(b"not held anywhere else")
        assert store.acquire_lease(f"worktree:{worktree}", "retention:job")

        result = retention.maintenance(store, root, max_jobs=100, max_bytes=10**9)

        assert result["pruned"] == []
        assert result["errors"]
        assert "job" in result["protected"]
        assert store.get_job("job") is not None
        assert unpreserved.read_bytes() == b"not held anywhere else"
        assert (directory / "stdout").read_bytes() == b"job output"
        assert not retention_leases(store)
