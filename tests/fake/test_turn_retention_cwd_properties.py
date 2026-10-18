"""T1/T3: an independent path oracle around the real turn reservation path."""
import os
from pathlib import Path
import tempfile

from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import folders
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _end, _live
from tests.fake.test_turn_wait_reasons import SETTINGS, message_in, reason
from tests.unit.retention_world import git


def contains(parent, child):
    """Reference containment, independent of folders.above/within/retiring."""
    return os.path.commonpath((str(parent), str(child))) == str(parent)


@settings(max_examples=40, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow])
@example(parts=["pkg"], level=1, external=True, alias=False, writable=False, holder="retention:retired")
@example(parts=["pkg"], level=1, external=True, alias=True, writable=False, holder="retention:retired")
@example(parts=[], level=0, external=False, alias=False, writable=False, holder="retention:retired")
@example(parts=["pkg"], level=1, external=True, alias=True, writable=True, holder="retention:retired")
@example(parts=["pkg"], level=1, external=False, alias=False, writable=True, holder="detached-writer")
@given(parts=st.lists(st.sampled_from(["pkg", "lib", "a:b", "a;b"]), max_size=3),
       level=st.integers(0, 5), external=st.booleans(), alias=st.booleans(),
       writable=st.booleans(), holder=st.sampled_from(["retention:retired", "detached-writer", "gate-round:test"]))
def test_t1_t3_actual_cwd_fences_hold_reservation_and_name_the_folder(parts, level, external, alias, writable, holder):
    """A fake retention holds fences; actual daemon SQL reserves or waits.

    Git holds come from persisted core.worktree. Fake preparation may return a
    symlink spelling of the same cwd, exercising off-transaction canonicalization.
    The oracle compares canonical filesystem identities with commonpath, without
    asking production fence or row helpers whether the turn should wait.
    """
    with tempfile.TemporaryDirectory(prefix="turn-cwd-t1-") as temporary:
        base = Path(temporary)
        with fleet_daemon(base / "state") as (daemon, harness, patch):
            for lane in CODEX:
                measure(daemon, lane)
            actual = base / "tree"
            actual = actual.joinpath(*parts)
            actual.mkdir(parents=True)
            git(actual, "init", "--quiet", "-b", "task/turn")
            (actual / "f.txt").write_text("turn\n")
            git(actual, "add", ".")
            git(actual, "commit", "--quiet", "-m", "turn")
            held = actual
            if external:
                held = base / "external"
                held.mkdir()
                (held / "f.txt").write_text("turn\n")
                git(actual, "config", "core.worktree", str(held))
            actual, held = Path(actual.resolve()), Path(held.resolve())
            typed = str(actual)
            if alias:
                link = base / "cwd-link"
                link.symlink_to(actual, target_is_directory=True)
                typed = str(link) + "/./"
            _, mid, turn = message_in(
                daemon, harness, "Fence property", workspace=actual,
                settings={**SETTINGS, "permission": "accept-edits" if writable else "read-only"})
            recorded = daemon._submitted(turn)
            assert (recorded.get("write_target") if writable else recorded.get("folder")) == str(held)
            patch.setattr(daemon, "_workspace", lambda job: (typed, None, None, []))
            canonical = folders.canonical

            def canonical_off_lock(path):
                assert not daemon.store._holds_writer(), "canonicalization under the reservation lock"
                return canonical(path)

            patch.setattr(folders, "canonical", canonical_off_lock)
            # A fake retirement's selecting transaction; no live row is present yet.
            fence = actual if level == 0 else actual.parents[min(level - 1, len(actual.parents) - 1)]
            key = "worktree:" + str(fence)
            assert daemon.store.acquire_lease(key, holder)
            retention_blocks = holder.startswith("retention:") and (
                contains(fence, actual) or contains(fence, held))
            exact_writer_blocks = writable and fence == held
            daemon._admit_turns()
            assert _live(daemon, turn) == (not (retention_blocks or exact_writer_blocks)), daemon._holds
            if retention_blocks:
                assert daemon._holds[turn]["reason"] == "lease-held"
                assert key in daemon._holds[turn]["leases"]
                assert str(fence) in reason(daemon, mid), (fence, reason(daemon, mid))
            daemon.store.release_leases(holder)
            daemon._admit_turns()
            assert _live(daemon, turn), daemon._holds
            if actual != held:
                assert folders.turn_holds(daemon.store.query, str(actual), (folders.READER,)), "cwd has no retention pin"
            _end(daemon, turn)
            assert not daemon.store.query("SELECT 1 FROM leases WHERE holder=?", (turn,))
