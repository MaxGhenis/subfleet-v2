"""Review property for PR #131 (not part of the PR): C-5.5's group source.

For every process table and every set of recorded identities: a live member of
the attempt's process group is in the census, unless the leader's pid now holds
another process (XNU never hands out a pid that names a live group or session,
so then the group is someone else's). Recorded identities may only add writers.
"""
from hypothesis import given, settings, strategies as st

from subfleet import procs

BOOT = "boot-1"
PGID = 100


@st.composite
def worlds(draw):
    pids = draw(st.lists(st.integers(101, 140), unique=True, max_size=8))
    leader = draw(st.sampled_from(["gone", "same", "reused"]))
    rows = {}
    if leader != "gone":
        rows[PGID] = (1, PGID, "Ss", "g-start" if leader == "same" else "g-new")
    for pid in pids:
        ppid = draw(st.sampled_from([1, PGID, *pids]))
        in_group = draw(st.booleans()) and leader != "reused"
        rows[pid] = (ppid, PGID if in_group else pid, draw(st.sampled_from(["S", "R", "Z"])),
                     draw(st.sampled_from(["s1", "s2"])))
    recorded = {PGID: procs.ProcessIdentity(PGID, BOOT, "g-start")}
    for pid in draw(st.lists(st.integers(101, 140), unique=True, max_size=8)):
        recorded[pid] = procs.ProcessIdentity(pid, BOOT, draw(st.sampled_from(["s1", "s2"])))
    return rows, recorded, leader


@settings(max_examples=500, deadline=None)
@given(world=worlds())
def test_every_live_group_member_is_in_the_census_unless_the_leader_pid_was_reused(world):
    rows, recorded, leader = world
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(procs, "snapshot", lambda: procs.ProcessTable(dict(rows), boot_id=BOOT))
        mp.setattr(procs, "_read", lambda argv, **kw: "")
        census = procs.containment(PGID, PGID, None, "attempt-x", root="/r", recorded=recorded)
    finally:
        mp.undo()
    members = {pid for pid, row in rows.items() if row[1] == PGID and not row[2].startswith("Z")}
    if leader != "reused":
        assert members <= census.live_pids, (members - census.live_pids, rows, recorded)
    if members:
        assert not census.verified_empty or leader == "reused"
