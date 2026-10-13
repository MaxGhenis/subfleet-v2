"""The mirror's scope rules and removal plan, as pure functions: C-23.28a.

`subfleet/sessions/prune.py` decides where each session belongs and which
copies outside that scope a pass may remove. Its invariants hold for every
input, so they are stated as Hypothesis properties over random stores (up to
six folders of three kinds, up to seven sessions, random flags, times, home,
loaded folder and rules), with a few examples for the readings that matter
most. `test_mirror_prune.py` holds the real mirror to the same plan on real
files.

Invariants:

* **Never the only copy.** Every session keeps at least one copy.
* **The newest survives.** The newest copy of every session (by its own
  activity times) is never removed.
* **Hands off.** Nothing is removed from the loaded folder or the home
  folder, and nothing at all while the loaded folder is unknown, removal is
  off, the pass is blocked, or the session's flags are unsettled.
* **No churn.** Every removed copy is outside its session's scope, and stays
  outside it once the plan is applied, so the spread never puts it back.
* **Idempotence.** Applying a plan leaves nothing for the next plan to remove.
* **Accounting.** Copies outside their scope = newest kept + held + removed.
* **Monotone in the thresholds.** Longer thresholds never remove more.
* **Off is off.** With every rule off, every session belongs everywhere and
  nothing is outside.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from subfleet.sessions import prune
from subfleet.sessions.folder_kinds import SETTLED, SWITCH, UNKNOWN

NOW = 1_790_000_000.0          # 2026-09-21 13:33 UTC, the planner's clock
DAY = prune.DAY_S
FOLDERS = ("a/o1", "b/o2", "c/o3", "d/o1", "e/o2", "f/o4")


@st.composite
def worlds(draw):
    """A random store the planner could meet, with flags agreeing per session.

    Flag sync runs before the plan and holds or rewrites any session whose
    copies disagree, so a session reaching the plan unheld has one archived
    and one starred value; `held` covers the rest.
    """
    count = draw(st.integers(min_value=1, max_value=len(FOLDERS)))
    folders = FOLDERS[:count]
    kinds = {folder: draw(st.sampled_from((SETTLED, SWITCH, UNKNOWN))) for folder in folders}
    plain = [folder for folder in folders if kinds[folder] != SWITCH]
    home = draw(st.one_of(st.none(), st.sampled_from(plain))) if plain else None
    loaded = draw(st.one_of(st.none(), st.sampled_from(folders)))
    sessions = draw(st.integers(min_value=1, max_value=7))
    copies, openable, archived_at, held = [], set(), {}, set()
    for number in range(sessions):
        identity = f"s{number}"
        archived = draw(st.booleans())
        starred = draw(st.booleans()) and draw(st.booleans())
        disagree = draw(st.booleans()) and draw(st.booleans())
        where = draw(st.lists(st.sampled_from(folders), min_size=1, max_size=len(folders),
                              unique=True))
        for place in where:
            flag = (not archived) if disagree and draw(st.booleans()) else archived
            copies.append(prune.Copy(
                folder=place, name=f"local_{identity}.json", identity=identity,
                archived=flag, starred=starred,
                activity_ms=int((NOW - draw(st.floats(0, 60)) * DAY) * 1000),
                mtime=NOW - draw(st.floats(0, 60)) * DAY))
        if disagree or draw(st.booleans()) and draw(st.booleans()):
            held.add(identity)
        if draw(st.booleans()) or draw(st.booleans()):
            openable.add(identity)
        if draw(st.booleans()) and draw(st.booleans()):
            archived_at[identity] = NOW - draw(st.floats(0, 60)) * DAY
    rules = prune.Rules(skip_switch_folders=draw(st.booleans()),
                        archive_days=draw(st.sampled_from((0.0, 1.0, 7.0, 14.0, 30.0))),
                        dead_days=draw(st.sampled_from((0.0, 1.0, 7.0, 30.0))),
                        prune=draw(st.booleans()) or draw(st.booleans()))
    situation = prune.Situation(folders=folders, kinds=kinds, home=home, loaded=loaded,
                                openable=frozenset(openable), archived_at=archived_at, now=NOW)
    return copies, situation, rules, frozenset(held)


def by_session(copies):
    groups = {}
    for copy in copies:
        groups.setdefault(copy.identity, []).append(copy)
    return groups


def apply(copies, decided):
    gone = {(item.folder, item.name) for item in decided.removals}
    return [copy for copy in copies if (copy.folder, copy.name) not in gone]


SETTINGS = settings(max_examples=400, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(worlds())
def test_never_the_only_copy_and_the_newest_survives(world):
    """C-23.28a: every session keeps a copy, and its newest copy is never removed."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    before, after = by_session(copies), by_session(apply(copies, decided))
    for identity, group in before.items():
        assert after.get(identity), f"{identity} lost its last copy"
        assert (max(copy.activity_ms for copy in after[identity])
                == max(copy.activity_ms for copy in group)), f"{identity} lost its newest copy"


@SETTINGS
@given(worlds())
def test_hands_off_the_loaded_folder_the_home_and_unsettled_sessions(world):
    """C-23.28a: the loaded folder and the home are never touched, and nothing
    is removed while the load is unknown, removal is off, or flags are unsettled."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    for removal in decided.removals:
        assert removal.folder != situation.loaded
        assert removal.folder != situation.home
        assert removal.identity not in held
    if situation.loaded is None or not rules.prune:
        assert not decided.removals
    assert not prune.plan(copies, situation, rules, held=held,
                          blocked="the inventory is incomplete this pass").removals


@SETTINGS
@given(worlds())
def test_no_churn_what_is_removed_stays_out_of_scope(world):
    """C-23.28a: a removed copy is outside its session's scope before and after
    the plan is applied, so neither pass spreads it back."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    before, after = by_session(copies), by_session(apply(copies, decided))
    for removal in decided.removals:
        for group in (before, after):
            placed = prune.scope(removal.identity, group[removal.identity], situation, rules)
            assert removal.folder not in placed.folders


@SETTINGS
@given(worlds())
def test_a_plan_applied_leaves_nothing_to_remove(world):
    """C-23.28a: idempotence. The next pass over what a plan left removes nothing."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    again = prune.plan(apply(copies, decided), situation, rules, held=held)
    assert again.removals == []


@SETTINGS
@given(worlds())
def test_every_copy_outside_its_scope_is_accounted_for(world):
    """C-23.28a: outside = the newest kept + held + removed, and removals name a
    keeper that survives and is at least as new."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    assert (sum(decided.outside.values())
            == sum(decided.kept.values()) + sum(decided.held.values()) + len(decided.removals))
    left = {(copy.folder, copy.name): copy for copy in apply(copies, decided)}
    for removal in decided.removals:
        keeper = left[removal.keeper]
        assert keeper.identity == removal.identity
        assert keeper.activity_ms >= removal.newness
    assert sum(decided.sessions.values()) == len(by_session(copies))


@SETTINGS
@given(worlds(), st.sampled_from((1.0, 7.0, 30.0)))
def test_longer_thresholds_never_remove_more(world, extra):
    """C-23.28a: monotone. Raising either threshold, or seeding switch folders,
    can only keep more."""
    copies, situation, rules, held = world
    removed = {(item.folder, item.name) for item in
               prune.plan(copies, situation, rules, held=held).removals}
    stricter = []
    if rules.archive_days:
        stricter.append(replace(rules, archive_days=rules.archive_days + extra))
    if rules.dead_days:
        stricter.append(replace(rules, dead_days=rules.dead_days + extra))
    stricter.append(replace(rules, skip_switch_folders=False))
    for other in stricter:
        fewer = {(item.folder, item.name) for item in
                 prune.plan(copies, situation, other, held=held).removals}
        assert fewer <= removed


@SETTINGS
@given(worlds())
def test_with_every_rule_off_every_session_belongs_everywhere(world):
    """C-23.28a: off is off. The mirror spreads and keeps as it did before."""
    copies, situation, rules, held = world
    off = prune.Rules(prune=rules.prune)
    decided = prune.plan(copies, situation, off, held=held)
    assert not decided.removals and not decided.outside
    for identity, group in by_session(copies).items():
        placed = prune.scope(identity, group, situation, off)
        assert placed.folders == frozenset(situation.folders)


@SETTINGS
@given(worlds())
def test_scope_is_one_of_three_shapes(world):
    """C-23.28a: a live session belongs in every seeded folder, an old archived
    one in the home only, an old dead one in the home only (or nowhere)."""
    copies, situation, rules, _held = world
    seeded = frozenset(folder for folder in situation.folders
                       if prune.seeded(folder, situation, rules))
    for identity, group in by_session(copies).items():
        placed = prune.scope(identity, group, situation, rules)
        if placed.kind in ("live", "dead-young"):
            assert placed.folders == seeded
        else:
            assert placed.folders == (frozenset({situation.home}) if situation.home
                                      else frozenset())
        if placed.kind == "archived":
            assert all(copy.archived for copy in group)
            assert not any(copy.starred for copy in group)
            assert identity in situation.openable
            assert situation.now - prune.quiet_since(identity, group, situation) \
                >= rules.archive_days * DAY > 0
        if placed.kind == "dead":
            assert identity not in situation.openable and rules.dead_days > 0
    for folder in situation.folders:
        if situation.kinds.get(folder) == SWITCH and rules.skip_switch_folders:
            assert folder not in seeded


@SETTINGS
@given(worlds())
def test_the_sessions_left_unplanned_have_nothing_outside_their_scope(world):
    """C-23.28a: the mirror plans only sessions `may_reach_outside` names, and
    decides every scope from a `Summary`; skipping the others loses nothing,
    and the summary decides as the copies do."""
    copies, situation, rules, held = world
    for identity, group in by_session(copies).items():
        summary = prune.summarize(group, situation)
        placed = prune.scope_of(identity, summary, situation, rules)
        assert placed == prune.scope(identity, group, situation, rules)
        if not prune.may_reach_outside(placed, summary, rules):
            assert not prune.plan(group, situation, rules, held=held).outside


# --- examples -----------------------------------------------------------------

def situation(**changes) -> prune.Situation:
    base = prune.Situation(
        folders=("home/h", "work/w", "other/o", "work/h"),
        kinds={"home/h": SETTLED, "work/w": SETTLED, "other/o": UNKNOWN, "work/h": SWITCH},
        home="home/h", loaded="work/w", openable=frozenset({"s"}), archived_at={}, now=NOW)
    return replace(base, **changes)


def copy(folder: str, *, days: float = 30, archived: bool = True, starred: bool = False,
         written: float | None = None) -> prune.Copy:
    stamp = NOW - days * DAY
    return prune.Copy(folder=folder, name="local_s.json", identity="s", archived=archived,
                      starred=starred, activity_ms=int(stamp * 1000),
                      mtime=NOW - (written if written is not None else days) * DAY)


RULES = prune.Rules(skip_switch_folders=True, archive_days=14, dead_days=7, prune=True)


def test_an_old_archived_session_keeps_its_home_and_the_loaded_copy():
    """C-23.28a: the example the rule exists for. Quiet 30 days and archived in
    every account: the home keeps it, the loaded folder keeps it until the
    app leaves it, and the other copies go."""
    copies = [copy(folder) for folder in situation().folders]
    decided = prune.plan(copies, situation(), RULES)
    assert {item.folder for item in decided.removals} == {"other/o", "work/h"}
    assert decided.held == {prune.HELD_LOADED: 1}
    assert all(item.keeper == ("home/h", "local_s.json") for item in decided.removals)


def test_the_archive_stamp_starts_the_clock_and_a_re_save_does_not():
    """C-23.28a: archiving leaves the activity times alone (850 records on
    2026-09-22 were a month quiet when the app archived them), so the merge
    base's archive stamp starts the clock. The app also re-saves records it
    holds (191 archived ones in the loaded folder within a day on
    2026-09-25), so a fresh file write alone does not."""
    quiet = [copy(folder, days=40) for folder in situation().folders]
    stamped = situation(archived_at={"s": NOW - 3 * DAY})
    assert prune.scope("s", quiet, stamped, RULES).kind == "live"
    assert [item.folder for item in prune.plan(quiet, stamped, RULES).removals] == ["work/h"], \
        "live: only the switch folder's copy goes"
    resaved = [copy(folder, days=40, written=0.5) for folder in situation().folders]
    assert prune.scope("s", resaved, situation(archived_at={"s": NOW - 20 * DAY}),
                       RULES).kind == "archived"
    focused = [copy(folder, days=2) for folder in situation().folders]
    assert prune.scope("s", focused, situation(archived_at={"s": NOW - 20 * DAY}),
                       RULES).kind == "live", "looking at a session is activity"


def test_a_newer_copy_outside_the_home_is_kept_beside_it():
    """C-23.28a: copies of one session differ in activity; when the home's is
    older, the newest stays too, and only the rest go."""
    copies = [copy("home/h", days=40), copy("work/w", days=30), copy("other/o", days=20),
              copy("work/h", days=35)]
    decided = prune.plan(copies, situation(loaded="work/w"), RULES)
    assert decided.kept == {prune.ARCHIVED: 1}
    assert {item.folder for item in decided.removals} == {"work/h"}
    assert decided.removals[0].keeper == ("other/o", "local_s.json")


@pytest.mark.parametrize("change", [
    {"archived": False}, {"starred": True}, {"days": 3},
])
def test_what_keeps_an_archived_session_everywhere(change):
    """C-23.28a: unarchived, starred, or quiet less than the rule: live."""
    copies = [copy(folder, **change) for folder in situation().folders]
    placed = prune.scope("s", copies, situation(), RULES)
    assert placed.kind == "live"
    assert placed.folders == frozenset({"home/h", "work/w", "other/o"})
    assert {item.folder for item in prune.plan(copies, situation(), RULES).removals} == {"work/h"}


def test_without_a_home_an_archived_session_stays_everywhere():
    """C-23.28a: the archived rule needs a home; without one it does nothing."""
    copies = [copy(folder) for folder in situation().folders]
    assert prune.scope("s", copies, situation(home=None), RULES).kind == "live"


def test_an_old_dead_session_keeps_its_newest_copy():
    """C-23.28a: a dead session (no transcript) is never spread; once quiet
    `mirror_dead_days` it keeps the home's copy, or else the newest."""
    dead = situation(openable=frozenset(), home=None)
    copies = [copy("home/h", days=20), copy("work/w", days=10), copy("other/o", days=30)]
    decided = prune.plan(copies, replace(dead, loaded="work/h"), RULES)
    assert {item.folder for item in decided.removals} == {"home/h", "other/o"}
    young = [copy("home/h", days=2), copy("other/o", days=3)]
    assert prune.scope("s", young, dead, RULES).kind == "dead-young"
