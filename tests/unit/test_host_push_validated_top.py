"""C-8.5, C-6.5: a push job writes in the checkout top submit validated, and no git
runs on that checkout at submit or at admission.

Review of #141 at 2cbf6fca, P2-B: submit spelled a push job's validated top
(`_row_folder(workdir, top)`) and marked it `unspelled` when the checkout had moved
after `host_push.validate_submit`. Put back before admission, the job went through
admission's generic rediscovery, `git rev-parse --show-toplevel` on the push
checkout, whose own `core.worktree` then replaced the validated write target: the
job took `worktree:…/elsewhere`, not `worktree:…/work`, and a writer holding the
real checkout did not stop it (C-8.5's quarantine-only host Git, C-6.5's one writer
per write target).

Now no push row is `unspelled`, and admission's rediscovery (`_rediscovered`)
refuses a push job too. The reviewer's three probes (review-141-r2/probes/
probe_push.py) are adopted, each run on a row this daemon writes (`fresh`) and on
one as 2cbf6fca wrote it, `unspelled` set (`legacy`), which only the second guard
covers. Their setup asserted the defect's precondition (`unspelled` True); it now
asserts the row as written. Every git process is recorded where it starts
(`subprocess.Popen`), not only `git_toplevel`. The last test pins what a push job
whose checkout is not there at admission does: as on release/217 (4c6a8a9c9), the
in-place recheck refuses it.
"""
import pytest

from subfleet import daemon as daemon_module, folders, host_push
from test_host_push import git, submit, worlds  # noqa: F401
from test_host_push_admission import notices, pin_routing
from tests.unit.test_retention_shared_folders import git_spawns

ROWS = ["fresh", "legacy"]


def plant_unspelled(world, job_id):
    """The row 2cbf6fca wrote for a push top that was away at submit."""
    with world.core.store.transaction("test.legacy_row") as tx:
        tx.execute("UPDATE events SET data_json=json_set(data_json, '$.unspelled', json('true')) "
                   "WHERE job_id=? AND kind='job.submitted'", (job_id,))
    assert world.core._submitted(job_id)["unspelled"] is True


def submit_while_top_is_away(world, row="fresh"):
    """The checkout moves after metadata validation, before row spelling.

    The real validate_submit still reads checkout metadata and queries only the
    fixture's file:// remote. Moving it just before validate_submit returns
    models a move while its quarantine was querying the remote.
    """
    validate = host_push.validate_submit
    away = world.repo.with_name("checkout-away")

    def validate_then_move(*args, **kwargs):
        result = validate(*args, **kwargs)
        world.repo.rename(away)
        return result

    with world.patch.context() as patch:
        patch.setattr(host_push, "validate_submit", validate_then_move)
        try:
            job_id = submit(world, in_place=True)
        finally:
            if away.exists():
                away.rename(world.repo)
    submitted = world.core._submitted(job_id)
    assert "unspelled" not in submitted, submitted
    assert submitted["write_target"] == folders.canonical(world.repo), submitted
    if row == "legacy":
        plant_unspelled(world, job_id)
    return job_id


def on_checkout(world, spawned):
    """The git processes in `spawned` that ran on the push checkout, wherever it
    was moved, or on the folder its `core.worktree` names: started there (`-C`,
    or their working directory), or naming it as their git directory or work tree."""
    places = [folders.canonical(path) for path in
              (world.repo, world.repo.with_name("checkout-away"), world.repo.parent / "elsewhere")]
    found = []
    for argv, at in spawned:
        named = [at, *(arg.split("=", 1)[1] for arg in argv if arg.startswith(("--git-dir=", "--work-tree=")))]
        if any(folders.within(folders.canonical(path), place) for path in named for place in places):
            found.append((argv, at))
    return found


@pytest.mark.parametrize("row", ROWS)
def test_a_push_job_runs_no_git_on_its_checkout_at_submit_or_admission(worlds, row):
    """The review's probe (probe_push.py:34), with every git process recorded."""
    with worlds() as world:
        pin_routing(world, world.patch)
        calls = []
        inspect = daemon_module.git_toplevel

        def watch(workdir, **kwargs):
            calls.append(str(workdir))
            return inspect(workdir, **kwargs)

        world.patch.setattr(daemon_module, "git_toplevel", watch)
        spawned = git_spawns(world.patch)
        job_id = submit_while_top_is_away(world, row)
        assert calls == [], "submit must not run Git in a push checkout"
        assert on_checkout(world, spawned) == [], spawned
        world.core._admit_kind("detached")
        assert world.core.store.list_attempts(job_id)
        assert calls == [], "admission must not run Git in a push checkout"
        assert on_checkout(world, spawned) == [], spawned


@pytest.mark.parametrize("row", ROWS)
def test_a_push_job_keeps_the_validated_top_as_its_lease_target(worlds, row):
    """The review's probe (probe_push.py:52): the checkout's `core.worktree` names
    another folder, and the job's `worktree:` lease is still on the checkout."""
    with worlds() as world:
        pin_routing(world, world.patch)
        elsewhere = world.repo.parent / "elsewhere"
        elsewhere.mkdir()
        git(world.repo, "config", "core.worktree", str(elsewhere))
        spawned = git_spawns(world.patch)
        job_id = submit_while_top_is_away(world, row)
        expected = folders.canonical(world.repo)
        assert world.core._submitted(job_id)["write_target"] == expected
        world.core._admit_kind("detached")
        (attempt,) = world.core.store.list_attempts(job_id)
        assert attempt["state"] == "reserved"
        held = {lease["lease_key"] for lease in world.core.store.list_leases(job_id)}
        assert folders.exclusive_key(expected) in held, held
        assert folders.exclusive_key(folders.canonical(elsewhere)) not in held, held
        assert on_checkout(world, spawned) == [], spawned


@pytest.mark.parametrize("row", ROWS)
def test_a_push_job_waits_for_a_writer_on_its_validated_top(worlds, row):
    """The review's probe (probe_push.py:69): a writer holding the checkout keeps
    the push job waiting, whatever the checkout's `core.worktree` names."""
    with worlds() as world:
        pin_routing(world, world.patch)
        elsewhere = world.repo.parent / "elsewhere"
        elsewhere.mkdir()
        git(world.repo, "config", "core.worktree", str(elsewhere))
        spawned = git_spawns(world.patch)
        job_id = submit_while_top_is_away(world, row)
        expected = folders.canonical(world.repo)
        assert world.core.store.acquire_lease(folders.exclusive_key(expected), "external-writer")
        world.core._admit_kind("detached")
        assert world.core.store.list_attempts(job_id) == [], (
            "a push writer must wait for the lease on the checkout it actually writes in"
        )
        hold = world.core._holds.get(job_id) or {}
        assert hold.get("reason") == "lease-held" and hold.get("leases") == [folders.exclusive_key(expected)], hold
        assert on_checkout(world, spawned) == [], spawned


@pytest.mark.parametrize("when", ["away-since-submit", "moved-after-submit"])
def test_a_push_job_whose_checkout_is_not_there_at_admission_is_refused_as_on_release_217(worlds, when):
    """#158's reviewed behavior, unchanged (release/217 at 4c6a8a9c9 passes this
    test): an in-place push job whose checkout is not there at admission is refused
    by admission's in-place recheck (`host_push.validate_write_location`, which reads
    the checkout's metadata as data). The job fails with exit 7 and a notice saying
    its workspace was refused, with no attempt and no lease, and no git ran on the
    checkout. Away since submit (moved just after validation, never back), or moved
    after a submit that saw it."""
    with worlds() as world:
        pin_routing(world, world.patch)
        spawned = git_spawns(world.patch)
        away = world.repo.with_name("checkout-away")
        if when == "away-since-submit":
            validate = host_push.validate_submit

            def validate_then_move(*args, **kwargs):
                result = validate(*args, **kwargs)
                world.repo.rename(away)
                return result

            with world.patch.context() as patch:
                patch.setattr(host_push, "validate_submit", validate_then_move)
                job_id = submit(world, in_place=True)
        else:
            job_id = submit(world, in_place=True)
            world.repo.rename(away)
        assert "unspelled" not in world.core._submitted(job_id)
        for _ in range(2):
            world.core._admit_kind("detached")                    # never raises
            row = world.core._job(job_id)
            assert (row["state"], row["rc"]) == ("failed", 7), dict(row)
            assert world.core.store.list_attempts(job_id) == []
            assert world.core.store.list_leases(job_id) == []
            (text,) = notices(world.core, job_id)
            assert "workspace refused" in text and "committed Git checkout" in text, text
        assert on_checkout(world, spawned) == [], spawned
