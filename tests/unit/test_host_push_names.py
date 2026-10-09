"""C-8.5, review P2-2 and P3-6: branch and repository names as GitHub and a
case-insensitive clone read them.

Protected and reserved names compare casefolded and NFC-normalized; a new branch
may not fold onto an existing remote branch; ownership is one key per
repository however its URL is spelled. Real submit, acceptance and local push;
a GitHub spelling reaches the local origin only inside the test's Git wrapper.
"""
import pytest

from subfleet import host_push
from subfleet.adapters.base import AdapterError
from test_host_push import accept, assert_failed, commit, git, remote_heads, submit, worlds  # noqa: F401


@pytest.mark.parametrize("branch", ["mаin", "MAİN", "ｍａｉｎ", "main​",
                                    "re⁄lease/217", "саse/тwin"])
def test_a_unicode_lookalike_is_refused_at_submit(worlds, branch):
    """Cyrillic, dotted capital I, fullwidth, zero-width, fraction-slash: a
    branch is ASCII, so no lookalike of a protected name gets past submit."""
    with worlds() as world:
        with pytest.raises(AdapterError) as refused:
            submit(world, branch)
        assert refused.value.code == 7 and "invalid push branch" in str(refused.value)
        assert not world.core.store.query("SELECT * FROM jobs")


@pytest.mark.parametrize("existing", ["jobs/Feature", "JOBS/FEATURE"])
def test_a_case_twin_of_an_existing_unowned_branch_is_refused_at_submit(worlds, existing):
    with worlds() as world:
        git(world.repo, "push", "origin", f"{world.base}:refs/heads/{existing}")
        with pytest.raises(AdapterError) as refused:
            submit(world, "jobs/feature")
        assert refused.value.code == 7 and "differs only in case" in str(refused.value)
        assert existing in str(refused.value)


def test_a_case_twin_created_after_submit_is_refused_at_push(worlds):
    with worlds() as world:
        submit(world, "jobs/feature")
        commit(world.repo)
        git(world.repo, "push", "origin", f"{world.base}:refs/heads/jobs/Feature")
        row = accept(world)
        assert "differs only in case" in row["push_error"] and not row["push_sha"]
        assert remote_heads(world) == {"refs/heads/trunk": world.base, "refs/heads/jobs/Feature": world.base}


def test_a_protected_pattern_matches_in_any_case_at_push(worlds):
    """Policy can change between submit and push; the recheck folds too."""
    with worlds() as world:
        submit(world, "jobs/Finished")
        world.core.policy["push"]["protected"] = ["JOBS/*"]
        assert_failed(world, accept(world), "protected")


def fail_first_push(world):
    """The first `git push` fails after the daemon's claim, as a transport would."""
    inner = host_push.git
    failed = []

    def wrapped(repo, *args, **kwargs):
        if args[0] == "push" and not failed:
            failed.append(args[1])
            raise host_push.PushError("host git push failed (exit 128)")
        return inner(repo, *args, **kwargs)
    world.patch.setattr(host_push, "git", wrapped)


def test_the_family_owns_its_branch_in_the_spelling_it_claimed(worlds):
    """A child may not publish its family's branch under another case: refused
    at submit once the branch exists, and at push while only the claim does."""
    with worlds() as world:
        parent = submit(world, "jobs/finished")
        first = commit(world.repo)
        assert accept(world)["push_sha"] == first
        with pytest.raises(AdapterError, match="differs only in case"):
            submit(world, "jobs/FINISHED", parent_job_id=parent)
    with worlds() as world:
        parent = submit(world, "jobs/finished")
        commit(world.repo)
        fail_first_push(world)
        assert "exit 128" in accept(world)["push_error"]
        child = submit(world, "jobs/FINISHED", parent_job_id=parent)
        commit(world.repo, text="second")
        row = accept(world, child)
        assert "owns the branch as 'jobs/finished'" in row["push_error"] and not row["push_sha"]
        assert remote_heads(world) == {"refs/heads/trunk": world.base}


#: Review P3-6: three spellings of one repository.
SPELLINGS = ["https://github.com/MaxGhenis/x.git", "git@github.com:MaxGhenis/x", "https://github.com/maxghenis/X"]


def test_three_spellings_of_one_repository_racing_for_one_branch_have_one_owner(worlds):
    """Three independent families, one per spelling, submit for one new branch.
    The first claims it and its push fails after the claim, so the branch is
    still absent; the other two are refused as another family's, not let in
    under a second key for the same repository."""
    with worlds(allowed_remotes=["github.com/MaxGhenis/*"]) as world:
        world.aliases.update(dict.fromkeys(SPELLINGS, world.remote))
        jobs = []
        for spelling in SPELLINGS:
            git(world.repo, "remote", "set-url", "origin", spelling)
            jobs.append(submit(world, "jobs/race"))
        assert [world.core._job(job)["push_remote"] for job in jobs] == SPELLINGS
        commit(world.repo, text="raced")
        fail_first_push(world)
        assert "exit 128" in accept(world, jobs[0])["push_error"]
        for job in jobs[1:]:
            row = accept(world, job)
            assert "another job family" in (row["push_error"] or "") and not row["push_sha"], (
                f"{job} was not refused as another family's: pushed {row['push_sha']}")
        assert "refs/heads/jobs/race" not in remote_heads(world)
        owners = world.core.store.query("SELECT * FROM job_owned_branches")
        assert [(o["remote_key"], o["branch_key"], o["family_job_id"], o["sha"]) for o in owners] == [
            ("github.com/maxghenis/x", "jobs/race", jobs[0], None)]


def test_a_family_continues_its_branch_through_another_spelling(worlds):
    """One owner key: a child whose checkout spells the origin differently
    fast-forwards its family's branch, as with the same spelling."""
    with worlds(allowed_remotes=["github.com/MaxGhenis/*"]) as world:
        world.aliases.update(dict.fromkeys(SPELLINGS, world.remote))
        git(world.repo, "remote", "set-url", "origin", SPELLINGS[0])
        parent = submit(world, "jobs/continued")
        first = commit(world.repo)
        assert accept(world)["push_sha"] == first
        git(world.repo, "remote", "set-url", "origin", SPELLINGS[2])
        child = submit(world, "jobs/continued", parent_job_id=parent)
        second = commit(world.repo, text="second")
        row = accept(world, child)
        assert row["push_sha"] == second and not row["push_error"]
        assert remote_heads(world)["refs/heads/jobs/continued"] == second
