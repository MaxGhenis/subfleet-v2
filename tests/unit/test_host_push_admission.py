"""C-8.5, C-6.8, review r3 P2: no push job's workspace stops the admission pass.

`host_push.prepare_workspace` reads the caller's checkout when admission
places a push job. A refusal there (the checkout or Git said no) fails that
job with exit 7 and a notice; the host's own trouble (an `OSError`) is C-6.8's
workspace wait. Before round 3, a `PushError`, `UnicodeDecodeError` or
`configparser` error raised out of the detached pass instead, on every try,
and no detached job queued behind the push job was ever placed.

Each case submits a push job and then an ordinary read-only job, breaks the
push job's checkout as the review did, and runs the real detached pass.
Routing is pinned (`_pick`), as in `test_gate_admission`; nothing launches.
"""
import configparser
from dataclasses import asdict
import errno
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, host_push, protocol
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Decision
from test_host_push import commit, git, submit, worlds  # noqa: F401


def pin_routing(world, patch):
    """Every job routes to the fixture's lane and model, with no probe."""
    core = world.core
    chosen = Decision(("astra",), (), "test-lane", "astra", "test", "test")
    core._pick = lambda *args, **kwargs: chosen
    core._pin_roster = core.store.lane_rows
    core._route_stands = lambda basis, decision: (None, 0, decision)
    core._prepare_route = lambda *args: (set(), None)
    patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    patch.setattr(daemon_module.scheduler, "probe_required", lambda *args: False)


def ordinary(world):
    """A plain read-only job submitted after the push job, outside its checkout."""
    folder = world.repo.parent / "plain"
    folder.mkdir(exist_ok=True)
    args = protocol.SubmitArgs(request_id=str(uuid4()), kind="dispatch", workdir=str(folder),
                               prompt_path=str(world.prompt), task="build", pinned_model="astra",
                               sandbox="read-only", allow_tmp=True)
    return world.core.dispatch("submit", asdict(args))["job_id"]


def replace_git(repo, other):
    """`repo`'s Git directory becomes `other`'s, with `repo`'s origin and branch."""
    remote = git(repo, "remote", "get-url", "origin")
    shutil.rmtree(repo / ".git")
    os.rename(other / ".git", repo / ".git")
    git(repo, "remote", "set-url", "origin", remote)


def shallow(world):
    """`git clone --depth 1` at the same commit: the boundary's parent is missing."""
    copy = world.repo.parent / "shallow-copy"
    git(world.repo.parent, "clone", "-q", "--depth", "1", "--branch", "feature/source",
        world.repo.as_uri(), str(copy))
    assert (copy / ".git/shallow").is_file()
    replace_git(world.repo, copy)


def shared(world):
    """`git clone --shared`: every object is borrowed through alternates."""
    pool, copy = world.repo.parent / "pool.git", world.repo.parent / "shared-copy"
    git(world.repo.parent, "clone", "-q", "--bare", str(world.repo), str(pool))
    git(world.repo.parent, "clone", "-q", "--shared", "--branch", "feature/source", str(pool), str(copy))
    assert (copy / ".git/objects/info/alternates").is_file()
    replace_git(world.repo, copy)


def partial(world):
    """`git clone --filter=blob:none`: the blobs are its promisor remote's."""
    copy = world.repo.parent / "partial-copy"
    git(world.repo, "config", "uploadpack.allowFilter", "true")
    git(world.repo.parent, "clone", "-q", "--filter=blob:none", "--no-checkout", "--branch", "feature/source",
        world.repo.as_uri(), str(copy))
    replace_git(world.repo, copy)


def fifo_head(world):
    head = world.repo / ".git/HEAD"
    head.unlink()
    os.mkfifo(head)


def symlink_head(world):
    head, elsewhere = world.repo / ".git/HEAD", world.repo.parent / "HEAD-elsewhere"
    elsewhere.write_bytes(head.read_bytes())
    head.unlink()
    head.symlink_to(elsewhere)


def append_config(world, data):
    with (world.repo / ".git/config").open("ab") as config:
        config.write(data)


def prepend_config(world, data):
    config = world.repo / ".git/config"
    config.write_bytes(data + config.read_bytes())


def gitfile_loop(world):
    """`.git` a gitfile naming a symlink loop: Python 3.12's `resolve` raises
    RuntimeError there, which no refusal caught."""
    loop = world.repo.parent / "loop"
    loop.symlink_to(world.repo.parent / "loop-back")
    (world.repo.parent / "loop-back").symlink_to(loop)
    shutil.rmtree(world.repo / ".git")
    (world.repo / ".git").write_text(f"gitdir: {loop}\n")


#: case -> (what breaks the checkout after submit, words of the job's notice).
#: The review's nine, then a partial clone and a gitfile naming a symlink loop.
CASES = {
    "shallow": (shallow, "the checkout is shallow"),
    "clone-shared": (shared, "objects/info/alternates"),
    "checkout-deleted": (lambda world: shutil.rmtree(world.repo), "committed Git checkout"),
    "origin-removed": (lambda world: git(world.repo, "remote", "remove", "origin"), "origin URL"),
    "orphan-checkout": (lambda world: git(world.repo, "checkout", "-q", "--orphan", "orphaned"),
                        "HEAD naming a committed baseline"),
    "config-not-utf8": (lambda world: append_config(world, b"\n# \xff\xfe\n"), "UnicodeDecodeError"),
    "config-line-before-section": (lambda world: prepend_config(world, b"stray\n"),
                                   "MissingSectionHeaderError"),
    "head-fifo": (fifo_head, "is not a regular file"),
    "head-symlink": (symlink_head, "is a symlink"),
    "partial-clone": (partial, "partial clone"),
    "gitfile-symlink-loop": (gitfile_loop, "symlink"),
}


def notices(core, job_id):
    return [row["text"] for row in core.store.query("SELECT text FROM notices WHERE job_id=?", (job_id,))]


def placed(core, job_id):
    return [a["state"] for a in core.store.list_attempts(job_id)] == ["reserved"]


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_push_job_whose_checkout_cannot_be_prepared_never_stops_the_jobs_behind_it(worlds, case):
    """The review's reproductions through the real detached pass: the push job
    fails with exit 7 and a notice naming why, and the job behind it is placed
    in the same pass. A second pass changes nothing for either."""
    breaks, words = CASES[case]
    with worlds() as world:
        pin_routing(world, world.patch)
        core = world.core
        if case == "shallow":
            commit(world.repo, "second.txt", "second")      # a parent the shallow clone leaves out
        push_job = submit(world)
        behind = ordinary(world)
        breaks(world)
        for _ in range(2):
            core._admit_kind("detached")                    # never raises
            row = core._job(push_job)
            assert (row["state"], row["rc"]) == ("failed", 7), dict(row)
            assert core.store.list_attempts(push_job) == []
            (text,) = notices(core, push_job)
            assert "host push workspace refused" in text and words in text, text
            assert placed(core, behind), core._holds.get(behind)
        assert not (core.root / "worktrees" / push_job).exists()
        assert core.store.list_leases(push_job) == []


def test_a_checkout_shallow_or_borrowing_objects_is_refused_at_submit(worlds):
    """Review r3 P2's other half: these passed submit and could never push. A
    shallow checkout is refused in place too; one that borrows its objects
    or is a partial clone only when the job works in a copy, since in place
    the job's own Git reads those objects into its bundle."""
    for case, in_place_refused in (("shallow", True), ("clone-shared", False), ("partial-clone", False)):
        breaks, words = CASES[case]
        with worlds() as world:
            if case == "shallow":
                commit(world.repo, "second.txt", "second")
            breaks(world)
            with pytest.raises(AdapterError, match=words) as refused:
                submit(world)
            assert refused.value.code == 7 and "host push refused" in str(refused.value)
            if in_place_refused:
                with pytest.raises(AdapterError, match=words):
                    submit(world, in_place=True)
            else:
                job_id = submit(world, in_place=True)
                assert world.core._job(job_id)["push_branch"] == "jobs/finished"


def test_a_workspace_wait_never_finds_half_a_checkout(worlds):
    """C-6.8: the host's trouble after `git clone` (a full disk) is a workspace
    wait, and the checkout, built in the quarantine, never reached its place.
    The next look prepares it whole and places the job."""
    with worlds() as world:
        pin_routing(world, world.patch)
        core = world.core
        push_job = submit(world)
        destination = core.root / "worktrees" / push_job
        actual = host_push.git
        failures = []

        def full_disk_after_clone(repo, *args, **kwargs):
            result = actual(repo, *args, **kwargs)
            if args[0] == "clone" and not failures:
                failures.append(Path(args[-1]))
                assert Path(args[-1]).is_dir() and not destination.exists()
                raise OSError(errno.ENOSPC, "No space left on device")
            return result
        world.patch.setattr(host_push, "git", full_disk_after_clone)
        core._admit_kind("detached")
        row = core._job(push_job)
        assert (row["state"], row["wait_reason"]) == ("waiting", "workspace"), dict(row)
        assert not destination.exists() and not failures[0].exists()
        with core.store.transaction("test.due") as tx:
            tx.execute("UPDATE jobs SET next_check_at=NULL WHERE job_id=?", (push_job,))
        core._admit_kind("detached")
        assert placed(core, push_job), core._holds.get(push_job)
        assert git(destination, "rev-parse", "HEAD") == core._job(push_job)["workdir_head"]
        assert (destination / ".git/info/exclude").read_text() == f"/{host_push.BUNDLE_PATH[0]}/\n"


def test_git_that_cannot_start_is_the_hosts_trouble_not_the_jobs(worlds):
    """A `PushError` Git raised because it could not start (its cause an
    `OSError`) is a workspace wait, as a failed `worktree add` is."""
    with worlds() as world:
        pin_routing(world, world.patch)
        core = world.core
        push_job = submit(world)
        actual = host_push.git

        def cannot_fork(repo, *args, **kwargs):
            if args[0] == "cat-file":
                try:
                    raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
                except OSError as exc:
                    raise host_push.PushError(f"host git {args[0]} failed (OSError)") from exc
            return actual(repo, *args, **kwargs)
        world.patch.setattr(host_push, "git", cannot_fork)
        core._admit_kind("detached")
        row = core._job(push_job)
        assert (row["state"], row["wait_reason"]) == ("waiting", "workspace"), dict(row)


#: Where a failure is injected, and what is raised there. `checkout_metadata`
#: also stands for an in-place job's `validate_write_location`.
STAGES = ("checkout_metadata", "check_object_store", "git:fsck", "git:clone")


def raised(kind, text):
    if kind == "PushError":
        return host_push.PushError(text)
    if kind == "UnicodeDecodeError":
        return UnicodeDecodeError("utf-8", b"\xff", 0, 1, text or "invalid start byte")
    if kind == "MissingSectionHeaderError":
        return configparser.MissingSectionHeaderError("<string>", 1, text)
    if kind == "RuntimeError":
        return RuntimeError(text)
    if kind == "KeyError":
        return KeyError(text)
    if kind == "TimeoutExpired":
        return subprocess.TimeoutExpired(["git"], 60)
    if kind == "OSError-transient":
        return OSError(errno.ENOSPC, text)
    return OSError(errno.ELOOP, text)


KINDS = ("PushError", "UnicodeDecodeError", "MissingSectionHeaderError", "RuntimeError", "KeyError",
         "TimeoutExpired", "OSError-transient", "OSError-permanent")


#: Each example builds a repository, submits through `ls-remote` and prepares a
#: checkout: a few seconds under load. 4 stages x 8 kinds x 2 placements.
INVARIANT = settings(max_examples=16, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@INVARIANT
@given(stage=st.sampled_from(STAGES), kind=st.sampled_from(KINDS), in_place=st.booleans(),
       text=st.text(st.characters(codec="utf-8") | st.sampled_from(["\udcff", "\ud800"]), max_size=30))
def test_no_push_job_workspace_failure_ever_raises_out_of_an_admission_pass(worlds, stage, kind, in_place, text):
    """The invariant, for any failure anywhere in a push job's workspace
    preparation, any message (a surrogate included: it reaches the notice,
    which SQLite writes as strict UTF-8), in place or not: the pass returns,
    the job behind is placed, and the push job either failed with one notice
    or (the host's transient trouble, not in place) waits for its workspace.
    No half-built checkout is ever left in place."""
    with worlds() as world:
        pin_routing(world, world.patch)
        core = world.core
        push_job = submit(world, in_place=in_place)
        behind = ordinary(world)
        error = raised(kind, text)
        if stage.startswith("git:"):
            actual, verb = host_push.git, stage[4:]

            def failing(repo, *args, **kwargs):
                if args[0] == verb:
                    raise error
                return actual(repo, *args, **kwargs)
            world.patch.setattr(host_push, "git", failing)
        else:
            def failing(*args, **kwargs):
                raise error
            world.patch.setattr(host_push, stage, failing)
        core._admit_kind("detached")
        assert placed(core, behind), core._holds.get(behind)
        row = core._job(push_job)
        reached = not stage.startswith("git:") or not in_place      # an in-place job runs no Git here
        if not reached:
            assert placed(core, push_job)
            return
        if kind == "OSError-transient" and not in_place:
            assert (row["state"], row["wait_reason"]) == ("waiting", "workspace"), dict(row)
            assert notices(core, push_job) == []
        else:
            assert row["state"] == "failed", dict(row)
            assert row["rc"] == (1 if kind.startswith("OSError") and not in_place else 7), dict(row)
            (notice,) = notices(core, push_job)
            notice.encode("utf-8")
        if not in_place:
            assert not (core.root / "worktrees" / push_job).exists()
