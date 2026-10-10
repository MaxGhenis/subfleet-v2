"""T2: real selection and admission, with a small retention that really renames.

The fake archive omits Git/archive construction; it retains the production
job-directory guard and selecting transaction. Its move oracle reads attempts
and the expected provider cwd, independently of retention's lease-row helpers.
"""
from pathlib import Path
import tempfile

from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _end, _live
from tests.fake.test_turn_retention_cwd_properties import contains
from tests.fake.test_turn_wait_reasons import SETTINGS, message_in
from tests.unit.retention_world import Clock, git


def selecting_pass(daemon):
    """Only the real selecting transaction is needed by the fake archive."""
    return retention._Pass(
        daemon.store, daemon.root, budgets={"detached": (0, 0), "turn": (0, 0)},
        turn_keep_s=0, pins=None, explicit=set(), salvage_referenced_elsewhere=None,
        cancel=None, deadline=None, state=retention.RetentionState(), holders=lambda *a, **k: {},
        clock=Clock(), batch=1, slice_s=1, measure_s=1, progress={"errors": [], "deferred": {}})


@settings(max_examples=40, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow])
@example(where="job", depth=0, after_selection=False, writable=False, external=False, alias=False)
@example(where="job", depth=1, after_selection=True, writable=False, external=False, alias=False)
@example(where="job", depth=2, after_selection=True, writable=True, external=True, alias=True)
@example(where="tree", depth=1, after_selection=True, writable=False, external=True, alias=True)
@example(where="tree", depth=0, after_selection=False, writable=True, external=True, alias=False)
@given(where=st.sampled_from(["tree", "job"]), depth=st.integers(0, 3),
       after_selection=st.booleans(), writable=st.booleans(), external=st.booleans(), alias=st.booleans())
def test_t2_retention_never_moves_a_live_turns_actual_cwd(where, depth, after_selection, writable, external, alias):
    with tempfile.TemporaryDirectory(prefix="turn-cwd-t2-") as temporary:
        base = Path(temporary)
        with fleet_daemon(base / "state") as (daemon, harness, patch):
            for lane in CODEX:
                measure(daemon, lane)
            tree = daemon.root / "worktrees" / "retired"
            directory = daemon.root / "jobs" / "retired"
            tree.mkdir(parents=True)
            directory.mkdir(parents=True)
            (directory / "stdout").write_text("retired output")
            daemon.store.add_job(job_id="retired", request_id="retired", payload_digest="d", kind="dispatch",
                                 workdir=str(harness.workdir), worktree=str(tree), prompt_path="/prompt",
                                 sandbox="workspace-write", state="succeeded")
            actual = (tree if where == "tree" else directory).joinpath(*(["sub:dir"] * depth))
            actual.mkdir(parents=True, exist_ok=True)
            git(actual, "init", "--quiet", "-b", "task/turn")
            (actual / "f.txt").write_text("turn\n")
            git(actual, "add", ".")
            git(actual, "commit", "--quiet", "-m", "turn")
            if external:
                held = base / "external"
                held.mkdir()
                (held / "f.txt").write_text("turn\n")
                git(actual, "config", "core.worktree", str(held))
            actual = actual.resolve()
            typed = str(actual)
            if alias:
                link = base / "cwd-link"
                link.symlink_to(actual, target_is_directory=True)
                typed = str(link) + "/./"
            patch.setattr(daemon, "_workspace", lambda job: (typed, None, None, []))
            turns, moves, instances = [], [], []
            expected_cwds = {}

            def submit():
                _, _, turn = message_in(
                    daemon, harness, "Move property", workspace=actual,
                    settings={**SETTINGS, "permission": "accept-edits" if writable else "read-only"})
                turns.append(turn)
                expected_cwds[turn] = actual
                daemon._admit_turns()

            class FakeRetirement(rarch.Retirement):
                def begin(self, job, pool):
                    self.journal = {"worktree": job["worktree"], "job_dir": str(directory)}
                    self.renamed = []
                    instances.append(self)
                    if after_selection and not turns:
                        submit()

                def lock(self):
                    pass

                def quarantine(self):
                    # This production guard couples job-folder pins and its fence.
                    self._fence_job_folder()
                    for name in ("worktree", "job_dir"):
                        source = Path(self.journal[name])
                        live = daemon.store.query(
                            "SELECT DISTINCT job_id FROM attempts WHERE state IN "
                            "('reserved','starting','running','finalizing','quarantined')")
                        assert not any(contains(source, expected_cwds[row["job_id"]])
                                       for row in live if row["job_id"] in expected_cwds), (
                                           "moving a live provider cwd", source, live, expected_cwds)
                        target = self.work / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source.rename(target)
                        self.renamed.append((source, target))
                        moves.append(source)

                def rollback(self, *args, **kwargs):
                    for source, target in reversed(self.renamed):
                        target.rename(source)
                    self.renamed.clear()
                    daemon.store.release_leases("retention:retired")
                    return {"conflicts": []}

            patch.setattr(rarch, "Retirement", FakeRetirement)
            if not after_selection:
                submit()
                assert _live(daemon, turns[0]), daemon._holds
                # Blind the census to prove the selecting transaction's tree
                # guard independently; job-directory safety also has a move guard.
                patch.setattr(folders, "turn_folders", lambda read: set())
            driver = selecting_pass(daemon)
            selected = driver._start(daemon._job("retired"), set())
            assert not driver.errors, driver.errors
            turn = turns[0]
            if _live(daemon, turn):
                assert actual.is_dir() and not moves, (where, moves)
                assert selected is None
            for instance in instances:
                instance.rollback()
            daemon._admit_turns()
            assert _live(daemon, turn), daemon._holds
            _end(daemon, turn)
            # It becomes retireable as soon as the turn ends; pins do not leak.
            moves.clear()
            driver = selecting_pass(daemon)
            selected = driver._start(daemon._job("retired"), set())
            assert selected is not None and not driver.errors, driver.progress
            assert moves == [tree, directory]
            selected.rollback()
