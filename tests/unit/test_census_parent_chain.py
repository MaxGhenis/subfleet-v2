"""D-KILL4, C-5.5: the parent-chain rule, on a simulated host.

A marked member whose parent the census lacks has its parent chain followed.
An ancestor of the daemon's uid whose cwd is in the job's own worktree is added
(and then signalled with the census); the first one that is not is held: it
keeps the census from being empty, but is never added, walked or signalled.
"""

from __future__ import annotations

import pytest

from subfleet import procs
from tests.kill4_world import World

ATTEMPT = "20261020-114117-shell-b/a1"
ROOT = "/fixture/state"


@pytest.fixture
def world(monkeypatch, tmp_path):
    w = World(ATTEMPT, ROOT).install(monkeypatch)
    w.worktree = tmp_path / "worktrees" / "job"
    w.worktree.mkdir(parents=True)
    w.elsewhere = tmp_path / "elsewhere"
    w.elsewhere.mkdir()
    return w


def census(world, *, worktree=True, exclude=frozenset(), held=None, recorded=None):
    rule = procs.ParentChain(str(world.worktree) if worktree else None, frozenset({0, 1, *exclude}),
                             procs.os.geteuid(), held or {})
    return procs.containment(4242, 4242, 4243, ATTEMPT, root=ROOT, parent_chain=rule,
                             recorded_identities=recorded or {})


def driver_shape(world, *, driver_cwd, respawns=False, uid=None):
    """The guardian and its provider are gone. A hidden-marker shell loop, reparented
    to launchd in a session of its own, keeps one marked child alive."""
    driver = world.add(5000, 1, 5000, marked=True, hidden=True, cwd=str(driver_cwd), respawns=respawns,
                       **({"uid": uid} if uid is not None else {}))
    child = world.add(5001, 5000, 5000, marked=True, cwd=str(driver_cwd))
    return driver, child


def test_a_hidden_driver_whose_cwd_is_the_worktree_is_added_above_its_marked_child(world):
    driver, child = driver_shape(world, driver_cwd=world.worktree)
    found = census(world)
    assert found.marker_pids == {child.pid}              # the driver's markers are hidden
    assert found.chain_pids == {driver.pid}
    assert driver.pid in found.live_pids and driver.pid in found.identities
    assert found.to_dict()["chain"] == [f"pid {driver.pid}: ancestor of marked pid {child.pid}, cwd in worktree"]
    assert not found.held_pids and not found.unverifiable
    assert "command" not in str(found.to_dict()) and "SUBFLEET_ATTEMPT" not in str(found.to_dict())


def test_a_driver_in_a_subfolder_of_the_worktree_is_added_and_a_sibling_folder_is_not(world):
    inside = world.worktree / "src"
    inside.mkdir()
    driver_shape(world, driver_cwd=inside)
    assert census(world).chain_pids == {5000}
    sibling = world.worktree.with_name(world.worktree.name + "-other")
    sibling.mkdir()
    world.procs[5000].cwd = str(sibling)
    found = census(world)
    assert not found.chain_pids and found.held_pids == {5000}


@pytest.mark.parametrize("where", ["outside", "in-place"])
def test_an_uncounted_driver_outside_the_worktree_is_held_never_added(world, where):
    driver, child = driver_shape(world, driver_cwd=world.elsewhere)
    found = census(world, worktree=(where == "outside"))
    assert not found.chain_pids
    assert found.held_pids == {driver.pid}
    assert driver.pid not in found.live_pids               # never a signal target
    assert not found.containment_empty                     # but it holds the attempt
    assert found.held_identities[driver.pid] == world.identity(driver.pid)
    assert found.to_dict()["held"] == [
        f"pid {driver.pid}: parent of marked pid {child.pid}, outside the census and the worktree; "
        "held, never signalled"]


def test_a_held_parent_stays_held_by_its_recorded_birth_after_its_children_are_gone(world):
    driver, child = driver_shape(world, driver_cwd=world.elsewhere)
    first = census(world, worktree=False)
    world.procs[child.pid].alive = False
    found = census(world, worktree=False, held=first.held_identities)
    assert found.held_pids == {driver.pid} and not found.live_pids
    assert "recorded parent of a marked process" in found.to_dict()["held"][0]
    # A different birth at that pid is another process: not held.
    world.procs[driver.pid].start = "Sun Sep  6 00:00:00 2026"
    assert census(world, worktree=False, held=first.held_identities).containment_empty


def test_the_chain_stops_at_the_first_ancestor_that_does_not_qualify(world):
    world.add(4990, 1, 4990, hidden=True, cwd=str(world.worktree))       # above the operator's shell
    world.add(4995, 4990, 4995, hidden=True, cwd=str(world.elsewhere))   # an operator's shell
    world.add(5000, 4995, 5000, marked=True, hidden=True, cwd=str(world.worktree))
    world.add(5001, 5000, 5000, marked=True)
    found = census(world)
    assert found.chain_pids == {5000}
    assert found.held_pids == {4995}
    assert 4990 not in found.live_pids | found.held_pids   # nothing above the shell is looked at


def test_several_qualifying_ancestors_are_added_up_to_launchd(world):
    world.add(4990, 1, 4990, hidden=True, cwd=str(world.worktree))
    world.add(5000, 4990, 4990, marked=True, hidden=True, cwd=str(world.worktree))
    world.add(5001, 5000, 4990, marked=True)
    found = census(world)
    assert found.chain_pids == {4990, 5000} and not found.held_pids


@pytest.mark.parametrize("why", ["other-uid", "excluded", "gone-or-exiting"])
def test_an_ancestor_that_cannot_be_ours_is_neither_added_nor_held(world, why, monkeypatch):
    if why == "other-uid":
        driver_shape(world, driver_cwd=world.worktree, uid=0)
        found = census(world)
    elif why == "excluded":
        driver_shape(world, driver_cwd=world.worktree)
        found = census(world, exclude={5000})                # the daemon, or another attempt's guardian
    else:
        driver_shape(world, driver_cwd=world.worktree)
        live = world.kernel

        def exiting():
            kernel = live()
            kernel.exiting.add(5000)
            return kernel
        monkeypatch.setattr(world, "kernel", exiting)
        found = census(world)
    assert not found.chain_pids and not found.held_pids
    assert found.marker_pids == {5001}


def test_a_cwd_holder_that_is_no_ancestor_of_a_marked_member_is_neither_added_nor_held(world):
    world.add(6000, 1, 6000, cwd=str(world.worktree))       # an operator's editor in the worktree
    world.add(5001, 1, 5001, marked=True)                    # a marked runner reparented to launchd
    found = census(world)
    assert 6000 not in found.live_pids | found.held_pids
    assert found.marker_pids == {5001} and not found.chain_pids


def test_parents_inside_the_census_need_no_chain(world):
    world.add(4242, 1, 4242, marked=True)                    # the guardian
    world.add(4243, 4242, 4242, marked=True)                 # the provider
    world.add(4300, 4243, 4300, marked=True, cwd=str(world.elsewhere))
    found = census(world, recorded={4242: world.identity(4242)})
    assert not found.chain_pids and not found.held_pids
    assert found.live_pids == {4242, 4243, 4300}


def test_a_failed_ancestor_read_makes_the_census_unverifiable(world, monkeypatch):
    driver_shape(world, driver_cwd=world.worktree)
    live = world.kernel

    def failing():
        kernel = live()
        kernel.fail.add(("cwd", 5000))
        return kernel
    monkeypatch.setattr(world, "kernel", failing)
    found = census(world)
    assert found.unverifiable and "parent chain cwd inspection unavailable for pid 5000" in found.errors
    assert not found.chain_pids


def test_without_the_rule_the_census_is_unchanged(world):
    driver_shape(world, driver_cwd=world.worktree)
    found = procs.containment(4242, 4242, 4243, ATTEMPT, root=ROOT)
    assert found.marker_pids == {5001} and not found.chain_pids and not found.held_pids
