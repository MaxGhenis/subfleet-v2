"""Round 3 invariants 1–3 over generated interleavings (Hypothesis, bounded).

One writable source job runs in its own worktree. Each example interleaves: the
source ending with or without a cwd observer, the observer leaving or another
arriving, the cwd worker's recheck, a resume of the source and its end, a
writable turn into the worktree, a retention pass and a daemon restart. The
submit, admission, finalization, export, worker and retention are the real ones;
only the provider and the kernel's cwd read are fixtures.

After every step:
1. Retention safety: a folder with a live cwd observer, or under a reader row,
   is still there.
2. No idle refusal: a finished source holds no `worktree:` key, its kept row is a
   reader row, and a resume or turn submitted while no live writer holds the
   folder is neither refused nor held for it.
3. Writer exclusivity: no two live writers hold the folder at once, a detached
   writer and a turn included (C-6.5, C-24.5).
"""

import contextlib
import os
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, folders, procs, retention
from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon
from subfleet.procs import Containment
from tests.fake.conftest import Harness
from tests.fake.test_resume_contract import finish_reserved, resume_job
from tests.fake.test_state_contract import receipt_fixture, reserve
from tests.fake.test_turn_wait_reasons import message_in
from tests.fake.test_workspace_contract import repository
from tests.fake_adapter import FakeAdapter

LIVE = ("queued", "waiting", "running")
STEPS = st.sampled_from(["end-observed", "end-quiet", "observer-leaves", "observer-enters", "worker",
                         "resume", "finish-resume", "turn", "retention", "restart"])
#: Bounded (the ci profile's spirit): each example builds a repository, a worktree
#: and a daemon, and a retention pass archives what it retires.
BOUNDED = settings(max_examples=12, deadline=None, derandomize=True, database=None,
                   suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


class World:
    """A daemon with one writable source, and what each step left."""

    def __init__(self, root: Path, patch: pytest.MonkeyPatch):
        self.root, self.patch = root, patch
        self.harness = Harness(root)
        self.observers: frozenset[int] = frozenset()
        patch.setattr(procs, "worktree_cwd_pids", lambda worktree: self.observers)
        self.daemon = self._start()
        repository(self.daemon, self.harness)
        self.source, attempt, self.adir = reserve(self.daemon, self.harness, sandbox="workspace-write")
        self.daemon.store.update_attempt(attempt["attempt_id"], native_session_id="native-source-session")
        self.attempt_id = attempt["attempt_id"]
        self.worktree = self.daemon.store.get_job(self.source)["worktree"]
        self.target = folders.canonical(self.worktree)
        self.ended = False

    def _start(self) -> Daemon:
        daemon = Daemon(self.harness.root)

        def refuse_real_launch(*args):
            raise AssertionError("state-only fixtures must never launch a guardian")
        self.patch.setattr(daemon, "_launch", refuse_real_launch)
        return daemon

    # --- what holds the folder -------------------------------------------------------------------

    def leases(self) -> dict[str, str]:
        return {row["lease_key"]: row["holder"] for row in self.daemon.store.list_leases()}

    def on_folder(self, key: str, prefix: str) -> bool:
        if not key.startswith(prefix):
            return False
        folder = key[len(prefix):] if prefix == folders.EXCLUSIVE else (folders.parse(key) or (None, ""))[1]
        return bool(folder) and folders.canonical(folder) == self.target

    def live(self, job_id: str) -> bool:
        row = self.daemon.store.get_job(job_id)
        return bool(row) and row["state"] in LIVE

    def writers(self) -> tuple[set[str], set[str]]:
        """Live jobs holding the folder: detached writers, and writable turns."""
        held = self.leases()
        detached = {h for k, h in held.items() if self.on_folder(k, folders.EXCLUSIVE) and self.live(h)}
        turns = {h for k, h in held.items() if self.on_folder(k, folders.TURN) and self.live(h)}
        return detached, turns

    def held_for_folder(self, hold: dict | None) -> bool:
        """Whether an admission hold names this folder's writer key or a reader row on it."""
        return bool(hold) and hold.get("reason") == "lease-held" and any(
            self.on_folder(key, folders.EXCLUSIVE) or self.on_folder(key, folders.READER)
            for key in hold.get("leases", ()))

    # --- steps -------------------------------------------------------------------------------------

    def step(self, what: str) -> None:
        getattr(self, "do_" + what.replace("-", "_"))()

    def _end(self, observers: frozenset[int]) -> None:
        if self.ended:
            return
        self.observers = observers
        attempt = self.daemon.store.get_attempt(self.attempt_id)
        self.daemon._finalize(receipt_fixture(self.daemon, attempt, self.adir))
        self.ended = True

    def do_end_observed(self):
        self._end(frozenset({4500}))

    def do_end_quiet(self):
        self._end(frozenset())

    def do_observer_leaves(self):
        self.observers = frozenset()

    def do_observer_enters(self):
        if os.path.isdir(self.worktree):        # one can only enter a folder that is there
            self.observers = frozenset({4501})

    def do_worker(self):
        self.daemon._cwd_recheck_next = {}
        for job_id in self.daemon._pending_worktree_holds():
            self.daemon._release_worktree_hold(job_id)

    def do_resume(self):
        if not self.ended or not self.daemon.store.get_job(self.source):
            return
        detached, turns = self.writers()
        idle = not detached and not turns
        try:
            resumed = resume_job(self.daemon, self.harness, self.source)
        except AdapterError as exc:
            assert not idle or exc.reason not in ("worktree-lease", "conversation-writer"), (exc.reason, str(exc))
            return
        self.daemon._admit()
        if idle:
            assert not self.held_for_folder(self.daemon._holds.get(resumed)), self.daemon._holds.get(resumed)

    def do_finish_resume(self):
        for row in self.daemon.store.query("SELECT job_id FROM jobs WHERE kind='resume' AND state='running'"):
            finish_reserved(self.daemon, row["job_id"])
            return

    def do_turn(self):
        if not os.path.isdir(self.worktree):
            return
        detached, _ = self.writers()
        try:
            _, _, turn = message_in(self.daemon, self.harness, "A look in the tree", workspace=self.worktree)
        except AssertionError:
            assert detached, "a turn was refused while no detached writer held the folder"
            return
        self.daemon._admit_turns()
        if not detached:
            assert not self.held_for_folder(self.daemon._holds.get(turn)), self.daemon._holds.get(turn)

    def do_retention(self):
        store = self.daemon.store
        store.acknowledge_notices("fake-session", [row["notice_id"] for row in store.list_notices()])
        retention.maintenance(store, self.daemon.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})

    def do_restart(self):
        self.daemon.close()
        self.daemon = self._start()

    # --- invariants --------------------------------------------------------------------------------

    def check(self) -> None:
        held = self.leases()
        readers = [k for k in held if self.on_folder(k, folders.READER)]
        if self.observers or readers:
            assert os.path.isdir(self.worktree), ("1: retention removed a held folder", self.observers, readers)
        if self.ended:
            source = {self.source, self.attempt_id}
            assert not [k for k, h in held.items() if k.startswith(folders.EXCLUSIVE) and h in source], held
            assert all(k.startswith(folders.READER) for k, h in held.items()
                       if h == folders.cwd_holder(self.source)), held
        detached, turns = self.writers()
        assert len(detached) <= 1 and not (detached and turns - detached), ("3: two writers", detached, turns)


@contextlib.contextmanager
def world(root: Path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
        patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
        patch.setattr(daemon_module.procs, "snapshot", lambda: daemon_module.procs.ProcessTable({
            os.getpid(): (1, os.getpgrp(), "S", "unit-test-start")}, "unit-test-boot"))
        patch.setattr(daemon_module.procs, "identity", lambda pid: daemon_module.procs.snapshot().identity(pid))
        patch.setattr(daemon_module.procs, "group_members", lambda *args: {})
        patch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
        patch.setattr(daemon_module, "CWD_SETTLE_S", 0)
        register("codex", FakeAdapter)
        state = World(root, patch)
        try:
            yield state
        finally:
            state.daemon.close()
        state.harness.check_notices()


@BOUNDED
@given(steps=st.lists(STEPS, min_size=1, max_size=8))
def test_r3_interleavings_keep_folders_retained_holds_open_and_writers_exclusive(tmp_path_factory, steps):
    root = tmp_path_factory.mktemp("r3-world") / "state"
    root.mkdir()
    with world(root) as state:
        state.check()
        for what in steps:
            state.step(what)
            state.check()
