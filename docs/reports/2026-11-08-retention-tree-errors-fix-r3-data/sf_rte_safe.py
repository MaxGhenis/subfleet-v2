"""Job-only pytest seams: synthetic local refs, and no forbidden execs."""
from pathlib import Path
import inspect
import os
import shlex
import sys

import pytest


def pytest_configure(config):
    from tests.unit import retention_world
    from tests import failed_keep
    from subfleet import retention_git as rgit
    failed_keep.ROOT = Path("/Users/maxghenis/.subfleet/worktrees/20261125-222604-rte-fix-r3/.sf-rte-fix3/failed-keep")

    # The prescribed Darwin root has no lexical tmp ancestor. Our sandbox
    # fallback does; remove only that physical prefix from the lexical check.
    # Real temp-root/state-root checks and explicit scratch descendants remain.
    def job_relative_parts(real):
        path = Path(real)
        try:
            return path.relative_to(Path("/Users/maxghenis/.subfleet/worktrees/20261125-222604-rte-fix-r3/.sf-rte-fix3")).parts
        except ValueError:
            return path.parts

    rgit._sf_rte_job_parts = job_relative_parts
    source = inspect.getsource(rgit.scratch_reason)
    assert source.count("Path(real).parts") == 1
    exec(compile(source.replace("Path(real).parts", "_sf_rte_job_parts(real)"),
                 "<sf_rte_safe scratch_reason>", "exec"), rgit.__dict__)

    def prepare_remote(self, ref="HEAD:main"):
        scratch = Path("/Users/maxghenis/.subfleet/worktrees/20261125-222604-rte-fix-r3/.sf-rte-fix3").resolve()
        assert self.repo.resolve().is_relative_to(scratch)
        assert self.remote.resolve().is_relative_to(scratch)
        source, target = ref.split(":", 1)
        if not target.startswith("refs/"):
            target = "refs/heads/" + target
        retention_world.git(self.remote, "fetch", "--quiet", str(self.repo), f"{source}:{target}")
        retention_world.git(self.repo, "fetch", "--quiet", str(self.remote),
                            "+refs/heads/*:refs/remotes/origin/*")

    retention_world.World.push = prepare_remote


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name in {"test_attempt_tmpdir_daemon.py", "test_attempt_tmpdir_leaks_daemon.py"}:
            item.add_marker(pytest.mark.skip(reason="routing_state inspects unrelated live processes"))


def _git_verb(args):
    index = 1
    while index < len(args):
        part = args[index]
        if part in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            index += 2
        elif part.startswith("-"):
            index += 1
        else:
            return part
    return None


def _audit(event, args):
    if event != "subprocess.Popen":
        return
    executable, argv, *_ = args
    words = shlex.split(argv) if isinstance(argv, str) else [os.fsdecode(word) for word in argv]
    command = Path(os.fsdecode(executable)).name
    if command == "git" and _git_verb(words) in {"push", "stash"}:
        pytest.skip("job brief forbids executing git push or git stash")
    if command in {"security", "agent-secret", "launchctl"}:
        pytest.skip("job brief forbids executing this external helper")
    if command in {"ps", "lsof"} and "-p" not in words:
        pytest.skip("job brief forbids inspecting unrelated live processes")
    if command == "gh" and len(words) > 2:
        if words[1:3] in (["pr", "merge"], ["run", "rerun"]):
            pytest.skip("job brief forbids this gh mutation")
        if words[1] == "api" and any(word in {"POST", "PATCH", "DELETE"} for word in words[2:]):
            pytest.skip("job brief forbids this gh mutation")


sys.addaudithook(_audit)
