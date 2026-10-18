"""C-8.5, review r2 P3: submit reads checkout metadata as data, never waiting.

`checkout_metadata` reads a checkout's origin and HEAD with
`retention_fs.read_regular` and looks at `.git` with lstat. A FIFO, a symlink,
a directory or an oversize file at any path it reads refuses promptly. A FIFO
planted as a nested `.git/config` had held submit, `_submit_lock` and every
submit after it; a submit refused that way now leaves the lock free.
"""
from dataclasses import asdict
import os
from types import SimpleNamespace
import threading
import time
from uuid import uuid4

import pytest

from subfleet import host_push, protocol
from subfleet.adapters.base import AdapterError
from test_host_push import commit, git, submit, worlds  # noqa: F401

#: How long a refusal may take. It is a few system calls; the bound only has to
#: tell a refusal from a read that waits forever, on a loaded test machine.
PROMPT_S = 30


def release(fifo):
    """Let a reader blocked opening `fifo` go: open the other end and close it,
    so it reads end-of-file. Nothing is waiting: nothing to do."""
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


def promptly(call, fifo=None):
    """What `call` raised or returned, failing if it is still running after
    PROMPT_S. A call still blocked on `fifo` is let go first, so no thread is
    left holding what it held."""
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            outcome["error"] = exc
    thread = threading.Thread(target=run, name="checkout-metadata", daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(PROMPT_S)
    blocked = thread.is_alive()
    if blocked and fifo is not None:
        release(fifo)
        thread.join(PROMPT_S)
    assert not blocked, f"still blocked after {PROMPT_S} s reading checkout metadata"
    return outcome, time.monotonic() - started


@pytest.fixture
def checkouts(tmp_path):
    """A main checkout and a linked worktree, as Git writes them, whose own
    config (`extensions.worktreeConfig`) names a different origin."""
    main, linked = tmp_path / "main", tmp_path / "linked"
    main.mkdir()
    git(main, "init", "-b", "feature/source", "--template=")
    head = commit(main, "baseline.txt", "baseline")
    git(main, "remote", "add", "origin", "file:///origin.git")
    git(main, "config", "extensions.worktreeConfig", "true")
    git(main, "worktree", "add", "-b", "feature/linked", str(linked))
    admin = main / ".git/worktrees/linked"
    (admin / "config.worktree").write_text('[remote "origin"]\n\turl = file:///linked-origin.git\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return SimpleNamespace(main=main, linked=linked, admin=admin, head=head, elsewhere=elsewhere)


def test_both_layouts_read_as_data(checkouts):
    """The control: what Git wrote reads, loose and packed, main and linked."""
    c = checkouts
    assert host_push.checkout_metadata(c.main) == ("file:///origin.git", c.head, "feature/source", c.main)
    assert host_push.checkout_metadata(c.linked) == ("file:///linked-origin.git", c.head, "feature/linked", c.linked)
    git(c.main, "pack-refs", "--all")
    assert not (c.main / ".git/refs/heads/feature/source").exists()
    assert host_push.checkout_metadata(c.main)[1] == c.head
    assert host_push.checkout_metadata(c.linked)[1] == c.head
    host_push.validate_write_location(c.main)
    host_push.validate_write_location(c.linked)


#: name -> (the checkout read, the file, its cap): every file `checkout_metadata` reads.
PATHS = {
    "gitfile": ("linked", lambda c: c.linked / ".git", host_push.POINTER_CAP),
    "commondir": ("linked", lambda c: c.admin / "commondir", host_push.POINTER_CAP),
    "config": ("main", lambda c: c.main / ".git/config", host_push.CONFIG_CAP),
    "config.worktree": ("linked", lambda c: c.admin / "config.worktree", host_push.CONFIG_CAP),
    "HEAD": ("main", lambda c: c.main / ".git/HEAD", host_push.POINTER_CAP),
    "loose-ref": ("main", lambda c: c.main / ".git/refs/heads/feature/source", host_push.POINTER_CAP),
    "packed-refs": ("main", lambda c: c.main / ".git/packed-refs", host_push.PACKED_REFS_CAP),
}
#: kind -> the refusal's words. `.git` itself is looked at with lstat.
KINDS = {"fifo": "is not a regular file", "symlink": "is a symlink",
         "directory": "is not a regular file", "oversize": "is larger than"}
GITFILE = "must be a directory or a gitdir file"


def plant(path, kind, cap, elsewhere):
    """Put `kind` where `path` is. Followed, or read whole, each would still
    name a good checkout: only reading as data refuses it."""
    content = path.read_bytes()
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        (elsewhere / path.name).write_bytes(content)
        path.symlink_to(elsewhere / path.name)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(content + b"\n" * (cap + 1 - len(content)))


# An empty directory replacing a linked checkout's gitfile has no local config
# and refuses promptly. The valid main-checkout directory is covered above.
@pytest.mark.parametrize("name,kind", [(name, kind) for name in sorted(PATHS) for kind in KINDS])
def test_each_metadata_path_refuses_promptly(checkouts, name, kind):
    which, where, cap = PATHS[name]
    if name == "packed-refs":
        git(checkouts.main, "pack-refs", "--all")
    path = where(checkouts)
    plant(path, kind, cap, checkouts.elsewhere)
    checkout = getattr(checkouts, which)
    outcome, _ = promptly(lambda: host_push.validate_write_location(checkout),
                          fifo=path if kind == "fifo" else None)
    error = outcome.get("error")
    assert isinstance(error, AdapterError) and error.code == 7 and error.fix, outcome
    reason = GITFILE if name == "gitfile" and kind in ("fifo", "symlink") else KINDS[kind]
    if name == "gitfile" and kind == "directory":
        reason = "is missing"
        assert str(path / "config") in str(error), str(error)
    assert reason in str(error) and path.name in str(error), str(error)
    with pytest.raises(host_push.PushError, match=reason):
        host_push.checkout_metadata(checkout)


def submit_in(world, workdir):
    args = protocol.SubmitArgs(request_id=str(uuid4()), kind="dispatch", workdir=str(workdir),
        prompt_path=str(world.prompt), task="build", pinned_model="astra", push_branch="jobs/nested",
        allow_tmp=True, sandbox="workspace-write")
    return world.core.dispatch("submit", asdict(args))


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_submit_refused_on_planted_metadata_leaves_the_submit_lock_free(worlds, kind):
    """The review's case through real submit: a checkout nested in the caller's,
    whose `.git/config` a job could have written. Refused promptly, and the
    next submit runs."""
    with worlds() as world:
        nested = world.repo / "sub"
        nested.mkdir()
        git(nested, "init", "-b", "feature/nested", "--template=")
        commit(nested, "nested.txt", "nested")
        git(nested, "remote", "add", "origin", world.remote)
        assert submit_in(world, nested)["job_id"]            # a good checkout as it stands
        config = nested / ".git/config"
        plant(config, kind, host_push.CONFIG_CAP, world.repo.parent)
        outcome, _ = promptly(lambda: submit_in(world, nested), fifo=config if kind == "fifo" else None)
        error = outcome.get("error")
        assert isinstance(error, AdapterError) and error.code == 7 and error.fix, outcome
        assert "host push refused" in str(error) and KINDS[kind] in str(error), str(error)
        assert world.core._submit_lock.acquire(timeout=PROMPT_S), "the refused submit kept _submit_lock"
        world.core._submit_lock.release()
        job_id = submit(world)
        assert world.core._job(job_id)["push_branch"] == "jobs/finished"
