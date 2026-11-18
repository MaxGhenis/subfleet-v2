"""C-8.4 / R8-F1: keep the metadata locations real Git must traverse."""
import json
import shutil

import pytest

from subfleet import dependencies
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.test_retention_dependency_units import repo
from tests.unit.test_retention_pointer_whitespace import git


@pytest.mark.parametrize("layout", ["admin-path", "gitfile"])
def test_running_job_keeps_tree_containing_required_git_pointer(world, layout, record_property):
    tree = world.job("owner")
    owner, commit = source(world, tree)
    stable = repo(world.base / "independent-storage")
    fetched = git("-C", stable, "fetch", "-q", owner, "HEAD")
    assert fetched.returncode == 0, fetched.stderr
    checkout = world.base / "outside"
    checkout.mkdir()
    shutil.copy2(stable / "f.txt", checkout / "f.txt")
    alias = tree / "storage-alias"
    if layout == "admin-path":
        alias.symlink_to(stable / ".git", target_is_directory=True)
        (checkout / ".git").write_text("gitdir: " + str(alias) + "\n")
    else:
        alias.write_text("gitdir: " + str(stable / ".git") + "\n")
        (checkout / ".git").symlink_to(alias)

    health = git("-C", checkout, "fsck", "--no-dangling")
    before = git("-C", checkout, "show", commit + ":owner-only.txt")
    status = git("-C", checkout, "status", "--porcelain")
    assert health.returncode == before.returncode == status.returncode == 0
    assert before.stdout == b"R5 owner-only storage\n" and status.stdout == b""
    recorded = dependencies.record(str(checkout))
    reader(world, recorded)
    result = maintain(world)
    after = git("-C", checkout, "show", commit + ":owner-only.txt")
    record_property("R8-F1", json.dumps(dict(layout=layout, recorded=recorded, result=result,
                                             git_before=before.returncode, git_after=after.returncode,
                                             error_after=after.stderr.decode()), sort_keys=True))
    assert "owner" not in result["pruned"] and tree.is_dir(), (recorded, result)
    assert after.returncode == 0 and after.stdout == before.stdout, after.stderr
